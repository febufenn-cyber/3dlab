"""API test fixtures: SQLite DB, local storage in tmpdir, in-memory queue."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from sceneforge_api.db import ApiKey, Database, mint_api_key
from sceneforge_api.main import create_app
from sceneforge_api.queue import InMemoryQueue
from sceneforge_api.settings import Settings
from sceneforge_api.storage import LocalStorage


@pytest_asyncio.fixture
async def api(tmp_path):
    """(client, ctx) — a fully wired app on SQLite + tmp local storage."""
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        storage_backend="local",
        local_storage_dir=str(tmp_path / "storage"),
        reaper_interval_s=0,  # tests drive reap_once directly
    )
    database = Database(settings.database_url)
    queue = InMemoryQueue()
    storage = LocalStorage(tmp_path / "storage")
    app = create_app(settings=settings, database=database, queue=queue, storage=storage)

    await database.create_all()
    key_id, plain, key_hash = mint_api_key()
    w_id, w_plain, w_hash = mint_api_key()
    async with database.sessionmaker() as session:
        session.add(ApiKey(id=key_id, key_hash=key_hash, name="test-customer"))
        session.add(ApiKey(id=w_id, key_hash=w_hash, name="test-worker", is_worker=True))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ctx = {
            "app": app,
            "queue": queue,
            "storage": storage,
            "database": database,
            "api_key": plain,
            "key_id": key_id,
            "headers": {"Authorization": f"Bearer {plain}"},
            "worker_headers": {"Authorization": f"Bearer {w_plain}"},
        }
        yield client, ctx
    await database.dispose()
