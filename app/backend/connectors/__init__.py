"""Salty Steak Native Desktop AI Platform — structured service connectors."""

from .batch import BatchReport, collect, iterate_pages, run_batch, summarise_for_model
from .contract import (
    AUTH_AVAILABLE,
    AUTH_NOT_CONFIGURED,
    AUTH_REQUIRED,
    AuthenticationRequired,
    BaseConnector,
    Connector,
    ConnectorDescriptor,
    ConnectorError,
    OperationResult,
    OperationSpec,
    Page,
    SERVICE_CALENDAR,
    SERVICE_MAIL,
)
from .local_calendar import LocalCalendarConnector
from .local_mail import LocalMailConnector
from .manager import ApprovalRequired, ConnectorDenied, ConnectorManager

__all__ = [
    "AUTH_AVAILABLE",
    "AUTH_NOT_CONFIGURED",
    "AUTH_REQUIRED",
    "ApprovalRequired",
    "AuthenticationRequired",
    "BaseConnector",
    "BatchReport",
    "Connector",
    "ConnectorDenied",
    "ConnectorDescriptor",
    "ConnectorError",
    "ConnectorManager",
    "LocalCalendarConnector",
    "LocalMailConnector",
    "OperationResult",
    "OperationSpec",
    "Page",
    "SERVICE_CALENDAR",
    "SERVICE_MAIL",
    "collect",
    "iterate_pages",
    "run_batch",
    "summarise_for_model",
]
