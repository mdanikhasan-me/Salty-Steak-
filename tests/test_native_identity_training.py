from __future__ import annotations

import hashlib
from pathlib import Path

import gguf
import numpy as np

from app.backend.training import native_identity

from app.backend.training.native_identity import (
    NativeIdentitySettings,
    NativeTraceSample,
    _trace_label_offsets,
    export_identity_adapter,
    load_output_adapter_initialization,
    train_output_adapter,
    identity_vocabulary,
    write_dataset_artifacts,
)


def test_retention_traces_are_bounded_anchors_not_a_full_hidden_space() -> None:
    assert _trace_label_offsets(1, retain=True) == (0,)
    assert _trace_label_offsets(2, retain=True) == (0, 1)
    assert _trace_label_offsets(11, retain=True) == (0, 5, 10)
    assert _trace_label_offsets(11, retain=False) == tuple(range(11))


def test_identity_vocabulary_includes_hard_negatives_without_targeting_them(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        native_identity,
        "_token_ids",
        lambda runtime, text: {
            "good": [1],
            "Sawlperho": [91, 92],
            "<|im_end|>": [2],
        }[text],
    )
    example = native_identity.IdentityExample(
        id="example",
        category="identity",
        messages=(("user", "prompt"),),
        response="good",
    )

    vocabulary = identity_vocabulary(
        object(),
        [example],
        hard_negative_texts=("Sawlperho",),
    )

    assert vocabulary == {1, 2, 91, 92}
from app.backend.training.base_steak_identity_dataset import (
    holdout_examples,
    retention_examples,
    training_examples,
)


def _sample(example_id: str, hidden, target: int | None) -> NativeTraceSample:
    return NativeTraceSample(
        example_id=example_id,
        category="retention" if target is None else "identity",
        hidden=np.asarray(hidden, dtype=np.float32),
        candidates=np.asarray([1, 2, 3, 4], dtype=np.int64),
        base_logits=np.asarray([2.0, 1.0, 0.0, -1.0], dtype=np.float32),
        target_token=target,
    )


def test_low_rank_training_learns_unseen_identity_without_moving_retention(
    monkeypatch,
) -> None:
    observed_batch_sizes: list[int] = []
    original_adapter_delta = native_identity._adapter_delta

    def bounded_adapter_delta(hidden, candidates, lora_a, lora_b):
        observed_batch_sizes.append(int(hidden.shape[0]))
        return original_adapter_delta(hidden, candidates, lora_a, lora_b)

    monkeypatch.setattr(native_identity, "_adapter_delta", bounded_adapter_delta)
    settings = NativeIdentitySettings(
        rank=2,
        epochs=300,
        learning_rate=0.05,
        retention_weight=8.0,
        minimum_train_accuracy=1.0,
        minimum_holdout_accuracy=1.0,
        minimum_epochs_before_early_stop=20,
        sample_batch_size=1,
        seed=17,
    )
    result = train_output_adapter(
        train_samples=[
            _sample("train-a", [1.0, 0.0, 0.0, 0.0], 3),
            _sample("train-b", [0.9, 0.1, 0.0, 0.0], 3),
        ],
        holdout_samples=[_sample("holdout", [0.8, 0.2, 0.0, 0.0], 3)],
        retention_samples=[_sample("retain", [0.0, 0.0, 1.0, 0.0], None)],
        hidden_size=4,
        vocabulary_size=8,
        settings=settings,
        device_name="cpu",
    )

    final = result.metrics["final"]
    assert final["adapted_train_accuracy"] == 1.0
    assert final["adapted_holdout_accuracy"] == 1.0
    assert final["baseline_holdout_accuracy"] == 0.0
    assert final["retention_delta_max"] < 0.1
    assert result.metrics["sample_batch_size"] == 1
    assert result.metrics["epochs_completed"] == 20
    assert max(observed_batch_sizes) == 1


def test_exported_identity_adapter_is_a_steak20_gguf(tmp_path) -> None:
    settings = NativeIdentitySettings(
        rank=2,
        epochs=1,
        minimum_epochs_before_early_stop=1,
    )
    learned = type(
        "Result",
        (),
        {
            "lora_a": np.zeros((2, 4), dtype=np.float32),
            "lora_b": np.zeros((8, 2), dtype=np.float32),
        },
    )()
    output = tmp_path / "identity.gguf"

    report = export_identity_adapter(
        output,
        result=learned,
        settings=settings,
        base_model_sha256="a" * 64,
        train_dataset_sha256="b" * 64,
        holdout_dataset_sha256="c" * 64,
        retention_dataset_sha256="d" * 64,
    )

    reader = gguf.GGUFReader(output, "r")
    assert report["target_tensor"] == "output.weight"
    assert reader.get_field("general.architecture").contents() == "steak20"
    assert reader.get_field("general.name").contents() == "Base Steak 2.0 Identity Adapter"
    assert reader.get_field("general.author").contents() == "MD Anik Hasan (Sawlper)"
    assert {tensor.name for tensor in reader.tensors} == {
        "output.weight.lora_a",
        "output.weight.lora_b",
    }

    lora_a, lora_b, source = load_output_adapter_initialization(
        output,
        expected_sha256=report["sha256"],
    )
    assert lora_a.shape == (2, 4)
    assert lora_b.shape == (8, 2)
    assert source["rank"] == 2
    assert source["sha256"] == report["sha256"]


def test_dataset_manifest_hashes_the_exact_bytes_written_to_disk(tmp_path) -> None:
    result = write_dataset_artifacts(
        tmp_path / "dataset",
        training=training_examples(),
        holdout=holdout_examples(),
        retention=retention_examples(),
    )

    for record in result.values():
        payload = Path(record["path"]).read_bytes()
        assert b"\r\n" not in payload
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
