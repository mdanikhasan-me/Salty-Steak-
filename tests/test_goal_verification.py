"""Zonted is a claim about the world, so something has to check the world.

These cases were first written against a verifier that summed up step evidence:
if every executed step verified, the goal was verified. That model is wrong, and
`test_a_step_summing_verifier_cannot_grant_success_on_its_own` below is the
demonstration — a run that deletes the protected file as its own perfectly
successful step passes it.

The contract now:

* the goal's declared predicates are the only thing that can **grant** success;
* steps may only **veto**, never grant;
* unknown is not success.
"""

from __future__ import annotations

from app.backend.chat.goal_state import GoalSpec, Predicate, runtime_observer
from app.backend.chat.verification import verify_goal


def files_step(**outcome):
    base = {
        "status": "succeeded",
        "operation": "delete",
        "mutating": True,
        "preserved_paths": [],
        "failed_paths": [],
        "after_state": {"still_present": [], "preserved_present": []},
    }
    return {
        "action": "files.manage",
        "status": "succeeded",
        "observation": {**base, **outcome},
    }


def test_a_capability_returning_succeeded_is_not_verification() -> None:
    """The exact false-success shape, in the smallest form it takes."""

    verified, evidence = verify_goal(
        {"kind": "action", "capability": "terminal.execute",
         "result": {"status": "succeeded", "exit_code": 0}}
    )
    assert verified is None
    assert evidence["reason"] == "no_required_predicates"


def test_discord_report_contract_verifies_dynamic_bounded_read_only_evidence() -> None:
    orchestration = {
        "kind": "action",
        "capability": "discord.inspect",
        "status": "completed",
        "answer": (
            "I found 91 candidates. This is not a complete inventory; coverage "
            "is limited to the first bounded batch."
        ),
        "steps": [
            {
                "action": "discord.inspect",
                "status": "succeeded",
                "observation": {
                    "operation": "find_channels",
                    "candidate_index_size": 91,
                },
            },
            {
                "action": "discord.inspect",
                "status": "succeeded",
                "observation": {
                    "operation": "scan_batch",
                    "scans": [{"selected_channel": {"channel": "giveaways"}}],
                    "scan_gaps": [],
                },
            },
            {"action": "respond", "status": "completed", "observation": {}},
        ],
    }
    spec = GoalSpec(
        goal="inspect Discord",
        required=(Predicate("discord_report_valid", "$discord_report"),),
    )

    verified, evidence = verify_goal(
        orchestration,
        spec=spec,
        observe=runtime_observer(broker=None, orchestration=orchestration),
    )

    assert verified is True
    assert evidence["verified_count"] == 1


def test_discord_report_contract_rejects_mutation_or_missing_scope_disclosure() -> None:
    orchestration = {
        "kind": "action",
        "capability": "discord.inspect",
        "status": "completed",
        "answer": "I checked everything and joined it.",
        "steps": [
            {
                "action": "discord.inspect",
                "status": "succeeded",
                "observation": {
                    "operation": "find_channels",
                    "candidate_index_size": 4,
                },
            },
            {
                "action": "input.control",
                "status": "succeeded",
                "observation": {"operation": "click"},
            },
        ],
    }
    observe = runtime_observer(broker=None, orchestration=orchestration)

    assert observe(Predicate("discord_report_valid", "$discord_report")) is False


def test_recursive_absolute_glob_observes_nested_files(tmp_path) -> None:
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    target = nested / "cache.tmp"
    target.write_text("temporary", encoding="utf-8")
    predicate = Predicate("absent", str(tmp_path / "**" / "*.tmp"))

    observe = runtime_observer(broker=None, orchestration={})

    assert observe(predicate) is False
    target.unlink()
    assert observe(predicate) is True


def test_a_step_summing_verifier_cannot_grant_success_on_its_own() -> None:
    """Both steps did exactly what they claimed. The goal was destroyed.

    A requirement nobody executed is a requirement no step can miss, which is
    why step evidence cannot be the source of success.
    """

    verified, _ = verify_goal(
        {
            "kind": "action",
            "steps": [
                files_step(affected_paths=[r"C:\w\run-a.log"]),
                files_step(affected_paths=[r"C:\w\notes.txt"]),
            ],
        }
    )
    assert verified is not True


def test_declared_predicates_are_what_grant_success() -> None:
    verified, evidence = verify_goal(
        {"kind": "action", "steps": [files_step(affected_paths=[r"C:\w\run-a.log"])]},
        spec=GoalSpec(
            goal="delete the logs, keep the notes",
            required=(
                Predicate("absent", r"C:\w\run-a.log"),
                Predicate("present", r"C:\w\notes.txt", source="protected"),
            ),
        ),
        observe=lambda predicate: predicate.kind == "absent" or True,
    )
    assert verified is True
    assert evidence["required_count"] == 2
    assert evidence["verified_count"] == 2


def test_a_predicate_nobody_observed_keeps_the_turn_unverified() -> None:
    """The shipped run: right outcome on disk, no evidence for the keep half."""

    verified, evidence = verify_goal(
        {"kind": "action", "steps": [files_step(affected_paths=[r"C:\w\run-a.log"])]},
        spec=GoalSpec(
            goal="delete the logs, keep the notes",
            required=(
                Predicate("absent", r"C:\w\run-a.log"),
                Predicate("present", r"C:\w\notes.txt", source="protected"),
            ),
        ),
        observe=lambda predicate: True if predicate.kind == "absent" else None,
    )
    assert verified is None
    assert evidence["unverified"] == [
        {"kind": "present", "subject": r"C:\w\notes.txt"}
    ]


def test_a_step_that_lost_a_protected_path_vetoes_a_satisfied_goal() -> None:
    """Defence in depth: even when every declared predicate passes."""

    verified, evidence = verify_goal(
        {
            "kind": "action",
            "steps": [
                files_step(
                    affected_paths=[r"C:\w\run-a.log"],
                    preserved_paths=[r"C:\w\notes.txt", r"C:\w\plan.md"],
                    after_state={"still_present": [], "preserved_present": []},
                )
            ],
        },
        spec=GoalSpec(goal="g", required=(Predicate("absent", r"C:\w\run-a.log"),)),
        observe=lambda predicate: True,
    )
    assert verified is False
    assert evidence["reason"] == "a_step_contradicted_the_goal"
    assert len(evidence["protected_lost"]) == 2


def test_having_gathered_evidence_is_not_having_answered_the_question() -> None:
    """Two different concepts that were one.

    "Find the cheapest currently in-stock 2TB Gen4 SSD in Bangladesh and give
    me the exact product link" carries the capacity, the region, current stock,
    an exact offer page and freshness. An observation about the wrong capacity,
    or about a seller who is out of stock, is real evidence and is not that
    goal. Observation, claim and source counts measure how much was read.
    """

    verified, evidence = verify_goal(
        {
            "kind": "research",
            "observations": [{"product": "X", "price_display": "1", "url": "https://e"}],
            "research": {"claim_count": 4},
        }
    )
    assert verified is None
    assert evidence["reason"] == "no_required_predicates"
    # What was gathered is still reported. It is information, not a verdict.
    assert evidence["observations"] == 1


def test_research_predicates_are_checked_against_bound_observations() -> None:
    observations = [
        {"product": "Lexar NM790 2TB", "url": "https://shop.test/nm790",
         "stock": "in_stock", "price_display": "40,999 Tk"},
    ]
    spec = GoalSpec(
        goal="cheapest in-stock 2TB, exact link",
        required=(
            Predicate("exact_page", "https://shop.test/nm790"),
            Predicate("stock_confirmed", "Lexar NM790 2TB"),
        ),
    )
    verified, _ = verify_goal(
        {"kind": "research", "observations": observations, "research": {"claim_count": 3}},
        spec=spec,
    )
    assert verified is True


def test_unknown_stock_never_satisfies_a_hard_in_stock_requirement() -> None:
    """"in stock is a must" is a predicate, and unknown is not in stock."""

    verified, _ = verify_goal(
        {
            "kind": "research",
            "observations": [
                {"product": "Lexar NM790 2TB", "url": "https://shop.test/nm790",
                 "stock": "unknown"}
            ],
            "research": {"claim_count": 3},
        },
        spec=GoalSpec(
            goal="cheapest in-stock 2TB",
            required=(Predicate("stock_confirmed", "Lexar NM790 2TB"),),
        ),
    )
    assert verified is False


def test_a_homepage_never_satisfies_a_request_for_an_exact_offer_page() -> None:
    verified, _ = verify_goal(
        {
            "kind": "research",
            "observations": [{"product": "X", "url": "https://shop.test/"}],
            "research": {"claim_count": 1},
        },
        spec=GoalSpec(
            goal="exact product link",
            required=(Predicate("exact_page", "https://shop.test/nm790"),),
        ),
    )
    assert verified is False


def test_research_that_found_nothing_is_not_verified() -> None:
    verified, _ = verify_goal(
        {"kind": "research", "observations": [], "claims": [],
         "research": {"claim_count": 0}}
    )
    assert verified is False


def test_cited_independent_sources_do_not_by_themselves_verify_answer_coverage() -> None:
    verified, evidence = verify_goal(
        {
            "kind": "research",
            "answer": (
                "Python 3.14.7 is released "
                "[officially](https://www.python.org/downloads/release/python-3147) "
                "and independently covered by "
                "[Technical Example](https://technical.example/python-3147)."
            ),
            "sources": [
                {
                    "url": "https://www.python.org/downloads/release/python-3147",
                    "validation": "validated",
                },
                {
                    "url": "https://technical.example/python-3147",
                    "validation": "validated",
                },
            ],
            "claims": [{"text": "Python 3.14.7 is released."}],
            "research": {
                "claim_count": 1,
                "stop_reason": "evidence_sufficient",
            },
        }
    )

    assert verified is None
    assert evidence["reason"] == "answer_coverage_not_verified"
    assert evidence["citations_checked"] is True
    assert evidence["cited_publishers"] == 2


def test_generic_research_cannot_override_a_failed_relevance_gate() -> None:
    verified, evidence = verify_goal(
        {
            "kind": "research",
            "answer": (
                "Python 3.14.1 is current "
                "[officially](https://www.python.org/python-3141) and has "
                "[independent coverage](https://technical.example/python-features)."
            ),
            "sources": [
                {
                    "url": "https://www.python.org/python-3141",
                    "validation": "validated",
                },
                {
                    "url": "https://technical.example/python-features",
                    "validation": "validated",
                },
            ],
            "claims": [{"text": "Python 3.14.1 is current."}],
            "research": {
                "claim_count": 20,
                "stop_reason": "evidence_sufficient",
                "evidence_sufficient": False,
                "relevant_publisher_count": 1,
            },
        }
    )

    assert verified is None
    assert evidence["reason"] == "research_evidence_not_sufficient"
    assert evidence["relevant_publishers"] == 1


def test_generic_research_rejects_an_unvalidated_citation() -> None:
    verified, evidence = verify_goal(
        {
            "kind": "research",
            "answer": "Claim [source](https://invented.example/report).",
            "sources": [
                {
                    "url": "https://official.example/report",
                    "validation": "validated",
                }
            ],
            "claims": [{"text": "Claim."}],
            "research": {
                "claim_count": 1,
                "stop_reason": "evidence_sufficient",
            },
        }
    )

    assert verified is False
    assert evidence["reason"] == "answer_cited_unvalidated_sources"


def test_subdomains_do_not_satisfy_independent_research_citations() -> None:
    verified, evidence = verify_goal(
        {
            "kind": "research",
            "answer": (
                "Claim [docs](https://docs.python.org/3/) and "
                "[blog](https://blog.python.org/post)."
            ),
            "sources": [
                {"url": "https://docs.python.org/3/", "validation": "validated"},
                {"url": "https://blog.python.org/post", "validation": "validated"},
            ],
            "claims": [{"text": "Claim."}],
            "research": {
                "claim_count": 1,
                "stop_reason": "evidence_sufficient",
            },
        }
    )

    assert verified is None
    assert evidence["reason"] == "research_lacks_independent_sources"
    assert evidence["validated_publishers"] == 1


def test_an_ordinary_answer_is_not_a_goal_to_verify() -> None:
    """Nothing reached outside, so there is nothing to check."""

    verified, evidence = verify_goal({"kind": "respond"})
    assert verified is None
    assert evidence["reason"] == "nothing_reached_outside"


def test_extra_successful_work_cannot_compensate_for_a_missing_predicate() -> None:
    verified, _ = verify_goal(
        {
            "kind": "plan",
            "steps": [files_step(affected_paths=[rf"C:\w\extra-{n}.log"]) for n in range(6)],
        },
        spec=GoalSpec(
            goal="g",
            required=(Predicate("present", r"C:\w\notes.txt", source="protected"),),
        ),
        observe=lambda predicate: None,
    )
    assert verified is None


def test_a_failed_predicate_outranks_unrelated_satisfied_ones() -> None:
    verified, evidence = verify_goal(
        {"kind": "plan", "steps": [files_step(affected_paths=[r"C:\w\a.log"])]},
        spec=GoalSpec(
            goal="g",
            required=(
                Predicate("absent", r"C:\w\a.log"),
                Predicate("absent", r"C:\w\b.log"),
                Predicate("present", r"C:\w\notes.txt", source="protected"),
            ),
        ),
        observe=lambda predicate: predicate.kind == "absent",
    )
    assert verified is False
    assert evidence["verified_count"] == 2
    assert len(evidence["failed"]) == 1


def test_window_and_browser_predicates_use_fresh_read_only_observations() -> None:
    calls: list[tuple[str, dict]] = []

    class Broker:
        def invoke(self, request):
            capability = request["capability"]
            arguments = dict(request["arguments"])
            calls.append((capability, arguments))
            if capability == "window.control":
                return {
                    "status": "succeeded",
                    "windows": [
                        {"title": "Project Notes - Notepad", "process_id": 42}
                    ],
                }
            if capability == "browser.control":
                return {
                    "status": "succeeded",
                    "url": "https://example.test/result",
                    "visible": True,
                }
            raise AssertionError(capability)

    spec = GoalSpec(
        goal="open the result visibly",
        required=(
            Predicate("window_present", "Notepad"),
            Predicate("active_url", "https://example.test/result"),
            Predicate("browser_visible", "true"),
        ),
    )

    verified, evidence = verify_goal(
        {"kind": "plan"},
        spec=spec,
        observe=runtime_observer(broker=Broker(), orchestration={"kind": "plan"}),
    )

    assert verified is True
    assert evidence["verified_count"] == 3
    # The two browser predicates share one fresh session-state read.
    assert calls == [
        ("window.control", {"action": "list"}),
        ("browser.control", {"command": "get_session_state"}),
    ]


def test_artifact_predicate_binds_to_the_runtime_artifact_not_a_guessed_path(
    tmp_path,
) -> None:
    artifact = tmp_path / "result.png"
    artifact.write_bytes(b"\x89PNG\r\n\x1a\n" + b"content")
    orchestration = {
        "kind": "plan",
        "steps": [{"observation": {"artifact": {"path": str(artifact)}}}],
    }
    spec = GoalSpec(
        goal="make an image",
        required=(Predicate("artifact_valid", "$artifact"),),
    )

    verified, _evidence = verify_goal(
        orchestration,
        spec=spec,
        observe=runtime_observer(broker=None, orchestration=orchestration),
    )

    assert verified is True


def test_media_playing_requires_fresh_time_progression_not_a_play_click() -> None:
    reads = iter(
        [
            {
                "status": "succeeded",
                "media": [
                    {
                        "element": "web-tab-1:web-el-7",
                        "kind": "video",
                        "playing": True,
                        "current_time": 12.0,
                    }
                ],
            },
            {
                "status": "succeeded",
                "media": [
                    {
                        "element": "web-tab-1:web-el-7",
                        "kind": "video",
                        "playing": True,
                        "current_time": 12.8,
                    }
                ],
            },
        ]
    )

    class Broker:
        def invoke(self, request):
            assert request["capability"] == "browser.control"
            assert request["arguments"] == {"command": "get_media"}
            return next(reads)

    spec = GoalSpec(
        goal="play the video",
        required=(Predicate("media_playing", "$active_media"),),
    )
    verified, _evidence = verify_goal(
        {"kind": "plan"},
        spec=spec,
        observe=runtime_observer(
            broker=Broker(), orchestration={"kind": "plan"}, media_probe_delay=0
        ),
    )

    assert verified is True


def test_paused_media_never_satisfies_playing_even_after_a_successful_click() -> None:
    class Broker:
        def invoke(self, request):
            return {
                "status": "succeeded",
                "media": [
                    {
                        "element": "web-tab-1:web-el-7",
                        "kind": "video",
                        "playing": False,
                        "paused": True,
                        "current_time": 12.0,
                    }
                ],
            }

    spec = GoalSpec(
        goal="play the video",
        required=(Predicate("media_playing", "$active_media"),),
    )
    verified, _evidence = verify_goal(
        {"kind": "plan", "steps": [{"status": "succeeded"}]},
        spec=spec,
        observe=runtime_observer(
            broker=Broker(), orchestration={"kind": "plan"}, media_probe_delay=0
        ),
    )

    assert verified is False
