"""Stage 7 — quality report: what the reconstruction actually covered.

This is the anti-hallucination contract (brief §2): coverage numbers and
warnings are computed from evidence (registration stats, boundary coverage)
and are shipped both inside semantic.json (`quality`) and as a standalone
quality_report.json. A failed run emits the same report shape with
status="failed" so API consumers handle one format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import Quality


def build_quality(
    ingest_stats: dict[str, Any],
    geometry_stats: dict[str, Any],
    coverage: dict[str, Any],
    scale_confidence: float,
    scale_method: str,
    splat_stats: dict[str, Any] | None = None,
) -> Quality:
    warnings: list[str] = []

    per_room = coverage.get("per_room", {})
    for room_id, pct in per_room.items():
        if pct < 80.0:
            warnings.append(f"{room_id} partially covered ({pct:.0f}% of boundary observed)")

    reg_ratio = geometry_stats.get("registered_ratio", 0.0)
    if reg_ratio < 0.75:
        warnings.append(
            f"only {reg_ratio:.0%} of frames registered — some areas may be missing"
        )
    if scale_method == "none":
        warnings.append("no metric scale recovered — dimensions are in arbitrary units")
    elif scale_method == "camera_height_prior":
        warnings.append(
            "scale from camera-height prior only (no door detected, no user dimension) — "
            "dimension error may exceed the stated tolerance"
        )
    elif scale_confidence < 0.5:
        warnings.append(f"low scale confidence ({scale_confidence:.2f}) — treat dimensions as rough")
    if splat_stats and splat_stats.get("compact_mb", 0) > splat_stats.get("target_max_mb", 25):
        warnings.append("splat exceeds the 25 MB mobile budget")

    # Overall coverage: boundary coverage dominates; registration modulates it.
    boundary_pct = coverage.get("overall_pct", 0.0)
    coverage_pct = round(boundary_pct * (0.5 + 0.5 * min(1.0, reg_ratio / 0.9)), 1)

    # Reconstruction confidence: registration, reprojection error, point density.
    err = geometry_stats.get("mean_reproj_error_px")
    err_term = 1.0 if err is None else max(0.0, min(1.0, (2.5 - float(err)) / 2.0))
    pts_term = min(1.0, geometry_stats.get("sparse_points", 0) / 20_000.0)
    confidence = round(
        max(0.0, min(1.0, 0.5 * min(1.0, reg_ratio / 0.9) + 0.3 * err_term + 0.2 * pts_term)), 2
    )

    return Quality(
        coverage_pct=min(100.0, max(0.0, coverage_pct)),
        registered_frames=int(geometry_stats.get("registered_frames", 0)),
        blur_rejected_pct=float(ingest_stats.get("blur_rejected_pct", 0.0)),
        reconstruction_confidence=confidence,
        warnings=warnings,
    )


def write_quality_report(
    path: Path,
    status: str,
    quality: Quality | None,
    stage_timings_s: dict[str, float],
    extra: dict[str, Any] | None = None,
) -> Path:
    """Standalone quality_report.json — the same file for success and failure."""
    doc: dict[str, Any] = {
        "status": status,  # succeeded | failed
        "quality": quality.model_dump(mode="json") if quality else None,
        "stage_timings_s": {k: round(v, 1) for k, v in stage_timings_s.items()},
    }
    if extra:
        doc.update(extra)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return path
