"""arq worker settings.

  arq sceneforge_worker.arq_worker.WorkerSettings

Run this process wherever the GPU adapter needs it (see run_job.py docstring).
"""

from __future__ import annotations

import os

from arq.connections import RedisSettings

from .run_job import process_scene


class WorkerSettings:
    functions = [process_scene]
    redis_settings = RedisSettings.from_dsn(
        os.environ.get("SCENEFORGE_REDIS_URL", "redis://localhost:6379")
    )
    max_jobs = int(os.environ.get("SCENEFORGE_WORKER_MAX_JOBS", "1"))  # 1 GPU job at a time
    job_timeout = int(os.environ.get("SCENEFORGE_JOB_TIMEOUT_S", "2400"))  # 40 min hard cap
    keep_result = 3600
