from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path

import pytest

from app.backend.datasets.errors import DatasetFormatError
from app.backend.datasets.source_inspection import inspect_source, source_descriptor


def jsonl_bytes() -> bytes:
    return (
        "\n".join(
            json.dumps({"prompt": f"Question {index}", "answer": f"Answer {index}"})
            for index in range(3)
        )
        + "\n"
    ).encode("utf-8")


def test_magic_bytes_override_misleading_extension(tmp_path: Path) -> None:
    source = tmp_path / "not-really-text.txt"
    with gzip.open(source, "wb") as handle:
        handle.write(jsonl_bytes())
    descriptor = source_descriptor(source)
    assert descriptor.compression == "gzip"
    assert descriptor.format == "jsonl"
    inspection = inspect_source(source)
    assert inspection["record_estimate"] == 3
    assert inspection["samples"][1]["answer"] == "Answer 1"


def test_jsonl_gzip_streaming(tmp_path: Path) -> None:
    source = tmp_path / "records.jsonl.gz"
    with gzip.open(source, "wb") as handle:
        handle.write(jsonl_bytes())
    inspection = inspect_source(source)
    assert inspection["format"] == "jsonl"
    assert inspection["compression"] == "gzip"
    assert inspection["encoding"] == "utf-8"
    assert inspection["record_estimate"] == 3


def test_zip_requires_one_supported_member_and_streams_it(tmp_path: Path) -> None:
    source = tmp_path / "dataset.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/records.jsonl", jsonl_bytes())
        archive.writestr("README.md", "metadata")
    inspection = inspect_source(source)
    assert inspection["compression"] == "zip"
    assert inspection["archive_member"] == "nested/records.jsonl"
    assert inspection["record_estimate"] == 3

    ambiguous = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("one.jsonl", jsonl_bytes())
        archive.writestr("two.csv", "a,b\n1,2\n")
    with pytest.raises(DatasetFormatError, match="exactly one"):
        inspect_source(ambiguous)


def test_zstandard_jsonl_when_configured_codec_is_available(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    if not pyarrow.Codec.is_available("zstd"):
        pytest.skip("pyarrow zstd codec unavailable")
    source = tmp_path / "records.jsonl.zst"
    source.write_bytes(pyarrow.compress(jsonl_bytes(), codec="zstd").to_pybytes())
    inspection = inspect_source(source)
    assert inspection["format"] == "jsonl"
    assert inspection["compression"] == "zstd"
    assert inspection["record_estimate"] == 3
