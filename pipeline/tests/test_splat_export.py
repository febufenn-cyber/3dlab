import numpy as np
import pytest

from sceneforge_pipeline.splat_export import (
    read_splat,
    write_gsplat_ply,
    write_splat,
)


def _random_gaussians(n=100, seed=0):
    rng = np.random.default_rng(seed)
    return {
        "means": rng.normal(size=(n, 3)),
        "scales_log": rng.normal(-3, 0.5, size=(n, 3)),
        "quats": rng.normal(size=(n, 4)),
        "opacities_logit": rng.normal(size=(n,)),
        "sh0": rng.normal(0, 0.5, size=(n, 1, 3)),
        "shN": rng.normal(0, 0.1, size=(n, 8, 3)),
    }


def test_splat_roundtrip(tmp_path):
    g = _random_gaussians(50)
    path = write_splat(
        tmp_path / "s.splat",
        means=g["means"], scales_log=g["scales_log"], quats=g["quats"],
        opacities_logit=g["opacities_logit"], sh0=g["sh0"],
    )
    assert path.stat().st_size == 50 * 32  # exact record layout

    back = read_splat(path)
    assert back["means"].shape == (50, 3)
    # Records are re-ordered by importance; compare as sets via sorted views.
    got = np.sort(back["means"].round(4).view([("x", float), ("y", float), ("z", float)]), axis=0)
    want = np.sort(
        g["means"].astype(np.float32).astype(float).round(4)
        .view([("x", float), ("y", float), ("z", float)]), axis=0
    )
    np.testing.assert_array_equal(got, want)
    # Quats decode to unit-ish vectors.
    norms = np.linalg.norm(back["quats"], axis=1)
    assert np.all(norms < 1.2) and np.all(norms > 0.8)


def test_ply_header_and_size(tmp_path):
    g = _random_gaussians(10)
    path = write_gsplat_ply(
        tmp_path / "s.ply",
        means=g["means"], scales_log=g["scales_log"], quats=g["quats"],
        opacities_logit=g["opacities_logit"], sh0=g["sh0"], shN=g["shN"],
    )
    raw = path.read_bytes()
    header, _, body = raw.partition(b"end_header\n")
    text = header.decode("ascii")
    assert "element vertex 10" in text
    assert "property float f_dc_0" in text
    assert "property float f_rest_23" in text  # 8 coeffs × 3 channels
    assert "property float rot_3" in text
    n_props = text.count("property float")
    assert len(body) == 10 * n_props * 4


def test_ply_without_shn(tmp_path):
    g = _random_gaussians(5)
    path = write_gsplat_ply(
        tmp_path / "s0.ply",
        means=g["means"], scales_log=g["scales_log"], quats=g["quats"],
        opacities_logit=g["opacities_logit"], sh0=g["sh0"], shN=None,
    )
    assert b"f_rest_" not in path.read_bytes().partition(b"end_header")[0]


def test_splat_opacity_encoding(tmp_path):
    n = 4
    path = write_splat(
        tmp_path / "o.splat",
        means=np.zeros((n, 3)),
        scales_log=np.full((n, 3), -3.0),
        quats=np.tile([1.0, 0, 0, 0], (n, 1)),
        opacities_logit=np.array([-10.0, 0.0, 10.0, 2.0]),
        sh0=np.zeros((n, 1, 3)),
    )
    back = read_splat(path)
    alphas = sorted(back["rgba"][:, 3].tolist())
    assert alphas[0] == 0          # sigmoid(-10) ≈ 0
    assert alphas[-1] == 255       # sigmoid(10) ≈ 1
    assert 120 <= alphas[1] <= 136  # sigmoid(0) = 0.5
