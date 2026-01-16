"""Salty Steak Native Desktop AI Platform — plans, execution and recovery."""

from .engine import (
    WAIT_APPROVAL,
    WAIT_REASONS,
    WAIT_SIGN_IN,
    WorkflowEngine,
    WorkflowResult,
    load_checkpoint,
    resume,
)
from .executor import Executor, NeedsUser, NodeOutcome, classify_failure
from .plan import Plan, PlanError, PlanNode, build_plan

__all__ = [
    "Executor",
    "NeedsUser",
    "NodeOutcome",
    "Plan",
    "PlanError",
    "PlanNode",
    "WAIT_APPROVAL",
    "WAIT_REASONS",
    "WAIT_SIGN_IN",
    "WorkflowEngine",
    "WorkflowResult",
    "build_plan",
    "classify_failure",
    "load_checkpoint",
    "resume",
]
