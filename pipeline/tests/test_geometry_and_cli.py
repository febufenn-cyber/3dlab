import json

import pytest

from sceneforge_pipeline.cli import main as cli_main
from sceneforge_pipeline.config import GeometryConfig
from sceneforge_pipeline.errors import QualityGateError
from sceneforge_pipeline.stages.geometry import (
    ColmapGlomapBackend,
    ResearchOnlyBackend,
    get_backend,
    summarize_model,
)
from sceneforge_pipeline.viewer_html import write_viewer_html
from .conftest import make_synthetic_model


def test_registration_gate_passes():
    model = make_synthetic_model(n_images=40)
    res = summarize_model(model, total_input=50, cfg=GeometryConfig())
    assert res.registered == 40
    assert res.stats["registered_ratio"] == 0.8


def test_registration_gate_fails_low_count():
    model = make_synthetic_model(n_images=10)
    with pytest.raises(QualityGateError) as exc:
        summarize_model(model, total_input=12, cfg=GeometryConfig())
    assert exc.value.reason_code == "insufficient_registration"


def test_registration_gate_fails_low_ratio():
    model = make_synthetic_model(n_images=35)
    with pytest.raises(QualityGateError):
        summarize_model(model, total_input=100, cfg=GeometryConfig())


def test_research_backend_locked_without_flag():
    with pytest.raises(PermissionError):
        get_backend(ResearchOnlyBackend.name, research=False)


def test_research_backend_unlocked_with_flag():
    backend = get_backend(ResearchOnlyBackend.name, research=True)
    assert isinstance(backend, ResearchOnlyBackend)


def test_default_backend_is_commercial_safe():
    backend = get_backend("colmap_glomap", research=False)
    assert isinstance(backend, ColmapGlomapBackend)
    assert backend.commercial_safe


def test_unknown_backend_rejected():
    with pytest.raises(ValueError):
        get_backend("does_not_exist", research=True)


# ---- CLI ------------------------------------------------------------------


def test_cli_schema_prints_valid_json(capsys):
    assert cli_main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["scene_id"]


def test_cli_validate_accepts_good_file(tmp_path, capsys):
    from .test_schema import BRIEF_EXAMPLE

    f = tmp_path / "semantic.json"
    f.write_text(json.dumps(BRIEF_EXAMPLE))
    assert cli_main(["validate", str(f)]) == 0


def test_cli_validate_rejects_bad_file(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"scene_id": "nope"}))
    assert cli_main(["validate", str(f)]) == 4


def test_cli_build_missing_video_errors(tmp_path):
    rc = cli_main(["build", str(tmp_path / "nope.mp4"), "-o", str(tmp_path / "out")])
    assert rc == 3


# ---- viewer html -----------------------------------------------------------


def test_viewer_html_contract(tmp_path):
    out = write_viewer_html(
        tmp_path / "viewer.html",
        scene_id="scn_abcabcabcabc",
        splat_filename="scene.ksplat",
        quality={"coverage_pct": 87.0},
        scale={"method": "door_prior", "confidence": 0.8, "tolerance_pct": 10},
        initial_pos=[1.0, 2.0, 1.5],
        look_at=[0.0, 0.0, 1.0],
    )
    html = out.read_text()
    assert "scene.ksplat" in html
    assert "scn_abcabcabcabc" in html
    assert "not survey-grade" in html
    assert "importmap" in html
    assert "cdn.jsdelivr.net" in html
