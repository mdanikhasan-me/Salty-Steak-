"""Salty Steak Native Desktop AI Platform — the multi-source research loop.

Retrieve, extract, compare, follow up, stop.

The loop itself is deterministic. It decides which page to open next and when
there is nothing left worth opening, and it hands the model claims rather than
pages. That keeps a research task at a handful of reasoning turns regardless of
how many sources it reads, and it is why the stopping rules have to be named
rather than felt.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .ledger import Budget, ResearchLedger



SENTENCE = re.compile(r"(?<=[.!?])\s+")


BOILERPLATE = (
    "cookie",
    "privacy policy",
    "terms of service",
    "all rights reserved",
    "skip to content",
    "sign in",
    "subscribe",
    "advertisement",
)

MIN_STATEMENT_WORDS = 4
MAX_STATEMENT_CHARACTERS = 300


def statements_from_page(page: Mapping[str, Any], *, limit: int = 40) -> list[str]:
    """Pull candidate statements out of a structured page reading.

    Works from the page summary the browser bridge already produces, so an
    ordinary structured page needs no screenshot and no vision pass.
    """

    text = str(page.get("summary") or page.get("text") or "")
    found: list[str] = []
    for raw in SENTENCE.split(text):
        statement = " ".join(str(raw).split())
        if len(statement) > MAX_STATEMENT_CHARACTERS:
            continue
        if len(statement.split()) < MIN_STATEMENT_WORDS:
            continue
        lowered = statement.casefold()
        if any(noise in lowered for noise in BOILERPLATE):
            continue
        found.append(statement)
        if len(found) >= limit:
            break
    return found


class ResearchLoop:
    """Drive retrieval and extraction until a named stopping rule fires."""

    def __init__(
        self,
        question: str,
        *,
        search: Callable[[str], Sequence[Mapping[str, Any]]],
        read: Callable[[str], Mapping[str, Any]],
        extract: Callable[[Mapping[str, Any]], Iterable[str]] = statements_from_page,
        follow_up: Callable[[ResearchLedger], str | None] | None = None,
        budget: Budget | None = None,
        task: Any = None,
    ) -> None:
        self.ledger = ResearchLedger(question, budget=budget)
        self.search = search
        self.read = read
        self.extract = extract
        self.follow_up = follow_up
        self.task = task

    def run(self, initial_query: str | None = None) -> dict[str, Any]:
        query = initial_query or self.ledger.question

        while True:
            stop, reason = self.ledger.should_stop()
            if stop:
                break
            if self._stopped():
                return {**self.ledger.report(), "stop_reason": "cancelled"}

            if not self.ledger.record_query(query):


                break
            self._event("research_query", query=query)

            candidates = list(self.search(query) or [])
            opened = 0
            for candidate in candidates:
                if self._stopped():
                    return {**self.ledger.report(), "stop_reason": "cancelled"}
                halt, _ = self.ledger.should_stop()
                if halt:
                    break

                url = str(candidate.get("url") or "")
                source = self.ledger.add_source(url, str(candidate.get("title") or ""))
                if source is None:


                    continue

                try:
                    page = self.read(url)
                except Exception as error:
                    self._event("research_source_failed", url=url, error=str(error)[:200])
                    continue

                summary = self.ledger.ingest(source, self.extract(page))
                opened += 1
                self._event("research_source", **summary, url=url)

            if opened == 0:
                break

            stop, reason = self.ledger.should_stop()
            if stop:
                break

            nxt = self.follow_up(self.ledger) if self.follow_up else None
            if not nxt:
                break
            query = nxt

        return self.ledger.report()

    def _stopped(self) -> bool:
        return bool(self.task is not None and self.task.stop_requested)

    def _event(self, kind: str, **detail: Any) -> None:
        if self.task is not None:
            self.task.record_event(kind, **detail)


__all__ = ["ResearchLoop", "statements_from_page"]
