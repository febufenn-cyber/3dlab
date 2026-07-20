"""The arq task: turn a queued scene into results.

Flow: mark processing → presign video GET + artifact PUTs → hand the BuildJob
to the configured GPUWorker adapter → POST the outcome to the API's _result
endpoint (which also fires the customer webhook).

This module runs wherever the arq worker runs: on the GPU box itself
(local adapter) or on the OCI VM as a thin dispatcher (modal/hf_space).
"""

from __future__ import annotations

import logging
import os

import httpx

from sceneforge_api.settings import get_settings
from sceneforge_api.storage import make_storage

from .adapters import make_worker
from .jobspec import ARTIFACT_NAMES, BuildJob

log = logging.getLogger(__name__)

API_BASE = os.environ.get("SCENEFORGE_API_BASE", "http://localhost:8000")
WORKER_API_KEY = os.environ.get("SCENEFORGE_WORKER_API_KEY", "")


def _api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WORKER_API_KEY}"}


async def _post_result(scene_id: str, payload: dict, retries: int = 3) -> None:
    """Report to the API with retries — a blip in the control plane must not
    turn a finished GPU job into a lost result."""
    import asyncio

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(retries):
            try:
                r = await client.post(
                    f"{API_BASE}/v1/scenes/{scene_id}/_result",
                    json=payload, headers=_api_headers(),
                )
                r.raise_for_status()
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise  # 4xx is a contract problem, retrying won't help
                last_exc = e
            except httpx.HTTPError as e:
                last_exc = e
            await asyncio.sleep(2 ** attempt)
    raise last_exc  # type: ignore[misc]


async def process_scene(ctx: dict, scene_id: str) -> str:
    """arq entrypoint (see arq_worker.WorkerSettings)."""
    settings = get_settings()
    storage = make_storage(settings)

    video_key = f"{scene_id}/video.mp4"
    # The API stores the exact key; probe common extensions for robustness.
    for ext in ("mp4", "mov", "m4v", "webm"):
        candidate = f"{scene_id}/video.{ext}"
        if storage.exists(candidate):
            video_key = candidate
            break

    await _post_result(scene_id, {"state": "processing"})
    try:
        job = BuildJob(
            scene_id=scene_id,
            video_url=storage.presign_download(video_key, settings.presign_expiry_s),
            artifact_put_urls={
                name: storage.presign_upload(
                    f"{scene_id}/assets/{name}", "application/octet-stream",
                    settings.presign_expiry_s,
                )
                for name in ARTIFACT_NAMES
            },
            quality=os.environ.get("SCENEFORGE_QUALITY", "standard"),
        )
        worker = make_worker()
        log.info("[%s] dispatching to %s adapter", scene_id, worker.name)
        outcome = worker.build(job)

        assets = {
            name.rsplit(".", 1)[0].replace(".", "_"): f"{scene_id}/assets/{name}"
            for name in outcome.artifacts_uploaded
        }
        await _post_result(
            scene_id,
            {
                "state": outcome.status,
                "quality_report": outcome.quality_report,
                "semantic": outcome.semantic,
                "assets": assets,
                "error_code": outcome.error_code,
            },
        )
        log.info("[%s] %s (%s)", scene_id, outcome.status, outcome.error_code or "ok")
        return outcome.status
    except Exception as e:  # never leave a scene stuck in `processing`
        log.exception("[%s] worker crashed", scene_id)
        await _post_result(
            scene_id,
            {
                "state": "failed",
                "error_code": "worker_crash",
                "quality_report": {"status": "failed", "failure": {"message": str(e)[:1000]}},
            },
        )
        raise
