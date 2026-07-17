"""Shared fixtures: synthetic room point clouds and COLMAP models.

The synthetic room is the CPU-testable stand-in for a real reconstruction:
a w×d rectangular room with 2.6 m walls, a floor, a door gap, and a table-
sized cluster, plus a ring of camera centers at phone height. Geometry and
splat *training* are exercised on real GPUs (docs/QUALITY.md); everything
downstream of them is tested here.
"""

from __future__ import annotations

import numpy as np
import pytest

from sceneforge_pipeline.colmap_model import Camera, ColmapModel, Image, Point3D


def make_room_cloud(
    w: float = 4.0,
    d: float = 3.0,
    h: float = 2.6,
    door_center_x: float = 1.5,
    door_width: float = 0.9,
    step: float = 0.03,
    seed: int = 7,
) -> tuple[np.ndarray, np.ndarray]:
    """(points, cam_centers) for a rectangular room, Z-up, floor at z=0.

    The door gap is cut into the y=0 wall from floor to 2.0 m.
    """
    rng = np.random.default_rng(seed)
    pts: list[np.ndarray] = []

    xs = np.arange(0, w, step)
    zs = np.arange(0, h, step)
    ys = np.arange(0, d, step)

    for x in xs:  # y=0 wall (with door gap) and y=d wall
        for z in zs:
            in_door = abs(x - door_center_x) < door_width / 2 and z < 2.0
            if not in_door:
                pts.append([x, 0.0, z])
            pts.append([x, d, z])
    for y in ys:  # x=0 and x=w walls
        for z in zs:
            pts.append([0.0, y, z])
            pts.append([w, y, z])
    floor_step = step * 3
    for x in np.arange(0, w, floor_step):  # floor
        for y in np.arange(0, d, floor_step):
            pts.append([x, y, 0.0])
    # A table: 1.2 × 0.6 top at 0.72 m in the middle of the room.
    for x in np.arange(2.2, 3.4, 0.02):
        for y in np.arange(1.2, 1.8, 0.02):
            pts.append([x, y, 0.72 + rng.normal(0, 0.004)])

    points = np.array(pts)
    points += rng.normal(0, 0.005, points.shape)  # sensor noise

    theta = np.linspace(0, 2 * np.pi, 60, endpoint=False)
    cam_centers = np.stack(
        [
            w / 2 + (w / 2 - 0.8) * np.cos(theta),
            d / 2 + (d / 2 - 0.8) * np.sin(theta),
            np.full_like(theta, 1.4),
        ],
        axis=1,
    )
    return points, cam_centers


@pytest.fixture
def room_cloud():
    return make_room_cloud()


def make_synthetic_model(
    n_images: int = 5, depth: float = 5.0, focal: float = 500.0
) -> ColmapModel:
    """Tiny pinhole model: identity-pose cameras looking down +Z at a plane of
    points at the given depth, with consistent 2D observations."""
    cam = Camera(1, "SIMPLE_PINHOLE", 640, 480, np.array([focal, 320.0, 240.0]))
    points: dict[int, Point3D] = {}
    grid = np.mgrid[-1.5:1.6:0.5, -1.5:1.6:0.5].reshape(2, -1).T
    for i, (x, y) in enumerate(grid, start=1):
        points[i] = Point3D(
            i, np.array([x, y, depth]), np.array([128, 128, 128], dtype=np.uint8), 0.8,
            np.array(list(range(1, n_images + 1)), dtype=np.int32),
        )
    images: dict[int, Image] = {}
    for img_id in range(1, n_images + 1):
        xys, ids = [], []
        for pid, p in points.items():
            u = focal * p.xyz[0] / p.xyz[2] + 320.0
            v = focal * p.xyz[1] / p.xyz[2] + 240.0
            xys.append([u, v])
            ids.append(pid)
        images[img_id] = Image(
            img_id,
            qvec=np.array([1.0, 0.0, 0.0, 0.0]),
            tvec=np.zeros(3),
            camera_id=1,
            name=f"frame_{img_id:05d}.jpg",
            xys=np.array(xys),
            point3d_ids=np.array(ids, dtype=np.int64),
        )
    return ColmapModel(cameras={1: cam}, images=images, points=points)


@pytest.fixture
def synthetic_model():
    return make_synthetic_model()
