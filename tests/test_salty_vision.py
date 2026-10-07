from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

import app.backend.runtime.salty_vision as vision_module
from app.backend.runtime.salty_vision import (
    VISION_RUNTIME_FILES,
    SaltyVisionBroker,
    VisionInputPermission,
    VisionPermissionLease,
    deterministic_smoke_image_png,
)


def _broker(tmp_path: Path) -> SaltyVisionBroker:
    text = tmp_path / "text.gguf"
    projector = tmp_path / "projector.gguf"
    text.write_bytes(b"text")
    projector.write_bytes(b"projector")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in VISION_RUNTIME_FILES:
        (runtime / name).write_bytes(name.encode("ascii"))
    return SaltyVisionBroker(
        text_model_path=text,
        projector_path=projector,
        runtime_directory=runtime,
        temporary_root=tmp_path / "temporary",
        text_model_sha256=hashlib.sha256(b"text").hexdigest(),
        projector_sha256=hashlib.sha256(b"projector").hexdigest(),
        timeout_seconds=5,
    )


def test_deterministic_smoke_png_has_stable_identity_and_dimensions() -> None:
    image = deterministic_smoke_image_png()

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", image[16:24]) == (64, 64)
    assert hashlib.sha256(image).hexdigest() == (
        "14a487697c059a674563f01808991bea7652c342936c0bc46967625195deee66"
    )


def test_vision_is_unavailable_until_integrity_and_real_smoke_pass(tmp_path: Path) -> None:
    broker = _broker(tmp_path)

    cold = broker.status()
    assert cold["candidate_built"] is True
    assert cold["available"] is False
    assert cold["application_available"] is False
    assert cold["activation_allowed"] is False
    assert cold["candidate_ready"] is False
    assert cold["integrity_verified"] is False
    assert cold["smoke_passed"] is False

    verified = broker.verify_integrity()
    assert verified["integrity_verified"] is True
    assert verified["available"] is False


def test_permission_is_rejected_before_any_caller_path_is_opened(tmp_path: Path) -> None:
    broker = _broker(tmp_path)
    denied = VisionInputPermission(
        granted=False,
        request_id="request-1",
        source="user_attachment",
        approved_image_sha256="0" * 64,
        approved_path=str(tmp_path / "does-not-exist.png"),
        conversation_id="conversation-1",
        target_model_id="base-steak-2-0-9b-steak20",
    )

    with pytest.raises(PermissionError, match="not explicitly granted"):
        broker.generate(
            prompt="Describe this image.",
            permission=VisionPermissionLease(denied),
            image_path=tmp_path / "does-not-exist.png",
        )


def test_one_shot_broker_binds_image_permission_and_offline_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(tmp_path)
    broker.verify_integrity()
    image = deterministic_smoke_image_png()
    image_sha256 = hashlib.sha256(image).hexdigest()
    permission = VisionInputPermission(
        granted=True,
        request_id="request-2",
        source="user_attachment",
        approved_image_sha256=image_sha256,
        conversation_id="conversation-2",
        target_model_id="base-steak-2-0-9b-steak20",
    )
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "<think>\n\n</think>\n\nA red square.\n", ""

    def popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = dict(kwargs)
        return FakeProcess()

    monkeypatch.setattr(vision_module.subprocess, "Popen", popen)

    result = broker.generate(
        prompt="Describe this image.",
        permission=VisionPermissionLease(permission),
        image_bytes=image,
        maximum_output_tokens=16,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--offline" in command
    assert command[command.index("--device") + 1] == "none"
    assert command[command.index("--gpu-layers") + 1] == "0"
    assert "--no-mmproj-offload" in command
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "timeout" not in kwargs
    assert result.text == "A red square."
    assert result.image_sha256 == image_sha256
    assert result.technical_details["permission"]["request_id"] == "request-2"
    assert "approved_path" not in result.technical_details["permission"]
    assert result.technical_details["permission"]["path_bound"] is False
    assert result.technical_details["network_listener_created"] is False


def test_path_permission_is_bound_to_exact_resolved_path_and_hash(
    tmp_path: Path,
) -> None:
    broker = _broker(tmp_path)
    broker.verify_integrity()
    image = tmp_path / "image.png"
    image.write_bytes(deterministic_smoke_image_png())
    other = tmp_path / "other.png"
    permission = VisionInputPermission(
        granted=True,
        request_id="request-3",
        source="user_attachment",
        approved_image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        approved_path=str(other),
        conversation_id="conversation-3",
        target_model_id="base-steak-2-0-9b-steak20",
    )

    with pytest.raises(PermissionError, match="does not match"):
        broker.generate(
            prompt="Describe this image.",
            permission=VisionPermissionLease(permission),
            image_path=image,
        )


def test_vision_loads_the_hash_bound_identity_adapter_with_a_relative_cli_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _broker(tmp_path)
    adapter = tmp_path / "models" / "identity.gguf"
    adapter.parent.mkdir()
    adapter.write_bytes(b"adapter")
    broker.text_adapters = (
        {
            "id": "identity-v1",
            "path": str(adapter.resolve()),
            "sha256": hashlib.sha256(b"adapter").hexdigest(),
            "scale": 0.5,
        },
    )
    broker.verify_integrity()
    image = deterministic_smoke_image_png()
    permission = VisionInputPermission(
        granted=True,
        request_id="request-adapter",
        source="app_owned_validation",
        approved_image_sha256=hashlib.sha256(image).hexdigest(),
    )
    captured: list[str] = []

    class FakeProcess:
        pid = 4321
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "A red square.", ""

    def popen(command, **_kwargs):
        captured.extend(command)
        return FakeProcess()

    monkeypatch.setattr(vision_module.subprocess, "Popen", popen)

    result = broker.generate(
        prompt="Describe it.",
        permission=permission,
        image_bytes=image,
        maximum_output_tokens=16,
    )

    scaled = captured[captured.index("--lora-scaled") + 1]
    assert scaled.endswith("identity.gguf:0.5")
    assert not Path(scaled.rsplit(":", 1)[0]).is_absolute()
    assert result.technical_details["text_adapters"][0]["sha256"] == hashlib.sha256(
        b"adapter"
    ).hexdigest()
