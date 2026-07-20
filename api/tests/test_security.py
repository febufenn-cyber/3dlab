"""Security regression tests — one per confirmed red-team finding (P2 pass).

Each test replays the verifier's actual attack and asserts it is now blocked.
See SECURITY.md for the finding catalog.
"""

import datetime as dt

import pytest

from sceneforge_api.db import Scene, mint_api_key, ApiKey, utcnow
from sceneforge_api.reaper import reap_once
from sceneforge_api.security import BodyLimitMiddleware, RateLimiter

pytestmark = pytest.mark.asyncio


# --- CRITICAL: request-body cap (unauth OOM) --------------------------------


async def test_oversized_body_rejected_before_auth(api):
    """A >cap body is 413'd — and crucially not buffered — even under a bogus
    key, so it can't OOM the container. (Content-Length fast path.)"""
    client, ctx = api
    cap = ctx["app"].state.settings.max_request_body_kb * 1024
    big = b"x" * (cap + 1)
    r = await client.post(
        "/v1/scenes",
        content=big,
        headers={"Authorization": "Bearer sk_bogus", "Content-Type": "application/json"},
    )
    assert r.status_code == 413


async def test_normal_body_still_accepted(api):
    """The cap doesn't break legitimate small requests."""
    client, ctx = api
    r = await client.post("/v1/scenes", json={"filename": "flat.mp4"}, headers=ctx["headers"])
    assert r.status_code == 201


async def test_body_limit_middleware_streamed_chunks():
    """Chunked body with no Content-Length is still capped (buffer-and-replay):
    the app is never invoked once the streamed total exceeds the cap."""
    invoked = {"app": False}

    async def app(scope, receive, send):
        invoked["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodyLimitMiddleware(app, max_bytes=1000)
    scope = {"type": "http", "path": "/v1/scenes", "headers": []}  # no content-length

    chunks = [
        {"type": "http.request", "body": b"a" * 600, "more_body": True},
        {"type": "http.request", "body": b"b" * 600, "more_body": False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    sent = []

    async def send(m):
        sent.append(m)

    await mw(scope, receive, send)
    assert invoked["app"] is False
    assert sent[0]["status"] == 413


# --- HIGH: rate limit + outstanding cap + awaiting_upload reaping -----------


async def test_create_rate_limited(api_factory):
    """A create flood from one key 429s once the token bucket drains."""
    async with api_factory(create_rate_capacity=10, create_rate_per_sec=0.001,
                           max_outstanding_scenes=1000) as (client, ctx):
        statuses = []
        for _ in range(15):
            r = await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
            statuses.append(r.status_code)
        assert statuses[0] == 201
        assert statuses[:10] == [201] * 10  # capacity exactly 10
        assert statuses[10] == 429          # then drained


async def test_outstanding_scene_cap(api_factory):
    """Beyond the per-key outstanding cap, creation 429s — bounding row growth."""
    async with api_factory(max_outstanding_scenes=3, create_rate_capacity=1000) as (client, ctx):
        statuses = []
        for _ in range(6):
            r = await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
            statuses.append(r.status_code)
        assert statuses[:3] == [201, 201, 201]
        assert 429 in statuses[3:]


async def test_reaper_expires_abandoned_uploads(api):
    """awaiting_upload scenes past presign expiry are failed, not accumulated
    forever (the create-and-abandon flood vector)."""
    client, ctx = api
    r = await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
    sid = r.json()["scene_id"]
    # Backdate past presign expiry.
    async with ctx["database"].sessionmaker() as session:
        scene = await session.get(Scene, sid)
        scene.updated_at = utcnow() - dt.timedelta(
            seconds=ctx["app"].state.settings.presign_expiry_s + 60
        )
        await session.commit()
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], ctx["app"].state.settings
    )
    assert result["expired_uploads"] == [sid]
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json()["state"] == "failed"
    assert r.json()["error_code"] == "upload_abandoned"


# --- MEDIUM: reaper batching ------------------------------------------------


async def test_reaper_batches_queries(api_factory):
    """The reaper never loads more than `reaper_batch` rows per query, so a
    large stale backlog can't OOM the tick."""
    async with api_factory(reaper_batch=5, create_rate_capacity=1000) as (client, ctx):
        ids = []
        for _ in range(12):
            r = await client.post("/v1/scenes", json={"filename": "f.mp4"}, headers=ctx["headers"])
            sid = r.json()["scene_id"]
            await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"x")
            await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
            ids.append(sid)
        async with ctx["database"].sessionmaker() as session:
            for sid in ids:
                scene = await session.get(Scene, sid)
                scene.updated_at = utcnow() - dt.timedelta(seconds=3600)
            await session.commit()
        result = await reap_once(
            ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], ctx["app"].state.settings
        )
        assert len(result["requeued"]) == 5  # capped to the batch, not all 12


# --- LOW: content-type validation -------------------------------------------


async def test_hostile_content_type_coerced(api, monkeypatch):
    """A tenant cannot make their stored upload serve as text/html: a
    non-video content_type is coerced to application/octet-stream in the
    presigned PUT."""
    client, ctx = api
    captured = {}
    real_presign = ctx["storage"].presign_upload

    def spy(key, content_type, expires):
        captured["content_type"] = content_type
        return real_presign(key, content_type, expires)

    monkeypatch.setattr(ctx["storage"], "presign_upload", spy)
    r = await client.post(
        "/v1/scenes",
        json={"filename": "x.mp4", "content_type": "text/html"},
        headers=ctx["headers"],
    )
    assert r.status_code == 201
    assert captured["content_type"] == "application/octet-stream"


async def test_allowed_content_type_preserved(api, monkeypatch):
    client, ctx = api
    captured = {}
    real_presign = ctx["storage"].presign_upload

    def spy(key, content_type, expires):
        captured["content_type"] = content_type
        return real_presign(key, content_type, expires)

    monkeypatch.setattr(ctx["storage"], "presign_upload", spy)
    await client.post(
        "/v1/scenes",
        json={"filename": "x.mov", "content_type": "video/quicktime"},
        headers=ctx["headers"],
    )
    assert captured["content_type"] == "video/quicktime"


# --- unit: rate limiter refill ----------------------------------------------


def test_rate_limiter_refills():
    t = {"now": 0.0}
    rl = RateLimiter(capacity=2, refill_per_sec=1.0, clock=lambda: t["now"])
    assert rl.allow("k") and rl.allow("k")  # capacity 2
    assert not rl.allow("k")                 # drained
    t["now"] = 1.0                            # +1s → +1 token
    assert rl.allow("k")
    assert not rl.allow("k")
