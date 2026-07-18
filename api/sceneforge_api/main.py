"""SceneForge API (brief §6 Phase 2).

Endpoints:
  POST /v1/scenes                  → scene id + presigned upload URL
  POST /v1/scenes/{id}/uploaded    → confirm upload, enqueue processing
  GET  /v1/scenes/{id}             → state + quality report + asset URLs
  GET  /v1/scenes/{id}/semantic    → the semantic JSON document
  GET  /healthz
Dev-only (local storage backend): PUT /v1/_local-upload/{key},
GET /v1/_local-download/{key}.

Auth: `Authorization: Bearer sk_...` (hashed keys in Postgres, minted with
`python -m sceneforge_api.keycli`). Worker callbacks use the same key scheme.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select

from .db import ApiKey, Database, Scene, find_key, new_scene_id
from .queue import make_queue
from .settings import Settings, get_settings
from .storage import LocalStorage, make_storage
from .webhooks import deliver

log = logging.getLogger(__name__)


# --- request/response models --------------------------------------------------


class SceneCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200, description="e.g. flat.mp4")
    content_type: str = Field(default="video/mp4", max_length=100)
    webhook_url: Optional[HttpUrl] = None


class SceneCreateResponse(BaseModel):
    scene_id: str
    state: str
    upload_url: str
    upload_expires_s: int
    next: str = "PUT the video to upload_url, then POST /v1/scenes/{scene_id}/uploaded"


class SceneStatusResponse(BaseModel):
    scene_id: str
    state: str
    quality_report: Optional[dict[str, Any]] = None
    assets: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None
    created_at: str
    updated_at: str


class WorkerResult(BaseModel):
    """Posted by GPU workers to /v1/scenes/{id}/_result.

    Must live at module scope: FastAPI resolves the (PEP 563 stringified)
    annotation via module globals — a closure-local class silently degrades
    the body parameter into a query parameter.
    """

    state: str = Field(pattern="^(processing|succeeded|failed)$")
    quality_report: Optional[dict[str, Any]] = None
    semantic: Optional[dict[str, Any]] = None
    assets: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None


# --- app factory ----------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
    queue=None,
    storage=None,
) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_url)
    queue = queue if queue is not None else make_queue(settings.redis_url)
    storage = storage or make_storage(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.create_all()
        yield
        await queue.close()
        await database.dispose()

    app = FastAPI(title="SceneForge API", version="1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.queue = queue
    app.state.storage = storage

    async def get_session():
        async with database.sessionmaker() as session:
            yield session

    async def require_key(
        session=Depends(get_session), authorization: str = Header(default="")
    ) -> ApiKey:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Missing 'Authorization: Bearer sk_...' header")
        key = await find_key(session, authorization.removeprefix("Bearer ").strip())
        if key is None:
            raise HTTPException(401, "Invalid or inactive API key")
        return key

    # --- routes -------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": "sceneforge-api"}

    @app.post("/v1/scenes", response_model=SceneCreateResponse, status_code=201)
    async def create_scene(
        body: SceneCreateRequest,
        session=Depends(get_session),
        key: ApiKey = Depends(require_key),
    ):
        scene_id = new_scene_id()
        ext = "".join(c for c in body.filename.rsplit(".", 1)[-1].lower() if c.isalnum())[:5] or "mp4"
        video_key = f"{scene_id}/video.{ext}"
        upload_url = storage.presign_upload(
            video_key, body.content_type, settings.presign_expiry_s
        )
        scene = Scene(
            id=scene_id,
            api_key_id=key.id,
            state="awaiting_upload",
            video_key=video_key,
            webhook_url=str(body.webhook_url) if body.webhook_url else None,
        )
        session.add(scene)
        await session.commit()
        return SceneCreateResponse(
            scene_id=scene_id,
            state="awaiting_upload",
            upload_url=upload_url,
            upload_expires_s=settings.presign_expiry_s,
        )

    async def _get_owned_scene(scene_id: str, session, key: ApiKey) -> Scene:
        scene = (
            await session.execute(select(Scene).where(Scene.id == scene_id))
        ).scalar_one_or_none()
        if scene is None or scene.api_key_id != key.id:
            # 404 for both cases: never confirm another tenant's scene ids.
            raise HTTPException(404, "Scene not found")
        return scene

    @app.post("/v1/scenes/{scene_id}/uploaded", response_model=SceneStatusResponse)
    async def confirm_upload(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        scene = await _get_owned_scene(scene_id, session, key)
        if scene.state != "awaiting_upload":
            raise HTTPException(409, f"Scene is {scene.state}, not awaiting_upload")
        if not storage.exists(scene.video_key):
            raise HTTPException(400, "Video not found in storage — upload it first")
        scene.state = "queued"
        await session.commit()
        await queue.enqueue(scene_id)
        return _status_response(scene)

    @app.get("/v1/scenes/{scene_id}", response_model=SceneStatusResponse)
    async def get_scene(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        scene = await _get_owned_scene(scene_id, session, key)
        return _status_response(scene)

    @app.get("/v1/scenes/{scene_id}/semantic")
    async def get_semantic(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        scene = await _get_owned_scene(scene_id, session, key)
        if scene.state != "succeeded" or not scene.semantic:
            raise HTTPException(409, f"Scene is {scene.state}; semantic JSON not available")
        return scene.semantic

    # --- worker callbacks (same bearer auth; workers get their own key) ------

    @app.post("/v1/scenes/{scene_id}/_result", include_in_schema=False)
    async def worker_result(
        scene_id: str,
        body: WorkerResult,
        session=Depends(get_session),
        key: ApiKey = Depends(require_key),
    ):
        if not key.is_worker:
            # Customer keys must never be able to rewrite scene results —
            # not even for their own scenes.
            raise HTTPException(403, "Worker key required")
        scene = (
            await session.execute(select(Scene).where(Scene.id == scene_id))
        ).scalar_one_or_none()
        if scene is None:
            raise HTTPException(404, "Scene not found")
        scene.state = body.state
        if body.quality_report is not None:
            scene.quality_report = body.quality_report
        if body.semantic is not None:
            scene.semantic = body.semantic
        if body.assets is not None:
            scene.assets = body.assets
        scene.error_code = body.error_code
        await session.commit()
        if body.state in ("succeeded", "failed") and scene.webhook_url:
            payload = {
                "event": f"scene.{body.state}",
                "scene": _status_response(scene).model_dump(mode="json"),
            }
            ok = await deliver(scene.webhook_url, payload, settings.webhook_secret)
            if not ok:
                log.warning("webhook delivery to %s failed permanently", scene.webhook_url)
        return {"ok": True}

    # --- dev-only local storage endpoints ------------------------------------

    if isinstance(storage, LocalStorage):

        @app.put("/v1/_local-upload/{key:path}", include_in_schema=False)
        async def local_upload(key: str, request: Request):
            data = await request.body()
            if len(data) > settings.max_upload_mb * 1024 * 1024:
                raise HTTPException(413, "Upload too large")
            try:
                storage.put_bytes(key, data)
            except ValueError:
                raise HTTPException(400, "Bad key")
            return {"ok": True, "bytes": len(data)}

        @app.get("/v1/_local-download/{key:path}", include_in_schema=False)
        async def local_download(key: str):
            try:
                if not storage.exists(key):
                    raise HTTPException(404, "Not found")
                return Response(
                    content=storage.get_bytes(key), media_type="application/octet-stream"
                )
            except ValueError:
                raise HTTPException(400, "Bad key")

    def _status_response(scene: Scene) -> SceneStatusResponse:
        assets = None
        if scene.assets:
            assets = {
                name: storage.public_url(key) if not str(key).startswith("http") else key
                for name, key in scene.assets.items()
            }
        return SceneStatusResponse(
            scene_id=scene.id,
            state=scene.state,
            quality_report=scene.quality_report,
            assets=assets,
            error_code=scene.error_code,
            created_at=scene.created_at.isoformat(),
            updated_at=scene.updated_at.isoformat(),
        )

    return app


app = create_app()
