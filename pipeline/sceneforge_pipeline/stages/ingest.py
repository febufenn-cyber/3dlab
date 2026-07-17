"""Stage 1 — ingest: video → curated frame set (brief §4.1).

ffmpeg extracts frames at a fixed fps with the long side capped; we then drop
blurry frames (variance of Laplacian) and near-duplicates (normalized
cross-correlation of consecutive downsampled grays), and enforce the capture
contract's duration/frame-count floor. Every rejection is counted so the
quality report can state exactly what happened.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..config import IngestConfig
from ..errors import DependencyMissingError, QualityGateError

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    frames_dir: Path
    kept: list[str]                       # filenames, ffmpeg extraction order
    stats: dict = field(default_factory=dict)


def probe_duration_s(video: Path) -> float:
    """Container duration via ffprobe (ships with ffmpeg)."""
    if shutil.which("ffprobe") is None:
        raise DependencyMissingError("ffprobe not found — install ffmpeg")
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def extract_frames(video: Path, out_dir: Path, cfg: IngestConfig) -> list[Path]:
    """Extract frames with ffmpeg at cfg.fps, long side capped to cfg.long_side_px."""
    if shutil.which("ffmpeg") is None:
        raise DependencyMissingError("ffmpeg not found — install ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Scale filter: cap the long side, keep aspect, never upscale, even dims.
    scale = (
        f"scale=w='if(gt(iw,ih),min({cfg.long_side_px},iw),-2)':"
        f"h='if(gt(iw,ih),-2,min({cfg.long_side_px},ih))'"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vf", f"fps={cfg.fps},{scale}",
        "-qscale:v", "2",
        str(out_dir / "frame_%05d.jpg"),
    ]
    subprocess.run(cmd, check=True)
    return sorted(out_dir.glob("frame_*.jpg"))


def variance_of_laplacian(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normalized cross-correlation of two equally-sized float arrays."""
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom < 1e-9:
        return 1.0  # two flat frames are duplicates of each other
    return float((a * b).sum() / denom)


def curate_frames(frames: list[Path], cfg: IngestConfig) -> tuple[list[Path], dict]:
    """Drop blurry frames and near-duplicates; return survivors + stats."""
    kept: list[Path] = []
    blur_scores: dict[str, float] = {}
    n_blur = n_dup = 0
    prev_small: np.ndarray | None = None

    for f in frames:
        img = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if img is None:
            n_blur += 1
            continue
        score = variance_of_laplacian(img)
        blur_scores[f.name] = round(score, 2)
        if score < cfg.blur_threshold:
            n_blur += 1
            continue
        small = cv2.resize(img, (64, 64)).astype(np.float64)
        if prev_small is not None and _ncc(prev_small, small) > cfg.dedup_correlation:
            n_dup += 1
            continue
        prev_small = small
        kept.append(f)

    # Enforce the hard cap by uniform subsampling (keeps temporal spread).
    if len(kept) > cfg.max_frames:
        idx = np.linspace(0, len(kept) - 1, cfg.max_frames).round().astype(int)
        kept = [kept[i] for i in sorted(set(idx.tolist()))]

    total = len(frames)
    stats = {
        "frames_extracted": total,
        "frames_kept": len(kept),
        "blur_rejected": n_blur,
        "duplicate_rejected": n_dup,
        "blur_rejected_pct": round(100.0 * n_blur / total, 1) if total else 0.0,
        "blur_scores_sample": dict(list(blur_scores.items())[:10]),
    }
    return kept, stats


def run_ingest(video: Path, workdir: Path, cfg: IngestConfig) -> IngestResult:
    video = Path(video)
    if not video.exists():
        raise FileNotFoundError(video)

    duration = probe_duration_s(video)
    if duration < cfg.min_duration_s:
        raise QualityGateError(
            "video_too_short",
            f"Video is {duration:.1f}s; the capture contract requires "
            f"{cfg.min_duration_s:.0f}s+ (recommended 60–120s). "
            "A short clip cannot cover a room — re-record a slow, full walkthrough.",
            {"duration_s": round(duration, 1), "capture_rule": "duration_60_120s"},
        )
    if duration > cfg.max_duration_s:
        log.warning("Video is %.0fs (>%.0fs); processing the whole clip", duration, cfg.max_duration_s)

    raw_dir = workdir / "frames_raw"
    frames = extract_frames(video, raw_dir, cfg)
    kept, stats = curate_frames(frames, cfg)
    stats["duration_s"] = round(duration, 1)

    if len(kept) < cfg.min_frames:
        raise QualityGateError(
            "insufficient_sharp_frames",
            f"Only {len(kept)} usable frames after blur/duplicate filtering "
            f"(need ≥ {cfg.min_frames}). Likely fast panning or low light — "
            "re-record with slower motion and lights on.",
            {**stats, "capture_rule": "slow_pan_lights_on"},
        )

    # Materialize the curated set in a clean directory for SfM.
    frames_dir = workdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for f in frames_dir.glob("*.jpg"):
        f.unlink()
    for f in kept:
        shutil.copy2(f, frames_dir / f.name)

    log.info(
        "ingest: %d extracted → %d kept (%d blurry, %d duplicates)",
        stats["frames_extracted"], stats["frames_kept"],
        stats["blur_rejected"], stats["duplicate_rejected"],
    )
    return IngestResult(frames_dir=frames_dir, kept=[f.name for f in kept], stats=stats)
