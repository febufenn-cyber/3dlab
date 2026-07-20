"""Stuck-scene reaper: no scene may sit in a non-terminal state forever.

Two failure classes it heals, both honestly (never inventing success):

* ``processing`` older than ``processing_timeout_s`` → the worker died or hung
  without reporting. The scene is failed with ``worker_timeout`` and the
  customer webhook fires — the same contract as any other failure.
* ``queued`` older than ``queued_timeout_s`` → the enqueue was lost (queue
  restart, enqueue error at /uploaded time) or the backlog stalled. The scene
  is re-enqueued; its ``updated_at`` is touched first so one scene is retried
  at most once per timeout window, not once per reaper tick.

``reap_once`` is a pure-ish, directly-testable coroutine; ``reaper_loop`` is
the lifespan wrapper (interval 0 disables it — tests drive reap_once
themselves).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from .db import Scene, utcnow
from .queue import JobQueue
from .scene_views import status_response
from .settings import Settings
from .storage import Storage
from .webhooks import deliver

log = logging.getLogger(__name__)


async def reap_once(
    sessionmaker,
    queue: JobQueue,
    storage: Storage,
    settings: Settings,
    now: dt.datetime | None = None,
) -> dict:
    now = now or utcnow()
    proc_cutoff = now - dt.timedelta(seconds=settings.processing_timeout_s)
    queued_cutoff = now - dt.timedelta(seconds=settings.queued_timeout_s)
    # Abandoned uploads: the presigned URL is already dead, so a scene still in
    # awaiting_upload past its expiry will never progress — expire it so these
    # rows can't accumulate forever from a create-and-abandon flood.
    upload_cutoff = now - dt.timedelta(seconds=settings.presign_expiry_s)
    batch = max(1, settings.reaper_batch)

    timed_out: list[tuple[str, str | None]] = []  # (scene_id, webhook_url)
    webhook_payloads: list[tuple[str, dict]] = []
    requeue_ids: list[str] = []
    expired_uploads: list[str] = []

    async with sessionmaker() as session:
        stuck = (
            (
                await session.execute(
                    select(Scene)
                    .where(Scene.state == "processing", Scene.updated_at < proc_cutoff)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for scene in stuck:
            scene.state = "failed"
            scene.error_code = "worker_timeout"
            scene.quality_report = {
                "status": "failed",
                "failure": {
                    "reason_code": "worker_timeout",
                    "message": (
                        f"No worker result within {settings.processing_timeout_s}s; "
                        "the job was abandoned. Re-submit with POST /v1/scenes/{id}/requeue."
                    ),
                },
            }
            scene.updated_at = now
            timed_out.append((scene.id, scene.webhook_url))

        stale = (
            (
                await session.execute(
                    select(Scene)
                    .where(Scene.state == "queued", Scene.updated_at < queued_cutoff)
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for scene in stale:
            scene.updated_at = now  # rate-limits retries to one per timeout window
            requeue_ids.append(scene.id)

        abandoned = (
            (
                await session.execute(
                    select(Scene)
                    .where(
                        Scene.state == "awaiting_upload", Scene.updated_at < upload_cutoff
                    )
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
        for scene in abandoned:
            scene.state = "failed"
            scene.error_code = "upload_abandoned"
            scene.updated_at = now
            expired_uploads.append(scene.id)

        await session.commit()

        # Build webhook payloads inside the session (attributes are loaded).
        for scene in stuck:
            if scene.webhook_url:
                webhook_payloads.append(
                    (
                        scene.webhook_url,
                        {
                            "event": "scene.failed",
                            "scene": status_response(scene, storage).model_dump(mode="json"),
                        },
                    )
                )

    for sid in requeue_ids:
        try:
            await queue.enqueue(sid)
        except Exception:  # queue still down — next tick retries after the window
            log.warning("reaper: re-enqueue of %s failed; will retry next window", sid)

    for url, payload in webhook_payloads:
        ok = await deliver(url, payload, settings.webhook_secret)
        if not ok:
            log.warning("reaper: webhook delivery to %s failed permanently", url)

    if timed_out or requeue_ids or expired_uploads:
        log.info(
            "reaper: failed %d stuck processing %s; re-enqueued %d stale queued %s; "
            "expired %d abandoned uploads %s",
            len(timed_out), [s for s, _ in timed_out], len(requeue_ids), requeue_ids,
            len(expired_uploads), expired_uploads,
        )
    return {
        "failed_processing": [s for s, _ in timed_out],
        "requeued": requeue_ids,
        "expired_uploads": expired_uploads,
    }


async def reaper_loop(
    sessionmaker, queue: JobQueue, storage: Storage, settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Run reap_once every reaper_interval_s until `stop` is set."""
    while not stop.is_set():
        try:
            await reap_once(sessionmaker, queue, storage, settings)
        except Exception:
            log.exception("reaper tick failed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.reaper_interval_s)
        except (asyncio.TimeoutError, TimeoutError):
            continue
