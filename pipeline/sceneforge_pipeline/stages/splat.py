"""Stage 3 — photoreal reconstruction: Gaussian splat training (brief §4.3).

Built directly on gsplat's (Apache-2.0) public API — ``gsplat.rasterization``
plus ``MCMCStrategy`` for densification. MCMCStrategy is chosen over
DefaultStrategy because its ``cap_max`` gives a hard gaussian-count ceiling,
which is what makes the ≤25 MB payload budget enforceable rather than hoped-for.

torch/gsplat are imported lazily: this module lives in the GPU worker image;
the API host (ARM, no CUDA) never imports it. Loss is L1 + a DSSIM term only
if ``fused_ssim`` is installed (optional); pure L1 otherwise (DECISIONS.md).

Frames are undistorted to a PINHOLE model with `colmap image_undistorter`
before training, since the rasterizer assumes an ideal pinhole camera.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..colmap_model import ColmapModel, read_model
from ..config import SplatConfig
from ..errors import DependencyMissingError, PipelineError
from ..splat_export import write_gsplat_ply, write_splat

log = logging.getLogger(__name__)


@dataclass
class SplatResult:
    ply_path: Path
    compact_path: Path            # .ksplat or .splat — what the viewer loads
    compact_format: str
    poster_path: Path | None
    num_gaussians: int
    stats: dict


def undistort(frames_dir: Path, sparse_dir: Path, workdir: Path, colmap_bin: str = "colmap") -> tuple[Path, Path]:
    """COLMAP image_undistorter → PINHOLE model + undistorted images."""
    if shutil.which(colmap_bin) is None:
        raise DependencyMissingError(f"'{colmap_bin}' needed for undistortion")
    out = workdir / "undistorted"
    subprocess.run(
        [
            colmap_bin, "image_undistorter",
            "--image_path", str(frames_dir),
            "--input_path", str(sparse_dir),
            "--output_path", str(out),
            "--output_type", "COLMAP",
        ],
        check=True, capture_output=True,
    )
    return out / "images", out / "sparse"


def train_splat(
    frames_dir: Path,
    model: ColmapModel,
    workdir: Path,
    cfg: SplatConfig,
    out_dir: Path,
    world_transform: np.ndarray | None = None,
) -> SplatResult:
    """Train a 3DGS scene and export compact assets.

    ``world_transform`` is the 4x4 similarity (gravity-align + metric scale)
    produced by the scale/semantics stages, applied to the exported splat so
    the viewer, floor plan and semantic JSON share one coordinate frame.
    """
    try:
        import torch
        from gsplat import rasterization
        from gsplat.strategy import MCMCStrategy
    except ImportError as e:  # pragma: no cover - GPU worker only
        raise DependencyMissingError(
            "torch+gsplat are required for splat training (GPU worker image only). "
            f"Underlying error: {e}"
        ) from e
    if not torch.cuda.is_available():  # pragma: no cover
        raise DependencyMissingError("CUDA GPU required for splat training")

    device = "cuda"
    t0 = time.time()

    # ---- dataset -----------------------------------------------------------
    cams, viewmats, Ks, paths = _load_dataset(frames_dir, model)
    if len(paths) == 0:
        raise PipelineError("No registered frames available for splat training")
    images = _load_images(paths, device="cpu")  # keep frames on CPU, move per-step
    H, W = images[0].shape[:2]

    xyz, rgb = model.points_array()
    if len(xyz) < 1000:
        raise PipelineError(f"Sparse cloud too small to initialize splats ({len(xyz)} pts)")

    # ---- init gaussians from the sparse cloud ------------------------------
    n0 = len(xyz)
    means = torch.tensor(xyz, dtype=torch.float32, device=device)
    dist = _knn_mean_dist(xyz)
    scales = torch.log(torch.tensor(dist, dtype=torch.float32, device=device))[:, None].repeat(1, 3)
    quats = torch.zeros((n0, 4), device=device)
    quats[:, 0] = 1.0
    opacities = torch.logit(torch.full((n0,), 0.1, device=device))
    colors = torch.tensor(rgb / 255.0, dtype=torch.float32, device=device)
    sh0 = ((colors - 0.5) / 0.28209479177387814)[:, None, :]
    k_rest = (cfg.sh_degree + 1) ** 2 - 1
    shN = torch.zeros((n0, k_rest, 3), device=device)

    params = torch.nn.ParameterDict(
        {
            "means": torch.nn.Parameter(means),
            "scales": torch.nn.Parameter(scales),
            "quats": torch.nn.Parameter(quats),
            "opacities": torch.nn.Parameter(opacities),
            "sh0": torch.nn.Parameter(sh0),
            "shN": torch.nn.Parameter(shN),
        }
    ).to(device)

    scene_scale = float(np.linalg.norm(xyz.std(axis=0)))
    lrs = {
        "means": 1.6e-4 * scene_scale, "scales": 5e-3, "quats": 1e-3,
        "opacities": 5e-2, "sh0": 2.5e-3, "shN": 2.5e-3 / 20,
    }
    optimizers = {
        name: torch.optim.Adam([{"params": [params[name]], "lr": lr, "name": name}], eps=1e-15)
        for name, lr in lrs.items()
    }
    strategy = MCMCStrategy(cap_max=cfg.max_gaussians)
    strategy.check_sanity(params, optimizers)
    strategy_state = strategy.initialize_state()

    try:
        from fused_ssim import fused_ssim  # optional quality bump
    except ImportError:
        fused_ssim = None

    # ---- optimize -----------------------------------------------------------
    viewmats_t = torch.tensor(viewmats, dtype=torch.float32, device=device)
    Ks_t = torch.tensor(Ks, dtype=torch.float32, device=device)
    rng = np.random.default_rng(0)
    means_lr_decay = (0.01) ** (1.0 / cfg.iterations)

    for step in range(cfg.iterations):
        i = int(rng.integers(0, len(images)))
        gt = images[i].to(device, non_blocking=True) / 255.0
        sh_deg = min(step // 1000, cfg.sh_degree)

        renders, alphas, info = rasterization(
            means=params["means"],
            quats=params["quats"],
            scales=torch.exp(params["scales"]),
            opacities=torch.sigmoid(params["opacities"]),
            colors=torch.cat([params["sh0"], params["shN"]], dim=1),
            viewmats=viewmats_t[i : i + 1],
            Ks=Ks_t[i : i + 1],
            width=W, height=H,
            sh_degree=sh_deg,
            packed=False,
        )
        render = renders[0].clamp(0.0, 1.0)
        l1 = (render - gt).abs().mean()
        if fused_ssim is not None:
            ssim = fused_ssim(
                render.permute(2, 0, 1)[None], gt.permute(2, 0, 1)[None], padding="valid"
            )
            loss = 0.8 * l1 + 0.2 * (1.0 - ssim)
        else:
            loss = l1

        # Ordering per gsplat's reference trainer (examples/simple_trainer.py
        # @ v1.5.3): backward → optimizer steps → strategy.step_post_backward,
        # with the CURRENT (decayed) means lr driving MCMC noise injection.
        strategy.step_pre_backward(params, optimizers, strategy_state, step, info)
        loss.backward()
        for opt in optimizers.values():
            opt.step()
            opt.zero_grad(set_to_none=True)
        current_means_lr = optimizers["means"].param_groups[0]["lr"]
        strategy.step_post_backward(
            params, optimizers, strategy_state, step, info, lr=current_means_lr
        )
        for group in optimizers["means"].param_groups:
            group["lr"] *= means_lr_decay

        if step % 1000 == 0:
            log.info(
                "splat: step %d/%d loss=%.4f gaussians=%d",
                step, cfg.iterations, float(loss), len(params["means"]),
            )

    train_s = time.time() - t0

    # ---- export -------------------------------------------------------------
    with torch.no_grad():
        out = {k: v.detach().cpu().numpy() for k, v in params.items()}
    out = _crop_outliers(out, xyz)
    if world_transform is not None:
        out = _apply_similarity(out, world_transform)

    out_dir.mkdir(parents=True, exist_ok=True)
    ply_path = write_gsplat_ply(
        out_dir / "scene.ply",
        means=out["means"], scales_log=out["scales"], quats=out["quats"],
        opacities_logit=out["opacities"], sh0=out["sh0"], shN=out["shN"],
    )
    compact_path, compact_format = compress_splat(ply_path, out, out_dir, cfg)

    poster_path = _render_poster(
        params, rasterization, torch, viewmats_t, Ks_t, W, H, cfg, out_dir
    )

    sizes = {
        "ply_mb": round(ply_path.stat().st_size / 1e6, 2),
        "compact_mb": round(compact_path.stat().st_size / 1e6, 2),
        "compact_format": compact_format,
    }
    if sizes["compact_mb"] > cfg.target_max_mb:
        log.warning("compact splat %.1f MB exceeds %.0f MB target", sizes["compact_mb"], cfg.target_max_mb)
    stats = {
        "iterations": cfg.iterations,
        "num_gaussians": int(len(out["means"])),
        "train_seconds": round(train_s, 1),
        "final_loss": round(float(loss), 4),
        "ssim_used": fused_ssim is not None,
        **sizes,
    }
    (out_dir / "splat_stats.json").write_text(json.dumps(stats, indent=2))
    return SplatResult(
        ply_path=ply_path, compact_path=compact_path, compact_format=compact_format,
        poster_path=poster_path, num_gaussians=stats["num_gaussians"], stats=stats,
    )


def _find_ksplat_converter() -> Path | None:
    """Locate viewer/tools/convert-to-ksplat.mjs.

    The repo-relative path only works for editable installs; a pip-installed
    package (worker image) lives in site-packages, so the image sets
    SCENEFORGE_KSPLAT_TOOL to the absolute location instead.
    """
    import os

    env = os.environ.get("SCENEFORGE_KSPLAT_TOOL")
    candidates = [Path(env)] if env else []
    candidates.append(
        Path(__file__).resolve().parents[3] / "viewer" / "tools" / "convert-to-ksplat.mjs"
    )
    for c in candidates:
        if c.exists():
            return c
    return None


def compress_splat(ply_path: Path, arrays: dict, out_dir: Path, cfg: SplatConfig) -> tuple[Path, str]:
    """Compress to .ksplat via the bundled node converter; fall back to .splat."""
    node = shutil.which("node")
    converter = _find_ksplat_converter()
    if cfg.export_format == "ksplat" and node and converter is not None:
        ksplat = out_dir / "scene.ksplat"
        proc = subprocess.run(
            [node, str(converter), str(ply_path), str(ksplat)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and ksplat.exists():
            return ksplat, "ksplat"
        log.warning("ksplat conversion failed (%s); falling back to .splat", proc.stderr[-500:])
    splat = write_splat(
        out_dir / "scene.splat",
        means=arrays["means"], scales_log=arrays["scales"], quats=arrays["quats"],
        opacities_logit=arrays["opacities"], sh0=arrays["sh0"],
    )
    return splat, "splat"


# ---- helpers ---------------------------------------------------------------


def _load_dataset(frames_dir: Path, model: ColmapModel):
    """Per-registered-image (viewmat 4x4, K 3x3, path); requires PINHOLE-family model."""
    cams, viewmats, Ks, paths = [], [], [], []
    for im in model.images.values():
        cam = model.cameras[im.camera_id]
        if cam.model in ("PINHOLE", "SIMPLE_PINHOLE"):
            if cam.model == "PINHOLE":
                fx, fy, cx, cy = cam.params[:4]
            else:
                fx = fy = cam.params[0]
                cx, cy = cam.params[1:3]
        else:
            raise PipelineError(
                f"Camera model {cam.model} is not pinhole — run undistort() first"
            )
        p = frames_dir / im.name
        if not p.exists():
            continue
        vm = np.eye(4)
        vm[:3, :3] = im.rotmat()
        vm[:3, 3] = im.tvec
        viewmats.append(vm)
        Ks.append(np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]]))
        cams.append(cam)
        paths.append(p)
    return cams, np.array(viewmats), np.array(Ks), paths


def _load_images(paths: list[Path], device: str):
    import cv2
    import torch

    images = []
    for p in paths:
        bgr = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if bgr is None:
            raise PipelineError(f"Unreadable frame {p}")
        images.append(torch.tensor(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), device=device))
    return images


def _knn_mean_dist(xyz: np.ndarray, k: int = 3) -> np.ndarray:
    from scipy.spatial import cKDTree

    tree = cKDTree(xyz)
    d, _ = tree.query(xyz, k=k + 1)
    return np.clip(d[:, 1:].mean(axis=1), 1e-4, None)


def _crop_outliers(arrays: dict, sparse_xyz: np.ndarray) -> dict:
    """Drop gaussians far outside the sparse-cloud bounding box (floaters)."""
    lo = np.percentile(sparse_xyz, 1, axis=0)
    hi = np.percentile(sparse_xyz, 99, axis=0)
    pad = 0.5 * (hi - lo) + 1e-3
    keep = np.all((arrays["means"] > lo - pad) & (arrays["means"] < hi + pad), axis=1)
    return {k: v[keep] for k, v in arrays.items()}


def _apply_similarity(arrays: dict, T: np.ndarray) -> dict:
    """Apply a 4x4 similarity (rotation+uniform scale+translation) to gaussians."""
    R = T[:3, :3]
    s = float(np.cbrt(max(np.linalg.det(R), 1e-12)))
    Rn = R / s
    out = dict(arrays)
    out["means"] = arrays["means"] @ R.T + T[:3, 3]
    out["scales"] = arrays["scales"] + math.log(s)
    out["quats"] = _rotate_quats(arrays["quats"], Rn)
    return out


def _rotate_quats(quats: np.ndarray, R: np.ndarray) -> np.ndarray:
    """Left-multiply quaternion array (wxyz) by rotation matrix R."""
    w = np.sqrt(max(0.0, 1.0 + R[0, 0] + R[1, 1] + R[2, 2])) / 2.0
    if w < 1e-6:
        # Fall back through the generic conversion for 180° rotations.
        from scipy.spatial.transform import Rotation

        rq = Rotation.from_matrix(R).as_quat()  # xyzw
        q_r = np.array([rq[3], rq[0], rq[1], rq[2]])
    else:
        q_r = np.array(
            [w, (R[2, 1] - R[1, 2]) / (4 * w), (R[0, 2] - R[2, 0]) / (4 * w), (R[1, 0] - R[0, 1]) / (4 * w)]
        )
    w1, x1, y1, z1 = q_r
    w2, x2, y2, z2 = quats[:, 0], quats[:, 1], quats[:, 2], quats[:, 3]
    return np.stack(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        axis=1,
    )


def _render_poster(params, rasterization, torch, viewmats_t, Ks_t, W, H, cfg, out_dir: Path) -> Path | None:
    """Render the median camera view as poster.png (pre-transform frame is fine
    because we render with the matching pre-transform camera)."""
    try:
        import cv2

        i = len(viewmats_t) // 2
        with torch.no_grad():
            renders, _, _ = rasterization(
                means=params["means"], quats=params["quats"],
                scales=torch.exp(params["scales"]),
                opacities=torch.sigmoid(params["opacities"]),
                colors=torch.cat([params["sh0"], params["shN"]], dim=1),
                viewmats=viewmats_t[i : i + 1], Ks=Ks_t[i : i + 1],
                width=W, height=H, sh_degree=cfg.sh_degree, packed=False,
            )
        img = (renders[0].clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
        poster = out_dir / "poster.png"
        cv2.imwrite(str(poster), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        return poster
    except Exception as e:  # poster is best-effort, never fail the scene for it
        log.warning("poster render failed: %s", e)
        return None
