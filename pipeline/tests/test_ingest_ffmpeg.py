"""Ingest integration tests against real ffmpeg/ffprobe.

A ~25 s MJPG .avi is synthesized with cv2.VideoWriter — a crop panning across
a fixed-seed noise texture, so every frame is sharp and non-duplicate — and
pushed through the real extract/curate path. Skipped cleanly on hosts without
ffmpeg; CI installs it so these always run there.
"""

from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest

from sceneforge_pipeline.config import IngestConfig
from sceneforge_pipeline.errors import QualityGateError
from sceneforge_pipeline.stages.ingest import probe_duration_s, run_ingest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

W, H = 640, 480
FPS = 10
N_FRAMES = 250  # 25 s at 10 fps
PAN_STEP = 4    # px of pan per source frame → extracted frames decorrelate


def _write_video(path, blurry=False):
    """Synthesize a real 25 s MJPG clip.

    Sharp variant: a 640×480 crop pans across fixed-seed noise, so frames are
    high-frequency and pairwise distinct. Blurry variant: one heavily blurred
    frame repeated (static), so curation rejects everything.
    """
    rng = np.random.default_rng(42)
    texture = rng.integers(0, 256, (H, W + PAN_STEP * N_FRAMES, 3), dtype=np.uint8)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    assert writer.isOpened()
    static = cv2.GaussianBlur(texture[:, :W], (51, 51), 20) if blurry else None
    for i in range(N_FRAMES):
        if blurry:
            frame = static
        else:
            x = i * PAN_STEP
            frame = np.ascontiguousarray(texture[:, x : x + W])
        writer.write(frame)
    writer.release()
    return path


@pytest.fixture(scope="module")
def sharp_video(tmp_path_factory):
    return _write_video(tmp_path_factory.mktemp("ingest_ffmpeg") / "pan.avi")


def test_probe_duration_matches_synthesized_clip(sharp_video):
    assert probe_duration_s(sharp_video) == pytest.approx(25.0, abs=1.0)


def test_run_ingest_extracts_and_curates_real_video(sharp_video, tmp_path):
    cfg = IngestConfig()
    result = run_ingest(sharp_video, tmp_path / "work", cfg)

    stats = result.stats
    assert stats["frames_extracted"] == pytest.approx(75, abs=2)  # 3 fps × 25 s
    assert stats["frames_kept"] >= cfg.min_frames
    assert stats["blur_rejected"] == 0

    jpgs = sorted(result.frames_dir.glob("*.jpg"))
    assert [p.name for p in jpgs] == result.kept
    for p in jpgs:
        img = cv2.imread(str(p))
        assert img is not None
        # 640 < long_side_px cap: the scale filter must never upscale.
        assert max(img.shape[:2]) == W


def test_run_ingest_rejects_blurry_static_video(tmp_path):
    video = _write_video(tmp_path / "blurry.avi", blurry=True)
    with pytest.raises(QualityGateError) as exc:
        run_ingest(video, tmp_path / "work", IngestConfig())
    assert exc.value.reason_code == "insufficient_sharp_frames"
    report = exc.value.report
    assert report["frames_kept"] < IngestConfig().min_frames
    assert report["blur_rejected"] + report["duplicate_rejected"] > 0
