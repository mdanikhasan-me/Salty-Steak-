from __future__ import annotations

import pytest

from app.backend.automation.discord_inspector import (
    DiscordDesktopInspector,
    DiscordInspectionCancelled,
    DiscordInspectionError,
    _channel_key,
    _inventory_channel_key,
    _semantic_text,
    _discovery_priority,
)


def test_semantic_channel_text_folds_styling_and_drops_decorative_numerals() -> None:
    assert _semantic_text("〢🎁〃giveaways") == "giveaways"
    assert _semantic_text("🎉︱𝖦ɪᴠᴇᴀᴡᴀʏꜱ") == "giveaways"
    assert _semantic_text("Pokémon GO Raiders") == "pokemon go raiders"


def test_inventory_keeps_same_named_channels_in_different_servers_distinct() -> None:
    first = {"channel": "giveaways", "server": "Alpha"}
    second = {"channel": "giveaways", "server": "Beta"}
    duplicate = {"channel": "🎁︱Giveaways", "server": "Alpha"}

    assert _inventory_channel_key(first) != _inventory_channel_key(second)
    assert _inventory_channel_key(first) == _inventory_channel_key(duplicate)
    assert len({_inventory_channel_key(first), _inventory_channel_key(second)}) == 2


class _DiscordFixture:
    def __init__(self) -> None:
        self.quick_open = False
        self.query = ""
        self.active_title = "Friends - Discord"
        self.channel_result_name = "giveaways, Text Channel, Test Guild"
        self.invoke_title = "#giveaways | Test Guild - Discord"
        self.account_observed = True
        self.message_nodes = [
            (
                "GiveawayBot Test prize Ended: today "
                "~1 entry per IP Entries: 12 Winners: @Winner"
            )
        ]
        self.calls: list[tuple[str, dict]] = []

    def window(self, arguments):
        self.calls.append(("window", dict(arguments)))
        record = {
            "handle": 101,
            "process_id": 202,
            "title": self.active_title,
            "visible": True,
        }
        if arguments["action"] == "list":
            return {"status": "succeeded", "action": "list", "windows": [record]}
        return {
            "status": "succeeded",
            "action": "focus",
            "window": record,
            "focus_evidence": {"foreground_after": True},
        }

    def launch(self, arguments):
        self.calls.append(("launch", dict(arguments)))
        return {"status": "succeeded", "target": "discord"}

    def input_control(self, arguments):
        self.calls.append(("input", dict(arguments)))
        if arguments["action"] == "key_combo":
            self.quick_open = True
        elif arguments["action"] == "key_press":
            self.quick_open = False
        return {
            "status": "succeeded",
            "action": arguments["action"],
            "foreground_window": {"window_handle": 101, "process_id": 202},
        }

    def uia(self, arguments):
        self.calls.append(("uia", dict(arguments)))
        command = arguments["command"]
        if command == "find_control":
            name = arguments.get("name")
            control_type = arguments.get("control_type")
            element = arguments.get("element")
            if name == "Quick Switcher" and control_type == "ComboBox":
                matches = (
                    [{"name": name, "control_type": control_type, "element": "combo"}]
                    if self.quick_open
                    else []
                )
            elif name == "Quick Switcher" and control_type == "Window":
                matches = (
                    [{"name": name, "control_type": control_type, "element": "quick"}]
                    if self.quick_open
                    else []
                )
            elif element == "quick" and control_type == "ListItem":
                matches = [
                    {
                        "name": self.channel_result_name,
                        "control_type": "ListItem",
                        "element": "channel-1",
                    }
                ] if self.query.startswith("#") else [
                    {
                        "name": "Test Guild, Server",
                        "control_type": "ListItem",
                        "element": "server-1",
                    }
                ]
            elif element == "quick" and control_type == "List":
                matches = [{"control_type": "List", "element": "results-list"}]
            elif name == "Messages in" and control_type == "List":
                matches = [
                    {
                        "name": "Messages in giveaways",
                        "control_type": "List",
                        "element": "messages",
                    }
                ]
            elif name == "User status and settings":
                matches = (
                    [
                        {
                            "name": "User status and settings",
                            "control_type": "Group",
                            "element": "account-group",
                        }
                    ]
                    if self.account_observed
                    else []
                )
            else:
                matches = []
            return {"status": "succeeded", "command": command, "matches": matches, "count": len(matches)}
        if command == "set_value":
            self.query = str(arguments["value"])
            return {"status": "succeeded", "command": command, "element": {"name": "Quick Switcher"}}
        if command == "invoke":
            self.quick_open = False
            self.active_title = self.invoke_title
            return {"status": "succeeded", "command": command, "element": {"name": arguments["name"]}}
        if command == "get_active_window":
            return {
                "status": "succeeded",
                "command": command,
                "window": {
                    "name": self.active_title,
                    "window_handle": 101,
                    "process_id": 202,
                    "focused": True,
                },
            }
        if command == "get_properties":
            return {
                "status": "succeeded",
                "command": command,
                "element": {
                    "scroll": {
                        "vertical_scrollable": False,
                        "vertical_percent": -1,
                        "vertical_view_size": 100,
                    }
                },
            }
        if command == "get_tree":
            if arguments.get("element") == "account-group":
                return {
                    "status": "succeeded",
                    "command": command,
                    "nodes": [
                        {"control_type": "Text", "name": "Test User"},
                        {"control_type": "Text", "name": "test.user"},
                    ],
                }
            return {
                "status": "succeeded",
                "command": command,
                "nodes": [
                    {"control_type": "ListItem", "name": value}
                    for value in self.message_nodes
                ],
            }
        if command == "scroll":
            return {"status": "succeeded", "command": command}
        raise AssertionError(arguments)


def _inspector(
    fixture: _DiscordFixture,
    *,
    inventory_cache: dict | None = None,
) -> DiscordDesktopInspector:
    return DiscordDesktopInspector(
        launch=fixture.launch,
        window=fixture.window,
        input_control=fixture.input_control,
        uia=fixture.uia,
        sleep=lambda _seconds: None,
        inventory_cache=inventory_cache,
    )


def test_channel_query_is_canonicalised_and_returns_exact_pairs() -> None:
    fixture = _DiscordFixture()
    result = _inspector(fixture).run(
        {"operation": "find_channels", "query": "giveaway", "max_scrolls": 0}
    )

    assert result["status"] == "succeeded"
    assert result["channels"] == [
        {
            "name": "giveaways, Text Channel, Test Guild",
            "kind": "channel",
            "channel": "giveaways",
            "channel_type": "Text",
            "server": "Test Guild",
            "channel_key": _channel_key("giveaways", "Test Guild"),
            "element": "channel-1",
        }
    ]
    set_value = next(
        arguments
        for kind, arguments in fixture.calls
        if kind == "uia" and arguments.get("command") == "set_value"
    )
    assert set_value["value"] == "# giveaway"
    assert any(
        kind == "input"
        and arguments.get("expected_window_handle") == 101
        and arguments.get("expected_process_id") == 202
        for kind, arguments in fixture.calls
    )


def test_server_query_retries_when_discord_returns_stale_channel_rows() -> None:
    class _StaleBulkFixture(_DiscordFixture):
        def __init__(self) -> None:
            super().__init__()
            self.bulk_calls = 0

        def uia(self, arguments):
            if arguments.get("command") == "collect_list":
                self.calls.append(("uia", dict(arguments)))
                self.bulk_calls += 1
                name = (
                    "giveaways, Text Channel, Wrong Guild"
                    if self.bulk_calls == 1
                    else "Test Guild, Server"
                )
                return {
                    "status": "succeeded",
                    "command": "collect_list",
                    "items": [{"name": name, "element": f"bulk-{self.bulk_calls}"}],
                    "pages_read": 1,
                    "scroll_boundary_reached": True,
                    "boundary_basis": "not_scrollable",
                    "truncated_by_limit": False,
                }
            return super().uia(arguments)

    fixture = _StaleBulkFixture()
    records = _inspector(fixture)._query_results(
        prefix="*", query="", limit=20, max_scrolls=1
    )

    assert fixture.bulk_calls == 2
    assert records == [
        {
            "name": "Test Guild, Server",
            "kind": "server",
            "channel": None,
            "channel_type": None,
            "server": "Test Guild",
            "channel_key": None,
            "element": "bulk-2",
        }
    ]


def test_bulk_result_without_scroll_pattern_uses_independent_boundary_check() -> None:
    class _NoScrollBulkFixture(_DiscordFixture):
        def uia(self, arguments):
            if arguments.get("command") == "collect_list":
                self.calls.append(("uia", dict(arguments)))
                return {
                    "status": "succeeded",
                    "command": "collect_list",
                    "items": [
                        {
                            "name": "giveaways, Text Channel, Test Guild",
                            "element": "bulk-channel",
                        }
                    ],
                    "pages_read": 1,
                    "scroll_boundary_reached": False,
                    "boundary_basis": "scroll_pattern_unavailable",
                    "truncated_by_limit": False,
                }
            return super().uia(arguments)

    fixture = _NoScrollBulkFixture()
    inspector = _inspector(fixture)
    records = inspector._query_results(
        prefix="#", query="", limit=20, max_scrolls=1
    )

    assert records[0]["channel"] == "giveaways"
    assert inspector.coverage["channels"]["scroll_boundary_reached"] is True
    assert (
        inspector.coverage["channels"]["boundary_basis"]
        == "scroll_pattern_not_scrollable"
    )


def test_server_open_retries_with_semantic_lowercase_query() -> None:
    fixture = _DiscordFixture()
    fixture.invoke_title = "#general | CRAAZE - Discord"
    inspector = _inspector(fixture)
    queries: list[str] = []

    def results(*, prefix, query, limit, max_scrolls):
        del limit, max_scrolls
        assert prefix == "*"
        queries.append(query)
        if query == "CRAAZE":
            return []
        return [
            {
                "name": "CRAAZE, Server",
                "kind": "server",
                "server": "CRAAZE",
                "element": "server-craaze",
            }
        ]

    inspector._query_results = results  # type: ignore[method-assign]
    selected = inspector._open_server("CRAAZE")

    assert queries == ["CRAAZE", "craaze"]
    assert selected["name"] == "CRAAZE, Server"


def test_server_open_can_select_from_the_complete_unfiltered_index() -> None:
    fixture = _DiscordFixture()
    fixture.invoke_title = "#general | Aim-Assist Andy's server - Discord"
    inspector = _inspector(fixture)
    queries: list[str] = []

    def results(*, prefix, query, limit, max_scrolls):
        del limit, max_scrolls
        assert prefix == "*"
        queries.append(query)
        if query:
            return []
        return [
            {
                "name": "Aim-Assist Andy's server, Server",
                "kind": "server",
                "server": "Aim-Assist Andy's server",
                "element": "server-aim-assist",
            }
        ]

    inspector._query_results = results  # type: ignore[method-assign]
    selected = inspector._open_server("Aim-Assist Andy's server")

    assert queries[-1] == ""
    assert selected["name"] == "Aim-Assist Andy's server, Server"


def test_server_open_accepts_an_exact_already_selected_title() -> None:
    fixture = _DiscordFixture()
    fixture.active_title = "#welcome | Aim-Assist Andy's server - Discord"
    inspector = _inspector(fixture)

    selected = inspector._open_server("Aim-Assist Andy's server")

    assert selected["selection_match_basis"] == "already_selected_window_title"
    assert not any(
        kind == "uia" and arguments.get("command") == "invoke"
        for kind, arguments in fixture.calls
    )


def test_channel_open_falls_back_to_the_observed_server_channel_index() -> None:
    fixture = _DiscordFixture()
    fixture.channel_result_name = (
        "social-media-giveaway, Announcement Channel, Test Guild"
    )
    fixture.invoke_title = "#social-media-giveaway | Test Guild - Discord"
    inspector = _inspector(fixture)

    def results(*, prefix, query, limit, max_scrolls):
        del limit, max_scrolls
        if prefix == "*":
            return [
                {
                    "name": "Test Guild, Server",
                    "kind": "server",
                    "server": "Test Guild",
                    "element": "server-1",
                }
            ]
        if query:
            return []
        return [
            {
                "name": fixture.channel_result_name,
                "kind": "channel",
                "channel": "social-media-giveaway",
                "channel_type": "Announcement",
                "server": "Test Guild",
                "channel_key": _channel_key(
                    "social-media-giveaway", "Test Guild"
                ),
                "element": "channel-server-scoped",
            }
        ]

    inspector._query_results = results  # type: ignore[method-assign]
    selected, _window = inspector._open_channel(
        fixture.channel_result_name,
        max_scrolls=30,
    )

    assert selected["readback_matches_channel_key"] is True
    assert selected["selection_match_basis"].startswith("server_scoped_")


def test_live_candidate_query_is_account_bound_and_can_feed_a_fast_scan() -> None:
    fixture = _DiscordFixture()
    cache: dict = {}

    discovery = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "find_channels", "query": "giveaway", "max_scrolls": 0}
    )

    assert discovery["index_scope"] == "task_targeted_candidates"
    assert discovery["candidate_index_size"] == 1
    assert discovery["candidate_server_count"] == 1
    assert discovery["coverage"]["channels"]["complete_inventory"] is False
    assert cache["candidate_account"]["handle"] == "test.user"
    assert cache["candidate_validated_for_task"] is True
    assert cache["candidate_channels"][0]["channel"] == "giveaways"
    assert "element" not in cache["candidate_channels"][0]

    scan = _inspector(fixture, inventory_cache=cache).run(
        {
            "operation": "scan_batch",
            "cursor": 0,
            "batch_limit": 1,
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert scan["index_scope"] == "task_targeted_candidates"
    assert scan["coverage"]["batch"]["complete_inventory"] is False
    assert scan["scans"][0]["candidate_classification"] == "confirmed_giveaway"


def test_stop_interrupts_a_discord_macro_between_native_calls() -> None:
    fixture = _DiscordFixture()
    stopped = {"value": False}

    def window(arguments):
        result = fixture.window(arguments)
        stopped["value"] = True
        return result

    inspector = DiscordDesktopInspector(
        launch=fixture.launch,
        window=window,
        input_control=fixture.input_control,
        uia=fixture.uia,
        sleep=lambda _seconds: None,
        should_stop=lambda: stopped["value"],
    )

    with pytest.raises(DiscordInspectionCancelled, match="stopped"):
        inspector.run(
            {"operation": "find_channels", "query": "giveaway", "max_scrolls": 0}
        )

    assert [kind for kind, _arguments in fixture.calls] == ["window"]


def test_read_channel_navigates_by_exact_result_and_reports_explicit_state() -> None:
    fixture = _DiscordFixture()
    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["selected_channel"]["readback_channel"] == "giveaways"
    assert result["selected_channel"]["readback_server"] == "Test Guild"
    assert result["message_list"] == "Messages in giveaways"
    assert result["explicit_state"] == "ended"
    assert "Winners" in result["messages"][0]
    assert result["requirements"][0]["type"] == "entry_limit"
    assert result["requirements"][0]["status"] == "unknown"
    assert result["giveaway_items"][0]["requirements"][0]["type"] == "entry_limit"
    assert result["disposition"] == "ended"
    quick_switcher_values = [
        arguments["value"]
        for kind, arguments in fixture.calls
        if kind == "uia"
        and arguments.get("command") == "set_value"
        and arguments.get("name") == "Quick Switcher"
    ]
    assert quick_switcher_values[0] == "# giveaways guild"


def test_requirements_remain_bound_to_their_exact_giveaway_item() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "GiveawayBot Prize A Ends: tomorrow Must have level 10 Entries: 1",
        "GiveawayBot Prize B Ends: next week Must have 3 valid invites Entries: 2",
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert len(result["giveaway_items"]) == 2
    first_types = {
        requirement["type"] for requirement in result["giveaway_items"][0]["requirements"]
    }
    second_types = {
        requirement["type"] for requirement in result["giveaway_items"][1]["requirements"]
    }
    assert "level" in first_types
    assert "invites" not in first_types
    assert "invites" in second_types
    assert "level" not in second_types


def test_older_long_running_giveaway_remains_active_when_newest_item_ended() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "GiveawayBot Month-long Prize Ends: next month Must have level 10 Entries: 30",
        "GiveawayBot Small Prize Ended: today Winners: @Winner Entries: 50",
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 100,
            "max_scrolls": 0,
        }
    )

    assert result["explicit_state"] == "active"
    assert result["giveaway_state_counts"] == {
        "active": 1,
        "ended": 1,
        "unknown": 0,
    }
    assert result["active_giveaway_items"][0]["state"] == "active"
    assert result["active_giveaway_items"][0]["requirements"][0]["type"] == "level"
    assert result["newest_observed_giveaway"]["state"] == "ended"
    assert result["newest_observed_giveaway"]["is_newest_observed"] is True
    assert result["active_giveaway_items"][0]["event_key"] != (
        result["ended_giveaway_items"][0]["event_key"]
    )


def test_bracketed_ended_and_discord_timestamp_states_are_structured() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "Discord points Giveaway [ENDED] Prize: 1000 points Winner: five",
        "Knife Giveaway Ends <t:1893456000:R> Requirement: level 10",
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 100,
            "max_scrolls": 0,
        }
    )

    assert result["giveaway_state_counts"] == {
        "active": 1,
        "ended": 1,
        "unknown": 0,
    }
    assert result["ended_giveaway_items"][0]["end_evidence"] == "[ENDED]"
    assert result["active_giveaway_items"][0]["state"] == "active"


def test_past_end_timestamp_overrides_stale_active_entry_count() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "GiveawayBot Old Prize Ends <t:1:R> Entries: 42"
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    item = result["giveaway_items"][0]
    assert item["state"] == "ended"
    assert item["state_basis"] == "end_timestamp_in_past"
    assert item["end_timestamp"] == 1
    assert item["observed_at_timestamp"] > item["end_timestamp"]


def test_unlabelled_timestamp_does_not_prove_active_lifecycle() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "GiveawayBot Prize announced <t:1893456000:R>"
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["giveaway_items"] == []
    assert result["explicit_state"] == "unknown"
    assert result["candidate_classification"] == "probable_giveaway"
    assert result["candidate_classification"] != "confirmed_giveaway"


def test_results_messages_are_ended_outcomes_not_unknown_live_events() -> None:
    fixture = _DiscordFixture()
    fixture.message_nodes = [
        "GiveawayBot New giveaway [RESULTS] The winner of this giveaway is @Winner Participants: 25"
    ]

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "giveaways, Text Channel, Test Guild",
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["explicit_state"] == "ended"
    assert result["giveaway_state_counts"] == {
        "active": 0,
        "ended": 1,
        "unknown": 0,
    }
    assert result["ended_giveaway_items"][0]["end_evidence"] == "[RESULTS]"


def test_decorative_channel_glyph_mismatch_uses_stable_semantic_identity() -> None:
    fixture = _DiscordFixture()
    fixture.channel_result_name = "\U0001f381-giveaways, Text Channel, Test Guild"
    fixture.invoke_title = "#\U0001f381-giveaways | Test Guild - Discord"

    result = _inspector(fixture).run(
        {
            "operation": "read_channel",
            "channel": "\U0001f389 giveaways, Text Channel, Test Guild",
            "max_scrolls": 0,
        }
    )

    selected = result["selected_channel"]
    assert selected["selection_match_basis"] == "stable_channel_key"
    assert selected["readback_matches_channel_key"] is True
    assert selected["channel_key"] == _channel_key("giveaways", "Test Guild")
    assert result["discovery_signals"]["gift_icon"] is True
    assert result["discovery_signals"]["decorative_symbol_in_channel_name"] is True


def test_navigation_refuses_a_fresh_title_for_the_wrong_channel() -> None:
    fixture = _DiscordFixture()
    fixture.invoke_title = "#general | Other Guild - Discord"

    with pytest.raises(DiscordInspectionError, match="matching fresh title readback"):
        _inspector(fixture).run(
            {
                "operation": "open_channel",
                "channel": "giveaways, Text Channel, Test Guild",
                "max_scrolls": 0,
            }
        )


def test_requirement_extraction_structures_common_rules_and_blocks_unknown_ones() -> None:
    requirements = DiscordDesktopInspector._extract_requirements(
        [
            "Giveaway Ends: tomorrow. Must have level 12 and 5 valid invites. "
            "Discord account must be at least 3 months old. "
            "You must post a photo of today's newspaper before entering."
        ]
    )

    by_type = {requirement["type"]: requirement for requirement in requirements}
    assert by_type["level"]["key"] == "level"
    assert by_type["level"]["operator"] == "minimum"
    assert by_type["level"]["expected"] == 12
    assert by_type["invites"]["expected"] == 5
    assert by_type["account_age"]["key"] == "account_age_days"
    assert by_type["account_age"]["expected"] == 90
    assert by_type["manual_review"]["operator"] == "equals"
    assert by_type["manual_review"]["expected"] is True


def test_scan_batch_requires_complete_inventory_and_returns_a_cursor() -> None:
    fixture = _DiscordFixture()
    cache = {
        "schema": "salty-steak-discord-inspection-v1",
        "complete": True,
        "validated_for_task": True,
        "account": {
            "label": "Test User",
            "handle": "test.user",
            "observed": True,
        },
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "kind": "channel",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
            }
        ],
        "coverage": {"channels": {"scroll_boundary_reached": True}},
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {
            "operation": "scan_batch",
            "cursor": 0,
            "batch_limit": 1,
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["cursor"] == 0
    assert result["next_cursor"] == 1
    assert result["done"] is True
    assert result["scans"][0]["candidate_classification"] == "confirmed_giveaway"
    assert result["scans"][0]["explicit_state"] == "ended"
    assert result["coverage"]["batch"]["succeeded"] == 1

    with pytest.raises(DiscordInspectionError, match="complete inventory"):
        _inspector(fixture, inventory_cache={}).run(
            {"operation": "scan_batch", "cursor": 0, "batch_limit": 1}
        )


def test_scan_batch_accepts_exact_channels_from_partial_current_task_inventory() -> None:
    fixture = _DiscordFixture()
    key = _channel_key("giveaways", "Test Guild")
    cache = {
        "schema": "salty-steak-discord-inventory-cache-v2",
        "owner_task_id": "task-1",
        "complete": False,
        "validated_for_task": False,
        "account": {
            "label": "Test User",
            "handle": "test.user",
            "observed": True,
        },
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": key,
            }
        ],
        "coverage": {
            "channels": {
                "scroll_boundary_reached": False,
                "truncated_by_limit": True,
            }
        },
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {
            "operation": "scan_batch",
            "channels": [key],
            "cursor": 0,
            "batch_limit": 1,
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["status"] == "succeeded"
    assert result["index_scope"] == "explicit_observed_channels"
    assert result["total_channels"] == 1
    assert result["done"] is True
    assert result["coverage"]["inventory"] == {
        "source": "explicit_current_task_inventory_channels",
        "complete": False,
        "selected_channels": 1,
    }
    assert result["scans"][0]["selected_channel"]["channel_key"] == key


def test_explicit_partial_inventory_scan_rejects_unknown_or_ambiguous_names() -> None:
    fixture = _DiscordFixture()
    cache = {
        "owner_task_id": "task-1",
        "complete": False,
        "account": {"handle": "test.user", "observed": True},
        "channels": [
            {
                "name": f"giveaways, Text Channel, {server}",
                "channel": "giveaways",
                "server": server,
                "channel_key": _channel_key("giveaways", server),
            }
            for server in ("Alpha", "Beta")
        ],
    }

    for selector in ("missing-key", "giveaways"):
        with pytest.raises(DiscordInspectionError, match="exactly one channel"):
            _inspector(fixture, inventory_cache=cache).run(
                {
                    "operation": "scan_batch",
                    "channels": [selector],
                    "cursor": 0,
                    "batch_limit": 1,
                }
            )


def test_scan_uses_fast_uia_deadlines_and_publishes_real_substep_progress() -> None:
    fixture = _DiscordFixture()
    progress: list[dict] = []
    cache = {
        "complete": True,
        "validated_for_task": True,
        "account": {"label": "Test User", "handle": "test.user", "observed": True},
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
            }
        ],
        "coverage": {"channels": {"scroll_boundary_reached": True}},
    }
    inspector = DiscordDesktopInspector(
        launch=fixture.launch,
        window=fixture.window,
        input_control=fixture.input_control,
        uia=fixture.uia,
        sleep=lambda _seconds: None,
        inventory_cache=cache,
        on_progress=lambda value: progress.append(dict(value)),
    )

    result = inspector.run(
        {
            "operation": "scan_batch",
            "cursor": 0,
            "batch_limit": 1,
            "limit": 20,
            "max_scrolls": 0,
        }
    )

    assert result["coverage"]["batch"]["succeeded"] == 1
    uia_calls = [arguments for kind, arguments in fixture.calls if kind == "uia"]
    assert uia_calls
    assert {arguments["timeout_ms"] for arguments in uia_calls} == {5_000}
    assert any(
        item.get("phase") == "starting"
        and item.get("capability") == "ui.automation"
        and item.get("command") == "collect_list"
        for item in progress
    )
    assert any(
        item.get("phase") == "finished"
        and item.get("capability") == "ui.automation"
        for item in progress
    )


def test_timed_out_bulk_collection_becomes_a_gap_without_keyboard_walk() -> None:
    class _TimedOutBulkFixture(_DiscordFixture):
        def uia(self, arguments):
            if arguments.get("command") == "collect_list":
                self.calls.append(("uia", dict(arguments)))
                return {
                    "status": "failed",
                    "command": "collect_list",
                    "failure_kind": "timeout",
                    "error": "test deadline",
                }
            return super().uia(arguments)

    fixture = _TimedOutBulkFixture()
    cache = {
        "complete": True,
        "validated_for_task": True,
        "account": {"label": "Test User", "handle": "test.user", "observed": True},
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
            }
        ],
        "coverage": {"channels": {"scroll_boundary_reached": True}},
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {
            "operation": "scan_batch",
            "cursor": 0,
            "batch_limit": 1,
            "limit": 20,
            "max_scrolls": 8,
        }
    )

    assert result["scans"] == []
    assert result["next_cursor"] == 1
    assert result["done"] is True
    assert result["coverage"]["batch"]["gaps"] == 1
    assert "5-second" in result["scan_gaps"][0]["error"]
    assert not any(
        kind == "input" and arguments.get("key") == "down"
        for kind, arguments in fixture.calls
    )


def test_complete_inventory_binds_cache_to_the_observed_account() -> None:
    fixture = _DiscordFixture()
    cache: dict = {}

    result = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "inventory", "limit": 20, "max_scrolls": 1}
    )

    assert result["account"]["handle"] == "test.user"
    assert result["account"]["identity_confirmed"] is True
    assert result["coverage"]["account"]["observed"] is True
    assert cache["complete"] is True
    assert cache["validated_for_task"] is True
    assert cache["account"]["handle"] == "test.user"
    assert cache["channels"][0]["channel_key"] == _channel_key(
        "giveaways", "Test Guild"
    )
    assert "element" not in cache["channels"][0]


def test_inventory_reuses_fresh_cache_after_account_and_server_validation() -> None:
    fixture = _DiscordFixture()
    cache = {
        "schema": "salty-steak-discord-inventory-cache-v2",
        "complete": True,
        "created_at": __import__("time").time(),
        "account": {
            "label": "Test User",
            "handle": "test.user",
            "observed": True,
        },
        "servers": [
            {"name": "Test Guild, Server", "server": "Test Guild", "kind": "server"}
        ],
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "kind": "channel",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
                "discovery_priority": 100,
            }
        ],
        "coverage": {
            "channels": {
                "unique_results": 1,
                "per_server": {
                    "Test Guild": {"scroll_boundary_reached": True}
                },
            }
        },
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "inventory", "limit": 20, "max_scrolls": 1}
    )

    assert result["inventory_reused"] is True
    assert cache["validated_for_task"] is True
    assert result["channels"][0]["channel"] == "giveaways"
    assert result["coverage"]["channels"]["source"] == (
        "validated_account_inventory_cache"
    )
    assert not any(
        kind == "uia" and arguments.get("command") == "invoke"
        for kind, arguments in fixture.calls
    )


def test_reused_inventory_recomputes_priority_instead_of_trusting_stale_scores() -> None:
    fixture = _DiscordFixture()
    now = __import__("time").time()
    cache = {
        "schema": "salty-steak-discord-inventory-cache-v2",
        "complete": True,
        "created_at": now,
        "account": {
            "label": "Test User",
            "handle": "test.user",
            "observed": True,
        },
        "servers": [
            {"name": "Test Guild, Server", "server": "Test Guild", "kind": "server"}
        ],
        "channels": [
            {
                "name": "giveaway-winners, Text Channel, Test Guild",
                "channel": "giveaway-winners",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaway-winners", "Test Guild"),
                "discovery_priority": 999,
                "inventory_index": 0,
            },
            {
                "name": "giveaways, Announcement Channel, Test Guild",
                "channel": "giveaways",
                "channel_type": "Announcement",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
                "discovery_priority": 1,
                "inventory_index": 1,
            },
        ],
        "coverage": {
            "channels": {
                "unique_results": 2,
                "per_server": {"Test Guild": {"scroll_boundary_reached": True}},
            }
        },
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "inventory", "limit": 20, "max_scrolls": 1}
    )

    assert [record["channel"] for record in result["channels"]] == [
        "giveaways",
        "giveaway-winners",
    ]
    assert result["channels"][0]["discovery_priority"] > result["channels"][1][
        "discovery_priority"
    ]


def test_complete_inventory_serves_name_filters_without_reopening_discord() -> None:
    fixture = _DiscordFixture()
    cache = {
        "complete": True,
        "validated_for_task": True,
        "account": {"label": "Test User", "handle": "test.user", "observed": True},
        "channels": [
            {
                "name": "prizes, Text Channel, Test Guild",
                "kind": "channel",
                "channel": "prizes",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("prizes", "Test Guild"),
                "discovery_priority": 20,
            },
            {
                "name": "general, Text Channel, Test Guild",
                "kind": "channel",
                "channel": "general",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("general", "Test Guild"),
                "discovery_priority": 0,
            },
        ],
    }

    result = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "find_channels", "query": "prize", "limit": 20}
    )

    assert [record["channel"] for record in result["channels"]] == ["prizes"]
    assert result["coverage"]["channels"]["source"] == (
        "complete_account_bound_inventory"
    )
    assert fixture.calls == []


def test_unvalidated_persisted_inventory_cannot_serve_filters_or_batches() -> None:
    fixture = _DiscordFixture()
    cache = {
        "complete": True,
        "validated_for_task": False,
        "account": {"label": "Test User", "handle": "test.user", "observed": True},
        "channels": [
            {
                "name": "giveaways, Text Channel, Test Guild",
                "channel": "giveaways",
                "channel_type": "Text",
                "server": "Test Guild",
                "channel_key": _channel_key("giveaways", "Test Guild"),
            }
        ],
    }

    with pytest.raises(DiscordInspectionError, match="validation in this task"):
        _inspector(fixture, inventory_cache=cache).run(
            {"operation": "scan_batch", "cursor": 0, "batch_limit": 1}
        )

    result = _inspector(fixture, inventory_cache=cache).run(
        {"operation": "find_channels", "query": "giveaway", "max_scrolls": 0}
    )
    assert result["coverage"]["channels"].get("source") != (
        "complete_account_bound_inventory"
    )


def test_discovery_priority_uses_live_labels_without_excluding_other_channels() -> None:
    gift = _discovery_priority(
        {"channel": "\U0001f381-prizes", "channel_type": "Text"}
    )
    giveaway = _discovery_priority(
        {"channel": "weekly-giveaway", "channel_type": "Text"}
    )
    ordinary = _discovery_priority(
        {"channel": "general", "channel_type": "Text"}
    )
    winners = _discovery_priority(
        {"channel": "giveaway-winners", "channel_type": "Text"}
    )

    assert giveaway > ordinary
    assert gift > ordinary
    assert giveaway > winners
    assert ordinary == 0


def test_adapter_exposes_no_external_action_operation() -> None:
    fixture = _DiscordFixture()
    try:
        _inspector(fixture).run({"operation": "join_giveaway"})
    except ValueError as error:
        assert "current_account" in str(error)
        assert "join_giveaway" not in str(error)
    else:
        raise AssertionError("unsupported external Discord action was accepted")


def test_browser_tab_that_merely_mentions_discord_is_not_a_discord_window() -> None:
    assert DiscordDesktopInspector._is_discord_title("Friends - Discord") is True
    assert DiscordDesktopInspector._is_discord_title("#giveaways | Guild - Discord") is True
    assert DiscordDesktopInspector._is_discord_title("- Discord") is True
    assert (
        DiscordDesktopInspector._is_discord_title(
            "Discord automation discussion - Google Search - Microsoft Edge"
        )
        is False
    )


def test_account_extraction_ignores_quest_text_outside_profile_control() -> None:
    account = DiscordDesktopInspector._extract_account(
        [
            {
                "name": "Ready when you are...",
                "control_type": "Text",
                "bounds": {"x": 267, "y": 700, "width": 132, "height": 18},
            },
            {
                "name": "Manage profile and status",
                "control_type": "Button",
                "bounds": {"x": 212, "y": 757, "width": 156, "height": 46},
            },
            {
                "name": ".sawlper",
                "control_type": "Text",
                "bounds": {"x": 256, "y": 796, "width": 44, "height": 17},
            },
            {
                "name": "Online",
                "control_type": "Text",
                "bounds": {"x": 256, "y": 783, "width": 34, "height": 16},
            },
        ]
    )

    assert account == {
        "label": "sawlper",
        "handle": ".sawlper",
        "observed": True,
    }


def test_account_extraction_recognises_current_plain_discord_username() -> None:
    account = DiscordDesktopInspector._extract_account(
        [
            {
                "name": "Manage profile and status",
                "control_type": "Button",
                "bounds": {"x": 212, "y": 757, "width": 180, "height": 54},
            },
            {
                "name": "Anik Hasan",
                "control_type": "Text",
                "bounds": {"x": 256, "y": 768, "width": 80, "height": 17},
            },
            {
                "name": "sawlper",
                "control_type": "Text",
                "bounds": {"x": 256, "y": 789, "width": 52, "height": 17},
            },
        ]
    )

    assert account == {
        "label": "Anik Hasan",
        "handle": "sawlper",
        "observed": True,
    }


def test_inventory_reports_server_and_channel_coverage_separately() -> None:
    fixture = _DiscordFixture()
    result = _inspector(fixture).run(
        {"operation": "inventory", "limit": 20, "max_scrolls": 0}
    )

    assert result["servers"][0]["name"] == "Test Guild, Server"
    assert result["channels"][0]["name"] == "giveaways, Text Channel, Test Guild"
    assert result["coverage"]["servers"]["query"] == "* "
    assert result["coverage"]["servers"]["unique_results"] == 1
    assert result["coverage"]["channels"]["unique_results"] == 1
    assert result["coverage"]["channels"]["servers_scanned"] == 1
    assert result["coverage"]["channels"]["per_server"]["Test Guild"]["query"] == "# "
