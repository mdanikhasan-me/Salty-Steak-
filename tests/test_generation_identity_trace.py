from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.backend.application import Application
from app.backend.database.control import utc_now
from tools.trace_generation import trace_generation


def test_generation_trace_resolves_committed_message_and_checkpoint(
    tmp_path: Path,
) -> None:
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "config",
        tmp_path / "config",
    )
    application = Application(tmp_path, recover_operations=False)
    try:
        version_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        checkpoint = application.paths.versions / version_id
        checkpoint.mkdir(parents=True)
        (checkpoint / "model.safetensors").write_bytes(b"fixture")
        (checkpoint / "config.json").write_text(
            '{"model_type":"salty_potato"}',
            encoding="utf-8",
        )
        now = utc_now()
        application.database.execute(
            """
            INSERT INTO saved_versions(
                id, label, checkpoint_path, checksum, size_bytes,
                total_trained_steps, additional_steps, architecture_revision,
                context_tokens, tokenizer_checksum, integrity, imported,
                created_at, verified_at
            ) VALUES (?, 'Trace fixture', ?, ?, 7, 1, 1, 2, 8192, ?,
                      'verified', 1, ?, ?)
            """,
            (
                version_id,
                str(checkpoint),
                "b" * 64,
                "c" * 64,
                now,
                now,
            ),
        )
        conversation = application.create_conversation()
        user_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
        assistant_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"
        generation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa4"
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, created_at
            ) VALUES (?, ?, 'user', 'hello', 0, ?)
            """,
            (user_id, conversation["id"], now),
        )
        application.database.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, sequence, saved_version_id,
                technical_details_json, created_at
            ) VALUES (?, ?, 'assistant', 'response', 1, ?, ?, ?)
            """,
            (
                assistant_id,
                conversation["id"],
                version_id,
                json.dumps(
                    {
                        "generation_id": generation_id,
                        "user_message_id": user_id,
                        "active_version_id": version_id,
                        "template_version": "salty-chat-v1",
                    }
                ),
                now,
            ),
        )

        result = trace_generation(application.paths.database, generation_id)

        assert result["chat_generation"]["assistant_message_id"] == assistant_id
        assert result["saved_version"]["id"] == version_id
        assert result["identity_summary"]["checkpoint_digest"] == "b" * 64
        assert result["complete"] is False
        assert "raw_dataset" in result["missing_links"]
    finally:
        application.close()
