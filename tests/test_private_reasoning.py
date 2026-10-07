"""Private model reasoning must never reach a user-visible surface.

The reported bug was Activity / Response Details showing the model's own
scratchpad. Read out of the live database, the persisted turn for "hi" held:

    details.reasoning_text
        The user just said "hi" and then "/think". This is a greeting, not
        really a request for any specific computer ...

That was rendered on purpose — `reasoningOf()` fed `<p class="response-reasoning">`
in ResponseDetails and `<RichText>` in the Cooking panel. It is not a stray
token echo, so no amount of stripping "/think" addresses it: it is a whole
private channel being published.

The boundary is drawn at the API. Reasoning stays in the database, where it is
legitimate internal diagnostics, and never enters the payload the interface
receives — so no future frontend change can render it back.
"""

from __future__ import annotations

from app.backend.chat.service import PRIVATE_DETAIL_FIELDS, public_technical_details

SCRATCHPAD = (
    'The user just said "hi" and then "/think". This is a greeting, not really '
    "a request for anything. I should reply briefly rather than use JSON "
    "action format. Let me answer warmly."
)


def test_the_scratchpad_never_leaves_the_backend() -> None:
    public = public_technical_details(
        {"reasoning_text": SCRATCHPAD, "generated_output_tokens": 12}
    )
    assert "reasoning_text" not in public
    # Everything that is not private survives untouched.
    assert public["generated_output_tokens"] == 12


def test_explicit_raw_local_developer_view_can_receive_the_scratchpad() -> None:
    public = public_technical_details(
        {
            "reasoning_text": SCRATCHPAD,
            "reasoning_visibility_effective": "raw_local",
        }
    )
    assert public["reasoning_text"] == SCRATCHPAD
    assert public["reasoning_characters"] == len(SCRATCHPAD)


def test_every_private_channel_is_withheld_not_just_the_scratchpad() -> None:
    """Four fields carried model prose. All of them are internal."""

    details = {
        "reasoning_text": SCRATCHPAD,
        "unperformed_claim": "The user wants to open Notepad. I will use the ...",
        "route_trace": {"raw_reply": '{"action":"greeting","reason":"..."}'},
        "orchestration": {
            "kind": "respond",
            "unreadable_decision": '{"action":"greeting","reason":"the user ..."}',
        },
    }
    public = public_technical_details(details)

    for field in PRIVATE_DETAIL_FIELDS:
        assert field not in public
    # Nested private state goes too, while its container survives.
    assert "unreadable_decision" not in public["orchestration"]
    assert public["orchestration"]["kind"] == "respond"


def test_the_summary_says_that_reasoning_happened_without_quoting_it() -> None:
    """Telemetry is not the thing being removed. The prose is."""

    public = public_technical_details({"reasoning_text": SCRATCHPAD})
    assert public["reasoning_characters"] == len(SCRATCHPAD)
    assert public["reasoned"] is True
    assert SCRATCHPAD[:20] not in str(public)


def test_a_turn_that_did_not_reason_says_so() -> None:
    public = public_technical_details({"generated_output_tokens": 4})
    assert public.get("reasoned") is False
    assert public.get("reasoning_characters") == 0


def test_the_original_details_are_not_mutated() -> None:
    """The database keeps its diagnostics; only the payload is narrowed."""

    details = {"reasoning_text": SCRATCHPAD, "orchestration": {"kind": "respond"}}
    public_technical_details(details)
    assert details["reasoning_text"] == SCRATCHPAD


def test_reopening_a_stored_conversation_cannot_reveal_it_either() -> None:
    """History is the case that made this permanent rather than transient.

    The scratchpad is persisted, so every reopen of an old conversation
    re-served it. Narrowing happens on the way out, which covers turns written
    long before this existed.
    """

    stored = {"reasoning_text": SCRATCHPAD, "turn_completion": "cooked"}
    assert "reasoning_text" not in public_technical_details(stored)
    assert public_technical_details(stored)["turn_completion"] == "cooked"


def test_legitimate_operational_detail_is_untouched() -> None:
    """Do not solve a leak by deleting the telemetry around it."""

    details = {
        "turn_completion": "zonted",
        "goal_verified": True,
        "goal_evidence": {"required_count": 3, "verified_count": 3},
        "visible_output_tokens": 210,
        "decode_tokens_per_second": 28.6,
        "orchestration": {
            "kind": "action",
            "steps": [{"action": "files.manage", "status": "succeeded"}],
            "sources": [{"url": "https://example.test", "title": "Example"}],
        },
    }
    public = public_technical_details(details)

    assert public["goal_evidence"]["verified_count"] == 3
    assert public["visible_output_tokens"] == 210
    assert public["orchestration"]["steps"][0]["action"] == "files.manage"
    assert public["orchestration"]["sources"][0]["url"] == "https://example.test"
