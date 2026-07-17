import cv2
import numpy as np
import pytest

from sceneforge_pipeline.config import IngestConfig
from sceneforge_pipeline.errors import QualityGateError
from sceneforge_pipeline.stages.ingest import (
    curate_frames,
    run_ingest,
    variance_of_laplacian,
)


def _write_sharp(path, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, (480, 640), dtype=np.uint8)
    cv2.imwrite(str(path), img)


def _write_blurry(path):
    img = np.full((480, 640), 128, dtype=np.uint8)
    cv2.circle(img, (320, 240), 100, 160, -1)
    img = cv2.GaussianBlur(img, (51, 51), 20)
    cv2.imwrite(str(path), img)


def test_variance_of_laplacian_orders_sharpness():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (200, 200), dtype=np.uint8)
    blurry = cv2.GaussianBlur(sharp, (31, 31), 10)
    assert variance_of_laplacian(sharp) > variance_of_laplacian(blurry) * 10


def test_curate_drops_blur_and_duplicates(tmp_path):
    paths = []
    for i in range(6):
        p = tmp_path / f"frame_{i:05d}.jpg"
        _write_sharp(p, seed=i)
        paths.append(p)
    dup = tmp_path / "frame_00006.jpg"
    _write_sharp(dup, seed=5)  # identical content to frame 5 → duplicate
    paths.append(dup)
    blur = tmp_path / "frame_00007.jpg"
    _write_blurry(blur)
    paths.append(blur)

    kept, stats = curate_frames(paths, IngestConfig())
    names = [k.name for k in kept]
    assert dup.name not in names
    assert blur.name not in names
    assert stats["blur_rejected"] >= 1
    assert stats["duplicate_rejected"] >= 1
    assert stats["frames_extracted"] == 8


def test_curate_caps_max_frames(tmp_path):
    paths = []
    for i in range(30):
        p = tmp_path / f"frame_{i:05d}.jpg"
        _write_sharp(p, seed=i)
        paths.append(p)
    cfg = IngestConfig(max_frames=10)
    kept, _ = curate_frames(paths, cfg)
    assert len(kept) <= 10


def test_run_ingest_rejects_short_video(tmp_path, monkeypatch):
    from sceneforge_pipeline.stages import ingest as mod

    monkeypatch.setattr(mod, "probe_duration_s", lambda v: 9.0)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with pytest.raises(QualityGateError) as exc:
        run_ingest(video, tmp_path / "work", IngestConfig())
    assert exc.value.reason_code == "video_too_short"
    assert exc.value.report["capture_rule"] == "duration_60_120s"


def test_run_ingest_rejects_too_few_sharp_frames(tmp_path, monkeypatch):
    from sceneforge_pipeline.stages import ingest as mod

    monkeypatch.setattr(mod, "probe_duration_s", lambda v: 60.0)

    def fake_extract(video, out_dir, cfg):
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(10):  # all blurry → below min_frames after curation
            p = out_dir / f"frame_{i:05d}.jpg"
            _write_blurry(p)
            paths.append(p)
        return paths

    monkeypatch.setattr(mod, "extract_frames", fake_extract)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    with pytest.raises(QualityGateError) as exc:
        run_ingest(video, tmp_path / "work", IngestConfig())
    assert exc.value.reason_code == "insufficient_sharp_frames"
