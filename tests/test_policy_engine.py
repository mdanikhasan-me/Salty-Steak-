from __future__ import annotations

import json

import pytest

from app.backend.automation.policy import (
    ALLOW,
    DENY,
    REQUIRE_APPROVAL,
    RISK_DESTRUCTIVE,
    RISK_FINANCIAL,
    RISK_READ,
    RISK_SEND_EXTERNAL,
    RISK_WRITE_LOCAL,
    PolicyEngine,
    await_approval,
    classify,
)
from app.backend.chat.task_runtime import EXECUTING, WAITING, TaskContext


# ----------------------------------------------------------- classification


def test_reading_is_read_and_clicking_is_not() -> None:
    assert classify("screen.capture", {}) == RISK_READ
    assert classify("browser.control", {"command": "read_page"}) == RISK_READ
    assert classify("browser.control", {"command": "click", "element": "web-el-1"}) == (
        RISK_WRITE_LOCAL
    )


def test_a_button_that_spends_money_outranks_the_capability() -> None:
    # Clicking is ordinarily local. Clicking Place order is not.
    assert (
        classify(
            "browser.control",
            {"command": "click", "element": "web-el-9", "name": "Place order"},
        )
        == RISK_FINANCIAL
    )


def test_sending_on_the_users_behalf_is_recognised() -> None:
    assert (
        classify("browser.control", {"command": "click", "name": "Send reply"})
        == RISK_SEND_EXTERNAL
    )


def test_a_destructive_command_is_recognised_through_the_existing_check() -> None:
    assert (
        classify("terminal.execute", {"argv": ["powershell", "Remove-Item", "x"]})
        == RISK_DESTRUCTIVE
    )
    assert classify("terminal.execute", {"argv": ["git", "status"]}) == RISK_WRITE_LOCAL


def test_file_risk_comes_from_the_operation_and_recoverability() -> None:
    assert classify("files.manage", {"operation": "read"}) == RISK_READ
    assert classify("files.manage", {"operation": "copy"}) == RISK_WRITE_LOCAL
    write = {"operation": "write", "path": "note.txt", "content": "hello"}
    assert classify("files.manage", write) == RISK_WRITE_LOCAL
    assert PolicyEngine(authority_mode="ask_every_time").evaluate("files.manage", write).outcome == ALLOW
    assert PolicyEngine(authority_mode="full_access").evaluate("files.manage", write).outcome == ALLOW
    assert classify(
        "files.manage", {"operation": "delete", "permanent": False}
    ) == RISK_WRITE_LOCAL
    assert classify(
        "files.manage", {"operation": "delete", "permanent": True}
    ) == RISK_DESTRUCTIVE


# ------------------------------------------------------------------ decisions


def test_reading_never_interrupts_the_user() -> None:
    engine = PolicyEngine(authority_mode="ask_every_time")

    decision = engine.evaluate("browser.control", {"command": "read_page"})

    assert decision.outcome == ALLOW
    assert decision.request is None


def test_an_ordinary_click_does_not_interrupt_the_user() -> None:
    # Starting the task authorised the task. Asking again before every click
    # would train the user to approve without reading, which is worse than not
    # asking: the prompts that matter would stop being noticed.
    arguments = {"command": "click", "element": "web-el-1"}

    for mode in ("ask_every_time", "full_access"):
        assert PolicyEngine(authority_mode=mode).evaluate(
            "browser.control", arguments
        ).outcome == ALLOW


def test_a_destructive_command_is_held_back_until_full_access() -> None:
    arguments = {"argv": ["powershell", "Remove-Item", "-Recurse", "notes"]}

    assert PolicyEngine().evaluate("terminal.execute", arguments).outcome == (
        REQUIRE_APPROVAL
    )
    assert PolicyEngine(authority_mode="full_access").evaluate(
        "terminal.execute", arguments
    ).outcome == ALLOW


def test_a_page_merely_mentioning_checkout_is_still_only_a_read() -> None:
    # Classification keys on what the action does, not on what words happen to
    # be on the page.
    decision = PolicyEngine().evaluate(
        "browser.control", {"command": "read_page", "summary": "proceed to checkout"}
    )
    assert decision.outcome == ALLOW
    assert decision.risk == RISK_READ


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "click", "name": "Send reply"},
        {"command": "click", "name": "Place order"},
    ],
)
def test_full_access_cannot_wave_through_reaching_other_people_or_money(
    arguments: dict,
) -> None:
    # Full access says the user trusts the runtime with this computer. It does
    # not say the runtime may speak in their name or spend their money.
    decision = PolicyEngine(authority_mode="full_access").evaluate(
        "browser.control", arguments
    )

    assert decision.outcome == REQUIRE_APPROVAL
    assert decision.request is not None


def test_pre_approval_cannot_be_stretched_to_cover_sending() -> None:
    engine = PolicyEngine(auto_approved=frozenset({"browser.control"}))

    assert engine.evaluate("browser.control", {"command": "click"}).outcome == ALLOW
    assert engine.evaluate(
        "browser.control", {"command": "click", "name": "Send invoice"}
    ).outcome == REQUIRE_APPROVAL


def test_a_forbidden_risk_is_refused_rather_than_asked_about() -> None:
    engine = PolicyEngine(
        authority_mode="full_access", forbidden=frozenset({RISK_FINANCIAL})
    )

    decision = engine.evaluate("browser.control", {"command": "click", "name": "Pay now"})

    assert decision.outcome == DENY
    assert decision.request is None


# ------------------------------------------------------------------ approval


def _decision():
    return PolicyEngine().evaluate(
        "browser.control", {"command": "click", "name": "Send reply"}
    )


def test_an_approved_action_proceeds_and_the_task_resumes() -> None:
    task = TaskContext(goal="reply to Sam")
    task.transition(EXECUTING)

    assert await_approval(_decision(), ask=lambda _request: True, task=task) is True
    assert task.state == EXECUTING
    kinds = [event.kind for event in task.events]
    assert "approval_requested" in kinds and "approval_answered" in kinds


@pytest.mark.parametrize("answer", [False, None, "yes", 1, {"approved": True}, []])
def test_only_an_explicit_yes_is_permission(answer) -> None:
    # A truthy object, a string, or a missing answer must never be read as
    # consent to send something irreversible.
    assert await_approval(_decision(), ask=lambda _request: answer) is False


def test_a_failed_prompt_is_not_consent() -> None:
    task = TaskContext(goal="reply")

    def broken(_request):
        raise RuntimeError("the approval surface is gone")

    assert await_approval(_decision(), ask=broken, task=task) is False
    assert "approval_failed" in [event.kind for event in task.events]


def test_the_task_says_it_is_waiting_on_a_person() -> None:
    task = TaskContext(goal="reply to Sam")
    seen: list[str] = []

    await_approval(_decision(), ask=lambda _request: seen.append(task.state) or True, task=task)

    assert seen == [WAITING]


def test_an_approval_prompt_carries_no_secret() -> None:
    task = TaskContext(goal="sign in and reply")
    decision = PolicyEngine().evaluate(
        "browser.control",
        {"command": "set_value", "name": "Send", "password": "hunter2-secret"},
    )

    await_approval(decision, ask=lambda _request: True, task=task)

    assert "hunter2-secret" not in json.dumps(task.snapshot(include_events=True))
    assert "hunter2-secret" not in json.dumps(decision.to_dict())
