from __future__ import annotations

import zipfile
from pathlib import Path

from app.backend.chat.attachments import inspect_chat_attachment


def test_plain_text_attachment_is_bounded_and_auditable(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    result = inspect_chat_attachment(path, filename=path.name, media_type="text/markdown")

    assert result["kind"] == "text"
    assert result["extraction"] == "bounded_text"
    assert result["sha256"]
    assert "alpha" in result["prompt_text"]
    assert "notes.md" in result["prompt_text"]


def test_zip_attachment_lists_and_reads_safe_text_members(tmp_path: Path) -> None:
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("src/main.py", "print('ready')")
        archive.writestr("assets/data.bin", b"\x00\x01\x02")
        archive.writestr("../escape.txt", "must not be trusted")

    result = inspect_chat_attachment(path, filename=path.name, media_type="application/zip")

    assert result["kind"] == "archive"
    assert "src/main.py" in result["members"]
    assert "../escape.txt" not in result["members"]
    assert "print('ready')" in result["prompt_text"]
    assert "must not be trusted" not in result["prompt_text"]


def test_unknown_binary_is_accepted_without_false_content_claim(tmp_path: Path) -> None:
    path = tmp_path / "sample.custom"
    path.write_bytes(b"\x00\xff\x10\x80")

    result = inspect_chat_attachment(path, filename=path.name)

    assert result["kind"] == "binary"
    assert result["extraction"] == "metadata_only"
    assert "Do not claim to have read its contents" in result["prompt_text"]

