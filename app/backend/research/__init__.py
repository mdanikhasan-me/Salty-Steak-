"""Salty Steak Native Desktop AI Platform — evidence gathering."""

from .ledger import Budget, Claim, ResearchLedger, Source, similarity
from .loop import ResearchLoop, statements_from_page

__all__ = [
    "Budget",
    "Claim",
    "ResearchLedger",
    "ResearchLoop",
    "Source",
    "similarity",
    "statements_from_page",
]
