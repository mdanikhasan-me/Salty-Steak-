from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.application import Application
from app.backend.database.control import Database, SCHEMA_VERSION, utc_now


def _fixture(tmp_path: Path) -> tuple[Application, Path, str]:
    workspace = tmp_path / "workspace"
    database = Database(workspace / "control" / "salty-potato.db")
    conversation_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    artifact_id = "image_12345678"
    relative = Path(conversation_id) / f"{artifact_id}.png"
    artifact = workspace / "conversations" / "artifacts" / relative
    artifact.parent.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\nfixture"
    artifact.write_bytes(payload)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO conversations(id,title,created_at,updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, "Image", now, now),
        )
        connection.execute(
            """
            INSERT INTO operations(id,type,target_id,dedupe_key,state,phase,
                current_progress,created_at,updated_at,finished_at,result_json)
            VALUES (?, 'chat_image_generation', ?, ?, 'completed', 'Completed',
                1, ?, ?, ?, '{}')
            """,
            (operation_id, conversation_id, f"image:{artifact_id}", now, now, now),
        )
        connection.execute(
            """
            INSERT INTO messages(id,conversation_id,role,content,sequence,created_at)
            VALUES (?, ?, 'assistant', '', 0, ?)
            """,
            (message_id, conversation_id, now),
        )
        connection.execute(
            """
            INSERT INTO chat_artifacts(
                id,conversation_id,message_id,operation_id,proposal_id,kind,
                relative_path,media_type,size_bytes,sha256,width,height,
                provenance_json,created_at
            ) VALUES (?, ?, ?, ?, ?, 'image', ?, 'image/png', ?, ?, 64, 64, ?, ?)
            """,
            (
                artifact_id,
                conversation_id,
                message_id,
                operation_id,
                "proposal_12345678",
                str(relative),
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                json.dumps({"no_external_service": True}),
                now,
            ),
        )
    application = Application.__new__(Application)
    application.database = database
    application.paths = SimpleNamespace(conversations=workspace / "conversations")
    return application, artifact, artifact_id


def test_schema_9_adds_the_checksum_bound_chat_artifact_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite")
    assert SCHEMA_VERSION == 17
    columns = {
        row["name"] for row in database.fetch_all("PRAGMA table_info(chat_artifacts)")
    }
    assert {"proposal_id", "relative_path", "sha256", "width", "height"} <= columns


def test_generated_image_delivery_rechecks_size_and_hash(tmp_path: Path) -> None:
    application, artifact, artifact_id = _fixture(tmp_path)
    response = application.image_artifact_content(artifact_id)
    assert response.path == artifact.resolve()
    assert response.content_type == "image/png"
    assert response.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()

    artifact.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="size no longer matches"):
        application.image_artifact_content(artifact_id)


def test_generated_image_delivery_rejects_untrusted_identifiers(tmp_path: Path) -> None:
    application, _artifact, _artifact_id = _fixture(tmp_path)
    with pytest.raises(ValueError, match="identifier"):
        application.image_artifact_content("../../secret")
