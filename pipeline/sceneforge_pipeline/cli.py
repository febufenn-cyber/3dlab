"""rf-scene — the SceneForge pipeline CLI (brief §6 Phase 1).

  rf-scene build video.mp4 -o out/ [--quality fast|standard] [--research]
           [--known-dimension METERS] [--skip-splat] [--backend NAME]
  rf-scene schema            # print the frozen semantic JSON Schema
  rf-scene validate FILE     # validate a semantic.json against the contract

Exit codes: 0 ok · 2 capture rejected by quality gate (report written) ·
3 pipeline/dependency error · 4 validation error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import PipelineConfig
from .errors import DependencyMissingError, PipelineError, QualityGateError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rf-scene", description=__doc__)
    p.add_argument("--version", action="version", version=f"rf-scene {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="video → splat + semantics + floor plan")
    b.add_argument("video", type=Path)
    b.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    b.add_argument("--quality", choices=["fast", "standard"], default="standard")
    b.add_argument(
        "--research", action="store_true",
        help="allow non-commercially-licensed backends (see LICENSES.md) — "
             "NEVER use on the commercial path",
    )
    b.add_argument(
        "--known-dimension", type=float, default=None, metavar="METERS",
        help="fallback scale: the longest side of the largest room, in meters",
    )
    b.add_argument(
        "--backend", default=None,
        choices=["lingbot", "colmap_glomap", "research_feedforward"],
        help="geometry backend (default 'lingbot' feed-forward, Apache-2.0; "
             "'colmap_glomap' is the classical fallback)",
    )
    b.add_argument(
        "--skip-splat", action="store_true",
        help="stop after semantics/floorplan (no GPU needed; for debugging)",
    )

    sub.add_parser("schema", help="print the frozen semantic JSON Schema")

    v = sub.add_parser("validate", help="validate a semantic.json file")
    v.add_argument("file", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.command == "schema":
        from .schema import json_schema

        print(json.dumps(json_schema(), indent=2))
        return 0

    if args.command == "validate":
        from pydantic import ValidationError

        from .schema import validate_semantics

        try:
            validate_semantics(json.loads(args.file.read_text()))
        except (ValidationError, json.JSONDecodeError) as e:
            print(f"INVALID: {e}", file=sys.stderr)
            return 4
        print(f"{args.file}: valid (schema 1.0)")
        return 0

    # build
    from dataclasses import replace

    from .pipeline import build_scene

    cfg = PipelineConfig.preset(
        args.quality, research=args.research, known_dimension_m=args.known_dimension
    )
    if args.backend:
        cfg = replace(cfg, geometry=replace(cfg.geometry, backend=args.backend))

    try:
        result = build_scene(args.video, args.out, cfg, skip_splat=args.skip_splat)
    except QualityGateError as e:
        print(
            f"\nCAPTURE REJECTED ({e.reason_code}): {e}\n"
            f"Details: {args.out / 'quality_report.json'}\n"
            "See docs/CAPTURE_GUIDE.md for how to record a valid walkthrough.",
            file=sys.stderr,
        )
        return 2
    except (DependencyMissingError, PipelineError, PermissionError) as e:
        print(f"\nPIPELINE ERROR: {e}", file=sys.stderr)
        return 3

    print(f"\nScene {result.scene_id} built → {result.out_dir}/")
    for label, path in [
        ("semantic JSON", result.semantic_path),
        ("floor plan", result.floorplan_path),
        ("quality report", result.quality_report_path),
        ("splat", result.splat_path),
        ("viewer", result.viewer_path),
    ]:
        if path is not None:
            print(f"  {label:14s} {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
