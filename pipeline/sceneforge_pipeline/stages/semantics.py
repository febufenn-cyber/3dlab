"""Stage 5 — deterministic semantic layout (brief §4.5).

Pure permissive-OSS baseline that always runs: numpy + OpenCV + shapely.
Point cloud (metric, Z-up, floor at z≈0) →
  1.0–1.5 m horizontal slice → 2D occupancy grid → wall segments (Hough +
  merge) → footprint → rooms (free-space components) → openings (wall gaps +
  detector projections) → object OBBs (voxel clustering + PCA yaw).

Never invents geometry: rooms come only from observed free space, and the
per-room boundary coverage feeds the quality report so partially-seen rooms
are flagged, not completed (brief §2).

Model-assisted alternatives (SpatialLM) can replace this behind the same
output types later; the baseline must keep working without them.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from shapely.geometry import Polygon

from ..config import SemanticsConfig
from ..schema import Opening, Room, SceneObject, Wall, ObjectBBox
from .scale import Detection3D

log = logging.getLogger(__name__)

# Furniture label → room label heuristics (applied by majority vote).
_ROOM_LABEL_VOTES = {
    "bed": "bedroom",
    "wardrobe": "bedroom",
    "sofa": "living_room",
    "television": "living_room",
    "refrigerator": "kitchen",
    "oven": "kitchen",
    "sink": "kitchen",
    "toilet": "bathroom",
    "shower": "bathroom",
    "bathtub": "bathroom",
    "dining table": "dining_room",
}


@dataclass
class SemanticsResult:
    rooms: list[Room]
    walls: list[Wall]
    openings: list[Opening]
    objects: list[SceneObject]
    ceiling_height_m: float | None
    coverage: dict = field(default_factory=dict)   # per-room + overall boundary coverage
    debug: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Gravity / floor alignment
# --------------------------------------------------------------------------


def estimate_gravity_transform(points: np.ndarray, cam_centers: np.ndarray) -> np.ndarray:
    """4x4 rigid transform: arbitrary SfM frame → Z-up with floor at z=0.

    A handheld walkthrough keeps the camera at roughly constant height, so the
    best-fit plane of camera centers is horizontal; its normal is the up axis.
    Sign is chosen so most points lie *below* the camera plane (floor +
    furniture dominate ceilings in phone captures).
    """
    if len(cam_centers) < 3:
        raise ValueError("Need ≥3 camera centers to estimate gravity")
    c0 = cam_centers.mean(axis=0)
    _, _, vt = np.linalg.svd(cam_centers - c0)
    up = vt[2]  # smallest-variance direction of the (planar) trajectory
    below = float(np.median((points - c0) @ up))
    if below > 0:
        up = -up

    # Rotation taking `up` to +Z (Rodrigues about up × z).
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(up, z)
    s, c = np.linalg.norm(v), float(np.dot(up, z))
    if s < 1e-9:
        R = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        R = np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))

    rotated_z = (points @ R.T)[:, 2]
    floor_z = float(np.percentile(rotated_z, 2))
    T = np.eye(4)
    T[:3, :3] = R
    T[2, 3] = -floor_z
    return T


def apply_transform(points: np.ndarray, T: np.ndarray) -> np.ndarray:
    return points @ T[:3, :3].T + T[:3, 3]


def camera_height_scale(points_aligned: np.ndarray, cam_centers_aligned: np.ndarray,
                        camera_height_m: float = 1.4) -> float:
    """Weak prior: handheld phones sit ~1.4 m above the floor (fallback scale)."""
    med = float(np.median(cam_centers_aligned[:, 2]))
    if med <= 1e-6:
        return 1.0
    return camera_height_m / med


# --------------------------------------------------------------------------
# Occupancy grid
# --------------------------------------------------------------------------


@dataclass
class Grid:
    origin: np.ndarray  # (2,) world coords of cell (0,0) corner
    res: float
    shape: tuple[int, int]  # (rows, cols) = (y, x)

    def w2g(self, xy: np.ndarray) -> np.ndarray:
        """World (x,y) → integer grid (row, col)."""
        g = np.floor((xy - self.origin) / self.res).astype(int)
        return g[:, ::-1] if g.ndim == 2 else g[::-1]

    def g2w(self, rc: np.ndarray) -> np.ndarray:
        """Grid (row, col) centers → world (x,y)."""
        rc = np.asarray(rc, dtype=float)
        cr = rc[:, ::-1] if rc.ndim == 2 else rc[::-1]
        return self.origin + (cr + 0.5) * self.res


def make_grid(points_xy: np.ndarray, res: float, pad_m: float = 0.5) -> Grid:
    lo = np.percentile(points_xy, 1, axis=0) - pad_m
    hi = np.percentile(points_xy, 99, axis=0) + pad_m
    cols = max(8, int(math.ceil((hi[0] - lo[0]) / res)))
    rows = max(8, int(math.ceil((hi[1] - lo[1]) / res)))
    return Grid(origin=lo, res=res, shape=(rows, cols))


def rasterize(points_xy: np.ndarray, grid: Grid, min_count: int = 2) -> np.ndarray:
    """Binary occupancy mask (rows×cols, uint8) of cells with ≥min_count points."""
    mask = np.zeros(grid.shape, dtype=np.uint8)
    if len(points_xy) == 0:
        return mask
    rc = grid.w2g(points_xy)
    ok = (
        (rc[:, 0] >= 0) & (rc[:, 0] < grid.shape[0])
        & (rc[:, 1] >= 0) & (rc[:, 1] < grid.shape[1])
    )
    rc = rc[ok]
    counts = np.zeros(grid.shape, dtype=np.int32)
    np.add.at(counts, (rc[:, 0], rc[:, 1]), 1)
    mask[counts >= min_count] = 1
    return mask


# --------------------------------------------------------------------------
# Walls
# --------------------------------------------------------------------------


def extract_walls(wall_mask: np.ndarray, grid: Grid, ceiling_height_m: float | None) -> list[Wall]:
    closed = cv2.morphologyEx(wall_mask * 255, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    min_len_px = max(4, int(0.60 / grid.res))
    lines = cv2.HoughLinesP(
        closed, rho=1, theta=np.pi / 180, threshold=25,
        minLineLength=min_len_px, maxLineGap=int(0.20 / grid.res),
    )
    if lines is None:
        return []
    segs = []
    # OpenCV 4 returns (N,1,4); OpenCV 5 returns (N,4) — normalize.
    for x0, y0, x1, y1 in np.asarray(lines).reshape(-1, 4):
        a = grid.g2w(np.array([y0, x0]))
        b = grid.g2w(np.array([y1, x1]))
        segs.append((a, b))
    segs = merge_segments(segs, angle_tol_deg=8.0, offset_tol_m=0.12, gap_tol_m=0.30)
    return [
        Wall(
            id=f"w{i+1}",
            start=(round(float(a[0]), 3), round(float(a[1]), 3)),
            end=(round(float(b[0]), 3), round(float(b[1]), 3)),
            height_m=round(ceiling_height_m, 2) if ceiling_height_m else None,
        )
        for i, (a, b) in enumerate(segs)
    ]


def merge_segments(
    segs: list[tuple[np.ndarray, np.ndarray]],
    angle_tol_deg: float,
    offset_tol_m: float,
    gap_tol_m: float,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Merge near-collinear, near-touching Hough fragments into wall runs."""
    remaining = [(np.asarray(a, float), np.asarray(b, float)) for a, b in segs]
    merged: list[tuple[np.ndarray, np.ndarray]] = []
    while remaining:
        a, b = remaining.pop(0)
        changed = True
        while changed:
            changed = False
            keep = []
            for c, d in remaining:
                if _collinear_mergeable(a, b, c, d, angle_tol_deg, offset_tol_m, gap_tol_m):
                    a, b = _merge_two(a, b, c, d)
                    changed = True
                else:
                    keep.append((c, d))
            remaining = keep
        merged.append((a, b))
    return merged


def _seg_angle(a: np.ndarray, b: np.ndarray) -> float:
    d = b - a
    return math.atan2(d[1], d[0]) % math.pi  # undirected


def _collinear_mergeable(a, b, c, d, angle_tol_deg, offset_tol_m, gap_tol_m) -> bool:
    ang1, ang2 = _seg_angle(a, b), _seg_angle(c, d)
    diff = min(abs(ang1 - ang2), math.pi - abs(ang1 - ang2))
    if diff > math.radians(angle_tol_deg):
        return False
    u = (b - a) / (np.linalg.norm(b - a) + 1e-12)
    n = np.array([-u[1], u[0]])
    if max(abs(np.dot(c - a, n)), abs(np.dot(d - a, n))) > offset_tol_m:
        return False
    t = sorted([0.0, float(np.linalg.norm(b - a))])
    tc, td = float(np.dot(c - a, u)), float(np.dot(d - a, u))
    lo2, hi2 = min(tc, td), max(tc, td)
    return not (lo2 > t[1] + gap_tol_m or hi2 < t[0] - gap_tol_m)


def _merge_two(a, b, c, d):
    u = (b - a) / (np.linalg.norm(b - a) + 1e-12)
    pts = np.array([a, b, c, d])
    ts = (pts - a) @ u
    return a + u * ts.min(), a + u * ts.max()


# --------------------------------------------------------------------------
# Footprint & rooms
# --------------------------------------------------------------------------


def compute_footprint(
    wall_mask: np.ndarray, floor_mask: np.ndarray, cam_mask: np.ndarray, grid: Grid
) -> np.ndarray:
    """Filled interior footprint: everything enclosed by observed structure."""
    k = max(3, int(0.30 / grid.res))
    occupied = ((wall_mask | floor_mask | cam_mask) * 255).astype(np.uint8)
    closed = cv2.morphologyEx(occupied, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    # Fill from the border: whatever the flood can't reach is interior.
    h, w = closed.shape
    ff = closed.copy()
    ff_mask = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, ff_mask, (0, 0), 255)
    interior = cv2.bitwise_not(ff)  # holes only
    footprint = ((closed | interior) > 0).astype(np.uint8)
    return footprint


def extract_rooms(
    footprint: np.ndarray,
    wall_mask: np.ndarray,
    grid: Grid,
    cfg: SemanticsConfig,
    ceiling_height_m: float | None,
) -> tuple[list[Room], np.ndarray]:
    """Rooms = connected components of eroded free space; returns rooms + label map."""
    wall_dil = cv2.dilate(wall_mask * 255, np.ones((3, 3), np.uint8)) > 0
    free = (footprint > 0) & ~wall_dil
    erode_px = max(1, int(cfg.door_gap_max_m / 2.0 / grid.res))
    free_eroded = cv2.erode(free.astype(np.uint8) * 255, np.ones((erode_px, erode_px), np.uint8)) > 0

    n_labels, labels = cv2.connectedComponents(free_eroded.astype(np.uint8), connectivity=4)
    rooms: list[Room] = []
    room_map = np.zeros_like(labels)
    next_id = 1
    for lbl in range(1, n_labels):
        comp = labels == lbl
        # Grow the eroded component back inside the free space.
        grown = cv2.dilate(comp.astype(np.uint8) * 255, np.ones((erode_px * 2 + 1,) * 2, np.uint8)) > 0
        comp_full = grown & free
        area_m2 = float(comp_full.sum()) * grid.res ** 2
        if area_m2 < cfg.min_room_area_m2:
            continue
        poly = _mask_to_polygon(comp_full, grid)
        if poly is None:
            continue
        rooms.append(
            Room(
                id=f"r{next_id}",
                label="room",
                polygon=[(round(float(x), 3), round(float(y), 3)) for x, y in poly],
                area_m2=round(area_m2, 2),
                ceiling_height_m=round(ceiling_height_m, 2) if ceiling_height_m else None,
            )
        )
        room_map[comp_full] = next_id
        next_id += 1
    rooms.sort(key=lambda r: -r.area_m2)
    return rooms, room_map


def _mask_to_polygon(mask: np.ndarray, grid: Grid) -> list[tuple[float, float]] | None:
    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    eps = max(2.0, 0.10 / grid.res)
    approx = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
    if len(approx) < 3:
        return None
    # OpenCV contours are (x=col, y=row); convert to world and force CCW.
    world = np.array([grid.g2w(np.array([r, c])) for c, r in approx])
    if _signed_area(world) < 0:
        world = world[::-1]
    return [tuple(p) for p in world]


def _signed_area(poly: np.ndarray) -> float:
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def label_rooms(rooms: list[Room], objects: list[SceneObject]) -> None:
    """Majority-vote room labels from contained furniture (in place)."""
    for room in rooms:
        poly = Polygon(room.polygon)
        votes: dict[str, int] = {}
        for obj in objects:
            vote = _ROOM_LABEL_VOTES.get(obj.label)
            if vote and poly.contains(_pt(obj.bbox.center[:2])):
                votes[vote] = votes.get(vote, 0) + 1
        if votes:
            room.label = max(votes, key=votes.get)


def _pt(xy):
    from shapely.geometry import Point

    return Point(float(xy[0]), float(xy[1]))


# --------------------------------------------------------------------------
# Openings
# --------------------------------------------------------------------------


def extract_openings(
    walls: list[Wall],
    dets3d: list[Detection3D],
    cfg: SemanticsConfig,
) -> list[Opening]:
    openings: list[Opening] = []
    # (a) detector-projected doors/windows snapped to the nearest wall.
    for det in dets3d:
        if det.label not in ("door", "window"):
            continue
        wall_id = _nearest_wall(walls, det.center_world[:2], max_dist_m=0.4)
        openings.append(
            Opening(
                id="",
                type=det.label,  # "door" | "window"
                wall_id=wall_id,
                center=tuple(round(float(v), 3) for v in det.center_world),
                width_m=round(max(0.4, min(det.width_world, 2.5)), 2),
                height_m=round(max(0.4, min(det.height_world, 2.6)), 2),
            )
        )
    # (b) door-sized gaps between collinear wall runs.
    for i, w1 in enumerate(walls):
        for w2 in walls[i + 1 :]:
            gap = _collinear_gap(w1, w2)
            if gap is None:
                continue
            width, center = gap
            if cfg.door_gap_min_m <= width <= cfg.door_gap_max_m:
                openings.append(
                    Opening(
                        id="",
                        type="door",
                        wall_id=w1.id,
                        center=(round(center[0], 3), round(center[1], 3), 1.0),
                        width_m=round(width, 2),
                        height_m=2.0,
                    )
                )
    openings = _dedup_openings(openings, min_sep_m=0.6)
    for i, o in enumerate(openings):
        o.id = f"o{i+1}"
    return openings


def _nearest_wall(walls: list[Wall], xy, max_dist_m: float) -> str | None:
    best, best_d = None, max_dist_m
    p = np.array(xy, dtype=float)
    for w in walls:
        a, b = np.array(w.start), np.array(w.end)
        d = _point_seg_dist(p, a, b)
        if d < best_d:
            best, best_d = w.id, d
    return best


def _point_seg_dist(p, a, b) -> float:
    ab = b - a
    t = float(np.clip(np.dot(p - a, ab) / (np.dot(ab, ab) + 1e-12), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * ab)))


def _collinear_gap(w1: Wall, w2: Wall) -> tuple[float, np.ndarray] | None:
    a1, b1 = np.array(w1.start), np.array(w1.end)
    a2, b2 = np.array(w2.start), np.array(w2.end)
    ang1, ang2 = _seg_angle(a1, b1), _seg_angle(a2, b2)
    diff = min(abs(ang1 - ang2), math.pi - abs(ang1 - ang2))
    if diff > math.radians(8):
        return None
    u = (b1 - a1) / (np.linalg.norm(b1 - a1) + 1e-12)
    n = np.array([-u[1], u[0]])
    if max(abs(np.dot(a2 - a1, n)), abs(np.dot(b2 - a1, n))) > 0.15:
        return None
    t1 = sorted([0.0, float(np.dot(b1 - a1, u))])
    t2 = sorted([float(np.dot(a2 - a1, u)), float(np.dot(b2 - a1, u))])
    if t2[0] > t1[1]:  # w2 after w1
        width = t2[0] - t1[1]
        center = a1 + u * (t1[1] + width / 2.0)
    elif t1[0] > t2[1]:  # w2 before w1
        width = t1[0] - t2[1]
        center = a1 + u * (t2[1] + width / 2.0)
    else:
        return None  # overlapping
    return float(width), center


def _dedup_openings(openings: list[Opening], min_sep_m: float) -> list[Opening]:
    kept: list[Opening] = []
    for o in openings:  # detector-sourced first (listed first) wins ties
        c = np.array(o.center[:2])
        if all(np.linalg.norm(c - np.array(k.center[:2])) > min_sep_m or k.type != o.type for k in kept):
            kept.append(o)
    return kept


# --------------------------------------------------------------------------
# Objects
# --------------------------------------------------------------------------


def extract_objects(
    points: np.ndarray,
    walls: list[Wall],
    dets3d: list[Detection3D],
    cfg: SemanticsConfig,
) -> list[SceneObject]:
    band = points[(points[:, 2] >= cfg.object_z_min_m) & (points[:, 2] <= cfg.object_z_max_m)]
    if len(band) == 0:
        return []
    # Drop points hugging a wall line.
    keep = np.ones(len(band), dtype=bool)
    for w in walls:
        a, b = np.array(w.start), np.array(w.end)
        ab = b - a
        denom = float(np.dot(ab, ab)) + 1e-12
        t = np.clip((band[:, :2] - a) @ ab / denom, 0.0, 1.0)
        proj = a + t[:, None] * ab
        keep &= np.linalg.norm(band[:, :2] - proj, axis=1) > 0.15
    band = band[keep]
    if len(band) == 0:
        return []

    clusters = _voxel_cluster(band, eps=cfg.object_cluster_eps_m, min_points=cfg.object_min_points)
    objects: list[SceneObject] = []
    for i, idx in enumerate(clusters):
        pts = band[idx]
        center, size, yaw = _oriented_bbox(pts)
        if max(size[0], size[1]) < 0.15 or max(size[0], size[1]) > 5.0:
            continue
        label, conf = _label_cluster(center, dets3d)
        objects.append(
            SceneObject(
                id=f"ob{i+1}",
                label=label,
                bbox=ObjectBBox(
                    center=tuple(round(float(v), 3) for v in center),
                    size=tuple(round(float(v), 3) for v in size),
                    rot_z=round(float(yaw), 4),
                ),
                confidence=conf,
            )
        )
    return objects


def _voxel_cluster(points: np.ndarray, eps: float, min_points: int) -> list[np.ndarray]:
    """Connected components over an eps-voxel hash (26-connectivity), BFS."""
    keys = np.floor(points / eps).astype(np.int64)
    voxels: dict[tuple, list[int]] = {}
    for i, k in enumerate(map(tuple, keys)):
        voxels.setdefault(k, []).append(i)
    seen: set[tuple] = set()
    clusters: list[np.ndarray] = []
    offsets = [
        (dx, dy, dz)
        for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
    ]
    for start in voxels:
        if start in seen:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:
            v = stack.pop()
            members.extend(voxels[v])
            for off in offsets:
                nb = (v[0] + off[0], v[1] + off[1], v[2] + off[2])
                if nb in voxels and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(members) >= min_points:
            clusters.append(np.array(members))
    return clusters


def _oriented_bbox(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    xy = pts[:, :2]
    mu = xy.mean(axis=0)
    cov = np.cov((xy - mu).T)
    evals, evecs = np.linalg.eigh(cov)
    major = evecs[:, int(np.argmax(evals))]
    yaw = math.atan2(float(major[1]), float(major[0]))
    R = np.array([[math.cos(-yaw), -math.sin(-yaw)], [math.sin(-yaw), math.cos(-yaw)]])
    local = (xy - mu) @ R.T
    lo, hi = local.min(axis=0), local.max(axis=0)
    size_xy = hi - lo
    center_local = (hi + lo) / 2.0
    center_xy = mu + center_local @ np.linalg.inv(R).T
    z_lo, z_hi = float(pts[:, 2].min()), float(pts[:, 2].max())
    center = np.array([center_xy[0], center_xy[1], (z_lo + z_hi) / 2.0])
    size = np.array([size_xy[0], size_xy[1], z_hi - z_lo])
    return center, size, yaw


def _label_cluster(center: np.ndarray, dets3d: list[Detection3D]) -> tuple[str, float]:
    best_label, best_conf, best_d = "object", 0.3, 1.0
    for det in dets3d:
        if det.label in ("door", "window"):
            continue
        d = float(np.linalg.norm(center - det.center_world))
        if d < best_d:
            best_label, best_conf, best_d = det.label, round(min(det.score, 0.99), 2), d
    return best_label, best_conf


# --------------------------------------------------------------------------
# Coverage (honesty metrics for the quality report)
# --------------------------------------------------------------------------


def boundary_coverage(
    rooms: list[Room], points_xy: np.ndarray, sample_step_m: float = 0.25, radius_m: float = 0.35
) -> dict:
    """For each room: fraction of its boundary within radius of any observed point."""
    from scipy.spatial import cKDTree

    if len(points_xy) == 0:
        return {"overall_pct": 0.0, "per_room": {}}
    tree = cKDTree(points_xy)
    per_room: dict[str, float] = {}
    weights: list[tuple[float, float]] = []
    for room in rooms:
        poly = np.array(room.polygon)
        samples = []
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            n = max(1, int(np.linalg.norm(b - a) / sample_step_m))
            for t in np.linspace(0, 1, n, endpoint=False):
                samples.append(a + t * (b - a))
        samples = np.array(samples)
        d, _ = tree.query(samples)
        frac = float((d <= radius_m).mean())
        per_room[room.id] = round(100.0 * frac, 1)
        perim = float(Polygon(room.polygon).length)
        weights.append((frac, perim))
    overall = (
        100.0 * sum(f * w for f, w in weights) / max(sum(w for _, w in weights), 1e-9)
        if weights
        else 0.0
    )
    return {"overall_pct": round(overall, 1), "per_room": per_room}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def extract_semantics(
    points: np.ndarray,            # (N,3) metric, Z-up, floor≈0
    cam_centers: np.ndarray,       # (M,3) same frame
    dets3d: list[Detection3D],
    cfg: SemanticsConfig,
) -> SemanticsResult:
    z = points[:, 2]
    ceiling = float(np.percentile(z, 98))
    ceiling_height = ceiling if 1.8 <= ceiling <= 4.5 else None

    hi = min(cfg.slice_z_max_m, (ceiling * 0.8) if ceiling_height else cfg.slice_z_max_m)
    band = points[(z >= cfg.slice_z_min_m) & (z <= max(hi, cfg.slice_z_min_m + 0.2))]
    grid = make_grid(points[:, :2], cfg.grid_res_m)
    wall_mask = rasterize(band[:, :2], grid, min_count=2)

    floor_mask = rasterize(points[z < 0.30][:, :2], grid, min_count=1)
    cam_mask = rasterize(cam_centers[:, :2], grid, min_count=1)
    cam_mask = cv2.dilate(cam_mask, np.ones((int(0.6 / grid.res),) * 2, np.uint8))

    walls = extract_walls(wall_mask, grid, ceiling_height)
    footprint = compute_footprint(wall_mask, floor_mask, cam_mask, grid)
    rooms, room_map = extract_rooms(footprint, wall_mask, grid, cfg, ceiling_height)
    objects = extract_objects(points, walls, dets3d, cfg)
    label_rooms(rooms, objects)
    openings = extract_openings(walls, dets3d, cfg)
    coverage = boundary_coverage(rooms, points[:, :2])

    log.info(
        "semantics: %d rooms, %d walls, %d openings, %d objects, boundary coverage %.0f%%",
        len(rooms), len(walls), len(openings), len(objects), coverage["overall_pct"],
    )
    return SemanticsResult(
        rooms=rooms,
        walls=walls,
        openings=openings,
        objects=objects,
        ceiling_height_m=ceiling_height,
        coverage=coverage,
        debug={"grid_shape": grid.shape, "wall_cells": int(wall_mask.sum())},
    )


def apply_user_dimension(result: SemanticsResult, known_dimension_m: float) -> float:
    """Resolve a user-provided known dimension: the caller states the longest
    side of the largest room. Returns the correction factor applied in place."""
    if not result.rooms:
        return 1.0
    poly = np.array(result.rooms[0].polygon)
    ext = poly.max(axis=0) - poly.min(axis=0)
    measured = float(max(ext))
    if measured < 1e-6:
        return 1.0
    c = known_dimension_m / measured
    _rescale(result, c)
    return c


def _rescale(result: SemanticsResult, c: float) -> None:
    for r in result.rooms:
        r.polygon = [(x * c, y * c) for x, y in r.polygon]
        r.area_m2 = round(r.area_m2 * c * c, 2)
        if r.ceiling_height_m:
            r.ceiling_height_m = round(r.ceiling_height_m * c, 2)
    for w in result.walls:
        w.start = (w.start[0] * c, w.start[1] * c)
        w.end = (w.end[0] * c, w.end[1] * c)
        if w.height_m:
            w.height_m = round(w.height_m * c, 2)
    for o in result.openings:
        o.center = tuple(v * c for v in o.center)
        o.width_m = round(o.width_m * c, 2)
        o.height_m = round(o.height_m * c, 2)
    for ob in result.objects:
        ob.bbox.center = tuple(v * c for v in ob.bbox.center)
        ob.bbox.size = tuple(v * c for v in ob.bbox.size)
    if result.ceiling_height_m:
        result.ceiling_height_m = round(result.ceiling_height_m * c, 2)
