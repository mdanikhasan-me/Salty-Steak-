from __future__ import annotations

import hashlib

import gguf
import numpy as np

from tools.rewrite_base_steak_metadata import rewrite


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rewrite_changes_identity_namespace_without_changing_tensors(tmp_path) -> None:
    source = tmp_path / "source.gguf"
    output = tmp_path / "Base-Steak-2.0.gguf"
    writer = gguf.GGUFWriter(source, "qwen35")
    writer.add_name("Contaminated old name")
    writer.add_string("general.basename", "Contaminated old base")
    writer.add_uint32("qwen35.block_count", 1)
    writer.add_string("tokenizer.ggml.pre", "qwen35")
    writer.add_tensor("output.weight", np.arange(12, dtype=np.float32).reshape(3, 4))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    source_hash = _sha256(source)

    report = rewrite(source, output, source_hash)

    assert _sha256(source) == source_hash
    assert report["tensor_payloads_identical"] is True
    assert report["target_architecture"] == "steak20"
    reader = gguf.GGUFReader(output, "r")
    assert reader.get_field("general.architecture").contents() == "steak20"
    assert reader.get_field("general.name").contents() == "Base Steak 2.0"
    assert reader.get_field("general.author").contents() == "MD Anik Hasan (Sawlper)"
    assert reader.get_field("steak20.block_count").contents() == 1
    assert reader.get_field("qwen35.block_count") is None
    assert reader.get_field("tokenizer.ggml.pre").contents() == "steak20"
    assert np.array_equal(reader.tensors[0].data, np.arange(12, dtype=np.float32).reshape(3, 4))
