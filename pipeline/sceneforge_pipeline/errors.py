"""Pipeline error taxonomy.

QualityGateError is the honest-failure channel (brief §2, §4.1): when a
capture violates the capture contract we fail fast with a machine-readable
report instead of producing garbage geometry.
"""

from __future__ import annotations

from typing import Any


class PipelineError(RuntimeError):
    """Base class for all pipeline failures."""


class DependencyMissingError(PipelineError):
    """A required external binary/model is not available on this host."""


class QualityGateError(PipelineError):
    """Capture failed validation against the capture contract.

    Carries a structured report: which rule broke, measured values, and the
    human-readable remediation (which maps 1:1 to docs/CAPTURE_GUIDE.md).
    """

    def __init__(self, reason_code: str, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.reason_code = reason_code
        self.report = dict(report)
        self.report.setdefault("reason_code", reason_code)
        self.report.setdefault("message", message)


class GeometryFailure(PipelineError):
    """SfM could not produce a usable reconstruction (distinct from a quality gate:
    the capture looked fine but registration failed)."""
