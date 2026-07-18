"""Pipeline configuration: every tunable in one place, with the quality presets.

Presets are sized so `fast` fits well under the ~10 min GPU budget on a
T4/A10 for a 1–2 room scene and `standard` stays inside it (brief §2, §4.3).
Wall-clock evidence lives in docs/RUNBOOK.md; update both together.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

Quality = Literal["fast", "standard"]


@dataclass(frozen=True)
class IngestConfig:
    fps: float = 3.0                    # brief §4.1: 2–4 fps
    max_frames: int = 300               # hard cap (brief: 150–300)
    min_frames: int = 40                # below this the capture contract fails
    long_side_px: int = 1280            # brief §4.3 resolution cap, applied at extraction
    blur_threshold: float = 60.0        # variance-of-Laplacian on grayscale (1280px frames)
    dedup_correlation: float = 0.985    # NCC of downsampled consecutive grays above this = near-duplicate
    min_duration_s: float = 20.0        # <20 s cannot cover a room; contract says 60–120 s
    max_duration_s: float = 300.0


@dataclass(frozen=True)
class GeometryConfig:
    backend: str = "colmap_glomap"      # default commercial-safe path (brief §4.2)
    matcher: str = "sequential"         # video ⇒ sequential matching with loop detection
    loop_detection: bool = True
    min_registered_frames: int = 30     # quality gate: fewer ⇒ fail fast
    min_registered_ratio: float = 0.55  # registered / extracted-kept
    colmap_bin: str = "colmap"
    glomap_bin: str = "glomap"
    # lingbot-map feed-forward backend (opt-in `--backend lingbot`; see
    # LICENSES.md — code Apache-2.0, weights license inferred/unverified).
    lingbot_python: str = "python"
    lingbot_module: str = "lingbot_map.inference"
    lingbot_model_path: str = "/opt/lingbot-map/weights"
    lingbot_mode: str = "streaming"     # streaming | windowed
    lingbot_use_sdpa: bool = False      # set True on GPUs without FlashInfer (e.g. T4/SM75)
    lingbot_conf_threshold: float = 0.5
    lingbot_max_points: int = 200_000


@dataclass(frozen=True)
class SplatConfig:
    iterations: int = 15_000
    sh_degree: int = 2                  # SH2 halves payload vs SH3; interiors rarely need SH3
    max_gaussians: int = 700_000        # capped so .splat/.ksplat lands ≤ 25 MB (brief §4.3)
    export_format: str = "ksplat"       # ksplat via viewer/tools converter; .splat fallback
    target_max_mb: float = 25.0


@dataclass(frozen=True)
class ScaleConfig:
    door_height_m: float = 2.0          # scale prior (brief §4.4)
    detector: str = "owlv2"             # Apache-2.0 checkpoint, verified in LICENSES.md
    detector_model: str = "google/owlv2-base-patch16-ensemble"
    min_door_detections: int = 3
    detection_score_min: float = 0.30
    max_frames_scanned: int = 40        # detector runs on a subsample of registered frames
    tolerance_pct: float = 10.0
    # Open-vocabulary queries: doors drive the scale prior; the rest label
    # object clusters and vote on room labels in the semantics stage.
    vocabulary: tuple[str, ...] = (
        "a door", "a window", "a table", "a dining table", "a chair", "a sofa",
        "a bed", "a refrigerator", "a television", "a lamp", "a wardrobe",
        "a toilet", "a sink", "an oven", "a plant",
    )


@dataclass(frozen=True)
class SemanticsConfig:
    grid_res_m: float = 0.05            # occupancy grid cell size
    slice_z_min_m: float = 1.0          # wall-slice band (brief §4.5)
    slice_z_max_m: float = 1.5
    min_room_area_m2: float = 1.5
    door_gap_min_m: float = 0.55
    door_gap_max_m: float = 1.30
    object_z_min_m: float = 0.10
    object_z_max_m: float = 1.80
    object_min_points: int = 60
    object_cluster_eps_m: float = 0.15


@dataclass(frozen=True)
class PipelineConfig:
    quality: Quality = "standard"
    research: bool = False              # unlocks non-commercial backends; see LICENSES.md
    known_dimension_m: float | None = None  # user-provided fallback scale (largest room's long side)
    workdir: Path = Path("workdir")
    ingest: IngestConfig = field(default_factory=IngestConfig)
    geometry: GeometryConfig = field(default_factory=GeometryConfig)
    splat: SplatConfig = field(default_factory=SplatConfig)
    scale: ScaleConfig = field(default_factory=ScaleConfig)
    semantics: SemanticsConfig = field(default_factory=SemanticsConfig)

    @staticmethod
    def preset(quality: Quality = "standard", **overrides) -> "PipelineConfig":
        cfg = PipelineConfig(quality=quality, **overrides)
        if quality == "fast":
            cfg = replace(
                cfg,
                ingest=replace(cfg.ingest, fps=2.0, max_frames=180, long_side_px=1024),
                splat=replace(cfg.splat, iterations=7_000, max_gaussians=450_000, sh_degree=1),
            )
        return cfg
