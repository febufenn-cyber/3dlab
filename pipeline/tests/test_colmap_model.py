import numpy as np

from sceneforge_pipeline.colmap_model import read_model, write_model
from .conftest import make_synthetic_model


def test_binary_roundtrip(tmp_path):
    model = make_synthetic_model(n_images=3)
    write_model(model, tmp_path)
    back = read_model(tmp_path)

    assert set(back.cameras) == set(model.cameras)
    cam = back.cameras[1]
    assert cam.model == "SIMPLE_PINHOLE"
    assert cam.width == 640 and cam.height == 480
    np.testing.assert_allclose(cam.params, model.cameras[1].params)

    assert set(back.images) == set(model.images)
    im = back.images[1]
    assert im.name == "frame_00001.jpg"
    np.testing.assert_allclose(im.qvec, model.images[1].qvec)
    np.testing.assert_allclose(im.xys, model.images[1].xys)
    np.testing.assert_array_equal(im.point3d_ids, model.images[1].point3d_ids)

    assert set(back.points) == set(model.points)
    np.testing.assert_allclose(back.points[1].xyz, model.points[1].xyz)
    assert back.points[1].error == model.points[1].error


def test_cam_center_identity_pose():
    model = make_synthetic_model(n_images=1)
    np.testing.assert_allclose(model.images[1].cam_center(), np.zeros(3), atol=1e-12)


def test_points_array_shapes():
    model = make_synthetic_model()
    xyz, rgb = model.points_array()
    assert xyz.shape[1] == 3 and rgb.shape[1] == 3
    assert xyz.shape[0] == len(model.points)
