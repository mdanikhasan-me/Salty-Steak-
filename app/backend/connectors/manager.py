"""Salty Steak Native Desktop AI Platform — connector registry and execution.

One place that knows which services exist, and the only place that runs them.

Every connector mutation passes through the policy engine here rather than
inside each provider. A provider that classified its own risk would eventually
disagree with another provider about whether sending needs approval, and the
weakest one would set the standard for the whole application. So the operation
declares its consequence, the manager asks policy, and the connector never sees
the decision at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..automation.credentials import redact
from ..automation.policy import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    PolicyEngine,
    await_approval,
)
from .contract import (
    AUTH_AVAILABLE,
    AuthenticationRequired,
    BaseConnector,
    ConnectorError,
    OperationResult,
)

MANAGER_SCHEMA = "salty-steak-connector-manager-v1"


class ConnectorDenied(ConnectorError):
    """Raised when policy refused an operation outright."""

    def __init__(self, message: str) -> None:
        super().__init__(message, kind="denied")


class ApprovalRequired(ConnectorError):
    """Raised when the user must approve before this can run."""

    def __init__(self, message: str, *, request: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, kind="approval_required")
        self.request = dict(request or {})


class ConnectorManager:
    """Own the connectors and enforce policy on the way through."""

    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        approve: Any = None,
        task: Any = None,
    ) -> None:
        self._connectors: dict[str, BaseConnector] = {}
        self.policy = policy or PolicyEngine()
        self.approve = approve
        self.task = task



    def register(self, connector: BaseConnector) -> BaseConnector:
        self._connectors[connector.descriptor.connector_id] = connector
        return connector

    def unregister(self, connector_id: str) -> bool:
        return self._connectors.pop(connector_id, None) is not None

    def get(self, connector_id: str) -> BaseConnector:
        connector = self._connectors.get(connector_id)
        if connector is None:
            known = ", ".join(sorted(self._connectors)) or "none"
            raise ConnectorError(
                f"There is no connector called {connector_id!r}. Configured: {known}",
                kind="unknown_connector",
            )
        return connector

    def of_type(self, service_type: str) -> list[BaseConnector]:
        """Every connector for a kind of service, so a plan can ask for
        "a calendar" without naming a provider."""

        return [
            connector
            for connector in self._connectors.values()
            if connector.descriptor.service_type == service_type
        ]



    def catalogue(self, *, configured_only: bool = False) -> list[dict[str, Any]]:
        """What the model is told about the services it can reach.

        Safe by construction: a descriptor carries an account name and an
        authentication state, never a token, cookie or password.
        """

        entries = []
        for connector in sorted(self._connectors.values(), key=lambda item: item.descriptor.connector_id):
            described = connector.describe()
            if configured_only and not described["available"]:
                continue
            entries.append(described)
        return entries

    def orchestration_hints(self) -> list[str]:
        """One compact line per configured service, for the routing prompt.

        Naming a service without saying what it does tells the model a mailbox
        exists but not that it can search or label one, so it reaches for the
        browser instead. Operations are listed bare, destructive ones marked,
        and the real schema stays in the connector where it belongs.
        """

        lines: list[str] = []
        for connector in sorted(
            self._connectors.values(), key=lambda item: item.descriptor.connector_id
        ):
            if connector.authentication_state() != AUTH_AVAILABLE:
                continue
            names = []
            for spec in connector.operations():
                names.append(f"{spec.name}*" if spec.risk == "destructive" else spec.name)
            lines.append(f"{connector.descriptor.connector_id}: {', '.join(names)}")
        return lines

    def manifest(self) -> dict[str, Any]:
        catalogue = self.catalogue()
        return {
            "schema": MANAGER_SCHEMA,
            "connectors": catalogue,
            "configured": [item["connector"] for item in catalogue if item["available"]],
            "count": len(catalogue),
        }



    def evaluate(self, connector_id: str, operation: str, arguments: Mapping[str, Any]):
        """The policy decision for an operation, without running it."""

        connector = self.get(connector_id)
        spec = connector.operation(operation)
        return self.policy.evaluate(
            f"connector.{connector.descriptor.service_type}.{operation}",
            {**dict(arguments), "declared_risk": spec.risk},
            summary=f"{spec.summary} ({connector.descriptor.account})",
        )

    def invoke(
        self,
        connector_id: str,
        operation: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        should_stop: Any = None,
    ) -> OperationResult:
        """Run one operation, after policy has agreed to it."""

        arguments = dict(arguments or {})
        connector = self.get(connector_id)
        spec = connector.operation(operation)



        if self._stopped(should_stop):
            raise ConnectorError("The task was stopped.", kind="cancelled")

        decision = self.evaluate(connector_id, operation, arguments)
        if decision.outcome == DENY:
            raise ConnectorDenied(decision.reason)
        if decision.outcome == REQUIRE_APPROVAL:
            approved = (
                await_approval(decision, ask=self.approve, task=self.task)
                if self.approve is not None
                else False
            )
            if not approved:
                raise ApprovalRequired(
                    f"{spec.summary} needs your approval: {decision.reason}",
                    request=decision.request.to_dict() if decision.request else None,
                )
        if self._stopped(should_stop):
            raise ConnectorError("The task was stopped.", kind="cancelled")

        result = connector.invoke(operation, arguments)
        if self.task is not None:
            self.task.record_event(
                "connector",
                connector=connector_id,
                operation=operation,
                risk=spec.risk,
                status=result.status,


                arguments=redact(arguments),
            )
            self.task.metrics.record_tier("api")
        return result

    def verify(
        self, connector_id: str, operation: str, arguments: Mapping[str, Any]
    ) -> OperationResult:
        """Re-read state through the operation the spec names for checking.

        A success code says the service accepted a request. It does not say the
        state the user asked for is now true, which is the only thing worth
        reporting back.
        """

        connector = self.get(connector_id)
        spec = connector.operation(operation)
        if not spec.verify_with:
            raise ConnectorError(
                f"{operation!r} declares no way to verify itself.",
                kind="not_verifiable",
            )

        return connector.invoke(spec.verify_with, dict(arguments))

    def authenticated(self, connector_id: str) -> bool:
        return self.get(connector_id).authentication_state() == AUTH_AVAILABLE

    @staticmethod
    def _stopped(should_stop: Any) -> bool:
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception:
            return False


__all__ = [
    "ApprovalRequired",
    "ConnectorDenied",
    "ConnectorManager",
    "MANAGER_SCHEMA",
]
