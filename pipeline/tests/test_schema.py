import json

import pytest
from pydantic import ValidationError

from sceneforge_pipeline.schema import (
    SCHEMA_VERSION,
    SceneSemantics,
    dump_semantics,
    json_schema,
    validate_semantics,
)

BRIEF_EXAMPLE = {
    "scene_id": "scn_0123456789ab",
    "schema_version": "1.0",
    "units": "meters",
    "scale": {"method": "door_prior", "confidence": 0.8, "tolerance_pct": 10},
    "rooms": [
        {
            "id": "r1",
            "label": "living_room",
            "polygon": [[0, 0], [4, 0], [4, 3], [0, 3]],
            "area_m2": 12.0,
            "ceiling_height_m": 2.7,
        }
    ],
    "walls": [{"id": "w1", "start": [0, 0], "end": [4, 0], "height_m": 2.7}],
    "openings": [
        {
            "id": "o1",
            "type": "door",
            "wall_id": "w1",
            "center": [1.5, 0, 1.0],
            "width_m": 0.9,
            "height_m": 2.0,
        }
    ],
    "objects": [
        {
            "id": "ob1",
            "label": "table",
            "bbox": {"center": [2.8, 1.5, 0.4], "size": [1.2, 0.6, 0.8], "rot_z": 0.0},
            "confidence": 0.72,
        }
    ],
    "assets": {
        "splat_url": "scene.ksplat",
        "floorplan_svg_url": "floorplan.svg",
        "poster_url": "poster.png",
    },
    "quality": {
        "coverage_pct": 87,
        "registered_frames": 214,
        "blur_rejected_pct": 12,
        "reconstruction_confidence": 0.81,
        "warnings": ["bedroom_2 partially covered"],
    },
}


def test_brief_example_validates():
    scene = validate_semantics(BRIEF_EXAMPLE)
    assert scene.schema_version == SCHEMA_VERSION == "1.0"
    assert scene.rooms[0].area_m2 == 12.0
    assert scene.openings[0].type == "door"


def test_dump_roundtrip():
    scene = validate_semantics(BRIEF_EXAMPLE)
    again = validate_semantics(json.loads(dump_semantics(scene)))
    assert again == scene


def test_bad_scene_id_rejected():
    doc = {**BRIEF_EXAMPLE, "scene_id": "not-a-scene-id"}
    with pytest.raises(ValidationError):
        validate_semantics(doc)


def test_extra_field_rejected():
    doc = {**BRIEF_EXAMPLE, "surprise": True}
    with pytest.raises(ValidationError):
        validate_semantics(doc)


def test_wrong_units_rejected():
    doc = {**BRIEF_EXAMPLE, "units": "feet"}
    with pytest.raises(ValidationError):
        validate_semantics(doc)


def test_confidence_bounds():
    doc = json.loads(json.dumps(BRIEF_EXAMPLE))
    doc["scale"]["confidence"] = 1.5
    with pytest.raises(ValidationError):
        validate_semantics(doc)


def test_json_schema_is_draft_valid():
    import jsonschema

    schema = json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(SceneSemantics.model_validate(BRIEF_EXAMPLE).model_dump(mode="json"), schema)
