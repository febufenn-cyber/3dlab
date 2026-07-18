"""Stage 2 — geometry: frames → camera poses + point cloud (brief §4.2).

Backends behind one `GeometryBackend` interface (identical contract, so a
default swap is one line):

* `colmap_glomap` — **default, commercial-safe.** COLMAP (BSD-3) feature
  extraction/matching + global SfM mapping. Invoked as external binaries in
  the GPU worker image (api/docker/Dockerfile.worker).
* `lingbot` — feed-forward streaming SfM (Robbyant/Ant Group; poses+cloud in
  one pass). Code Apache-2.0; weights license inferred-permissive and flagged
  (LICENSES.md / DECISIONS.md D29). Opt-in via `--backend lingbot`; see
  stages/lingbot.py for the output→ColmapModel conversion.
* `research_feedforward` — DUSt3R/MASt3R/VGGT class (CC-BY-NC). Registered but
  refuses to run without --research.
"""

from __future__ import annotations

import abc
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..colmap_model import ColmapModel, read_model
from ..config import GeometryConfig
from ..errors import DependencyMissingError, GeometryFailure, QualityGateError

log = logging.getLogger(__name__)


@dataclass
class GeometryResult:
    model: ColmapModel                 # poses + sparse points, COLMAP convention (world→cam)
    registered: int
    total_input: int
    mean_reproj_error: float
    stats: dict


class GeometryBackend(abc.ABC):
    """Contract: frames in, poses + sparse cloud out, COLMAP model convention."""

    name: str = "abstract"
    commercial_safe: bool = False

    @abc.abstractmethod
    def reconstruct(self, frames_dir: Path, workdir: Path, cfg: GeometryConfig) -> GeometryResult:
        ...


class ColmapGlomapBackend(GeometryBackend):
    """COLMAP feature+matching + global SfM mapping. BSD-3 throughout.

    History note (verified 2026-07-17, see LICENSES.md/DECISIONS.md): the
    standalone GLOMAP repo was archived 2026-03-09 and its global mapper was
    merged into COLMAP 4.1.0 as `colmap global_mapper`. This backend prefers
    the merged command when the installed COLMAP has it and falls back to the
    legacy `glomap mapper` binary (final release 1.2.0) otherwise, so both
    older worker images and current COLMAP builds work.
    """

    name = "colmap_glomap"
    commercial_safe = True

    @staticmethod
    def _has_global_mapper(colmap_bin: str) -> bool:
        try:
            proc = subprocess.run(
                [colmap_bin, "help"], capture_output=True, text=True, timeout=30
            )
            return "global_mapper" in (proc.stdout + proc.stderr)
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _run(self, cmd: list[str], log_file: Path) -> None:
        log.info("geometry: %s", " ".join(cmd[:4]) + " ...")
        with open(log_file, "a") as fh:
            fh.write("\n$ " + " ".join(cmd) + "\n")
            fh.flush()
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            tail = log_file.read_text()[-2000:]
            raise GeometryFailure(
                f"{cmd[0]} exited with {proc.returncode}. Log tail:\n{tail}"
            )

    def reconstruct(self, frames_dir: Path, workdir: Path, cfg: GeometryConfig) -> GeometryResult:
        if shutil.which(cfg.colmap_bin) is None:
            raise DependencyMissingError(
                f"'{cfg.colmap_bin}' not found on PATH. The geometry stage runs inside "
                "the GPU worker image (api/docker/Dockerfile.worker)."
            )
        use_merged_mapper = self._has_global_mapper(cfg.colmap_bin)
        if not use_merged_mapper and shutil.which(cfg.glomap_bin) is None:
            raise DependencyMissingError(
                f"Neither 'colmap global_mapper' (COLMAP ≥ 4.1) nor a '{cfg.glomap_bin}' "
                "binary is available — one global mapper is required."
            )
        sfm_dir = workdir / "sfm"
        sfm_dir.mkdir(parents=True, exist_ok=True)
        db = sfm_dir / "database.db"
        log_file = sfm_dir / "colmap.log"
        if db.exists():
            db.unlink()

        # 1) Features. SIMPLE_RADIAL + single camera: one phone, fixed lens.
        self._run(
            [
                cfg.colmap_bin, "feature_extractor",
                "--database_path", str(db),
                "--image_path", str(frames_dir),
                "--ImageReader.camera_model", "SIMPLE_RADIAL",
                "--ImageReader.single_camera", "1",
                "--SiftExtraction.use_gpu", "1",
            ],
            log_file,
        )
        # 2) Sequential matching (video!) with loop detection (brief §4.2).
        matcher_args = [
            cfg.colmap_bin, "sequential_matcher",
            "--database_path", str(db),
            "--SiftMatching.use_gpu", "1",
            "--SequentialMatching.overlap", "15",
        ]
        if cfg.loop_detection:
            # Loop detection needs a vocab tree; the worker image bakes one in.
            vocab = Path("/opt/colmap/vocab_tree_flickr100K_words32K.bin")
            if vocab.exists():
                matcher_args += [
                    "--SequentialMatching.loop_detection", "1",
                    "--SequentialMatching.vocab_tree_path", str(vocab),
                ]
            else:
                log.warning("vocab tree missing at %s — loop detection disabled", vocab)
        self._run(matcher_args, log_file)

        # 3) Global SfM (much faster than incremental mapping): merged
        #    `colmap global_mapper` (≥4.1) or the legacy standalone glomap.
        out_dir = sfm_dir / "global_sfm"
        out_dir.mkdir(exist_ok=True)
        if use_merged_mapper:
            mapper_cmd = [cfg.colmap_bin, "global_mapper"]
        else:
            mapper_cmd = [cfg.glomap_bin, "mapper"]
        self._run(
            [
                *mapper_cmd,
                "--database_path", str(db),
                "--image_path", str(frames_dir),
                "--output_path", str(out_dir),
            ],
            log_file,
        )

        model_dir = self._pick_largest_model(out_dir)
        model = read_model(model_dir)
        return summarize_model(
            model,
            total_input=len(list(frames_dir.glob("*.jpg"))),
            cfg=cfg,
            extra={"backend": self.name, "model_dir": str(model_dir)},
        )

    @staticmethod
    def _pick_largest_model(out_dir: Path) -> Path:
        """GLOMAP/COLMAP may emit several disconnected models (0/, 1/, …): take the largest."""
        candidates = [d for d in sorted(out_dir.iterdir()) if (d / "images.bin").exists()]
        if not candidates:
            raise GeometryFailure(f"No reconstruction produced under {out_dir}")
        def n_images(d: Path) -> int:
            from ..colmap_model import read_images_binary
            return len(read_images_binary(d / "images.bin"))
        best = max(candidates, key=n_images)
        if len(candidates) > 1:
            log.warning(
                "geometry: %d disconnected models; using largest (%s). "
                "The capture likely broke continuity — this lowers coverage.",
                len(candidates), best.name,
            )
        return best


class ResearchOnlyBackend(GeometryBackend):
    """Placeholder for DUSt3R / MASt3R / VGGT (non-commercial licenses).

    Deliberately unimplemented in v1: it exists so the CLI flag, config plumbing
    and license gating are exercised end-to-end. Implementing it is a
    self-contained task once a license-compatible checkpoint is chosen.
    """

    name = "research_feedforward"
    commercial_safe = False

    def reconstruct(self, frames_dir: Path, workdir: Path, cfg: GeometryConfig) -> GeometryResult:
        raise DependencyMissingError(
            "The research geometry backend (DUSt3R/MASt3R/VGGT class) is not bundled: "
            "the checkpoints are non-commercial (see LICENSES.md). Install one manually "
            "and implement ResearchOnlyBackend.reconstruct — the interface is frozen."
        )


class LingbotMapBackend(GeometryBackend):
    """lingbot-map (Robbyant/Ant Group) feed-forward streaming SfM.

    Poses + dense point cloud in one forward pass — far under the GPU budget
    versus incremental SfM. CODE is Apache-2.0 (verified); model WEIGHTS carry
    no separate license on the repo and the HF card is unreachable from this
    build's proxy, so the weights license is recorded as *inferred* Apache-2.0
    and flagged for direct confirmation (see LICENSES.md / DECISIONS.md D28).
    Because that is unresolved, this backend is NOT the default — COLMAP is —
    but it is fully selectable via `--backend lingbot`. The heavy lifting (the
    output→ColmapModel conversion) is pure and unit-tested; only inference
    needs the GPU. See stages/lingbot.py.
    """

    name = "lingbot"
    commercial_safe = True  # code Apache-2.0; weights inferred-permissive, see LICENSES.md

    def reconstruct(self, frames_dir: Path, workdir: Path, cfg: GeometryConfig) -> GeometryResult:
        from .lingbot import lingbot_to_colmap_model, run_lingbot_inference

        frame_names = [p.name for p in sorted(frames_dir.glob("*.jpg"))]
        output = run_lingbot_inference(frames_dir, workdir, cfg)
        model = lingbot_to_colmap_model(
            output,
            conf_threshold=cfg.lingbot_conf_threshold,
            max_points=cfg.lingbot_max_points,
            frame_names=frame_names,
        )
        return summarize_model(
            model,
            total_input=len(frame_names),
            cfg=cfg,
            extra={"backend": self.name},
        )


_BACKENDS: dict[str, type[GeometryBackend]] = {
    ColmapGlomapBackend.name: ColmapGlomapBackend,
    LingbotMapBackend.name: LingbotMapBackend,
    ResearchOnlyBackend.name: ResearchOnlyBackend,
}


def get_backend(name: str, research: bool) -> GeometryBackend:
    try:
        backend_cls = _BACKENDS[name]
    except KeyError:
        raise ValueError(f"Unknown geometry backend {name!r}; known: {sorted(_BACKENDS)}")
    backend = backend_cls()
    if not backend.commercial_safe and not research:
        raise PermissionError(
            f"Geometry backend {name!r} is research-only (non-commercial license). "
            "Re-run with --research if and only if this is not a commercial deployment."
        )
    return backend


def summarize_model(
    model: ColmapModel, total_input: int, cfg: GeometryConfig, extra: dict | None = None
) -> GeometryResult:
    """Registration stats + the fail-fast gate on poor registration (brief §4.1/§6)."""
    registered = len(model.images)
    errors = [p.error for p in model.points.values() if p.error >= 0]
    mean_err = float(np.mean(errors)) if errors else float("nan")
    ratio = registered / total_input if total_input else 0.0
    stats = {
        "registered_frames": registered,
        "input_frames": total_input,
        "registered_ratio": round(ratio, 3),
        "sparse_points": len(model.points),
        "mean_reproj_error_px": round(mean_err, 3) if errors else None,
        **(extra or {}),
    }
    if registered < cfg.min_registered_frames or ratio < cfg.min_registered_ratio:
        raise QualityGateError(
            "insufficient_registration",
            f"Only {registered}/{total_input} frames registered in SfM "
            f"(need ≥ {cfg.min_registered_frames} and ≥ {cfg.min_registered_ratio:.0%}). "
            "Typical causes: fast rotation, textureless walls filling the frame, "
            "mirrors, or gaps in the walkthrough. See docs/CAPTURE_GUIDE.md.",
            {**stats, "capture_rule": "slow_continuous_pan"},
        )
    return GeometryResult(
        model=model,
        registered=registered,
        total_input=total_input,
        mean_reproj_error=mean_err,
        stats=stats,
    )
