"""Job queue abstraction.

Architecture rule (brief §5): the queue lives on the OCI VM (Redis/Valkey via
docker-compose); GPU workers PULL jobs, so the GPU provider is swappable
without touching the API. arq (MIT) is the Redis queue; InMemoryQueue backs
tests and zero-dependency dev.
"""

from __future__ import annotations

import abc
import logging

log = logging.getLogger(__name__)

JOB_NAME = "process_scene"


class JobQueue(abc.ABC):
    @abc.abstractmethod
    async def enqueue(self, scene_id: str) -> None: ...

    async def close(self) -> None:  # pragma: no cover - trivial default
        return None


class ArqQueue(JobQueue):
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(RedisSettings.from_dsn(self.redis_url))
        return self._pool

    async def enqueue(self, scene_id: str) -> None:
        import uuid

        pool = await self._get_pool()
        # Unique job ids: a deterministic id would make requeue-after-failure a
        # silent no-op while arq keeps the previous job's result (keep_result).
        # Double-processing safety lives in the DB state machine instead
        # (atomic transitions in main.py) — a duplicate job wastes GPU minutes
        # at worst, never corrupts state.
        job = await pool.enqueue_job(
            JOB_NAME, scene_id, _job_id=f"job:{scene_id}:{uuid.uuid4().hex[:8]}"
        )
        if job is None:  # can't happen with unique ids; fail loudly if it does
            raise RuntimeError(f"arq refused to enqueue scene {scene_id}")
        log.info("enqueued %s", scene_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


class InMemoryQueue(JobQueue):
    """Records enqueued ids; tests assert on them, dev logs them."""

    def __init__(self):
        self.jobs: list[str] = []

    async def enqueue(self, scene_id: str) -> None:
        self.jobs.append(scene_id)
        log.info("in-memory queue: %s (no worker attached)", scene_id)


def make_queue(redis_url: str) -> JobQueue:
    return ArqQueue(redis_url) if redis_url else InMemoryQueue()
