"""Explicit non-JSON responses owned by the loopback API adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BinaryFileResponse:
    """A validated local artifact that the HTTP layer may stream inline."""

    path: Path
    content_type: str
    filename: str
    sha256: str | None = None
