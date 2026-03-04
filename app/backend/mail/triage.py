"""Salty Steak — semantic mail triage.

Deciding which of someone's messages do not matter to them is a judgement, not
a rule. A sender list gets the easy half right and the important half wrong: an
order confirmation, a payment receipt, a bank alert and a university notice all
look exactly like the marketing mail they sit next to — same bulk sender, same
templated subject, same unsubscribe footer. Those are the ones that must never
be swept up, and no keyword can tell them apart from an advert for the same
shop.

So the judgement is Base Steak's. This module is the deterministic half:

    fetch metadata in pages
      -> derive cheap features (bulk markers, list headers, repetition)
      -> group near-identical senders so one decision covers many messages
      -> ask the model about compact batches, never one message at a time
      -> fetch full content only for what it says it is unsure about
      -> ask once more about only those
      -> anything still uncertain is left alone

The features exist to make the model's job cheaper and its input smaller. They
never decide anything: a message the features flag as bulk is still shown to
the model, because "bulk" and "unimportant" are different claims and only the
second one matters here.

One model call per batch, plus at most one resolving call. A mailbox of two
thousand messages costs a few dozen generations, not two thousand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

TRIAGE_SCHEMA = "salty-steak-mail-triage-v1"


BATCH_SIZE = 25
MAX_SUBJECT = 120
MAX_SNIPPET = 160
MAX_SENDER = 64



_LIST_HEADERS = ("list-unsubscribe", "list-id", "precedence")
_BULK_WORDS = re.compile(
    r"\b(unsubscribe|view in browser|manage preferences|opt out|"
    r"privacy policy|you are receiving this)\b",
    re.IGNORECASE,
)
_NO_REPLY = re.compile(r"\b(no[-_.]?reply|donotreply|notifications?@|mailer@)", re.IGNORECASE)


@dataclass(frozen=True)
class Message:
    """The part of a message triage is allowed to reason from."""

    identifier: str
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    @property
    def domain(self) -> str:
        match = re.search(r"@([\w.-]+)", self.sender or "")
        return (match.group(1) if match else "").casefold()


@dataclass
class Verdict:
    """What the model concluded about one message, and how sure it was."""

    identifier: str
    category: str = "uncertain"
    low_value: bool = False
    reason: str = ""

    @property
    def actionable(self) -> bool:
        return self.low_value and self.category != "uncertain"


@dataclass
class TriageReport:
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    model_calls: int = 0
    batches: int = 0
    examined: int = 0
    resolved_uncertain: int = 0
    grouped: int = 0

    @property
    def low_value(self) -> list[Verdict]:
        return [item for item in self.verdicts.values() if item.actionable]

    @property
    def uncertain(self) -> list[Verdict]:
        return [
            item
            for item in self.verdicts.values()
            if item.category == "uncertain" or (item.low_value and not item.actionable)
        ]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for verdict in self.verdicts.values():
            counts[verdict.category] = counts.get(verdict.category, 0) + 1
        return {
            "schema": TRIAGE_SCHEMA,
            "examined": self.examined,
            "model_calls": self.model_calls,
            "batches": self.batches,
            "grouped_duplicates": self.grouped,
            "resolved_uncertain": self.resolved_uncertain,
            "low_value": len(self.low_value),
            "uncertain": len(self.uncertain),
            "categories": counts,
        }


def features(message: Message) -> dict[str, Any]:
    """Cheap, honest observations about a message's shape."""

    headers = {str(k).casefold(): str(v) for k, v in (message.headers or {}).items()}
    text = f"{message.subject} {message.snippet}"
    return {
        "list_mail": any(name in headers for name in _LIST_HEADERS),
        "bulk_language": bool(_BULK_WORDS.search(text)),
        "no_reply_sender": bool(_NO_REPLY.search(message.sender or "")),
    }


def _compact(message: Message, *, with_body: bool = False) -> dict[str, Any]:
    signals = [name for name, present in features(message).items() if present]
    record: dict[str, Any] = {
        "id": message.identifier,
        "from": (message.sender or "")[:MAX_SENDER],
        "subject": (message.subject or "")[:MAX_SUBJECT],
    }
    if message.snippet:
        record["preview"] = message.snippet[:MAX_SNIPPET]
    if signals:
        record["signals"] = signals
    if with_body and message.body:
        record["body"] = message.body[:1_200]
    return record


def group_repeats(messages: Sequence[Message]) -> tuple[list[Message], dict[str, list[str]]]:
    """Collapse messages a single decision can cover.

    A shop that sent eleven near-identical offers does not need eleven
    judgements. The representative carries the decision and the rest follow it,
    which is where most of the saving in a real mailbox comes from.

    Grouping is only ever applied to messages that share a sender domain *and*
    a normalised subject, so two genuinely different messages from one sender
    are still judged separately.
    """

    buckets: dict[tuple[str, str], list[Message]] = {}
    for message in messages:
        subject = re.sub(r"[\W\d_]+", " ", (message.subject or "").casefold()).strip()
        buckets.setdefault((message.domain, subject), []).append(message)

    representatives: list[Message] = []
    followers: dict[str, list[str]] = {}
    for group in buckets.values():
        head, *rest = group
        representatives.append(head)
        if rest:
            followers[head.identifier] = [item.identifier for item in rest]
    return representatives, followers


CLASSIFY_INSTRUCTION = (
    "You are sorting one person's mailbox. For each message decide whether it "
    "is genuinely low value *to them*.\n"
    "Treat as important, never low value: anything about money, an order, a "
    "delivery, an account, security, a login, a bill, a deadline, study or "
    "coursework, a job application, or a message a real person wrote to them.\n"
    "Low value means bulk mail they would not miss: adverts, offers, "
    "promotions, and newsletters they show no sign of caring about.\n"
    "If you are not sure, say uncertain. Leaving something alone costs nothing; "
    "filing away a receipt or a security alert is a real mistake.\n"
    'Reply with one JSON object: {"verdicts":[{"id":"...","category":"...",'
    '"low_value":true|false}]}. Use one entry per id you were given and no '
    "other text."
)

RESOLVE_INSTRUCTION = (
    "These are the messages you were unsure about, now with more of their "
    "content. Decide again, with the same rule: anything about money, orders, "
    "delivery, accounts, security, study or a real person stays important. If "
    "you are still unsure, say uncertain and it will be left alone.\n"
    'Reply with one JSON object: {"verdicts":[{"id":"...","category":"...",'
    '"low_value":true|false}]} and no other text.'
)


def _read_verdicts(reply: str) -> list[dict[str, Any]]:
    from ..chat.actions import _whole_json_object
    from ..chat.orchestrator import strip_reasoning

    parsed = _whole_json_object(strip_reasoning(str(reply or "")))
    if not isinstance(parsed, Mapping):
        return []
    entries = parsed.get("verdicts")
    return [item for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []


def classify(
    messages: Sequence[Message],
    *,
    generate: Callable[[list[dict[str, str]]], str],
    fetch_body: Callable[[Sequence[str]], Mapping[str, str]] | None = None,
    batch_size: int = BATCH_SIZE,
) -> TriageReport:
    """Judge a mailbox, hierarchically and conservatively."""

    report = TriageReport(examined=len(messages))
    if not messages:
        return report

    representatives, followers = group_repeats(messages)
    report.grouped = sum(len(items) for items in followers.values())
    by_id = {message.identifier: message for message in messages}

    uncertain_ids: list[str] = []
    for start in range(0, len(representatives), max(1, batch_size)):
        batch = representatives[start : start + max(1, batch_size)]
        report.batches += 1
        report.model_calls += 1
        reply = generate(
            [
                {"role": "system", "content": CLASSIFY_INSTRUCTION},
                {
                    "role": "user",
                    "content": json.dumps(
                        [_compact(item) for item in batch], default=str
                    ),
                },
            ]
        )
        seen = set()
        for entry in _read_verdicts(reply):
            identifier = str(entry.get("id") or "")
            if identifier not in by_id:
                continue
            seen.add(identifier)
            category = str(entry.get("category") or "uncertain").strip().casefold()
            verdict = Verdict(
                identifier=identifier,
                category=category,
                low_value=bool(entry.get("low_value")) and category != "uncertain",
                reason=str(entry.get("reason") or "")[:200],
            )
            report.verdicts[identifier] = verdict
            if category == "uncertain":
                uncertain_ids.append(identifier)

        for item in batch:
            if item.identifier not in seen:
                report.verdicts[item.identifier] = Verdict(
                    identifier=item.identifier,
                    category="uncertain",
                    reason="the model returned no verdict for this message",
                )
                uncertain_ids.append(item.identifier)

    if uncertain_ids and fetch_body is not None:
        bodies = fetch_body(uncertain_ids)
        enriched = [
            Message(
                identifier=identifier,
                sender=by_id[identifier].sender,
                subject=by_id[identifier].subject,
                snippet=by_id[identifier].snippet,
                headers=by_id[identifier].headers,
                body=str(bodies.get(identifier) or ""),
            )
            for identifier in uncertain_ids
            if identifier in by_id
        ]
        for start in range(0, len(enriched), max(1, batch_size)):
            batch = enriched[start : start + max(1, batch_size)]
            report.batches += 1
            report.model_calls += 1
            reply = generate(
                [
                    {"role": "system", "content": RESOLVE_INSTRUCTION},
                    {
                        "role": "user",
                        "content": json.dumps(
                            [_compact(item, with_body=True) for item in batch],
                            default=str,
                        ),
                    },
                ]
            )
            for entry in _read_verdicts(reply):
                identifier = str(entry.get("id") or "")
                if identifier not in by_id:
                    continue
                category = str(entry.get("category") or "uncertain").strip().casefold()
                if category == "uncertain":
                    continue
                report.verdicts[identifier] = Verdict(
                    identifier=identifier,
                    category=category,
                    low_value=bool(entry.get("low_value")),
                    reason=str(entry.get("reason") or "")[:200],
                )
                report.resolved_uncertain += 1



    for head, rest in followers.items():
        decision = report.verdicts.get(head)
        if decision is None:
            continue
        for identifier in rest:
            report.verdicts[identifier] = Verdict(
                identifier=identifier,
                category=decision.category,
                low_value=decision.low_value,
                reason=f"same sender and subject as {head}",
            )
    return report


__all__ = [
    "BATCH_SIZE",
    "Message",
    "TRIAGE_SCHEMA",
    "TriageReport",
    "Verdict",
    "classify",
    "features",
    "group_repeats",
]
