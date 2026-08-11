"""Bounded, local inspection for files attached to a Chat turn.

The browser is deliberately not asked to guess whether a file is text.  Files
arrive here as bytes, are hashed, classified, and (when the format is safely
inspectable) converted into a bounded prompt excerpt.  Unknown binary formats
are still valid attachments: their trustworthy metadata is returned without
pretending their contents were read.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import html
import json
import lzma
import mimetypes
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree


MAX_ATTACHMENT_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_CHARACTERS = 96_000
MAX_ARCHIVE_MEMBERS = 256
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024

TEXT_SUFFIXES = {
    ".asm", ".bat", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css",
    ".csv", ".diff", ".env", ".go", ".h", ".hpp", ".htm", ".html", ".ini",
    ".java", ".js", ".json", ".jsonl", ".jsx", ".kt", ".log", ".lua", ".md",
    ".mjs", ".php", ".plist", ".ps1", ".py", ".rb", ".rs", ".sh", ".sql",
    ".svg", ".swift", ".tex", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".xml",
    ".yaml", ".yml",
}
OOXML_SUFFIXES = {".docx", ".pptx", ".xlsx"}
ARCHIVE_SUFFIXES = {
    ".zip", ".tar", ".tgz", ".tbz", ".tbz2", ".txz", ".gz", ".bz2", ".xz",
}


def inspect_chat_attachment(
    path: str | Path,
    *,
    filename: str,
    media_type: str = "",
) -> dict[str, Any]:
    """Return auditable metadata plus a bounded model-readable excerpt."""

    source = Path(path)
    size = source.stat().st_size
    if size < 1:
        raise ValueError("The selected file is empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError("Chat attachments are limited to 512 MiB per file.")

    checked_name = Path(str(filename).replace("\\", "/")).name or "file"
    suffix = source.suffix.casefold()
    declared = str(media_type or "").strip().casefold()
    guessed = mimetypes.guess_type(checked_name)[0] or "application/octet-stream"
    digest = _sha256(source)
    kind = "binary"
    text = ""
    members: list[str] = []
    extraction = "metadata_only"
    truncated = False

    if suffix in OOXML_SUFFIXES:
        kind = "document"
        text, members, truncated = _inspect_ooxml(source, suffix)
        extraction = "ooxml_text" if text else "metadata_only"
    elif suffix == ".zip" or declared in {"application/zip", "application/x-zip-compressed"}:
        kind = "archive"
        text, members, truncated = _inspect_zip(source)
        extraction = "safe_archive_text"
    elif suffix in {".tar", ".tgz", ".tbz", ".tbz2", ".txz"} or tarfile.is_tarfile(source):
        kind = "archive"
        text, members, truncated = _inspect_tar(source)
        extraction = "safe_archive_text"
    elif suffix in {".gz", ".bz2", ".xz"}:
        kind = "compressed"
        text, truncated = _inspect_single_stream(source, suffix)
        members = [checked_name.rsplit(".", 1)[0] or checked_name]
        extraction = "safe_compressed_text" if text else "metadata_only"
    elif suffix in TEXT_SUFFIXES or declared.startswith("text/") or _looks_textual(source):
        kind = "text"
        text, truncated = _read_text(source, MAX_EXTRACTED_CHARACTERS)
        extraction = "bounded_text"
    elif suffix == ".pdf" or declared == "application/pdf":
        kind = "pdf"
        members = _pdf_metadata(source)
        extraction = "pdf_metadata"

    prompt_text = _prompt_block(
        filename=checked_name,
        size=size,
        media_type=declared or guessed,
        sha256=digest,
        kind=kind,
        extraction=extraction,
        text=text,
        members=members,
        truncated=truncated,
    )
    return {
        "schema": "salty-steak-chat-attachment-v1",
        "name": checked_name,
        "size": size,
        "media_type": declared or guessed,
        "sha256": digest,
        "kind": kind,
        "extraction": extraction,
        "truncated": truncated,
        "members": members[:MAX_ARCHIVE_MEMBERS],
        "prompt_text": prompt_text,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_textual(path: Path) -> bool:
    sample = path.read_bytes()[:8_192]
    if not sample or b"\x00" in sample:
        return False
    decoded = sample.decode("utf-8", errors="replace")
    replacements = decoded.count("\ufffd")
    controls = sum(ord(character) < 9 or 13 < ord(character) < 32 for character in decoded)
    return replacements <= max(2, len(decoded) // 100) and controls <= max(2, len(decoded) // 100)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_text(path: Path, limit: int) -> tuple[str, bool]:
    with path.open("rb") as source:
        data = source.read(max(limit * 4, limit) + 1)
    text = _decode(data)
    return text[:limit], len(text) > limit or path.stat().st_size > len(data)


def _safe_member(name: str) -> str | None:
    normalised = str(name or "").replace("\\", "/")
    path = PurePosixPath(normalised)
    if not normalised or path.is_absolute() or ".." in path.parts:
        return None
    return "/".join(part for part in path.parts if part not in {"", "."})[:500]


def _text_from_bytes(data: bytes, name: str) -> str:
    suffix = Path(name).suffix.casefold()
    if suffix not in TEXT_SUFFIXES and b"\x00" in data[:8_192]:
        return ""
    if suffix not in TEXT_SUFFIXES:
        decoded = data[:8_192].decode("utf-8", errors="replace")
        if decoded.count("\ufffd") > max(2, len(decoded) // 100):
            return ""
    return _decode(data)


def _inspect_zip(path: Path) -> tuple[str, list[str], bool]:
    excerpts: list[str] = []
    names: list[str] = []
    extracted = 0
    truncated = False
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            truncated = True
        for info in infos[:MAX_ARCHIVE_MEMBERS]:
            safe_name = _safe_member(info.filename)
            if safe_name is None or info.is_dir():
                continue
            names.append(safe_name)
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                truncated = True
                continue
            extracted += int(info.file_size)
            if extracted > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                truncated = True
                break
            remaining = MAX_EXTRACTED_CHARACTERS - sum(len(item) for item in excerpts)
            if remaining <= 0:
                truncated = True
                break
            data = archive.read(info)
            text = _text_from_bytes(data, safe_name)
            if text:
                excerpts.append(f"--- {safe_name} ---\n{text[:remaining]}")
                if len(text) > remaining:
                    truncated = True
    return "\n\n".join(excerpts)[:MAX_EXTRACTED_CHARACTERS], names, truncated


def _inspect_tar(path: Path) -> tuple[str, list[str], bool]:
    excerpts: list[str] = []
    names: list[str] = []
    extracted = 0
    truncated = False
    with tarfile.open(path, mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            truncated = True
        for member in members[:MAX_ARCHIVE_MEMBERS]:
            safe_name = _safe_member(member.name)
            if safe_name is None or not member.isfile():
                continue
            names.append(safe_name)
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                truncated = True
                continue
            extracted += int(member.size)
            if extracted > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                truncated = True
                break
            stream = archive.extractfile(member)
            if stream is None:
                continue
            text = _text_from_bytes(stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1), safe_name)
            remaining = MAX_EXTRACTED_CHARACTERS - sum(len(item) for item in excerpts)
            if text and remaining > 0:
                excerpts.append(f"--- {safe_name} ---\n{text[:remaining]}")
                if len(text) > remaining:
                    truncated = True
            elif remaining <= 0:
                truncated = True
                break
    return "\n\n".join(excerpts)[:MAX_EXTRACTED_CHARACTERS], names, truncated


def _inspect_single_stream(path: Path, suffix: str) -> tuple[str, bool]:
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[suffix]
    with opener(path, "rb") as source:
        data = source.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    text = _text_from_bytes(data, path.stem)
    return text[:MAX_EXTRACTED_CHARACTERS], len(data) > MAX_ARCHIVE_MEMBER_BYTES or len(text) > MAX_EXTRACTED_CHARACTERS


def _inspect_ooxml(path: Path, suffix: str) -> tuple[str, list[str], bool]:
    prefixes = {
        ".docx": ("word/",),
        ".pptx": ("ppt/slides/", "ppt/notesSlides/"),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
    }[suffix]
    parts: list[str] = []
    names: list[str] = []
    truncated = False
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not any(info.filename.startswith(prefix) for prefix in prefixes):
                continue
            if not info.filename.casefold().endswith(".xml"):
                continue
            names.append(info.filename)
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                truncated = True
                continue
            try:
                root = ElementTree.fromstring(archive.read(info))
            except ElementTree.ParseError:
                continue
            text = " ".join(value.strip() for value in root.itertext() if value.strip())
            if text:
                parts.append(text)
            if sum(len(item) for item in parts) >= MAX_EXTRACTED_CHARACTERS:
                truncated = True
                break
    joined = "\n\n".join(parts)
    return joined[:MAX_EXTRACTED_CHARACTERS], names, truncated or len(joined) > MAX_EXTRACTED_CHARACTERS


def _pdf_metadata(path: Path) -> list[str]:
    sample = path.read_bytes()[:1_000_000]
    version = re.search(rb"%PDF-([0-9.]+)", sample)
    pages = len(re.findall(rb"/Type\s*/Page\b", sample))
    result = [f"PDF version {version.group(1).decode('ascii') if version else 'unknown'}"]
    if pages:
        result.append(f"At least {pages} page object(s) were detected")
    return result


def _prompt_block(
    *,
    filename: str,
    size: int,
    media_type: str,
    sha256: str,
    kind: str,
    extraction: str,
    text: str,
    members: list[str],
    truncated: bool,
) -> str:
    metadata = {
        "name": filename,
        "size_bytes": size,
        "media_type": media_type,
        "sha256": sha256,
        "kind": kind,
        "extraction": extraction,
        "truncated": truncated,
        "members": members[:MAX_ARCHIVE_MEMBERS],
    }
    block = [
        f"--- BEGIN USER-ATTACHED FILE: {filename} ---",
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    ]
    if text:
        block.extend(["Extracted content:", text])
    elif kind == "binary":
        block.append(
            "The file was attached successfully, but this build has no safe content "
            "extractor for this binary format. Do not claim to have read its contents."
        )
    elif kind == "pdf":
        block.append(
            "PDF metadata was inspected, but text extraction is unavailable in this "
            "dependency-free path. Do not infer unseen page content."
        )
    block.append(f"--- END USER-ATTACHED FILE: {filename} ---")
    return "\n".join(block)
