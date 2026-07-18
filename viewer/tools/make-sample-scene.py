#!/usr/bin/env python3
"""Generate the SYNTHETIC sample scene for viewer/demo/sample/.

Everything this script emits is procedurally generated — no capture, no
photos, no reconstruction. It exists so the demo page renders out of the
box, and it is labeled as synthetic in both the demo note and the poster
itself (SceneForge never passes generated content off as a real capture).
The first real captured scene replaces it after the first GPU run
(QUALITY.md, "Pending hardware").

The scene is a 4×3×2.6 m room in the SceneForge frame (Z-up, floor z=0):
wood floor, four walls (one accent) with a 0.9×2.0 m door gap, a table
(1.2×0.6 m top at 0.72 m + 4 legs) and a rug. Gaussians are sampled on
those surfaces at ~3 cm spacing, written as a standard 3DGS PLY
(sceneforge_pipeline.splat_export.write_gsplat_ply) and compressed with
the same node converter the pipeline uses (viewer/tools/convert-to-ksplat.mjs,
SH degree 0). Hard budget: scene.ksplat ≤ 3.0 MB — spacing is widened until
it fits. poster.png is a stylized painter's-algorithm splat of the gaussian
centers through a pinhole camera; the demo page's camera-position/look-at
attributes match that camera.

Usage: python3 viewer/tools/make-sample-scene.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from sceneforge_pipeline.splat_export import SH_C0, write_gsplat_ply

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "viewer" / "demo" / "sample"
CONVERTER = ROOT / "viewer" / "tools" / "convert-to-ksplat.mjs"

ROOM_X, ROOM_Y, ROOM_Z = 4.0, 3.0, 2.6      # metres, Z-up, floor z=0
DOOR_W, DOOR_H = 0.9, 2.0                    # gap in the x=ROOM_X wall
MAX_KSPLAT_MB = 3.0                          # hard budget for the demo asset

# Poster camera — keep in sync with camera-position / look-at in
# viewer/demo/index.html.
CAM_POS = np.array([0.7, 0.6, 1.5])
CAM_LOOKAT = np.array([2.5, 1.8, 1.0])

WOOD = (0.58, 0.42, 0.28)          # floor
WALL_A = (0.91, 0.89, 0.85)        # off-white, x-walls
WALL_B = (0.87, 0.85, 0.80)       # off-white, y=0 wall
ACCENT = (0.70, 0.35, 0.27)        # terracotta accent, y=ROOM_Y wall
TABLE = (0.42, 0.28, 0.18)         # walnut table
RUG = (0.18, 0.24, 0.42)           # deep blue rug field
RUG_BORDER = (0.80, 0.76, 0.66)    # cream rug border


def _sample_rect(rng, origin, u_vec, v_vec, spacing):
    """Jittered grid of points covering origin + s*u_vec + t*v_vec, s,t∈[0,1]."""
    origin = np.asarray(origin, dtype=np.float64)
    u_vec = np.asarray(u_vec, dtype=np.float64)
    v_vec = np.asarray(v_vec, dtype=np.float64)
    nu = max(1, int(round(np.linalg.norm(u_vec) / spacing)))
    nv = max(1, int(round(np.linalg.norm(v_vec) / spacing)))
    s, t = np.meshgrid((np.arange(nu) + 0.5) / nu, (np.arange(nv) + 0.5) / nv)
    s = (s + rng.uniform(-0.35, 0.35, s.shape) / nu).ravel()
    t = (t + rng.uniform(-0.35, 0.35, t.shape) / nv).ravel()
    return origin + s[:, None] * u_vec + t[:, None] * v_vec


def _tint(color, n):
    return np.tile(np.asarray(color, dtype=np.float64), (n, 1))


def build_scene(rng, spacing: float) -> dict[str, np.ndarray]:
    """Sample gaussians on the room surfaces; returns 3DGS-convention arrays."""
    parts: list[tuple[np.ndarray, np.ndarray]] = []

    # Floor — warm wood with subtle plank banding.
    floor = _sample_rect(rng, (0, 0, 0), (ROOM_X, 0, 0), (0, ROOM_Y, 0), spacing)
    plank = 1.0 - 0.05 * (np.floor(floor[:, 1] / 0.18) % 2)
    parts.append((floor, _tint(WOOD, len(floor)) * plank[:, None]))

    # Walls (two off-white tones + one accent); door gap in the x=ROOM_X wall.
    w = _sample_rect(rng, (0, 0, 0), (0, ROOM_Y, 0), (0, 0, ROOM_Z), spacing)
    parts.append((w, _tint(WALL_A, len(w))))
    w = _sample_rect(rng, (ROOM_X, 0, 0), (0, ROOM_Y, 0), (0, 0, ROOM_Z), spacing)
    door_lo = (ROOM_Y - DOOR_W) / 2.0 + 0.5          # gap y ∈ [1.55, 2.45]
    in_gap = (
        (w[:, 1] > door_lo) & (w[:, 1] < door_lo + DOOR_W) & (w[:, 2] < DOOR_H)
    )
    w = w[~in_gap]
    parts.append((w, _tint(WALL_A, len(w))))
    w = _sample_rect(rng, (0, 0, 0), (ROOM_X, 0, 0), (0, 0, ROOM_Z), spacing)
    parts.append((w, _tint(WALL_B, len(w))))
    w = _sample_rect(rng, (0, ROOM_Y, 0), (ROOM_X, 0, 0), (0, 0, ROOM_Z), spacing)
    parts.append((w, _tint(ACCENT, len(w))))

    # Rug on the floor (slightly above it), deep blue with a cream border.
    rug_w, rug_d, cx, cy = 2.0, 1.4, 2.5, 1.8
    rug = _sample_rect(
        rng, (cx - rug_w / 2, cy - rug_d / 2, 0.012),
        (rug_w, 0, 0), (0, rug_d, 0), spacing,
    )
    edge = np.minimum.reduce([
        rug[:, 0] - (cx - rug_w / 2), (cx + rug_w / 2) - rug[:, 0],
        rug[:, 1] - (cy - rug_d / 2), (cy + rug_d / 2) - rug[:, 1],
    ])
    colors = np.where((edge < 0.12)[:, None], _tint(RUG_BORDER, len(rug)),
                      _tint(RUG, len(rug)))
    parts.append((rug, colors))

    # Table: 1.2×0.6 m top at 0.72 m + 4 legs (two crossed vertical strips each).
    top_w, top_d, top_z = 1.2, 0.6, 0.72
    top = _sample_rect(
        rng, (cx - top_w / 2, cy - top_d / 2, top_z),
        (top_w, 0, 0), (0, top_d, 0), spacing,
    )
    parts.append((top, _tint(TABLE, len(top))))
    for lx in (cx - top_w / 2 + 0.05, cx + top_w / 2 - 0.05):
        for ly in (cy - top_d / 2 + 0.05, cy + top_d / 2 - 0.05):
            for org, u in (
                ((lx - 0.025, ly, 0.0), (0.05, 0, 0)),
                ((lx, ly - 0.025, 0.0), (0, 0.05, 0)),
            ):
                leg = _sample_rect(rng, org, u, (0, 0, top_z - 0.02), spacing)
                parts.append((leg, _tint(TABLE, len(leg))))

    means = np.concatenate([p for p, _ in parts])
    rgb = np.concatenate([c for _, c in parts])
    n = len(means)
    rgb = np.clip(rgb + rng.normal(0.0, 0.02, rgb.shape), 0.02, 0.98)

    quats = np.concatenate(
        [np.ones((n, 1)), rng.normal(0.0, 0.03, (n, 3))], axis=1
    )
    quats /= np.linalg.norm(quats, axis=1, keepdims=True)
    return {
        "means": means,
        "scales_log": np.log(0.025) + rng.normal(0.0, 0.15, (n, 3)),
        "quats": quats,
        "opacities_logit": 2.0 + rng.normal(0.0, 0.2, n),
        "sh0": ((rgb - 0.5) / SH_C0)[:, None, :],
        "rgb": rgb,
    }


def convert_to_ksplat(scene: dict[str, np.ndarray], out_path: Path) -> float:
    """PLY → .ksplat via the pipeline's node converter (SH degree 0); returns MB."""
    with tempfile.TemporaryDirectory() as td:
        ply = Path(td) / "sample.ply"
        write_gsplat_ply(
            ply,
            means=scene["means"], scales_log=scene["scales_log"],
            quats=scene["quats"], opacities_logit=scene["opacities_logit"],
            sh0=scene["sh0"], shN=None,
        )
        subprocess.run(
            ["node", str(CONVERTER), str(ply), str(out_path), "1", "1", "0"],
            check=True, cwd=ROOT,
        )
    return out_path.stat().st_size / 1e6


def render_poster(scene: dict[str, np.ndarray], path: Path,
                  width: int = 960, height: int = 540) -> None:
    """Painter's-algorithm splat of the gaussian centers through a pinhole cam."""
    fwd = CAM_LOOKAT - CAM_POS
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, (0.0, 0.0, 1.0))
    right /= np.linalg.norm(right)
    cam_up = np.cross(right, fwd)
    fx = (width / 2.0) / np.tan(np.radians(68.0) / 2.0)  # ~68° horizontal FOV

    rel = scene["means"] - CAM_POS
    x, y, z = rel @ right, rel @ cam_up, rel @ fwd
    keep = z > 0.25
    u = width / 2.0 + fx * x[keep] / z[keep]
    v = height / 2.0 - fx * y[keep] / z[keep]
    z, rgb = z[keep], scene["rgb"][keep]
    inside = (u > -20) & (u < width + 20) & (v > -20) & (v < height + 20)
    u, v, z, rgb = u[inside], v[inside], z[inside], rgb[inside]

    img = np.full((height, width, 3), (26, 22, 20), dtype=np.uint8)  # #14161a
    shade = np.clip(1.06 - 0.055 * z, 0.6, 1.0)
    bgr = (np.clip(rgb[:, ::-1] * shade[:, None], 0.0, 1.0) * 255).astype(int)
    for i in np.argsort(-z):  # far → near
        radius = int(np.clip(round(12.0 / z[i]), 1, 30))
        cv2.circle(img, (int(round(u[i])), int(round(v[i]))), radius,
                   tuple(int(c) for c in bgr[i]), -1, lineType=cv2.LINE_AA)
    img = cv2.GaussianBlur(img, (3, 3), 0)

    # Honest labeling, baked into the poster itself.
    img[height - 26:] = (img[height - 26:] * 0.3).astype(np.uint8)
    cv2.putText(
        img, "SYNTHETIC SAMPLE - procedurally generated, not a real capture",
        (10, height - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (235, 235, 235), 1,
        cv2.LINE_AA,
    )
    cv2.imwrite(str(path), img)


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    ksplat = SAMPLE_DIR / "scene.ksplat"
    spacing = 0.028
    for _ in range(6):
        scene = build_scene(np.random.default_rng(20260718), spacing)
        mb = convert_to_ksplat(scene, ksplat)
        n = len(scene["means"])
        print(f"spacing {spacing * 100:.1f} cm → {n} gaussians → {mb:.2f} MB")
        if mb <= MAX_KSPLAT_MB:
            break
        spacing *= 1.2  # widen sampling until the asset fits the budget
    else:
        sys.exit(f"could not fit scene.ksplat under {MAX_KSPLAT_MB} MB")

    render_poster(scene, SAMPLE_DIR / "poster.png")
    print(f"wrote {ksplat} ({mb:.2f} MB, {n} gaussians) + poster.png")


if __name__ == "__main__":
    main()
