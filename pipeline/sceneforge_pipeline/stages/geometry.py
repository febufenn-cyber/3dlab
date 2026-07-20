"""Stage 2 — geometry: frames → camera poses + point cloud (brief §4.2).

Backends behind one `GeometryBackend` interface (identical contract, so the
default is one line of config):

* `lingbot` — **default, commercial-safe (Apache-2.0).** Feed-forward streaming
  reconstruction (Robbyant/Ant Group; poses + cloud in one pass, ~20 fps) —
  seconds not minutes, chosen for the GPU budget (D34). Fails honestly via a
  point-confidence gate (a learned model never "fails to register"). See
  stages/lingbot.py.
* `colmap_glomap` — commercial-safe classical **fallback** (`--backend
  colmap_glomap`). COLMAP (BSD-3) feature extraction/matching + global SfM.
  Deterministic; fails honestly by failing to register a bad capture.
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
    # True when the backend's cameras are already an ideal pinhole model with
    # the ORIGINAL frames usable as-is (lingbot). COLMAP needs a separate
    # undistortion pass; lingbot does not — the pipeline branches on this.
    already_undistorted: bool = False


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
    """lingbot-map (Robbyant/Ant Group) feed-forward streaming SfM — **default**.

    Poses + dense point cloud in one forward pass (~20 fps) — seconds not
    minutes, the best fit for the ≤10 min GPU budget. Apache-2.0 (code verified
    via the GitHub license API; weights corroborated + owner-confirmed — see
    LICENSES.md / DECISIONS.md D34). COLMAP remains the classical fallback.

    Honest-failure gate (`_apply_confidence_gate`): a learned model always emits
    poses, so unlike SfM it never "fails to register" a bad capture. We instead
    reject when too few world points clear the confidence threshold — keeping the
    no-hallucination contract even with a feed-forward default. The
    output→ColmapModel conversion is pure and unit-tested; only inference needs
    the GPU. See stages/lingbot.py.
    """

    name = "lingbot"
    commercial_safe = True  # Apache-2.0 (LICENSES.md, verified 2026-07-20)

    def reconstruct(self, frames_dir: Path, workdir: Path, cfg: GeometryConfig) -> GeometryResult:
        from .lingbot import (
            lingbot_confidence_stats,
            lingbot_to_colmap_model,
            run_lingbot_inference,
        )

        frame_names = [p.name for p in sorted(frames_dir.glob("*.jpg"))]
        output = run_lingbot_inference(frames_dir, workdir, cfg)
        conf_stats = lingbot_confidence_stats(output, cfg.lingbot_conf_threshold)
        _apply_confidence_gate(conf_stats, cfg)  # fail loud before building the model
        model = lingbot_to_colmap_model(
            output,
            conf_threshold=cfg.lingbot_conf_threshold,
            max_points=cfg.lingbot_max_points,
            frame_names=frame_names,
        )
        result = summarize_model(
            model,
            total_input=len(frame_names),
            cfg=cfg,
            extra={"backend": self.name, **conf_stats},
            skip_registration_gate=True,  # feed-forward: reg ratio is always 1.0
        )
        result.already_undistorted = True  # lingbot cameras are ideal pinhole
        return result


def _apply_confidence_gate(conf_stats: dict, cfg: GeometryConfig) -> None:
    """Fail loud when the feed-forward reconstruction is too low-confidence to
    trust — the learned-model equivalent of COLMAP's registration gate."""
    if not conf_stats.get("has_confidence"):
        log.warning("lingbot: no confidence channel in output; confidence gate skipped")
        return
    n_conf = conf_stats["n_confident"]
    ratio = conf_stats["confident_ratio"]
    if n_conf < cfg.lingbot_min_confident_points or ratio < cfg.lingbot_min_confident_ratio:
        raise QualityGateError(
            "insufficient_reconstruction_confidence",
            f"lingbot reconstruction is too low-confidence: {n_conf} confident points "
            f"({ratio:.0%} of total; need ≥ {cfg.lingbot_min_confident_points} and "
            f"≥ {cfg.lingbot_min_confident_ratio:.0%}). The capture is likely too sparse, "
            "blurry, or textureless — see docs/CAPTURE_GUIDE.md.",
            {**conf_stats, "capture_rule": "slow_continuous_pan"},
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
    model: ColmapModel, total_input: int, cfg: GeometryConfig, extra: dict | None = None,
    skip_registration_gate: bool = False,
) -> GeometryResult:
    """Registration stats + the fail-fast gate on poor registration (brief §4.1/§6).

    ``skip_registration_gate`` is set by feed-forward backends where every frame
    is always "registered" (ratio 1.0) so that gate is meaningless — they gate
    on confidence instead (see LingbotMapBackend)."""
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
    if not skip_registration_gate and (
        registered < cfg.min_registered_frames or ratio < cfg.min_registered_ratio
    ):
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
