from __future__ import annotations

import json
import threading
import time

import pytest

from app.backend.automation.approvals import (
    APPROVED,
    EXPIRED,
    REJECTED,
    ApprovalBroker,
    fingerprint,
)
from app.backend.chat.task_runtime import TaskContext


def _request(broker: ApprovalBroker, **overrides):
    payload = {
        "capability": "connector.mail.send",
        "risk": "send_external",
        "summary": "Send a message to alice@example.com",
        "reason": "This sends something on your behalf and cannot be undone.",
        "arguments": {"to": "alice@example.com", "subject": "Invoice"},
        "task_id": "task-1",
        "node_id": "n3",
    }
    payload.update(overrides)
    return broker.request(**payload)


def test_the_interface_is_told_what_it_needs_to_ask_a_person() -> None:
    broker = ApprovalBroker()
    ticket = _request(broker)

    payload = ticket.to_dict()

    assert payload["task"] == "task-1"
    assert payload["node"] == "n3"
    assert payload["risk"] == "send_external"
    assert "alice@example.com" in payload["summary"]
    assert payload["state"] == "pending"
    assert payload["expires_in_seconds"] > 0


def test_a_yes_for_one_action_cannot_authorise_another() -> None:
    broker = ApprovalBroker()
    send = _request(broker)
    delete = _request(
        broker,
        capability="connector.mail.delete",
        summary="Delete 400 messages",
        arguments={"ids": ["m1", "m2"]},
        node_id="n9",
    )

    assert send.fingerprint != delete.fingerprint
    # Answering the send with the delete's fingerprint is refused.
    assert broker.approve(send.request_id, expect_fingerprint=delete.fingerprint) is False
    assert send.state == "pending"

    assert broker.approve(send.request_id, expect_fingerprint=send.fingerprint) is True
    assert send.state == APPROVED
    # The delete is untouched by the send's approval.
    assert delete.state == "pending"


def test_the_same_action_in_a_different_task_is_a_different_request() -> None:
    arguments = {"to": "alice@example.com"}
    first = fingerprint(
        capability="connector.mail.send", arguments=arguments, task_id="task-1", node_id="n1"
    )
    second = fingerprint(
        capability="connector.mail.send", arguments=arguments, task_id="task-2", node_id="n1"
    )
    assert first != second


def test_changing_the_recipient_changes_the_request() -> None:
    first = fingerprint(
        capability="connector.mail.send",
        arguments={"to": "alice@example.com"},
        task_id="t",
        node_id="n",
    )
    second = fingerprint(
        capability="connector.mail.send",
        arguments={"to": "attacker@example.com"},
        task_id="t",
        node_id="n",
    )
    assert first != second


def test_an_answered_request_cannot_be_answered_again() -> None:
    broker = ApprovalBroker()
    ticket = _request(broker)

    assert broker.approve(ticket.request_id) is True
    assert broker.reject(ticket.request_id) is False
    assert ticket.state == APPROVED


def test_silence_is_not_consent() -> None:
    broker = ApprovalBroker(timeout_seconds=0.05)
    ticket = _request(broker)

    assert broker.wait_for(ticket, poll_seconds=0.01) is False
    assert ticket.state == EXPIRED
    # An expired request cannot be revived by a late yes.
    assert broker.approve(ticket.request_id) is False


def test_an_expired_request_is_not_listed_as_pending() -> None:
    broker = ApprovalBroker(timeout_seconds=0.05)
    _request(broker)
    time.sleep(0.08)
    assert broker.pending() == []


def test_stopping_the_task_answers_the_prompt_as_no() -> None:
    broker = ApprovalBroker()
    ticket = _request(broker)

    assert broker.wait_for(ticket, should_stop=lambda: True, poll_seconds=0.01) is False
    assert ticket.state == REJECTED


def test_a_real_answer_releases_the_waiting_workflow() -> None:
    broker = ApprovalBroker()
    ticket = _request(broker)
    answers: list[bool] = []

    waiter = threading.Thread(
        target=lambda: answers.append(broker.wait_for(ticket, poll_seconds=0.01))
    )
    waiter.start()
    time.sleep(0.05)
    broker.approve(ticket.request_id, expect_fingerprint=ticket.fingerprint)
    waiter.join(timeout=2)

    assert answers == [True]


def test_the_callback_bridges_policy_onto_tickets_and_records_them() -> None:
    broker = ApprovalBroker()
    task = TaskContext(goal="send the invoice")
    ask = broker.callback(task=task)
    results: list[bool] = []

    thread = threading.Thread(
        target=lambda: results.append(
            ask(
                {
                    "capability": "connector.mail.send",
                    "risk": "send_external",
                    "summary": "Send to alice@example.com",
                    "arguments": {"to": "alice@example.com"},
                }
            )
        )
    )
    thread.start()
    # The interface can see the prompt while the workflow waits on it.
    for _ in range(50):
        pending = broker.pending()
        if pending:
            break
        time.sleep(0.02)
    assert pending and pending[0]["risk"] == "send_external"
    broker.approve(pending[0]["request_id"])
    thread.join(timeout=2)

    assert results == [True]
    kinds = [event.kind for event in task.events]
    assert "approval_required" in kinds and "approval_answered" in kinds


def test_no_secret_reaches_a_prompt_or_its_fingerprint() -> None:
    broker = ApprovalBroker()
    task = TaskContext(goal="sign in and send")
    ticket = _request(
        broker,
        arguments={"to": "alice@example.com", "password": "hunter2-secret", "token": "tok_live"},
    )
    task.record_event("approval_required", **ticket.to_dict())

    text = json.dumps([ticket.to_dict(), task.snapshot(include_events=True)])
    assert "hunter2-secret" not in text
    assert "tok_live" not in text
    # The recipient is still visible: the user has to see who this reaches.
    assert "alice@example.com" in text


def test_events_are_drained_for_the_interface_once() -> None:
    broker = ApprovalBroker()
    ticket = _request(broker)
    broker.approve(ticket.request_id)

    events = broker.drain_events()

    assert [event["event"] for event in events] == [
        "approval_required",
        "approval_answered",
    ]
    assert broker.drain_events() == []
