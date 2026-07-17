"""The job contract shared by every GPU adapter (brief §5 architecture rule).

A BuildJob carries only URLs and small strings — no blobs — so it can cross
any transport (arq payload, Modal call, gradio JSON). `execute_build` is the
single implementation that actually runs the pipeline; every adapter just
decides WHERE it executes:

  local    → this process (a CUDA box running the arq worker)
  modal    → inside a Modal function (dispatcher stays on the CPU VM)
  hf_space → inside a HF ZeroGPU Space via gradio_client

Artifacts are moved via presigned URLs in both directions, so the dispatcher
never proxies bytes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Fixed artifact set the API presigns; execute_build uploads the subset that exists.
ARTIFACT_NAMES = (
    "scene.ksplat",
    "scene.splat",
    "floorplan.svg",
    "poster.png",
    "viewer.html",
    "semantic.json",
    "quality_report.json",
)


@dataclass
class BuildJob:
    scene_id: str
    video_url: str                       # presigned GET for the uploaded video
    artifact_put_urls: dict[str, str]    # artifact name → presigned PUT
    quality: str = "standard"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BuildJob":
        return BuildJob(**d)


@dataclass
class BuildOutcome:
    status: str                          # succeeded | failed
    error_code: str | None = None
    quality_report: dict | None = None
    semantic: dict | None = None
    artifacts_uploaded: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BuildOutcome":
        return BuildOutcome(**d)


def execute_build(job: BuildJob, rf_scene_cmd: str = "rf-scene") -> BuildOutcome:
    """Download video → `rf-scene build` → upload artifacts → outcome.

    Runs on whatever machine has the GPU. Requires: rf-scene on PATH (the
    worker image provides it), httpx for transfers.
    """
    import httpx

    with tempfile.TemporaryDirectory(prefix=f"sf_{job.scene_id}_") as tmp:
        tmpdir = Path(tmp)
        video = tmpdir / "input.mp4"
        out_dir = tmpdir / "out"

        log.info("[%s] downloading video", job.scene_id)
        with httpx.stream("GET", job.video_url, timeout=300.0, follow_redirects=True) as r:
            r.raise_for_status()
            with open(video, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)

        cmd = [rf_scene_cmd, "build", str(video), "-o", str(out_dir), "--quality", job.quality]
        log.info("[%s] running: %s", job.scene_id, " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        sys.stderr.write(proc.stderr[-4000:] if proc.stderr else "")

        quality_report = _read_json(out_dir / "quality_report.json")
        semantic = _read_json(out_dir / "semantic.json")

        uploaded = _upload_artifacts(job, out_dir)

        if proc.returncode == 0:
            return BuildOutcome(
                status="succeeded",
                quality_report=quality_report,
                semantic=semantic,
                artifacts_uploaded=uploaded,
            )
        if proc.returncode == 2:  # capture rejected by quality gate — honest failure
            code = (quality_report or {}).get("failure", {}).get("reason_code", "capture_rejected")
            return BuildOutcome(
                status="failed", error_code=code,
                quality_report=quality_report, artifacts_uploaded=uploaded,
            )
        return BuildOutcome(
            status="failed",
            error_code="pipeline_error",
            quality_report=quality_report
            or {"status": "failed", "failure": {"message": proc.stderr[-1000:]}},
            artifacts_uploaded=uploaded,
        )


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _upload_artifacts(job: BuildJob, out_dir: Path) -> list[str]:
    import httpx

    uploaded = []
    for name in ARTIFACT_NAMES:
        src = out_dir / name
        url = job.artifact_put_urls.get(name)
        if not url or not src.exists():
            continue
        with open(src, "rb") as fh:
            resp = httpx.put(url, content=fh.read(), timeout=300.0)
        if resp.status_code < 300:
            uploaded.append(name)
        else:
            log.warning("[%s] upload %s → HTTP %s", job.scene_id, name, resp.status_code)
    return uploaded
