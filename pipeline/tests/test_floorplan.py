import xml.etree.ElementTree as ET

from sceneforge_pipeline.schema import validate_semantics
from sceneforge_pipeline.stages.floorplan import render_floorplan_svg
from .test_schema import BRIEF_EXAMPLE


def test_svg_renders_and_parses(tmp_path):
    scene = validate_semantics(BRIEF_EXAMPLE)
    out = render_floorplan_svg(scene, tmp_path / "floorplan.svg")
    text = out.read_text()

    root = ET.fromstring(text)  # well-formed XML
    assert root.tag.endswith("svg")

    assert "living room" in text          # room label
    assert "12.0 m²" in text              # area annotation
    assert "±10%" in text                 # tolerance on dimensions
    assert "Not survey-grade" in text     # honesty disclaimer
    assert "door_prior" in text           # scale method surfaced
    assert "1 m" in text                  # scale bar
    assert "table" in text                # object label


def test_svg_handles_empty_scene(tmp_path):
    empty = {
        **BRIEF_EXAMPLE,
        "rooms": [],
        "walls": [],
        "openings": [],
        "objects": [],
    }
    scene = validate_semantics(empty)
    out = render_floorplan_svg(scene, tmp_path / "empty.svg")
    ET.fromstring(out.read_text())


def test_svg_escapes_labels(tmp_path):
    doc = {**BRIEF_EXAMPLE}
    doc["rooms"] = [dict(BRIEF_EXAMPLE["rooms"][0], label="a<b&c")]
    scene = validate_semantics(doc)
    out = render_floorplan_svg(scene, tmp_path / "esc.svg")
    ET.fromstring(out.read_text())
