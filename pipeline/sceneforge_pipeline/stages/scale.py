"""Stage 4 — metric scale via door prior (brief §4.4, "the honesty problem").

Monocular SfM is scale-ambiguous. We recover approximate metric scale by
detecting doors with an Apache-2.0 open-vocabulary detector (OWLv2), measuring
each detection's height in reconstruction units through the sparse depth the
SfM already gives us, and fitting a single scale so the median door height is
2.0 m. The method, confidence and ±tolerance are reported in the output —
estimates are never presented as survey-grade.

Fallback chain: door_prior → user-provided dimension → scale 1.0 with
method="none", confidence 0.0 (and a warning in the quality report).

The transformers/torch import happens only inside detect_openings(); every
geometric routine below is pure numpy so it is unit-tested on CPU.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..colmap_model import ColmapModel, Image
from ..config import ScaleConfig
from ..schema import ScaleInfo

log = logging.getLogger(__name__)


@dataclass
class Detection2D:
    image_id: int
    label: str            # normalized: "door", "window", "table", "sofa", ...
    score: float
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in pixels


@dataclass
class Detection3D:
    """A 2D detection lifted to 3D.

    Coordinates are in RAW (unscaled, un-aligned) reconstruction units; the
    pipeline orchestrator applies the gravity+scale similarity via
    `transform(T)` before the semantics stage consumes them.
    """

    label: str
    score: float
    center_world: np.ndarray      # (3,)
    width_world: float
    height_world: float

    def transform(self, T: np.ndarray) -> "Detection3D":
        R = T[:3, :3]
        s = float(np.cbrt(max(np.linalg.det(R), 1e-12)))
        return Detection3D(
            label=self.label,
            score=self.score,
            center_world=R @ self.center_world + T[:3, 3],
            width_world=self.width_world * s,
            height_world=self.height_world * s,
        )


@dataclass
class ScaleResult:
    scale: float                  # multiply reconstruction units by this to get meters
    info: ScaleInfo
    detections_3d: list[Detection3D] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _normalize_label(raw: str) -> str:
    """'a dining table' → 'dining table', 'an oven' → 'oven'."""
    for prefix in ("a ", "an "):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def detect_openings(
    frames_dir: Path, model: ColmapModel, cfg: ScaleConfig
) -> list[Detection2D]:
    """OWLv2 zero-shot detection over the full vocabulary on a frame subsample.

    Doors feed the scale prior; other labels feed object/room labeling in the
    semantics stage.
    """
    try:
        from transformers import pipeline as hf_pipeline
    except ImportError as e:  # pragma: no cover - GPU worker only
        from ..errors import DependencyMissingError

        raise DependencyMissingError(
            f"transformers required for door detection (GPU worker image): {e}"
        ) from e

    detector = hf_pipeline(
        "zero-shot-object-detection", model=cfg.detector_model, device_map="auto"
    )
    images = list(model.images.values())
    idx = np.linspace(0, len(images) - 1, min(cfg.max_frames_scanned, len(images)))
    detections: list[Detection2D] = []
    for i in sorted(set(idx.round().astype(int).tolist())):
        im = images[i]
        path = frames_dir / im.name
        if not path.exists():
            continue
        for det in detector(str(path), candidate_labels=list(cfg.vocabulary)):
            if det["score"] < cfg.detection_score_min:
                continue
            box = det["box"]
            detections.append(
                Detection2D(
                    image_id=im.image_id,
                    label=_normalize_label(det["label"]),
                    score=float(det["score"]),
                    box=(box["xmin"], box["ymin"], box["xmax"], box["ymax"]),
                )
            )
    log.info("scale: %d detections over %d frames", len(detections), len(idx))
    return detections


def median_depth_in_box(model: ColmapModel, image: Image, box: tuple[float, float, float, float]) -> float | None:
    """Median camera-frame depth of triangulated keypoints observed inside the box.

    Uses the 2D observations COLMAP already associates with 3D points — no
    reprojection or dense depth needed.
    """
    x0, y0, x1, y1 = box
    if image.xys.size == 0:
        return None
    inside = (
        (image.xys[:, 0] >= x0) & (image.xys[:, 0] <= x1)
        & (image.xys[:, 1] >= y0) & (image.xys[:, 1] <= y1)
        & (image.point3d_ids >= 0)
    )
    ids = image.point3d_ids[inside]
    pts = np.array([model.points[pid].xyz for pid in ids if int(pid) in model.points])
    if len(pts) < 5:
        return None
    cam_pts = pts @ image.rotmat().T + image.tvec
    depths = cam_pts[:, 2]
    depths = depths[depths > 1e-6]
    if len(depths) < 5:
        return None
    return float(np.median(depths))


def _intrinsics(model: ColmapModel, image: Image) -> tuple[float, float, float, float]:
    cam = model.cameras[image.camera_id]
    if cam.model == "PINHOLE":
        fx, fy, cx, cy = cam.params[:4]
    else:  # SIMPLE_PINHOLE / SIMPLE_RADIAL: shared focal
        fx = fy = cam.params[0]
        cx, cy = cam.params[1], cam.params[2]
    return float(fx), float(fy), float(cx), float(cy)


def lift_detections(model: ColmapModel, detections: list[Detection2D]) -> list[Detection3D]:
    """Back-project 2D boxes to 3D via sparse depth (unscaled units)."""
    out: list[Detection3D] = []
    for det in detections:
        image = model.images.get(det.image_id)
        if image is None:
            continue
        depth = median_depth_in_box(model, image, det.box)
        if depth is None:
            continue
        fx, fy, cx, cy = _intrinsics(model, image)
        x0, y0, x1, y1 = det.box
        u, v = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        cam_pt = np.array([(u - cx) / fx * depth, (v - cy) / fy * depth, depth])
        world = image.rotmat().T @ (cam_pt - image.tvec)
        out.append(
            Detection3D(
                label=det.label,
                score=det.score,
                center_world=world,
                width_world=(x1 - x0) * depth / fx,
                height_world=(y1 - y0) * depth / fy,
            )
        )
    return out


def fit_scale_from_doors(doors: list[Detection3D], cfg: ScaleConfig) -> tuple[float, float, dict]:
    """Scale + confidence from lifted door detections.

    Confidence combines detection count and the spread (MAD/median) of the
    implied scales: many consistent doors ⇒ high confidence.
    """
    heights = np.array([d.height_world for d in doors if d.label == "door" and d.height_world > 1e-6])
    stats: dict = {"door_detections_used": int(len(heights))}
    if len(heights) < cfg.min_door_detections:
        return 1.0, 0.0, {**stats, "reason": "too_few_door_detections"}
    med = float(np.median(heights))
    scale = cfg.door_height_m / med
    rel_mad = float(np.median(np.abs(heights - med)) / med)
    count_term = min(1.0, len(heights) / 10.0)
    spread_term = float(np.clip(1.0 - rel_mad / 0.25, 0.0, 1.0))
    confidence = round(0.5 * count_term + 0.5 * spread_term, 2)
    stats.update({"median_door_height_units": round(med, 4), "rel_mad": round(rel_mad, 3)})
    return scale, confidence, stats


def run_scale(
    frames_dir: Path,
    model: ColmapModel,
    cfg: ScaleConfig,
    known_dimension_m: float | None = None,
    detections: list[Detection2D] | None = None,
) -> ScaleResult:
    """Full scale stage. `detections` can be injected (tests / cached runs)."""
    if detections is None:
        try:
            detections = detect_openings(frames_dir, model, cfg)
        except Exception as e:
            log.warning("scale: detector unavailable/failed (%s); no detections", e)
            detections = []

    dets3d = lift_detections(model, detections)
    scale, confidence, stats = fit_scale_from_doors(dets3d, cfg)

    if confidence > 0.0:
        info = ScaleInfo(method="door_prior", confidence=confidence, tolerance_pct=cfg.tolerance_pct)
    elif known_dimension_m is not None and known_dimension_m > 0:
        # Caller maps the known dimension to reconstruction units at the
        # semantics stage (largest room's long side); here we record intent.
        info = ScaleInfo(method="user_dimension", confidence=0.7, tolerance_pct=cfg.tolerance_pct)
        stats["known_dimension_m"] = known_dimension_m
        scale = 1.0  # resolved later by semantics.apply_user_dimension
    else:
        info = ScaleInfo(method="none", confidence=0.0, tolerance_pct=cfg.tolerance_pct)
        scale = 1.0
        log.warning(
            "scale: no reliable door prior and no user dimension — the orchestrator "
            "will fall back to the camera-height prior or arbitrary units."
        )

    return ScaleResult(scale=scale, info=info, detections_3d=dets3d, stats=stats)
