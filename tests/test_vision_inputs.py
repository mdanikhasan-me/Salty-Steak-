from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.backend.chat.vision_inputs import VisionInputStore
from app.backend.runtime.salty_vision import deterministic_smoke_image_png


def _stage(store: VisionInputStore, tmp_path: Path) -> tuple[Path, dict]:
    source = tmp_path / "selected.png"
    source.write_bytes(deterministic_smoke_image_png())
    staged = store.stage(
        source,
        filename=source.name,
        declared_media_type="image/png",
        source="user_attachment",
        consent_evidence={"user_confirmed": True, "selection_scope": "one_local_image"},
    )
    return source, staged


def test_vision_input_recomputes_identity_and_token_is_one_use(tmp_path: Path) -> None:
    store = VisionInputStore(tmp_path / "inputs")
    source, staged = _stage(store, tmp_path)
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    claim = store.claim(
        staged["vision_input_token"],
        operation_id="operation-1",
        conversation_id="conversation-1",
        target_model_id="base-steak-2-0-9b",
        prompt="Describe it.",
    )

    assert claim.image_sha256 == expected_hash == staged["sha256"]
    assert claim.image_path != source.resolve()
    assert claim.image_path.read_bytes() == source.read_bytes()
    with pytest.raises(PermissionError, match="already been used"):
        store.claim(
            staged["vision_input_token"],
            operation_id="operation-2",
            conversation_id="conversation-1",
            target_model_id="base-steak-2-0-9b",
            prompt="Again.",
        )


def test_failed_analysis_retry_reuses_immutable_hash_with_new_token(tmp_path: Path) -> None:
    store = VisionInputStore(tmp_path / "inputs")
    _source, staged = _stage(store, tmp_path)
    first = store.claim(
        staged["vision_input_token"],
        operation_id="operation-1",
        conversation_id="conversation-1",
        target_model_id="base-steak-2-0-9b",
        prompt="Describe it.",
    )
    store.finish("operation-1", "failed")

    retry = store.issue_retry("operation-1")
    second = store.claim(
        retry["vision_input_token"],
        operation_id="operation-2",
        conversation_id="conversation-1",
        target_model_id="base-steak-2-0-9b",
        prompt=retry["prompt"],
    )

    assert retry["sha256"] == first.image_sha256 == second.image_sha256
    assert first.image_path == second.image_path
    assert retry["immutable_retry_payload"] is True


def test_delete_conversation_purges_exact_app_owned_images(tmp_path: Path) -> None:
    store = VisionInputStore(tmp_path / "inputs")
    _source, staged = _stage(store, tmp_path)
    claim = store.claim(
        staged["vision_input_token"],
        operation_id="operation-1",
        conversation_id="conversation-to-delete",
        target_model_id="base-steak-2-0-9b",
        prompt="Describe it.",
    )
    directory = claim.image_path.parent
    unrelated_source = tmp_path / "unrelated.png"
    unrelated_source.write_bytes(deterministic_smoke_image_png())
    unrelated = store.stage(
        unrelated_source,
        filename="unrelated.png",
        declared_media_type="image/png",
        source="user_attachment",
        consent_evidence={"user_confirmed": True},
    )

    assert store.purge_conversation("conversation-to-delete") == 1
    assert not directory.exists()
    assert (store.root / unrelated["input_id"]).is_dir()


def test_unclaimed_input_expiration_is_bounded(tmp_path: Path) -> None:
    store = VisionInputStore(tmp_path / "inputs")
    _source, staged = _stage(store, tmp_path)
    metadata = store.root / staged["input_id"] / "input.json"
    old = metadata.stat().st_mtime - 1000
    os.utime(metadata, (old, old))

    assert store.purge_expired_unclaimed(maximum_age_seconds=60) == 1
    assert not metadata.parent.exists()


def test_image_mime_suffix_and_magic_must_all_match(tmp_path: Path) -> None:
    store = VisionInputStore(tmp_path / "inputs")
    source = tmp_path / "fake.png"
    source.write_bytes(b"GIF89a")

    with pytest.raises(ValueError, match="supported"):
        store.stage(
            source,
            filename="fake.png",
            declared_media_type="image/png",
            source="user_attachment",
            consent_evidence={"user_confirmed": True},
        )
