"""Salty Steak Native Desktop AI Platform — research ledger.

Evidence with provenance, rather than pages pasted into a prompt.

Reading five sources into the model's context and asking for a summary throws
away the two things that make research trustworthy: where each statement came
from, and whether the sources actually agreed. It also fills the context with
navigation menus and cookie banners.

So the browser retrieves, the ledger condenses, and the model reasons over
claims rather than over pages. Every claim keeps its sources. Claims that say
the same thing are merged and keep both sources; claims that contradict are
kept apart deliberately, because a disagreement between sources is a finding,
not noise to be averaged away.

Stopping is explicit. "Research until satisfied" is not a stopping condition;
budgets and a coverage threshold are.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

LEDGER_SCHEMA = "salty-steak-research-ledger-v1"


DUPLICATE_THRESHOLD = 0.72



NOISE = frozenset(
    """
    the a an and or of to in on for is are was were be been it its this that
    with from by as at his her their they them we you i than then
    """.split()
)


NEGATIONS = frozenset(
    {"not", "never", "no", "cannot", "without", "denies", "rejects", "closed", "unavailable"}
)



NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "hundred",
    "thousand", "million", "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
}


def _words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9']+", str(text).casefold())
        if word not in NOISE and len(word) > 1
    }


def _numbers(text: str) -> set[str]:
    """Every value a statement asserts, in digits or in words."""

    lowered = str(text).casefold()
    found = set(re.findall(r"\d+(?:[.,]\d+)?", lowered))
    found.update(word for word in re.findall(r"[a-z]+", lowered) if word in NUMBER_WORDS)
    return found


def similarity(left: str, right: str) -> float:
    """How much two statements overlap, ignoring filler."""

    first, second = _words(left), _words(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


@dataclass
class Source:
    """Somewhere a claim came from."""

    source_id: str
    url: str
    title: str = ""
    retrieved_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_id,
            "url": self.url,
            "title": self.title,
            "retrieved_at": self.retrieved_at,
        }


@dataclass
class Claim:
    """One statement, and everywhere it was found."""

    claim_id: str
    text: str
    sources: list[str] = field(default_factory=list)

    contradicts: list[str] = field(default_factory=list)
    confidence: float = 0.5

    @property
    def corroborated(self) -> bool:
        return len(self.sources) > 1

    @property
    def disputed(self) -> bool:
        return bool(self.contradicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim_id,
            "text": self.text,
            "sources": list(self.sources),
            "source_count": len(self.sources),
            "corroborated": self.corroborated,
            "disputed": self.disputed,
            "contradicts": list(self.contradicts),
            "confidence": round(self.confidence, 2),
        }


@dataclass
class Budget:
    """When to stop. Named limits rather than a feeling of sufficiency."""

    max_sources: int = 8
    max_queries: int = 6
    max_seconds: float = 300.0

    coverage_target: int = 5

    barren_limit: int = 2


class ResearchLedger:
    """Accumulate evidence, keep its provenance, and know when to stop."""

    def __init__(self, question: str, *, budget: Budget | None = None) -> None:
        self.question = str(question)
        self.budget = budget or Budget()
        self.sources: dict[str, Source] = {}
        self.claims: dict[str, Claim] = {}
        self.queries: list[str] = []
        self.open_questions: list[str] = []
        self.started_at = time.monotonic()
        self._barren = 0
        self._seen_urls: set[str] = set()



    def record_query(self, query: str) -> bool:
        """Note a search. False when it has already been tried."""

        text = str(query).strip()
        if not text or text.casefold() in {item.casefold() for item in self.queries}:
            return False
        self.queries.append(text)
        return True

    def add_source(self, url: str, title: str = "") -> Source | None:
        """Register a source. None when it was already read."""

        clean = str(url).split("#", 1)[0].rstrip("/")
        if clean in self._seen_urls:
            return None
        self._seen_urls.add(clean)
        source = Source(
            source_id=f"src-{len(self.sources) + 1}", url=clean, title=str(title)
        )
        self.sources[source.source_id] = source
        return source

    def add_claim(self, text: str, source_id: str) -> Claim:
        """Add a statement, merging duplicates and flagging disagreements.

        A claim found in two places is one claim with two sources — that is
        corroboration, and collapsing it is what keeps the ledger small. A
        claim that conflicts with an existing one is kept separately, because
        losing the disagreement would be losing the most useful thing found.
        """

        text = " ".join(str(text).split())
        for existing in self.claims.values():
            score = similarity(existing.text, text)
            if score < DUPLICATE_THRESHOLD:
                continue
            if self._conflicts(existing.text, text):
                break
            if source_id not in existing.sources:
                existing.sources.append(source_id)

                existing.confidence = min(0.99, existing.confidence + 0.2)
            return existing

        claim = Claim(
            claim_id=f"clm-{len(self.claims) + 1}", text=text, sources=[source_id]
        )
        for existing in self.claims.values():
            if similarity(existing.text, text) >= 0.4 and self._conflicts(
                existing.text, text
            ):
                claim.contradicts.append(existing.claim_id)
                existing.contradicts.append(claim.claim_id)

                existing.confidence = min(existing.confidence, 0.4)
                claim.confidence = 0.4
        self.claims[claim.claim_id] = claim
        return claim

    def ingest(
        self, source: Source, statements: Iterable[str]
    ) -> dict[str, Any]:
        """Take everything one page had to say and fold it in."""

        added, merged = 0, 0
        before = len(self.claims)
        for statement in statements:
            if not str(statement).strip():
                continue
            claim = self.add_claim(statement, source.source_id)
            if len(self.claims) > before:
                added += 1
                before = len(self.claims)
            else:
                merged += 1



        if added == 0:
            self._barren += 1
        else:
            self._barren = 0
        return {"source": source.source_id, "new_claims": added, "merged": merged}

    @staticmethod
    def _conflicts(left: str, right: str) -> bool:
        """Whether two similar statements actually disagree."""

        left_numbers, right_numbers = _numbers(left), _numbers(right)
        if left_numbers and right_numbers and left_numbers != right_numbers:

            return True
        left_words, right_words = _words(left), _words(right)
        negated_left = bool(left_words & NEGATIONS)
        negated_right = bool(right_words & NEGATIONS)
        return negated_left != negated_right



    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def corroborated_claims(self) -> list[Claim]:
        return [claim for claim in self.claims.values() if claim.corroborated]

    @property
    def disputed_claims(self) -> list[Claim]:
        return [claim for claim in self.claims.values() if claim.disputed]

    def should_stop(self) -> tuple[bool, str]:
        """Whether to stop, and the named reason why."""

        if len(self.sources) >= self.budget.max_sources:
            return True, "source_budget"
        if len(self.queries) >= self.budget.max_queries:
            return True, "query_budget"
        if self.elapsed_seconds >= self.budget.max_seconds:
            return True, "time_budget"
        if len(self.corroborated_claims) >= self.budget.coverage_target:
            return True, "coverage_reached"
        if self._barren >= self.budget.barren_limit:
            return True, "no_new_information"
        return False, ""



    def evidence_for_model(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """The claims worth reasoning over, best supported first.

        Disputed claims are placed first rather than buried: a contradiction
        between sources is the thing most likely to change an answer.
        """

        ranked = sorted(
            self.claims.values(),
            key=lambda claim: (not claim.disputed, -len(claim.sources), claim.claim_id),
        )
        return [claim.to_dict() for claim in ranked[:limit]]

    def report(self) -> dict[str, Any]:
        stop, reason = self.should_stop()
        return {
            "schema": LEDGER_SCHEMA,
            "question": self.question,
            "queries": list(self.queries),
            "sources": [source.to_dict() for source in self.sources.values()],
            "claims": [claim.to_dict() for claim in self.claims.values()],
            "source_count": len(self.sources),
            "claim_count": len(self.claims),
            "corroborated": len(self.corroborated_claims),
            "disputed": len(self.disputed_claims),
            "open_questions": list(self.open_questions),
            "stopped": stop,
            "stop_reason": reason,
            "seconds": round(self.elapsed_seconds, 2),
        }

    def provenance(self, claim_id: str) -> list[dict[str, Any]]:
        """Where a specific statement came from, so an answer can be checked."""

        claim = self.claims[claim_id]
        return [self.sources[source].to_dict() for source in claim.sources]


__all__ = [
    "Budget",
    "Claim",
    "DUPLICATE_THRESHOLD",
    "LEDGER_SCHEMA",
    "ResearchLedger",
    "Source",
    "similarity",
]
