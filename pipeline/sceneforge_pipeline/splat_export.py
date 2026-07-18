"""Gaussian splat export writers (CPU-only, no torch import).

Two formats:

* Standard 3DGS ``.ply`` — interchange format every tool understands.
* ``.splat`` (antimatter15 layout, 32 bytes/gaussian) — the compact fallback
  the mkkellogg GaussianSplats3D viewer loads directly. Primary compressed
  format is ``.ksplat``, produced by viewer/tools/convert-to-ksplat.mjs from
  the .ply (node); when node is unavailable the pipeline falls back to .splat.

Layout of one .splat record (little-endian):
  float32 x, y, z; float32 sx, sy, sz (linear scales);
  uint8 r, g, b, a; uint8 qw, qx, qy, qz  (quat mapped from [-1,1] to 0..255)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

SH_C0 = 0.28209479177387814  # SH DC basis constant


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def write_gsplat_ply(
    path: Path,
    means: np.ndarray,        # (N,3) float
    scales_log: np.ndarray,   # (N,3) log-scales (3DGS convention)
    quats: np.ndarray,        # (N,4) wxyz, not necessarily normalized
    opacities_logit: np.ndarray,  # (N,)
    sh0: np.ndarray,          # (N,1,3) DC SH coefficients
    shN: np.ndarray | None,   # (N,K,3) higher-order SH or None
) -> Path:
    """Standard INRIA 3DGS PLY layout (binary_little_endian)."""
    n = means.shape[0]
    k = 0 if shN is None or shN.size == 0 else shN.shape[1]
    props = (
        ["x", "y", "z", "nx", "ny", "nz"]
        + [f"f_dc_{i}" for i in range(3)]
        + [f"f_rest_{i}" for i in range(3 * k)]
        + ["opacity"]
        + [f"scale_{i}" for i in range(3)]
        + [f"rot_{i}" for i in range(4)]
    )
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        + "".join(f"property float {p}\n" for p in props)
        + "end_header\n"
    )
    cols: list[np.ndarray] = [
        means.astype(np.float32),
        np.zeros((n, 3), dtype=np.float32),               # normals, unused
        sh0.reshape(n, 3).astype(np.float32),
    ]
    if k:
        # 3DGS PLY expects f_rest grouped channel-major: all R coeffs, all G, all B.
        cols.append(shN.transpose(0, 2, 1).reshape(n, 3 * k).astype(np.float32))
    cols += [
        opacities_logit.reshape(n, 1).astype(np.float32),
        scales_log.astype(np.float32),
        quats.astype(np.float32),
    ]
    data = np.concatenate(cols, axis=1)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header.encode("ascii"))
        fh.write(data.astype("<f4").tobytes())
    return path


def write_splat(
    path: Path,
    means: np.ndarray,
    scales_log: np.ndarray,
    quats: np.ndarray,
    opacities_logit: np.ndarray,
    sh0: np.ndarray,
) -> Path:
    """Compact .splat (antimatter15) file — 32 bytes per gaussian.

    Gaussians are sorted by opacity*volume descending, the ordering the
    GaussianSplats3D loader recommends for progressive display.
    """
    n = means.shape[0]
    scales = np.exp(scales_log.astype(np.float64))
    opac = _sigmoid(opacities_logit.astype(np.float64)).reshape(n)
    rgb = np.clip(0.5 + SH_C0 * sh0.reshape(n, 3).astype(np.float64), 0.0, 1.0)

    order = np.argsort(-(opac * scales.prod(axis=1)))
    means, scales, opac, rgb = means[order], scales[order], opac[order], rgb[order]
    q = quats[order].astype(np.float64)
    q /= np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-9)

    rec = np.zeros(n, dtype=[("pos", "<f4", 3), ("scale", "<f4", 3),
                             ("rgba", "u1", 4), ("quat", "u1", 4)])
    rec["pos"] = means.astype(np.float32)
    rec["scale"] = scales.astype(np.float32)
    rec["rgba"][:, :3] = (rgb * 255).round().astype(np.uint8)
    rec["rgba"][:, 3] = (opac * 255).round().astype(np.uint8)
    rec["quat"] = np.clip((q * 128 + 128).round(), 0, 255).astype(np.uint8)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(rec.tobytes())
    return path


def read_splat(path: Path) -> dict[str, np.ndarray]:
    """Inverse of write_splat, for tests and sanity checks."""
    raw = np.fromfile(path, dtype=[("pos", "<f4", 3), ("scale", "<f4", 3),
                                   ("rgba", "u1", 4), ("quat", "u1", 4)])
    return {
        "means": raw["pos"].astype(np.float64),
        "scales": raw["scale"].astype(np.float64),
        "rgba": raw["rgba"].copy(),
        "quats": (raw["quat"].astype(np.float64) - 128.0) / 128.0,
    }
