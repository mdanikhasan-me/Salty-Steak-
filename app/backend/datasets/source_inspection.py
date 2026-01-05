"""Read supported dataset sources and produce bounded inspection summaries."""

from __future__ import annotations

import codecs
import csv
import gzip
import io
import json
import zipfile
from contextlib import contextmanager
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DatasetFormatError


SUPPORTED_FORMATS = frozenset({"txt", "jsonl", "json", "csv", "parquet"})
SUPPORTED_COMPRESSIONS = frozenset({"none", "gzip", "zstd", "zip"})


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    format: str
    compression: str
    archive_member: str | None = None
    detection: str = "content_and_magic"


@dataclass(frozen=True, slots=True)
class _RecordEvent:
    index: int
    record: Any | None
    error: str | None = None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    return str(value)


def _zstd_reader(path: Path) -> Any:
    try:
        import zstandard

        raw = path.open("rb")
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        return io.BufferedReader(reader)
    except ImportError:
        try:
            import pyarrow
        except ImportError as exc:
            raise DatasetFormatError(
                "Zstandard sources require the configured zstandard or pyarrow package"
            ) from exc
        if not pyarrow.Codec.is_available("zstd"):
            raise DatasetFormatError(
                "The configured pyarrow package has no Zstandard codec"
            )
        return pyarrow.CompressedInputStream(
            pyarrow.input_stream(str(path), compression=None), "zstd"
        )


@contextmanager
def _open_binary_source(
    path: Path, descriptor: SourceDescriptor
) -> Iterator[Any]:
    if descriptor.compression == "none":
        with path.open("rb") as handle:
            yield handle
        return
    if descriptor.compression == "gzip":
        with gzip.open(path, "rb") as handle:
            yield handle
        return
    if descriptor.compression == "zstd":
        handle = _zstd_reader(path)
        try:
            yield handle
        finally:
            handle.close()
        return
    if descriptor.compression == "zip":
        if not descriptor.archive_member:
            raise DatasetFormatError("ZIP source has no selected dataset member")
        with zipfile.ZipFile(path) as archive:
            with archive.open(descriptor.archive_member, "r") as handle:
                yield handle
        return
    raise DatasetFormatError(
        f"Unsupported source compression: {descriptor.compression}"
    )


@contextmanager
def _open_text_source(
    path: Path,
    descriptor: SourceDescriptor,
    encoding: str,
    *,
    newline: str | None = None,
) -> Iterator[io.TextIOWrapper]:
    with _open_binary_source(path, descriptor) as raw:
        text = io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline=newline)
        try:
            yield text
        finally:


            text.close()


def _format_from_name(name: str) -> str | None:
    lowered = name.casefold()
    for compression_suffix in (".gz", ".gzip", ".zst", ".zstd"):
        if lowered.endswith(compression_suffix):
            lowered = lowered[: -len(compression_suffix)]
            break
    suffix = Path(lowered).suffix.lower().lstrip(".")
    return suffix if suffix in SUPPORTED_FORMATS else None


def _infer_text_format(sample: bytes, name: str) -> str:
    named = _format_from_name(name)
    stripped = sample.lstrip(codecs.BOM_UTF8 + b" \t\r\n")
    if stripped.startswith(b"["):
        return "json"
    if stripped.startswith(b"{"):
        lines = [line for line in stripped.splitlines() if line.strip()]
        if len(lines) > 1:
            try:
                for line in lines[:8]:
                    json.loads(line)
                return "jsonl"
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return "jsonl" if named == "jsonl" else "json"
    if named in {"csv", "txt", "json", "jsonl"}:
        return named
    try:
        decoded = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = ""
    first = decoded.splitlines()[0] if decoded.splitlines() else ""
    if first.count(",") >= 1 or first.count("\t") >= 1:
        return "csv"
    return "txt"


def source_descriptor(path: str | Path) -> SourceDescriptor:
    """Detect physical compression and logical format from magic and content."""

    resolved = Path(path).expanduser().resolve(strict=True)
    with resolved.open("rb") as handle:
        magic = handle.read(8)
    if magic.startswith(b"PAR1"):
        return SourceDescriptor("parquet", "none")
    if magic.startswith(b"\x1f\x8b"):
        descriptor = SourceDescriptor(
            _format_from_name(resolved.name) or "jsonl", "gzip"
        )
        with _open_binary_source(resolved, descriptor) as handle:
            sample = handle.read(128 * 1024)
        return SourceDescriptor(
            "parquet"
            if sample.startswith(b"PAR1")
            else _infer_text_format(sample, resolved.name),
            "gzip",
        )
    if magic.startswith(b"\x28\xb5\x2f\xfd"):
        descriptor = SourceDescriptor(
            _format_from_name(resolved.name) or "jsonl", "zstd"
        )
        with _open_binary_source(resolved, descriptor) as handle:
            sample = handle.read(128 * 1024)
        return SourceDescriptor(
            "parquet"
            if sample.startswith(b"PAR1")
            else _infer_text_format(sample, resolved.name),
            "zstd",
        )
    if magic.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(resolved) as archive:
            candidates = [
                item
                for item in archive.infolist()
                if not item.is_dir() and _format_from_name(item.filename)
            ]
            if len(candidates) != 1:
                raise DatasetFormatError(
                    "ZIP dataset must contain exactly one supported source file; "
                    f"found {len(candidates)}"
                )
            member = candidates[0]
            with archive.open(member) as handle:
                sample = handle.read(128 * 1024)
        return SourceDescriptor(
            "parquet"
            if sample.startswith(b"PAR1")
            else _infer_text_format(sample, member.filename),
            "zip",
            archive_member=member.filename,
        )
    with resolved.open("rb") as handle:
        sample = handle.read(128 * 1024)
    return SourceDescriptor(
        "parquet"
        if sample.startswith(b"PAR1")
        else _infer_text_format(sample, resolved.name),
        "none",
    )


def _detect_encoding(
    path: Path, descriptor: SourceDescriptor | None = None
) -> str:
    descriptor = descriptor or source_descriptor(path)
    with _open_binary_source(path, descriptor) as handle:
        sample = handle.read(128 * 1024)
    if sample.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(
        codecs.BOM_UTF16_BE
    ):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        sample.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise DatasetFormatError(
            f"{path.name} is not valid UTF-8 or BOM-marked UTF-16 text"
        ) from exc
    return encoding


def _source_format(path: Path) -> str:
    return source_descriptor(path).format


def _json_records(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        records = document.get("records")
        if isinstance(records, list):
            return records
        return [document]
    raise DatasetFormatError("JSON dataset must contain an object or an array of records")


def _record_events(
    path: Path,
    format_name: str,
    encoding: str | None,
) -> Iterator[_RecordEvent]:
    descriptor = source_descriptor(path)
    if descriptor.format != format_name:
        raise DatasetFormatError(
            "Stored dataset format differs from current magic/content detection"
        )
    if format_name == "txt":
        assert encoding is not None
        try:
            with _open_text_source(
                path, descriptor, encoding, newline=""
            ) as handle:
                for index, line in enumerate(handle, start=1):
                    yield _RecordEvent(index, {"text": line.rstrip("\r\n")})
        except (OSError, UnicodeError) as exc:
            yield _RecordEvent(1, None, str(exc))
        return

    if format_name == "jsonl":
        assert encoding is not None
        try:
            with _open_text_source(path, descriptor, encoding) as handle:
                for index, line in enumerate(handle, start=1):
                    if not line.strip():
                        yield _RecordEvent(index, None, "empty line")
                        continue
                    try:
                        yield _RecordEvent(index, json.loads(line))
                    except json.JSONDecodeError as exc:
                        yield _RecordEvent(index, None, str(exc))
        except (OSError, UnicodeError) as exc:
            yield _RecordEvent(1, None, str(exc))
        return

    if format_name == "json":
        assert encoding is not None
        try:
            with _open_text_source(path, descriptor, encoding) as handle:
                document = json.load(handle)
            for index, record in enumerate(_json_records(document), start=1):
                yield _RecordEvent(index, record)
        except (OSError, UnicodeError, json.JSONDecodeError, DatasetFormatError) as exc:
            yield _RecordEvent(1, None, str(exc))
        return

    if format_name == "csv":
        assert encoding is not None
        try:
            with _open_text_source(
                path, descriptor, encoding, newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    yield _RecordEvent(1, None, "CSV header is missing")
                    return
                try:
                    for index, record in enumerate(reader, start=2):
                        yield _RecordEvent(index, dict(record))
                except csv.Error as exc:
                    yield _RecordEvent(reader.line_num, None, str(exc))
        except (OSError, UnicodeError, csv.Error) as exc:
            yield _RecordEvent(1, None, str(exc))
        return

    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise DatasetFormatError(
            "Parquet support requires the configured pyarrow package"
        ) from exc
    try:
        with _open_binary_source(path, descriptor) as handle:
            parquet_file = parquet.ParquetFile(handle)
            index = 0
            for batch in parquet_file.iter_batches(batch_size=2048):
                for record in batch.to_pylist():
                    index += 1
                    yield _RecordEvent(index, record)
    except Exception as exc:
        yield _RecordEvent(1, None, str(exc))


def inspect_source(
    source_path: str | Path,
    *,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """Inspect a source without retaining more than the requested sample count."""

    path = Path(source_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise DatasetFormatError(f"Dataset source is not a file: {path}")
    descriptor = source_descriptor(path)
    format_name = descriptor.format
    encoding = (
        None
        if format_name == "parquet"
        else _detect_encoding(path, descriptor)
    )
    samples: list[Any] = []
    errors: list[dict[str, Any]] = []
    fields: set[str] = set()
    record_count = 0
    for event in _record_events(path, format_name, encoding):
        record_count += 1
        if event.error is not None:
            if len(errors) < sample_limit:
                errors.append({"record": event.index, "error": event.error})
            continue
        if isinstance(event.record, Mapping):
            fields.update(str(key) for key in event.record)
        elif event.record is not None:
            fields.add("value")
        if len(samples) < sample_limit:
            samples.append(_json_safe(event.record))
    return {
        "path": str(path),
        "source_filename": path.name,
        "format": format_name,
        "compression": descriptor.compression,
        "archive_member": descriptor.archive_member,
        "detection": descriptor.detection,
        "encoding": encoding,
        "size_bytes": path.stat().st_size,
        "record_estimate": record_count,
        "detected_fields": sorted(fields),
        "samples": samples,
        "inspection_errors": errors,
    }
