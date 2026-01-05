"""Errors that the local HTTP adapter may safely return to its caller."""

from __future__ import annotations


class HTTPProblem(Exception):
    """A deliberately user facing HTTP failure."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
