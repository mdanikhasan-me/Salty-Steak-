from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.backend.chat.agent_loop import (
    AgentLoop,
    AgentTaskError,
    MAX_ITERATIONS,
    build_system_prompt,
    is_destructive,
    parse_agent_action,
    summarise_observation,
)
from app.backend.chat.task_runtime import TaskContext


ALL_CAPABILITIES = [
    "screen.capture",
    "input.control",
    "application.launch",
    "terminal.execute",
]


class _RecordingBroker:
    """Stand in for AutomationBroker and record what the loop asked for."""

    def __init__(self, results: list[object] | None = None) -> None:
        self.calls: list[dict] = []
        self.results = list(results or [])

    def invoke(self, request: dict) -> dict:
        self.calls.append(request)
        if not self.results:
            return {"status": "succeeded"}
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


class _HumanActivityBroker(_RecordingBroker):
    def __init__(self) -> None:
        super().__init__([{"status": "succeeded", "action": "focus"}])
        self.progress = None
        self.started: list[str] = []
        self.ended: list[str] = []

    def begin_agent_task(self, task_id: str, *, should_stop=None) -> None:
        self.started.append(task_id)
        self.should_stop = should_stop

    def end_agent_task(self, task_id: str) -> None:
        self.ended.append(task_id)

    def set_invocation_progress_callback(self, callback) -> None:
        self.progress = callback

    def invoke(self, request: dict) -> dict:
        self.calls.append(request)
        assert self.progress is not None
        self.progress(
            {
                "state": "waiting_for_user_idle",
                "waiting_for": "user_idle",
                "summary": "Paused while you use the computer.",
                "resume_after_idle_seconds": 12,
            }
        )
        self.progress(
            {
                "state": "resumed_after_user_idle",
                "summary": "Resuming after you stopped using the computer.",
            }
        )
        return {"status": "succeeded", "action": "focus"}


def _scripted(replies: list[str]):
    def generate(_messages: list[dict[str, str]]) -> str:
        return replies.pop(0)

    return generate


def test_direct_capabilities_do_not_require_a_screenshot_first() -> None:
    prompt = build_system_prompt(ALL_CAPABILITIES)

    # Opening a link is deterministic, so the agent must not be told to observe
    # the screen before doing it; that would add a pointless vision round trip.
    assert "without observing the screen first" in prompt
    assert "Take a screenshot before input.control, and only then" in prompt

    # Rules that name a capability follow that capability's grant.
    capture_only = build_system_prompt(["screen.capture"])
    assert "input.control" not in capture_only
    assert "application.launch" not in capture_only


def test_opening_a_link_is_understood_by_the_model_then_executed_natively() -> None:
    """The model decides what; the runtime decides how.

    This is the architectural regression for "open YouTube for me": the request
    reaches the model, which emits one structured action, and the runtime
    executes it natively without ever looking at the screen.
    """

    broker = _RecordingBroker(
        [{"status": "succeeded", "target": "https://www.youtube.com"}]
    )
    seen: list[list[dict[str, str]]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append(messages)
        if len(seen) == 1:
            # The model names the destination loosely; resolution is the
            # runtime's job, not something the model has to recall exactly.
            return (
                '{"action":"application.launch","reason":"open the site",'
                '"arguments":{"target":"youtube"}}'
            )
        return '{"action":"respond","answer":"YouTube is open."}'

    loop = AgentLoop(broker=broker, generate=generate, capabilities=ALL_CAPABILITIES)
    outcome = loop.run("open YouTube for me")

    assert outcome["state"] == "completed"
    # The model was consulted, and the user's actual words reached it.
    assert "open YouTube for me" in seen[0][-1]["content"]
    assert outcome["model_turns"] == 2

    # Exactly one native execution. No capture, no vision, no raw input.
    assert [call["capability"] for call in broker.calls] == ["application.launch"]
    assert broker.calls[0]["arguments"]["target"] == "https://www.youtube.com"
    assert outcome["tiers_used"] == ["native"]
    assert outcome["steps"][0]["route"]["resolution"] == "known_site"


def test_system_prompt_advertises_only_granted_capabilities() -> None:
    prompt = build_system_prompt(["screen.capture"])

    assert "screen.capture" in prompt
    # An ungranted tool must not appear, or the model plans around a door the
    # broker will refuse to open.
    assert "input.control" not in prompt
    assert "application.launch" not in prompt
    assert "respond" in prompt


def test_parse_action_requires_one_whole_json_object() -> None:
    action = parse_agent_action(
        '{"action":"screen.capture","reason":"look first","arguments":{}}',
        ALL_CAPABILITIES,
    )
    assert action["action"] == "screen.capture"
    assert action["reason"] == "look first"

    for malformed in (
        "Sure! I will take a screenshot.",
        '{"action":"screen.capture"} and then I will click',
        "[]",
        '{"action":"delete.everything","arguments":{}}',
    ):
        with pytest.raises(ValueError):
            parse_agent_action(malformed, ALL_CAPABILITIES)


def test_parse_action_accepts_a_fenced_object_and_respond_answer() -> None:
    fenced = '```json\n{"action":"respond","answer":"all done"}\n```'
    action = parse_agent_action(fenced, ALL_CAPABILITIES)
    assert action["action"] == "respond"
    assert action["answer"] == "all done"

    # A respond with no answer is not a usable final turn.
    with pytest.raises(ValueError, match="non-empty answer"):
        parse_agent_action('{"action":"respond","answer":"  "}', ALL_CAPABILITIES)


def test_loop_runs_tools_until_the_model_responds() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "artifact": {
                    "path": "C:/shot.bmp",
                    "width": 1920,
                    "height": 1080,
                    "source_width": 3840,
                    "source_height": 2160,
                    "scale_divisor": 2,
                },
            },
            {"status": "succeeded", "action": "mouse_click"},
        ]
    )
    steps: list[dict] = []
    loop = AgentLoop(
        broker=broker,
        generate=_scripted(
            [
                '{"action":"screen.capture","reason":"observe","arguments":{}}',
                '{"action":"input.control","reason":"click start",'
                '"arguments":{"action":"mouse_click","x":20,"y":30}}',
                '{"action":"respond","reason":"done","answer":"Opened the menu."}',
            ]
        ),
        capabilities=ALL_CAPABILITIES,
        on_step=steps.append,
    )

    outcome = loop.run("open the start menu")

    assert outcome["state"] == "completed"
    assert outcome["answer"] == "Opened the menu."
    assert [call["capability"] for call in broker.calls] == [
        "screen.capture",
        "input.control",
    ]
    assert all(call["user_confirmed"] is True for call in broker.calls)
    # Progress is streamed per step, including the final respond.
    assert [step["step"] for step in steps] == [1, 2, 3]
    assert steps[0]["reason"] == "observe"


def test_human_activity_pause_and_resume_are_visible_in_task_progress() -> None:
    broker = _HumanActivityBroker()
    snapshots = []
    outcome = AgentLoop(
        broker=broker,
        generate=_scripted(
            [
                '{"action":"window.control","reason":"focus the requested app",'
                '"arguments":{"action":"focus","title":"Notepad"}}',
                '{"action":"respond","answer":"The app is ready."}',
            ]
        ),
        capabilities=["window.control"],
        on_progress=snapshots.append,
    ).run("Focus Notepad for me.")

    assert outcome["state"] == "completed"
    assert broker.started == [outcome["task"]["task_id"]]
    assert broker.ended == broker.started
    assert any(
        snapshot["state"] == "waiting"
        and snapshot["waiting_for"] == "user_idle"
        and snapshot["reason"] == "Paused while you use the computer."
        for snapshot in snapshots
    )
    assert any(
        snapshot["state"] == "executing"
        and snapshot["reason"] == "Resuming after you stopped using the computer."
        for snapshot in snapshots
    )


def test_loop_feeds_tool_failures_back_instead_of_aborting() -> None:
    broker = _RecordingBroker([PermissionError("capability has not been granted")])
    observations: list[str] = []

    def generate(messages: list[dict[str, str]]) -> str:
        observations.append(messages[-1]["content"])
        if len(observations) == 1:
            return '{"action":"terminal.execute","reason":"try","arguments":{"argv":["x"]}}'
        return '{"action":"respond","reason":"stop","answer":"Could not run it."}'

    loop = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
    )
    outcome = loop.run("run something")

    assert outcome["state"] == "completed"
    # The failure reached the model as an observation it could react to.
    assert "PermissionError" in observations[1]
    assert outcome["steps"][0]["status"] == "failed"


def test_explicit_do_not_type_constraint_blocks_raw_input_and_allows_structured_value() -> None:
    broker = _RecordingBroker(
        [{"status": "succeeded", "command": "set_value", "element": {}}]
    )
    replies = iter(
        [
            '{"action":"input.control","reason":"wrong route",'
            '"arguments":{"action":"type_text","text":"giveaway"}}',
            '{"action":"ui.automation","reason":"structured route",'
            '"arguments":{"command":"set_value","element":"el-1",'
            '"name":"Quick Switcher","value":"# giveaway"}}',
            '{"action":"respond","answer":"done"}',
        ]
    )
    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "ui.automation"],
    ).run("Find the channel. Do not type any text into the application.")

    assert outcome["state"] == "completed"
    assert outcome["steps"][0]["status"] == "blocked"
    assert [call["capability"] for call in broker.calls] == ["ui.automation"]


def test_native_discord_task_preserves_the_models_chosen_primitive() -> None:
    broker = _RecordingBroker(
        [
            {"status": "succeeded", "command": "get_active_window"},
            {"status": "succeeded", "operation": "current_account", "account": {}},
        ]
    )
    replies = iter(
        [
            '{"action":"ui.automation","reason":"read Discord one control at a time",'
            '"arguments":{"command":"get_active_window"}}',
            '{"action":"discord.inspect","reason":"inspect the account next",'
            '"arguments":{"operation":"current_account"}}',
            '{"action":"respond","answer":"Discord account inspected."}',
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "ui.automation", "discord.inspect"],
    ).run("Open Discord and inspect the signed-in Discord account.")

    assert outcome["state"] == "completed"
    assert outcome["steps"][0]["status"] == "succeeded"
    assert [call["capability"] for call in broker.calls] == [
        "ui.automation",
        "discord.inspect",
    ]
    assert outcome["metrics"]["replans"] == 0


def test_explicit_exhaustive_discord_request_does_not_rewrite_the_models_operation() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "operation": "find_channels",
                "account": {"observed": True, "handle": "test.user"},
                "channels": [
                    {
                        "name": "prizes, Text Channel, Test Guild",
                        "channel": "prizes",
                        "server": "Test Guild",
                    }
                ],
                "coverage": {"channels": {"complete_inventory": False}},
            }
        ]
    )
    replies = iter(
        [
            '{"action":"discord.inspect","reason":"search likely names",'
            '"arguments":{"operation":"find_channels","query":"giveaway"}}',
            '{"action":"respond","answer":"Inventory complete."}',
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run("Open Discord and inventory every server and channel.")

    assert outcome["state"] == "completed"
    assert broker.calls[0]["capability"] == "discord.inspect"
    assert broker.calls[0]["arguments"] == {
        "operation": "find_channels",
        "query": "giveaway",
    }
    assert outcome["steps"][0]["reason"] == "search likely names"


def test_discord_content_mission_runs_only_model_selected_operations() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "operation": "find_channels",
                "account": {"observed": True, "handle": "test.user"},
                "channels": [{"name": "prizes, Text Channel, Test Guild", "server": "Test Guild", "channel": "prizes"}],
                "coverage": {
                    "channels": {
                        "scroll_boundary_reached": True,
                        "truncated_by_limit": False,
                        "complete_inventory": False,
                    },
                },
            },
            {
                "status": "succeeded",
                "operation": "scan_batch",
                "cursor": 0,
                "next_cursor": 1,
                "done": True,
                "scans": [],
                "coverage": {"batch": {"done": True}},
            },
        ]
    )
    replies = iter(
        [
            '{"action":"discord.inspect","reason":"discover candidates",'
            '"arguments":{"operation":"find_channels","query":"giveaway",'
            '"limit":500,"max_scrolls":20}}',
            '{"action":"discord.inspect","reason":"inspect the selected batch",'
            '"arguments":{"operation":"scan_batch","cursor":0,'
            '"batch_limit":3,"limit":60,"max_scrolls":4}}',
            '{"action":"respond","answer":"Content batch inspected."}',
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run("Open Discord and find active giveaway messages and requirements.")

    assert outcome["state"] == "completed"
    assert [call["arguments"]["operation"] for call in broker.calls] == [
        "find_channels",
        "scan_batch",
    ]
    assert broker.calls[0]["arguments"] == {
        "operation": "find_channels",
        "query": "giveaway",
        "limit": 500,
        "max_scrolls": 20,
    }
    assert broker.calls[1]["arguments"]["cursor"] == 0
    assert broker.calls[1]["arguments"]["batch_limit"] == 3
    assert broker.calls[1]["arguments"]["limit"] == 60
    assert broker.calls[1]["arguments"]["max_scrolls"] == 4
    assert outcome["model_turns"] == 3


def test_model_can_choose_complete_inventory_and_scan_to_final_cursor() -> None:
    channels = [
        {
            "name": f"channel-{index}, Text Channel, Test Guild",
            "channel": f"channel-{index}",
            "server": "Test Guild",
        }
        for index in range(13)
    ]

    def scan(index: int) -> dict[str, object]:
        return {
            "selected_channel": {
                "server": "Test Guild",
                "channel": f"channel-{index}",
                "readback_server": "Test Guild",
                "readback_channel": f"channel-{index}",
                "readback_matches_channel_key": True,
            },
            "candidate_classification": "confirmed_giveaway",
            "criteria_status": "no_requirement_text_observed",
            "giveaway_state_counts": {"active": 1, "ended": 0, "unknown": 0},
            "requirements": [],
            "coverage": {"messages": {"history_boundary_reached": True}},
        }

    inventory = {
        "status": "succeeded",
        "operation": "inventory",
        "account": {"observed": True, "handle": "test.user"},
        "servers": [{"name": "Test Guild, Server"}],
        "channels": channels,
        "discovery_candidate_count": 3,
        "inventory_gaps": [],
        "coverage": {
            "servers": {"scroll_boundary_reached": True},
            "channels": {
                "scroll_boundary_reached": True,
                "truncated_by_limit": False,
                "gaps": [],
            },
        },
    }
    first = {
        "status": "succeeded",
        "operation": "scan_batch",
        "cursor": 0,
        "next_cursor": 10,
        "done": False,
        "scans": [scan(index) for index in range(10)],
        "scan_gaps": [],
        "coverage": {"batch": {"done": False}},
    }
    second = {
        "status": "succeeded",
        "operation": "scan_batch",
        "cursor": 10,
        "next_cursor": 13,
        "done": True,
        "scans": [scan(index) for index in range(10, 13)],
        "scan_gaps": [],
        "coverage": {"batch": {"done": True}},
    }
    broker = _RecordingBroker([inventory, first, second])

    task = (
        "Open Discord, inventory every server and every channel, and find all "
        "channels with active giveaway messages and requirements."
    )
    replies = iter(
        [
            '{"action":"discord.inspect","reason":"inventory the requested scope",'
            '"arguments":{"operation":"inventory","limit":5000,"max_scrolls":100}}',
            '{"action":"discord.inspect","reason":"scan the first batch",'
            '"arguments":{"operation":"scan_batch","cursor":0,"batch_limit":10,'
            '"limit":500,"max_scrolls":30}}',
            '{"action":"discord.inspect","reason":"continue from observed cursor",'
            '"arguments":{"operation":"scan_batch","cursor":10,"batch_limit":10,'
            '"limit":500,"max_scrolls":30}}',
            '{"action":"respond","answer":"I inspected 13 active giveaway items."}',
        ]
    )
    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run(task)

    assert outcome["state"] == "completed"
    assert [call["arguments"]["operation"] for call in broker.calls] == [
        "inventory",
        "scan_batch",
        "scan_batch",
    ]
    assert broker.calls[0]["arguments"] == {
        "operation": "inventory",
        "limit": 5_000,
        "max_scrolls": 100,
    }
    assert broker.calls[1]["arguments"] == {
        "operation": "scan_batch",
        "cursor": 0,
        "batch_limit": 10,
        "limit": 500,
        "max_scrolls": 30,
    }
    assert broker.calls[2]["arguments"]["cursor"] == 10
    assert "13 active" in outcome["answer"]
    assert outcome["model_turns"] == 4


def test_discord_model_owns_the_final_report_after_its_selected_observations() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "operation": "inventory",
                "account": {"observed": True, "label": "test.user"},
                "servers": [{"name": "Test Guild, Server"}],
                "channels": [{"name": "prizes, Text Channel, Test Guild"}],
                "discovery_candidate_count": 1,
                "inventory_gaps": [],
                "coverage": {
                    "servers": {"scroll_boundary_reached": True},
                    "channels": {
                        "scroll_boundary_reached": True,
                        "truncated_by_limit": False,
                        "gaps": [],
                    },
                },
            },
            {
                "status": "succeeded",
                "operation": "scan_batch",
                "cursor": 0,
                "next_cursor": 1,
                "done": False,
                "scan_gaps": [],
                "scans": [
                    {
                        "selected_channel": {
                            "server": "Test Guild",
                            "channel": "prizes",
                            "readback_matches_channel_key": True,
                        },
                        "candidate_classification": "confirmed_giveaway",
                        "criteria_status": "needs_manual_review",
                        "giveaway_state_counts": {
                            "active": 1,
                            "ended": 0,
                            "unknown": 1,
                        },
                        "requirements": [
                            {
                                "type": "manual_review",
                                "key": "manual_requirement:test",
                                "operator": "equals",
                                "expected": True,
                                "status": "unknown",
                            }
                        ],
                    }
                ],
                "coverage": {"batch": {"done": False}},
            },
        ]
    )
    replies = iter(
        [
            '{"action":"discord.inspect","reason":"observe the requested scope",'
            '"arguments":{"operation":"inventory"}}',
            '{"action":"discord.inspect","reason":"read one observed batch",'
            '"arguments":{"operation":"scan_batch","cursor":0,"batch_limit":1}}',
            '{"action":"respond","answer":"I inspected 1 server and 1 channel. '
            'One active item was observed, but coverage is incomplete and its '
            'criteria need manual review."}',
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run("Open Discord and find active giveaway messages and requirements.")

    assert outcome["state"] == "completed"
    assert outcome["answer"].startswith("I inspected 1 server")
    assert outcome["metrics"]["parse_failures"] == 0
    assert [step["action"] for step in outcome["steps"]] == [
        "discord.inspect",
        "discord.inspect",
        "respond",
    ]


def test_malformed_discord_planner_text_never_starts_a_hidden_workflow() -> None:
    inventory = {
        "status": "succeeded",
        "operation": "find_channels",
        "account": {"observed": True, "label": "test.user"},
        "channels": [
            {
                "name": "prizes, Text Channel, Test Guild",
                "channel": "prizes",
                "server": "Test Guild",
            }
        ],
        "candidate_index_size": 91,
        "candidate_server_count": 27,
        "coverage": {
            "channels": {
                "scroll_boundary_reached": True,
                "truncated_by_limit": False,
                "complete_inventory": False,
            },
        },
    }
    scan = {
        "status": "succeeded",
        "operation": "scan_batch",
        "cursor": 0,
        "next_cursor": 1,
        "done": False,
        "scan_gaps": [],
        "scans": [],
        "coverage": {"batch": {"done": False}},
    }
    broker = _RecordingBroker([inventory, scan])
    outcome = AgentLoop(
        broker=broker,
        generate=_scripted(["not json", "still not json", '{"action":"respond"']),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run("Open Discord and find active giveaway messages and requirements.")

    assert outcome["state"] == "failed"
    assert outcome["answer"].strip()
    assert broker.calls == []


def test_discord_report_rejects_lifecycle_items_mislabeled_as_channels() -> None:
    evidence = {
        "inventory": {"servers": 41, "channels": 91, "complete": False},
        "content_scan": {
            "verified_channel_count": 3,
            "lifecycle": {"active": 0, "ended": 5, "unknown": 11},
            "requirement_record_count": 11,
            "done": False,
        },
    }
    incorrect = (
        "I found 91 candidate channels across 41 servers. The lifecycle scan "
        "covered 16 total channels: 0 active, 5 ended, and 11 unknown. I opened "
        "3 channels, and coverage is incomplete."
    )
    correct = (
        "I found 91 candidate channels across 41 servers and opened 3 channels. "
        "Their giveaway items included 0 active, 5 ended, and 11 unknown. "
        "Coverage is incomplete."
    )

    assert AgentLoop._discord_report_is_usable(incorrect, evidence) is False
    assert AgentLoop._discord_report_is_usable(correct, evidence) is True


def test_discord_observation_keeps_evidence_but_not_the_full_inventory_dump() -> None:
    per_server = {
        f"Server {index}": {
            "scroll_boundary_reached": True,
            "truncated_by_limit": False,
            "unique_results": 20,
        }
        for index in range(51)
    }
    result = {
        "status": "succeeded",
        "operation": "scan_batch",
        "account": {"observed": True, "label": "test.user"},
        "cursor": 3,
        "next_cursor": 13,
        "done": False,
        "coverage": {
            "batch": {"cursor": 3, "next_cursor": 13, "total_channels": 1130},
            "inventory": {
                "channels": {
                    "scroll_boundary_reached": True,
                    "truncated_by_limit": False,
                    "per_server": per_server,
                }
            },
        },
        "scans": [
            {
                "selected_channel": {
                    "server": f"Server {index}",
                    "channel": f"channel-{index}",
                    "channel_key": f"key-{index}",
                    "readback_matches_channel_key": True,
                },
                "candidate_classification": "confirmed_giveaway",
                "criteria_status": "needs_manual_review",
                "disposition": "requirements_unverified",
                "explicit_state": "active",
                "giveaway_state_counts": {"active": 1, "ended": 0, "unknown": 0},
                "messages": [{"text": "m" * 5_000} for _ in range(10)],
                "active_giveaway_items": [
                    {
                        "event_key": f"event-{index}",
                        "state": "active",
                        "evidence": "e" * 5_000,
                        "requirements": [
                            {
                                "type": "manual_review",
                                "key": "manual",
                                "operator": "equals",
                                "expected": True,
                                "status": "unknown",
                                "text": "r" * 5_000,
                            }
                        ],
                    }
                ],
            }
            for index in range(10)
        ],
    }

    observation = summarise_observation("discord.inspect", result)
    encoded = json.dumps(observation, sort_keys=True)

    assert len(encoded) < 15_000
    assert observation["scan_count"] == 10
    assert observation["scans_sampled"] == 6
    assert observation["scan_aggregate"]["lifecycle"] == {
        "active": 10,
        "ended": 0,
        "unknown": 0,
    }
    assert observation["scan_aggregate"]["confirmed_channel_count"] == 10
    assert observation["scan_aggregate"]["manual_review_channel_count"] == 10
    assert observation["scan_aggregate"]["incomplete_history_channel_count"] == 10
    assert observation["coverage"]["inventory"]["channels"]["per_server_count"] == 51
    assert "per_server" not in observation["coverage"]["inventory"]["channels"]
    assert observation["scans"][0]["message_count"] == 10
    assert "message_samples" not in observation["scans"][0]
    assert len(
        observation["scans"][0]["representative_giveaway_items"][0]["evidence"]
    ) == 360
    assert len(
        observation["scans"][0]["requirements"][0]["text"]
    ) == 240


def test_discord_followup_scan_uses_forward_cursor_and_bounded_batch() -> None:
    inventory = {
        "status": "succeeded",
        "operation": "inventory",
        "account": {"observed": True, "handle": "test.user"},
        "servers": [{"name": "Test Guild, Server"}],
        "channels": [{"name": "prizes, Text Channel, Test Guild"}],
        "inventory_gaps": [],
        "coverage": {
            "servers": {"scroll_boundary_reached": True},
            "channels": {
                "scroll_boundary_reached": True,
                "truncated_by_limit": False,
                "gaps": [],
            },
        },
    }
    first_scan = {
        "status": "succeeded",
        "operation": "scan_batch",
        "cursor": 0,
        "next_cursor": 3,
        "done": False,
        "scans": [],
        "coverage": {"batch": {"done": False}},
    }
    second_scan = {
        "status": "succeeded",
        "operation": "scan_batch",
        "cursor": 3,
        "next_cursor": 6,
        "done": False,
        "scans": [],
        "coverage": {"batch": {"done": False}},
    }
    broker = _RecordingBroker([inventory, first_scan, second_scan])
    replies = iter(
        [
            '{"action":"discord.inspect","reason":"build the requested inventory",'
            '"arguments":{"operation":"inventory"}}',
            '{"action":"discord.inspect","reason":"keep scanning",'
            '"arguments":{"operation":"scan_batch","cursor":0,"batch_limit":3,'
            '"limit":20,"max_scrolls":4}}',
            '{"action":"discord.inspect","reason":"continue from the returned cursor",'
            '"arguments":{"operation":"scan_batch","cursor":3,"batch_limit":3,'
            '"limit":20,"max_scrolls":4}}',
            '{"action":"respond","answer":"The next batch was inspected."}',
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "discord.inspect"],
    ).run("Open Discord and find active giveaway messages and requirements.")

    assert outcome["state"] == "completed"
    assert broker.calls[2]["arguments"] == {
        "operation": "scan_batch",
        "cursor": 3,
        "batch_limit": 3,
        "limit": 20,
        "max_scrolls": 4,
    }


def test_explicit_do_not_send_constraint_blocks_named_send_control() -> None:
    broker = _RecordingBroker()
    replies = iter(
        [
            '{"action":"ui.automation","reason":"send",'
            '"arguments":{"command":"invoke","element":"el-send","name":"Send"}}',
            '{"action":"respond","answer":"I left it unsent."}',
        ]
    )
    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "ui.automation"],
    ).run("Draft it, but do not send anything.")

    assert outcome["state"] == "completed"
    assert outcome["steps"][0]["status"] == "blocked"
    assert broker.calls == []


def test_keyboard_input_is_bound_to_the_observed_active_window() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "command": "focus",
                "element": {
                    "name": "Friends - Discord",
                    "window_handle": 198356,
                    "process_id": 19960,
                    "focused": True,
                },
            },
            {"status": "succeeded", "action": "key_combo"},
        ]
    )
    replies = iter(
        [
            '{"action":"ui.automation","reason":"focus Discord",'
            '"arguments":{"command":"focus","process_id":19960}}',
            '{"action":"input.control","reason":"open quick switcher",'
            '"arguments":{"action":"key_combo","combo":"Ctrl+K"}}',
            '{"action":"respond","answer":"done"}',
        ]
    )
    outcome = AgentLoop(
        broker=broker,
        generate=lambda _messages: next(replies),
        capabilities=[*ALL_CAPABILITIES, "ui.automation"],
    ).run("open Discord quick switcher")

    assert outcome["state"] == "completed"
    input_arguments = broker.calls[1]["arguments"]
    assert input_arguments["expected_window_handle"] == 198356
    assert input_arguments["expected_process_id"] == 19960


def test_browser_query_handles_reach_the_next_model_turn() -> None:
    broker = _RecordingBroker(
        [
            {
                "status": "succeeded",
                "command": "query",
                "matches": [
                    {
                        "element": "web-tab-1/web-el-9",
                        "role": "button",
                        "name": "Continue",
                        "token": "must-not-leak",
                    }
                ],
                "count": 1,
            },
            {"status": "succeeded", "command": "click"},
        ]
    )
    seen: list[str] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append(messages[-1]["content"])
        if len(seen) == 1:
            return (
                '{"action":"browser.control","reason":"find the control",'
                '"arguments":{"command":"query","name":"Continue"}}'
            )
        if len(seen) == 2:
            assert "web-tab-1/web-el-9" in seen[-1]
            assert "must-not-leak" not in seen[-1]
            assert "[redacted]" in seen[-1]
            return (
                '{"action":"browser.control","reason":"use observed control",'
                '"arguments":{"command":"click",'
                '"element":"web-tab-1/web-el-9"}}'
            )
        return '{"action":"respond","answer":"Continued."}'

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=["browser.control"],
    ).run("continue on the page")

    assert outcome["state"] == "completed"
    assert broker.calls[1]["arguments"]["element"] == "web-tab-1/web-el-9"


def test_file_content_and_ui_text_are_not_discarded_from_observations() -> None:
    file_observation = summarise_observation(
        "files.manage",
        {
            "status": "succeeded",
            "operation": "read",
            "path": "C:/notes.txt",
            "content": "runtime file content",
            "matched_paths": ["C:/notes.txt"],
        },
    )
    ui_observation = summarise_observation(
        "ui.automation",
        {
            "status": "succeeded",
            "command": "get_text",
            "text": "runtime control text",
        },
    )

    assert file_observation["content"] == "runtime file content"
    assert ui_observation["text"] == "runtime control text"


def test_loop_blocks_destructive_commands_unless_full_access() -> None:
    destructive = (
        '{"action":"terminal.execute","reason":"clean",'
        '"arguments":{"argv":["powershell","Remove-Item","C:/data"]}}'
    )
    broker = _RecordingBroker()
    loop = AgentLoop(
        broker=broker,
        generate=_scripted([destructive]),
        capabilities=ALL_CAPABILITIES,
        authority_mode="ask_every_time",
    )

    outcome = loop.run("clean up the folder")

    assert outcome["state"] == "needs_review"
    # Nothing was executed; the step halted before reaching the broker.
    assert broker.calls == []

    permitted = _RecordingBroker([{"status": "succeeded", "exit_code": 0}])
    unattended = AgentLoop(
        broker=permitted,
        generate=_scripted(
            [destructive, '{"action":"respond","answer":"Removed it."}']
        ),
        capabilities=ALL_CAPABILITIES,
        authority_mode="full_access",
    )
    assert unattended.run("clean up")["state"] == "completed"
    assert len(permitted.calls) == 1


def test_a_repeated_ineffective_action_forces_a_change_of_strategy() -> None:
    """The old failure mode: fifteen identical screenshots and no progress."""

    broker = _RecordingBroker([{"status": "succeeded", "artifact": {}}] * 20)
    offered: list[list[str]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        last = messages[-1]["content"]
        if "not making progress" in last:
            offered.append(json.loads(last)["available"])
            return '{"action":"respond","answer":"I am blocked."}'
        return '{"action":"screen.capture","reason":"look again","arguments":{}}'

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        max_iterations=15,
    ).run("keep looking")

    assert outcome["state"] == "completed"
    # It stopped after the third identical result, not after fifteen.
    assert len(broker.calls) == 3
    assert outcome["metrics"]["stagnation_breaks"] == 1
    assert outcome["metrics"]["escalations"] == 1
    # The model was told which other rungs remain available.
    assert "screen.capture" not in offered[0]
    assert "application.launch" in offered[0]


def test_stagnation_fails_cleanly_when_no_other_capability_exists() -> None:
    broker = _RecordingBroker([{"status": "succeeded", "artifact": {}}] * 10)

    outcome = AgentLoop(
        broker=broker,
        generate=lambda _m: '{"action":"screen.capture","reason":"look","arguments":{}}',
        capabilities=["screen.capture"],
        max_iterations=15,
    ).run("keep looking")

    assert outcome["state"] == "failed"
    assert len(broker.calls) == 3
    assert "same result" in outcome["answer"]


def test_a_changing_result_is_not_treated_as_stagnation() -> None:
    broker = _RecordingBroker(
        [
            {"status": "succeeded", "action": "one"},
            {"status": "succeeded", "action": "two"},
            {"status": "succeeded", "action": "three"},
            {"status": "succeeded", "action": "four"},
        ]
    )
    replies = [
        '{"action":"input.control","reason":"type","arguments":{"action":"type_text","text":"a"}}',
    ] * 4 + ['{"action":"respond","answer":"done"}']

    outcome = AgentLoop(
        broker=broker,
        generate=_scripted(replies),
        capabilities=ALL_CAPABILITIES,
    ).run("type four times")

    # Real progress each time, so nothing is interrupted.
    assert outcome["state"] == "completed"
    assert len(broker.calls) == 4
    assert outcome["metrics"]["stagnation_breaks"] == 0


def test_loop_stops_at_the_iteration_cap() -> None:
    broker = _RecordingBroker([{"status": "succeeded"}] * 50)
    loop = AgentLoop(
        broker=broker,
        generate=lambda _messages: '{"action":"screen.capture","reason":"again","arguments":{}}',
        capabilities=ALL_CAPABILITIES,
        max_iterations=4,
    )

    outcome = loop.run("loop forever")

    assert outcome["state"] == "exhausted"
    assert outcome["step_count"] == 4
    assert len(broker.calls) == 4


def test_default_mission_budget_is_eight_hours_with_a_secondary_runaway_guard() -> None:
    loop = AgentLoop(
        broker=_RecordingBroker(),
        generate=lambda _messages: '{"action":"respond","answer":"done"}',
        capabilities=ALL_CAPABILITIES,
    )

    budget = loop.task.snapshot()["mission_budget"]
    assert MAX_ITERATIONS == 8_192
    assert budget["step_limit"] == 8_192
    assert budget["duration_limit_seconds"] == 8 * 60 * 60


def test_elapsed_time_budget_stops_before_a_late_action_can_execute() -> None:
    now = {"value": 0.0}

    def clock() -> float:
        return now["value"]

    def generate(_messages: list[dict[str, str]]) -> str:
        now["value"] = 2.0
        return '{"action":"screen.capture","reason":"late","arguments":{}}'

    broker = _RecordingBroker([{"status": "succeeded"}])
    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        max_duration_seconds=1,
        clock=clock,
    ).run("inspect for one second")

    assert outcome["state"] == "exhausted"
    assert "mission time budget" in outcome["answer"]
    assert broker.calls == []


def test_planning_preview_streams_counts_and_never_raw_control_json() -> None:
    progress: list[dict] = []

    def generate_with_preview(_messages, on_preview) -> str:
        on_preview(
            {
                "token_count": 9,
                "character_count": 61,
                "tail_text": '{"action":"terminal.execute","argv":["secret"]}',
            }
        )
        return '{"action":"respond","answer":"done"}'

    outcome = AgentLoop(
        broker=_RecordingBroker(),
        generate=lambda _messages: "unused",
        generate_with_preview=generate_with_preview,
        capabilities=ALL_CAPABILITIES,
        on_progress=progress.append,
    ).run("report only")

    assert outcome["state"] == "completed"
    assert any(item["generation_preview"].get("token_count") == 9 for item in progress)
    assert all("secret" not in json.dumps(item) for item in progress)
    assert outcome["metrics"]["planning_output_tokens"] == 9


def test_mission_checkpoint_is_written_and_finishes_terminal(tmp_path: Path) -> None:
    checkpoint = tmp_path / "mission.json"
    outcome = AgentLoop(
        broker=_RecordingBroker(),
        generate=lambda _messages: '{"action":"respond","answer":"done"}',
        capabilities=ALL_CAPABILITIES,
        checkpoint_path=checkpoint,
    ).run("finish cleanly")

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert outcome["state"] == "completed"
    assert saved["schema"] == "salty-steak-agent-mission-checkpoint-v1"
    assert saved["state"] == "completed"
    assert saved["total_step_count"] == 1


def test_loop_gives_up_after_repeated_unparsable_replies() -> None:
    broker = _RecordingBroker()
    loop = AgentLoop(
        broker=broker,
        generate=lambda _messages: "I am afraid I cannot do that.",
        capabilities=ALL_CAPABILITIES,
    )

    outcome = loop.run("do something")

    assert outcome["state"] == "failed"
    assert broker.calls == []


def test_loop_honours_a_stop_request_between_steps() -> None:
    broker = _RecordingBroker([{"status": "succeeded"}] * 5)
    stopped = {"value": False}

    def generate(_messages: list[dict[str, str]]) -> str:
        stopped["value"] = True
        return '{"action":"screen.capture","reason":"observe","arguments":{}}'

    loop = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        should_stop=lambda: stopped["value"],
    )
    outcome = loop.run("watch the screen")

    assert outcome["state"] == "cancelled"
    # The stop was observed before the tool ran.
    assert broker.calls == []


def test_a_stop_during_a_model_call_cannot_execute_the_late_reply() -> None:
    """The dangerous cancellation case: the reply arrives after Stop.

    A model call is the longest thing in the loop, so a stop usually lands
    while one is in flight. That reply must never reach the broker.
    """

    from app.backend.chat.task_runtime import STOPPED, TaskContext

    broker = _RecordingBroker([{"status": "succeeded"}])
    task = TaskContext(goal="long task")

    def generate(_messages: list[dict[str, str]]) -> str:
        # The user presses Stop while this call is still running.
        task.request_stop()
        return (
            '{"action":"application.launch","reason":"open",'
            '"arguments":{"target":"https://example.com"}}'
        )

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        task=task,
    ).run("do something long")

    assert outcome["state"] == "cancelled"
    # The action the model returned was never carried out.
    assert broker.calls == []
    assert task.state == STOPPED
    assert outcome["metrics"]["cancellation_latency_seconds"] is not None


def test_metrics_report_what_a_task_actually_cost() -> None:
    broker = _RecordingBroker(
        [
            {"status": "succeeded", "artifact": {"path": "C:/shot.bmp"}},
            {"status": "succeeded", "target": "https://www.youtube.com"},
        ]
    )
    replies = [
        '{"action":"screen.capture","reason":"look","arguments":{}}',
        '{"action":"application.launch","reason":"open","arguments":{"target":"youtube"}}',
        '{"action":"respond","answer":"done"}',
    ]
    outcome = AgentLoop(
        broker=broker,
        generate=_scripted(replies),
        capabilities=ALL_CAPABILITIES,
    ).run("look then open youtube")

    metrics = outcome["metrics"]
    assert metrics["model_calls"] == 3
    assert metrics["screenshots"] == 1
    assert metrics["vision_calls"] == 1
    assert metrics["native_calls"] == 1
    assert metrics["raw_input_calls"] == 0
    assert metrics["tool_calls"] == 2
    assert metrics["first_action_seconds"] is not None
    assert outcome["tiers_used"] == ["native", "vision"]


def test_loop_requires_at_least_one_granted_capability() -> None:
    loop = AgentLoop(
        broker=_RecordingBroker(),
        generate=lambda _messages: "",
        capabilities=[],
    )
    with pytest.raises(AgentTaskError, match="nothing this task can do"):
        loop.run("do anything")


def test_older_screenshots_are_dropped_from_the_running_context() -> None:
    broker = _RecordingBroker(
        [
            {"status": "succeeded", "artifact": {"path": "C:/one.bmp"}},
            {"status": "succeeded", "artifact": {"path": "C:/two.bmp"}},
        ]
    )
    seen: list[list[dict[str, str]]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append([dict(message) for message in messages])
        if len(seen) <= 2:
            return '{"action":"screen.capture","reason":"observe","arguments":{}}'
        return '{"action":"respond","answer":"done"}'

    AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
    ).run("look twice")

    final = json.dumps(seen[-1])
    # Only the newest capture keeps its path; the earlier one is collapsed so a
    # long task cannot fill the context with stale screenshots.
    assert "C:/two.bmp" in final
    assert "C:/one.bmp" not in final
    assert "superseded by a newer screenshot" in final


def test_screenshots_are_described_by_the_vision_runtime() -> None:
    broker = _RecordingBroker(
        [{"status": "succeeded", "artifact": {"path": "C:/shot.bmp", "width": 800}}]
    )
    seen: list[str] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append(messages[-1]["content"])
        if len(seen) == 1:
            return '{"action":"screen.capture","reason":"look","arguments":{}}'
        return '{"action":"respond","answer":"I can see it."}'

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        describe_screenshot=lambda path: f"A settings window is open ({path}).",
    ).run("look at the screen")

    assert outcome["state"] == "completed"
    # The description reaches the model as part of the observation, which is
    # what lets it choose coordinates instead of guessing.
    assert "A settings window is open" in seen[1]
    assert outcome["steps"][0]["observation"]["visual_analysis"].startswith(
        "A settings window is open"
    )


def test_a_failing_vision_runtime_does_not_end_the_task() -> None:
    broker = _RecordingBroker(
        [{"status": "succeeded", "artifact": {"path": "C:/shot.bmp"}}]
    )

    def exploding(_path: str) -> str:
        raise RuntimeError("vision runtime is unavailable")

    outcome = AgentLoop(
        broker=broker,
        generate=_scripted(
            [
                '{"action":"screen.capture","reason":"look","arguments":{}}',
                '{"action":"respond","answer":"Continued without sight."}',
            ]
        ),
        capabilities=ALL_CAPABILITIES,
        describe_screenshot=exploding,
    ).run("look at the screen")

    # Losing sight degrades the run; it does not abort it.
    assert outcome["state"] == "completed"
    observation = outcome["steps"][0]["observation"]
    assert "visual_analysis" not in observation
    assert "vision runtime is unavailable" in observation["visual_analysis_error"]


def test_authority_mode_travels_with_every_broker_invocation() -> None:
    broker = _RecordingBroker([{"status": "succeeded"}])
    AgentLoop(
        broker=broker,
        generate=_scripted(
            [
                '{"action":"screen.capture","reason":"look","arguments":{}}',
                '{"action":"respond","answer":"done"}',
            ]
        ),
        capabilities=ALL_CAPABILITIES,
        authority_mode="full_access",
    ).run("look")

    # The broker decides what full access permits, so it must be told.
    assert broker.calls[0]["authority_mode"] == "full_access"


def test_earlier_screenshot_descriptions_are_trimmed_not_discarded() -> None:
    broker = _RecordingBroker(
        [
            {"status": "succeeded", "artifact": {"path": "C:/one.bmp"}},
            {"status": "succeeded", "artifact": {"path": "C:/two.bmp"}},
        ]
    )
    seen: list[list[dict[str, str]]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append([dict(message) for message in messages])
        if len(seen) <= 2:
            return '{"action":"screen.capture","reason":"observe","arguments":{}}'
        return '{"action":"respond","answer":"done"}'

    AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=ALL_CAPABILITIES,
        describe_screenshot=lambda path: "W" * 400 + f" for {path}",
    ).run("look twice")

    final = json.dumps(seen[-1])
    # The older view survives as a short trace so the agent remembers what it
    # already tried, while only the newest description is kept in full.
    assert "(earlier view)" in final
    assert final.count("W" * 400) == 1


def test_destructive_classification_covers_only_terminal_commands() -> None:
    assert is_destructive("terminal.execute", {"argv": ["cmd", "del", "x"]}) is True
    assert is_destructive("terminal.execute", {"argv": ["git", "status"]}) is False
    assert is_destructive("input.control", {"action": "type_text"}) is False


def test_screenshot_observation_reports_the_real_screen_geometry() -> None:
    observation = summarise_observation(
        "screen.capture",
        {
            "status": "succeeded",
            "artifact": {
                "path": "C:/shot.bmp",
                "width": 1920,
                "height": 1080,
                "source_width": 3840,
                "source_height": 2160,
                "scale_divisor": 2,
            },
        },
    )
    # Pointer actions address real pixels, so the true geometry travels with
    # the reduced image.
    assert observation["screen_width"] == 3840
    assert observation["image_width"] == 1920
    assert observation["scale_divisor"] == 2


def test_a_filesystem_observation_carries_the_evidence_of_what_it_did() -> None:
    """A real run deleted the right three files and could not prove it.

    `summarise_observation` had no branch for files.manage, so a filesystem
    result reduced to {"action", "status"} and everything the capability had
    gone to the trouble of reading back — which paths it touched, which it
    preserved, what was still on disk afterwards — was discarded before
    anything could look at it. Two things broke: no verifier could confirm the
    goal, and the model could not see what it had already done, so it spent
    eight steps re-listing a folder it had finished with.
    """

    observation = summarise_observation(
        "files.manage",
        {
            "status": "succeeded",
            "operation": "delete",
            "mutating": True,
            "matched_paths": [r"C:\w\a.log", r"C:\w\b.log"],
            "affected_paths": [r"C:\w\a.log", r"C:\w\b.log"],
            "preserved_paths": [r"C:\w\notes.txt"],
            "failed_paths": [],
            "after_state": {
                "still_present": [],
                "preserved_present": [r"C:\w\notes.txt"],
            },
        },
    )

    assert observation["operation"] == "delete"
    assert observation["affected_paths"] == [r"C:\w\a.log", r"C:\w\b.log"]
    assert observation["preserved_paths"] == [r"C:\w\notes.txt"]
    assert observation["after_state"]["still_present"] == []
    assert observation["mutating"] is True


def test_a_filesystem_listing_shows_the_model_what_it_found() -> None:
    """Otherwise the next step has to guess at filenames it has already seen."""

    observation = summarise_observation(
        "files.manage",
        {
            "status": "succeeded",
            "operation": "search",
            "mutating": False,
            "matched_paths": [r"C:\w\a.log", r"C:\w\b.log"],
        },
    )

    assert observation["matched_paths"] == [r"C:\w\a.log", r"C:\w\b.log"]


def test_running_out_of_steps_never_reads_as_a_report_of_success() -> None:
    """The live failure: eight steps listed under "what I completed"."""

    broker = _RecordingBroker(
        [{"status": "succeeded", "target": "https://www.youtube.com"}] * 40
    )
    replies = iter(
        [
            json.dumps(
                {
                    "action": "application.launch",
                    "reason": f"Step {index} towards playing the video.",
                    "arguments": {"target": f"https://example.com/{index}"},
                }
            )
            for index in range(40)
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda messages: next(replies),
        capabilities=["application.launch"],
        authority_mode="full_access",
        max_iterations=4,
    ).run("Open YouTube and play Despacito")

    assert outcome["state"] == "exhausted"
    answer = outcome["answer"]
    # The user must not be told a task finished when it did not.
    assert "completed" not in answer.casefold()
    assert "incomplete" in answer.casefold()
    assert "decision runaway guard" in answer.casefold()


def test_the_browser_capability_explains_how_to_present_its_owned_pane() -> None:
    # The agent really did search YouTube and click the video — in a window the
    # user cannot see, and then told them it had played it.
    prompt = build_system_prompt(["browser.control"])

    assert "browser pane" in prompt
    assert "show_window" in prompt


def test_an_effect_already_achieved_is_recognised_across_capabilities() -> None:
    """The Despacito loop: same destination, three different calls."""

    from app.backend.chat.agent_loop import effect_key

    # Whichever rung reaches it, arriving at a page is one effect.
    launched = effect_key("application.launch", {"target": "https://www.youtube.com"})
    browsed = effect_key(
        "browser.control", {"command": "open_url", "url": "https://www.youtube.com/"}
    )
    assert launched is not None and launched == browsed

    # A different destination is a different effect.
    assert effect_key("application.launch", {"target": "https://example.com"}) != launched

    # An effect that cannot be stated plainly is never suppressed.
    assert effect_key("terminal.execute", {"argv": ["cmd"]}) is None
    assert effect_key("browser.control", {"command": "read_page"}) is None
    assert effect_key(
        "window.control", {"action": "focus", "title": "Discord"}
    ) is None


def test_the_loop_does_not_spend_a_step_on_something_already_true() -> None:
    broker = _RecordingBroker(
        [{"status": "succeeded", "target": "https://www.youtube.com"}] * 10
    )
    replies = iter(
        [
            json.dumps(
                {
                    "action": "application.launch",
                    "reason": "open it",
                    "arguments": {"target": "https://www.youtube.com"},
                }
            ),
            # The same destination by another route: must not be executed again.
            json.dumps(
                {
                    "action": "application.launch",
                    "reason": "open it again",
                    "arguments": {"target": "https://www.youtube.com"},
                }
            ),
            json.dumps({"action": "respond", "answer": "Done."}),
        ]
    )

    outcome = AgentLoop(
        broker=broker,
        generate=lambda messages: next(replies),
        capabilities=["application.launch"],
        authority_mode="full_access",
        max_iterations=6,
    ).run("open youtube")

    assert outcome["state"] == "completed"
    # Two launch decisions, one actual launch.
    launches = [call for call in broker.calls if call["capability"] == "application.launch"]
    assert len(launches) == 1


def test_gmail_and_discord_playbooks_preserve_login_and_send_boundaries() -> None:
    prompt = build_system_prompt(["browser.control", "ui.automation", "application.launch"])

    assert "https://mail.google.com" in prompt
    assert "https://discord.com/app" in prompt
    assert "never presses Send" in prompt
    assert "explicit approval" in prompt
    assert "Never claim a message was sent" in prompt
    assert "Never request, extract, paste, log, or store a raw Discord user token" in prompt
    assert "Discord's visible Account Switcher" in prompt
    assert "If that exact channel is already joined, do not click again" in prompt
    assert "Never loop or retry a successful/pending join" in prompt


def test_native_discord_inspector_supersedes_lower_level_setup_calls() -> None:
    prompt = build_system_prompt(
        [
            "discord.inspect",
            "application.launch",
            "window.control",
            "ui.automation",
            "screen.capture",
            "input.control",
        ]
    )

    direct_rule = "For a Discord inspection task, make discord.inspect the first action"
    assert direct_rule in prompt
    assert "the first operation must be inventory" in prompt
    assert "can never satisfy that scope" in prompt
    assert "Focus Discord's top-level window with window.control" not in prompt


def test_control_identity_does_not_prove_an_idempotent_effect() -> None:
    from app.backend.chat.agent_loop import effect_key

    arguments = {
        "command": "invoke",
        "element": "el-voice-42",
        "name": "General voice channel",
    }

    assert effect_key("ui.automation", arguments) is None


@pytest.mark.parametrize("destination", [
    "https://example.com/Case?q=two#first",
    "https://example.com/Case?q=one#second",
    "https://example.com/case?q=one#first",
    "https://example.com/Case/?q=one#first",
])
def test_navigation_effect_preserves_distinct_destinations(destination: str) -> None:
    from app.backend.chat.agent_loop import effect_key

    original = effect_key("browser.control", {
        "command": "navigate", "url": "https://example.com/Case?q=one#first",
    })
    assert original != effect_key("browser.control", {
        "command": "navigate", "url": destination,
    })


@pytest.mark.parametrize("capability,command", [
    ("browser.control", "click"), ("browser.control", "select"),
    ("ui.automation", "invoke"), ("ui.automation", "select"),
    ("ui.automation", "toggle"),
])
def test_repeated_controls_execute_when_the_observed_state_changes(capability, command) -> None:
    broker = _RecordingBroker([
        {"status": "succeeded", "text": f"Page {index}", "title": f"Page {index}"}
        for index in range(3)
    ])
    action = {"action": capability, "arguments": {
        "command": command, "element": "next-control", "name": "Next",
    }}
    replies = iter([json.dumps(action)] * 3 + [
        json.dumps({"action": "respond", "answer": "Reached the last page."}),
    ])
    outcome = AgentLoop(broker=broker, generate=lambda _: next(replies),
                        capabilities=[capability], authority_mode="full_access").run(
        "Advance through three pages")
    assert outcome["state"] == "completed"
    assert len(broker.calls) == 3


def test_repeated_suppressed_effects_do_not_claim_completion() -> None:
    action = json.dumps({"action": "application.launch", "arguments": {
        "target": "https://example.com",
    }})
    broker = _RecordingBroker()
    outcome = AgentLoop(broker=broker, generate=lambda _: action,
                        capabilities=["application.launch"],
                        authority_mode="full_access", max_iterations=10).run(
        "Open the site and complete the remaining setup")
    assert outcome["state"] == "failed"
    assert "incomplete" in outcome["answer"]
    assert len(broker.calls) == 1


def test_opaque_send_handle_recovers_its_observed_name_before_policy() -> None:
    task = TaskContext()
    task.world_state.absorb(
        "browser.control",
        {"matches": [{"element": "web-tab-1/web-el-7", "name": "Send"}]},
    )
    loop = AgentLoop(
        broker=_RecordingBroker(),
        generate=_scripted([]),
        capabilities=["browser.control"],
        task=task,
    )

    checked = loop._semantic_action_arguments(
        "browser.control",
        {"command": "click", "element": "web-tab-1/web-el-7"},
    )

    assert checked["name"] == "Send"


def test_failed_completion_probe_returns_to_actions_then_rechecks(tmp_path):
    target = tmp_path / "result.txt"
    task = TaskContext()
    observations = []

    def check(result):
        observations.append(result)
        return target.exists(), {"missing": [] if target.exists() else [str(target)]}

    class WritingBroker(_RecordingBroker):
        def invoke(self, request):
            target.write_text("done", encoding="utf-8")
            return super().invoke(request)

    task.completion_probe = check
    replies = _scripted([
        json.dumps({"action": "respond", "answer": "Done."}),
        json.dumps({"action": "files.manage", "arguments": {
            "operation": "write", "path": str(target), "content": "done",
        }}),
        json.dumps({"action": "respond", "answer": "File created."}),
    ])
    seen = []
    def generate(messages):
        seen.append(list(messages))
        return replies(messages)
    broker = WritingBroker()
    outcome = AgentLoop(broker=broker, generate=generate, capabilities=["files.manage"],
                        authority_mode="full_access", task=task).run("Create the result file")
    assert outcome["state"] == "completed"
    assert len(broker.calls) == 1 and len(observations) == 2
    assert observations[0]["steps"] == []
    assert observations[1]["steps"][0]["action"] == "files.manage"
    assert "not yet verified" in seen[1][-1]["content"]


@pytest.mark.parametrize("verdict", [False, None, "error"])
def test_unverified_completion_stops_after_bounded_rechecks(verdict):
    task = TaskContext()
    checks = []
    def check(result):
        checks.append(result)
        if verdict == "error":
            raise RuntimeError("Observer unavailable")
        return verdict, {"reason": "still_missing"}
    task.completion_probe = check
    outcome = AgentLoop(broker=_RecordingBroker(),
        generate=lambda _: json.dumps({"action": "respond", "answer": "Done."}),
        capabilities=["files.manage"], task=task).run("Complete the task")
    assert outcome["state"] == "failed"
    assert "incomplete" in outcome["answer"]
    assert len(checks) == 3


def test_cancellation_during_completion_probe_never_reports_success():
    task = TaskContext()
    def check(_):
        task.cancellation.trip()
        return True, {}
    task.completion_probe = check
    outcome = AgentLoop(broker=_RecordingBroker(),
        generate=lambda _: json.dumps({"action": "respond", "answer": "Done."}),
        capabilities=["files.manage"], task=task).run("Complete the task")
    assert outcome["state"] == "cancelled"


def test_file_observation_retains_readback_and_overwrite_digest():
    observation = summarise_observation("files.manage", {
        "status": "succeeded", "operation": "read", "content": "hello",
        "sha256": "a" * 64, "readback_verified": True,
    })
    assert observation["content"] == "hello"
    assert observation["sha256"] == "a" * 64
    assert observation["readback_verified"] is True
