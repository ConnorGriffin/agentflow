"""Stable work-classification vocabulary shared by intake, routing, and runners."""

from __future__ import annotations

from enum import Enum


class Complexity(str, Enum):
    """The model-size dial intake stamps per issue (ADR 0018)."""

    STANDARD = "standard"
    DEEP = "deep"


class Effort(str, Enum):
    """How much work an issue warrants independently of model size (ADR 0018)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTRA = "extra"


class MockupScope(str, Enum):
    """How wide a mockup round reopens the visual world (ADR 0048)."""

    LOCAL = "local"
    SURFACE = "surface"
