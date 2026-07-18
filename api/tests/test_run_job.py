"""Tests for sceneforge_worker.run_job.process_scene — no network, stub adapter.

`_post_result` is replaced with an async recorder and `make_worker` (as bound
in run_job's namespace) with a stub GPUWorker, so the whole flow runs against
tmp-dir LocalStorage.
"""

import pytest

import sceneforge_worker.run_job as run_job
from sceneforge_api.storage import LocalStorage
from sceneforge_worker.adapters import GPUWorker
from sceneforge_worker.jobspec import ARTIFACT_NAMES, BuildJob, BuildOutcome

pytestmark = pytest.mark.asyncio

# What a full successful build uploads (scene.splat intentionally absent).
UPLOADED = [
    "scene.ksplat",
    "floorplan.svg",
    "poster.png",
    "viewer.html",
    "semantic.json",
    "quality_report.json",
]


class StubGPUWorker(GPUWorker):
    name = "stub"

    def __init__(self, outcome: BuildOutcome | None = None, exc: Exception | None = None):
        self.outcome = outcome
        self.exc = exc
        self.jobs: list[BuildJob] = []

    def build(self, job: BuildJob) -> BuildOutcome:
        self.jobs.append(job)
        if self.exc is not None:
            raise self.exc
        assert self.outcome is not None
        return self.outcome


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """(storage, posts, use_worker) — tmp local storage + recorded result posts."""
    monkeypatch.setenv("SCENEFORGE_STORAGE", "local")
    monkeypatch.setenv("SCENEFORGE_LOCAL_STORAGE_DIR", str(tmp_path / "storage"))

    posts: list[tuple[str, dict]] = []

    async def record_post(scene_id: str, payload: dict) -> None:
        posts.append((scene_id, payload))

    monkeypatch.setattr(run_job, "_post_result", record_post)

    def use_worker(stub: GPUWorker) -> None:
        monkeypatch.setattr(run_job, "make_worker", lambda name=None: stub)

    return LocalStorage(tmp_path / "storage"), posts, use_worker


async def test_success_posts_processing_then_exact_asset_keys(worker_env):
    storage, posts, use_worker = worker_env
    sid = "scn_ok"
    storage.put_bytes(f"{sid}/video.mp4", b"fake video bytes")
    stub = StubGPUWorker(
        BuildOutcome(
            status="succeeded",
            quality_report={"status": "succeeded", "psnr": 31.2},
            semantic={"rooms": [{"label": "kitchen"}]},
            artifacts_uploaded=UPLOADED,
        )
    )
    use_worker(stub)

    assert await run_job.process_scene({}, sid) == "succeeded"

    assert posts[0] == (sid, {"state": "processing"})
    assert len(posts) == 2
    final_sid, final = posts[-1]
    assert final_sid == sid
    assert final["state"] == "succeeded"
    assert final["error_code"] is None
    assert final["quality_report"] == {"status": "succeeded", "psnr": 31.2}
    assert final["semantic"] == {"rooms": [{"label": "kitchen"}]}
    # Pins the asset-key contract the viewer's resolveSplatUrl and
    # QUICKSTART.md rely on: 'scene.ksplat' → key 'scene', etc.
    assert final["assets"] == {
        "scene": f"{sid}/assets/scene.ksplat",
        "floorplan": f"{sid}/assets/floorplan.svg",
        "poster": f"{sid}/assets/poster.png",
        "viewer": f"{sid}/assets/viewer.html",
        "semantic": f"{sid}/assets/semantic.json",
        "quality_report": f"{sid}/assets/quality_report.json",
    }


async def test_build_job_carries_video_url_and_all_put_urls(worker_env):
    storage, _posts, use_worker = worker_env
    sid = "scn_job"
    storage.put_bytes(f"{sid}/video.mp4", b"x")
    stub = StubGPUWorker(BuildOutcome(status="succeeded"))
    use_worker(stub)

    await run_job.process_scene({}, sid)

    assert len(stub.jobs) == 1
    job = stub.jobs[0]
    assert job.scene_id == sid
    assert "video.mp4" in job.video_url
    assert set(job.artifact_put_urls) == set(ARTIFACT_NAMES)
    for name, url in job.artifact_put_urls.items():
        assert f"{sid}/assets/{name}" in url


async def test_probes_alternate_video_extension(worker_env):
    storage, _posts, use_worker = worker_env
    sid = "scn_mov"
    storage.put_bytes(f"{sid}/video.mov", b"quicktime-ish bytes")
    stub = StubGPUWorker(BuildOutcome(status="succeeded"))
    use_worker(stub)

    await run_job.process_scene({}, sid)

    assert "video.mov" in stub.jobs[0].video_url
    assert "video.mp4" not in stub.jobs[0].video_url


async def test_crash_posts_failed_and_reraises(worker_env):
    storage, posts, use_worker = worker_env
    sid = "scn_boom"
    storage.put_bytes(f"{sid}/video.mp4", b"x")
    use_worker(StubGPUWorker(exc=RuntimeError("CUDA device fell off the bus")))

    with pytest.raises(RuntimeError):
        await run_job.process_scene({}, sid)

    assert posts[0] == (sid, {"state": "processing"})
    final_sid, final = posts[-1]
    assert final_sid == sid
    assert final["state"] == "failed"
    assert final["error_code"] == "worker_crash"
    assert "CUDA device fell off the bus" in final["quality_report"]["failure"]["message"]


async def test_honest_failure_passed_through(worker_env):
    storage, posts, use_worker = worker_env
    sid = "scn_reject"
    storage.put_bytes(f"{sid}/video.mp4", b"x")
    report = {"status": "failed", "failure": {"reason_code": "insufficient_registration"}}
    use_worker(
        StubGPUWorker(
            BuildOutcome(
                status="failed", error_code="insufficient_registration", quality_report=report
            )
        )
    )

    assert await run_job.process_scene({}, sid) == "failed"

    final = posts[-1][1]
    assert final["state"] == "failed"
    assert final["error_code"] == "insufficient_registration"
    assert final["quality_report"] == report
    assert final["assets"] == {}
