"""Database layer: scenes + API keys (SQLAlchemy 2 async).

Scene states (brief §6 Phase 2):
  awaiting_upload → queued → processing → succeeded | failed

API keys are stored as SHA-256 hashes only; the plaintext `sk_...` is shown
once at mint time (see keycli.py). No auth UI in v1 by design.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from typing import Any, AsyncIterator

from sqlalchemy import JSON, DateTime, ForeignKey, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCENE_STATES = ("awaiting_upload", "queued", "processing", "succeeded", "failed")


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(default=True)
    # Worker keys may post to the _result callback; customer keys may not.
    is_worker: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # scn_...
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="awaiting_upload", index=True)
    video_key: Mapped[str] = mapped_column(String(255))            # storage key of the upload
    webhook_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    semantic: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assets: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, future=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s


# --- helpers ----------------------------------------------------------------


def new_scene_id() -> str:
    return "scn_" + uuid.uuid4().hex[:12]


def mint_api_key() -> tuple[str, str, str]:
    """(key_id, plaintext sk_..., sha256 hash). Plaintext is never stored."""
    plain = "sk_" + secrets.token_urlsafe(32)
    return "key_" + uuid.uuid4().hex[:12], plain, hash_api_key(plain)


def hash_api_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


async def find_key(session: AsyncSession, plain: str) -> ApiKey | None:
    res = await session.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(plain), ApiKey.active.is_(True))
    )
    return res.scalar_one_or_none()
