from __future__ import annotations

import pytest

from app.backend.automation.routing import (
    TIER_NATIVE,
    TIER_RAW_INPUT,
    TIER_VISION,
    describe_routing,
    execution_route_record,
    normalise_launch_target,
    ordered_capabilities,
    requires_sight,
    resolve_execution,
)


ALL = [
    "terminal.execute",
    "screen.capture",
    "input.control",
    "application.launch",
    "window.control",
]


def test_the_ladder_orders_structured_routes_above_pixels() -> None:
    # Deliberately shuffled input: the order out is the ladder, not the input.
    assert ordered_capabilities(ALL) == [
        "application.launch",
        "terminal.execute",
        "window.control",
        "screen.capture",
        "input.control",
    ]
    # Raw synthetic input is always last, whatever else is granted.
    assert ordered_capabilities(["input.control", "application.launch"]) == [
        "application.launch",
        "input.control",
    ]


def test_sight_is_only_reachable_when_a_granted_rung_needs_it() -> None:
    assert requires_sight(["application.launch", "terminal.execute"]) is False
    assert requires_sight(["application.launch", "window.control"]) is False
    assert requires_sight(["screen.capture"]) is True
    assert requires_sight(["input.control"]) is True


@pytest.mark.parametrize(
    "named,exact,resolution",
    [
        ("youtube", "https://www.youtube.com", "known_site"),
        ("YouTube", "https://www.youtube.com", "known_site"),
        ("  github  ", "https://github.com", "known_site"),
        ("https://example.com/docs", "https://example.com/docs", "explicit_url"),
        ("www.example.org", "https://www.example.org", "explicit_url"),
        ("notepad", "notepad", "known_application"),
        ("vs code", "code", "known_application"),
        ("some-internal-tool", "some-internal-tool", "verbatim"),
    ],
)
def test_loosely_named_targets_resolve_to_exact_ones(
    named: str,
    exact: str,
    resolution: str,
) -> None:
    # The model is good at knowing the user meant YouTube and unreliable at
    # recalling the exact URL, so the runtime owns resolution.
    assert normalise_launch_target(named) == (exact, resolution)


def test_resolution_never_overrides_the_capability_the_model_chose() -> None:
    # Even when a structured route exists, the router does not silently swap
    # the model's decision for a different capability.
    route = resolve_execution(
        "input.control", {"action": "mouse_click", "x": 10, "y": 20}, ALL
    )
    assert route.capability == "input.control"
    assert route.tier == TIER_RAW_INPUT
    assert route.arguments == {"action": "mouse_click", "x": 10, "y": 20}


def test_launch_arguments_are_resolved_and_reported() -> None:
    route = resolve_execution("application.launch", {"target": "youtube"}, ALL)

    assert route.capability == "application.launch"
    assert route.arguments["target"] == "https://www.youtube.com"
    assert route.tier == TIER_NATIVE
    assert route.resolution == "known_site"
    assert "resolved 'youtube' to https://www.youtube.com" in route.notes

    record = execution_route_record(route)
    assert record["tier"] == "native"
    assert record["resolution"] == "known_site"


def test_launching_a_known_application_waits_for_its_window() -> None:
    route = resolve_execution("application.launch", {"target": "notepad"}, ALL)

    assert route.arguments["target"] == "notepad"
    # A freshly launched window needs a moment before anything can act on it.
    assert route.arguments["wait_ms"] > 0

    discord = resolve_execution("application.launch", {"target": "discord"}, ALL)
    assert discord.arguments["wait_ms"] == 5_000


def test_a_browser_url_without_a_verb_is_normalised_to_open_url() -> None:
    route = resolve_execution(
        "browser.control",
        {"url": "https://www.youtube.com"},
        ["browser.control"],
    )

    assert route.arguments == {
        "url": "https://www.youtube.com",
        "command": "open_url",
    }
    assert "inferred open_url" in route.notes[0]


def test_an_explicit_target_is_left_exactly_as_the_model_gave_it() -> None:
    route = resolve_execution(
        "application.launch", {"target": "https://internal.example/app?a=1"}, ALL
    )
    assert route.arguments["target"] == "https://internal.example/app?a=1"
    assert route.notes == ()


def test_an_ungranted_capability_is_reported_rather_than_silently_run() -> None:
    route = resolve_execution("screen.capture", {}, ["application.launch"])

    assert route.capability == "screen.capture"
    assert route.tier == TIER_VISION
    assert "capability is not currently granted" in route.notes


def test_an_unknown_capability_cannot_outrank_a_structured_route() -> None:
    route = resolve_execution("something.invented", {}, ALL)

    assert route.tier == TIER_RAW_INPUT


def test_routing_description_reports_the_active_ladder() -> None:
    description = describe_routing(["application.launch", "input.control"])

    assert [rung["capability"] for rung in description["ladder"]] == [
        "application.launch",
        "input.control",
    ]
    assert description["ladder"][0]["tier"] == "native"
    assert description["ladder"][0]["requires_sight"] is False
    assert description["ladder"][1]["rank"] == TIER_RAW_INPUT
    assert description["sight_reachable"] is True


def test_the_argument_name_the_real_model_used_is_read_not_rejected() -> None:
    # The live model answered "Open YouTube" with {"app_name": "YouTube"}. The
    # broker rejected the unknown field and the whole turn failed. The name is
    # translated here rather than by loosening the broker's contract.
    route = resolve_execution(
        "application.launch", {"app_name": "YouTube"}, ALL
    )

    assert route.arguments == {"target": "https://www.youtube.com"}
    assert "read 'app_name' as 'target'" in route.notes
    assert route.resolution == "known_site"


def test_an_alias_never_overwrites_the_argument_that_was_named_correctly() -> None:
    route = resolve_execution(
        "application.launch",
        {"target": "notepad", "app_name": "YouTube"},
        ALL,
    )

    assert route.arguments["target"] == "notepad"
    assert any("ignored 'app_name'" in note for note in route.notes)


def test_an_invented_argument_is_still_passed_through_for_the_broker_to_refuse() -> None:
    # Aliasing covers the names a model really reaches for. It must not become
    # a place where anything unrecognised is quietly dropped, because then a
    # wrong call would look like a correct one.
    route = resolve_execution(
        "application.launch", {"target": "notepad", "elevate": True}, ALL
    )

    assert route.arguments["elevate"] is True


def test_plan_vocabulary_is_read_as_capability_arguments() -> None:
    # A plan step calls what it does an "operation" and what it acts on a
    # "target". The live model carried that vocabulary into the arguments of a
    # window.control node and the broker refused the whole step, while the
    # identical one-step request would have been resolved and run.
    route = resolve_execution(
        "window.control",
        {"operation": "focus", "target": "Notepad"},
        ["window.control"],
    )

    assert route.arguments == {"action": "focus", "title": "Notepad"}


def test_a_decorative_field_is_dropped_when_the_call_is_unambiguous() -> None:
    # Live failure: browser.control carried "verify": true and the whole call
    # was refused, though "command" already fully specified it.
    from app.backend.automation.invocation import prune_unsupported

    pruned, dropped = prune_unsupported(
        "browser.control", {"command": "open_url", "url": "https://x.test", "verify": True}
    )

    assert pruned == {"command": "open_url", "url": "https://x.test"}
    assert dropped == ["verify"]


def test_nothing_is_pruned_when_the_call_is_not_fully_specified() -> None:
    # Without its essential field the call is ambiguous, so tidying it up and
    # executing would be guessing. The model is asked instead.
    from app.backend.automation.invocation import prune_unsupported

    pruned, dropped = prune_unsupported("browser.control", {"verify": True})

    assert dropped == []
    assert pruned == {"verify": True}
