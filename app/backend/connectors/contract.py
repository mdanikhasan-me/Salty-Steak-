"""Salty Steak Native Desktop AI Platform — service connector contract.

The structured way to reach a service, and the tier that outranks the browser.

Driving a mail client through its web interface means reading a rendered page
to find out what the service already knows exactly. When a service exposes a
real interface, using it is faster, more reliable, and produces answers the
runtime can verify rather than infer from a screenshot. So the execution order
is: connector, then browser, then native, then UI Automation, then vision, then
raw input.

A connector describes itself. The model is told which accounts exist, which
operations they support and what each operation would cost in consequence — and
is told none of the secrets that make them work. Provider specifics live behind
this contract: nothing in the agent loop knows what a mailbox is.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..automation.policy import (
    RISK_DESTRUCTIVE,
    RISK_FINANCIAL,
    RISK_READ,
    RISK_SEND_EXTERNAL,
    RISK_WRITE_LOCAL,
)

CONNECTOR_SCHEMA = "salty-steak-connector-v1"



OPERATION_SEARCH = "search"
OPERATION_LIST = "list"
OPERATION_GET = "get"
OPERATION_CREATE = "create"
OPERATION_UPDATE = "update"
OPERATION_DELETE = "delete"
OPERATION_BATCH = "batch"
OPERATION_WATCH = "watch"

CORE_OPERATIONS = (
    OPERATION_SEARCH,
    OPERATION_LIST,
    OPERATION_GET,
    OPERATION_CREATE,
    OPERATION_UPDATE,
    OPERATION_DELETE,
    OPERATION_BATCH,
    OPERATION_WATCH,
)


SERVICE_MAIL = "mail"
SERVICE_CALENDAR = "calendar"
SERVICE_FILES = "files"
SERVICE_CONTACTS = "contacts"
SERVICE_TASKS = "tasks"


AUTH_AVAILABLE = "available"
AUTH_REQUIRED = "required"
AUTH_EXPIRED = "expired"
AUTH_NOT_CONFIGURED = "not_configured"


class ConnectorError(RuntimeError):
    """Raised when an operation cannot be completed."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


class AuthenticationRequired(ConnectorError):
    """Raised when the user must sign in before this can proceed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="authentication_required")


@dataclass(frozen=True)
class OperationSpec:
    """One thing a connector can do, and what doing it would mean.

    The risk is declared here, once, by the connector that owns the operation.
    Classifying "send mail" separately inside every provider is how two
    providers end up disagreeing about whether sending needs approval.
    """

    name: str
    summary: str
    risk: str = RISK_READ
    arguments: Mapping[str, str] = field(default_factory=dict)
    required: Sequence[str] = ()
    paginated: bool = False
    batchable: bool = False



    verify_with: str | None = None

    @property
    def mutating(self) -> bool:
        return self.risk != RISK_READ

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.name,
            "summary": self.summary,
            "risk": self.risk,
            "arguments": dict(self.arguments),
            "required": list(self.required),
            "paginated": self.paginated,
            "batchable": self.batchable,
            "mutating": self.mutating,
            "verify_with": self.verify_with,
        }


@dataclass(frozen=True)
class ConnectorDescriptor:
    """What a connector is, in terms the model may see.

    Everything here is safe to put in a prompt. Nothing here can authenticate.
    """

    connector_id: str
    service_type: str
    display_name: str
    account: str
    credential: str | None = None
    supports_watch: bool = False

    def to_dict(self, *, authentication: str, operations: Sequence[OperationSpec]) -> dict[str, Any]:
        return {
            "schema": CONNECTOR_SCHEMA,
            "connector": self.connector_id,
            "service_type": self.service_type,
            "display_name": self.display_name,


            "account": self.account,
            "authentication": authentication,
            "available": authentication == AUTH_AVAILABLE,
            "supports_watch": self.supports_watch,
            "operations": [spec.to_dict() for spec in operations],
            "secret_visible_to_model": False,
        }


@dataclass
class Page:
    """One page of results, and how to ask for the next."""

    items: list[dict[str, Any]]
    next_cursor: str | None = None
    total_estimate: int | None = None

    @property
    def exhausted(self) -> bool:
        return self.next_cursor is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "count": len(self.items),
            "next_cursor": self.next_cursor,
            "total_estimate": self.total_estimate,
        }


@dataclass
class OperationResult:
    """What an operation did."""

    status: str
    operation: str
    connector: str
    data: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    at: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connector": self.connector,
            "operation": self.operation,
            "verified": self.verified,
            **self.data,
        }


class Connector(Protocol):
    """What every service connector must provide.

    A future Gmail or Google Calendar provider implements exactly this, which
    is why the deterministic connectors used in tests are not mocks: they are
    the same contract with a local store behind them.
    """

    descriptor: ConnectorDescriptor

    def operations(self) -> Sequence[OperationSpec]:
        """Everything this connector can do."""

    def authentication_state(self) -> str:
        """Whether this connector could act right now."""

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> OperationResult:
        """Carry out one operation. Never called without a policy decision."""


class BaseConnector:
    """Shared behaviour so a provider only writes what is provider-specific."""

    descriptor: ConnectorDescriptor

    def __init__(self, descriptor: ConnectorDescriptor, *, vault: Any = None) -> None:
        self.descriptor = descriptor
        self._vault = vault

    def operations(self) -> Sequence[OperationSpec]:
        raise NotImplementedError

    def operation(self, name: str) -> OperationSpec:
        for spec in self.operations():
            if spec.name == name:
                return spec
        raise ConnectorError(
            f"{self.descriptor.connector_id} has no operation {name!r}. "
            f"It supports: {', '.join(spec.name for spec in self.operations())}",
            kind="unknown_operation",
        )

    def authentication_state(self) -> str:
        """Authentication is a state, never a value.

        The credential is resolved at the moment of use and nowhere else, so
        the answer here can be shown to the model safely.
        """

        if self.descriptor.credential is None:
            return AUTH_AVAILABLE
        if self._vault is None:
            return AUTH_NOT_CONFIGURED
        try:
            return (
                AUTH_AVAILABLE
                if self._vault.available(self.descriptor.credential)
                else AUTH_REQUIRED
            )
        except Exception:
            return AUTH_NOT_CONFIGURED

    def describe(self) -> dict[str, Any]:
        return self.descriptor.to_dict(
            authentication=self.authentication_state(),
            operations=self.operations(),
        )

    def require_secret(self) -> str:
        """Resolve the credential, at the point of use, for one call.

        The value is returned to the connector's own transport and must not be
        placed in a result, an observation, an event or a log line.
        """

        if self.descriptor.credential is None:
            raise ConnectorError("This connector needs no credential.")
        if self._vault is None:
            raise AuthenticationRequired(
                f"{self.descriptor.display_name} is not signed in yet."
            )
        from ..automation.credentials import CredentialError

        try:
            return self._vault.resolve_for_use(self.descriptor.credential)
        except CredentialError as error:
            raise AuthenticationRequired(str(error)) from error

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> OperationResult:
        spec = self.operation(operation)
        state = self.authentication_state()
        if state != AUTH_AVAILABLE:
            raise AuthenticationRequired(
                f"{self.descriptor.display_name} needs you to sign in "
                f"({state.replace('_', ' ')})."
            )
        handler = getattr(self, f"op_{operation}", None)
        if handler is None:
            raise ConnectorError(
                f"{self.descriptor.connector_id} declares {operation!r} but does "
                "not implement it.",
                kind="not_implemented",
            )
        data = handler(dict(arguments))
        return OperationResult(
            status="succeeded",
            operation=spec.name,
            connector=self.descriptor.connector_id,
            data=data if isinstance(data, dict) else {"value": data},
        )


def risk_for(connector: BaseConnector, operation: str) -> str:
    """The declared consequence of an operation, for the policy engine."""

    return connector.operation(operation).risk


__all__ = [
    "AUTH_AVAILABLE",
    "AUTH_EXPIRED",
    "AUTH_NOT_CONFIGURED",
    "AUTH_REQUIRED",
    "AuthenticationRequired",
    "BaseConnector",
    "CORE_OPERATIONS",
    "Connector",
    "ConnectorDescriptor",
    "ConnectorError",
    "OperationResult",
    "OperationSpec",
    "Page",
    "RISK_DESTRUCTIVE",
    "RISK_FINANCIAL",
    "RISK_READ",
    "RISK_SEND_EXTERNAL",
    "RISK_WRITE_LOCAL",
    "SERVICE_CALENDAR",
    "SERVICE_CONTACTS",
    "SERVICE_FILES",
    "SERVICE_MAIL",
    "SERVICE_TASKS",
    "risk_for",
]
