"""Tests for the lingbot-map → ColmapModel conversion (pure CPU).

Only model inference needs a GPU; the conversion that lets lingbot output
flow through the unchanged downstream pipeline is verified here against a
synthetic prediction with known ground truth.
"""

import numpy as np
import pytest

from sceneforge_pipeline.colmap_model import qvec2rotmat, rotmat2qvec
from sceneforge_pipeline.config import GeometryConfig
from sceneforge_pipeline.errors import GeometryFailure
from sceneforge_pipeline.stages.geometry import LingbotMapBackend, get_backend
from sceneforge_pipeline.stages.lingbot import lingbot_to_colmap_model


def _c2w(R_c2w, center):
    T = np.eye(4)
    T[:3, :3] = R_c2w
    T[:3, 3] = center
    return T


def _rot_z(theta):
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def test_rotmat_qvec_roundtrip():
    for theta in np.linspace(-3, 3, 13):
        R = _rot_z(theta)
        np.testing.assert_allclose(qvec2rotmat(rotmat2qvec(R)), R, atol=1e-9)


def test_pose_inversion_recovers_camera_centers():
    # Two cameras at known world centers looking with known orientation.
    centers = [np.array([1.0, 2.0, 1.4]), np.array([-0.5, 0.3, 1.4])]
    rots = [_rot_z(0.0), _rot_z(0.7)]
    extrinsic = np.stack([_c2w(R, c) for R, c in zip(rots, centers)])
    K = np.array([[500.0, 0, 320], [0, 500.0, 240], [0, 0, 1]])
    intrinsic = np.broadcast_to(K, (2, 3, 3))
    world_points = np.array([[0.0, 0.0, 0.0], [1.0, 1.0, 0.5], [2.0, 0.0, 1.0]])[None].repeat(2, 0)
    out = {
        "extrinsic": extrinsic,
        "intrinsic": intrinsic,
        "world_points": world_points,
    }
    model = lingbot_to_colmap_model(out)
    assert len(model.images) == 2
    for img, center in zip(model.images.values(), centers):
        np.testing.assert_allclose(img.cam_center(), center, atol=1e-6)
    # Intrinsics preserved as PINHOLE fx,fy,cx,cy.
    cam = model.cameras[model.images[1].camera_id]
    assert cam.model == "PINHOLE"
    np.testing.assert_allclose(cam.params, [500.0, 500.0, 320.0, 240.0])


def test_confidence_filter_drops_low_points():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[500.0, 0, 320], [0, 500.0, 240], [0, 0, 1]])[None]
    pts = np.array([[0, 0, 5.0], [1, 1, 5.0], [2, 2, 5.0], [3, 3, 5.0]])[None]
    conf = np.array([0.9, 0.1, 0.8, 0.05])[None]  # two below 0.5
    model = lingbot_to_colmap_model(
        {"extrinsic": extrinsic, "intrinsic": intrinsic,
         "world_points": pts, "world_points_conf": conf},
        conf_threshold=0.5,
    )
    assert len(model.points) == 2
    kept = np.array([p.xyz for p in model.points.values()])
    assert {tuple(k) for k in kept} == {(0, 0, 5.0), (2, 2, 5.0)}


def test_colors_sampled_from_images():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[10.0, 0, 1], [0, 10.0, 1], [0, 0, 1]])[None]
    # world_points and images share (H,W) so colours map 1:1.
    world_points = np.array([[[0, 0, 5.0], [1, 0, 5.0]], [[0, 1, 5.0], [1, 1, 5.0]]])[None]
    images = np.array([[[10, 20, 30], [40, 50, 60]], [[70, 80, 90], [100, 110, 120]]],
                      dtype=np.uint8)[None]
    model = lingbot_to_colmap_model(
        {"extrinsic": extrinsic, "intrinsic": intrinsic,
         "world_points": world_points, "images": images}
    )
    rgbs = {tuple(int(v) for v in p.rgb) for p in model.points.values()}
    assert (10, 20, 30) in rgbs and (100, 110, 120) in rgbs


def test_float_images_scaled_to_uint8():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[10.0, 0, 1], [0, 10.0, 1], [0, 0, 1]])[None]
    world_points = np.array([[[0, 0, 5.0]]])[None]  # (1,1,1,3)
    images = np.array([[[1.0, 0.5, 0.0]]], dtype=np.float32)[None]
    model = lingbot_to_colmap_model(
        {"extrinsic": extrinsic, "intrinsic": intrinsic,
         "world_points": world_points, "images": images}
    )
    rgb = next(iter(model.points.values())).rgb
    np.testing.assert_array_equal(rgb, [255, 127, 0])


def test_non_finite_points_dropped():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[10.0, 0, 1], [0, 10.0, 1], [0, 0, 1]])[None]
    pts = np.array([[0, 0, 5.0], [np.nan, 0, 5.0], [1, 1, np.inf]])[None]
    model = lingbot_to_colmap_model(
        {"extrinsic": extrinsic, "intrinsic": intrinsic, "world_points": pts}
    )
    assert len(model.points) == 1


def test_max_points_cap():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[10.0, 0, 1], [0, 10.0, 1], [0, 0, 1]])[None]
    pts = (np.arange(3000).reshape(1000, 3).astype(float))[None]
    model = lingbot_to_colmap_model(
        {"extrinsic": extrinsic, "intrinsic": intrinsic, "world_points": pts},
        max_points=100,
    )
    assert len(model.points) == 100


def test_missing_poses_raises():
    with pytest.raises(GeometryFailure):
        lingbot_to_colmap_model({"intrinsic": np.eye(3)[None], "world_points": np.zeros((1, 1, 3))})


def test_no_confident_points_raises():
    extrinsic = _c2w(np.eye(3), np.zeros(3))[None]
    intrinsic = np.array([[10.0, 0, 1], [0, 10.0, 1], [0, 0, 1]])[None]
    pts = np.array([[0, 0, 5.0], [1, 1, 5.0]])[None]
    conf = np.array([0.1, 0.2])[None]
    with pytest.raises(GeometryFailure):
        lingbot_to_colmap_model(
            {"extrinsic": extrinsic, "intrinsic": intrinsic,
             "world_points": pts, "world_points_conf": conf},
            conf_threshold=0.5,
        )


def test_lingbot_is_default_and_colmap_still_selectable():
    # lingbot is now the default backend (D34)…
    assert GeometryConfig().backend == "lingbot"
    backend = get_backend("lingbot", research=False)
    assert isinstance(backend, LingbotMapBackend)
    assert backend.commercial_safe is True
    # …and COLMAP remains the selectable classical fallback.
    from sceneforge_pipeline.stages.geometry import ColmapGlomapBackend

    assert isinstance(get_backend("colmap_glomap", research=False), ColmapGlomapBackend)


def test_confidence_stats():
    from sceneforge_pipeline.stages.lingbot import lingbot_confidence_stats

    conf = np.array([0.9, 0.8, 0.1, 0.2, 0.95])
    s = lingbot_confidence_stats({"world_points_conf": conf}, conf_threshold=0.5)
    assert s["has_confidence"] and s["n_total"] == 5 and s["n_confident"] == 3
    assert s["confident_ratio"] == 0.6
    # No confidence channel → gate is skipped, not failed.
    assert lingbot_confidence_stats({}, 0.5)["has_confidence"] is False


def test_confidence_gate_fails_low_confidence():
    from sceneforge_pipeline.errors import QualityGateError
    from sceneforge_pipeline.stages.geometry import _apply_confidence_gate

    cfg = GeometryConfig()  # floors: 2000 points, 10%
    # High point count but low confident-ratio → fail loud (honest failure).
    stats = {"has_confidence": True, "n_total": 100000, "n_confident": 3000,
             "confident_ratio": 0.03}
    with pytest.raises(QualityGateError) as exc:
        _apply_confidence_gate(stats, cfg)
    assert exc.value.reason_code == "insufficient_reconstruction_confidence"


def test_confidence_gate_passes_healthy():
    from sceneforge_pipeline.stages.geometry import _apply_confidence_gate

    cfg = GeometryConfig()
    good = {"has_confidence": True, "n_total": 100000, "n_confident": 60000,
            "confident_ratio": 0.6}
    _apply_confidence_gate(good, cfg)  # must not raise
    # No confidence channel → skipped, never raises.
    _apply_confidence_gate({"has_confidence": False}, cfg)
