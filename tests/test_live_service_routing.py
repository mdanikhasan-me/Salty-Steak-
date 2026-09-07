"""End-to-end through the real _generate_turn.

A user message goes into the production chat path, the model reply routes it,
and the persisted conversation is inspected. Only the model itself is scripted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.application import Application

BOILABIN = (
    "Create a distinctive premium Boilabin ecommerce brand logo and icon. "
    "Boilabin is an ecommerce marketplace. Deliverables: a standalone symbol, a "
    "horizontal lockup with the Boilabin wordmark, and a small monochrome "
    "version. Minimal, premium, flat vector. Do not use a shopping cart. No "
    "shopping bag. Avoid a delivery truck. No generic globe."
)

IMAGE_REPLY = (
    "<think>\nThe user wants a brand identity.\n</think>\n\n"
    "```json\n"
    + json.dumps(
        {
            "action": "generate_image",
            "reason": "The user asked for a brand identity.",
            "brief": {
                "subject": "Boilabin ecommerce brand identity",
                "image_type": "logo",
                "brand": "Boilabin",
                "deliverables": [
                    "standalone symbol",
                    "horizontal symbol with Boilabin wordmark",
                    "small monochrome version",
                ],
            },
        }
    )
    + "\n```"
)


@pytest.fixture()
def application(tmp_path: Path):
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        '[training]\ndevice="cpu"\nprecision="fp32"\n[server]\nhost="127.0.0.1"\nport=0\n',
        encoding="utf-8",
    )
    app = Application(tmp_path, recover_operations=False)
    yield app
    app.close()


def _wire(app, tmp_path: Path, reply: str, *, image_ready: bool = True):
    """Give the application a scripted model and a stated image runtime."""

    source_sha256 = "8" * 64
    captured: dict = {"messages": None}

    class FakeRuntime:
        profile = SimpleNamespace(profile_id="routing-test", context_limit=32768)
        loaded = True

        def describe(self) -> dict:
            return {
                "loaded": self.loaded,
                "runtime_id": "91000000-0000-4000-8000-000000000099",
                "configured_context_limit": 32768,
                "source_sha256": source_sha256,
                "model_path": str(tmp_path / "base.gguf"),
            }

        def load(self) -> dict:
            self.loaded = True
            return self.describe()

        warmup = load

        def unload(self) -> None:
            self.loaded = False

        @staticmethod
        def generate(**kwargs):
            # Kept so a test can inspect exactly what the model was shown.
            # The routing pass is the first: an image turn makes a second call
            # to author the render brief, and recording only the newest one
            # left these assertions reading the brief instruction instead of
            # the capability manifest they are about.
            if captured["messages"] is None:
                captured["messages"] = list(kwargs.get("messages") or [])
                captured["reasoning_mode"] = kwargs.get("reasoning_mode")
            captured.setdefault("calls", []).append(list(kwargs.get("messages") or []))
            generated_reply = reply(kwargs) if callable(reply) else reply
            return SimpleNamespace(
                cancelled=False,
                text=generated_reply,
                token_ids=[1, 2],
                omitted_turns=0,
                finish_reason="end_of_generation",
                technical_details={"finish_reason": "end_of_generation"},
            )

    runtime = FakeRuntime()
    app.model_bundle_runtime = runtime
    app.chat.model_bundle_runtime = runtime
    app.chat.model_bundle = {
        "id": "base-steak-2-0-9b",
        "display_name": "Base Steak 2.0",
        "checksum": source_sha256,
    }
    app.chat.image_generation_model = {
        "activation_allowed": image_ready,
        "external_service_required": False,
        "runtime_loaded": False,
        "runtime_reason": None if image_ready else "Missing local image pipeline",
    }
    return captured


def _send(app, text: str, *, settings=None):
    conversation = app.create_conversation()
    operation = app.chat.start_message(conversation["id"], text, generation_settings=settings)
    completed = app.operations.wait(operation["id"], timeout=20)
    assert completed["state"] == "completed", completed
    messages = app.get_conversation(conversation["id"])["messages"]
    return conversation["id"], messages


# ------------------------------------------------- routing through the path


def test_a_branding_request_reaches_image_generation_with_no_keyword(
    application, tmp_path: Path
) -> None:
    """The exact request that used to fall through the regex."""

    _wire(application, tmp_path, IMAGE_REPLY)

    conversation_id, messages = _send(application, BOILABIN)

    details = messages[1]["technical_details"]
    proposal = details["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert proposal["state"] == "pending_review"
    # The prompt is the checked render brief, not the user's raw message.
    prompt = proposal["arguments"]["prompt"]
    assert "TASK: logo" in prompt
    assert "Boilabin" in prompt
    assert "standalone symbol" in prompt
    # Prohibitions travel as a separate negative prompt. In the positive one
    # they are things to draw, which is how DO NOT INCLUDE ended up written
    # across a real render.
    assert "DO NOT INCLUDE" not in prompt
    negative_prompt = proposal["arguments"]["negative_prompt"].casefold()
    for forbidden in ("cart", "bag", "truck", "globe"):
        assert forbidden in negative_prompt
        assert forbidden not in prompt.casefold()
    # The request itself authorizes the render; no second confirmation is owed.
    # This fixture has no diffusion worker, so the automatic start falls back to
    # the still-persisted proposal after the runtime call fails.
    assert proposal["requires_confirmation"] is False
    assert proposal["execution_allowed"] is True
    assert proposal["authorization_source"] == "explicit_conversation_image_request"


def test_explicit_image_request_starts_without_a_second_confirmation(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(application, tmp_path, IMAGE_REPLY)
    started: list[dict[str, object]] = []

    def start_image(
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        generation_settings=None,
    ) -> dict[str, str]:
        started.append(
            {
                "conversation_id": conversation_id,
                "proposal_id": proposal_id,
                "assistant_message_id": assistant_message_id,
                "generation_settings": generation_settings,
            }
        )
        return {"id": "automatic-image-operation"}

    monkeypatch.setattr(application.chat, "confirm_image_generation", start_image)
    conversation_id, messages = _send(application, BOILABIN)

    assert len(started) == 1
    assert started[0]["conversation_id"] == conversation_id
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert started[0]["proposal_id"] == proposal["id"]
    assert proposal["requires_confirmation"] is False
    assert messages[1]["technical_details"]["turn_completion"] == "rendering"


def test_explicit_image_mode_reuses_a_complete_model_authored_brief(
    application, tmp_path: Path
) -> None:
    complete_reply = json.dumps(
        {
            "subject": "a copper lighthouse at blue hour",
            "image_type": "illustration",
            "style": "cinematic atmospheric lighting",
            "composition": "low angle with the glowing lantern centered",
            "colour": "deep blue twilight and warm copper",
            "background": "calm reflective water and distant rocks",
            "required_elements": ["copper lighthouse", "glowing lantern"],
        }
    )
    captured = _wire(application, tmp_path, complete_reply)

    _conversation_id, messages = _send(
        application,
        "Generate an image of a copper lighthouse at blue hour.",
        settings={"image_mode": True},
    )

    authoring_calls = [
        call
        for call in captured.get("calls", [])
        if any(
            message["role"] == "system"
            and "diffusion image model" in message["content"]
            for message in call
        )
    ]
    assert len(authoring_calls) == 1
    # Image mode is already an explicit command. The only model call authors
    # the visual brief; no separate generation repeats the route decision.
    assert len(captured.get("calls", [])) == 1
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert "copper lighthouse" in proposal["arguments"]["prompt"].casefold()
    assert "deep blue twilight" in proposal["arguments"]["prompt"].casefold()


def test_the_image_job_is_persisted_against_the_conversation(
    application, tmp_path: Path
) -> None:
    _wire(application, tmp_path, IMAGE_REPLY)

    conversation_id, _ = _send(application, BOILABIN)

    stored = application.chat.image_store.latest_for_conversation(conversation_id)
    assert stored is not None
    assert stored.brief.brand == "Boilabin"
    assert stored.revision == 1
    # Survives a fresh reader, as after a restart.
    from app.backend.imaging.store import ImageJobStore

    assert ImageJobStore(application.database).latest_for_conversation(
        conversation_id
    ).job_id == stored.job_id


def test_an_ordinary_question_stays_an_ordinary_answer(
    application, tmp_path: Path
) -> None:
    """Routing must not turn every descriptive request into a picture."""

    _wire(application, tmp_path, "LoRA trains small low-rank adapters instead of the full weights.")

    conversation_id, messages = _send(application, "Explain how LoRA works.")

    details = messages[1]["technical_details"]
    assert "host_action_proposal" not in details
    assert "LoRA" in messages[1]["content"]
    assert application.chat.image_store.latest_for_conversation(conversation_id) is None


def test_research_availability_never_runs_a_keyword_prefetch(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _wire(
        application,
        tmp_path,
        "I would need to research that before making a current claim.",
    )
    searches: list[str] = []
    monkeypatch.setattr(
        application.chat.web_search,
        "search",
        lambda query, limit=6: searches.append(query) or [],
    )

    _conversation_id, messages = _send(
        application,
        "What is the latest CUDA release?",
        settings={"research_available": True, "web_search_enabled": True},
    )

    assert searches == []
    assert "research" in " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    details = messages[1]["technical_details"]
    assert details["web_search"]["state"] == "available"

def test_explicit_research_control_reaches_research_even_when_model_answers_normally(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.backend.chat.runners import LiveRunners
    _wire(application, tmp_path, "An answer from memory is not enough here.")
    calls=[]
    def research(self, *, decision, request):
        calls.append((decision, request, self.research_profile))
        return {"answer":"Verified with public sources.","status":"completed","sources":[],"claims":[]}
    monkeypatch.setattr(LiveRunners,"run_research",research)
    _,messages=_send(application,"How do event loops work?",settings={"research_available":True,"research_command":True})
    assert len(calls)==1, messages[-1]
    assert calls[0][2]=="instant"
    assert "Verified with public sources" in messages[-1]["content"]


def test_research_off_still_offers_bounded_automatic_verification_when_reachable(
    application, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _wire(application, tmp_path, "A plain answer.")
    monkeypatch.setattr(application.chat, "_research_runtime_available", lambda: True)

    _conversation_id, messages = _send(
        application,
        "What is the current CUDA release?",
        settings={"research_available": False, "web_search_enabled": False},
    )

    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    assert '"research"' in system_text
    details = messages[1]["technical_details"]
    assert details["research_profile"] == "verification"
    assert details["web_search"]["state"] == "automatic_verification"


def test_the_routing_instruction_is_absent_when_nothing_is_reachable(
    application, tmp_path: Path
) -> None:
    captured = _wire(application, tmp_path, "Plain answer.", image_ready=False)

    _send(application, "Say hello.")

    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    # No image backend and no granted capability means nothing to offer, so the
    # turn carries no routing preamble at all.
    assert "generate_image" not in system_text


# ------------------------------------------------------- instant vs cooking


@pytest.mark.parametrize("mode", ["instant", "cooking"])
def test_both_modes_route_an_image_request_identically(
    application, tmp_path: Path, mode: str
) -> None:
    captured = _wire(application, tmp_path, IMAGE_REPLY)

    _, messages = _send(
        application, BOILABIN, settings={"reasoning_mode": mode}
    )

    assert captured["reasoning_mode"] == mode
    proposal = messages[1]["technical_details"]["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert "Boilabin" in proposal["arguments"]["prompt"]

    # The capability manifest is identical in both modes; only thinking differs.
    system_text = " ".join(
        message["content"]
        for message in captured["messages"]
        if message["role"] == "system"
    )
    assert "generate_image" in system_text


def test_the_text_model_writes_the_brief_before_the_image_model_draws_it(
    application, tmp_path: Path
) -> None:
    """The step the architecture always described, now actually taken.

    A routing decision is made in the same breath as the answer, so the brief
    inside it is whatever fitted there — "cow", "realistic". A diffusion model
    given three words draws three words. One bounded pass turns the request
    into something worth rendering, and the guard still runs after it.
    """

    captured = _wire(application, tmp_path, IMAGE_REPLY)
    _send(application, BOILABIN)

    calls = captured["calls"]
    # Found by what the call is, not by where it lands. A turn makes several
    # bounded passes — the route is checked against the request before it runs
    # — and pinning an index makes this test fail when another one is added
    # rather than when the brief stops being authored.
    authoring_call = next(
        (
            call
            for call in calls
            if any(
                message["role"] == "system"
                and "diffusion image model" in message["content"]
                for message in call
            )
        ),
        None,
    )
    assert authoring_call is not None, "the brief was never authored"
    authoring = " ".join(
        message["content"] for message in authoring_call if message["role"] == "system"
    )
    # The image model has no negation in its positive conditioning, so what
    # must not appear has to be collected somewhere it can subtract.
    assert "negative_constraints" in authoring
    # The request it is authoring from, not a fresh guess at one.
    user_text = " ".join(
        message["content"] for message in authoring_call if message["role"] == "user"
    )
    assert "Boilabin" in user_text


def test_generate_that_uses_the_assistant_description_as_its_referent(
    application, tmp_path: Path
) -> None:
    described = (
        "A rain-soaked neon street in Dhaka at blue hour, framed through a "
        "cinematic 35mm lens with rickshaw reflections on the pavement."
    )
    latest = "Generate an image of what you just described."

    def scripted(kwargs) -> str:
        messages = list(kwargs.get("messages") or [])
        system = " ".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        if "diffusion image model" in system:
            return json.dumps(
                {
                    "subject": "rain-soaked neon street in Dhaka with rickshaw reflections",
                    "image_type": "concept_art",
                    "style": "cinematic 35mm environment concept art",
                    "background": "blue-hour city haze",
                }
            )
        newest_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if latest in newest_user:
            return json.dumps(
                {
                    "action": "generate_image",
                    "reason": "The user explicitly requested the described scene.",
                    "brief": {
                        "subject": "what you just described",
                        "image_type": "concept_art",
                    },
                }
            )
        return described

    captured = _wire(application, tmp_path, scripted)
    conversation = application.create_conversation()
    first = application.chat.start_message(conversation["id"], "Imagine a cinematic street scene.")
    assert application.operations.wait(first["id"], timeout=20)["state"] == "completed"
    second = application.chat.start_message(conversation["id"], latest)
    assert application.operations.wait(second["id"], timeout=20)["state"] == "completed"

    messages = application.get_conversation(conversation["id"])["messages"]
    proposal = messages[-1]["technical_details"]["host_action_proposal"]
    assert proposal["kind"] == "image.generate"
    assert "rain-soaked neon street" in proposal["arguments"]["prompt"]
    assert "what you just described" not in proposal["arguments"]["prompt"]
    authoring_user_text = " ".join(
        str(message.get("content") or "")
        for call in captured["calls"]
        for message in call
        if message.get("role") == "user"
        and any(
            candidate.get("role") == "system"
            and "diffusion image model" in str(candidate.get("content") or "")
            for candidate in call
        )
    )
    assert described in authoring_user_text


@pytest.mark.parametrize("mode", ["instant", "cooking"])
def test_neither_mode_turns_an_explanation_into_an_image(
    application, tmp_path: Path, mode: str
) -> None:
    _wire(application, tmp_path, "LoRA adds low-rank adapters.")

    _, messages = _send(
        application, "Explain how LoRA works.", settings={"reasoning_mode": mode}
    )

    assert "host_action_proposal" not in messages[1]["technical_details"]
