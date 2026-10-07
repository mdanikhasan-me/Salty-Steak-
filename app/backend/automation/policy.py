"""Salty Steak Native Desktop AI Platform — approval and policy engine.

Which actions may run unattended, which need the user to say yes, and which
must not happen at all.

The existing authority modes answer this for the desktop, where the worst case
is a deleted file. An operator that sends mail, replies to a client, moves
money or cancels a meeting needs a second axis: not how much the user trusts
the runtime, but how far the consequence reaches. A command that only reads is
safe under any authority. A message sent on the user's behalf is not, because
it reaches another person and cannot be recalled.

Policy is evaluated on the request, before execution, and the answer is one of
three. Approval is genuinely blocking — a request the user never answers is
never performed. Nothing the model produces can raise its own authority: the
model states intent, the policy decides, and the user confirms.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .credentials import redact

POLICY_SCHEMA = "salty-steak-policy-v1"


RISK_READ = "read"
RISK_WRITE_LOCAL = "write_local"
RISK_DESTRUCTIVE = "destructive"
RISK_SEND_EXTERNAL = "send_external"
RISK_FINANCIAL = "financial"

RISK_ORDER = (
    RISK_READ,
    RISK_WRITE_LOCAL,
    RISK_DESTRUCTIVE,
    RISK_SEND_EXTERNAL,
    RISK_FINANCIAL,
)


ALLOW = "allow"
REQUIRE_APPROVAL = "require_approval"
DENY = "deny"




ALWAYS_CONFIRMED = frozenset({RISK_SEND_EXTERNAL, RISK_FINANCIAL})





CAPABILITY_RISK = {
    "files.manage": RISK_READ,
    "screen.capture": RISK_READ,
    "browser.control": RISK_READ,
    "discord.inspect": RISK_READ,
    "ui.automation": RISK_READ,
    "window.control": RISK_READ,
    "input.control": RISK_WRITE_LOCAL,
    "application.launch": RISK_WRITE_LOCAL,
    "terminal.execute": RISK_WRITE_LOCAL,
}



def _mutating_commands() -> frozenset[str]:
    from .browser_client import BROWSER_MUTATING_COMMANDS
    from .uia_client import UIA_MUTATING_COMMANDS

    return frozenset(BROWSER_MUTATING_COMMANDS | UIA_MUTATING_COMMANDS)


FINANCIAL_HINTS = (
    "checkout",
    "place order",
    "buy now",
    "pay now",
    "confirm payment",
    "submit payment",
    "transfer funds",
    "send money",
)

SEND_HINTS = ("send", "reply", "post", "publish", "share", "invite", "submit application")


class PolicyViolation(RuntimeError):
    """Raised when an action is refused outright."""


class ApprovalDenied(RuntimeError):
    """Raised when the user declined, or never answered, an approval."""


def classify(capability: str, arguments: Mapping[str, Any]) -> str:
    """Work out how far this specific request reaches.

    The capability sets a floor; the arguments can raise it. Clicking a button
    is ordinarily a local action, and is not one when the button says Place
    order.
    """

    declared = str(arguments.get("declared_risk") or "")



    if declared in RISK_ORDER and capability.startswith("connector."):



        risk = declared
    else:
        risk = CAPABILITY_RISK.get(capability, RISK_WRITE_LOCAL)
    haystack = " ".join(
        str(value) for value in arguments.values() if isinstance(value, (str, int))
    ).casefold()
    command = str(arguments.get("command") or arguments.get("action") or "").casefold()

    mutating = command in _mutating_commands()
    if capability == "files.manage":
        operation = str(arguments.get("operation") or "").strip().casefold()
        if operation in {"copy", "move", "rename", "create_directory", "write"}:
            risk = _raise(risk, RISK_WRITE_LOCAL)
        elif operation == "delete":
            risk = _raise(
                risk,
                RISK_DESTRUCTIVE
                if arguments.get("permanent") is True
                else RISK_WRITE_LOCAL,
            )
    if mutating:
        risk = _raise(risk, RISK_WRITE_LOCAL)
    if capability == "terminal.execute":
        from ..chat.actions import _looks_destructive

        argv = arguments.get("argv") or []
        if isinstance(argv, (list, tuple)) and _looks_destructive(
            [str(item) for item in argv]
        ):
            risk = _raise(risk, RISK_DESTRUCTIVE)


    if mutating and any(hint in haystack for hint in FINANCIAL_HINTS):
        risk = _raise(risk, RISK_FINANCIAL)
    elif mutating and any(hint in haystack for hint in SEND_HINTS):
        risk = _raise(risk, RISK_SEND_EXTERNAL)
    return risk


def _raise(current: str, candidate: str) -> str:
    return max(current, candidate, key=RISK_ORDER.index)


@dataclass
class ApprovalRequest:
    """A decision waiting on the user."""

    request_id: str
    capability: str
    risk: str
    summary: str
    arguments: dict[str, Any]
    requested_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "request_id": self.request_id,
            "capability": self.capability,
            "risk": self.risk,
            "summary": self.summary,


            "arguments": redact(self.arguments),
            "requested_at": self.requested_at,
        }


@dataclass
class PolicyDecision:
    """What the engine decided, and why."""

    outcome: str
    risk: str
    reason: str
    request: ApprovalRequest | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": POLICY_SCHEMA,
            "outcome": self.outcome,
            "risk": self.risk,
            "reason": self.reason,
        }
        if self.request is not None:
            payload["approval"] = self.request.to_dict()
        return payload


class PolicyEngine:
    """Decide, per request, between running, asking, and refusing."""

    def __init__(
        self,
        *,
        authority_mode: str = "ask_every_time",
        forbidden: frozenset[str] = frozenset(),
        auto_approved: frozenset[str] = frozenset(),
    ) -> None:
        self.authority_mode = (
            "full_access" if authority_mode == "full_access" else "ask_every_time"
        )

        self.forbidden = frozenset(forbidden)


        self.auto_approved = frozenset(auto_approved)

    def evaluate(
        self,
        capability: str,
        arguments: Mapping[str, Any],
        *,
        summary: str = "",
    ) -> PolicyDecision:
        arguments = dict(arguments)
        risk = classify(capability, arguments)

        if risk in self.forbidden:
            return PolicyDecision(
                DENY, risk, f"{risk.replace('_', ' ')} actions are turned off."
            )

        if risk in ALWAYS_CONFIRMED:



            return PolicyDecision(
                REQUIRE_APPROVAL,
                risk,
                (
                    "This sends something on your behalf and cannot be undone."
                    if risk == RISK_SEND_EXTERNAL
                    else "This involves money."
                ),
                self._request(capability, risk, summary, arguments),
            )

        if risk == RISK_READ:
            return PolicyDecision(ALLOW, risk, "Reading changes nothing.")

        if risk == RISK_WRITE_LOCAL:




            return PolicyDecision(ALLOW, risk, "Covered by starting this task.")

        if self.authority_mode == "full_access":
            return PolicyDecision(ALLOW, risk, "Full access is enabled.")

        if capability in self.auto_approved:
            return PolicyDecision(ALLOW, risk, f"{capability} is pre-approved.")

        return PolicyDecision(
            REQUIRE_APPROVAL,
            risk,
            "This permanently deletes or overwrites something.",
            self._request(capability, risk, summary, arguments),
        )

    def _request(
        self, capability: str, risk: str, summary: str, arguments: Mapping[str, Any]
    ) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=str(uuid.uuid4()),
            capability=capability,
            risk=risk,
            summary=summary or f"{capability} ({risk.replace('_', ' ')})",
            arguments=dict(arguments),
        )


def await_approval(
    decision: PolicyDecision,
    *,
    ask: Any,
    task: Any = None,
) -> bool:
    """Put an approval to the user and wait for a real answer.

    Silence is not consent. A prompt that is never answered, or a task that is
    stopped while one is open, leaves the action unperformed.
    """

    from ..chat.task_runtime import EXECUTING, WAITING

    if decision.request is None:
        return decision.allowed

    request = decision.request
    if task is not None:
        task.transition(WAITING, waiting_for="approval", risk=decision.risk)
        task.record_event("approval_requested", **request.to_dict())

    try:
        answer = ask(request.to_dict())
    except Exception as error:
        answer = False
        if task is not None:
            task.record_event("approval_failed", error=str(error))



    approved = answer is True

    if task is not None:
        task.record_event(
            "approval_answered", request_id=request.request_id, approved=approved
        )
        if not task.finished and not task.stop_requested:
            task.transition(EXECUTING, resumed_after="approval")
    return approved
