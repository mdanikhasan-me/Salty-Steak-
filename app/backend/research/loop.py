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
from .pages import (
    CATEGORY_LISTING,
    classify_page,
    looks_like_a_range,
    price_observation,
)



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


def _host_of(url: str) -> str:
    """The site an address belongs to, as a person would name it."""

    try:
        from urllib.parse import urlparse

        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


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
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.ledger = ResearchLedger(question, budget=budget)
        self.search = search
        self.read = read
        self.extract = extract
        self.follow_up = follow_up
        self.task = task





        self.on_progress = on_progress

        self.waves: list[dict[str, Any]] = []

    def run(self, initial_query: str | None = None) -> dict[str, Any]:
        query = initial_query or self.ledger.question


        ended = ""

        while True:
            stop, reason = self.ledger.should_stop()
            if stop:
                ended = reason
                break
            if self._stopped():
                return {**self.ledger.report(), "stop_reason": "cancelled"}

            if not self.ledger.record_query(query):


                ended = "query_repeated"
                break
            self._event("research_query", query=query)
            wave = self._wave(query)

            candidates = list(self.search(query) or [])



            for candidate in candidates:
                host = _host_of(str(candidate.get("url") or ""))
                if host and host not in {site["host"] for site in wave["sites"]}:
                    wave["sites"].append(
                        {
                            "host": host,
                            "url": str(candidate.get("url") or ""),
                            "title": str(candidate.get("title") or ""),
                            "state": "found",
                        }
                    )
            self._publish("searching")
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





                page_type = classify_page(page)
                source.page_type = page_type
                statements = list(self.extract(page))
                if page_type == CATEGORY_LISTING:
                    statements = [
                        f"[catalogue page, not one product] {statement}"
                        if looks_like_a_range(statement)
                        else statement
                        for statement in statements
                    ]

                observation = price_observation(page, page_type)
                if observation is not None:
                    self.ledger.record_observation(observation)

                summary = self.ledger.ingest(source, statements)
                opened += 1
                self._event(
                    "research_source", **summary, url=url, page_type=page_type
                )




                host = _host_of(url)
                for site in wave["sites"]:
                    if site["host"] != host:
                        continue
                    site["state"] = "verified" if observation is not None else "read"
                    site["page_type"] = page_type
                    break
                wave["opened"] = opened
                if observation is not None:
                    wave["verified"] += 1
                wave["state"] = "reading"
                self._publish("reading")

            if opened == 0:
                ended = "no_further_sources"
                break

            stop, reason = self.ledger.should_stop()
            if stop:
                ended = reason
                break

            nxt = self.follow_up(self.ledger) if self.follow_up else None
            if not nxt:
                ended = "evidence_sufficient"
                break
            query = nxt

        for wave in self.waves:
            wave["state"] = "done"
        self._publish("completed")

        report = self.ledger.report()
        if ended:
            report["stop_reason"] = ended
        report["waves"] = [dict(wave, sites=list(wave["sites"])) for wave in self.waves]
        return report



    def _wave(self, query: str) -> dict[str, Any]:
        """Begin a search wave and announce it."""

        wave = {
            "query": query,
            "sites": [],
            "opened": 0,
            "verified": 0,
            "rejected": 0,
            "state": "searching",
        }
        self.waves.append(wave)
        self._publish("searching")
        return wave

    def _publish(self, phase: str) -> None:
        """Hand the interface a whole, coherent picture of the work so far.

        A snapshot rather than a delta: the interface can then render whatever
        it has whenever it polls, and a missed update costs nothing.
        """

        if self.on_progress is None:
            return
        try:
            self.on_progress(
                {
                    "schema": "salty-steak-research-progress-v1",
                    "phase": phase,
                    "question": self.ledger.question,
                    "waves": [dict(wave, sites=list(wave["sites"])) for wave in self.waves],
                }
            )
        except Exception:

            return

    def _stopped(self) -> bool:
        return bool(self.task is not None and self.task.stop_requested)

    def _event(self, kind: str, **detail: Any) -> None:
        if self.task is not None:
            self.task.record_event(kind, **detail)


__all__ = ["ResearchLoop", "statements_from_page"]
