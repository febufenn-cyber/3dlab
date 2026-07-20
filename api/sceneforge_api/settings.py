"""API settings — everything comes from the environment (no secrets in repo).

Defaults are dev-friendly (SQLite + local storage + in-memory queue) so
`uvicorn sceneforge_api.main:app` works with zero setup; docker-compose sets
the production values (Postgres, Redis/Valkey, R2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("SCENEFORGE_DATABASE_URL", "sqlite+aiosqlite:///./sceneforge.db")
    )
    redis_url: str = field(default_factory=lambda: _env("SCENEFORGE_REDIS_URL", ""))
    # storage: "local" (dev) or "s3" (R2 / B2 / MinIO — anything S3-compatible)
    storage_backend: str = field(default_factory=lambda: _env("SCENEFORGE_STORAGE", "local"))
    local_storage_dir: str = field(
        default_factory=lambda: _env("SCENEFORGE_LOCAL_STORAGE_DIR", "./scenes")
    )
    s3_bucket: str = field(default_factory=lambda: _env("SCENEFORGE_S3_BUCKET", ""))
    s3_endpoint_url: str = field(default_factory=lambda: _env("SCENEFORGE_S3_ENDPOINT", ""))
    s3_region: str = field(default_factory=lambda: _env("SCENEFORGE_S3_REGION", "auto"))
    # public base for asset links (Cloudflare R2 public bucket / CDN domain)
    public_asset_base: str = field(default_factory=lambda: _env("SCENEFORGE_PUBLIC_ASSET_BASE", ""))
    presign_expiry_s: int = field(
        default_factory=lambda: int(_env("SCENEFORGE_PRESIGN_EXPIRY_S", "3600"))
    )
    max_upload_mb: int = field(default_factory=lambda: int(_env("SCENEFORGE_MAX_UPLOAD_MB", "500")))
    webhook_secret: str = field(default_factory=lambda: _env("SCENEFORGE_WEBHOOK_SECRET", ""))
    # Reliability reaper (reaper.py): scenes stuck in `processing` longer than
    # processing_timeout_s are failed honestly (worker_timeout); scenes sitting
    # in `queued` longer than queued_timeout_s are re-enqueued (heals lost
    # enqueues / queue restarts). interval 0 disables the loop (tests).
    processing_timeout_s: int = field(
        default_factory=lambda: int(_env("SCENEFORGE_PROCESSING_TIMEOUT_S", "3600"))
    )
    queued_timeout_s: int = field(
        default_factory=lambda: int(_env("SCENEFORGE_QUEUED_TIMEOUT_S", "1800"))
    )
    reaper_interval_s: int = field(
        default_factory=lambda: int(_env("SCENEFORGE_REAPER_INTERVAL_S", "300"))
    )


def get_settings() -> Settings:
    return Settings()
