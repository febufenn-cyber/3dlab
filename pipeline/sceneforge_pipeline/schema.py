"""Semantic scene JSON — the frozen v1.0 contract (brief §4.6).

This module is the single source of truth for the schema. The API, the
pipeline and the viewer all validate against these models. Breaking changes
require a schema_version bump; additive optional fields are allowed within
1.x. Do not edit field names or units casually.

Units: meters everywhere. Coordinate frame: Z-up, floor at z≈0, X/Y are the
floor plane. 2D coordinates ([x, y]) are floor-plane projections.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0"

_SCENE_ID_RE = re.compile(r"^scn_[a-z0-9]{8,32}$")


class _StrictModel(BaseModel):
    """Extra fields are rejected: the contract is frozen, typos must fail loudly."""

    model_config = ConfigDict(extra="forbid")


class ScaleInfo(_StrictModel):
    """How metric scale was recovered, and how much to trust it (brief §4.4)."""

    method: Literal["door_prior", "user_dimension", "camera_height_prior", "none"]
    confidence: float = Field(ge=0.0, le=1.0)
    tolerance_pct: float = Field(
        default=10.0,
        ge=0.0,
        description="Stated dimension tolerance. Estimates are never survey-grade.",
    )


class Room(_StrictModel):
    id: str
    label: str = Field(description="e.g. living_room, bedroom, room (when unknown)")
    polygon: list[tuple[float, float]] = Field(
        min_length=3, description="Closed floor polygon, CCW, meters, [x, y]"
    )
    area_m2: float = Field(ge=0.0)
    ceiling_height_m: Optional[float] = Field(default=None, ge=0.0)


class Wall(_StrictModel):
    id: str
    start: tuple[float, float]
    end: tuple[float, float]
    height_m: Optional[float] = Field(default=None, ge=0.0)


class Opening(_StrictModel):
    id: str
    type: Literal["door", "window"]
    wall_id: Optional[str] = Field(
        default=None, description="Wall this opening sits in; null if unattached"
    )
    center: tuple[float, float, float]
    width_m: float = Field(gt=0.0)
    height_m: float = Field(gt=0.0)


class ObjectBBox(_StrictModel):
    center: tuple[float, float, float]
    size: tuple[float, float, float]
    rot_z: float = Field(description="Yaw of the box around Z, radians")


class SceneObject(_StrictModel):
    id: str
    label: str
    bbox: ObjectBBox
    confidence: float = Field(ge=0.0, le=1.0)


class Assets(_StrictModel):
    splat_url: Optional[str] = None
    floorplan_svg_url: Optional[str] = None
    poster_url: Optional[str] = None


class Quality(_StrictModel):
    """Honesty block: what the reconstruction actually covered (brief §2 no-hallucination rule)."""

    coverage_pct: float = Field(ge=0.0, le=100.0)
    registered_frames: int = Field(ge=0)
    blur_rejected_pct: float = Field(ge=0.0, le=100.0)
    reconstruction_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class SceneSemantics(_StrictModel):
    """Top-level semantic scene document (the product's contract)."""

    scene_id: str
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    units: Literal["meters"] = "meters"
    scale: ScaleInfo
    rooms: list[Room] = Field(default_factory=list)
    walls: list[Wall] = Field(default_factory=list)
    openings: list[Opening] = Field(default_factory=list)
    objects: list[SceneObject] = Field(default_factory=list)
    assets: Assets = Field(default_factory=Assets)
    quality: Quality

    @field_validator("scene_id")
    @classmethod
    def _check_scene_id(cls, v: str) -> str:
        if not _SCENE_ID_RE.match(v):
            raise ValueError(f"scene_id must match {_SCENE_ID_RE.pattern!r}, got {v!r}")
        return v


def validate_semantics(doc: dict[str, Any]) -> SceneSemantics:
    """Parse+validate a semantic dict; raises pydantic.ValidationError on contract breach."""
    return SceneSemantics.model_validate(doc)


def dump_semantics(scene: SceneSemantics) -> str:
    """Canonical serialization: stable key order, 2-space indent, UTF-8."""
    return json.dumps(scene.model_dump(mode="json"), indent=2, ensure_ascii=False)


def json_schema() -> dict[str, Any]:
    """Machine-readable JSON Schema for external consumers (`rf-scene schema`)."""
    return SceneSemantics.model_json_schema()
