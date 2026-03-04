"""Salty Steak — mail triage, independent of which mailbox it runs against."""

from .triage import (
    BATCH_SIZE,
    Message,
    TRIAGE_SCHEMA,
    TriageReport,
    Verdict,
    classify,
    features,
    group_repeats,
)

__all__ = [
    "BATCH_SIZE",
    "Message",
    "TRIAGE_SCHEMA",
    "TriageReport",
    "Verdict",
    "classify",
    "features",
    "group_repeats",
]
