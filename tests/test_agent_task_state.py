"""What the executing loop knows about what the conversation already proved.

The routing generation could see the verified entities and the agent loop
could not. So "open the cheapest one you verified" arrived at the executor as
those seven words and nothing else, and the loop opened a shop's front page
and searched for the drive whose exact address it was already holding.
"""

from __future__ import annotations

from typing import Any

from app.backend.chat.agent_loop import AgentLoop


class _Broker:
    def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("this test never gets as far as executing")


def _loop(replies: list[str], **kwargs: Any) -> tuple[AgentLoop, list[list[dict]]]:
    seen: list[list[dict]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        seen.append([dict(message) for message in messages])
        return replies[min(len(seen) - 1, len(replies) - 1)]

    loop = AgentLoop(
        broker=_Broker(),
        generate=generate,
        capabilities=["browser.control"],
        **kwargs,
    )
    return loop, seen


ESTABLISHED = (
    "\nThis conversation has already established the following.\n"
    '{"entities": [{"ref": "e1", "name": "Lexar NM790 2TB", "price": 15400, '
    '"url": "https://shop.example.com/lexar-nm790-2tb"}]}'
)


def test_the_loop_is_told_what_the_conversation_verified() -> None:
    loop, seen = _loop(
        ['{"action":"respond","answer":"done"}'], established=ESTABLISHED
    )
    loop.run("Open the cheapest one you verified.")

    system = " ".join(
        message["content"] for message in seen[0] if message["role"] == "system"
    )
    assert "already established" in system
    # The exact address, so the loop never has to go and look for it.
    assert "https://shop.example.com/lexar-nm790-2tb" in system
    assert "e1" in system


def test_a_task_with_nothing_established_carries_no_preamble() -> None:
    # A first task in a fresh conversation must not pay for an empty section.
    loop, seen = _loop(['{"action":"respond","answer":"done"}'])
    loop.run("Open Notepad.")

    system = " ".join(
        message["content"] for message in seen[0] if message["role"] == "system"
    )
    assert "already established" not in system


def test_repeated_launch_without_goal_evidence_is_incomplete() -> None:
    """A successful launch alone cannot prove the requested page was reached."""

    from app.backend.chat.agent_loop import AgentLoop

    calls: list[str] = []

    class _OpeningBroker:
        def invoke(self, request: Any, **kwargs: Any) -> dict[str, Any]:
            payload = dict(request or {})
            calls.append(str(payload.get("capability") or ""))
            return {"status": "succeeded", **payload}

    open_it = (
        '{"action":"application.launch","reason":"open the page",'
        '"arguments":{"target":"https://shop.example.com/item"}}'
    )

    def generate(messages: list[dict[str, str]]) -> str:
        # The model keeps asking for the same thing, as it really did.
        return open_it

    loop = AgentLoop(
        broker=_OpeningBroker(),
        generate=generate,
        capabilities=["application.launch"],
        authority_mode="full_access",
        max_iterations=8,
    )
    outcome = loop.run("Open the cheapest one you verified.")

    assert outcome["state"] == "failed", outcome
    assert "incomplete" in outcome["answer"], outcome["answer"]
    assert "ran out of steps" not in outcome["answer"]
    # The page is opened once. The rest is recognised as already true.
    assert calls == ["application.launch"]
    assert len(outcome["steps"]) <= 4


def test_the_task_itself_still_comes_last() -> None:
    # State is context, not the instruction. The request must remain the thing
    # the loop is working on.
    loop, seen = _loop(
        ['{"action":"respond","answer":"done"}'], established=ESTABLISHED
    )
    loop.run("Open the cheapest one you verified.")

    assert seen[0][-1]["role"] == "user"
    assert seen[0][-1]["content"] == "Open the cheapest one you verified."
