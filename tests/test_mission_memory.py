from __future__ import annotations

import json
import sqlite3

import pytest

from app.backend.memory import AutomationMissionMemory


def test_five_principals_keep_recurring_items_and_actions_separate(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        item_keys = set()
        for index in range(5):
            principal = memory.identify_principal(
                service="owned-test-app",
                label=f"Test account {index + 1}",
                stable_id=f"account-{index + 1}",
                source="test_fixture",
            )
            location = memory.checkpoint_location(
                service="owned-test-app",
                principal_key=principal["principal_key"],
                container="Daily tasks",
                location="Rewards",
                location_kind="page",
            )
            item = memory.observe_item(
                service="owned-test-app",
                principal_key=principal["principal_key"],
                location_key=location["location_key"],
                item_kind="recurring_event",
                fingerprint_source="daily-2026-08-22",
                title="Daily reward 2026-08-22",
                state="active",
                recurrence="daily",
                evidence={"label": "Available"},
                verified=True,
            )
            item_keys.add(item["item_key"])
            first = memory.record_action(
                item_key=item["item_key"],
                action="enter",
                outcome="verified",
                authority="user_confirmed",
                audit_id=f"audit-{index}",
                evidence={"state_after": "Entered"},
            )
            second = memory.record_action(
                item_key=item["item_key"],
                action="enter",
                outcome="verified",
                authority="user_confirmed",
                audit_id=f"audit-repeat-{index}",
            )
            assert first["skip"] is False
            assert second["skip"] is True
            assert memory.should_skip_action(item["item_key"], "enter") is True

        assert len(item_keys) == 5
        assert memory.statistics()["mission_principals"] == 5
        assert memory.statistics()["mission_items"] == 5
        # The verified-action partial unique index preserves one proof per
        # account/item/action even when a second run asks to repeat it.
        assert memory.statistics()["mission_actions"] == 5
    finally:
        memory.close()


def test_new_recurrence_gets_a_new_fingerprint_instead_of_reusing_old_state(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        principal = memory.identify_principal(service="lab", label="Account A")
        location = memory.checkpoint_location(
            service="lab",
            principal_key=principal["principal_key"],
            container="Project",
            location="Recurring tasks",
        )
        today = memory.observe_item(
            service="lab",
            principal_key=principal["principal_key"],
            location_key=location["location_key"],
            item_kind="daily",
            fingerprint_source="2026-08-22",
            title="Daily item",
            state="completed",
        )
        tomorrow = memory.observe_item(
            service="lab",
            principal_key=principal["principal_key"],
            location_key=location["location_key"],
            item_kind="daily",
            fingerprint_source="2026-08-23",
            title="Daily item",
            state="active",
        )

        assert today["item_key"] != tomorrow["item_key"]
        assert memory.should_skip_action(tomorrow["item_key"], "enter") is False
    finally:
        memory.close()


def test_requirement_preflight_distinguishes_met_unmet_and_unknown(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        principal = memory.identify_principal(service="lab", label="Account A")
        location = memory.checkpoint_location(
            service="lab",
            principal_key=principal["principal_key"],
            container="Project",
            location="Tasks",
        )
        item = memory.observe_item(
            service="lab",
            principal_key=principal["principal_key"],
            location_key=location["location_key"],
            item_kind="recurring_event",
            fingerprint_source="criteria-item",
            title="Criteria item",
            state="active",
            requirements=[
                {"key": "verified", "operator": "equals", "expected": True},
                {"key": "level", "operator": "minimum", "expected": 5},
                {"key": "roles", "operator": "contains", "expected": "member"},
            ],
        )

        eligible = memory.preflight_item(
            item["item_key"],
            facts={"verified": True, "level": 7, "roles": ["member"]},
        )
        assert eligible["disposition"] == "eligible"
        assert {check["status"] for check in eligible["checks"]} == {"met"}

        ineligible = memory.preflight_item(
            item["item_key"],
            facts={"verified": True, "level": 2, "roles": ["member"]},
        )
        assert ineligible["disposition"] == "ineligible"
        assert any(check["status"] == "unmet" for check in ineligible["checks"])

        unknown = memory.preflight_item(
            item["item_key"], facts={"verified": True, "level": 7}
        )
        assert unknown["disposition"] == "incomplete_evidence"
        assert any(check["status"] == "unknown" for check in unknown["checks"])
    finally:
        memory.close()


def test_preflight_supports_maximum_and_blocks_verified_action_until_eligible(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        principal = memory.identify_principal(service="lab", label="Account A")
        location = memory.checkpoint_location(
            service="lab",
            principal_key=principal["principal_key"],
            container="Project",
            location="Tasks",
        )
        item = memory.observe_item(
            service="lab",
            principal_key=principal["principal_key"],
            location_key=location["location_key"],
            item_kind="recurring_event",
            fingerprint_source="one-entry-item",
            title="One entry per account",
            state="active",
            requirements=[
                {
                    "key": "prior_verified_entries",
                    "operator": "maximum",
                    "expected": 0,
                }
            ],
        )

        with pytest.raises(PermissionError, match="mission preflight"):
            memory.record_action(
                item_key=item["item_key"],
                action="enter",
                outcome="verified",
                authority="user_confirmed",
            )

        eligible = memory.preflight_item(
            item["item_key"], facts={"prior_verified_entries": 0}
        )
        assert eligible["disposition"] == "eligible"
        recorded = memory.record_action(
            item_key=item["item_key"],
            action="enter",
            outcome="verified",
            authority="user_confirmed",
        )
        assert recorded["skip"] is False

        duplicate = memory.preflight_item(
            item["item_key"], facts={"prior_verified_entries": 1}
        )
        assert duplicate["disposition"] == "ineligible"
    finally:
        memory.close()


def test_discord_observer_records_location_and_explicit_ended_evidence(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        selected = memory.observe_step(
            task_id="task-1",
            capability="ui.automation",
            arguments={
                "command": "invoke",
                "name": "giveaways, Text Channel, Example Server",
            },
            result={"status": "succeeded", "audit_record_id": "audit-select"},
            world_state={"active_window": {"title": "Friends - Discord"}},
        )
        assert selected["locations_observed"][0]["container"] == "Example Server"

        observed = memory.observe_step(
            task_id="task-1",
            capability="ui.automation",
            arguments={"command": "get_tree", "name": "Messages in giveaways"},
            result={
                "status": "succeeded",
                "audit_record_id": "audit-read",
                "nodes": [
                    {
                        "control_type": "ListItem",
                        "name": (
                            "GiveawayBot Verified App, Daily prize ~1 entry per IP "
                            "Ended: today (August 22, 2026 8:00 PM) "
                            "Hosted by: <@123> Entries: 42 Winners: @Winner"
                        ),
                    }
                ],
            },
            world_state={
                "active_window": {
                    "title": "#giveaways | Example Server - Discord"
                }
            },
        )

        assert observed["items_observed"][0]["state"] == "ended"
        stats = memory.statistics()
        assert stats["mission_locations"] == 1
        assert stats["mission_items"] == 1
        assert stats["mission_actions"] == 0
        briefing = memory.briefing("check Discord giveaways")
        assert "Example Server / giveaways" in briefing
        assert "state=ended" in briefing
        assert "verified_actions=none" in briefing
    finally:
        memory.close()


def test_discord_scan_batch_keeps_account_channel_item_and_requirements_together(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        observed = memory.observe_step(
            task_id="batch-task",
            capability="discord.inspect",
            arguments={"operation": "scan_batch", "cursor": 0},
            result={
                "status": "succeeded",
                "account": {
                    "label": "Test User",
                    "handle": "test.user",
                    "observed": True,
                },
                "scans": [
                    {
                        "selected_channel": {
                            "channel": "prizes",
                            "server": "Owned Test Guild",
                            "readback_channel": "prizes",
                            "readback_server": "Owned Test Guild",
                            "channel_key": "channel-key-1",
                        },
                        "coverage": {
                            "messages": {"history_boundary_reached": True}
                        },
                        "candidate_classification": "confirmed_giveaway",
                        "giveaway_items": [
                            {
                                "evidence": (
                                    "GiveawayBot Weekly Prize Ends: tomorrow "
                                    "Must have level 10 Entries: 4"
                                ),
                                "state": "active",
                                "requirements": [
                                    {
                                        "key": "level",
                                        "operator": "minimum",
                                        "expected": 10,
                                        "type": "level",
                                    }
                                ],
                                "disposition": "requirements_unverified",
                            }
                        ],
                    }
                ],
            },
            world_state={"active_window": {"title": "Friends - Discord"}},
        )

        assert len(observed["locations_observed"]) == 1
        assert len(observed["items_observed"]) == 1
        item_key = observed["items_observed"][0]["item_key"]
        assert memory.preflight_item(item_key, facts={"level": 12})[
            "disposition"
        ] == "eligible"
        assert "account Test User" in memory.briefing("Weekly Prize")
        assert memory.statistics()["mission_principals"] == 1
    finally:
        memory.close()


def test_mission_evidence_is_redacted_before_sqlite_persistence(tmp_path) -> None:
    path = tmp_path / "memory.db"
    memory = AutomationMissionMemory(path)
    principal = memory.identify_principal(service="lab", label="Account")
    location = memory.checkpoint_location(
        service="lab",
        principal_key=principal["principal_key"],
        container="Project",
        location="Page",
    )
    memory.observe_item(
        service="lab",
        principal_key=principal["principal_key"],
        location_key=location["location_key"],
        item_kind="record",
        fingerprint_source="record-1",
        title="Record",
        state="available",
        evidence={"token": "must-not-persist", "safe": "visible"},
    )
    memory.close()

    connection = sqlite3.connect(path)
    try:
        evidence = connection.execute(
            "SELECT evidence_json FROM mission_items"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "must-not-persist" not in evidence
    assert "visible" in evidence
    assert "redacted" in evidence.casefold()


def test_briefing_is_compact_even_after_many_observations(tmp_path) -> None:
    memory = AutomationMissionMemory(tmp_path / "memory.db")
    try:
        principal = memory.identify_principal(service="lab", label="Account")
        location = memory.checkpoint_location(
            service="lab",
            principal_key=principal["principal_key"],
            container="Project",
            location="Page",
        )
        for index in range(60):
            memory.observe_item(
                service="lab",
                principal_key=principal["principal_key"],
                location_key=location["location_key"],
                item_kind="record",
                fingerprint_source=f"record-{index}",
                title=f"Record {index}",
                state="available",
            )
        briefing = memory.briefing("lab records", limit=8)
        assert briefing.count("\n- ") == 8
        assert len(briefing) < 5_000
        assert "full history is in SQLite" in briefing
    finally:
        memory.close()
