import json

from sceneforge_pipeline.stages.quality import build_quality, write_quality_report


GOOD_GEO = {
    "registered_frames": 210,
    "registered_ratio": 0.92,
    "sparse_points": 45_000,
    "mean_reproj_error_px": 0.9,
}
GOOD_COVERAGE = {"overall_pct": 90.0, "per_room": {"r1": 95.0, "r2": 88.0}}


def test_good_run_high_scores():
    q = build_quality(
        ingest_stats={"blur_rejected_pct": 8.0},
        geometry_stats=GOOD_GEO,
        coverage=GOOD_COVERAGE,
        scale_confidence=0.8,
        scale_method="door_prior",
    )
    assert q.coverage_pct > 80
    assert q.reconstruction_confidence > 0.7
    assert q.warnings == []


def test_partial_room_warned():
    coverage = {"overall_pct": 70.0, "per_room": {"r1": 95.0, "r2": 55.0}}
    q = build_quality(
        ingest_stats={"blur_rejected_pct": 10.0},
        geometry_stats=GOOD_GEO,
        coverage=coverage,
        scale_confidence=0.8,
        scale_method="door_prior",
    )
    assert any("r2 partially covered" in w for w in q.warnings)


def test_no_scale_warned():
    q = build_quality(
        ingest_stats={}, geometry_stats=GOOD_GEO, coverage=GOOD_COVERAGE,
        scale_confidence=0.0, scale_method="none",
    )
    assert any("arbitrary units" in w for w in q.warnings)


def test_camera_prior_warned():
    q = build_quality(
        ingest_stats={}, geometry_stats=GOOD_GEO, coverage=GOOD_COVERAGE,
        scale_confidence=0.3, scale_method="camera_height_prior",
    )
    assert any("camera-height prior" in w for w in q.warnings)


def test_low_registration_lowers_confidence_and_warns():
    geo = {**GOOD_GEO, "registered_ratio": 0.5, "registered_frames": 60}
    q = build_quality(
        ingest_stats={}, geometry_stats=geo, coverage=GOOD_COVERAGE,
        scale_confidence=0.8, scale_method="door_prior",
    )
    assert any("registered" in w for w in q.warnings)
    assert q.reconstruction_confidence < 0.75


def test_report_file_shape(tmp_path):
    q = build_quality(
        ingest_stats={"blur_rejected_pct": 5.0}, geometry_stats=GOOD_GEO,
        coverage=GOOD_COVERAGE, scale_confidence=0.8, scale_method="door_prior",
    )
    path = write_quality_report(
        tmp_path / "quality_report.json", "succeeded", q,
        {"ingest": 12.3, "geometry": 100.4}, extra={"scene_id": "scn_abcabcabcabc"},
    )
    doc = json.loads(path.read_text())
    assert doc["status"] == "succeeded"
    assert doc["quality"]["coverage_pct"] == q.coverage_pct
    assert doc["stage_timings_s"]["geometry"] == 100.4
    assert doc["scene_id"] == "scn_abcabcabcabc"


def test_failed_report_shape(tmp_path):
    path = write_quality_report(
        tmp_path / "q.json", "failed", None, {},
        extra={"failure": {"reason_code": "insufficient_registration", "message": "x"}},
    )
    doc = json.loads(path.read_text())
    assert doc["status"] == "failed"
    assert doc["quality"] is None
    assert doc["failure"]["reason_code"] == "insufficient_registration"
