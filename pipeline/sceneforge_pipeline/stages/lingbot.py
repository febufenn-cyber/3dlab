"""lingbot-map geometry backend adapter (brief §4.2).

lingbot-map (Robbyant / Ant Group) is a feed-forward streaming 3D foundation
model: it estimates camera poses and a dense point cloud from a video in one
forward pass (~20 FPS at 518×378), so it clears the ≤10 min/scene GPU budget
far more comfortably than incremental SfM. Repo:
https://github.com/Robbyant/lingbot-map

Licensing (verified 2026-07-18, see LICENSES.md): the CODE is Apache-2.0
(README, verified). The WEIGHTS are not given a separate license on the repo
and the HF model card is unreachable from this build's egress proxy, so the
weights license is recorded as *inferred* Apache-2.0 and must be confirmed
directly before a commercial launch. Because of that unresolved weights
question, COLMAP remains the pipeline default; lingbot is opt-in via
`--backend lingbot`.

This module's value is the conversion `lingbot_to_colmap_model`: it maps
lingbot's native output (c2w `extrinsic`, `intrinsic`, `world_points` +
`world_points_conf`, `images`) into the pipeline's COLMAP-model representation
so every downstream stage (scale, semantics, splat) runs unchanged. The
conversion is pure NumPy and unit-tested on CPU; only the model inference
itself needs the GPU.

Output keys verified against the repo's demo.py (2026-07-18):
  extrinsic (c2w 4x4), intrinsic (3x3 K), world_points (…,3),
  world_points_conf (…), images (…,H,W,3), depth, depth_conf.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np

from ..colmap_model import Camera, ColmapModel, Image, Point3D, rotmat2qvec
from ..config import GeometryConfig
from ..errors import DependencyMissingError, GeometryFailure

log = logging.getLogger(__name__)


def _as_array(d, *names):
    """First present key among names, or None (npz/dict tolerant)."""
    for n in names:
        if n in d:
            return np.asarray(d[n])
    return None


def lingbot_confidence_stats(output: dict, conf_threshold: float = 0.5) -> dict:
    """Confidence summary for the honest-failure gate.

    A feed-forward model always emits poses, so we judge reconstruction health
    by how many world points clear ``conf_threshold``. Returns counts + ratio;
    when no confidence channel is present, reports ``has_confidence=False`` and
    the gate is skipped (we can't judge, so we don't reject).
    """
    conf = _as_array(output, "world_points_conf", "world_points_confidence", "conf")
    if conf is None or conf.size == 0:
        return {"has_confidence": False, "n_total": 0, "n_confident": 0, "confident_ratio": 0.0}
    conf = conf.reshape(-1)
    n_total = int(conf.size)
    n_conf = int((conf >= conf_threshold).sum())
    return {
        "has_confidence": True,
        "n_total": n_total,
        "n_confident": n_conf,
        "confident_ratio": round(n_conf / n_total, 4) if n_total else 0.0,
        "mean_confidence": round(float(conf.mean()), 4),
    }


def lingbot_to_colmap_model(
    output: dict,
    conf_threshold: float = 0.5,
    max_points: int = 200_000,
    frame_names: list[str] | None = None,
) -> ColmapModel:
    """Convert a lingbot-map prediction dict into a ColmapModel.

    Parameters
    ----------
    output : mapping with lingbot keys — ``extrinsic`` (N,4,4 camera-to-world),
        ``intrinsic`` (N,3,3 or 3,3), ``world_points`` (N,H,W,3 or N,P,3),
        optionally ``world_points_conf`` (same leading shape) and ``images``
        (N,H,W,3, uint8 or float in [0,1]) for point colours.
    conf_threshold : keep only points with confidence ≥ this (if conf present).
    max_points : uniform-subsample the surviving cloud to this cap.

    The COLMAP convention is world→camera, so we invert lingbot's c2w
    ``extrinsic``. One PINHOLE camera is emitted per frame.
    """
    extrinsic = _as_array(output, "extrinsic", "extrinsics", "camera_poses", "poses")
    if extrinsic is None:
        raise GeometryFailure("lingbot output missing camera poses ('extrinsic')")
    extrinsic = extrinsic.astype(np.float64)
    if extrinsic.ndim == 2:
        extrinsic = extrinsic[None]
    n = extrinsic.shape[0]

    intrinsic = _as_array(output, "intrinsic", "intrinsics", "K")
    if intrinsic is None:
        raise GeometryFailure("lingbot output missing intrinsics ('intrinsic')")
    intrinsic = intrinsic.astype(np.float64)
    if intrinsic.ndim == 2:
        intrinsic = np.broadcast_to(intrinsic, (n, 3, 3)).copy()

    images = _as_array(output, "images", "rgb")
    world_points = _as_array(output, "world_points", "world_pts", "points", "xyz")
    world_conf = _as_array(output, "world_points_conf", "world_points_confidence", "conf")

    # ---- cameras + images (poses) -----------------------------------------
    cameras: dict[int, Camera] = {}
    imgs: dict[int, Image] = {}
    for i in range(n):
        c2w = extrinsic[i]
        w2c = np.linalg.inv(c2w)
        R = w2c[:3, :3]
        t = w2c[:3, 3]
        K = intrinsic[i]
        fx, fy = float(K[0, 0]), float(K[1, 1])
        cx, cy = float(K[0, 2]), float(K[1, 2])
        if images is not None and images.ndim == 4:
            h, w = int(images.shape[1]), int(images.shape[2])
        else:
            w, h = int(round(cx * 2)), int(round(cy * 2))
        cam_id = i + 1
        cameras[cam_id] = Camera(
            cam_id, "PINHOLE", max(w, 1), max(h, 1), np.array([fx, fy, cx, cy])
        )
        name = frame_names[i] if frame_names and i < len(frame_names) else f"frame_{i+1:05d}.jpg"
        imgs[i + 1] = Image(
            image_id=i + 1,
            qvec=rotmat2qvec(R),
            tvec=t,
            camera_id=cam_id,
            name=name,
        )

    # ---- point cloud ------------------------------------------------------
    points: dict[int, Point3D] = {}
    if world_points is not None and world_points.size:
        pts = world_points.reshape(-1, 3)
        cols = None
        if images is not None and images.ndim == 4 and images.shape[1:3] == world_points.shape[1:3]:
            cols = images.reshape(-1, 3)
            if cols.dtype != np.uint8:
                cols = np.clip(cols * 255.0, 0, 255).astype(np.uint8)
        if world_conf is not None and world_conf.size == pts.shape[0]:
            keep = world_conf.reshape(-1) >= conf_threshold
            pts = pts[keep]
            if cols is not None:
                cols = cols[keep]
        # Drop non-finite points; never fabricate coordinates.
        finite = np.all(np.isfinite(pts), axis=1)
        pts = pts[finite]
        if cols is not None:
            cols = cols[finite]
        if len(pts) > max_points:
            idx = np.linspace(0, len(pts) - 1, max_points).astype(int)
            pts = pts[idx]
            if cols is not None:
                cols = cols[idx]
        for j, xyz in enumerate(pts, start=1):
            rgb = cols[j - 1] if cols is not None else np.array([128, 128, 128], np.uint8)
            points[j] = Point3D(j, xyz.astype(np.float64), rgb.astype(np.uint8), error=1.0)

    if not points:
        raise GeometryFailure(
            "lingbot produced no usable 3D points above the confidence threshold "
            f"({conf_threshold}). The capture may be too sparse or low-confidence."
        )
    return ColmapModel(cameras=cameras, images=imgs, points=points)


def run_lingbot_inference(
    frames_dir: Path, workdir: Path, cfg: GeometryConfig
) -> dict:
    """Shell out to lingbot-map inference and load its NPZ output.

    Runs only where lingbot-map + a CUDA GPU are installed (the worker image).
    The command mirrors the repo's demo.py CLI; the checkpoint path and flags
    come from GeometryConfig so a swap needs no code change.
    """
    if shutil.which(cfg.lingbot_python or "python") is None:  # pragma: no cover
        raise DependencyMissingError("python interpreter for lingbot not found")
    out_npz = workdir / "lingbot_out.npz"
    cmd = [
        cfg.lingbot_python or "python",
        "-m", cfg.lingbot_module,
        "--image_folder", str(frames_dir),
        "--model_path", cfg.lingbot_model_path,
        "--mode", cfg.lingbot_mode,
        "--save_npz", str(out_npz),
    ]
    if cfg.lingbot_use_sdpa:
        cmd.append("--use_sdpa")
    log.info("geometry(lingbot): %s", " ".join(cmd[:6]) + " ...")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_npz.exists():  # pragma: no cover - GPU only
        raise GeometryFailure(
            f"lingbot-map inference failed (exit {proc.returncode}). "
            f"stderr tail:\n{proc.stderr[-1500:]}"
        )
    with np.load(out_npz) as data:
        return {k: data[k] for k in data.files}
