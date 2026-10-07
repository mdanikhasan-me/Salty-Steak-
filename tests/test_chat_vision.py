from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

from app.backend.application import Application
from app.backend.chat.service import BASE_STEAK_MODEL_ID
from app.backend.runtime.salty_vision import (
    BASE_STEAK_TEXT_SHA256,
    BASE_STEAK_VISION_SHA256,
    VISION_RUNTIME_ID,
    SaltyVisionCancelled,
    SaltyVisionResult,
    deterministic_smoke_image_png,
)


def _project(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    shutil.copytree(source, tmp_path / "config")
    (tmp_path / "config" / "local.toml").write_text(
        "[training]\ndevice='cpu'\nprecision='fp32'\n[server]\nport=0\n",
        encoding="utf-8",
    )
    return tmp_path


class FakeTextRuntime:
    profile = SimpleNamespace(profile_id="base_steak_2_daily_64k", context_limit=65536)

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.loaded = True

    def unload(self) -> None:
        self.timeline.append("text_unload")
        self.loaded = False

    def warmup(self) -> dict[str, bool]:
        self.timeline.append("text_warmup")
        self.loaded = True
        return {"ready": True}


class FakeVisionBroker:
    projector_sha256 = BASE_STEAK_VISION_SHA256

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    def status(self):
        return {
            "application_available": True,
            "available": True,
            "activation_allowed": True,
            "runtime_id": VISION_RUNTIME_ID,
            "runtime_files_sha256": {"mtmd.dll": "a" * 64},
            "reason": "passed",
        }

    def generate(self, **kwargs):
        self.timeline.append("vision_generate")
        permission = kwargs["permission"].consume()
        assert permission.conversation_id
        assert permission.target_model_id == BASE_STEAK_MODEL_ID
        assert kwargs["should_stop"]() is False
        return SaltyVisionResult(
            text="A red square.",
            image_sha256=permission.approved_image_sha256,
            text_model_sha256=BASE_STEAK_TEXT_SHA256,
            projector_sha256=BASE_STEAK_VISION_SHA256,
            runtime_files_sha256={"mtmd.dll": "a" * 64},
            duration_seconds=0.1,
            command_exit_code=0,
            public_model_name="Base Steak 2.0",
            runtime_id=VISION_RUNTIME_ID,
            technical_details={"network_listener_created": False},
        )


def _enable_vision(application: Application) -> list[str]:
    timeline: list[str] = []
    application.chat.model_bundle_runtime = FakeTextRuntime(timeline)  # type: ignore[assignment]
    application.chat.model_bundle = {
        "id": BASE_STEAK_MODEL_ID,
        "display_name": "Base Steak 2.0",
        "checksum": BASE_STEAK_TEXT_SHA256,
    }
    application.chat.vision_broker = FakeVisionBroker(timeline)  # type: ignore[assignment]
    return timeline


def test_explicit_vision_analysis_persists_exact_provenance_and_success_title(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    timeline = _enable_vision(application)
    try:
        conversation = application.create_conversation()
        source = tmp_path / "selected.png"
        source.write_bytes(deterministic_smoke_image_png())
        staged = application.stage_vision_input(
            source,
            filename=source.name,
            media_type="image/png",
            source="user_attachment",
            consent_evidence={"user_confirmed": True},
        )
        operation = application.analyze_vision_input(
            conversation["id"],
            "Describe it.",
            staged["vision_input_token"],
            "request-key-vision-0001",
            64,
        )
        completed = application.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "completed"

        saved = application.get_conversation(conversation["id"])
        assert saved["title"] == "Describe it."
        assert [message["role"] for message in saved["messages"]] == [
            "user",
            "assistant",
        ]
        details = saved["messages"][1]["technical_details"]
        assert details["vision_analysis"] is True
        assert details["image_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert details["projector_sha256"] == BASE_STEAK_VISION_SHA256
        assert details["source_sha256"] == BASE_STEAK_TEXT_SHA256
        assert details["conversation_context_used"] is False
        assert details["reasoning_control_used"] is False
        assert details["automatic_screen_capture"] is False
        assert details["text_runtime_transition"] == {
            "policy": "unload_text_before_vision_rewarm_after",
            "text_runtime_present": True,
            "text_runtime_unloaded": True,
            "text_runtime_rewarm_attempted": True,
            "text_runtime_rewarm_ready": True,
            "text_runtime_rewarm_error": None,
        }
        assert timeline == ["text_unload", "vision_generate", "text_warmup"]
    finally:
        application.close()


def test_failed_vision_analysis_leaves_empty_new_chat_and_retry_input(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    timeline = _enable_vision(application)

    def fail(**_kwargs):
        raise RuntimeError("expected vision failure")

    application.chat.vision_broker.generate = fail  # type: ignore[method-assign,union-attr]
    try:
        conversation = application.create_conversation()
        source = tmp_path / "selected.png"
        source.write_bytes(deterministic_smoke_image_png())
        staged = application.stage_vision_input(
            source,
            filename=source.name,
            media_type="image/png",
            source="user_attachment",
            consent_evidence={"user_confirmed": True},
        )
        operation = application.analyze_vision_input(
            conversation["id"],
            "Do not keep this as the title.",
            staged["vision_input_token"],
            "request-key-vision-0002",
        )
        completed = application.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "failed"
        saved = application.get_conversation(conversation["id"])
        assert saved["title"] == "New chat"
        assert saved["messages"] == []
        retry = application.retry_vision_analysis(operation["id"])
        assert retry["sha256"] == staged["sha256"]
        assert retry["immutable_retry_payload"] is True
        assert timeline == ["text_unload", "text_warmup"]
    finally:
        application.close()


def test_cancelled_vision_analysis_rewarms_text_runtime_before_completion(
    tmp_path: Path,
) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    timeline = _enable_vision(application)

    def cancelled(**_kwargs):
        timeline.append("vision_generate")
        raise SaltyVisionCancelled("test cancellation")

    application.chat.vision_broker.generate = cancelled  # type: ignore[method-assign,union-attr]
    try:
        conversation = application.create_conversation()
        source = tmp_path / "selected.png"
        source.write_bytes(deterministic_smoke_image_png())
        staged = application.stage_vision_input(
            source,
            filename=source.name,
            media_type="image/png",
            source="user_attachment",
            consent_evidence={"user_confirmed": True},
        )
        operation = application.analyze_vision_input(
            conversation["id"],
            "Stop after this starts.",
            staged["vision_input_token"],
            "request-key-vision-cancel",
        )
        completed = application.operations.wait(operation["id"], timeout=10)
        assert completed["state"] == "interrupted"
        assert timeline == ["text_unload", "vision_generate", "text_warmup"]
    finally:
        application.close()


def test_deleting_conversation_purges_vision_retry_payload(tmp_path: Path) -> None:
    application = Application(_project(tmp_path), recover_operations=False)
    try:
        conversation = application.create_conversation()
        source = tmp_path / "selected.png"
        source.write_bytes(deterministic_smoke_image_png())
        staged = application.vision_inputs.stage(
            source,
            filename=source.name,
            declared_media_type="image/png",
            source="user_attachment",
            consent_evidence={"user_confirmed": True},
        )
        claim = application.vision_inputs.claim(
            staged["vision_input_token"],
            operation_id="operation-delete",
            conversation_id=conversation["id"],
            target_model_id=BASE_STEAK_MODEL_ID,
            prompt="Describe it.",
        )
        payload_directory = claim.image_path.parent
        application.delete_conversation(conversation["id"])
        assert not payload_directory.exists()
    finally:
        application.close()
