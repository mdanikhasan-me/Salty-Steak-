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
from urllib.parse import urlsplit
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

LEDGER_SCHEMA = "salty-steak-research-ledger-v2"


DUPLICATE_THRESHOLD = 0.72





CONTAINMENT_THRESHOLD = 0.75
MIN_CONTAINED_WORDS = 4



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




DELEGATED_SUFFIX_LABELS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})
MULTI_TENANT_SUFFIXES = frozenset(
    {
        "blogspot.com",
        "github.io",
        "netlify.app",
        "pages.dev",
        "substack.com",
        "vercel.app",
        "wordpress.com",
    }
)







NON_EDITORIAL_PUBLISHERS = frozenset(
    {
        "github.com",
        "githubusercontent.com",
        "scribd.com",
        "slideshare.net",
    }
)







RESEARCH_REQUEST_WORDS = frozenset(
    """
    answer any citation citations cite concise direct disagreement disagreements
    fewer find findings give identify independent information least official one
    prefer provide research result results source sources technical using validate
    validated validation verification verify
    what when where why how does do can could would should explain describe compare
    """.split()
)
STATUS_QUESTION_WORDS = frozenset(
    {"current", "latest", "release", "stable", "status", "version"}
)
STATUS_CLAIM_WORDS = frozenset(
    {
        "available",
        "bugfix",
        "current",
        "latest",
        "maintenance",
        "newest",
        "released",
        "stable",
    }
)


def _words(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W_]+(?:'[^\W_]+)?", str(text).casefold())
        if word not in NOISE and len(word) > 1
    }


def _numbers(text: str) -> set[str]:
    """Every value a statement asserts, in digits or in words."""





    lowered = re.sub(
        r"\b\d+(?:\.\d+){1,3}\b",
        " ",
        str(text).casefold(),
    )
    found = set(re.findall(r"\d+(?:[.,]\d+)?", lowered))
    found.update(word for word in re.findall(r"[a-z]+", lowered) if word in NUMBER_WORDS)
    return found


def similarity(left: str, right: str) -> float:
    """How much two statements overlap, ignoring filler."""

    first, second = _words(left), _words(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def publisher_domain(value: str) -> str:
    """Return the organisational domain used for independence checks.

    ``docs.python.org`` and ``blog.python.org`` are separate hosts but not
    independent publishers. Counting them twice caused the live research gate
    to stop before it had found the independent technical source the user
    explicitly requested.
    """

    raw = str(value or "").strip().casefold()
    host = (urlsplit(raw).hostname or raw).strip(".")
    if not host or ":" in host or re.fullmatch(r"\d+(?:\.\d+){3}", host):
        return host
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in MULTI_TENANT_SUFFIXES:
        return ".".join(labels[-3:])
    if len(labels[-1]) == 2 and labels[-2] in DELEGATED_SUFFIX_LABELS:
        return ".".join(labels[-3:])
    return suffix


def independence_key(value: str, *, title: str = "") -> str:
    """Return the publisher identity that can count as independent evidence.

    The visible host remains available through :func:`publisher_domain`.  This
    stricter identity is only for corroboration and completion gates.  A mirror
    whose title explicitly attributes another domain is assigned to that
    origin, while generic code/document hosts do not masquerade as editorially
    independent publishers.
    """

    base = publisher_domain(value)
    written_title = str(title or "")
    tail = written_title.rsplit("|", 1)[-1]
    attributed = (
        re.findall(
            r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b",
            tail.casefold(),
        )
        if "|" in written_title
        else []
    )
    if attributed:
        origin = publisher_domain(attributed[-1])
        if origin:
            return origin
    if base in NON_EDITORIAL_PUBLISHERS:
        return ""
    return base


def _versions(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:\.\d+){1,3}\b", str(text).casefold()))


def _version_parts(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return ()


def _same_statement(left: str, right: str) -> bool:
    """Merge lexical duplicates only; retain unverified paraphrases separately.

    Word overlap discards subject/object order, predicates and qualifications.
    Those differences must survive for the answer model to compare them.
    """
    def ordered_words(value: str) -> list[str]:
        return re.findall(r"[^\W_]+(?:[.'][^\W_]+)*", value.casefold())
    first, second = ordered_words(left), ordered_words(right)
    return bool(first) and first == second


@dataclass
class Source:
    """Somewhere a claim came from."""

    source_id: str
    url: str
    title: str = ""
    retrieved_at: float = field(default_factory=time.time)


    page_type: str = "unknown"
    requested_url: str = ""
    validation: str = "validated"
    content_sha256: str = ""
    content_characters: int = 0
    retrieval_details: dict[str, Any] = field(default_factory=dict)
    document_fingerprint: dict[str, Any] = field(default_factory=dict)
    document_group: str = ''
    document_match: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return (urlsplit(self.url).hostname or "").casefold()

    @property
    def publisher_domain(self) -> str:
        return publisher_domain(self.domain)

    @property
    def independence_key(self) -> str:
        return independence_key(self.url, title=self.title)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_id,
            "url": self.url,
            "title": self.title,
            "retrieved_at": self.retrieved_at,
            "page_type": self.page_type,
            "requested_url": self.requested_url or self.url,
            "validation": self.validation,
            "domain": self.domain,
            "publisher_domain": self.publisher_domain,
            "independence_key": self.independence_key,
            "content_sha256": self.content_sha256,
            "content_characters": self.content_characters,
            "retrieval_details": dict(self.retrieval_details),
            "document_group": self.document_group or self.source_id,
            "document_match": dict(self.document_match),
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


    max_sources_per_query: int = 8

    coverage_target: int = 5

    barren_limit: int = 2

    min_independent_sources: int = 2


    validation_rounds: int = 0
    profile: str = "instant"

    @classmethod
    def for_profile(cls, profile: str) -> "Budget":
        selected = str(profile or "verification").strip().casefold()
        if selected == "cooking":
            return cls(
                max_sources=4096,
                max_queries=1024,
                max_seconds=14_400.0,
                max_sources_per_query=8,
                coverage_target=12,
                barren_limit=12,
                min_independent_sources=2,
                validation_rounds=2,
                profile="cooking",
            )
        if selected == "instant":
            return cls(
                max_sources=64,
                max_queries=24,
                max_seconds=300.0,
                max_sources_per_query=5,
                coverage_target=6,
                barren_limit=5,
                min_independent_sources=2,
                validation_rounds=1,
                profile="instant",
            )
        return cls(
            max_sources=16,
            max_queries=8,
            max_seconds=180.0,
            max_sources_per_query=4,
            coverage_target=2,
            barren_limit=3,
            min_independent_sources=2,
            validation_rounds=1,
            profile="verification",
        )


class ResearchLedger:
    """Accumulate evidence, keep its provenance, and know when to stop."""

    def __init__(self, question: str, *, budget: Budget | None = None) -> None:
        self.question = str(question)
        self.budget = budget or Budget()
        self.sources: dict[str, Source] = {}
        self.claims: dict[str, Claim] = {}

        self.observations: list[dict[str, Any]] = []
        self.rejected_sources: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.open_questions: list[str] = []
        self.started_at = time.monotonic()
        self._elapsed_before_resume = 0.0
        self._barren = 0
        self._seen_urls: set[str] = set()
        self.validation_rounds_completed = 0
        self.coverage_required = False
        self.coverage_assessment: dict[str, Any] | None = None
        self.coverage_seconds = 0.0



    def record_query(self, query: str) -> bool:
        """Note a search. False when it has already been tried."""

        text = str(query).strip()
        if not text or text.casefold() in {item.casefold() for item in self.queries}:
            return False
        self.queries.append(text)
        return True

    def add_source(
        self,
        url: str,
        title: str = "",
        *,
        requested_url: str = "",
        validation: str = "validated",
        content_sha256: str = "",
        content_characters: int = 0,
        document_fingerprint: Mapping[str, Any] | None = None,
    ) -> Source | None:
        """Register a source. None when it was already read."""

        clean = str(url).split("#", 1)[0].rstrip("/")
        if clean in self._seen_urls:
            return None
        self._seen_urls.add(clean)
        source = Source(
            source_id=f"src-{len(self.sources) + 1}",
            url=clean,
            title=str(title),
            requested_url=str(requested_url or clean),
            validation=str(validation or "validated"),
            content_sha256=str(content_sha256 or ""),
            content_characters=max(0, int(content_characters or 0)),
            document_fingerprint=dict(document_fingerprint or {}),
        )
        source.document_group = source.source_id
        from .document_identity import copied_document_similarity
        matches = []
        for previous in self.sources.values():
            similarity = copied_document_similarity(source.document_fingerprint, previous.document_fingerprint)
            if similarity is not None and similarity >= 0.85:
                matches.append((previous, similarity))
        if matches:
            representative = matches[0][0].document_group or matches[0][0].source_id
            groups = {item.document_group or item.source_id for item,_ in matches}
            for previous in self.sources.values():
                if previous.document_group in groups:
                    previous.document_group = representative
            source.document_group = representative
            source.document_match = {'method':'near_duplicate_extracted_text',
                                     'source':matches[0][0].source_id,
                                     'estimated_jaccard':round(matches[0][1],4),
                                     'scope':'copy_detection_not_verified_authorship'}
        self.sources[source.source_id] = source
        return source

    def reject_source(self, url: str, reason: str, *, title: str = "") -> None:
        clean = str(url).split("#", 1)[0].rstrip("/")
        if not clean:
            return
        self.rejected_sources.append(
            {
                "url": clean,
                "title": str(title or "")[:240],
                "reason": str(reason or "unreadable")[:240],
            }
        )

    def add_claim(self, text: str, source_id: str) -> Claim:
        """Add a statement, merging duplicates and flagging disagreements.

        A claim found in two places is one claim with two sources — that is
        corroboration, and collapsing it is what keeps the ledger small. A
        claim that conflicts with an existing one is kept separately, because
        losing the disagreement would be losing the most useful thing found.
        """

        text = " ".join(str(text).split())
        for existing in self.claims.values():
            if not _same_statement(existing.text, text):
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
            if (
                self._has_independent_publisher(existing, source_id)
                and similarity(existing.text, text) >= 0.4
                and self._conflicts(existing.text, text)
            ):
                claim.contradicts.append(existing.claim_id)
                existing.contradicts.append(claim.claim_id)

                existing.confidence = min(existing.confidence, 0.4)
                claim.confidence = 0.4
        self.claims[claim.claim_id] = claim
        return claim

    def _has_independent_publisher(self, claim: Claim, source_id: str) -> bool:
        """Whether a new claim comes from a distinct editorial publisher.

        A disagreement is specifically a difference *between sources*. Two
        lifecycle rows on one page, two pages from python.org, or a GitHub
        mirror must not manufacture a cross-source dispute.
        """

        existing = self._independent_keys(claim.sources)
        return bool(existing) and len(self._independent_keys([*claim.sources,source_id])) > len(existing)

    def record_observation(self, observation: Mapping[str, Any]) -> None:
        """One page binding one product to one price, kept whole.

        These never merge with each other. Two retailers selling the same
        drive at different prices are two facts, not a disagreement to average
        away, and a follow-up asking for exact links needs each one's own
        address.
        """

        self.observations.append(dict(observation))

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

        if _same_statement(left, right):
            return False

        left_versions, right_versions = _versions(left), _versions(right)
        if left_versions and right_versions and left_versions != right_versions:




            current_words = {"current", "latest"}
            if (
                _words(left) & current_words
                and _words(right) & current_words
            ):
                return True
            return False
        left_numbers, right_numbers = _numbers(left), _numbers(right)
        if left_numbers and right_numbers and left_numbers != right_numbers:
            # Different numbers in related sentences often describe different
            # fields, years or quantities. Only flag a numeric conflict when
            # the surrounding assertion is the same.
            def numeric_shape(value: str) -> str:
                value=re.sub(r'\b\d+(?:[.,]\d+)*\b','<number>',value.casefold())
                for word in NUMBER_WORDS:
                    value=re.sub(r'\b'+re.escape(word)+r'\b','<number>',value)
                return ' '.join(value.split()).rstrip('.;')
            return numeric_shape(left)==numeric_shape(right)
        left_words, right_words = _words(left), _words(right)
        for positive, negative in (
            ({"allow", "allows", "allowed", "permit", "permits", "permitted"},
             {"ban", "bans", "banned", "prohibit", "prohibits", "prohibited", "forbid", "forbids", "forbidden"}),
            ({"enable", "enables", "enabled"}, {"disable", "disables", "disabled"}),
        ):
            left_polarity = (bool(left_words & positive), bool(left_words & negative))
            right_polarity = (bool(right_words & positive), bool(right_words & negative))
            if {left_polarity, right_polarity} == {(True, False), (False, True)}:
                def assertion(value: str) -> list[str]:
                    return [word for word in re.findall(r"[^\W_]+", value.casefold())
                            if word not in positive | negative]
                if assertion(left) == assertion(right):
                    return True
        negated_left = bool(left_words & NEGATIONS)
        negated_right = bool(right_words & NEGATIONS)
        return negated_left != negated_right



    @property
    def elapsed_seconds(self) -> float:
        return self._elapsed_before_resume + time.monotonic() - self.started_at

    def independent_source_count(self, claim: Claim) -> int:
        return len(self._independent_keys(claim.sources))

    def _independent_keys(self, source_ids: Iterable[str]) -> set[str]:
        """Group contributing evidence by publisher OR copied document.

        Only the contributing sources participate: copying one document does
        not make a publisher's unrelated original reporting a mirror too.
        """
        groups = []
        for identifier in sorted(set(source_ids)):
            source = self.sources.get(identifier)
            if source is None or not source.independence_key:
                continue
            publishers = {source.independence_key}
            documents = {source.document_group or source.source_id}
            remaining = []
            for old_publishers, old_documents in groups:
                if publishers & old_publishers or documents & old_documents:
                    publishers |= old_publishers
                    documents |= old_documents
                else:
                    remaining.append((old_publishers,old_documents))
            # A merge may connect a component visited earlier in this pass.
            changed = True
            while changed:
                changed = False
                for item in remaining[:]:
                    if publishers & item[0] or documents & item[1]:
                        publishers |= item[0]; documents |= item[1]
                        remaining.remove(item); changed = True
            groups = [*remaining,(publishers,documents)]
        return {min(publishers) for publishers,_ in groups}

    @property
    def evidence_publishers(self) -> set[str]:
        """Editorial publishers that contributed at least one extracted claim."""

        contributing = {
            source_id
            for claim in self.claims.values()
            for source_id in claim.sources
        }
        return self._independent_keys(contributing)

    @property
    def is_status_question(self) -> bool:
        """Whether the question asks for a current/latest version status."""
        # Stock status and current prices are not software release/version
        # questions. That route adds irrelevant technical-release query terms
        # and can stop before any usable offer has been observed.
        if re.search(r"\b(?:prices?|stock|retailers?|shopping|buy)\b", self.question, re.I):
            return False
        return bool(_words(self.question) & STATUS_QUESTION_WORDS)

    @property
    def question_subject_words(self) -> set[str]:
        """Meaningful subject words after removing research instructions."""

        return _words(self.question) - RESEARCH_REQUEST_WORDS - STATUS_QUESTION_WORDS

    def claim_relevant_to_question(self, claim: Claim) -> bool:
        """Whether a claim can contribute to this question's completion gate."""

        words = _words(claim.text)
        subject = self.question_subject_words
        if not self.is_status_question:
            # This is a conservative lexical eligibility test, not proof that
            # the claim is true. Unrelated snippets must not meet coverage.
            return not subject or len(words & subject) >= min(2, len(subject))
        subject_matches = len(words.intersection(subject))
        required_subject_matches = min(2, len(subject))
        if subject and subject_matches < required_subject_matches:
            return False
        return bool(words & STATUS_CLAIM_WORDS)

    @property
    def relevant_claims(self) -> list[Claim]:
        return [
            claim
            for claim in self.claims.values()
            if self.claim_relevant_to_question(claim)
        ]

    @property
    def relevant_evidence_publishers(self) -> set[str]:
        contributing = {
            source_id
            for claim in self.relevant_claims
            for source_id in claim.sources
        }
        return self._independent_keys(contributing)

    @property
    def relevant_corroborated_claims(self) -> list[Claim]:
        required = max(2, int(self.budget.min_independent_sources))
        return [
            claim
            for claim in self.relevant_claims
            if self.independent_source_count(claim) >= required
        ]

    @property
    def relevant_disputed_claims(self) -> list[Claim]:
        return [claim for claim in self.relevant_claims if claim.disputed]

    @property
    def status_target_version(self) -> str:
        """Newest discovered patch in a version series named by the question."""

        requested = [_version_parts(value) for value in _versions(self.question)]
        if not requested:
            return ""
        candidates = {
            value
            for claim in self.relevant_claims
            for value in _versions(claim.text)
            if any(
                len(_version_parts(value)) > len(prefix)
                and _version_parts(value)[: len(prefix)] == prefix
                for prefix in requested
                if prefix
            )
        }
        return max(candidates, key=_version_parts, default="")

    @property
    def status_target_publishers(self) -> set[str]:
        """Publishers that actually support the newest matching patch."""

        target = self.status_target_version
        if not target:
            return set(self.relevant_evidence_publishers)
        contributing = {
            source_id
            for claim in self.relevant_claims
            if target in _versions(claim.text)
            for source_id in claim.sources
        }
        return self._independent_keys(contributing)

    @property
    def evidence_sufficient(self) -> bool:
        """Whether evidence relevant to the actual question meets the gate."""
        from .query import requested_identifiers
        identifiers=requested_identifiers(self.question)
        if identifiers:
            statements=[' '.join(c.text.casefold().split()) for c in self.claims.values()]
            if any(not any(' '.join(identifier.casefold().split()) in text for text in statements)
                   for identifier in identifiers):
                return False
        required = max(2, int(self.budget.min_independent_sources))
        if self.coverage_required:
            review=self.coverage_assessment or {}
            return bool(self.claims) and review.get('status')=='assessed' and review.get('sufficient') is True
        if self.is_status_question:
            return bool(self.relevant_claims) and len(
                self.status_target_publishers
            ) >= required
        return (
            len(self.relevant_corroborated_claims) >= self.budget.coverage_target
            or (
                len(self.relevant_claims) >= self.budget.coverage_target
                and len(self.relevant_evidence_publishers) >= required
            )
        )

    @property
    def corroborated_claims(self) -> list[Claim]:
        required = max(2, int(self.budget.min_independent_sources))
        return [
            claim
            for claim in self.claims.values()
            if self.independent_source_count(claim) >= required
        ]

    @property
    def disputed_claims(self) -> list[Claim]:
        return [claim for claim in self.claims.values() if claim.disputed]

    def should_stop(self) -> tuple[bool, str]:
        """Whether to stop, and the named reason why."""



        if self.elapsed_seconds - self.coverage_seconds >= self.budget.max_seconds:
            return True, "time_budget"
        if (
            self.evidence_sufficient
            and self.validation_rounds_completed >= self.budget.validation_rounds
        ):
            return True, "evidence_sufficient"
        if len(self.sources) >= self.budget.max_sources:
            return True, "source_budget"
        if len(self.queries) >= self.budget.max_queries:
            return True, "query_budget"
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
        return [self._claim_record(claim) for claim in ranked[:limit]]

    def _claim_record(self, claim: Claim) -> dict[str, Any]:
        record = claim.to_dict()
        evidence = self.provenance(claim.claim_id)
        independent = self.independent_source_count(claim)
        record["evidence"] = evidence
        record["independent_source_count"] = independent
        record["corroborated"] = independent >= max(
            2, int(self.budget.min_independent_sources)
        )
        record["validated"] = bool(evidence) and all(
            source.get("validation") == "validated" for source in evidence
        )
        record["validation_scope"] = "source_readability"
        record["support_status"] = (
            "disputed" if claim.disputed else
            "independently_repeated" if record["corroborated"] else "single_source"
        )
        record["confidence_scope"] = "retrieval_heuristic_not_truth_probability"
        return record

    def report(self) -> dict[str, Any]:
        stop, reason = self.should_stop()
        return {
            "schema": LEDGER_SCHEMA,
            "question": self.question,
            "queries": list(self.queries),
            "sources": [source.to_dict() for source in self.sources.values()],
            "claims": [self._claim_record(claim) for claim in self.claims.values()],
            "observations": [dict(item) for item in self.observations],
            "rejected_sources": [dict(item) for item in self.rejected_sources],
            "source_count": len(self.sources),
            "claim_count": len(self.claims),
            "corroborated": len(self.corroborated_claims),
            "independent_publisher_count": len(self.evidence_publishers),
            "disputed": len(self.disputed_claims),
            "relevant_claim_count": len(self.relevant_claims),
            "relevant_corroborated": len(self.relevant_corroborated_claims),
            "relevant_disputed": len(self.relevant_disputed_claims),
            "relevant_publisher_count": len(self.relevant_evidence_publishers),
            "status_target_version": self.status_target_version,
            "status_target_publisher_count": len(self.status_target_publishers),
            "evidence_sufficient": self.evidence_sufficient,
            "open_questions": list(self.open_questions),
            "coverage_assessment": self.coverage_assessment,
            "coverage_required": self.coverage_required,
            "coverage_seconds": round(self.coverage_seconds,3),
            "retrieval_seconds": round(max(0,self.elapsed_seconds-self.coverage_seconds),3),
            "stopped": stop,
            "stop_reason": reason,
            "seconds": round(self.elapsed_seconds, 2),
            "profile": self.budget.profile,
            "hard_ceiling_seconds": self.budget.max_seconds,
            "coverage_target": self.budget.coverage_target,
            "minimum_independent_sources": self.budget.min_independent_sources,
            "validation_rounds_required": self.budget.validation_rounds,
            "maximum_sources": self.budget.max_sources,
            "maximum_queries": self.budget.max_queries,
            "validation_rounds_completed": self.validation_rounds_completed,
        }

    def provenance(self, claim_id: str) -> list[dict[str, Any]]:
        """Where a specific statement came from, so an answer can be checked."""

        claim = self.claims[claim_id]
        return [self.sources[source].to_dict() for source in claim.sources]

    def checkpoint(self) -> dict[str, Any]:
        """A complete, JSON-safe ledger snapshot for restart recovery."""

        return {
            "schema": LEDGER_SCHEMA,
            "coverage_assessment": self.coverage_assessment,
            "coverage_required": self.coverage_required,
            "coverage_seconds": self.coverage_seconds,
            "question": self.question,
            "budget": {
                name: getattr(self.budget, name)
                for name in Budget.__dataclass_fields__
            },
            "sources": [{**source.to_dict(), 'document_fingerprint':source.document_fingerprint}
                        for source in self.sources.values()],
            "claims": [claim.to_dict() for claim in self.claims.values()],
            "observations": [dict(item) for item in self.observations],
            "rejected_sources": [dict(item) for item in self.rejected_sources],
            "queries": list(self.queries),
            "open_questions": list(self.open_questions),
            "elapsed_seconds": self.elapsed_seconds,
            "barren": self._barren,
            "seen_urls": sorted(self._seen_urls),
            "validation_rounds_completed": self.validation_rounds_completed,
        }

    @classmethod
    def from_checkpoint(cls, payload: Mapping[str, Any]) -> "ResearchLedger":
        if payload.get("schema") != LEDGER_SCHEMA:
            raise ValueError("Unsupported research checkpoint schema")
        budget_payload = dict(payload.get("budget") or {})
        budget = Budget(
            **{
                name: budget_payload[name]
                for name in Budget.__dataclass_fields__
                if name in budget_payload
            }
        )
        ledger = cls(str(payload.get("question") or ""), budget=budget)
        ledger.coverage_required = bool(payload.get('coverage_required'))
        ledger.coverage_seconds=max(0,float(payload.get('coverage_seconds') or 0))
        review=payload.get('coverage_assessment')
        ledger.coverage_assessment = dict(review) if isinstance(review,Mapping) else None
        ledger.sources = {}
        for item in payload.get("sources") or []:
            if not isinstance(item, Mapping):
                continue
            source = Source(
                source_id=str(item.get("source") or f"src-{len(ledger.sources) + 1}"),
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                retrieved_at=float(item.get("retrieved_at") or time.time()),
                page_type=str(item.get("page_type") or "unknown"),
                requested_url=str(item.get("requested_url") or item.get("url") or ""),
                validation=str(item.get("validation") or "validated"),
                content_sha256=str(item.get("content_sha256") or ""),
                content_characters=max(0, int(item.get("content_characters") or 0)),
                retrieval_details=dict(item.get("retrieval_details") or {}),
                document_fingerprint=dict(item.get('document_fingerprint') or {}),
                document_group=str(item.get('document_group') or item.get('source') or ''),
                document_match=dict(item.get('document_match') or {}),
            )
            ledger.sources[source.source_id] = source
        ledger.claims = {}
        for item in payload.get("claims") or []:
            if not isinstance(item, Mapping):
                continue
            claim = Claim(
                claim_id=str(item.get("claim") or f"clm-{len(ledger.claims) + 1}"),
                text=str(item.get("text") or ""),
                sources=[str(value) for value in item.get("sources") or []],
                contradicts=[str(value) for value in item.get("contradicts") or []],
                confidence=float(item.get("confidence") or 0.5),
            )
            ledger.claims[claim.claim_id] = claim
        ledger.observations = [
            dict(item) for item in payload.get("observations") or []
            if isinstance(item, Mapping)
        ]
        ledger.rejected_sources = [
            dict(item) for item in payload.get("rejected_sources") or []
            if isinstance(item, Mapping)
        ]
        ledger.queries = [str(value) for value in payload.get("queries") or []]
        ledger.open_questions = [
            str(value) for value in payload.get("open_questions") or []
        ]
        ledger._elapsed_before_resume = max(
            0.0, float(payload.get("elapsed_seconds") or 0.0)
        )
        ledger.started_at = time.monotonic()
        ledger._barren = max(0, int(payload.get("barren") or 0))
        ledger._seen_urls = {
            str(value) for value in payload.get("seen_urls") or []
        } | {source.url for source in ledger.sources.values()}
        ledger.validation_rounds_completed = max(
            0, int(payload.get("validation_rounds_completed") or 0)
        )
        return ledger


__all__ = [
    "Budget",
    "Claim",
    "DUPLICATE_THRESHOLD",
    "LEDGER_SCHEMA",
    "ResearchLedger",
    "Source",
    "independence_key",
    "publisher_domain",
    "similarity",
]
