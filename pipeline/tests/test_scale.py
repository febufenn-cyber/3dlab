import numpy as np
import pytest

from sceneforge_pipeline.config import ScaleConfig
from sceneforge_pipeline.stages.scale import (
    Detection2D,
    Detection3D,
    fit_scale_from_doors,
    lift_detections,
    median_depth_in_box,
    run_scale,
)
from .conftest import make_synthetic_model

# Synthetic geometry (see conftest): SIMPLE_PINHOLE f=500, cx=320, cy=240,
# identity pose, points on a plane at depth 5. A 100 px tall box at that depth
# spans 100 * 5 / 500 = 1.0 world unit → doors "measure" 1.0 unit tall, so
# with the 2.0 m prior the fitted scale must be exactly 2.0.
DOOR_BOX = (250.0, 150.0, 400.0, 250.0)


def _door_detections(n=3):
    return [
        Detection2D(image_id=i, label="door", score=0.8, box=DOOR_BOX)
        for i in range(1, n + 1)
    ]


def test_median_depth_in_box(synthetic_model):
    depth = median_depth_in_box(synthetic_model, synthetic_model.images[1], DOOR_BOX)
    assert depth == pytest.approx(5.0, rel=1e-6)


def test_median_depth_needs_points(synthetic_model):
    # A box in a corner with no observed keypoints → None, never a guess.
    assert median_depth_in_box(synthetic_model, synthetic_model.images[1], (0, 0, 5, 5)) is None


def test_lift_detections_height(synthetic_model):
    dets = lift_detections(synthetic_model, _door_detections(1))
    assert len(dets) == 1
    assert dets[0].height_world == pytest.approx(1.0, rel=1e-6)
    # Box center (325, 200) → world x=(325-320)*5/500=0.05, y=(200-240)*5/500=-0.4
    np.testing.assert_allclose(dets[0].center_world, [0.05, -0.4, 5.0], atol=1e-6)


def test_fit_scale_exact(synthetic_model):
    dets3d = lift_detections(synthetic_model, _door_detections(3))
    scale, conf, stats = fit_scale_from_doors(dets3d, ScaleConfig())
    assert scale == pytest.approx(2.0, rel=1e-6)
    assert conf > 0.5
    assert stats["door_detections_used"] == 3


def test_fit_scale_too_few_doors(synthetic_model):
    dets3d = lift_detections(synthetic_model, _door_detections(2))
    scale, conf, stats = fit_scale_from_doors(dets3d, ScaleConfig())
    assert scale == 1.0 and conf == 0.0
    assert stats["reason"] == "too_few_door_detections"


def test_run_scale_door_prior(tmp_path, synthetic_model):
    res = run_scale(tmp_path, synthetic_model, ScaleConfig(), detections=_door_detections(4))
    assert res.info.method == "door_prior"
    assert res.scale == pytest.approx(2.0, rel=1e-6)
    assert 0 < res.info.confidence <= 1.0


def test_run_scale_none_without_signal(tmp_path, synthetic_model):
    res = run_scale(tmp_path, synthetic_model, ScaleConfig(), detections=[])
    assert res.info.method == "none"
    assert res.scale == 1.0
    assert res.info.confidence == 0.0


def test_run_scale_user_dimension(tmp_path, synthetic_model):
    res = run_scale(
        tmp_path, synthetic_model, ScaleConfig(), known_dimension_m=4.2, detections=[]
    )
    assert res.info.method == "user_dimension"
    assert res.stats["known_dimension_m"] == 4.2


def test_detection3d_transform_scales_and_rotates():
    det = Detection3D("door", 0.9, np.array([1.0, 0.0, 0.0]), 0.5, 1.0)
    theta = np.pi / 2
    T = np.eye(4)
    T[:3, :3] = 2.0 * np.array(
        [[np.cos(theta), -np.sin(theta), 0], [np.sin(theta), np.cos(theta), 0], [0, 0, 1]]
    )
    T[:3, 3] = [0.0, 0.0, 1.0]
    out = det.transform(T)
    np.testing.assert_allclose(out.center_world, [0.0, 2.0, 1.0], atol=1e-9)
    assert out.height_world == pytest.approx(2.0)
    assert out.width_world == pytest.approx(1.0)
