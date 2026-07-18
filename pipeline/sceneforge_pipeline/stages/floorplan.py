"""Stage 6 — 2D floor plan SVG rendered from the semantic JSON (brief §4.5).

Deliberately dependency-free (hand-built SVG): svgwrite is unmaintained and
this stays trivially auditable. The plan is drawn from the *contract* — the
validated SceneSemantics document — never from internal pipeline state, so
anything a customer renders from the JSON matches our SVG.

Honesty features: a scale bar, per-dimension ± tolerance from scale.confidence,
and a fixed "not survey-grade" disclaimer (brief §4.4).
"""

from __future__ import annotations

import math
from pathlib import Path

from ..schema import SceneSemantics

_FONT = "font-family='Helvetica,Arial,sans-serif'"

_ROOM_FILLS = ["#e8f0fe", "#e6f4ea", "#fef7e0", "#fce8e6", "#f3e8fd", "#e0f7fa"]


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_floorplan_svg(scene: SceneSemantics, out_path: Path, px_per_m: float = 60.0) -> Path:
    xs: list[float] = []
    ys: list[float] = []
    for room in scene.rooms:
        xs += [p[0] for p in room.polygon]
        ys += [p[1] for p in room.polygon]
    for wall in scene.walls:
        xs += [wall.start[0], wall.end[0]]
        ys += [wall.start[1], wall.end[1]]
    if not xs:
        xs, ys = [0.0, 1.0], [0.0, 1.0]

    pad_m = 1.0
    x0, y0 = min(xs) - pad_m, min(ys) - pad_m
    x1, y1 = max(xs) + pad_m, max(ys) + pad_m
    width = (x1 - x0) * px_per_m
    height = (y1 - y0) * px_per_m + 70  # footer band

    def X(x: float) -> float:
        return (x - x0) * px_per_m

    def Y(y: float) -> float:
        # SVG y grows downward; keep plan "north up".
        return (y1 - y) * px_per_m

    parts: list[str] = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width:.0f}' height='{height:.0f}' "
        f"viewBox='0 0 {width:.0f} {height:.0f}'>",
        f"<rect width='100%' height='100%' fill='white'/>",
    ]

    # Rooms (fill + label + area).
    for i, room in enumerate(scene.rooms):
        pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in room.polygon)
        fill = _ROOM_FILLS[i % len(_ROOM_FILLS)]
        parts.append(f"<polygon points='{pts}' fill='{fill}' stroke='none'/>")
        cx = sum(p[0] for p in room.polygon) / len(room.polygon)
        cy = sum(p[1] for p in room.polygon) / len(room.polygon)
        label = _esc(room.label.replace("_", " "))
        parts.append(
            f"<text x='{X(cx):.1f}' y='{Y(cy):.1f}' {_FONT} font-size='14' text-anchor='middle' fill='#333'>"
            f"{label}</text>"
            f"<text x='{X(cx):.1f}' y='{Y(cy) + 16:.1f}' {_FONT} font-size='11' text-anchor='middle' fill='#666'>"
            f"{room.area_m2:.1f} m²</text>"
        )

    # Walls.
    wall_px = max(3.0, 0.10 * px_per_m)
    for wall in scene.walls:
        parts.append(
            f"<line x1='{X(wall.start[0]):.1f}' y1='{Y(wall.start[1]):.1f}' "
            f"x2='{X(wall.end[0]):.1f}' y2='{Y(wall.end[1]):.1f}' "
            f"stroke='#222' stroke-width='{wall_px:.1f}' stroke-linecap='square'/>"
        )

    # Openings: doors = white break + swing arc; windows = double line.
    for o in scene.openings:
        ox, oy = o.center[0], o.center[1]
        ang = _opening_angle(scene, o)
        dx, dy = math.cos(ang), math.sin(ang)
        hw = o.width_m / 2.0
        ax, ay = ox - dx * hw, oy - dy * hw
        bx, by = ox + dx * hw, oy + dy * hw
        if o.type == "door":
            parts.append(
                f"<line x1='{X(ax):.1f}' y1='{Y(ay):.1f}' x2='{X(bx):.1f}' y2='{Y(by):.1f}' "
                f"stroke='white' stroke-width='{wall_px + 2:.1f}'/>"
                f"<path d='M {X(ax):.1f} {Y(ay):.1f} A {o.width_m * px_per_m:.1f} "
                f"{o.width_m * px_per_m:.1f} 0 0 1 "
                f"{X(ax + dy * o.width_m):.1f} {Y(ay - dx * o.width_m):.1f}' "
                f"fill='none' stroke='#888' stroke-width='1'/>"
            )
        else:
            n_off = 0.05
            for s in (-1, 1):
                parts.append(
                    f"<line x1='{X(ax - dy * n_off * s):.1f}' y1='{Y(ay + dx * n_off * s):.1f}' "
                    f"x2='{X(bx - dy * n_off * s):.1f}' y2='{Y(by + dx * n_off * s):.1f}' "
                    f"stroke='#4a90d9' stroke-width='2'/>"
                )

    # Objects: dashed OBB outlines with labels.
    for obj in scene.objects:
        cx, cy = obj.bbox.center[0], obj.bbox.center[1]
        sx, sy = obj.bbox.size[0] / 2.0, obj.bbox.size[1] / 2.0
        cos_r, sin_r = math.cos(obj.bbox.rot_z), math.sin(obj.bbox.rot_z)
        corners = []
        for ux, uy in ((-sx, -sy), (sx, -sy), (sx, sy), (-sx, sy)):
            wx = cx + ux * cos_r - uy * sin_r
            wy = cy + ux * sin_r + uy * cos_r
            corners.append(f"{X(wx):.1f},{Y(wy):.1f}")
        parts.append(
            f"<polygon points='{' '.join(corners)}' fill='none' stroke='#999' "
            f"stroke-width='1.5' stroke-dasharray='4,3'/>"
            f"<text x='{X(cx):.1f}' y='{Y(cy):.1f}' {_FONT} font-size='9' "
            f"text-anchor='middle' fill='#777'>{_esc(obj.label)}</text>"
        )

    # Room bounding dimensions (annotated with tolerance).
    tol = scene.scale.tolerance_pct
    for room in scene.rooms:
        rx = [p[0] for p in room.polygon]
        ry = [p[1] for p in room.polygon]
        w_m, h_m = max(rx) - min(rx), max(ry) - min(ry)
        y_dim = Y(max(ry)) - 6
        parts.append(
            f"<text x='{X((min(rx) + max(rx)) / 2):.1f}' y='{y_dim:.1f}' {_FONT} "
            f"font-size='10' text-anchor='middle' fill='#c05000'>"
            f"{w_m:.2f} m ±{tol:.0f}%</text>"
            f"<text x='{X(min(rx)) - 6:.1f}' y='{Y((min(ry) + max(ry)) / 2):.1f}' {_FONT} "
            f"font-size='10' text-anchor='end' fill='#c05000'>{h_m:.2f} m</text>"
        )

    # Footer: scale bar + disclaimer.
    bar_y = height - 40
    parts.append(
        f"<line x1='20' y1='{bar_y}' x2='{20 + px_per_m:.0f}' y2='{bar_y}' stroke='#000' stroke-width='3'/>"
        f"<text x='20' y='{bar_y - 6}' {_FONT} font-size='11'>1 m</text>"
        f"<text x='20' y='{bar_y + 18}' {_FONT} font-size='10' fill='#888'>"
        f"Estimated dimensions (±{tol:.0f}%, scale: {_esc(scene.scale.method)}, "
        f"confidence {scene.scale.confidence:.2f}). Not survey-grade. "
        f"Generated by SceneForge — scene {_esc(scene.scene_id)}.</text>"
    )
    parts.append("</svg>")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path


def _opening_angle(scene: SceneSemantics, opening) -> float:
    """Direction along the wall the opening sits in (fallback: x axis)."""
    for wall in scene.walls:
        if wall.id == opening.wall_id:
            return math.atan2(wall.end[1] - wall.start[1], wall.end[0] - wall.start[0])
    return 0.0
