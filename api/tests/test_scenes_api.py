"""API contract tests: auth, happy path, honest failure path (brief §6 Phase 2)."""

import pytest

pytestmark = pytest.mark.asyncio


async def _create_scene(client, ctx, **body):
    payload = {"filename": "flat.mp4", **body}
    r = await client.post("/v1/scenes", json=payload, headers=ctx["headers"])
    assert r.status_code == 201, r.text
    return r.json()


async def test_healthz(api):
    client, _ = api
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_auth_required(api):
    client, _ = api
    r = await client.post("/v1/scenes", json={"filename": "a.mp4"})
    assert r.status_code == 401


async def test_bad_key_rejected(api):
    client, _ = api
    r = await client.post(
        "/v1/scenes", json={"filename": "a.mp4"},
        headers={"Authorization": "Bearer sk_wrong"},
    )
    assert r.status_code == 401


async def test_create_scene_returns_upload_url(api):
    client, ctx = api
    data = await _create_scene(client, ctx)
    assert data["scene_id"].startswith("scn_")
    assert data["state"] == "awaiting_upload"
    assert data["scene_id"] in data["upload_url"]


async def test_happy_path_states(api):
    client, ctx = api
    data = await _create_scene(client, ctx)
    sid = data["scene_id"]

    # Confirm before upload → 400, not queued (honest failure).
    r = await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    assert r.status_code == 400

    # Upload via the dev endpoint the presigned URL points at.
    r = await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"fake video bytes")
    assert r.status_code == 200

    r = await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
    assert ctx["queue"].jobs == [sid]

    # Double confirm → 409.
    r = await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    assert r.status_code == 409

    # Worker: processing → succeeded with results.
    r = await client.post(
        f"/v1/scenes/{sid}/_result", json={"state": "processing"}, headers=ctx["headers"]
    )
    assert r.status_code == 200
    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    assert r.json()["state"] == "processing"

    semantic = {"scene_id": sid, "schema_version": "1.0", "rooms": []}
    r = await client.post(
        f"/v1/scenes/{sid}/_result",
        json={
            "state": "succeeded",
            "quality_report": {"status": "succeeded", "quality": {"coverage_pct": 88}},
            "semantic": semantic,
            "assets": {"splat": f"{sid}/assets/scene.ksplat"},
        },
        headers=ctx["headers"],
    )
    assert r.status_code == 200

    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    body = r.json()
    assert body["state"] == "succeeded"
    assert body["quality_report"]["quality"]["coverage_pct"] == 88
    assert "scene.ksplat" in body["assets"]["splat"]

    r = await client.get(f"/v1/scenes/{sid}/semantic", headers=ctx["headers"])
    assert r.status_code == 200
    assert r.json()["scene_id"] == sid


async def test_honest_failure_path(api):
    """A 10s clip of one wall → failed + insufficient coverage report, not a
    broken splat (brief §6 Phase 2 acceptance)."""
    client, ctx = api
    data = await _create_scene(client, ctx)
    sid = data["scene_id"]
    await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"short clip")
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])

    r = await client.post(
        f"/v1/scenes/{sid}/_result",
        json={
            "state": "failed",
            "error_code": "insufficient_registration",
            "quality_report": {
                "status": "failed",
                "failure": {
                    "reason_code": "insufficient_registration",
                    "message": "Only 8/30 frames registered",
                    "capture_rule": "slow_continuous_pan",
                },
            },
        },
        headers=ctx["headers"],
    )
    assert r.status_code == 200

    r = await client.get(f"/v1/scenes/{sid}", headers=ctx["headers"])
    body = r.json()
    assert body["state"] == "failed"
    assert body["error_code"] == "insufficient_registration"
    assert body["quality_report"]["failure"]["capture_rule"] == "slow_continuous_pan"
    # Semantic JSON is not available for failed scenes.
    r = await client.get(f"/v1/scenes/{sid}/semantic", headers=ctx["headers"])
    assert r.status_code == 409


async def test_tenant_isolation(api):
    """Scenes are invisible across API keys (404, not 403 — no id oracle)."""
    client, ctx = api
    data = await _create_scene(client, ctx)
    sid = data["scene_id"]

    from sceneforge_api.db import ApiKey, mint_api_key

    key_id, plain, key_hash = mint_api_key()
    async with ctx["database"].sessionmaker() as session:
        session.add(ApiKey(id=key_id, key_hash=key_hash, name="other-tenant"))
        await session.commit()

    r = await client.get(f"/v1/scenes/{sid}", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 404


async def test_unknown_scene_404(api):
    client, ctx = api
    r = await client.get("/v1/scenes/scn_missing00000", headers=ctx["headers"])
    assert r.status_code == 404


async def test_local_storage_key_escape_blocked(api):
    client, _ = api
    r = await client.put("/v1/_local-upload/../../etc/passwd", content=b"nope")
    assert r.status_code in (400, 404)
