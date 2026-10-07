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
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .ledger import Budget, ResearchLedger
from .query import focused_search_query, requested_urls
from .pages import (
    CATEGORY_LISTING,
    classify_page,
    looks_like_a_range,
    price_observation,
)



SENTENCE = re.compile(r"(?<=[.!?])\s+")


BOILERPLATE = (
    "produced no results", "returned no results",
    "search results 1..0", "no results for:", "search documentation search changelog",
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
    "504 gateway", "504 gateway time-out", "504 gateway timeout", "502 bad gateway",
    "gateway time-out", "service temporarily unavailable",
    "404 not found",
    "checking your browser",
    "verifying your browser",
    "verify you are human",
    "page not found",
    "invite invalid",
    "invite may be expired",
    "link has expired",
    "this link is invalid",
    "content is unavailable",
    "enable javascript and cookies to continue",
    "just a moment",
    "performing security verification",
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


def statements_from_page(
    page: Mapping[str, Any], *, limit: int = 40, question: str = ""
) -> list[str]:
    """Pull candidate statements out of a structured page reading.

    Works from the page summary the browser bridge already produces, so an
    ordinary structured page needs no screenshot and no vision pass.
    """

    text = str(page.get("summary") or page.get("text") or "")
    found: list[str] = []
    # PDF/OCR lines and table rows often lack sentence punctuation. Keep their
    # page context and split at line boundaries instead of dropping a whole page.
    chunks = []
    statement_limit = 900 if page.get('content_type') in {'text/markdown','text/x-markdown'} else MAX_STATEMENT_CHARACTERS
    table_rows = set(str(row) for row in page.get('table_rows') or [])
    if page.get("pages"):
        from .passages import pdf_blocks
        for item in page["pages"]:
            for block in pdf_blocks(str(item.get("text") or "")):
                for raw in SENTENCE.split(block):
                    label = f"; printed label {item['printed_label']}" if item.get('printed_label') else ""
                    chunks.append((raw, f" [PDF page {item.get('page')}{label}]"))
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            # Catalogue identifiers and their descriptions often occupy
            # separate blocks. Keep the binding instead of discarding both
            # as a short ID and an overlong page without sentence punctuation.
            if re.match(r'^(?:ref(?:erence)?|record\s*(?:id|number)|catalogue\s*(?:id|number))\s*:', line, re.I):
                if index+1 < len(lines) and not re.match(r'^ref(?:erence)?\s*:', lines[index+1], re.I):
                    line += ' — ' + lines[index+1]
            if line in table_rows or (page.get('table_rows_read') and ' | ' in line):
                chunks.append((line,''))
            elif page.get('content_type') in {'text/markdown','text/x-markdown'} and len(line)<=statement_limit:
                chunks.append((line,''))
            else:
                chunks.extend((raw, '') for raw in SENTENCE.split(line))
    for raw, page_reference in chunks:
        statement = " ".join(str(raw).split())
        row_limit = 2000 if raw in table_rows or (page.get('table_rows_read') and ' | ' in raw) else statement_limit
        if len(statement) > row_limit:
            continue
        if len(statement.split()) < MIN_STATEMENT_WORDS:
            continue
        lowered = statement.casefold()
        if any(noise in lowered for noise in BOILERPLATE):
            continue
        found.append(statement + page_reference)
        if not question and len(found) >= limit:
            break
    if question:
        noise = {
            "research", "what", "says", "about", "give", "concise", "answer",
            "source", "sources", "link", "links", "documentation", "official",
            "please", "using", "from", "with", "which", "that", "this",
        }
        def terms(value: str) -> set[str]:
            return {word[:5] for word in re.findall(r"[^\W_]+", value.casefold())
                    if len(word) >= 4 and word not in noise}
        wanted = terms(re.sub(r'https?://\S+', ' ', question, flags=re.I))
        found.sort(key=lambda value: len(wanted & terms(value)), reverse=True)
    return found[:limit]


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
    if page.get('render_incomplete'):
        raise ValueError('Rendered page remained too sparse after its bounded content wait')
    final_url = str(page.get("url") or requested_url).strip()
    parsed = urlsplit(final_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The result did not resolve to an HTTP page")
    title = " ".join(str(page.get("title") or "").split())[:240]
    text = str(page.get("summary") or page.get("text") or "").strip()
    if len(text) < 40:
        raise ValueError("The page returned too little readable content to validate")
    # An article explaining "404 Not Found" is readable evidence. Match an
    # error banner, not an arbitrary mention in a title or document body.
    title_banner = title.casefold().strip(' .!\u2026')
    body_banner = text.casefold().lstrip()
    signal = next(
        (value for value in INVALID_PAGE_SIGNALS
         if title_banner == value
         or (len(text) < 2000 and re.match(
             r'^' + re.escape(value) + r'(?:[.!:\n]|$)', body_banner))),
        "",
    )
    if not signal and len(text) < 2000:
        challenge = re.match(r'^(?:checking|verifying) your browser(?:\s*[|\-\u2013]|$)', title_banner)
        if challenge and re.search(r'complete (?:the check|the verification)|security verification', body_banner):
            signal = 'browser verification challenge'
    if signal:
        raise ValueError(f"The destination reported an invalid page: {signal}")
    if re.match(r'^search(?:\s*[|\-\u2013]|$)', title, re.I) and re.search(
            r'\byour query\b.{0,600}\b(?:produced|returned) no results\b', text, re.I | re.S):
        raise ValueError('The destination returned an empty search page, not source evidence')
    return {
        **dict(page),
        "url": final_url,
        "title": title,
        "summary": text,
        # Keep the reader's byte/document hash separate from the bounded text
        # packet used by the ledger. These are different representations.
        **({'reader_content_sha256':page.get('reader_content_sha256',page['content_sha256']),
            'reader_hash_scope':page.get('reader_hash_scope',page.get('hash_scope','unspecified'))}
           if page.get('content_sha256') else {}),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "hash_scope":"validated_summary_utf8",
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
        search_site: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        extract: Callable[[Mapping[str, Any]], Iterable[str]] = statements_from_page,
        follow_up: Callable[[ResearchLedger], str | None] | None = None,
        assess_coverage: Callable[[ResearchLedger], Mapping[str, Any]] | None = None,
        budget: Budget | None = None,
        task: Any = None,
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self.ledger = ResearchLedger(question, budget=budget)
        self.search = search
        self.read = read
        self.search_site = search_site
        self.extract = extract
        self.follow_up = follow_up
        self.assess_coverage = assess_coverage
        self._coverage_reviews = 0
        self._coverage_signature = ''
        self.task = task





        self.on_progress = on_progress

        self.waves: list[dict[str, Any]] = []
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self._next_query = str(question)
        self._checkpoint_completed = False
        self._attempted_urls: set[str] = set()
        self._validation_query_pending = False
        self._pending_candidates: list[dict[str, Any]] = []
        self._host_timeouts: dict[str, int] = {}
        self._restore_checkpoint()
        self.ledger.coverage_required = assess_coverage is not None

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
            query = focused_search_query(str(initial_query or self.ledger.question))


        ended = ""

        while True:
            stop, reason = self.ledger.should_stop()
            if stop:
                ended = reason
                break
            if self._stopped():
                return {**self.ledger.report(), "stop_reason": "cancelled"}

            supplied = requested_urls(self.ledger.question)
            direct_resume = bool(self.waves and self.waves[-1].get('source_origin') == 'user_urls'
                                 and self.waves[-1].get('state') != 'done')
            direct = bool(supplied and (not self.waves or direct_resume))
            recorded = False if direct else self.ledger.record_query(query)
            resumable_wave = (
                not recorded
                and bool(self.waves)
                and str(self.waves[-1].get("query") or "") == query
                and str(self.waves[-1].get("state") or "") != "done"
            ) or direct_resume
            if not recorded and not resumable_wave and not direct:


                ended = "query_repeated"
                break
            if resumable_wave:
                wave = self.waves[-1]
                wave["state"] = "searching"
            else:
                self._event('research_direct_sources' if direct else 'research_query', query=query)
                wave = self._wave(query)
                if direct:
                    wave['source_origin'] = 'user_urls'
            self._save_checkpoint(next_query=query)

            try:
                candidates = (list(self._pending_candidates) if resumable_wave and self._pending_candidates
                              else [{'url':url,'title':'User-provided source'} for url in supplied] if direct
                              else list(self.search(query) or []))
            except Exception as error:
                wave["state"] = "failed"
                wave["error"] = str(error)[:240]
                self._event("research_search_failed", query=query, error=str(error)[:240])
                self._publish("search_failed")
                ended = "search_unavailable"
                break

            requested_types = set(re.findall(r"[a-z]+", self.ledger.question.casefold())) & {
                "documentation", "manual", "specification", "reference",
            }
            question_words=set(re.findall(r'[a-z0-9]+',self.ledger.question.casefold()))
            if question_words & {'original','official','primary'}:
                # Prefer observed destinations whose host names the requested
                # organisation/product over mirrors. This is ordering, not a
                # declaration that a host or its claims have been verified.
                candidates.sort(key=lambda item:bool(question_words &
                    (set(_host_of(str(item.get('url') or '')).split('.'))-
                     {'www','com','org','net','gov','edu','docs'})),reverse=True)
            if requested_types:
                candidates.sort(
                    key=lambda item: bool(requested_types & set(re.findall(
                        r"[a-z]+", str(item.get("title") or "").casefold()
                    ))),
                    reverse=True,
                )



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
            opened_by_host: dict[str,int] = {}
            initial_hosts={_host_of(str(item.get('url') or '')) for item in candidates}
            for candidate_index, candidate in enumerate(candidates):
                if self._stopped():
                    return {**self.ledger.report(), "stop_reason": "cancelled"}
                if opened >= max(1, int(self.ledger.budget.max_sources_per_query)):


                    break
                halt, halt_reason = self.ledger.should_stop()
                # The final allowed query still owns its result-reading wave.
                if halt and halt_reason != "query_budget" and not (
                    halt_reason == "evidence_sufficient" and candidate.get("_discovered_from")
                ):
                    break

                self._pending_candidates = list(candidates[candidate_index+1:])

                url = str(candidate.get("url") or "")
                candidate_host=_host_of(url)
                if len(initial_hosts)>1 and opened_by_host.get(candidate_host,0)>=3:
                    continue
                normalised_url = url.split("#", 1)[0].rstrip("/")
                if candidate.get('_site_search'):
                    normalised_url += '::site-search:'+str(candidate['_site_search'].get('term'))
                if not normalised_url or normalised_url in self._attempted_urls:


                    continue
                self._attempted_urls.add(normalised_url)




                host = _host_of(url)
                if self._host_timeouts.get(host, 0) >= 2:
                    self.ledger.reject_source(url, 'Host repeatedly timed out during this search; other sources remain eligible.',
                        title=str(candidate.get('title') or ''))
                    continue
                for site in wave["sites"]:
                    if site["host"] == host:
                        site["state"] = "reading"
                        break
                wave["state"] = "reading"
                self._publish("reading")

                try:
                    if candidate.get('_site_search'):
                        if self.search_site is None:continue
                        page=validate_page(self.search_site(url,candidate['_site_search']),url)
                    else:
                        page = validate_page(self.read(url), url)
                except Exception as error:
                    if re.search(r'timed?\s*out|timeout|did not finish within', str(error), re.I):
                        self._host_timeouts[host] = self._host_timeouts.get(host, 0) + 1
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

                from .discovery import relevant_links, catalogue_search_links, url_key
                linked_sources = relevant_links(self.ledger.question, page,
                    depth=int(candidate.get('_discovery_depth') or 0),
                    attempted=list(self._attempted_urls))
                search_forms = catalogue_search_links(self.ledger.question,page,
                    depth=int(candidate.get('_discovery_depth') or 0))
                # Read observed source links before trying a site's generic
                # search box. Keep exact catalogue lookups ahead when needed.
                from .query import requested_identifiers
                discovered = (search_forms + linked_sources if requested_identifiers(self.ledger.question)
                              else linked_sources + search_forms)
                # Prioritize the newly discovered source within this wave.
                # Appending after all search hits made it unreachable at the
                # ordinary per-query page limit, even when it held the answer.
                earlier = {url_key(str(item.get('url') or '')) for item in candidates[:candidate_index+1]}
                discovered = [item for item in discovered if
                    (item.get('_site_search') and self.search_site is not None) or url_key(item['url']) not in earlier]
                discovered_keys = {url_key(item['url']) for item in discovered}
                tail = [item for item in candidates[candidate_index+1:]
                        if url_key(str(item.get('url') or '')) not in discovered_keys]
                candidates[candidate_index+1:] = [*discovered, *tail]
                self._pending_candidates = list(candidates[candidate_index+1:])
                for link in discovered:
                    wave['sites'].append({'host':_host_of(link['url']), 'url':link['url'],
                        'title':link['title'], 'state':'found',
                        'discovered_from':link['_discovered_from'], 'depth':link['_discovery_depth']})

                source = self.ledger.add_source(
                    str(page.get("url") or url),
                    str(page.get("title") or candidate.get("title") or ""),
                    requested_url=url,
                    validation=str(page.get("validation") or "validated"),
                    content_sha256=str(page.get("content_sha256") or ""),
                    content_characters=int(page.get("content_characters") or 0),
                )
                if source is None:
                    for site in wave["sites"]:
                        if site["host"] == host:
                            site["state"] = "skipped"
                            site["reason"] = (
                                "This destination duplicated evidence already read."
                            )
                            break
                    self._publish("validating")
                    self._save_checkpoint(next_query=query)
                    continue
                opened_by_host[host]=opened_by_host.get(host,0)+1





                page_type = classify_page(page)
                source.page_type = page_type
                source.retrieval_details = {
                    key: page[key] for key in (
                        'retrieval', 'page_count', 'truncated', 'read_limit',
                        'downloaded_bytes', 'document_bytes', 'range_requests',
                        'extraction', 'representation_validator', 'hash_scope',
                        'reader_content_sha256','reader_hash_scope',
                        'selected_ranges','selection','source_text_characters',
                        'content_type','table_rows_read','scanned_text_characters',
                        'page_text_characters','fallback_reason','render_wait_seconds','visibility',
                    ) if key in page
                }
                if candidate.get('_discovered_from'):
                    source.retrieval_details.update(discovered_from=candidate['_discovered_from'],
                        discovery_depth=int(candidate['_discovery_depth']))
                if page.get('pages'):
                    source.retrieval_details['pages_read'] = [item['page'] for item in page['pages']]
                statements = list(
                    statements_from_page(page, question=self.ledger.question)
                    if self.extract is statements_from_page
                    else self.extract(page)
                )
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




            wave["state"] = "done"
            wave["unread_discovered_links"] = [str(item.get('url') or '')
                for item in self._pending_candidates if item.get('_discovered_from')]
            self._pending_candidates = []
            self._publish("comparing")
            self._save_checkpoint(next_query=query)

            signature=json.dumps([(c.claim_id,tuple(c.sources),tuple(c.contradicts)) for c in self.ledger.relevant_claims],sort_keys=True)
            if (self.assess_coverage is not None and self.ledger.claims
                    and self.ledger.validation_rounds_completed >= self.ledger.budget.validation_rounds
                    and signature!=self._coverage_signature
                    and self._coverage_reviews<3 and not self._stopped()
                    and self.ledger.elapsed_seconds-self.ledger.coverage_seconds < self.ledger.budget.max_seconds):
                self._publish('assessing_coverage')
                started=time.monotonic()
                try:review=dict(self.assess_coverage(self.ledger))
                finally:
                    self.ledger.coverage_seconds+=time.monotonic()-started
                    self._coverage_reviews+=1
                self.ledger.coverage_assessment=review
                self._coverage_signature=signature
                self.ledger.open_questions=list(review.get('missing') or [])
                self._save_checkpoint(next_query=query)

            if opened == 0:
                coverage_query=(self.ledger.coverage_assessment or {}).get('query')
                if self.ledger.evidence_sufficient:
                    ended='evidence_sufficient';break
                if direct:
                    query = focused_search_query(self.ledger.question)
                    self._next_query = query
                    self._save_checkpoint(next_query=query)
                    continue
                if coverage_query and coverage_query.casefold() not in {q.casefold() for q in self.ledger.queries}:
                    query=coverage_query;self._next_query=query
                    self._save_checkpoint(next_query=query)
                    continue
                from .query import discovery_queries
                alternate = next((q for q in discovery_queries(self.ledger.question)
                                  if q not in self.ledger.queries), None)
                if alternate:
                    query=alternate
                    self._next_query=query
                    self._save_checkpoint(next_query=query)
                    continue
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

            if self.assess_coverage is not None and self._coverage_reviews>=3:
                ended='coverage_review_limit';break

            if (
                self.ledger.validation_rounds_completed
                < self.ledger.budget.validation_rounds
            ):
                nxt = self._validation_query()
                self._validation_query_pending = True
            else:
                nxt = ((self.ledger.coverage_assessment or {}).get('query')
                       if self.assess_coverage is not None else
                       self.follow_up(self.ledger) if self.follow_up else None)
            if not nxt and direct and not self.ledger.evidence_sufficient:
                nxt = focused_search_query(self.ledger.question)
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
            if wave.get("state") != "failed": wave["state"] = "done"




        self._publish("retrieval_completed")

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
                    "coverage_required": self.ledger.coverage_required,
                    "coverage_seconds": round(self.ledger.coverage_seconds,3),
                    "retrieval_seconds": round(max(0,self.ledger.elapsed_seconds-self.ledger.coverage_seconds),3),
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
                    "validation_rounds_required": (
                        self.ledger.budget.validation_rounds
                    ),
                    "minimum_independent_sources": (
                        self.ledger.budget.min_independent_sources
                    ),
                    "coverage_target": self.ledger.budget.coverage_target,
                    "query_count": len(self.ledger.queries),
                    "evidence_sufficient": self.ledger.evidence_sufficient,
                    "coverage_assessment": self.ledger.coverage_assessment,
                    "open_questions": list(self.ledger.open_questions),
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
        from .query import discovery_queries
        focused=next((q for q in discovery_queries(self.ledger.question) if q not in self.ledger.queries),None)
        if focused:return focused
        if self.ledger.is_status_question:
            return _status_search_query(
                self.ledger.question,
                independent=True,
                version=_latest_relevant_version(self.ledger),
            )
        disputed = self.ledger.relevant_disputed_claims
        if disputed:
            focus = re.sub(r"\s*\[PDF page[^\]]*\]", "", disputed[0].text)[:110]
            return (
                f'{focused_search_query(self.ledger.question)[:180]} {focus} verification'
            )
        unsupported = [
            claim
            for claim in self.ledger.relevant_claims
            if self.ledger.independent_source_count(claim)
            < self.ledger.budget.min_independent_sources
        ]
        if unsupported:
            return (
                f'{focused_search_query(self.ledger.question)} original publication'
            )
        return (
            f'{focused_search_query(self.ledger.question)} primary source'
        )

    def _evidence_gap_query(self) -> str | None:
        """Deterministic fallback when the model stops before the evidence does."""

        if not self.ledger.is_status_question:
            from .query import discovery_queries
            return next((q for q in discovery_queries(self.ledger.question)
                         if q not in self.ledger.queries), None)
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
            "pending_candidates": self._pending_candidates,
            "host_timeouts": self._host_timeouts,
            "coverage_reviews": self._coverage_reviews,
            "coverage_signature": self._coverage_signature,
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
            self._pending_candidates = [dict(item) for item in payload.get('pending_candidates') or []
                                        if isinstance(item, Mapping)]
            self._host_timeouts = {str(k):int(v) for k,v in (payload.get('host_timeouts') or {}).items()}
            self._coverage_reviews=max(0,int(payload.get('coverage_reviews') or 0))
            self._coverage_signature=str(payload.get('coverage_signature') or '')
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
