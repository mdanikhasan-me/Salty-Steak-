"""Dataset workflow exceptions shared by the dataset modules."""

from __future__ import annotations


class DatasetError(RuntimeError):
    """Base error for dataset workflow failures."""


class DatasetFormatError(DatasetError):
    """The source is unreadable or is not a genuinely supported format."""


class DatasetValidationError(DatasetError):
    """Validation or mapping prevents safe preparation."""


class DatasetPreparationError(DatasetError):
    """A verified prepared artifact could not be produced."""


class _RecordProblem(ValueError):
    """A single source record cannot be mapped to a training example."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
