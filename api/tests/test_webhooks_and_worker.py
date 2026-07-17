import hashlib
import hmac
import json

import pytest

from sceneforge_api.webhooks import sign
from sceneforge_worker.adapters import LocalGPUWorker, make_worker
from sceneforge_worker.jobspec import ARTIFACT_NAMES, BuildJob, BuildOutcome

pytestmark = pytest.mark.asyncio


def test_signature_matches_manual_hmac():
    body = json.dumps({"event": "scene.succeeded"}).encode()
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sign("secret", body) == expected


async def test_webhook_fires_on_success(api, monkeypatch):
    client, ctx = api
    deliveries = []

    async def fake_deliver(url, payload, secret="", retries=3):
        deliveries.append((url, payload, secret))
        return True

    import sceneforge_api.main as main_mod

    monkeypatch.setattr(main_mod, "deliver", fake_deliver)

    r = await client.post(
        "/v1/scenes",
        json={"filename": "flat.mp4", "webhook_url": "https://example.com/hook"},
        headers=ctx["headers"],
    )
    sid = r.json()["scene_id"]
    await client.put(f"/v1/_local-upload/{sid}/video.mp4", content=b"x")
    await client.post(f"/v1/scenes/{sid}/uploaded", headers=ctx["headers"])
    await client.post(
        f"/v1/scenes/{sid}/_result",
        json={"state": "succeeded", "quality_report": {"status": "succeeded"}},
        headers=ctx["headers"],
    )
    assert len(deliveries) == 1
    url, payload, _ = deliveries[0]
    assert url == "https://example.com/hook"
    assert payload["event"] == "scene.succeeded"
    assert payload["scene"]["scene_id"] == sid


async def test_no_webhook_for_processing(api, monkeypatch):
    client, ctx = api
    deliveries = []

    async def fake_deliver(url, payload, secret="", retries=3):
        deliveries.append(url)
        return True

    import sceneforge_api.main as main_mod

    monkeypatch.setattr(main_mod, "deliver", fake_deliver)

    r = await client.post(
        "/v1/scenes",
        json={"filename": "a.mp4", "webhook_url": "https://example.com/hook"},
        headers=ctx["headers"],
    )
    sid = r.json()["scene_id"]
    await client.post(
        f"/v1/scenes/{sid}/_result", json={"state": "processing"}, headers=ctx["headers"]
    )
    assert deliveries == []


# --- jobspec / adapters ------------------------------------------------------


def test_buildjob_roundtrip():
    job = BuildJob(
        scene_id="scn_abc",
        video_url="https://x/video.mp4",
        artifact_put_urls={n: f"https://x/{n}" for n in ARTIFACT_NAMES},
        quality="fast",
    )
    assert BuildJob.from_dict(job.to_dict()) == job


def test_buildoutcome_roundtrip():
    out = BuildOutcome(status="failed", error_code="insufficient_registration",
                       quality_report={"status": "failed"})
    assert BuildOutcome.from_dict(out.to_dict()) == out


def test_make_worker_default_is_local(monkeypatch):
    monkeypatch.delenv("SCENEFORGE_GPU_WORKER", raising=False)
    assert isinstance(make_worker(), LocalGPUWorker)


def test_make_worker_unknown_rejected():
    with pytest.raises(ValueError):
        make_worker("teleport")


def test_hf_worker_requires_space(monkeypatch):
    monkeypatch.delenv("SCENEFORGE_HF_SPACE", raising=False)
    with pytest.raises(ValueError):
        make_worker("hf_space")
