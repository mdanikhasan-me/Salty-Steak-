from __future__ import annotations

import json

import pytest

from app.backend.automation.policy import (
    RISK_DESTRUCTIVE,
    RISK_READ,
    RISK_SEND_EXTERNAL,
    PolicyEngine,
)
from app.backend.connectors import (
    ApprovalRequired,
    AuthenticationRequired,
    ConnectorDescriptor,
    ConnectorError,
    ConnectorManager,
    LocalCalendarConnector,
    LocalMailConnector,
    collect,
    iterate_pages,
    run_batch,
)
from app.backend.connectors.contract import SERVICE_MAIL


@pytest.fixture()
def mail() -> LocalMailConnector:
    connector = LocalMailConnector()
    connector.seed(
        [
            {"id": f"m{index}", "from": "sam@northwind.example", "subject": f"Atlas {index}"}
            for index in range(120)
        ]
        + [{"id": "promo", "from": "deals@shop.example", "subject": "50% off"}]
    )
    return connector


@pytest.fixture()
def manager(mail) -> ConnectorManager:
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(mail)
    registry.register(LocalCalendarConnector())
    return registry


# ------------------------------------------------------------------ metadata


def test_the_model_is_told_which_accounts_exist_and_no_secrets(manager) -> None:
    manifest = manager.manifest()

    mailbox = next(c for c in manifest["connectors"] if c["connector"] == "mail.local")
    assert mailbox["account"] == "local@salty.test"
    assert mailbox["authentication"] == "available"
    assert mailbox["secret_visible_to_model"] is False
    # Nothing in the manifest could authenticate anybody. The manifest does
    # say secret_visible_to_model, so this looks for credential values rather
    # than for the word.
    text = json.dumps(manifest).casefold()
    for forbidden in ("bearer ", "password", "cookie", "refresh_token", "api_key"):
        assert forbidden not in text


def test_operations_declare_their_own_consequence(manager) -> None:
    mailbox = manager.get("mail.local")

    assert mailbox.operation("search").risk == RISK_READ
    assert mailbox.operation("send").risk == RISK_SEND_EXTERNAL
    assert mailbox.operation("delete").risk == RISK_DESTRUCTIVE
    # A mutating operation says how it can be checked afterwards.
    assert mailbox.operation("apply_label").verify_with == "search"


def test_the_routing_hint_names_the_arguments_a_plan_must_supply(manager) -> None:
    # A model-authored plan that called apply_label without a label was
    # rejected by validation after a whole generation had been spent on it.
    # The hint the model reads now names what each operation needs.
    line = next(line for line in manager.orchestration_hints() if line.startswith("mail.local:"))

    assert "apply_label(ids,label)" in line
    assert "send(to,subject)" in line
    # Read operations with no required argument stay bare, and destruction is
    # still marked where it was.
    assert "search," in line
    assert "delete(ids)*" in line
    # It sits in front of every chat turn, so it stays a line, not a schema.
    assert len(line) < 400


def test_a_connector_can_be_found_by_what_it_is(manager) -> None:
    found = manager.of_type(SERVICE_MAIL)
    assert [connector.descriptor.connector_id for connector in found] == ["mail.local"]


def test_an_unknown_connector_or_operation_says_what_exists(manager) -> None:
    with pytest.raises(ConnectorError, match="mail.local"):
        manager.get("mail.gmail")
    with pytest.raises(ConnectorError, match="It supports"):
        manager.get("mail.local").operation("nuke")


# -------------------------------------------------------------------- policy


def test_sending_needs_approval_even_under_full_access(manager) -> None:
    with pytest.raises(ApprovalRequired):
        manager.invoke("mail.local", "send", {"to": "sam@x.test", "subject": "hi"})

    # Nothing was sent.
    assert manager.invoke("mail.local", "list_sent", {}).data["count"] == 0


def test_deleting_needs_approval_under_ask_every_time() -> None:
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="ask_every_time"))
    connector = LocalMailConnector()
    connector.seed([{"id": "m1"}])
    registry.register(connector)

    with pytest.raises(ApprovalRequired):
        registry.invoke("mail.local", "delete", {"ids": ["m1"]})
    assert connector.message_count == 1


def test_an_approved_send_proceeds_and_is_verifiable() -> None:
    registry = ConnectorManager(
        policy=PolicyEngine(), approve=lambda _request: True
    )
    registry.register(LocalMailConnector())

    registry.invoke("mail.local", "send", {"to": "sam@x.test", "subject": "hi"})

    verified = registry.verify("mail.local", "send", {})
    assert verified.data["count"] == 1
    assert verified.data["items"][0]["subject"] == "hi"


def test_a_connector_cannot_talk_its_own_risk_down(manager) -> None:
    # A model-supplied argument must never be able to reclassify a send.
    with pytest.raises(ApprovalRequired):
        manager.invoke(
            "mail.local",
            "send",
            {"to": "sam@x.test", "subject": "hi", "declared_risk": "read"},
        )


def test_reading_never_requires_approval(manager) -> None:
    assert manager.invoke("mail.local", "search", {"limit": 5}).succeeded


def test_mail_search_understands_common_from_and_subject_field_syntax() -> None:
    registry = ConnectorManager(policy=PolicyEngine())
    mail = LocalMailConnector()
    mail.seed(
        [
            {
                "id": "atlas",
                "from": "sam.okafor@example.com",
                "subject": "Project Atlas update",
            },
            {"id": "other", "from": "lee@example.com", "subject": "Project Atlas"},
        ]
    )
    registry.register(mail)

    result = registry.invoke(
        "mail.local",
        "search",
        {"query": 'from:Sam subject:"Project Atlas"'},
    )

    assert [item["id"] for item in result.data["items"]] == ["atlas"]


# ------------------------------------------------------------- authentication


def test_an_unconfigured_connector_refuses_to_act_and_says_why() -> None:
    connector = LocalMailConnector(
        ConnectorDescriptor(
            connector_id="mail.cloud",
            service_type=SERVICE_MAIL,
            display_name="Cloud Mail",
            account="anik@example.com",
            credential="cloud-mail",
        )
    )
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(connector)

    described = connector.describe()
    assert described["authentication"] == "not_configured"
    assert described["available"] is False
    assert "anik@example.com" in json.dumps(described)

    with pytest.raises(AuthenticationRequired, match="sign in"):
        registry.invoke("mail.cloud", "search", {})


# ---------------------------------------------------------------- pagination


def test_a_large_result_is_walked_a_page_at_a_time(manager) -> None:
    pages: list[int] = []

    found = list(
        iterate_pages(
            manager,
            "mail.local",
            "search",
            {"from": "sam@northwind.example"},
            page_size=25,
            on_page=lambda number, page: pages.append(len(page.items)),
        )
    )

    assert len(found) == 120
    # Five pages of 25, not one call returning everything.
    assert pages == [25, 25, 25, 25, 20]
    assert len({item["id"] for item in found}) == 120


def test_paging_can_stop_early_without_reading_the_rest(manager) -> None:
    found = list(
        iterate_pages(manager, "mail.local", "search", {}, page_size=10, max_items=15)
    )
    assert len(found) == 15


def test_a_stopped_task_stops_paging(manager) -> None:
    found = list(
        iterate_pages(manager, "mail.local", "search", {}, should_stop=lambda: True)
    )
    assert found == []


# --------------------------------------------------------------------- batch


def test_hundreds_of_records_are_changed_in_a_handful_of_calls(manager, mail) -> None:
    manager.invoke("mail.local", "create_label", {"name": "Project Atlas"})
    identifiers = [
        item["id"]
        for item in collect(manager, "mail.local", "search", {"from": "sam@northwind.example"})
    ]

    report = run_batch(
        manager,
        "mail.local",
        "apply_label",
        identifiers,
        {"label": "Project Atlas"},
        batch_size=50,
    )

    assert len(report.succeeded) == 120
    assert report.complete is True
    # The decisive number: 120 messages changed in 3 connector calls.
    assert report.batches == 3

    labelled = collect(manager, "mail.local", "search", {"label": "Project Atlas"})
    assert len(labelled) == 120


def test_a_failure_partway_through_keeps_the_work_already_done(manager) -> None:
    manager.invoke("mail.local", "create_label", {"name": "Keep"})
    identifiers = [f"m{index}" for index in range(60)] + ["ghost-1", "ghost-2"]

    report = run_batch(
        manager, "mail.local", "apply_label", identifiers, {"label": "Keep"}, batch_size=25
    )

    assert len(report.succeeded) == 60
    assert [entry["id"] for entry in report.failed] == ["ghost-1", "ghost-2"]
    assert report.complete is False
    # The 60 that worked are really labelled: nothing was rolled back.
    assert len(collect(manager, "mail.local", "search", {"label": "Keep"})) == 60


def test_stopping_a_batch_reports_exactly_what_was_not_attempted(manager) -> None:
    manager.invoke("mail.local", "create_label", {"name": "Half"})
    identifiers = [f"m{index}" for index in range(100)]
    done = {"batches": 0}

    def stop_after_two_batches() -> bool:
        # Counts completed batches rather than calls, because the manager also
        # consults this before and after each operation.
        return done["batches"] >= 2

    report = run_batch(
        manager,
        "mail.local",
        "apply_label",
        identifiers,
        {"label": "Half"},
        batch_size=25,
        should_stop=stop_after_two_batches,
        on_progress=lambda _report: done.__setitem__("batches", done["batches"] + 1),
    )

    assert report.cancelled is True
    assert len(report.succeeded) == 50
    # Nothing is reported as failed: a stop means not attempted, and every
    # record is accounted for so a resumed task knows where to pick up.
    assert report.failed == []
    assert len(report.succeeded) + len(report.skipped) == 100
    assert report.skipped[0] == "m50"


# ------------------------------------------------------------------ calendar


def test_the_calendar_refuses_to_double_book_silently() -> None:
    calendar = LocalCalendarConnector()
    calendar.seed(
        [
            {
                "title": "Existing",
                "start": "2026-08-20T14:00:00",
                "end": "2026-08-20T16:00:00",
            }
        ]
    )
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(calendar)

    with pytest.raises(ConnectorError) as failure:
        registry.invoke(
            "calendar.local",
            "create_event",
            {
                "title": "New",
                "start": "2026-08-20T15:00:00",
                "end": "2026-08-20T15:30:00",
            },
        )
    assert failure.value.kind == "conflict"
    assert "Existing" in str(failure.value)
    assert calendar.event_count == 1


def test_free_busy_reports_the_gaps_between_events() -> None:
    calendar = LocalCalendarConnector()
    calendar.seed(
        [
            {"title": "A", "start": "2026-08-20T09:00:00", "end": "2026-08-20T10:00:00"},
            {"title": "B", "start": "2026-08-20T14:00:00", "end": "2026-08-20T16:00:00"},
        ]
    )
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(calendar)

    result = registry.invoke(
        "calendar.local",
        "free_busy",
        {"start": "2026-08-20T08:00:00", "end": "2026-08-20T18:00:00"},
    ).data

    assert len(result["busy"]) == 2
    gaps = {(item["start"], item["end"]) for item in result["free"]}
    assert ("2026-08-20T10:00:00", "2026-08-20T14:00:00") in gaps
    assert ("2026-08-20T16:00:00", "2026-08-20T18:00:00") in gaps


def test_a_weekly_class_is_one_event_expanded_over_the_term() -> None:
    calendar = LocalCalendarConnector()
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(calendar)

    registry.invoke(
        "calendar.local",
        "create_event",
        {
            "title": "Databases",
            "start": "2026-09-01T10:00:00",
            "end": "2026-09-01T11:30:00",
            "repeat_weekly_on": ["tuesday", "thursday"],
            "repeat_until": "2026-09-30T23:59:00",
        },
    )

    # One stored event, not many copies.
    assert calendar.event_count == 1
    occurrences = registry.invoke(
        "calendar.local",
        "list_events",
        {"start": "2026-09-01T00:00:00", "end": "2026-09-30T23:59:00"},
    ).data["items"]
    assert len(occurrences) == 9
    assert all(item["recurring"] for item in occurrences)


def test_a_repeating_event_clashes_on_a_later_week_not_only_the_first() -> None:
    calendar = LocalCalendarConnector()
    calendar.seed(
        [
            {
                "title": "Dentist",
                "start": "2026-09-17T10:00:00",
                "end": "2026-09-17T11:00:00",
            }
        ]
    )
    registry = ConnectorManager(policy=PolicyEngine(authority_mode="full_access"))
    registry.register(calendar)

    # The first Thursday is free; the third is not. Checking only the first
    # occurrence would book straight over the appointment.
    with pytest.raises(ConnectorError) as failure:
        registry.invoke(
            "calendar.local",
            "create_event",
            {
                "title": "Databases",
                "start": "2026-09-03T10:00:00",
                "end": "2026-09-03T11:30:00",
                "repeat_weekly_on": ["thursday"],
                "repeat_until": "2026-09-30T23:59:00",
            },
        )
    assert failure.value.kind == "conflict"
    assert "Dentist" in str(failure.value)
