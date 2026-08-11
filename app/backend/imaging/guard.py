"""Salty Steak Native Desktop AI Platform — render brief integrity.

The check between deciding what to draw and spending minutes drawing it.

A user asked for a premium brand logo and received a photograph of a bus. The
image model was not the first thing to go wrong: by the time it ran, the brief
it was given no longer described a logo. Catching that costs microseconds;
missing it costs a full diffusion run and a user who has to explain themselves
again.

Two things are checked. Completeness — did the structuring pass drop the brand,
the deliverables, the forbidden motifs the user spelled out? And consistency —
does the brief still describe the thing that was asked for? Neither is a
classifier. Both are cheap comparisons against the user's own words, which is
the only source of truth available before generation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .brief import RenderBrief, extract_negatives, infer_image_type

GUARD_SCHEMA = "salty-steak-brief-guard-v1"



INCOMPATIBLE_TYPES = {
    "logo": {"photograph", "ui_mockup"},
    "icon": {"photograph", "ui_mockup"},
    "photograph": {"logo", "icon", "diagram"},
    "diagram": {"photograph"},
}



SUBJECT_DRIFT = re.compile(
    r"\b(bus|coach|truck|lorry|highway|motorway|car|train|aeroplane|airplane|"
    r"cow|cat|dog|horse|landscape|mountain|beach|sunset|forest)\b",
    re.IGNORECASE,
)






MIN_SHARED_TERMS = 1

STOPWORDS = frozenset(
    """
    a an the and or of to in on for is are was were be with from by as at it its
    this that these those i you we they my your our their please make create
    generate design want need should would could like using use
    """.split()
)


def _terms(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9']+", str(text or "").casefold())
        if len(word) > 2 and word not in STOPWORDS
    }


@dataclass
class BriefIssue:
    """One thing wrong with a brief, and what would fix it."""

    kind: str
    detail: str
    severity: str = "blocking"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass
class GuardReport:
    """Whether a brief may be sent to the image model."""

    issues: list[BriefIssue] = field(default_factory=list)

    @property
    def blocking(self) -> list[BriefIssue]:
        return [issue for issue in self.issues if issue.severity == "blocking"]

    @property
    def ok(self) -> bool:
        return not self.blocking

    def correction_request(self) -> str:
        """What to tell the orchestrator so it can fix the brief in one pass."""

        return "The render brief does not match the request. " + " ".join(
            f"{issue.kind}: {issue.detail}" for issue in self.blocking
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": GUARD_SCHEMA,
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def inspect(brief: RenderBrief, *, request: str | None = None) -> GuardReport:
    """Check a brief against the request it is supposed to represent."""

    report = GuardReport()
    original = str(request if request is not None else brief.original_request or "")
    requested_terms = _terms(original)

    if not brief.subject:
        report.issues.append(
            BriefIssue("missing_subject", "The brief does not say what to draw.")
        )
        return report


    wanted_type = infer_image_type(original)
    if wanted_type != "other" and brief.image_type != wanted_type:
        if brief.image_type in INCOMPATIBLE_TYPES.get(wanted_type, set()):
            report.issues.append(
                BriefIssue(
                    "wrong_image_type",
                    f"The request asks for a {wanted_type.replace('_', ' ')} but "
                    f"the brief describes a {brief.image_type.replace('_', ' ')}.",
                )
            )







    if requested_terms:



        brief_terms = _terms(f"{brief.subject} {brief.brand}")
        shared = len(requested_terms & brief_terms)
        if brief_terms and shared < MIN_SHARED_TERMS:
            report.issues.append(
                BriefIssue(
                    "subject_drift",
                    f"The brief subject {brief.subject!r} shares almost nothing "
                    "with what the user asked for.",
                )
            )


    drifted = SUBJECT_DRIFT.search(brief.subject)
    if drifted and not SUBJECT_DRIFT.search(original):
        report.issues.append(
            BriefIssue(
                "unrelated_subject",
                f"The brief introduces {drifted.group(0)!r}, which the user "
                "never mentioned.",
            )
        )


    for name in _proper_nouns(original):
        haystack = f"{brief.subject} {brief.brand} {brief.goal} {brief.text_content}".casefold()
        if name.casefold() not in haystack:
            report.issues.append(
                BriefIssue(
                    "missing_named_entity",
                    f"{name!r} appears in the request but not in the brief.",
                )
            )
            break


    demanded = extract_negatives(original)
    if demanded:
        kept = {item.casefold() for item in brief.negative_constraints}
        lost = [
            item
            for item in demanded
            if not any(item.casefold() in entry or entry in item.casefold() for entry in kept)
        ]
        if len(lost) > len(demanded) / 2:
            report.issues.append(
                BriefIssue(
                    "lost_negative_constraints",
                    f"{len(lost)} of {len(demanded)} things the user ruled out "
                    "are missing from the brief.",
                )
            )

    return report


def _proper_nouns(text: str) -> list[str]:
    """Capitalised words that are probably names.

    Its only job is to notice that a brand name present in the request has
    vanished from the brief. A name a request is actually about gets repeated;
    a capitalised section heading like "Visual direction" is written once. So
    a candidate has to appear more than once, which keeps the check from
    firing on ordinary prose.
    """

    raw = str(text or "")
    words = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", raw)
    common = {
        "The", "This", "That", "Please", "Create", "Generate", "Design", "Make",
        "Must", "Should", "Avoid", "Include", "With", "Without", "Also", "Then",
        "Deliverables", "Requirements", "Style", "Colour", "Color", "Symbol",
        "Version", "Standalone", "Horizontal", "Monochrome", "Forbidden",
        "Visual", "Never", "Plain", "Boilerplate", "One", "Direction",
    }
    repeated = [
        word
        for word in dict.fromkeys(words)
        if word not in common
        and len(re.findall(rf"\b{re.escape(word)}\b", raw, re.IGNORECASE)) > 1
    ]
    return repeated[:3]


__all__ = ["BriefIssue", "GUARD_SCHEMA", "GuardReport", "inspect"]
