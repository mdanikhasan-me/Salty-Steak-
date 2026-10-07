from __future__ import annotations

import hashlib
import time
from pathlib import Path

from PIL import Image

from app.backend.chat.service import ChatService
from app.backend.database.control import Database, json_text, new_id, utc_now
from app.backend.operations.manager import OperationManager
from app.backend.runtime.steak_gen import SteakGenPaths, SteakGenResult


class _FakeTextRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def unload(self) -> None:
        self.calls.append("unload")

    def warmup(self):
        self.calls.append("warmup")
        return {"ready": True}


class _FakeImageRuntime:
    def __init__(self, workspace: Path, calls: list[str]) -> None:
        self.paths = SteakGenPaths.from_workspace(workspace)
        self.calls = calls

    def generate(self, request, *, should_stop, on_event, timeout_seconds):
        assert request.prompt == "A small red mug"
        assert request.text_runtime_unloaded is True
        assert not should_stop()
        self.calls.append("generate")
        on_event(
            {"phase": "denoising", "completed": 1, "total": request.steps}
        )
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new(
            "RGB", (request.width, request.height), (220, 24, 24)
        ).save(output, "PNG")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return SteakGenResult(
            output_path=str(output),
            output_sha256=digest,
            width=request.width,
            height=request.height,
            seed=request.seed,
            steps=request.steps,
        )


def _wait_terminal(manager: OperationManager, operation_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        record = manager.get(operation_id)
        if record and record["state"] in {"completed", "failed", "interrupted"}:
            return record
        time.sleep(0.02)
    raise AssertionError("image operation did not finish")


def test_confirmed_image_uses_frozen_prompt_and_commits_one_artifact(tmp_path: Path):
    workspace = tmp_path / "workspace"
    bundle = workspace / "models" / "image-generation" / "steak-gen-1-scaledfp8"
    bundle.mkdir(parents=True)
    database = Database(workspace / "control" / "salty-potato.db")
    manager = OperationManager(database, max_workers=1, recover_incomplete=False)
    calls: list[str] = []
    text_runtime = _FakeTextRuntime()
    image_runtime = _FakeImageRuntime(workspace, calls)
    service = ChatService.__new__(ChatService)
    service.database = database
    service.operations = manager
    service.model_bundle_runtime = text_runtime
    service.image_generation_runtime = image_runtime
    service.image_generation_model = {
        "activation_allowed": True,
        "external_service_required": False,
    }
    service.image_artifact_root = (workspace / "conversations" / "artifacts").resolve()
    service._bundle_lifecycle_lock = __import__("threading").RLock()
    service._generation_lock = __import__("threading").RLock()

    conversation_id = new_id()
    message_id = new_id()
    proposal_id = new_id()
    now = utc_now()
    database.execute(
        "INSERT INTO conversations(id,title,created_at,updated_at) VALUES (?, 'Image', ?, ?)",
        (conversation_id, now, now),
    )
    database.execute(
        """
        INSERT INTO messages(
            id, conversation_id, role, content, sequence,
            technical_details_json, created_at
        ) VALUES (?, ?, 'assistant', 'Image request prepared for review.', 0, ?, ?)
        """,
        (
            message_id,
            conversation_id,
            json_text(
                {
                    "host_action_proposal": {
                        "schema": "salty-steak-host-action-proposal-v1",
                        "id": proposal_id,
                        "kind": "image.generate",
                        "state": "pending_review",
                        "arguments": {"prompt": "A small red mug"},
                    },
                    "turn_duration_ms": 1_200,
                }
            ),
            now,
        ),
    )
    try:
        operation = service.confirm_image_generation(
            conversation_id,
            proposal_id,
            message_id,
            {
                "model_id": "steak-gen-1-scaledfp8",
                "width": 768,
                "height": 512,
                "steps": 12,
                "seed": 7,
            },
        )
        terminal = _wait_terminal(manager, operation["id"])
        assert terminal["state"] == "completed", terminal
        assert service.wait_for_deferred_rewarm(timeout=5)
        assert text_runtime.calls == ["unload", "warmup"]
        assert calls == ["generate"]
        artifacts = database.fetch_all("SELECT * FROM chat_artifacts")
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact["proposal_id"] == proposal_id
        assert artifact["width"] == 768
        assert artifact["height"] == 512
        final = service.image_artifact_root / artifact["relative_path"]
        assert final.is_file()
        assert hashlib.sha256(final.read_bytes()).hexdigest() == artifact["sha256"]
        message = service.get_conversation(conversation_id)["messages"][0]
        assert message["technical_details"]["host_action_proposal"]["state"] == "completed"
        assert message["technical_details"]["generated_image"]["id"] == artifact["id"]
        timing = message["technical_details"]["turn_duration_breakdown"]
        assert timing["measurement"] == "end_to_end_until_image_commit"
        assert timing["text_preparation_ms"] == 1_200
        assert timing["image_operation_ms"] >= 0
        assert timing["total_ms"] == 1_200 + timing["image_operation_ms"]
        assert message["technical_details"]["turn_duration_ms"] == timing["total_ms"]
        assert service._reconcile_image_turn_durations() == 1
        reconciled = service.get_conversation(conversation_id)["messages"][0][
            "technical_details"
        ]
        durable = reconciled["turn_duration_breakdown"]
        assert durable["measurement"] == "durable_operation_timestamps"
        assert durable["total_ms"] >= (
            durable["text_preparation_ms"] + durable["image_operation_ms"]
        )
        assert service._reconcile_image_turn_durations() == 0
        # A replay returns the same durable operation and creates no second image.
        replay = service.confirm_image_generation(
            conversation_id, proposal_id, message_id, {"seed": 999}
        )
        assert replay["id"] == operation["id"]
        assert len(database.fetch_all("SELECT * FROM chat_artifacts")) == 1
    finally:
        manager.shutdown(wait=True)


def test_a_seed_nobody_chose_is_different_every_time(tmp_path: Path):
    """Asking twice for a cow is asking for two cows.

    The default seed was the constant 42, so the same words in a brand-new
    conversation produced a byte-identical picture, and Retry re-rendered
    exactly the image that had just been rejected. An explicit seed still pins
    the render, so reproducibility is something a caller asks for rather than
    an accident of the default.
    """

    seeds: list[int] = []

    class _RecordingRuntime(_FakeImageRuntime):
        def generate(self, request, **kwargs):
            seeds.append(request.seed)
            return super().generate(request, **kwargs)

    workspace = tmp_path / "workspace"
    (workspace / "models" / "image-generation" / "steak-gen-1-scaledfp8").mkdir(
        parents=True
    )
    database = Database(workspace / "control" / "salty-potato.db")
    manager = OperationManager(database, max_workers=1, recover_incomplete=False)
    service = ChatService.__new__(ChatService)
    service.database = database
    service.operations = manager
    service.model_bundle_runtime = _FakeTextRuntime()
    service.image_generation_runtime = _RecordingRuntime(workspace, [])
    service.image_generation_model = {
        "activation_allowed": True,
        "external_service_required": False,
    }
    service.image_artifact_root = (workspace / "conversations" / "artifacts").resolve()
    service._bundle_lifecycle_lock = __import__("threading").RLock()
    service._generation_lock = __import__("threading").RLock()

    conversation_id = new_id()
    now = utc_now()
    database.execute(
        "INSERT INTO conversations(id,title,created_at,updated_at) VALUES (?, 'Image', ?, ?)",
        (conversation_id, now, now),
    )
    try:
        for sequence in range(3):
            message_id = new_id()
            proposal_id = new_id()
            database.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'assistant', 'Creating the photograph: cow.', ?, ?, ?)
                """,
                (
                    message_id,
                    conversation_id,
                    sequence,
                    json_text(
                        {
                            "host_action_proposal": {
                                "schema": "salty-steak-host-action-proposal-v1",
                                "id": proposal_id,
                                "kind": "image.generate",
                                "state": "pending_review",
                                "arguments": {"prompt": "A small red mug"},
                            }
                        }
                    ),
                    now,
                ),
            )
            operation = service.confirm_image_generation(
                conversation_id, proposal_id, message_id
            )
            assert _wait_terminal(manager, operation["id"])["state"] == "completed"
        assert len(set(seeds)) == 3, seeds
        assert 42 not in seeds or len(set(seeds)) == 3
    finally:
        manager.shutdown(wait=True)
