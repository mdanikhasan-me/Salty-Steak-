"""Explicitly granted, local Windows automation boundary."""

from .broker import AutomationBroker
from .filesystem import FileActionError, recycle_confirmed_file, snapshot_regular_file

__all__ = [
    "AutomationBroker",
    "FileActionError",
    "recycle_confirmed_file",
    "snapshot_regular_file",
]
