"""Salty Steak Native Desktop AI Platform — the multi-source research loop.

Retrieve, extract, compare, follow up, stop.

The loop itself is deterministic. It decides which page to open next and when
there is nothing left worth opening, and it hands the model claims rather than
pages. That keeps a research task at a handful of reasoning turns regardless of
how many sources it reads, and it is why the stopping rules have to be named
rather than felt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
CHECKPOINT_SCHEMA = "salty-steak-research-checkpoint-v1"
INVALID_PAGE_SIGNALS = (
    "404 not found",
    "page not found",
    "invite invalid",
    "invite may be expired",
    "link has expired",
    "this link is invalid",
    "content is unavailable",
)

STATUS_QUERY_CLAUSE = re.compile(
    r"\b(?:using|prefer|identify|give|cite|include|with direct)\b",
    re.IGNORECASE,
)
LEADING_RESEARCH_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:research|search(?:\s+for)?|find|look\s+up|determine)\s+",
    re.IGNORECASE,
)
VERSION_TOKEN = re.compile(r"\b\d+(?:\.\d+){1,3}\b")


def _status_subject(question: str) -> str:
    """Turn a request-shaped status prompt into one focused search subject."""

    first_clause = re.split(
        r"(?:\.(?=\s|$)|\n)", str(question or ""), maxsplit=1
    )[0]
    first_clause = STATUS_QUERY_CLAUSE.split(first_clause, maxsplit=1)[0]
    first_clause = LEADING_RESEARCH_VERB.sub("", first_clause)
    return " ".join(first_clause.split())[:180] or "current release status"


def _version_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return ()


def _latest_relevant_version(ledger: ResearchLedger) -> str:
    """Newest discovered version matching any series named in the question."""

    requested = [
        (_version_key(value), value)
        for value in VERSION_TOKEN.findall(ledger.question)
    ]
    candidates: set[str] = set()
    for claim in ledger.relevant_claims:
        candidates.update(VERSION_TOKEN.findall(claim.text))
    if requested:
        prefixes = [parts for parts, _value in requested if parts]
        candidates = {
            value
            for value in candidates
            if any(_version_key(value)[: len(prefix)] == prefix for prefix in prefixes)
        }
    return max(candidates, key=_version_key, default="")


def _status_search_query(
    question: str,
    *,
    independent: bool,
    version: str = "",
) -> str:
    """A short freshness-anchored query for current-version research."""

    subject = _status_subject(question)
    as_of = datetime.now().astimezone().date().isoformat()
    focus = f" {version}" if version and version not in subject else ""
    authority = (
        "independent technical source confirmation"
        if independent
        else "official authoritative source"
    )
    return f"{subject}{focus} latest current as of {as_of} {authority}"


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


def validate_page(
    page: Mapping[str, Any],
    requested_url: str,
) -> dict[str, Any]:
    """Prove that a search result resolved to a readable HTTP page."""

    if not isinstance(page, Mapping):
        raise ValueError("The page reader returned no structured page")
    final_url = str(page.get("url") or requested_url).strip()
    parsed = urlsplit(final_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The result did not resolve to an HTTP page")
    title = " ".join(str(page.get("title") or "").split())[:240]
    text = str(page.get("summary") or page.get("text") or "").strip()
    if len(text) < 40:
        raise ValueError("The page returned too little readable content to validate")
    failure_surface = f"{title}\n{text[:1200]}".casefold()
    signal = next(
        (value for value in INVALID_PAGE_SIGNALS if value in failure_surface),
        "",
    )
    if signal:
        raise ValueError(f"The destination reported an invalid page: {signal}")
    return {
        **dict(page),
        "url": final_url,
        "title": title,
        "summary": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "content_characters": len(text),
        "validation": "validated",
    }


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
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.ledger = ResearchLedger(question, budget=budget)
        self.search = search
        self.read = read
        self.extract = extract
        self.follow_up = follow_up
        self.task = task





        self.on_progress = on_progress

        self.waves: list[dict[str, Any]] = []
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self._next_query = str(question)
        self._checkpoint_completed = False
        self._attempted_urls: set[str] = set()
        self._validation_query_pending = False
        self._restore_checkpoint()

    def run(self, initial_query: str | None = None) -> dict[str, Any]:
        if self._checkpoint_completed:
            report = self.ledger.report()
            report["stop_reason"] = "checkpoint_completed"
            report["waves"] = [
                dict(wave, sites=list(wave["sites"])) for wave in self.waves
            ]
            return report
        if self.ledger.queries:
            query = self._next_query
        elif self.ledger.is_status_question:
            query = _status_search_query(
                self.ledger.question,
                independent=False,
            )
        else:
            query = str(initial_query or self.ledger.question)


        ended = ""

        while True:
            stop, reason = self.ledger.should_stop()
            if stop:
                ended = reason
                break
            if self._stopped():
                return {**self.ledger.report(), "stop_reason": "cancelled"}

            recorded = self.ledger.record_query(query)
            resumable_wave = (
                not recorded
                and bool(self.waves)
                and str(self.waves[-1].get("query") or "") == query
                and str(self.waves[-1].get("state") or "") != "done"
            )
            if not recorded and not resumable_wave:


                ended = "query_repeated"
                break
            if resumable_wave:
                wave = self.waves[-1]
                wave["state"] = "searching"
            else:
                self._event("research_query", query=query)
                wave = self._wave(query)
            self._save_checkpoint(next_query=query)

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
                if opened >= max(1, int(self.ledger.budget.max_sources_per_query)):


                    break
                halt, _ = self.ledger.should_stop()
                if halt:
                    break

                url = str(candidate.get("url") or "")
                normalised_url = url.split("#", 1)[0].rstrip("/")
                if not normalised_url or normalised_url in self._attempted_urls:


                    continue
                self._attempted_urls.add(normalised_url)

                try:
                    page = validate_page(self.read(url), url)
                except Exception as error:
                    self.ledger.reject_source(
                        url,
                        str(error),
                        title=str(candidate.get("title") or ""),
                    )
                    self._event("research_source_failed", url=url, error=str(error)[:200])
                    host = _host_of(url)
                    for site in wave["sites"]:
                        if site["host"] == host:
                            site["state"] = "rejected"
                            site["reason"] = str(error)[:160]
                            break
                    wave["rejected"] += 1
                    self._publish("validating")
                    self._save_checkpoint(next_query=query)
                    continue

                source = self.ledger.add_source(
                    str(page.get("url") or url),
                    str(page.get("title") or candidate.get("title") or ""),
                    requested_url=url,
                    validation=str(page.get("validation") or "validated"),
                    content_sha256=str(page.get("content_sha256") or ""),
                    content_characters=int(page.get("content_characters") or 0),
                )
                if source is None:
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
                    site["state"] = "validated"
                    site["page_type"] = page_type
                    site["final_url"] = source.url
                    break
                wave["opened"] = opened
                wave["verified"] += 1
                if observation is not None:
                    wave["offers_verified"] += 1
                wave["state"] = "reading"
                self._publish("reading")
                self._save_checkpoint(next_query=query)

            if wave.get("validation"):
                self.ledger.validation_rounds_completed += 1
                self._validation_query_pending = False

            if opened == 0:
                if (
                    self.ledger.validation_rounds_completed
                    < self.ledger.budget.validation_rounds
                ):
                    self._validation_query_pending = True
                    query = self._validation_query()
                    self._next_query = query
                    self._save_checkpoint(next_query=query)
                    continue
                ended = (
                    "validation_exhausted"
                    if wave.get("validation")
                    else "no_further_sources"
                )
                break

            stop, reason = self.ledger.should_stop()
            if stop:
                ended = reason
                break

            nxt = self.follow_up(self.ledger) if self.follow_up else None
            if (
                not nxt
                and self.ledger.validation_rounds_completed
                < self.ledger.budget.validation_rounds
            ):
                nxt = self._validation_query()
                self._validation_query_pending = True
            if not nxt and not self.ledger.evidence_sufficient:
                nxt = self._evidence_gap_query()
            if not nxt:
                ended = (
                    "evidence_sufficient"
                    if self.ledger.evidence_sufficient
                    else "no_further_queries"
                )
                break
            query = nxt
            self._next_query = query
            self._save_checkpoint(next_query=query)

        for wave in self.waves:
            wave["state"] = "done"
        self._publish("completed")

        report = self.ledger.report()
        if ended:
            report["stop_reason"] = ended
        report["waves"] = [dict(wave, sites=list(wave["sites"])) for wave in self.waves]
        self._checkpoint_completed = True
        self._save_checkpoint(next_query="", completed=True)
        return report



    def _wave(self, query: str) -> dict[str, Any]:
        """Begin a search wave and announce it."""

        wave = {
            "query": query,
            "validation": self._validation_query_pending,
            "sites": [],
            "opened": 0,
            "verified": 0,
            "offers_verified": 0,
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
                    "profile": self.ledger.budget.profile,
                    "hard_ceiling_seconds": self.ledger.budget.max_seconds,
                    "elapsed_seconds": round(self.ledger.elapsed_seconds, 2),
                    "source_count": len(self.ledger.sources),
                    "claim_count": len(self.ledger.claims),
                    "corroborated": len(self.ledger.corroborated_claims),
                    "independent_publisher_count": len(
                        self.ledger.evidence_publishers
                    ),
                    "disputed": len(self.ledger.disputed_claims),
                    "rejected_source_count": len(self.ledger.rejected_sources),
                    "validation_rounds_completed": (
                        self.ledger.validation_rounds_completed
                    ),
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

    def _validation_query(self) -> str:
        """A focused independent check, not another broad duplicate search."""

        round_number = self.ledger.validation_rounds_completed + 1
        if self.ledger.is_status_question:
            return _status_search_query(
                self.ledger.question,
                independent=True,
                version=_latest_relevant_version(self.ledger),
            )
        disputed = self.ledger.relevant_disputed_claims
        if disputed:
            focus = disputed[0].text[:180]
            return (
                f'{focus} independent source verify contradiction '
                f'cross-check {round_number}'
            )
        unsupported = [
            claim
            for claim in self.ledger.claims.values()
            if self.ledger.independent_source_count(claim)
            < self.ledger.budget.min_independent_sources
        ]
        if unsupported:
            return (
                f'{unsupported[0].text[:180]} independent source verification '
                f'cross-check {round_number}'
            )
        return (
            f'{self.ledger.question} independent primary source verification '
            f'cross-check {round_number}'
        )

    def _evidence_gap_query(self) -> str | None:
        """Deterministic fallback when the model stops before the evidence does."""

        if not self.ledger.is_status_question:
            return None
        query = _status_search_query(
            self.ledger.question,
            independent=True,
            version=_latest_relevant_version(self.ledger),
        )
        searched = {item.casefold() for item in self.ledger.queries}
        if query.casefold() in searched:
            query = f"{query} alternate publisher {len(self.ledger.queries) + 1}"
        return query

    def _save_checkpoint(
        self,
        *,
        next_query: str,
        completed: bool = False,
    ) -> None:
        if self.checkpoint_path is None:
            return
        payload = {
            "schema": CHECKPOINT_SCHEMA,
            "completed": bool(completed),
            "next_query": str(next_query or ""),
            "validation_query_pending": self._validation_query_pending,
            "ledger": self.ledger.checkpoint(),
            "waves": [dict(wave, sites=list(wave["sites"])) for wave in self.waves],
            "attempted_urls": sorted(self._attempted_urls),
        }
        try:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.checkpoint_path.with_suffix(
                self.checkpoint_path.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.checkpoint_path)
        except OSError:



            return

    def _restore_checkpoint(self) -> None:
        if self.checkpoint_path is None or not self.checkpoint_path.is_file():
            return
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if payload.get("schema") != CHECKPOINT_SCHEMA:
                return
            restored = ResearchLedger.from_checkpoint(dict(payload.get("ledger") or {}))
            if restored.question != self.ledger.question:
                return
            self.ledger = restored
            self.waves = [
                dict(wave, sites=list(wave.get("sites") or []))
                for wave in payload.get("waves") or []
                if isinstance(wave, Mapping)
            ]
            self._attempted_urls = {
                str(value) for value in payload.get("attempted_urls") or []
            }
            self._next_query = str(
                payload.get("next_query") or self.ledger.question
            )
            self._validation_query_pending = bool(
                payload.get("validation_query_pending")
            )
            self._checkpoint_completed = bool(payload.get("completed"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return


__all__ = ["ResearchLoop", "statements_from_page", "validate_page"]
