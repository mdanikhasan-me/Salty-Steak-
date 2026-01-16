"""Salty Steak Native Desktop AI Platform — the approval broker.

Where a workflow's request to do something irreversible meets the user's answer.

An approval is bound to exactly one action. That sounds obvious and is the
whole point: a yes to "send this message to Alice" must not be usable for
"delete the mailbox", for the same action against a different recipient, or for
the same request arriving again tomorrow. So every request carries a
fingerprint of what it would actually do, and an answer that does not match the
fingerprint is not an answer to it.

Requests expire. A prompt nobody answered is not a quiet yes, and an approval
granted an hour ago should not still be sitting there ready to authorise
something the user has long forgotten about.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .credentials import redact

APPROVAL_SCHEMA = "salty-steak-approval-v1"



APPROVAL_TIMEOUT_SECONDS = 300.0

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"


def fingerprint(
    *, capability: str, arguments: Mapping[str, Any], task_id: str, node_id: str | None
) -> str:
    """Identify precisely the action being approved.

    Includes the task and node, so an approval cannot drift to a later task,
    and the arguments, so it cannot drift to a different recipient.
    """

    payload = json.dumps(
        {
            "capability": capability,


            "arguments": redact(dict(arguments)),
            "task": task_id,
            "node": node_id,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class ApprovalTicket:
    """One pending decision, and everything the interface needs to show it."""

    request_id: str
    fingerprint: str
    task_id: str
    node_id: str | None
    capability: str
    risk: str
    summary: str
    reason: str
    resource: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    state: str = PENDING
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS

    @property
    def expired(self) -> bool:
        return (
            self.state == PENDING
            and time.time() - self.created_at > self.timeout_seconds
        )

    def to_dict(self) -> dict[str, Any]:
        """The event shape the Activity interface renders. Never any secrets."""

        return {
            "schema": APPROVAL_SCHEMA,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "task": self.task_id,
            "node": self.node_id,
            "capability": self.capability,
            "risk": self.risk,

            "summary": self.summary,
            "reason": self.reason,
            "resource": self.resource,
            "arguments": redact(self.arguments),
            "state": EXPIRED if self.expired else self.state,
            "created_at": self.created_at,
            "expires_in_seconds": max(
                0.0, self.timeout_seconds - (time.time() - self.created_at)
            ),
        }


class ApprovalBroker:
    """Hold pending approvals and match answers to the exact action."""

    def __init__(self, *, timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS) -> None:
        self._tickets: dict[str, ApprovalTicket] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self.timeout_seconds = float(timeout_seconds)



    def request(
        self,
        *,
        capability: str,
        risk: str,
        summary: str,
        reason: str,
        arguments: Mapping[str, Any] | None = None,
        task_id: str = "",
        node_id: str | None = None,
        resource: str | None = None,
    ) -> ApprovalTicket:
        arguments = dict(arguments or {})
        ticket = ApprovalTicket(
            request_id=str(uuid.uuid4()),
            fingerprint=fingerprint(
                capability=capability,
                arguments=arguments,
                task_id=task_id,
                node_id=node_id,
            ),
            task_id=task_id,
            node_id=node_id,
            capability=capability,
            risk=risk,
            summary=summary,
            reason=reason,
            resource=resource,
            arguments=arguments,
            timeout_seconds=self.timeout_seconds,
        )
        with self._lock:
            self._tickets[ticket.request_id] = ticket
            self._events.append({"event": "approval_required", **ticket.to_dict()})
        return ticket



    def approve(self, request_id: str, *, expect_fingerprint: str | None = None) -> bool:
        return self._answer(request_id, APPROVED, expect_fingerprint)

    def reject(self, request_id: str, *, expect_fingerprint: str | None = None) -> bool:
        return self._answer(request_id, REJECTED, expect_fingerprint)

    def _answer(
        self, request_id: str, state: str, expect_fingerprint: str | None
    ) -> bool:
        with self._condition:
            ticket = self._tickets.get(request_id)
            if ticket is None or ticket.state != PENDING:
                return False
            if ticket.expired:
                ticket.state = EXPIRED
                self._condition.notify_all()
                return False
            if expect_fingerprint is not None and expect_fingerprint != ticket.fingerprint:


                return False
            ticket.state = state
            ticket.answered_at = time.time()
            self._events.append(
                {
                    "event": "approval_answered",
                    "request_id": request_id,
                    "state": state,
                    "fingerprint": ticket.fingerprint,
                }
            )
            self._condition.notify_all()
            return True



    def wait_for(
        self,
        ticket: ApprovalTicket,
        *,
        should_stop: Any = None,
        poll_seconds: float = 0.25,
    ) -> bool:
        """Block until the user answers, the request expires, or work stops.

        Only an explicit approval returns true. Expiry, rejection and
        cancellation are all no, because none of them is the user saying yes.
        """

        deadline = ticket.created_at + ticket.timeout_seconds
        with self._condition:
            while True:
                if ticket.state == APPROVED:
                    return True
                if ticket.state in {REJECTED, EXPIRED}:
                    return False
                if should_stop is not None and should_stop():
                    ticket.state = REJECTED
                    return False
                if time.time() >= deadline:
                    ticket.state = EXPIRED
                    self._events.append(
                        {"event": "approval_expired", "request_id": ticket.request_id}
                    )
                    return False
                self._condition.wait(poll_seconds)



    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                ticket.to_dict()
                for ticket in self._tickets.values()
                if ticket.state == PENDING and not ticket.expired
            ]

    def get(self, request_id: str) -> ApprovalTicket | None:
        with self._lock:
            return self._tickets.get(request_id)

    def drain_events(self) -> list[dict[str, Any]]:
        """Events since the last read, for the interface to render."""

        with self._lock:
            events, self._events = self._events, []
            return events

    def callback(self, *, task: Any = None, should_stop: Any = None):
        """An approval function the policy engine and executor can call.

        Bridges the policy engine's simple ask-and-wait shape onto tickets that
        the interface can list, answer and audit.
        """

        def ask(request: Mapping[str, Any]) -> bool:
            ticket = self.request(
                capability=str(request.get("capability") or ""),
                risk=str(request.get("risk") or ""),
                summary=str(request.get("summary") or ""),
                reason=str(request.get("reason") or request.get("summary") or ""),
                arguments=request.get("arguments") or {},
                task_id=getattr(task, "task_id", "") or "",
                node_id=request.get("node"),
                resource=request.get("resource"),
            )
            if task is not None:
                task.record_event("approval_required", **ticket.to_dict())
            approved = self.wait_for(ticket, should_stop=should_stop)
            if task is not None:
                task.record_event(
                    "approval_answered",
                    request_id=ticket.request_id,
                    approved=approved,
                    state=ticket.state,
                )
            return approved

        return ask


__all__ = [
    "APPROVAL_SCHEMA",
    "APPROVAL_TIMEOUT_SECONDS",
    "APPROVED",
    "ApprovalBroker",
    "ApprovalTicket",
    "EXPIRED",
    "PENDING",
    "REJECTED",
    "fingerprint",
]
