"""SceneForge API (brief §6 Phase 2).

Endpoints:
  POST /v1/scenes                  → scene id + presigned upload URL
  POST /v1/scenes/{id}/uploaded    → confirm upload, enqueue processing
  POST /v1/scenes/{id}/requeue     → re-run a failed scene
  GET  /v1/scenes/{id}             → state + quality report + asset URLs
  GET  /v1/scenes/{id}/semantic    → the semantic JSON document
  GET  /healthz

Reliability invariants (enforced here + by reaper.py):
  * state transitions are ATOMIC (guarded UPDATE ... WHERE state=...), so a
    double /uploaded or concurrent requeue can never double-enqueue;
  * an enqueue failure never 500s a confirmed upload — the scene stays
    `queued` and the reaper re-enqueues it;
  * no scene sits in `processing`/`queued` forever (reaper timeouts);
  * a late `processing` report can't regress a terminal scene (409), but a
    late TERMINAL report always wins — a real result beats a timeout.
Dev-only (local storage backend): PUT /v1/_local-upload/{key},
GET /v1/_local-download/{key}.

Auth: `Authorization: Bearer sk_...` (hashed keys in Postgres, minted with
`python -m sceneforge_api.keycli`). Worker callbacks use the same key scheme.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select, update

from .db import ApiKey, Database, Scene, find_key, new_scene_id, utcnow
from .observability import MetricsMiddleware, configure_logging, metrics
from .queue import make_queue
from .reaper import reaper_loop
from .scene_views import SceneStatusResponse, status_response
from .security import BodyLimitMiddleware, RateLimiter
from .settings import Settings, get_settings
from .storage import LocalStorage, make_storage
from .webhooks import deliver

# Uploads never traverse the API (presigned direct-to-R2), so only safe video
# container types are accepted for the STORED object's Content-Type — this
# prevents a tenant from having their upload served back as text/html etc.
_ALLOWED_CONTENT_TYPES = {
    "video/mp4", "video/quicktime", "video/webm", "video/x-matroska",
    "video/x-m4v", "video/3gpp", "application/octet-stream",
}
_NON_TERMINAL_STATES = ("awaiting_upload", "queued", "processing")

log = logging.getLogger(__name__)


# --- request/response models --------------------------------------------------


class SceneCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200, description="e.g. flat.mp4")
    content_type: str = Field(default="video/mp4", max_length=100)
    webhook_url: Optional[HttpUrl] = Field(default=None)
    # webhook_url is validated to http(s) at delivery time by the SSRF guard
    # (webhooks.is_url_allowed) and bounded by HttpUrl parsing here.


class SceneCreateResponse(BaseModel):
    scene_id: str
    state: str
    upload_url: str
    upload_expires_s: int
    next: str = "PUT the video to upload_url, then POST /v1/scenes/{scene_id}/uploaded"


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
    configure_logging(settings.log_format, settings.log_level)
    database = database or Database(settings.database_url)
    queue = queue if queue is not None else make_queue(settings.redis_url)
    storage = storage or make_storage(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.create_all()
        reaper_stop = asyncio.Event()
        reaper_task = None
        if settings.reaper_interval_s > 0:
            reaper_task = asyncio.create_task(
                reaper_loop(database.sessionmaker, queue, storage, settings, reaper_stop)
            )
        yield
        if reaper_task is not None:
            reaper_stop.set()
            await reaper_task
        await queue.close()
        await database.dispose()

    app = FastAPI(title="SceneForge API", version="1.0", lifespan=lifespan)
    # Order: MetricsMiddleware added last ⇒ outermost, so it times and counts
    # everything including body-limit 413s. BodyLimit stays inner.
    if settings.cors_origins.strip():
        # Explicit origins only (browser dashboards). allow_credentials stays
        # False: auth is the Bearer header, never cookies, so no CSRF surface.
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=3600,
        )
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_body_kb * 1024)
    app.add_middleware(MetricsMiddleware)
    create_limiter = RateLimiter(
        capacity=settings.create_rate_capacity, refill_per_sec=settings.create_rate_per_sec
    )
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
        """Liveness: cheap, always-ok. Uptime monitors hit this."""
        return {"ok": True, "service": "sceneforge-api"}

    @app.get("/readyz")
    async def readyz(response: Response):
        """Readiness: actually probes the DB (and queue if configured). Returns
        503 with per-component booleans when a dependency is down so an
        orchestrator/monitor can tell 'process alive' from 'can serve'."""
        components: dict[str, bool] = {}
        try:
            async with database.sessionmaker() as session:
                await session.execute(select(1))
            components["database"] = True
        except Exception:
            log.warning("readyz: database check failed", exc_info=True)
            components["database"] = False
        # Queue: only assert readiness for a real broker; the in-memory/dev
        # queue is always "ready". A ping method is optional.
        ping = getattr(queue, "ping", None)
        if ping is not None:
            try:
                await ping()
                components["queue"] = True
            except Exception:
                log.warning("readyz: queue check failed", exc_info=True)
                components["queue"] = False
        ready = all(components.values())
        if not ready:
            response.status_code = 503
        return {"ready": ready, "components": components}

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(
        response: Response, authorization: str = Header(default=""), token: str = "",
    ):
        """Prometheus exposition. Token-gated: with no SCENEFORGE_METRICS_TOKEN
        configured it 404s (metrics disabled), so scene/tenant counts are never
        exposed by default. Scrapers pass the token as a bearer or ?token=."""
        if not settings.metrics_token:
            raise HTTPException(404, "Not found")
        supplied = authorization.removeprefix("Bearer ").strip() or token
        if supplied != settings.metrics_token:
            raise HTTPException(401, "Invalid metrics token")
        # Refresh scene-state gauges at scrape time (pull, so no drift).
        try:
            async with database.sessionmaker() as session:
                rows = (
                    await session.execute(
                        select(Scene.state, func.count()).group_by(Scene.state)
                    )
                ).all()
            seen = {state: n for state, n in rows}
            for state in ("awaiting_upload", "queued", "processing", "succeeded", "failed"):
                metrics.scenes_by_state.set(float(seen.get(state, 0)), state=state)
        except Exception:
            log.warning("metrics: scene-state refresh failed", exc_info=True)
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/v1/scenes", response_model=SceneCreateResponse, status_code=201)
    async def create_scene(
        body: SceneCreateRequest,
        session=Depends(get_session),
        key: ApiKey = Depends(require_key),
    ):
        # Per-key rate limit (token bucket) — cheap flood control.
        if not create_limiter.allow(key.id):
            metrics.rejections.inc(kind="rate_limit")
            raise HTTPException(429, "Rate limit exceeded; slow down scene creation")
        # Per-key cap on outstanding non-terminal scenes — bounds row growth
        # and presigned-URL generation from a single (possibly leaked) key.
        outstanding = (
            await session.execute(
                select(func.count())
                .select_from(Scene)
                .where(Scene.api_key_id == key.id, Scene.state.in_(_NON_TERMINAL_STATES))
            )
        ).scalar_one()
        if outstanding >= settings.max_outstanding_scenes:
            metrics.rejections.inc(kind="outstanding_cap")
            raise HTTPException(
                429,
                f"Too many in-flight scenes ({outstanding}); finish or let them "
                "expire before creating more",
            )
        # Only safe video container types can become the stored object's
        # Content-Type; anything else is coerced to an inert octet-stream.
        content_type = (
            body.content_type
            if body.content_type in _ALLOWED_CONTENT_TYPES
            else "application/octet-stream"
        )
        scene_id = new_scene_id()
        ext = "".join(c for c in body.filename.rsplit(".", 1)[-1].lower() if c.isalnum())[:5] or "mp4"
        video_key = f"{scene_id}/video.{ext}"
        upload_url = storage.presign_upload(
            video_key, content_type, settings.presign_expiry_s
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

    async def _atomic_transition(session, scene_id: str, from_state: str, **values) -> bool:
        """Guarded UPDATE … WHERE state=from_state; True iff this call won the race."""
        result = await session.execute(
            update(Scene)
            .where(Scene.id == scene_id, Scene.state == from_state)
            .values(updated_at=utcnow(), **values)
        )
        await session.commit()
        return result.rowcount == 1

    async def _enqueue_resilient(scene_id: str) -> None:
        """An enqueue failure must never 500 a committed `queued` transition —
        the scene is durably queued in the DB and the reaper re-enqueues it."""
        try:
            await queue.enqueue(scene_id)
        except Exception:
            log.warning(
                "enqueue of %s failed (queue down?); scene stays queued — "
                "the reaper will re-enqueue it", scene_id, exc_info=True,
            )

    @app.post("/v1/scenes/{scene_id}/uploaded", response_model=SceneStatusResponse)
    async def confirm_upload(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        scene = await _get_owned_scene(scene_id, session, key)
        if scene.state != "awaiting_upload":
            raise HTTPException(409, f"Scene is {scene.state}, not awaiting_upload")
        if not storage.exists(scene.video_key):
            raise HTTPException(400, "Video not found in storage — upload it first")
        if not await _atomic_transition(session, scene_id, "awaiting_upload", state="queued"):
            raise HTTPException(409, "Scene is no longer awaiting_upload")
        await _enqueue_resilient(scene_id)
        await session.refresh(scene)
        return status_response(scene, storage)

    @app.post("/v1/scenes/{scene_id}/requeue", response_model=SceneStatusResponse)
    async def requeue_scene(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        """Re-run a FAILED scene (e.g. after worker_timeout or a re-shoot-free
        transient failure). Only `failed` is requeueable: succeeded scenes are
        final, and in-flight states are owned by the worker/reaper."""
        scene = await _get_owned_scene(scene_id, session, key)
        if not await _atomic_transition(
            session, scene_id, "failed", state="queued", error_code=None
        ):
            raise HTTPException(
                409, f"Scene is {scene.state}; only failed scenes can be requeued"
            )
        await _enqueue_resilient(scene_id)
        await session.refresh(scene)
        return status_response(scene, storage)

    @app.get("/v1/scenes/{scene_id}", response_model=SceneStatusResponse)
    async def get_scene(
        scene_id: str, session=Depends(get_session), key: ApiKey = Depends(require_key)
    ):
        scene = await _get_owned_scene(scene_id, session, key)
        return status_response(scene, storage)

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
        if body.state == "processing" and scene.state in ("succeeded", "failed"):
            # A late 'processing' heartbeat must not regress a terminal scene
            # (e.g. after the reaper timed it out). A late TERMINAL result is
            # still accepted below — a real outcome beats a timeout.
            raise HTTPException(409, f"Scene already terminal ({scene.state})")
        scene.state = body.state
        if body.quality_report is not None:
            scene.quality_report = body.quality_report
        if body.semantic is not None:
            scene.semantic = body.semantic
        if body.assets is not None:
            scene.assets = body.assets
        scene.error_code = body.error_code
        await session.commit()
        metrics.worker_results.inc(state=body.state)
        if body.state in ("succeeded", "failed") and scene.webhook_url:
            payload = {
                "event": f"scene.{body.state}",
                "scene": status_response(scene, storage).model_dump(mode="json"),
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

    return app


app = create_app()
