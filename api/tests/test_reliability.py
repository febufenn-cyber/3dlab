"""Reliability gauntlet: races, crashes, timeouts, queue outages.

The invariant under test: no scene is ever silently lost or stuck — every
path ends in an honest state a customer can see and act on.
"""

import asyncio
import datetime as dt

import pytest

from sceneforge_api.db import Scene, utcnow
from sceneforge_api.reaper import reap_once

pytestmark = pytest.mark.asyncio


async def _make_scene(ctx, client, upload=True):
    r = await client.post(
        "/v1/scenes", json={"filename": "flat.mp4"}, headers=ctx["headers"]
    )
    sid = r.json()["scene_id"]
    if upload:
        await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"x")
    return sid


async def _age_scene(ctx, sid, seconds, state=None):
    """Backdate a scene's updated_at (and optionally force a state)."""
    async with ctx["database"].sessionmaker() as session:
        scene = await session.get(Scene, sid)
        if state:
            scene.state = state
        scene.updated_at = utcnow() - dt.timedelta(seconds=seconds)
        await session.commit()


# --- atomic transitions (races) --------------------------------------------


async def test_double_upload_confirm_race(api):
    """Two concurrent /uploaded confirms: exactly one wins, one 409s, and the
    job is enqueued exactly once."""
    client, ctx = api
    sid = await _make_scene(ctx, client)
    r1, r2 = await asyncio.gather(
        client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"]),
        client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"]),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assert ctx["queue"].jobs == [sid]


async def test_requeue_failed_scene(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await client.post(
        f"/v1/scenes/{sid}/_result",
        json={"state": "failed", "error_code": "worker_timeout"},
        headers=ctx["worker_headers"],
    )
    r = await client.post(f"/v1/scenes/{sid}/requeue", headers=ctx["headers"])
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
    assert r.json()["error_code"] is None
    assert ctx["queue"].jobs.count(sid) == 2  # original + requeue


async def test_requeue_rejects_non_failed_states(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    # awaiting_upload → 409
    assert (
        await client.post(f"/v1/scenes/{sid}/requeue", headers=ctx["headers"])
    ).status_code == 409
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await client.post(
        f"/v1/scenes/{sid}/_result", json={"state": "succeeded"},
        headers=ctx["worker_headers"],
    )
    # succeeded is final → 409
    assert (
        await client.post(f"/v1/scenes/{sid}/requeue", headers=ctx["headers"])
    ).status_code == 409


async def test_requeue_tenant_isolated(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    from sceneforge_api.db import ApiKey, mint_api_key

    key_id, plain, key_hash = mint_api_key()
    async with ctx["database"].sessionmaker() as session:
        session.add(ApiKey(id=key_id, key_hash=key_hash, name="other"))
        await session.commit()
    r = await client.post(
        f"/v1/scenes/{sid}/requeue", headers={"Authorization": f"Bearer {plain}"}
    )
    assert r.status_code == 404


# --- enqueue outage ---------------------------------------------------------


async def test_enqueue_failure_does_not_lose_the_scene(api, monkeypatch):
    """Queue down at confirm time: the customer still gets 200/queued and the
    scene is durably queued for the reaper to re-enqueue."""
    client, ctx = api

    async def broken_enqueue(scene_id):
        raise ConnectionError("queue down")

    monkeypatch.setattr(ctx["queue"], "enqueue", broken_enqueue)
    sid = await _make_scene(ctx, client)
    r = await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    assert r.status_code == 200
    assert r.json()["state"] == "queued"


# --- worker-report ordering -------------------------------------------------


async def test_late_processing_cannot_regress_terminal_scene(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await client.post(
        f"/v1/scenes/{sid}/_result", json={"state": "succeeded"},
        headers=ctx["worker_headers"],
    )
    r = await client.post(
        f"/v1/scenes/{sid}/_result", json={"state": "processing"},
        headers=ctx["worker_headers"],
    )
    assert r.status_code == 409
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json()["state"] == "succeeded"


async def test_late_terminal_result_beats_timeout(api):
    """A worker that finishes after the reaper timed the scene out still gets
    its real result recorded — a genuine outcome beats a timeout guess."""
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await _age_scene(ctx, sid, seconds=7200, state="processing")
    settings = ctx["app"].state.settings
    await reap_once(ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings)
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json() ["state"] == "failed"
    # …then the zombie worker reports real success:
    r = await client.post(
        f"/v1/scenes/{sid}/_result",
        json={"state": "succeeded", "quality_report": {"status": "succeeded"}},
        headers=ctx["worker_headers"],
    )
    assert r.status_code == 200
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json()["state"] == "succeeded"


# --- reaper -----------------------------------------------------------------


async def test_reaper_fails_stuck_processing_and_fires_webhook(api, monkeypatch):
    client, ctx = api
    deliveries = []

    async def fake_deliver(url, payload, secret="", retries=3):
        deliveries.append((url, payload))
        return True

    import sceneforge_api.reaper as reaper_mod

    monkeypatch.setattr(reaper_mod, "deliver", fake_deliver)

    r = await client.post(
        "/v1/scenes",
        json={"filename": "a.mp4", "webhook_url": "https://example.com/hook"},
        headers=ctx["headers"],
    )
    sid = r.json()["scene_id"]
    await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"x")
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await _age_scene(ctx, sid, seconds=7200, state="processing")

    settings = ctx["app"].state.settings
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings
    )
    assert result["failed_processing"] == [sid]

    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    body = r.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "worker_timeout"
    assert "requeue" in body["quality_report"]["failure"]["message"]
    assert len(deliveries) == 1 and deliveries[0][1]["event"] == "scene.failed"


async def test_reaper_requeues_stale_queued_once_per_window(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await _age_scene(ctx, sid, seconds=3600)  # still `queued`, but stale

    settings = ctx["app"].state.settings
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings
    )
    assert result["requeued"] == [sid]
    assert ctx["queue"].jobs.count(sid) == 2  # original + reaper retry

    # Immediately reaping again must NOT enqueue a third time: updated_at was
    # touched, so retries are rate-limited to one per timeout window.
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings
    )
    assert result["requeued"] == []
    assert ctx["queue"].jobs.count(sid) == 2


async def test_reaper_leaves_fresh_scenes_alone(api):
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    settings = ctx["app"].state.settings
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings
    )
    assert result == {"failed_processing": [], "requeued": [], "expired_uploads": []}
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json()["state"] == "queued"


async def test_reaper_survives_queue_outage(api, monkeypatch):
    """Queue down during a reap tick: reaper logs and keeps going; the scene
    is retried the NEXT window because updated_at was already touched."""
    client, ctx = api
    sid = await _make_scene(ctx, client)
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await _age_scene(ctx, sid, seconds=3600)

    async def broken_enqueue(scene_id):
        raise ConnectionError("queue down")

    monkeypatch.setattr(ctx["queue"], "enqueue", broken_enqueue)
    settings = ctx["app"].state.settings
    result = await reap_once(
        ctx["database"].sessionmaker, ctx["queue"], ctx["storage"], settings
    )
    assert result["requeued"] == [sid]  # attempted; failure logged, not raised
