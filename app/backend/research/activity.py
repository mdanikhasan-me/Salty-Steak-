"""Truthful, bounded research activity for the native chat drawer.

The activity drawer is a public work journal, not a chain-of-thought viewer.
It reports observable stages and evidence state: what was searched, which
destinations were read or rejected, what checks ran, and how the cited answer
was assembled.  Stable ids let a live snapshot update in place instead of
adding a new row every time the UI polls.

Cooking may run for hours, so the journal deliberately keeps a fixed set of
stage rows plus bounded recent wave/source rows.  Evidence remains complete in
the research report and Sources view; the live DOM never grows with every page
ever visited.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_VISIBLE_WAVES = 8
MAX_VISIBLE_SOURCES = 24
MIN_RESEARCH_ACTIVITY_ROWS = 12

_POST_RETRIEVAL_PHASES = frozenset(
    {"retrieval_completed", "synthesizing", "drafting", "verifying", "completed"}
)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _duration(value: Any) -> str:
    seconds = _seconds(value)
    if seconds >= 3_600:
        hours = seconds / 3_600
        return f"{hours:g}-hour" if hours.is_integer() else f"{hours:.1f}-hour"
    if seconds >= 60:
        minutes = seconds / 60
        return f"{minutes:g}-minute" if minutes.is_integer() else f"{minutes:.1f}-minute"
    return f"{seconds:g}-second"


def _entry(
    entry_id: str,
    *,
    kind: str,
    label: str,
    detail: str,
    state: str,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "kind": kind,
        "label": str(label)[:120],
        "detail": " ".join(str(detail).split())[:400],
        "state": state,
    }


def _stage_state(
    *,
    done: bool,
    running: bool = False,
    final: bool = False,
    skipped: bool = False,
) -> str:
    if final and skipped:
        return "skipped"
    if done or final:
        return "completed"
    if running:
        return "running"
    return "pending"


def _past(final: bool, active: str, completed: str) -> str:
    return completed if final else active


def _waves(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(wave, sites=list(wave.get("sites") or []))
        for wave in snapshot.get("waves") or []
        if isinstance(wave, Mapping)
    ]


def _recent_sites(waves: Sequence[Mapping[str, Any]]) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    total = sum(len(wave.get("sites") or []) for wave in waves)
    recent: list[tuple[int, dict[str, Any]]] = []
    for wave_index in range(len(waves) - 1, -1, -1):
        sites = list(waves[wave_index].get("sites") or [])
        for site in reversed(sites):
            if isinstance(site, Mapping):
                recent.append((wave_index + 1, dict(site)))
            if len(recent) >= MAX_VISIBLE_SOURCES:
                break
        if len(recent) >= MAX_VISIBLE_SOURCES:
            break
    recent.reverse()
    return recent, max(0, total - len(recent))


def _source_entry(
    wave_index: int,
    site: Mapping[str, Any],
    ordinal: int,
    *,
    final: bool,
) -> dict[str, Any]:
    url = str(site.get("final_url") or site.get("url") or "")
    host = str(site.get("host") or "source").removeprefix("www.")
    state = str(site.get("state") or "found").casefold()
    identity_value = url or f"{host}:{ordinal}"
    identity = hashlib.sha256(
        f"{wave_index}:{identity_value}".encode("utf-8")
    ).hexdigest()[:12]
    title = str(site.get("title") or "").strip()
    page_type = str(site.get("page_type") or "").replace("_", " ").strip()
    reason = str(site.get("reason") or "").strip()
    if state in {"validated", "verified"}:
        label = f"Validated {host}"
        detail = title or f"Read a validated page in search wave {wave_index}."
        if page_type:
            detail = f"{detail} Page type: {page_type}."
        display_state = "completed"
    elif state in {"rejected", "failed", "skipped"}:
        label = f"Rejected {host}" if state != "skipped" else f"Skipped duplicate on {host}"
        detail = reason or "The destination was not accepted as evidence."
        display_state = "skipped" if state != "failed" else "failed"
    elif final:
        label = f"Did not use {host}"
        detail = reason or title or "The candidate did not become validated evidence before completion."
        display_state = "skipped"
    elif state in {"reading", "opening"}:
        label = f"Reading {host}"
        detail = title or f"Opening a candidate from search wave {wave_index}."
        display_state = "running"
    else:
        label = f"Queued {host}"
        detail = title or f"Candidate discovered in search wave {wave_index}."
        display_state = "pending"
    return _entry(
        f"research-source-{identity}",
        kind="research",
        label=label,
        detail=detail,
        state=display_state,
    )


def research_progress_snapshot(
    report: Mapping[str, Any],
    *,
    phase: str,
    generation_preview: Mapping[str, Any] | None = None,
    answer: str = "",
) -> dict[str, Any]:
    """Project a full report back into the bounded live progress contract."""

    snapshot = {
        "schema": "salty-steak-research-progress-v1",
        "phase": str(phase),
        "question": str(report.get("question") or ""),
        "profile": str(report.get("profile") or "verification"),
        "hard_ceiling_seconds": _seconds(report.get("hard_ceiling_seconds")),
        "elapsed_seconds": _seconds(
            report.get("elapsed_seconds")
            if report.get("elapsed_seconds") is not None
            else report.get("seconds")
        ),
        "source_count": _count(report.get("source_count")),
        "claim_count": _count(report.get("claim_count")),
        "corroborated": _count(report.get("corroborated")),
        "relevant_corroborated": _count(report.get("relevant_corroborated")),
        "independent_publisher_count": _count(
            report.get("independent_publisher_count")
        ),
        "relevant_publisher_count": _count(report.get("relevant_publisher_count")),
        "disputed": _count(report.get("disputed")),
        "relevant_disputed": _count(report.get("relevant_disputed")),
        "rejected_source_count": _count(
            report.get("rejected_source_count")
            if report.get("rejected_source_count") is not None
            else len(report.get("rejected_sources") or [])
        ),
        "validation_rounds_completed": _count(
            report.get("validation_rounds_completed")
        ),
        "validation_rounds_required": _count(
            report.get("validation_rounds_required")
        ),
        "minimum_independent_sources": _count(
            report.get("minimum_independent_sources")
        ),
        "coverage_target": _count(report.get("coverage_target")),
        "query_count": _count(
            report.get("query_count")
            if report.get("query_count") is not None
            else len(report.get("queries") or [])
        ),
        "evidence_sufficient": bool(report.get("evidence_sufficient")),
        "stop_reason": str(report.get("stop_reason") or ""),
        "status_target_version": str(report.get("status_target_version") or ""),
        "status_target_publisher_count": _count(
            report.get("status_target_publisher_count")
        ),
        "waves": _waves(report),
    }
    if generation_preview is not None:
        snapshot["generation_preview"] = dict(generation_preview)
    if answer:
        snapshot["answer_character_count"] = len(answer)
        snapshot["answer_word_count"] = len(answer.split())
        snapshot["citation_count"] = len(
            set(re.findall(r"https?://[^\s)\]>]+", answer))
        )
    return snapshot


def build_research_activity_journal(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a live or rewritten-completed public research journal."""

    phase = str(snapshot.get("phase") or "preparing").casefold()
    final = phase == "completed"
    post_retrieval = phase in _POST_RETRIEVAL_PHASES
    waves = _waves(snapshot)
    recent_sites, earlier_site_count = _recent_sites(waves)
    all_sites = sum(len(wave.get("sites") or []) for wave in waves)
    validated = max(
        _count(snapshot.get("source_count")),
        sum(_count(wave.get("verified")) for wave in waves),
    )
    rejected = max(
        _count(snapshot.get("rejected_source_count")),
        sum(_count(wave.get("rejected")) for wave in waves),
    )
    claims = _count(snapshot.get("claim_count"))
    corroborated = _count(
        snapshot.get("relevant_corroborated")
        if snapshot.get("relevant_corroborated") is not None
        else snapshot.get("corroborated")
    )
    disputed = _count(
        snapshot.get("relevant_disputed")
        if snapshot.get("relevant_disputed") is not None
        else snapshot.get("disputed")
    )
    publishers = _count(
        snapshot.get("relevant_publisher_count")
        if snapshot.get("relevant_publisher_count") is not None
        else snapshot.get("independent_publisher_count")
    )
    required_publishers = max(2, _count(snapshot.get("minimum_independent_sources")))
    required_rounds = _count(snapshot.get("validation_rounds_required"))
    completed_rounds = _count(snapshot.get("validation_rounds_completed"))
    coverage_target = _count(snapshot.get("coverage_target"))
    query_count = max(_count(snapshot.get("query_count")), len(waves))
    question = " ".join(str(snapshot.get("question") or "Research request").split())[:260]
    profile = str(snapshot.get("profile") or "verification").strip().capitalize()
    ceiling = _duration(snapshot.get("hard_ceiling_seconds"))
    evidence_sufficient = bool(snapshot.get("evidence_sufficient"))
    stop_reason = str(snapshot.get("stop_reason") or "")
    answer_characters = _count(snapshot.get("answer_character_count"))
    answer_words = _count(snapshot.get("answer_word_count"))
    citation_count = _count(snapshot.get("citation_count"))

    reading_now = any(
        str(site.get("state") or "").casefold() in {"reading", "opening"}
        for _wave_index, site in recent_sites
    )
    validation_wave_running = any(
        bool(wave.get("validation")) and str(wave.get("state") or "") != "done"
        for wave in waves
    )
    retrieval_started = bool(waves or all_sites or validated or rejected)
    synthesis_started = phase in {"synthesizing", "drafting", "verifying", "completed"}
    drafting_started = phase in {"drafting", "verifying", "completed"}
    verifying_started = phase in {"verifying", "completed"}

    entries: list[dict[str, Any]] = [
        _entry(
            "research-understanding",
            kind="thinking",
            label=_past(final, "Interpret the research question", "Interpreted the research question"),
            detail=question,
            state="completed",
        ),
        _entry(
            "research-budget",
            kind="thinking",
            label=_past(final, "Set the depth and hard ceiling", "Set the depth and hard ceiling"),
            detail=(
                f"{profile} research has a {ceiling} hard ceiling and may stop earlier "
                "only when its evidence gate is satisfied."
            ),
            state="completed",
        ),
        _entry(
            "research-standard",
            kind="verification",
            label=_past(final, "Define the evidence standard", "Defined the evidence standard"),
            detail=(
                f"Require at least {required_publishers} independent publishers, "
                f"{required_rounds} validation round{'s' if required_rounds != 1 else ''}, "
                f"and a coverage target of {coverage_target}."
            ),
            state="completed",
        ),
        _entry(
            "research-query-plan",
            kind="research",
            label=_past(final, "Plan focused search queries", "Ran focused search queries"),
            detail=f"{query_count} focused search wave{'s' if query_count != 1 else ''} planned or run so far.",
            state=_stage_state(
                done=bool(query_count and (len(waves) > 1 or post_retrieval)),
                running=bool(waves and not post_retrieval),
                final=final,
                skipped=not query_count,
            ),
        ),
        _entry(
            "research-discovery",
            kind="research",
            label=_past(final, "Discover candidate sources", "Discovered candidate sources"),
            detail=f"Found {all_sites} candidate destination{'s' if all_sites != 1 else ''} across the search waves.",
            state=_stage_state(
                done=bool(all_sites and post_retrieval),
                running=bool(waves and not post_retrieval),
                final=final,
                skipped=not all_sites,
            ),
        ),
    ]

    visible_waves = waves[-MAX_VISIBLE_WAVES:]
    hidden_waves = max(0, len(waves) - len(visible_waves))
    if hidden_waves:
        entries.append(
            _entry(
                "research-waves-earlier",
                kind="research",
                label=f"Summarized {hidden_waves} earlier search waves",
                detail="Earlier queries remain in the research report; the live drawer keeps the newest waves visible.",
                state="completed",
            )
        )
    wave_offset = len(waves) - len(visible_waves)
    for local_index, wave in enumerate(visible_waves, start=1):
        wave_index = wave_offset + local_index
        query = " ".join(str(wave.get("query") or "").split())[:260]
        wave_done = str(wave.get("state") or "") == "done" or final
        entries.append(
            _entry(
                f"research-wave-{wave_index}",
                kind="verification" if wave.get("validation") else "research",
                label=(
                    f"Validated with search wave {wave_index}"
                    if wave_done and wave.get("validation")
                    else f"Completed search wave {wave_index}"
                    if wave_done
                    else f"Validating with search wave {wave_index}"
                    if wave.get("validation")
                    else f"Searching wave {wave_index}"
                ),
                detail=(
                    f"{query} — {_count(wave.get('verified'))} validated, "
                    f"{_count(wave.get('rejected'))} rejected."
                ),
                state="completed" if wave_done else "running",
            )
        )

    if earlier_site_count:
        entries.append(
            _entry(
                "research-sources-earlier",
                kind="research",
                label=f"Summarized {earlier_site_count} earlier source checks",
                detail="The complete source ledger is retained; the live journal shows the most recent checks to stay responsive.",
                state="completed",
            )
        )
    entries.extend(
        _source_entry(wave_index, site, ordinal, final=final)
        for ordinal, (wave_index, site) in enumerate(recent_sites, start=1)
    )

    entries.extend(
        [
            _entry(
                "research-reading",
                kind="research",
                label=_past(final, "Read candidate pages", "Read candidate pages"),
                detail=f"Read {validated} validated page{'s' if validated != 1 else ''}.",
                state=_stage_state(
                    done=bool(validated and post_retrieval),
                    running=reading_now or bool(retrieval_started and not post_retrieval),
                    final=final,
                    skipped=not validated,
                ),
            ),
            _entry(
                "research-link-validation",
                kind="verification",
                label=_past(final, "Validate destinations and reject failures", "Validated destinations and rejected failures"),
                detail=f"Accepted {validated} readable pages and rejected {rejected} invalid, blocked, duplicate, or unusable links.",
                state=_stage_state(
                    done=post_retrieval,
                    running=retrieval_started and not post_retrieval,
                    final=final,
                ),
            ),
            _entry(
                "research-extraction",
                kind="thinking",
                label=_past(final, "Extract claims with provenance", "Extracted claims with provenance"),
                detail=f"Kept {claims} distinct claim{'s' if claims != 1 else ''} tied to their source pages.",
                state=_stage_state(
                    done=bool(claims and post_retrieval),
                    running=bool(validated and not post_retrieval),
                    final=final,
                    skipped=not claims,
                ),
            ),
            _entry(
                "research-independence",
                kind="verification",
                label=_past(final, "Check publisher independence", "Checked publisher independence"),
                detail=f"Found {publishers} independent publisher{'s' if publishers != 1 else ''}; the evidence gate requires {required_publishers}.",
                state=_stage_state(
                    done=publishers >= required_publishers or post_retrieval,
                    running=bool(validated and not post_retrieval),
                    final=final,
                ),
            ),
            _entry(
                "research-cross-check",
                kind="verification",
                label=_past(final, "Run independent validation rounds", "Ran independent validation rounds"),
                detail=f"Completed {completed_rounds} of {required_rounds} required validation rounds.",
                state=_stage_state(
                    done=completed_rounds >= required_rounds,
                    running=validation_wave_running,
                    final=final,
                    skipped=required_rounds == 0,
                ),
            ),
            _entry(
                "research-agreement",
                kind="thinking",
                label=_past(final, "Compare agreements between sources", "Compared agreements between sources"),
                detail=f"Found {corroborated} corroborated relevant finding{'s' if corroborated != 1 else ''}.",
                state=_stage_state(
                    done=post_retrieval,
                    running=bool(claims and not post_retrieval),
                    final=final,
                ),
            ),
            _entry(
                "research-disagreement",
                kind="thinking",
                label=_past(final, "Inspect contradictions and uncertainty", "Inspected contradictions and uncertainty"),
                detail=f"Found {disputed} material disagreement{'s' if disputed != 1 else ''}; none are hidden or averaged away.",
                state=_stage_state(
                    done=post_retrieval,
                    running=bool(claims and not post_retrieval),
                    final=final,
                ),
            ),
            _entry(
                "research-sufficiency",
                kind="verification",
                label=_past(final, "Judge whether the evidence is sufficient", "Judged the evidence sufficiency"),
                detail=(
                    f"Evidence gate {'passed' if evidence_sufficient else 'did not pass'}"
                    + (f"; stopping rule: {stop_reason.replace('_', ' ')}." if stop_reason else ".")
                ),
                state=_stage_state(
                    done=post_retrieval,
                    running=bool(retrieval_started and not post_retrieval),
                    final=final,
                ),
            ),
            _entry(
                "research-synthesis",
                kind="thinking",
                label=_past(final, "Synthesize only the supported findings", "Synthesized the supported findings"),
                detail="Turning the evidence ledger into a direct answer without filling gaps by guessing.",
                state=_stage_state(
                    done=phase in {"verifying", "completed"},
                    running=synthesis_started and phase not in {"verifying", "completed"},
                    final=final,
                    skipped=not claims,
                ),
            ),
            _entry(
                "research-citations",
                kind="verification",
                label=_past(final, "Verify claims and direct citations", "Verified claims and direct citations"),
                detail=(
                    f"Attached {citation_count} distinct direct citation{'s' if citation_count != 1 else ''} to the final answer."
                    if final
                    else "Checking that final claims point only to validated evidence URLs."
                ),
                state=_stage_state(
                    done=final,
                    running=verifying_started and not final,
                    final=final,
                    skipped=final and citation_count == 0,
                ),
            ),
            _entry(
                "research-answer",
                kind="writing",
                label=_past(final, "Write the final answer", "Wrote the final answer"),
                detail=(
                    f"Prepared {answer_words} words and {answer_characters} characters for the transcript."
                    if final
                    else "Drafting the answer separately from private model channels."
                ),
                state=_stage_state(
                    done=final,
                    running=drafting_started and not final,
                    final=final,
                ),
            ),
        ]
    )

    status_target = str(snapshot.get("status_target_version") or "").strip()
    if status_target:
        evidence_detail = (
            f"{_count(snapshot.get('status_target_publisher_count'))} publishers "
            f"confirmed {status_target} · {disputed} material disagreements · "
            f"{rejected} rejected {'link' if rejected == 1 else 'links'}"
        )
    else:
        evidence_detail = (
            f"{corroborated} corroborated findings · {disputed} disputed findings · "
            f"{rejected} rejected {'link' if rejected == 1 else 'links'}"
        )
    entries.append(
        _entry(
            "research-evidence",
            kind="verification",
            label=_past(final, "Compare and attribute the evidence", "Compared and attributed the evidence"),
            detail=evidence_detail,
            state=_stage_state(
                done=post_retrieval,
                running=bool(claims and not post_retrieval),
                final=final,
            ),
        )
    )

    for sequence, entry in enumerate(entries, start=1):
        entry["sequence"] = sequence
    if len(entries) < MIN_RESEARCH_ACTIVITY_ROWS:
        raise AssertionError("research activity journal fell below its public minimum")
    return entries


__all__ = [
    "MAX_VISIBLE_SOURCES",
    "MAX_VISIBLE_WAVES",
    "MIN_RESEARCH_ACTIVITY_ROWS",
    "build_research_activity_journal",
    "research_progress_snapshot",
]
