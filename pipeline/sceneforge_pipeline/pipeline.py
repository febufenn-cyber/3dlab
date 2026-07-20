"""Pipeline orchestrator: video → {splat, semantic.json, floorplan.svg,
quality_report.json, viewer.html} (brief §6 Phase 1).

Stage order and the coordinate-frame contract:

  ingest → geometry (raw SfM frame) → scale (door prior, raw units)
        → gravity alignment T_g → similarity T = S(s)·T_g
        → semantics (metric Z-up frame) → [user-dimension correction c]
        → floorplan/quality (contract frame) → splat training (raw frame,
          exported through T_final = S(c)·T so every asset shares one frame)

On a capture-contract violation the run fails fast: quality_report.json is
written with status="failed" and the QualityGateError re-raised — no partial
splat, no invented geometry (brief §2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import PipelineConfig
from .errors import PipelineError, QualityGateError
from .schema import Assets, SceneSemantics, ScaleInfo, dump_semantics
from .stages import ingest as ingest_stage
from .stages import semantics as sem_stage
from .stages.floorplan import render_floorplan_svg
from .stages.geometry import get_backend
from .stages.quality import build_quality, write_quality_report
from .stages.scale import run_scale
from .viewer_html import write_viewer_html

log = logging.getLogger(__name__)


@dataclass
class BuildResult:
    scene_id: str
    out_dir: Path
    semantic_path: Path
    floorplan_path: Path
    quality_report_path: Path
    splat_path: Path | None
    viewer_path: Path | None
    timings_s: dict[str, float] = field(default_factory=dict)


def scene_id_for(video: Path) -> str:
    """Deterministic scene id from content: scn_ + 12 hex of sha256."""
    h = hashlib.sha256()
    with open(video, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "scn_" + h.hexdigest()[:12]


def _similarity(scale: float, rigid: np.ndarray | None = None) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] *= scale
    T[3, 3] = 1.0
    if rigid is not None:
        T = T @ rigid
    return T


def build_scene(
    video: Path,
    out_dir: Path,
    cfg: PipelineConfig | None = None,
    skip_splat: bool = False,
) -> BuildResult:
    cfg = cfg or PipelineConfig()
    video = Path(video)
    if not video.exists():
        raise PipelineError(f"input video not found: {video}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = out_dir / "work"
    workdir.mkdir(exist_ok=True)

    scene_id = scene_id_for(video)
    timings: dict[str, float] = {}
    quality_report_path = out_dir / "quality_report.json"

    def _timed(name: str):
        class _T:
            def __enter__(self):
                self.t0 = time.time()

            def __exit__(self, *a):
                timings[name] = time.time() - self.t0

        return _T()

    try:
        # ---- 1. ingest ------------------------------------------------------
        with _timed("ingest"):
            ing = ingest_stage.run_ingest(video, workdir, cfg.ingest)

        # ---- 2. geometry ----------------------------------------------------
        with _timed("geometry"):
            backend = get_backend(cfg.geometry.backend, research=cfg.research)
            geo = backend.reconstruct(ing.frames_dir, workdir, cfg.geometry)
        model = geo.model

        # ---- 3. metric scale (raw units) -------------------------------------
        with _timed("scale"):
            scl = run_scale(
                ing.frames_dir, model, cfg.scale, known_dimension_m=cfg.known_dimension_m
            )

        # ---- 4. gravity + similarity -----------------------------------------
        xyz, _rgb = model.points_array()
        cam_centers = np.stack([im.cam_center() for im in model.images.values()])
        T_g = sem_stage.estimate_gravity_transform(xyz, cam_centers)

        scale_info = scl.info
        s = scl.scale
        if scale_info.method in ("none", "user_dimension"):
            # Provisional normalization so the wall-slice band lands sensibly.
            cams_g = sem_stage.apply_transform(cam_centers, T_g)
            pts_g = sem_stage.apply_transform(xyz, T_g)
            s = sem_stage.camera_height_scale(pts_g, cams_g)
            if scale_info.method == "none" and 0.1 < s < 10.0:
                scale_info = ScaleInfo(
                    method="camera_height_prior",
                    confidence=0.3,
                    tolerance_pct=max(cfg.scale.tolerance_pct, 15.0),
                )
        T = _similarity(s, T_g)

        points_m = sem_stage.apply_transform(xyz, T)
        cams_m = sem_stage.apply_transform(cam_centers, T)
        dets_m = [d.transform(T) for d in scl.detections_3d]

        # ---- 5. semantics -----------------------------------------------------
        with _timed("semantics"):
            sem = sem_stage.extract_semantics(points_m, cams_m, dets_m, cfg.semantics)

        correction = 1.0
        if scl.info.method == "user_dimension" and cfg.known_dimension_m:
            correction = sem_stage.apply_user_dimension(sem, cfg.known_dimension_m)
            log.info("scale: user dimension applied (correction ×%.3f)", correction)
        T_final = _similarity(correction) @ T

        # ---- 6. quality -------------------------------------------------------
        quality = build_quality(
            ingest_stats=ing.stats,
            geometry_stats=geo.stats,
            coverage=sem.coverage,
            scale_confidence=scale_info.confidence,
            scale_method=scale_info.method,
        )

        # ---- 7. splat ---------------------------------------------------------
        splat_path: Path | None = None
        viewer_path: Path | None = None
        splat_stats: dict = {}
        poster_name: str | None = None
        if not skip_splat:
            from .stages.splat import train_splat, undistort  # lazy: torch/gsplat

            with _timed("splat"):
                if geo.already_undistorted:
                    # Feed-forward backends (lingbot) emit an ideal pinhole model;
                    # the original frames are used as-is, no COLMAP undistortion.
                    und_images, und_model = ing.frames_dir, geo.model
                else:
                    model_dir = Path(geo.stats["model_dir"])
                    und_images, und_sparse = undistort(
                        ing.frames_dir, model_dir, workdir, cfg.geometry.colmap_bin
                    )
                    from .colmap_model import read_model

                    und_model = read_model(und_sparse)
                sp = train_splat(
                    und_images, und_model, workdir, cfg.splat, out_dir,
                    world_transform=T_final,
                )
            splat_path = sp.compact_path
            splat_stats = sp.stats
            poster_name = sp.poster_path.name if sp.poster_path else None

        # ---- 8. write the contract artifacts ----------------------------------
        scene = SceneSemantics(
            scene_id=scene_id,
            scale=scale_info,
            rooms=sem.rooms,
            walls=sem.walls,
            openings=sem.openings,
            objects=sem.objects,
            assets=Assets(
                splat_url=splat_path.name if splat_path else None,
                floorplan_svg_url="floorplan.svg",
                poster_url=poster_name,
            ),
            quality=quality,
        )
        semantic_path = out_dir / "semantic.json"
        semantic_path.write_text(dump_semantics(scene), encoding="utf-8")

        floorplan_path = render_floorplan_svg(scene, out_dir / "floorplan.svg")

        if splat_path is not None:
            look_at = points_m.mean(axis=0) * correction
            cam0 = np.median(cams_m, axis=0) * correction
            viewer_path = write_viewer_html(
                out_dir / "viewer.html",
                scene_id=scene_id,
                splat_filename=splat_path.name,
                quality=quality.model_dump(),
                scale=scale_info.model_dump(),
                initial_pos=[float(v) for v in cam0],
                look_at=[float(v) for v in look_at],
            )

        write_quality_report(
            quality_report_path,
            status="succeeded",
            quality=quality,
            stage_timings_s=timings,
            extra={
                "scene_id": scene_id,
                "ingest": ing.stats,
                "geometry": geo.stats,
                "scale": {**scl.stats, "method": scale_info.method,
                          "confidence": scale_info.confidence},
                "splat": splat_stats,
                "coverage": sem.coverage,
            },
        )
        log.info("scene %s built in %.1fs", scene_id, sum(timings.values()))
        return BuildResult(
            scene_id=scene_id,
            out_dir=out_dir,
            semantic_path=semantic_path,
            floorplan_path=floorplan_path,
            quality_report_path=quality_report_path,
            splat_path=splat_path,
            viewer_path=viewer_path,
            timings_s=timings,
        )

    except QualityGateError as e:
        write_quality_report(
            quality_report_path,
            status="failed",
            quality=None,
            stage_timings_s=timings,
            extra={"scene_id": scene_id, "failure": e.report},
        )
        log.error("scene %s failed capture-quality gate: %s", scene_id, e)
        raise
    except PipelineError as e:
        write_quality_report(
            quality_report_path,
            status="failed",
            quality=None,
            stage_timings_s=timings,
            extra={
                "scene_id": scene_id,
                "failure": {"reason_code": "pipeline_error", "message": str(e)},
            },
        )
        raise
