#!/usr/bin/env python3
"""Generate the SYNTHETIC sample semantics + floor plan for viewer/demo/sample/.

Companion to make-sample-scene.py: emits the semantic.json and floorplan.svg
that correspond to the SAME procedurally generated 4×3×2.6 m room the sample
scene.ksplat shows — so the app UI's "Insights" and "Floor plan" tabs render
real pipeline output formats without pretending a capture happened.

Honesty rules (brief §2, no-hallucination):
- The document is built from the generator's ground-truth constants, NOT from
  a reconstruction — so `scale.method` is "user_dimension" with confidence 1.0
  (the dimensions are literally known), and `quality.warnings` carries an
  explicit "synthetic sample" warning that the UI surfaces.
- The floor plan is rendered by the pipeline's own render_floorplan_svg, the
  exact code path a real scene uses.

Usage: python3 viewer/tools/make-sample-semantics.py
"""

from __future__ import annotations

from pathlib import Path

from sceneforge_pipeline.schema import (
    Assets,
    ObjectBBox,
    Opening,
    Quality,
    Room,
    ScaleInfo,
    SceneObject,
    SceneSemantics,
    Wall,
    dump_semantics,
)
from sceneforge_pipeline.stages.floorplan import render_floorplan_svg

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = ROOT / "viewer" / "demo" / "sample"

# Ground-truth constants — keep in sync with make-sample-scene.py.
ROOM_X, ROOM_Y, ROOM_Z = 4.0, 3.0, 2.6
DOOR_W, DOOR_H = 0.9, 2.0
DOOR_CY = (ROOM_Y - DOOR_W) / 2.0 + 0.5 + DOOR_W / 2.0   # gap y∈[1.55,2.45]
TABLE_W, TABLE_D, TABLE_Z = 1.2, 0.6, 0.72
TABLE_CX, TABLE_CY = 2.5, 1.8
RUG_W, RUG_D = 2.0, 1.4


def build() -> SceneSemantics:
    return SceneSemantics(
        # id charset is [a-z0-9]{8,32}; "0" reads as "synthetic sample zero".
        scene_id="scn_sample0synthetic",
        scale=ScaleInfo(method="user_dimension", confidence=1.0, tolerance_pct=0.0),
        rooms=[
            Room(
                id="room_0",
                label="room",
                polygon=[(0.0, 0.0), (ROOM_X, 0.0), (ROOM_X, ROOM_Y), (0.0, ROOM_Y)],
                area_m2=ROOM_X * ROOM_Y,
                ceiling_height_m=ROOM_Z,
            )
        ],
        walls=[
            Wall(id="wall_0", start=(0.0, 0.0), end=(ROOM_X, 0.0), height_m=ROOM_Z),
            Wall(id="wall_1", start=(ROOM_X, 0.0), end=(ROOM_X, ROOM_Y), height_m=ROOM_Z),
            Wall(id="wall_2", start=(ROOM_X, ROOM_Y), end=(0.0, ROOM_Y), height_m=ROOM_Z),
            Wall(id="wall_3", start=(0.0, ROOM_Y), end=(0.0, 0.0), height_m=ROOM_Z),
        ],
        openings=[
            Opening(
                id="door_0",
                type="door",
                wall_id="wall_1",
                center=(ROOM_X, DOOR_CY, DOOR_H / 2.0),
                width_m=DOOR_W,
                height_m=DOOR_H,
            )
        ],
        objects=[
            SceneObject(
                id="obj_0",
                label="table",
                bbox=ObjectBBox(
                    center=(TABLE_CX, TABLE_CY, TABLE_Z / 2.0),
                    size=(TABLE_W, TABLE_D, TABLE_Z),
                    rot_z=0.0,
                ),
                confidence=1.0,
            ),
            SceneObject(
                id="obj_1",
                label="rug",
                bbox=ObjectBBox(
                    center=(TABLE_CX, TABLE_CY, 0.006),
                    size=(RUG_W, RUG_D, 0.012),
                    rot_z=0.0,
                ),
                confidence=1.0,
            ),
        ],
        assets=Assets(
            splat_url="./scene.ksplat",
            floorplan_svg_url="./floorplan.svg",
            poster_url="./poster.png",
        ),
        quality=Quality(
            coverage_pct=100.0,
            registered_frames=0,
            blur_rejected_pct=0.0,
            reconstruction_confidence=1.0,
            warnings=[
                "synthetic sample: procedurally generated, not a real capture",
            ],
        ),
    )


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    doc = build()
    (SAMPLE_DIR / "semantic.json").write_text(dump_semantics(doc) + "\n")
    render_floorplan_svg(doc, SAMPLE_DIR / "floorplan.svg")
    print(f"wrote {SAMPLE_DIR / 'semantic.json'} + floorplan.svg")


if __name__ == "__main__":
    main()
