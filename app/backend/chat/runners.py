"""Salty Steak Native Desktop AI Platform — live runners for decided work.

Connects the three decisions that were recognised but not yet carried out:
one action, a plan, and research.

Everything here reuses machinery that already exists and is already proven —
the automation broker, AgentLoop, Plan validation, WorkflowEngine, the research
ledger — because a second implementation of any of them would be a second thing
to keep correct. What was missing was only the wiring from a live chat turn to
those runners, and the compact packets they need.

Base Steak stays the single brain. Planning, replanning and the agent loop all
go through the one inference path; there is no planner model.
"""

from __future__ import annotations

import json
import time
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..automation.capability_registry import (
    FILE_OPERATIONS,
    FILES_CAPABILITY,
    get_capability_descriptor,
)
from ..automation.credentials import redact
from ..automation.routing import apply_argument_aliases, resolve_execution

from ..research import Budget, ResearchLoop
from ..research.activity import (
    build_research_activity_journal,
    research_progress_snapshot,
)
from ..research.ledger import independence_key
from ..workflow import Executor, WorkflowEngine, build_plan
from ..workflow.plan import Plan, PlanError
from .orchestrator import strip_reasoning

RUNNER_SCHEMA = "salty-steak-live-runner-v1"



MAX_LIVE_NODES = 40
MAX_REPLAN_OBSERVATIONS = 12
MAX_REPLAN_ITEMS = 20
MAX_REPLAN_FIELDS = 30
MAX_REPLAN_TEXT = 800
MAX_REPLAN_DEPTH = 5


class PlanRejected(ValueError):
    """Raised when a model-produced plan cannot be trusted to run."""


def _nodes_in_the_right_slots(
    payload: Mapping[str, Any],
    capabilities: Sequence[str],
    connectors: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Put a granted capability the model wrote as a connector back in its slot.

    Asked to open Notepad, focus its window and capture the screen, the live
    model produced exactly the right three steps and wrote each one as
    ``"connector": "application.launch"``. The plan was rejected whole for
    naming a service that is not connected, which was true and useless: it had
    named a capability that *is* granted, in the wrong field, because the one
    worked example in the routing instruction is a connector node.

    A name that is a granted capability can only mean the capability — the two
    namespaces do not overlap — so it is moved rather than refused. Anything
    unrecognised is left exactly where the model put it and still fails
    validation.
    """

    granted = set(capabilities)
    configured = set(connectors)
    nodes = payload.get("nodes")
    if not granted or not isinstance(nodes, list):
        return payload




    CANDIDATE_FIELDS = ("connector", "node", "id", "name", "tool", "action", "target")

    moved: list[Any] = []
    for entry in nodes:
        if not isinstance(entry, Mapping):
            moved.append(entry)
            continue
        alternate_key = entry.get("key")
        if isinstance(alternate_key, Mapping):
            named = {
                str(alternate_key.get(field) or "")
                for field in ("name", "capability", "tool")
                if str(alternate_key.get(field) or "") in granted
            }
            if len(named) == 1:
                corrected_key = dict(entry)
                corrected_key["capability"] = named.pop()
                if (
                    "arguments" not in corrected_key
                    and isinstance(corrected_key.get("key_args"), Mapping)
                ):
                    corrected_key["arguments"] = dict(corrected_key["key_args"])
                corrected_key.pop("key", None)
                corrected_key.pop("key_args", None)
                moved.append(corrected_key)
                continue





        if str(entry.get("capability") or "") in granted:
            moved.append(entry)
            continue

        corrected = dict(entry)
        corrected.pop("capability", None)


        if str(corrected.get("connector") or "") in granted:
            corrected["capability"] = corrected.pop("connector")
            corrected.pop("operation", None)
            moved.append(corrected)
            continue









        if str(corrected.get("connector") or "") not in configured:
            found = next(
                (
                    str(corrected[field])
                    for field in CANDIDATE_FIELDS
                    if str(corrected.get(field) or "") in granted
                ),
                "",
            )
            if found:
                corrected["capability"] = found
                arguments = dict(corrected.get("arguments") or {})
                verb = str(entry.get("capability") or "").strip().casefold()
                if found == "window.control" and "action" not in arguments:
                    action_alias = {
                        "bring_to_front": "focus",
                        "focus_window": "focus",
                        "close_window": "close",
                        "list_windows": "list",
                    }.get(verb)
                    if action_alias:
                        arguments["action"] = action_alias
                elif found == "browser.control" and "command" not in arguments:
                    command_alias = {
                        "browser.launch": "open_url",
                        "browser.launch_url": "open_url",
                        "launch_url": "open_url",
                        "navigate_to_url": "open_url",
                    }.get(verb)
                    if command_alias:
                        arguments["command"] = command_alias
                elif found == "ui.automation" and "command" not in arguments:
                    command_alias = {
                        "type_text": "set_value",
                        "click_button": "invoke",
                        "click_element": "invoke",
                        "find_window_by_title": "find_control",
                    }.get(verb)
                    if command_alias:
                        arguments["command"] = command_alias
                corrected["arguments"] = arguments
                corrected.pop("connector", None)
                corrected.pop("operation", None)
                moved.append(corrected)
                continue





        if (
            FILES_CAPABILITY in granted
            and str(corrected.get("connector") or "") not in configured
        ):
            arguments, _notes = apply_argument_aliases(
                FILES_CAPABILITY, dict(corrected.get("arguments") or {})
            )
            if _looks_like_a_path(arguments.get("path")):
                operation = str(
                    arguments.get("operation") or corrected.get("operation") or ""
                ).strip().casefold()
                if operation in FILE_OPERATIONS:
                    arguments["operation"] = operation
                    corrected["capability"] = FILES_CAPABILITY
                    corrected["arguments"] = arguments
                    corrected.pop("connector", None)
                    corrected.pop("operation", None)
                    moved.append(corrected)
                    continue











        if FILES_CAPABILITY in granted and _looks_like_a_path(
            corrected.get("connector")
        ):
            arguments = dict(corrected.get("arguments") or {})
            arguments.setdefault("path", str(corrected["connector"]))
            operation = str(corrected.get("operation") or "").strip().casefold()
            if operation in FILE_OPERATIONS:
                arguments.setdefault("operation", operation)
            corrected["capability"] = FILES_CAPABILITY
            corrected["arguments"] = arguments
            corrected.pop("connector", None)
            corrected.pop("operation", None)
            moved.append(corrected)
            continue

        moved.append(entry)
    return {**payload, "nodes": moved}


def _looks_like_a_path(value: Any) -> bool:
    """Whether a value is a place on this computer rather than a service name.

    Structural, not a list of drive letters: a service name has no separators
    and no drive, and a path has one or the other.
    """

    text = str(value or "").strip()
    if not text or "://" in text:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return text.startswith(("\\\\", "/", "~")) or ("\\" in text and " " not in text[:3])


def validate_plan(
    payload: Mapping[str, Any],
    *,
    goal: str,
    capabilities: Sequence[str],
    connectors: Sequence[str],
) -> Plan:
    """Check a model-authored plan completely before any of it executes.

    Structure is validated by ``build_plan`` — ids, dependencies, cycles. What
    is added here is authority: a plan may only name capabilities that are
    actually granted and connectors that are actually configured. A plan that
    reaches for something it cannot have is rejected whole, because letting it
    start and fail half-way leaves the user with partial side effects.
    """

    try:
        plan = build_plan(
            _nodes_in_the_right_slots(payload, capabilities, connectors), goal=goal
        )
    except PlanError as error:
        raise PlanRejected(str(error)) from error

    if len(plan.nodes) > MAX_LIVE_NODES:
        raise PlanRejected(
            f"That plan has {len(plan.nodes)} steps; {MAX_LIVE_NODES} is the limit."
        )

    granted = set(capabilities)
    configured = set(connectors)
    for node in plan.nodes:
        if node.capability and node.capability not in granted:
            raise PlanRejected(
                f"Step {node.node_id!r} needs {node.capability!r}, which is not "
                "enabled."
            )
        if node.capability:
            route = resolve_execution(node.capability, node.arguments, capabilities)
            try:
                descriptor = get_capability_descriptor(node.capability)
            except KeyError:


                descriptor = None
            if descriptor is None:
                continue
            missing = [
                name
                for name in descriptor.required_arguments
                if name not in route.arguments
            ]
            if missing:
                raise PlanRejected(
                    f"Step {node.node_id!r} needs {node.capability!r} arguments: "
                    + ", ".join(missing)
                )
        if node.connector and node.connector not in configured:
            raise PlanRejected(
                f"Step {node.node_id!r} needs the {node.connector!r} service, "
                "which is not connected."
            )
    return plan


def _bounded_replan_value(value: Any, *, depth: int = 0) -> Any:
    value = redact(value)
    if depth >= MAX_REPLAN_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_replan_value(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_REPLAN_FIELDS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_replan_value(item, depth=depth + 1)
            for item in list(value)[:MAX_REPLAN_ITEMS]
        ]
    if isinstance(value, str):
        return value[:MAX_REPLAN_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_REPLAN_TEXT]


def replan_packet(
    plan: Plan, outcome: Any, *, world: Mapping[str, Any] | None = None
) -> str:
    """The compact brief handed back to Base Steak when a plan needs repair.

    Deliberately small. Resending the conversation and every tool event would
    put the whole failure history through prefill on each replan, which is both
    slow and worse: the model reads the noise instead of the failure.
    """

    packet = {
        "goal": plan.goal,
        "failed_node": outcome.node_id,
        "failure": outcome.cause,
        "error": (outcome.detail or {}).get("error", "")[:400],
        "completed": [
            node.node_id for node in plan.nodes if node.state == "completed"
        ],
        "remaining": [
            node.node_id
            for node in plan.nodes
            if node.state not in {"completed", "failed", "skipped"}
        ],



        "observations": {
            node_id: _bounded_replan_value(value)
            for node_id, value in list(plan.observations.items())[
                -MAX_REPLAN_OBSERVATIONS:
            ]
        },
        "known": dict(world or {}),
    }
    return (
        "A step failed and retrying it will not help. Produce a corrected plan "
        "as one JSON object with the same shape. Keep the completed steps as "
        "they are and change only what is left. Use {$ref: "
        '"node.output.field"} for values supplied by existing observations; '
        "do not copy or guess them.\n" + json.dumps(packet, default=str)
    )


def _describe_effect(route: Any, result: Mapping[str, Any]) -> str:
    """Say what actually happened when the model gave no reason of its own.

    "Done." is true and useless: it does not tell the user which application
    opened, which window moved, or where a screenshot went, so there is nothing
    to check the claim against. The broker already reports each of those, so
    the sentence is built from the result rather than from the request.
    """

    capability = str(getattr(route, "capability", "") or "")
    if capability == "application.launch":
        target = str(result.get("target") or "").strip()
        return f"I opened {target}." if target else "I opened that for you."
    if capability == "window.control":
        window = result.get("window") or {}
        title = str(window.get("title") or "").strip()
        action = str(result.get("action") or "").strip()
        if action == "list":
            return f"There are {int(result.get('window_count') or 0)} open windows."
        if title:
            return f"I brought {title} to the front." if action == "focus" else f"I closed {title}."
    if capability == "screen.capture":
        artifact = result.get("artifact") or {}
        path = str(artifact.get("path") or "").strip()
        if path:
            return f"I captured the screen to {path}."
    if capability == "terminal.execute":
        return f"The command finished with exit code {result.get('exit_code')}."
    if capability == "input.control":
        action = str(result.get("action") or "").strip()
        if action:
            return f"I sent {action.replace('_', ' ')} to the focused window."
    return "Done."






_PROCESS_PHRASES = (
    "i read ",
    "i searched",
    "distinct findings",
    "i stopped because",
    "source budget",
)




FINALISER_BASE_RULES = (
    "Answer the user's question using only the findings below. Write the "
    "answer only where the cited text supports it. A validated source means "
    "the page was readable, not that every claim is true. Repeated text across "
    "publishers can be copied; source counts and heuristic confidence are not "
    "truth probabilities. Distinguish single-source assertions from independent "
    "support and conflicting evidence. "
    "answer itself in plain prose — do not describe the search, do not count "
    "sources, do not mention findings or claims. "
    "Answer only what was asked; do not turn a narrow status question into a "
    "list of adjacent features. Prefer fewer, stronger sources over a "
    "long bibliography. Use no more than three source URLs unless the user asks "
    "for more. Keep the answer under 140 words unless the user asks for detail. "
    "For a current/latest version question, use the highest matching patch "
    "version in the settled findings; older release notes are historical. "
    "Do not add support-lifecycle or end-of-life dates unless the question asks. "
    "Cite with inline Markdown links in the form [source](exact URL); "
    "do not use numbered footnotes or bare URLs. "
    "Never combine parts of two different findings: if one finding names a "
    "thing and another names a number, they are not about each other unless a "
    "single finding says so. "
    "Do not estimate, round or fill a gap. If the findings do not give "
    "something that was asked for, say that they do not. "
    "If the findings disagree, say what the disagreement is. If they do not "
    "answer the question, say so plainly. "
    "If the user asks you to identify disagreements and no supplied finding is "
    "marked disputed, say that no material disagreement appeared in the "
    "validated evidence. "
    "Describe a disagreement only when both opposing findings and their source "
    "records are supplied. For time-sensitive status questions, compare dates "
    "and versions: an older page describing an earlier state is historical, "
    "not automatically a current contradiction. Prefer primary or official "
    "evidence for canonical status and use independent publishers to check it. "
    "When the user requests an independent comparison and the findings provide "
    "different publisher_domain values, cite at least two organisationally "
    "independent publishers. Subdomains of one publisher are not independent. "
    "Cite factual statements with Markdown links from that finding's sources. "
    "Use only the exact source URLs supplied; never invent, repair, or guess a link."
)






FINALISER_COMMERCE_RULES = (
    " A figure a finding gives as a range across a page or a catalogue is a "
    "range and must be reported as one, never as the price of a particular "
    "item. "
    "A price for a named item may only come from verified_products, which is "
    "the only place a product, its price and its seller were bound together "
    "by one page. Quote each seller's price separately and give its link; "
    "never average them. "
    "A finding marked as a catalogue page describes a whole shop, not any item "
    "on it. "
    "Write each price exactly as verified_products gives it, character for "
    "character. Never change its currency mark for another and never convert "
    "between currencies. "
    "Stock is evidence, not an assumption. A product whose stock is 'unknown' "
    "has NOT been confirmed available and may never be called in stock, "
    "available, or the cheapest one currently in stock — say its availability "
    "was not confirmed. "
    "Obey evidence_limits and every user constraint: budget, region, capacity, "
    "condition and seller restrictions. 'Under' and 'less than' are strict: "
    "an equal price does not qualify; 'up to' includes the limit. Keep rejected "
    "offers separate from recommendations. If none qualify, say none were confirmed; "
    "do not invent an offer or claim the whole market was exhausted."
)


def _has_priced_evidence(observations: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any observation actually bound an offer.

    A page can be observed without a price being established on it, and an
    observation with no price is not commerce evidence.
    """

    return any(
        str(item.get("price_display") or item.get("price") or "").strip()
        for item in observations
        if isinstance(item, Mapping)
    )


def finaliser_instruction(*, observations: Sequence[Mapping[str, Any]]) -> str:
    """The rules the answering pass is held to, for the evidence it actually has.

    Evidence-driven, never keyword-driven. The commerce block appears because
    priced observations exist, not because the question contained a word — a
    word list would be wrong about somebody's question sooner or later, and
    would reintroduce exactly the contamination it was meant to prevent.
    """

    if _has_priced_evidence(observations):
        return FINALISER_BASE_RULES + FINALISER_COMMERCE_RULES
    return FINALISER_BASE_RULES


def finaliser_payload(
    *,
    question: str,
    observations: Sequence[Mapping[str, Any]],
    evidence_limits: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """What the answering pass is shown.

    An empty ``verified_products`` list is not neutral: a shopping-shaped field
    still tells the model the answer is about shopping, so it is left out
    entirely rather than sent empty.
    """

    payload: dict[str, Any] = {"question": question, "findings": list(findings)}
    if not _has_priced_evidence(observations):
        return payload
    payload["verified_products"] = [
        {
            "product": item.get("product"),


            "price": item.get("price_display") or item.get("price"),
            "currency": item.get("currency_code") or item.get("currency"),
            "seller": item.get("seller"),
            "stock": item.get("stock") or "unknown",
            "variant": item.get("variant") or "",
            "url": item.get("url"),
        }
        for item in observations
    ]
    payload["evidence_limits"] = dict(evidence_limits)
    return payload


def research_review_evidence(question: str, report: Mapping[str, Any]) -> dict[str, Any]:
    """Keep source identities and answer evidence without replaying the ledger."""
    observations = list(report.get("observations") or [])
    claims = _select_finaliser_findings(question, list(report.get("claims") or []))[:8]
    sources = {str(s.get("source") or s.get("id") or ""): s
               for s in report.get("sources") or [] if isinstance(s, Mapping)}
    findings = []
    for claim in claims:
        evidence = claim.get("evidence") or [sources[key] for key in claim.get("sources", [])
                                            if isinstance(key, str) and key in sources]
        findings.append({"text": str(claim.get("text") or "")[:700],
            "sources": [s.get("url") for s in evidence if isinstance(s, Mapping)],
            "disputed": bool(claim.get("disputed"))})
    packet = finaliser_payload(question=question, observations=observations,
        evidence_limits=report.get("evidence_limits") or _evidence_limits(observations),
        findings=[] if _has_priced_evidence(observations) else findings)
    packet['source_urls'] = [s.get('url') for s in report.get('sources') or [] if isinstance(s, Mapping)]
    packet['scope'] = 'Bounded retrieved evidence; readable pages do not prove exhaustive coverage.'
    return packet


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)", re.IGNORECASE)
_FOOTNOTE_DEFINITION = re.compile(
    r"(?m)^\[\^([^\]]+)\]:\s*(https?://\S+)\s*$",
    re.IGNORECASE,
)
_FOOTNOTE_MARKER = re.compile(r"\[\^([^\]]+)\]")

_QUESTION_NOISE = frozenset(
    """
    a about an and answer any are at citations cite concise direct disagreement disagreements
    fewer find findings for from give identify in independent information is least more most
    official one prefer provide research result results source sources technical
    that the this to using validate validated validation verification verify with
    """.split()
)
_DIRECT_ANSWER_TERMS = frozenset(
    {
        "available",
        "availability",
        "current",
        "invalid",
        "latest",
        "release",
        "released",
        "stable",
        "status",
        "valid",
        "version",
    }
)
_STATUS_QUESTION_TERMS = frozenset({"current", "latest", "stable", "status", "version"})
_STATUS_FINDING_TERMS = frozenset(
    {"available", "current", "latest", "maintenance", "newest", "released", "stable"}
)
_VERSION_TOKEN = re.compile(r"\b\d+(?:\.\d+){1,3}\b(?![\d.]|\s*(?:%|percent\b|per\s+cent\b))", re.I)
_RESEARCH_BOILERPLATE = (
    "produced no results", "returned no results",
    "search results", "no results for:", "search documentation search changelog",
    "call to action",
    "enjoy the new release",
    "for more details",
    "more resources",
    "online documentation",
    "report bugs",
    "see also",
    "thanks to all",
)


def _is_research_boilerplate(value: str) -> bool:
    """Recognise navigation/document metadata that resembles a status answer."""

    text = " ".join(str(value or "").casefold().split())
    if any(phrase in text for phrase in _RESEARCH_BOILERPLATE):
        return True
    if "page source" in text or "contents abstract" in text:
        return True


    return (
        "status:" in text
        and ("type:" in text or "typ:" in text)
        and ("title:" in text or "pep index" in text)
    )


def _question_terms(value: str) -> set[str]:
    # A supplied URL identifies a source, not the answer's subject. Ranking its
    # hostname/path words promoted bibliographies over the requested rule.
    value = re.sub(r'https?://\S+', ' ', str(value or ''), flags=re.I)
    return {
        word
        for word in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(word) > 1 and word not in _QUESTION_NOISE and word not in {'read','search','public','web','url','urls'}
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return ()


def _latest_status_version(
    question: str,
    findings: Sequence[Mapping[str, Any]],
) -> str:
    """Newest settled version matching a series explicitly named by the user."""

    requested = [_version_tuple(value) for value in _VERSION_TOKEN.findall(question)]
    subject_terms = _question_terms(question) - _DIRECT_ANSWER_TERMS - {
        "what", "which", "how", "does", "please", "software", "number", "series",
    }
    subject_terms = {term for term in subject_terms if not term.isdigit()}
    versions: set[str] = set()
    for finding in findings:
        if finding.get("disputed") or finding.get("validated") is False:
            continue
        text = str(finding.get("text") or "")
        if not re.search(r"\b(?:release|released|version|patch|maintenance)\b", text, re.I):
            continue
        if not subject_terms:
            continue
        subject_pattern = r"\b(?:" + "|".join(re.escape(term) for term in subject_terms) + r")\s+(?:(?:version|release|v)\s*)?$"
        for match in _VERSION_TOKEN.finditer(text):
            value = match.group()
            # Evidence for a product somewhere in the finding cannot attribute
            # every number to that product. Require a direct subject/version
            # phrase and an adjacent release statement, otherwise leave the
            # answer to supported synthesis instead of fabricating an override.
            before = text[:match.start()]
            after = text[match.end():]
            if not re.search(subject_pattern, before, re.I):
                continue
            if not re.match(
                r"\s*(?:(?:is|was|has|been|the|a|an|now|current|latest|newest|stable|"
                r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
                r"maintenance|security|patch)\s+){0,7}(?:release[ds]?|version)\b",
                after, re.I,
            ):
                continue
            parts = _version_tuple(value)
            if requested and not any(
                parts[: len(prefix)] == prefix for prefix in requested if prefix
            ):
                continue
            versions.add(value)
    return max(versions, key=_version_tuple, default="")


def _status_fallback_answer(
    question: str,
    findings: Sequence[Mapping[str, Any]],
) -> str:
    """A claim-bounded answer when synthesis chooses an older patch release."""

    latest = _latest_status_version(question, findings)
    if not latest:
        return ""
    requested = _VERSION_TOKEN.findall(question)
    series = requested[0] if requested else ".".join(latest.split(".")[:2])
    descriptor = "maintenance " if any(
        latest in str(item.get("text") or "")
        and "maintenance" in str(item.get("text") or "").casefold()
        for item in findings
        if not item.get("disputed")
    ) else ""
    return (
        f"{latest} is the latest validated {descriptor}release in the "
        f"requested {series} series."
    )


def _status_answer_uses_latest(
    question: str,
    answer: str,
    findings: Sequence[Mapping[str, Any]],
) -> bool:
    if not _question_terms(question) & _STATUS_QUESTION_TERMS:
        return True
    latest = _latest_status_version(question, findings)
    return not latest or latest in _VERSION_TOKEN.findall(str(answer or ""))


def _trim_unrequested_status_scope(question: str, answer: str) -> str:
    """Keep a patch-status answer from inventing one undifferentiated support phase."""

    if not _question_terms(question) & _STATUS_QUESTION_TERMS:
        return str(answer or "").strip()
    if re.search(
        r"\b(?:active support|end[- ]of[- ]life|eol|lifecycle|security support|support until|support through)\b",
        str(question or ""),
        flags=re.IGNORECASE,
    ):
        return str(answer or "").strip()
    trimmed = re.sub(
        r",?\s+with\s+(?:active\s+)?support\s+"
        r"(?:(?:continuing|extending|lasting)\s+)?(?:through|until)\s+[^.]+(?=\.)",
        "",
        str(answer or ""),
        flags=re.IGNORECASE,
    )
    return "\n".join(" ".join(line.split()) for line in trimmed.splitlines()).strip()


def _finding_rank(
    question_terms: set[str], finding: Mapping[str, Any]
) -> tuple[Any, ...]:
    text = str(finding.get("text") or "")
    text_terms = _question_terms(text)
    overlap = len(question_terms & text_terms)
    direct_overlap = len(question_terms & text_terms & _DIRECT_ANSWER_TERMS)
    coverage = overlap / max(1, len(question_terms))
    boilerplate = _is_research_boilerplate(text)
    return (
        not boilerplate,
        direct_overlap,
        overlap,
        coverage,
        int(finding.get("independent_source_count") or 0),
        int(finding.get("source_count") or 0),
        float(finding.get("confidence") or 0.0),
        -len(text_terms),
    )


def _is_status_finding(question_terms: set[str], finding: Mapping[str, Any]) -> bool:
    """Whether a finding answers a narrow current/stable/version status ask."""

    if not question_terms & _STATUS_QUESTION_TERMS:
        return True
    text_terms = _question_terms(str(finding.get("text") or ""))
    subject_terms = {
        term
        for term in question_terms - _STATUS_QUESTION_TERMS - _DIRECT_ANSWER_TERMS
        if not term.isdigit()
    }
    subject_matches = not subject_terms or bool(text_terms & subject_terms)
    return subject_matches and bool(text_terms & _STATUS_FINDING_TERMS)


def _select_finaliser_findings(
    question: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    limit: int = 8,
    disputed_limit: int = 1,
) -> list[Mapping[str, Any]]:
    """Keep the evidence packet focused while preserving relevant disputes."""

    terms = _question_terms(question)
    records = [item for item in findings if isinstance(item, Mapping)]
    settled_records = [item for item in records if not item.get("disputed")]
    status_records = [
        item for item in settled_records if _is_status_finding(terms, item)
    ]
    if status_records:
        settled_records = status_records

    def settled_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
        rank = _finding_rank(terms, item)
        version = _version_tuple(_latest_status_version(question, [item]))
        requested_type = terms & {"documentation", "manual", "specification", "reference"}
        source_type_match = any(
            requested_type & _question_terms(str(source.get("title") or ""))
            for source in item.get("evidence", [])
            if isinstance(source, Mapping)
        )
        from urllib.parse import urlsplit
        primary_requested=bool(terms & {'original','official'})
        from ..research.query import requested_urls
        supplied={url.rstrip('/') for url in requested_urls(question)}
        subject_host_match=primary_requested and any(
            str(source.get('url') or '').rstrip('/') in supplied or
            bool(terms & set((urlsplit(str(source.get('url') or '')).hostname or '').split('.')))
            for source in item.get('evidence',[]) if isinstance(source,Mapping))
        return rank[:1] + (source_type_match,subject_host_match) + rank[1:2] + (version,) + rank[2:]

    settled = sorted(
        settled_records,
        key=settled_key,
        reverse=True,
    )
    disputed = sorted(
        (item for item in records if item.get("disputed")),
        key=lambda item: _finding_rank(terms, item),
        reverse=True,
    )
    by_id = {
        str(item.get("claim") or ""): item
        for item in records
        if str(item.get("claim") or "")
    }
    dispute_pairs: list[Mapping[str, Any]] = []
    paired: set[str] = set()
    direct_terms_requested = len(terms & _DIRECT_ANSWER_TERMS)
    for item in disputed:
        rank = _finding_rank(terms, item)
        if not rank[0] or rank[2] <= 0:
            continue
        if direct_terms_requested > 1 and rank[1] < 2:
            continue
        opponents = [
            by_id.get(str(identifier)) for identifier in item.get("contradicts") or []
        ]
        opponents = [
            opponent
            for opponent in opponents
            if opponent is not None
            and _finding_rank(terms, opponent)[0]
            and _finding_rank(terms, opponent)[2] > 0
            and not (
                direct_terms_requested > 1
                and _finding_rank(terms, opponent)[1] < 2
            )
        ]
        if not opponents:
            continue
        opponent = max(opponents, key=lambda value: _finding_rank(terms, value))
        pair_key = "|".join(
            sorted(
                (
                    str(item.get("claim") or id(item)),
                    str(opponent.get("claim") or id(opponent)),
                )
            )
        )
        if pair_key in paired:
            continue
        paired.add(pair_key)
        dispute_pairs.extend((item, opponent))
        if len(paired) >= max(0, int(disputed_limit)):
            break
    settled_limit = max(0, int(limit) - len(dispute_pairs))
    # Reserve most of the packet for the strongest answers before diversifying.
    # Otherwise six marginal publishers can displace a second crucial sentence
    # from the primary paper, leaving the model unable to answer a subquestion.
    core_count = min(settled_limit, max(1, settled_limit - 2))
    diverse: list[Mapping[str, Any]] = list(settled[:core_count])
    seen_publishers: set[str] = {
        str(source.get("independence_key") or source.get("domain") or source.get("url") or "")
        for item in diverse for source in item.get("evidence", []) if isinstance(source, Mapping)
    } - {""}
    for item in settled:
        if len(diverse) >= settled_limit:
            break
        if not _finding_rank(terms, item)[0] or _finding_rank(terms, item)[2] == 0:
            continue
        publishers = {
            str(source.get("independence_key") or source.get("domain") or source.get("url") or "")
            for source in item.get("evidence", []) if isinstance(source, Mapping)
        } - {""}
        if item not in diverse and publishers - seen_publishers:
            diverse.append(item)
            seen_publishers.update(publishers)
        if len(diverse) >= settled_limit:
            break
    selected_settled = (diverse + [item for item in settled if item not in diverse])[:settled_limit]
    selected = selected_settled + dispute_pairs
    return selected or records[: max(1, int(limit))]


def _citation_key(url: str) -> str:
    return str(url or "").split("#", 1)[0].rstrip("/.,;:)").casefold()


def _validated_source_links(
    findings: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[Mapping[str, Any]] = (),
    question: str = "",
    limit: int = 3,
) -> list[tuple[str, str]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_groups: list[Sequence[Mapping[str, Any]]] = [sources]
    source_groups.extend(
        finding.get("evidence") or finding.get("sources") or []
        for finding in findings
        if isinstance(finding, Mapping)
    )
    for group in source_groups:
        for source in group:
            if not isinstance(source, Mapping):
                continue
            if str(source.get("validation") or "validated") != "validated":
                continue
            url = str(source.get("url") or "").strip()
            key = _citation_key(url)
            if not url.casefold().startswith(("http://", "https://")) or key in seen:
                continue
            seen.add(key)
            title = " ".join(str(source.get("title") or source.get("domain") or url).split())
            title = title.replace("[", "").replace("]", "")[:100] or url
            publisher = str(source.get("independence_key") or "").strip()
            if not publisher:
                publisher = independence_key(url, title=title)
            title_terms = _question_terms(f"{title} {url}")
            question_terms = _question_terms(question)
            publisher_terms = _question_terms(publisher)
            candidates.append(
                {
                    "title": title,
                    "url": url,
                    "publisher": publisher,
                    "rank": (
                        bool(publisher),
                        len(publisher_terms & question_terms),
                        len(title_terms & question_terms & _DIRECT_ANSWER_TERMS),
                        len(title_terms & question_terms),
                        -len(candidates),
                        int(source.get("content_characters") or 0),
                    ),
                }
            )

    candidates.sort(key=lambda item: item["rank"], reverse=True)
    editorial = [item for item in candidates if item["publisher"]]
    pool = editorial or candidates




    links: list[tuple[str, str]] = []
    publishers: set[str] = set()
    for item in pool:
        title, url = str(item["title"]), str(item["url"])
        publisher = str(item["publisher"])
        if publisher and publisher in publishers:
            continue
        links.append((title, url))
        if publisher:
            publishers.add(publisher)
        if len(links) >= limit:
            return links
    selected = {_citation_key(url) for _title, url in links}
    for item in pool:
        title, url = str(item["title"]), str(item["url"])
        if _citation_key(url) in selected:
            continue
        links.append((title, url))
        selected.add(_citation_key(url))
        if len(links) >= limit:
            break
    return links


def _attach_validated_citations(
    answer: str,
    findings: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[Mapping[str, Any]] = (),
    question: str = "",
) -> str:
    """Remove invented links and ensure the answer exposes checked sources."""

    evidence_urls = {
        _citation_key(str(source.get("url") or ""))
        for finding in findings
        for source in finding.get("evidence", [])
        if isinstance(source, Mapping) and source.get("url")
    }
    cited_sources = (
        [source for source in sources if _citation_key(str(source.get("url") or "")) in evidence_urls]
        if evidence_urls else sources
    )
    links = _validated_source_links(
        findings,
        sources=cited_sources,
        question=question,
    )
    if not links:
        return answer
    allowed = {
        _citation_key(url): {"title": title, "url": url}
        for title, url in links
    }
    for source in [*sources,*(s for finding in findings for s in finding.get('evidence',[]))]:
        if not isinstance(source,Mapping) or source.get('validation','validated')!='validated':continue
        url=str(source.get('url') or '')
        if url.startswith(('http://','https://')):
            allowed[_citation_key(url)]={'title':str(source.get('title') or 'Source'),'url':url}
    used: set[str] = set()

    def keep_known(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        key = _citation_key(url)
        if key in allowed:
            used.add(key)
            if re.fullmatch(r"src-\d+", label.strip(), flags=re.IGNORECASE) or re.fullmatch(
                r"https?://\S+", label.strip(), flags=re.IGNORECASE
            ):
                record = allowed[key]
                return f"[{record['title']}]({record['url']})"
            return match.group(0)
        return label

    safe = _MARKDOWN_LINK.sub(keep_known, str(answer or "").strip())




    safe = _FOOTNOTE_DEFINITION.sub("", safe)
    safe = _FOOTNOTE_MARKER.sub("", safe)
    safe = re.sub(r"\bsrc-\d+\b", "", safe, flags=re.IGNORECASE)
    safe = re.sub(
        r"^\s*based on (?:the )?provided findings,?\s*",
        "",
        safe,
        flags=re.IGNORECASE,
    )

    protected: list[str] = []

    def protect_inline_link(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"@@SALTY_VALIDATED_LINK_{len(protected) - 1}@@"

    safe = _MARKDOWN_LINK.sub(protect_inline_link, safe)
    safe = re.sub(r"https?://[^\s<>()\]]+", "", safe, flags=re.IGNORECASE)
    for index, link in enumerate(protected):
        safe = safe.replace(f"@@SALTY_VALIDATED_LINK_{index}@@", link)
    cleaned_lines = [
        re.sub(r"[ \t]{2,}", " ", line).rstrip()
        for line in safe.splitlines()
    ]




    safe = "\n".join(
        line
        for line in cleaned_lines
        if not re.fullmatch(r"\s*(?:[-+*]|\d+[.)])\s*", line)
    )
    safe = re.sub(r"\s+([.,;:])", r"\1", safe)
    safe = re.sub(r"\n{3,}", "\n\n", safe).strip()

    missing = [
        (title, url) for title, url in links if _citation_key(url) not in used
    ]
    if not missing or used:
        return safe
    citations = "; ".join(f"[{title}]({url})" for title, url in missing)
    return f"{safe}\n\nValidated sources: {citations}".strip()


def _ensure_disagreement_answer(
    question: str,
    answer: str,
    findings: Sequence[Mapping[str, Any]],
) -> str:
    """Make an explicitly requested disagreement check visible in the answer."""

    if not re.search(
        r"\b(?:disagree(?:ment|ments|d)?|contradict(?:ion|ions|ed)?|conflict(?:s|ed)?)\b",
        str(question or ""),
        re.IGNORECASE,
    ):
        return str(answer or "").strip()
    text = str(answer or "").strip()
    if re.search(
        r"\b(?:disagree(?:ment|ments|d)?|contradict(?:ion|ions|ed)?|conflict(?:s|ed)?)\b",
        text,
        re.IGNORECASE,
    ):
        return text
    disputed = [item for item in findings if bool(item.get("disputed"))]
    if not disputed:
        note = "No material disagreement appeared in the validated evidence."
    else:
        first = " ".join(str(disputed[0].get("text") or "").split())[:220]
        note = f"The validated evidence contains a material disagreement: {first}"
    return f"{text}\n\n{note}".strip()


def _research_activity_journal(
    report: Mapping[str, Any], answer: str = ""
) -> list[dict[str, Any]]:
    """The cleaned, past-tense journal retained beside a finished answer."""

    return build_research_activity_journal(
        research_progress_snapshot(report, phase="completed", answer=answer)
    )


def _evidence_limits(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What the gathered evidence settles, and what it does not.

    Availability is its own observation and "unknown" is a real answer.
    Treating it as good as "in stock" is how a question about what can be
    bought today gets answered with something nobody established was buyable.
    """

    confirmed, unconfirmed, unavailable = [], [], []
    for item in observations:
        stock = str(item.get("stock") or "unknown")
        name = str(item.get("product") or "")[:120]
        if stock == "in_stock":
            confirmed.append(name)
        elif stock in {"out_of_stock", "preorder"}:
            unavailable.append(name)
        else:
            unconfirmed.append(name)
    return {
        "in_stock_confirmed": confirmed,
        "stock_not_confirmed": unconfirmed,
        "not_available": unavailable,


        "note": (
            "Availability was confirmed for none of these, so none of them can "
            "be called the cheapest one currently in stock."
            if not confirmed
            else "Only the items listed under in_stock_confirmed may be "
            "described as currently in stock."
        ),
    }


def _reads_like_process(answer: str) -> bool:
    head = answer.strip().lower()[:200]
    try:
        control = json.loads(answer)
    except (TypeError, ValueError):
        control = None
    if isinstance(control, dict) and set(control) <= {"query", "action", "reason", "question"} and set(control) & {"query", "action"}:
        return True
    return any(phrase in head for phrase in _PROCESS_PHRASES) or bool(re.match(
        r"(?:i will|i'll|let me)\s+(?:research|search|verify|investigate|look up)\b", head,
    ))


def _plain_failure(node: Any) -> str:
    """A node's failure without the exception plumbing wrapped around it.

    Failures arrive as "CapabilityCallFailed: UI Automation command must be one
    of: collapse, expand, ..." — a class name, a colon, and a schema dump. The
    class name means nothing to the reader and the list is not an explanation,
    so the sentence keeps the human half and drops the rest.
    """

    text = str(getattr(node, "failure", "") or "").strip()

    while True:
        head, separator, tail = text.partition(": ")
        if not separator or " " in head or not head or not head[0].isupper():
            break
        text = tail.strip()
    listing = text.find(" must be one of:")
    if listing != -1:
        text = text[: listing + len(" must be one of")].replace(
            " must be one of", " was not one this capability accepts"
        )
    return text or "the step did not succeed"


class LiveRunners:
    """Carry out an action, a plan, or research on behalf of a chat turn."""

    def __init__(
        self,
        *,
        broker: Any = None,
        connectors: Any = None,
        images: Any = None,
        generate: Callable[[list[dict[str, str]]], str] | None = None,
        generate_structured: Callable[[list[dict[str, str]]], str] | None = None,
        generate_research_review: Callable[[list[dict[str, str]]], str] | None = None,
        generate_with_preview: Callable[
            [list[dict[str, str]], Callable[[Mapping[str, Any]], None]], str
        ]
        | None = None,
        generate_structured_with_preview: Callable[
            [list[dict[str, str]], Callable[[Mapping[str, Any]], None]], str
        ]
        | None = None,
        generate_final_with_preview: Callable[
            [list[dict[str, str]], Callable[[Mapping[str, Any]], None]], str
        ]
        | None = None,
        task: Any = None,
        capabilities: Sequence[str] = (),
        authority_mode: str = "ask_every_time",
        approve: Callable[[Mapping[str, Any]], bool] | None = None,
        search: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
        read: Callable[[str], Mapping[str, Any]] | None = None,
        read_for_question: Callable[[str, str], Mapping[str, Any]] | None = None,
        search_site: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        memory: Any = None,
        mission_memory: Any = None,
        on_step: Callable[[Mapping[str, Any]], None] | None = None,
        on_agent_progress: Callable[[Mapping[str, Any]], None] | None = None,
        on_research: Callable[[Mapping[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        describe_screenshot: Callable[[str], str] | None = None,
        continue_until_satisfied: bool = False,
        follow_through_steps: int = 8_192,
        mission_duration_seconds: float = 8 * 60 * 60,
        established: str = "",
        checkpoint_path: str | Path | None = None,
        research_profile: str = "verification",
    ) -> None:
        self.broker = broker
        self.search_site = search_site
        self.connectors = connectors
        self.images = images
        self.generate = generate
        self.generate_structured = generate_structured or generate
        self.generate_research_review = generate_research_review
        self._coverage_findings: list[Mapping[str, Any]] = []
        self._answer_findings: list[Mapping[str, Any]] = []
        self.generate_with_preview = generate_with_preview
        self.generate_structured_with_preview = (
            generate_structured_with_preview or generate_with_preview
        )
        self.generate_final_with_preview = (
            generate_final_with_preview or generate_with_preview
        )
        self.task = task
        self.capabilities = list(capabilities)
        self.authority_mode = authority_mode
        self.approve = approve
        self.search = search
        self.read = read
        self.read_for_question = read_for_question



        self.memory = memory
        self.mission_memory = mission_memory
        self.on_step = on_step
        self.on_agent_progress = on_agent_progress

        self.on_research = on_research
        self.should_stop = should_stop
        self.describe_screenshot = describe_screenshot



        self.continue_until_satisfied = continue_until_satisfied


        self.follow_through_steps = max(1, int(follow_through_steps))
        self.mission_duration_seconds = max(0.001, float(mission_duration_seconds))




        self.established = str(established or "")
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        selected_profile = str(research_profile or "verification").strip().casefold()
        self.research_profile = (
            selected_profile
            if selected_profile in {"verification", "instant", "cooking"}
            else "verification"
        )



    def run_action(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Carry out a single capability call.

        Deliberately not routed through the workflow engine. "Open YouTube" is
        one action; wrapping it in a plan, a verifier and a checkpoint would add
        latency and moving parts to something that has neither dependencies nor
        anything to verify beyond the call's own result.
        """

        from ..automation.invocation import (
            CapabilityCallFailed,
            invoke_capability,
        )
        from ..automation.routing import resolve_execution
        from .agent_loop import AgentLoop

        capability = str(decision.get("capability") or decision.get("tool") or "")
        arguments = dict(decision.get("arguments") or {})

        if capability and capability in self.capabilities and self.broker is not None:
            route = resolve_execution(capability, arguments, self.capabilities)
            if self.task is not None:
                self.task.note_tool_call(route.capability, route.tier_name)
            try:
                result = invoke_capability(
                    self.broker,
                    capability,
                    arguments,
                    authority_mode=self.authority_mode,
                    granted=self.capabilities,
                    repair=self._repair_arguments,
                    task=self.task,
                )
            except CapabilityCallFailed as error:
                # A bad first choice is an observation for the agent, not the
                # end of a computer task. Keep permission failures terminal;
                # alternative actions still go through the broker's policy.
                if (
                    self.continue_until_satisfied
                    and (self.generate_structured is not None or self.generate is not None)
                    and error.kind != "PermissionError"
                    and not (self.should_stop and self.should_stop())
                ):
                    return self._continue_from(decision, route, {
                        "status": "failed", "failure_kind": error.kind,
                        "error": str(error),
                    }, request)
                return {
                    "answer": (
                        f"I could not carry that out: {error}"
                    ),
                    "capability": route.capability,
                    "tier": route.tier_name,
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                }
            if self.task is not None and getattr(self.task, "world_state", None):
                self.task.world_state.absorb(route.capability, result or {})







            if self.continue_until_satisfied and self.generate is not None:
                return self._continue_from(decision, route, result or {}, request)

            return {
                "answer": (
                    str(decision.get("reason") or "").strip()
                    or _describe_effect(route, result or {})
                ),
                "capability": route.capability,
                "tier": route.tier_name,
                "status": (result or {}).get("status", "succeeded"),
                "result": dict(result or {}),
            }




        if not self.capabilities:
            return {
                "answer": (
                    "Using your computer is Agent work, and Agent mode is off "
                    "for this message. Turn on Agent in the composer and ask "
                    "again, and I will do it."
                ),
                "status": "declined",
                "requires_agent_mode": True,
            }



        if self.generate is None or self.broker is None:
            return {"answer": "That action is not available.", "status": "declined"}
        outcome = AgentLoop(
            broker=self.broker,
            generate=self.generate_structured or self.generate,
            generate_with_preview=self.generate_structured_with_preview,
            generate_final_with_preview=self.generate_final_with_preview,
            capabilities=self.capabilities,
            authority_mode=self.authority_mode,
            approve=self.approve,
            task=self.task,
            memory=self.memory,
            mission_memory=self.mission_memory,
            on_step=self.on_step,
            on_progress=self.on_agent_progress,
            should_stop=self.should_stop,
            describe_screenshot=self.describe_screenshot,
            max_iterations=self.follow_through_steps,
            max_duration_seconds=self.mission_duration_seconds,
            checkpoint_path=self.checkpoint_path,
            established=self.established,
        ).run(request)
        agent_task = {
            **dict(outcome.get("task") or {}),
            "state": outcome.get("state"),
            "steps": list(outcome.get("steps") or []),
            "step_count": outcome.get("step_count"),
            "steps_truncated": bool(outcome.get("steps_truncated")),
        }
        return {
            "answer": str(outcome.get("answer") or ""),
            "status": outcome.get("state"),
            "steps": outcome.get("steps", []),
            "step_count": outcome.get("step_count"),
            "agent_task": agent_task,
            "metrics": dict(outcome.get("metrics") or {}),
        }



    def run_plan(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Validate a model-authored plan, then run it."""

        connectors = (
            [item["connector"] for item in self.connectors.catalogue()]
            if self.connectors is not None
            else []
        )
        try:
            plan = validate_plan(
                decision.get("plan") or decision,
                goal=str(decision.get("goal") or request),
                capabilities=self.capabilities,
                connectors=connectors,
            )
            self._preflight_connector_plan(plan)
        except PlanRejected as error:
            raw_payload = decision.get("plan") or decision
            raw_nodes = (
                list(raw_payload.get("nodes") or [])
                if isinstance(raw_payload, Mapping)
                else []
            )
            configured = set(connectors)
            names_configured_service = any(
                isinstance(node, Mapping)
                and str(node.get("connector") or "") in configured
                for node in raw_nodes
            )
            repaired_plan = None
            if names_configured_service and self.generate_structured is not None:
                repaired = self._repair_initial_connector_plan(
                    decision=decision,
                    request=request,
                    error=error,
                    connectors=connectors,
                )
                if repaired is not None:
                    try:
                        repaired_plan = validate_plan(
                            repaired.get("plan") or repaired,
                            goal=str(repaired.get("goal") or request),
                            capabilities=self.capabilities,
                            connectors=connectors,
                        )
                        self._preflight_connector_plan(repaired_plan)
                    except PlanRejected as repaired_error:
                        error = repaired_error
                        repaired_plan = None
            if repaired_plan is not None:
                plan = repaired_plan





            if (
                repaired_plan is None
                and
                not names_configured_service
                and self.generate is not None
                and self.broker is not None
                and self.capabilities
            ):
                from .agent_loop import AgentLoop

                outcome = AgentLoop(
                    broker=self.broker,
                    generate=self.generate_structured or self.generate,
                    generate_with_preview=self.generate_structured_with_preview,
                    generate_final_with_preview=self.generate_final_with_preview,
                    capabilities=self.capabilities,
                    authority_mode=self.authority_mode,
                    approve=self.approve,
                    task=self.task,
                    memory=self.memory,
                    mission_memory=self.mission_memory,
                    on_step=self.on_step,
                    on_progress=self.on_agent_progress,
                    should_stop=self.should_stop,
                    describe_screenshot=self.describe_screenshot,
                    max_iterations=self.follow_through_steps,
                    max_duration_seconds=self.mission_duration_seconds,
                    checkpoint_path=self.checkpoint_path,
                    established=self.established,
                ).run(
                    request,
                    opening=json.dumps(
                        {
                            "invalid_plan": str(error),
                            "note": (
                                "Choose the first valid capability call from the live "
                                "tool contract, observe its result, then continue."
                            ),
                        }
                    ),
                )
                agent_task = {
                    **dict(outcome.get("task") or {}),
                    "state": outcome.get("state"),
                    "steps": list(outcome.get("steps") or []),
                    "step_count": outcome.get("step_count"),
                    "steps_truncated": bool(outcome.get("steps_truncated")),
                }
                return {
                    "answer": str(outcome.get("answer") or ""),
                    "status": outcome.get("state"),
                    "steps": outcome.get("steps", []),
                    "step_count": outcome.get("step_count"),
                    "agent_task": agent_task,
                    "metrics": dict(outcome.get("metrics") or {}),
                    "invalid_plan_recovered_to_agent": True,
                    "plan_rejected": str(error),
                }
            if repaired_plan is None:

                return {
                    "answer": f"I could not build a safe plan for that: {error}",
                    "status": "rejected",
                    "plan_rejected": str(error),
                }

        executor = Executor(
            broker=self.broker,
            connectors=self.connectors,
            images=self.images,
            task=self.task,
            approve=self.approve,
            authority_mode=self.authority_mode,
            granted=self.capabilities,
            repair=self._repair_arguments if self.generate is not None else None,
        )
        engine = WorkflowEngine(
            executor=executor,
            task=self.task,
            replan=self._replan if self.generate is not None else None,



            validate=lambda payload, plan_goal: validate_plan(
                payload,
                goal=plan_goal,
                capabilities=self.capabilities,
                connectors=connectors,
            ),
            checkpoint_path=self.checkpoint_path,
        )
        result = engine.run(plan)
        return {
            "answer": self._plan_answer(result, request),
            "status": result.state,
            "plan": result.plan.to_dict(),
            "replans": result.replans,
            "waiting_for": result.waiting_for,
        }

    def _preflight_connector_plan(self, plan: Plan) -> None:
        """Reject unknown connector operations and missing fields before writes."""

        if self.connectors is None:
            if any(node.connector for node in plan.nodes):
                raise PlanRejected("The plan names a service but none is configured")
            return
        for node in plan.nodes:
            if not node.connector:
                continue
            try:
                connector = self.connectors.get(node.connector)
                spec = connector.operation(str(node.operation))
            except Exception as error:
                raise PlanRejected(str(error)) from error
            missing = [name for name in spec.required if name not in node.arguments]
            if (
                "id" in missing
                and "ids" in node.arguments
                and len(spec.required) == 1
            ):
                node.arguments["id"] = node.arguments.pop("ids")
                missing.remove("id")
            if missing:
                raise PlanRejected(
                    f"Step {node.node_id!r} operation {node.operation!r} needs: "
                    + ", ".join(missing)
                )
            for name in spec.required:
                value = node.arguments.get(name)
                if (
                    name.endswith("id")
                    and isinstance(value, str)
                    and re.search(r"[<>{}]|(?:^|\s)(?:from|unknown|placeholder)(?:\s|$)", value, re.I)
                ):
                    raise PlanRejected(
                        f"Step {node.node_id!r} contains a placeholder instead of an observed {name}"
                    )
            ids_value = node.arguments.get("ids")
            if (
                getattr(spec, "batchable", False)
                and isinstance(ids_value, Mapping)
                and set(ids_value) == {"$ref"}
                and re.search(r"\.items\[\d+\]\.id$", str(ids_value["$ref"]))
                and re.search(r"\b(messages|emails|items|results)\b", plan.goal, re.I)
                and not re.search(r"\b(first|one|single|only one)\b", plan.goal, re.I)
            ):
                node.arguments["ids"] = {
                    "$ref": re.sub(
                        r"\.items\[\d+\]\.id$",
                        ".items[*].id",
                        str(ids_value["$ref"]),
                    )
                }
            effective_ids = node.arguments.get("ids")
            if (
                getattr(spec, "batchable", False)
                and isinstance(effective_ids, Mapping)
                and set(effective_ids) == {"$ref"}
            ):
                reference = str(effective_ids["$ref"])
                producer = next(
                    (
                        candidate
                        for candidate in sorted(plan.nodes, key=lambda item: len(item.node_id), reverse=True)
                        if reference.startswith(f"{candidate.node_id}.output")
                    ),
                    None,
                )
                if (
                    producer is not None
                    and producer.connector == node.connector
                    and producer.operation == "search"
                ):
                    producer.arguments.setdefault("limit", 500)




            thread_value = node.arguments.get("thread_id")
            if (
                isinstance(thread_value, Mapping)
                and set(thread_value) == {"$ref"}
                and str(thread_value["$ref"]).endswith(".id")
            ):
                node.arguments["thread_id"] = {
                    "$ref": str(thread_value["$ref"])[:-3] + ".thread_id"
                }

    def _repair_initial_connector_plan(
        self,
        *,
        decision: Mapping[str, Any],
        request: str,
        error: Exception,
        connectors: Sequence[str],
    ) -> Mapping[str, Any] | None:
        """Give one rejected service plan back to the same model before execution."""

        if self.generate_structured is None or self.connectors is None:
            return None
        try:
            hints = self.connectors.orchestration_hints()
        except Exception:
            hints = list(connectors)
        prompt = (
            "Return ONE valid compact JSON object and no prose. The previous plan "
            f"was rejected before execution: {error}. Copy this exact shape, changing "
            "only query and label text: "
            '{"action":"plan","nodes":[{"node":"find","connector":"mail.local",'
            '"operation":"search","arguments":{"query":"from:Sam Project Atlas",'
            '"limit":500}},{"node":"label","connector":"mail.local","operation":'
            '"create_label","arguments":{"name":"Project Atlas"}},{"node":"tag",'
            '"connector":"mail.local","operation":"apply_label","arguments":{"ids":'
            '{"$ref":"find.output.items[*].id"},"label":{"$ref":"label.output.label"}},'
            '"depends_on":["find","label"]}]}. Do not add nodes. Do not delete. '
            "Available exact operations: " + "; ".join(hints)
        )
        reply = self.generate_structured(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": request},
            ]
        )
        from .dispatch import read_decision

        repaired = read_decision(str(reply))
        if repaired is None or repaired.get("action") != "plan":
            return None
        if self.task is not None:
            self.task.metrics.planning_model_calls += 1
        return repaired

    def _continue_from(
        self,
        decision: Mapping[str, Any],
        route: Any,
        result: Mapping[str, Any],
        request: str,
    ) -> dict[str, Any]:
        """Carry on from a completed first action until the goal is satisfied.

        The agent loop is the thing that already knows how to observe, decide
        and stop; this only gives it a running start so the work the decision
        layer already did is not repeated. The loop ends the moment the model
        responds, so a genuinely one-step goal costs one extra generation
        rather than a whole re-plan.
        """

        from .agent_loop import AgentLoop

        opening = {
            "completed_step": {
                "action": route.capability,
                "arguments": dict(route.arguments),
                "observation": dict(result),
            },
            "goal": request,
            "note": (
                "This was attempted and failed. Use the failure to choose a different "
                "approach within the granted tools and original constraints. Do not "
                "claim this step succeeded or blindly repeat it."
                if result.get("status") == "failed" else
                "This already ran. If the goal is now satisfied, respond. "
                "Otherwise continue from here."
            ),
        }
        outcome = AgentLoop(
            broker=self.broker,
            generate=self.generate_structured or self.generate,
            generate_with_preview=self.generate_structured_with_preview,
            generate_final_with_preview=self.generate_final_with_preview,
            capabilities=self.capabilities,
            authority_mode=self.authority_mode,
            approve=self.approve,
            task=self.task,
            memory=self.memory,
            mission_memory=self.mission_memory,
            on_step=self.on_step,
            on_progress=self.on_agent_progress,
            should_stop=self.should_stop,
            describe_screenshot=self.describe_screenshot,
            max_iterations=self.follow_through_steps,
            max_duration_seconds=self.mission_duration_seconds,
            checkpoint_path=self.checkpoint_path,
            established=self.established,
        ).run(request, opening=json.dumps(opening, default=str))
        steps = list(outcome.get("steps") or [])
        agent_task = {
            **dict(outcome.get("task") or {}),
            "state": outcome.get("state"),
            "steps": steps,
            "step_count": outcome.get("step_count"),
            "steps_truncated": bool(outcome.get("steps_truncated")),
        }
        return {
            "answer": str(outcome.get("answer") or "")
            or _describe_effect(route, result),
            "capability": route.capability,
            "tier": route.tier_name,
            "status": outcome.get("state") or "succeeded",
            "result": dict(result),
            "steps": steps,
            "step_count": outcome.get("step_count"),
            "agent_task": agent_task,
            "metrics": dict(outcome.get("metrics") or {}),
        }

    def _plan_answer(self, result: Any, request: str) -> str:
        """What to tell the user once a plan has finished running.

        The engine answers "Done." for a completed plan, which is true and
        useless: asked to open Notepad, bring it forward and say what was on
        the screen, the three steps ran and the reply was the single word
        "Done." The steps each reported what they actually did, so the report
        is built from those observations — the same evidence the audit records
        keep — rather than from the request.
        """

        nodes = list(getattr(getattr(result, "plan", None), "nodes", []) or [])
        sentences: list[str] = []
        for node in nodes:
            if node.state != "completed" or not node.capability:
                continue
            route = SimpleNamespace(capability=node.capability)
            sentences.append(_describe_effect(route, node.result or {}))
        spoken = [sentence for sentence in sentences if sentence != "Done."]

        if str(getattr(result, "state", "")) == "completed":
            if not spoken:
                return str(getattr(result, "message", "") or "Done.")
            return " ".join(spoken)






        failed = next((node for node in nodes if node.state == "failed"), None)
        if failed is None:
            return str(getattr(result, "message", "") or "")
        parts = list(spoken)
        parts.append(f"I could not finish the last step: {_plain_failure(failed)}")
        return " ".join(parts)

    def _repair_arguments(self, brief: str) -> Mapping[str, Any] | None:
        """One correction, from the model, given the capability's real contract.

        The brief is machine-readable on purpose: what was rejected, why, and
        the exact field names the capability accepts. A model that is told only
        "unknown field" can do nothing but guess again.
        """

        repair_generate = self.generate_structured or self.generate
        if repair_generate is None:
            return None
        reply = repair_generate(
            [
                {
                    "role": "system",
                    "content": (
                        "That tool call was rejected. Reply with one JSON "
                        "object holding only the corrected arguments — no "
                        "prose, no capability name, no explanation."
                    ),
                },
                {"role": "user", "content": brief},
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        if not isinstance(parsed, Mapping):
            return None

        inner = parsed.get("arguments")
        if isinstance(inner, Mapping):
            return inner
        return parsed

    def _replan(self, plan: Plan, outcome: Any) -> Mapping[str, Any] | None:
        """Ask Base Steak — the same model, not a planner — to repair a plan."""

        if self.generate is None:
            return None
        if self.task is not None:
            self.task.metrics.planning_model_calls += 1
        world = (
            self.task.world_state.briefing()
            if self.task is not None and getattr(self.task, "world_state", None)
            else {}
        )
        reply = self.generate(
            [
                {"role": "system", "content": "Reply with one JSON object and nothing else."},
                {"role": "user", "content": replan_packet(plan, outcome, world=world)},
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        return parsed if isinstance(parsed, Mapping) else None



    def _publish_research_stage(
        self,
        report: Mapping[str, Any],
        phase: str,
        *,
        generation_preview: Mapping[str, Any] | None = None,
        answer: str = "",
    ) -> None:
        if self.on_research is None:
            return
        try:
            self.on_research(
                research_progress_snapshot(
                    report,
                    phase=phase,
                    generation_preview=generation_preview,
                    answer=answer,
                )
            )
        except Exception:


            return

    def run_research(self, *, decision: Mapping[str, Any], request: str) -> dict[str, Any]:
        """Gather and compare sources, reasoning only where it is needed."""

        if self.search is None or self.read is None:
            return {
                "answer": "Web research is not available in this build.",
                "status": "declined",
            }

        question = str(decision.get("question") or decision.get("goal") or request)
        self._coverage_findings = []
        self._answer_findings = []



        budget = Budget.for_profile(self.research_profile)
        research_checkpoint = None
        if self.checkpoint_path is not None:
            research_checkpoint = self.checkpoint_path.with_name(
                self.checkpoint_path.stem + "-research.json"
            )
        loop = ResearchLoop(
            question,
            search=self.search,
            read=(lambda url:self.read_for_question(url,question)) if self.read_for_question else self.read,
            search_site=self.search_site,
            follow_up=self._follow_up,
            assess_coverage=self._assess_coverage if self.generate_research_review else None,
            budget=budget,
            task=self.task,
            on_progress=self.on_research,
            checkpoint_path=research_checkpoint,
        )
        report = loop.run(str(decision.get("query") or question))
        self._publish_research_stage(report, "synthesizing")
        answer = self._answer_from(question, report)
        status = "failed" if getattr(self, "_answer_synthesis_failed", False) else "completed"
        self._publish_research_stage(report, status, answer=answer)
        return {
            "answer": answer,
            "status": status,
            "activity_journal": _research_activity_journal(report, answer),
            "research": {
                key: report[key]
                for key in (
                    "question",
                    "queries",
                    "source_count",
                    "claim_count",
                    "corroborated",
                    "independent_publisher_count",
                    "disputed",
                    "relevant_claim_count",
                    "relevant_corroborated",
                    "relevant_disputed",
                    "relevant_publisher_count",
                    "status_target_version",
                    "status_target_publisher_count",
                    "evidence_sufficient",
                    "coverage_assessment",
                    "coverage_required",
                    "coverage_seconds",
                    "retrieval_seconds",
                    "open_questions",
                    "stop_reason",
                    "profile",
                    "hard_ceiling_seconds",
                    "coverage_target",
                    "minimum_independent_sources",
                    "validation_rounds_required",
                    "validation_rounds_completed",
                )
            }


            | {"process_summary": self._summarise(report)},

            "sources": report["sources"],
            # Persist the evidence actually sent to the answer pass, not a
            # separately reranked list that can drop a cited primary source.
            "claims": list(self._answer_findings),



            "observations": report.get("observations") or [],




            "evidence_limits": _evidence_limits(report.get("observations") or []),
        }

    def _assess_coverage(self, ledger: Any) -> dict[str, Any]:
        from ..research.coverage import assess_coverage
        all_findings=[c for c in ledger.report()['claims'] if not _is_research_boilerplate(str(c.get('text') or ''))]
        pool=_select_finaliser_findings(ledger.question,all_findings,limit=40)
        wanted=_question_terms(ledger.question)
        uncovered=set(wanted)
        findings=[]
        # Repeated sentences about the first requested fact must not crowd out
        # a less frequently mentioned second/third part of the question.
        while pool and len(findings)<16:
            best=max(pool,key=lambda c:(len(uncovered & _question_terms(str(c.get('text') or ''))),
                _finding_rank(wanted,c)))
            findings.append(best);pool.remove(best)
            uncovered-=_question_terms(str(best.get('text') or ''))
        # Include a small distinct set per publisher too; a mirror's matching
        # sentence must not hide the original publisher's equivalent evidence.
        if wanted & {'original','official'}:
            from urllib.parse import urlsplit
            originals=[c for c in all_findings if any(
                wanted & set((urlsplit(str(s.get('url') or '')).hostname or '').split('.'))
                for s in c.get('evidence',[]) if isinstance(s,Mapping))]
            primary=[]
            for need in ledger.question.split('?'):
                if not need.strip() or need.strip().casefold().startswith('cite '):continue
                # Cite each requested subtopic, not a pile of generic pages
                # matching the words "original documentation".
                import math
                from collections import Counter
                need_terms={word[:5] for word in _question_terms(need) if len(word)>2}
                terms_by_claim=[{word[:5] for word in _question_terms(str(c.get('text') or ''))} for c in originals]
                frequency=Counter(t for terms in terms_by_claim for t in terms)
                scored=sorted(zip(originals,terms_by_claim),key=lambda pair:sum(
                    1+math.log((len(originals)+1)/(frequency[t]+1)) for t in need_terms & pair[1]),reverse=True)
                candidates=[c for c,_ in scored[:6]]
                for candidate in candidates:
                    if candidate not in primary:primary.append(candidate)
            findings=[*primary,*[c for c in findings if c not in primary]][:24]
        remaining=min(300.0,ledger.budget.max_seconds)
        deadline=time.monotonic()+remaining
        self._coverage_findings=list(findings)
        def generate_review(messages):
            seconds=max(0.0,deadline-time.monotonic())
            if seconds <= 0:
                return '{}'
            if self.task is not None:
                self.task.metrics.planning_model_calls += 1
            return self.generate_research_review(messages,time_budget_seconds=seconds)
        return assess_coverage(ledger.question,findings,generate_review)

    def _follow_up(self, ledger: Any) -> str | None:
        """One model call decides whether another query is worth making.

        Not per page. The retrieval and extraction between queries is entirely
        deterministic, which is what keeps a research task at a few reasoning
        turns however many sources it reads.
        """

        if self.generate is None:
            return None
        terms = _question_terms(ledger.question)
        gaps = sorted(
            (
                claim.to_dict()
                for claim in ledger.relevant_disputed_claims
                if _finding_rank(terms, claim.to_dict())[0]
                and _finding_rank(terms, claim.to_dict())[2] > 0
            ),
            key=lambda item: _finding_rank(terms, item),
            reverse=True,
        )[:5]
        evidence_covered = ledger.evidence_sufficient
        if not gaps and evidence_covered:
            return None
        if self.task is not None:
            self.task.metrics.planning_model_calls += 1
        reply = self.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "Reply with one JSON object: "
                        '{"query":"..."} for one more search, or {"query":null} '
                        "if the evidence is sufficient."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": ledger.question,
                            "already_searched": ledger.queries,
                            "disagreements": gaps,
                            "settled": len(ledger.corroborated_claims),
                        },
                        default=str,
                    ),
                },
            ]
        )
        from .actions import _whole_json_object

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        query = (parsed or {}).get("query") if isinstance(parsed, Mapping) else None
        return str(query) if query else None

    def _answer_from(self, question: str, report: Mapping[str, Any]) -> str:
        """Answer the question from the evidence that was gathered.

        The research runner used to return its own execution statistics as the
        assistant's reply: "I read 6 sources and kept 145 distinct findings. I
        stopped because: source budget." Asked for the tallest building in the
        world, the user was told how the search went and never told the answer.
        Retrieval and comparison are the work; the answer is the product, and
        the statistics belong in the provenance beside it.
        """

        self._answer_synthesis_failed = False
        claims = list(report.get("claims") or [])
        if not claims:
            self._answer_synthesis_failed = True
            return (
                "I could not find enough to answer that. "
                + self._summarise(report)
            )





        ordered = _select_finaliser_findings(question, claims)
        if self._coverage_findings:
            identifiers={c.get('claim') for c in self._coverage_findings}
            ordered=[*self._coverage_findings,*[c for c in ordered if c.get('claim') not in identifiers]]
        coverage=report.get('coverage_assessment') or {}
        required_ids={cid for item in coverage.get('requirements') or [] for cid in item.get('claims') or []}
        if required_ids:
            selected_ids={c.get('claim') for c in ordered}
            # Preserve evidence explicitly used to assess the user's requested
            # facts; diversity/ranking must not discard it before the answer.
            ordered=[c for c in claims if c.get('claim') in required_ids and c.get('claim') not in selected_ids]+ordered
        for requirement in coverage.get('requirements') or []:
            need=str(requirement.get('need') or '')
            if not need:continue
            selected_ids={c.get('claim') for c in ordered}
            extra=_select_finaliser_findings(need,claims,limit=2)
            ordered += [c for c in extra if c.get('claim') not in selected_ids]

        self._answer_findings = list(ordered)

        if self.generate is not None or self.generate_with_preview is not None:
            if self.task is not None:
                self.task.metrics.model_calls += 1




            observations = list(report.get("observations") or [])
            messages = [
                {
                    "role": "system",
                    "content": finaliser_instruction(observations=observations),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        finaliser_payload(
                            question=question,
                            observations=observations,
                            evidence_limits=_evidence_limits(observations),
                            findings=[
                                {
                                    "claim": claim.get("claim"),
                                    "text": claim.get("text"),
                                    "source_count": int(claim.get("source_count") or 0),
                                    "sources": [
                                        {key: source[key] for key in (
                                            "source", "url", "title", "publisher_domain",
                                            "validation", "published_at", "updated_at",
                                        ) if key in source}
                                        for source in claim.get("evidence", [])
                                        if isinstance(source, Mapping)
                                    ],
                                    "disputed": bool(claim.get("disputed")),
                                    "contradicts": list(claim.get("contradicts") or []),
                                    "confidence": claim.get("confidence"),
                                    "validated": bool(claim.get("validated")),
                                    "validation_scope": claim.get("validation_scope", "source_readability"),
                                    "support_status": claim.get("support_status"),
                                    "confidence_scope": claim.get("confidence_scope", "retrieval_heuristic_not_truth_probability"),
                                    "independent_source_count": int(
                                        claim.get("independent_source_count") or 0
                                    ),
                                }
                                for claim in ordered
                            ],
                        ),
                        default=str,
                    ),
                },
            ]
            if coverage:
                payload=json.loads(messages[-1]['content'])
                payload['coverage_review']={k:coverage.get(k) for k in ['status','requirements','missing','scope']}
                payload['coverage_note']='Answer supported parts; explicitly identify unresolved requested facts. Do not fill gaps from memory.'
                messages[-1]['content']=json.dumps(payload,ensure_ascii=False)
            self._publish_research_stage(report, "drafting")
            if self.generate_with_preview is not None:
                reply = self.generate_with_preview(
                    messages,
                    lambda preview: self._publish_research_stage(
                        report,
                        "drafting",
                        generation_preview=preview,
                    ),
                )
            else:
                assert self.generate is not None
                reply = self.generate(messages)
            self._publish_research_stage(report, "verifying")
            answer = strip_reasoning(str(reply or "")).strip()
            if answer and not _reads_like_process(answer):
                answer = _ensure_disagreement_answer(question, answer, ordered)
                if not _status_answer_uses_latest(question, answer, ordered):
                    answer = _ensure_disagreement_answer(
                        question,
                        _status_fallback_answer(question, ordered),
                        ordered,
                    )
                answer = _trim_unrequested_status_scope(question, answer)
                return _attach_validated_citations(
                    answer,
                    ordered,
                    sources=list(report.get("sources") or []),
                    question=question,
                )



        if self.generate is not None or self.generate_with_preview is not None:
            self._answer_synthesis_failed = True
            return "I found sources, but could not produce a supported answer to your question."
        fallback = "\n".join(
            f"- {claim.get('text')}"
            for claim in ordered[:6]
            if str(claim.get("text") or "").strip()
        ) or self._summarise(report)
        fallback = _ensure_disagreement_answer(question, fallback, ordered)
        return _attach_validated_citations(
            fallback,
            ordered,
            sources=list(report.get("sources") or []),
            question=question,
        )

    @staticmethod
    def _summarise(report: Mapping[str, Any]) -> str:
        lines = [
            f"I read {report['source_count']} sources and kept "
            f"{report['claim_count']} distinct findings."
        ]
        disputed = int(
            report.get("relevant_disputed")
            if "relevant_disputed" in report
            else report.get("disputed") or 0
        )
        if disputed:
            lines.append(
                f"{disputed} of them disagree between sources and are worth "
                "checking before relying on."
            )
        lines.append(f"I stopped because: {report['stop_reason'].replace('_', ' ')}.")
        return " ".join(lines)


__all__ = [
    "FINALISER_BASE_RULES",
    "FINALISER_COMMERCE_RULES",
    "LiveRunners",
    "MAX_LIVE_NODES",
    "PlanRejected",
    "RUNNER_SCHEMA",
    "finaliser_instruction",
    "finaliser_payload",
    "replan_packet",
    "validate_plan",
]
