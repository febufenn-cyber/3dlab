import numpy as np
import pytest

from sceneforge_pipeline.config import SemanticsConfig
from sceneforge_pipeline.stages.scale import Detection3D
from sceneforge_pipeline.stages.semantics import (
    apply_transform,
    apply_user_dimension,
    boundary_coverage,
    camera_height_scale,
    estimate_gravity_transform,
    extract_semantics,
)
from .conftest import make_room_cloud

ROOM_W, ROOM_D = 4.0, 3.0
TRUE_AREA = ROOM_W * ROOM_D


@pytest.fixture(scope="module")
def sem_result():
    points, cams = make_room_cloud(w=ROOM_W, d=ROOM_D)
    return extract_semantics(points, cams, dets3d=[], cfg=SemanticsConfig())


def test_finds_exactly_one_room(sem_result):
    assert len(sem_result.rooms) == 1


def test_room_area_within_tolerance(sem_result):
    area = sem_result.rooms[0].area_m2
    assert area == pytest.approx(TRUE_AREA, rel=0.20), f"area {area} vs truth {TRUE_AREA}"


def test_ceiling_height(sem_result):
    assert sem_result.ceiling_height_m == pytest.approx(2.6, abs=0.25)


def test_walls_found_on_all_sides(sem_result):
    assert len(sem_result.walls) >= 4
    angles = set()
    for w in sem_result.walls:
        dx, dy = w.end[0] - w.start[0], w.end[1] - w.start[1]
        angles.add(round(abs(np.arctan2(dy, dx)) % np.pi, 1))
    assert len(angles) >= 2  # both orientations present


def test_table_object_found(sem_result):
    boxes = [o for o in sem_result.objects]
    assert boxes, "expected at least the table cluster"
    best = min(boxes, key=lambda o: np.hypot(o.bbox.center[0] - 2.8, o.bbox.center[1] - 1.5))
    assert abs(best.bbox.center[0] - 2.8) < 0.4
    assert abs(best.bbox.center[1] - 1.5) < 0.4
    long_side = max(best.bbox.size[0], best.bbox.size[1])
    assert long_side == pytest.approx(1.2, abs=0.3)


def test_door_gap_detected(sem_result):
    doors = [o for o in sem_result.openings if o.type == "door"]
    assert doors, "the 0.9 m wall gap should yield a door"
    d = min(doors, key=lambda o: abs(o.center[0] - 1.5) + abs(o.center[1]))
    assert d.center[0] == pytest.approx(1.5, abs=0.4)
    assert abs(d.center[1]) < 0.4
    assert 0.5 <= d.width_m <= 1.4


def test_coverage_high_for_full_capture(sem_result):
    assert sem_result.coverage["overall_pct"] > 75.0


def test_gravity_transform_recovers_up():
    points, cams = make_room_cloud()
    rng = np.random.default_rng(3)
    A = rng.normal(size=(3, 3))
    Q, _ = np.linalg.qr(A)
    if np.linalg.det(Q) < 0:
        Q[:, 0] *= -1
    t = np.array([5.0, -2.0, 3.0])
    pts_rot = points @ Q.T + t
    cams_rot = cams @ Q.T + t

    T = estimate_gravity_transform(pts_rot, cams_rot)
    aligned = apply_transform(pts_rot, T)
    z = aligned[:, 2]
    assert np.percentile(z, 2) == pytest.approx(0.0, abs=0.05)   # floor at 0
    assert np.percentile(z, 99) == pytest.approx(2.6, abs=0.15)  # ceiling up
    cams_aligned = apply_transform(cams_rot, T)
    assert np.median(cams_aligned[:, 2]) == pytest.approx(1.4, abs=0.1)


def test_camera_height_scale():
    points, cams = make_room_cloud()
    half = 0.5
    s = camera_height_scale(points * half, cams * half)
    assert s == pytest.approx(2.0, rel=0.05)  # shrunk scene → scale back up


def test_detector_labels_objects_and_rooms():
    points, cams = make_room_cloud()
    dets = [
        Detection3D("bed", 0.9, np.array([2.8, 1.5, 0.5]), 1.4, 0.5),
    ]
    res = extract_semantics(points, cams, dets3d=dets, cfg=SemanticsConfig())
    labeled = [o for o in res.objects if o.label == "bed"]
    assert labeled, "cluster near detection should inherit its label"
    assert res.rooms[0].label == "bedroom"


def test_apply_user_dimension_rescales():
    points, cams = make_room_cloud()
    res = extract_semantics(points, cams, dets3d=[], cfg=SemanticsConfig())
    area_before = res.rooms[0].area_m2
    poly = np.array(res.rooms[0].polygon)
    long_before = float(max(poly.max(axis=0) - poly.min(axis=0)))
    c = apply_user_dimension(res, known_dimension_m=8.0)
    assert c == pytest.approx(8.0 / long_before, rel=1e-6)
    assert res.rooms[0].area_m2 == pytest.approx(area_before * c * c, rel=0.01)


def test_boundary_coverage_flags_unseen_region():
    points, cams = make_room_cloud()
    # Simulate never pointing the camera at the far end of the room: no wall
    # AND no floor observed beyond x=2.9, though the camera walked there, so
    # the footprint still extends into the unobserved zone.
    keep = points[:, 0] < ROOM_W - 1.1
    res = extract_semantics(points[keep], cams, dets3d=[], cfg=SemanticsConfig())
    assert res.rooms, "room should still be found from the observed part"
    cov = boundary_coverage(res.rooms, points[keep][:, :2])
    assert cov["overall_pct"] < 97.0, (
        "an unobserved room end must lower boundary coverage — this is the "
        "anti-hallucination signal"
    )
