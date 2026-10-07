from __future__ import annotations

from pathlib import Path

import pytest

from app.backend.training.engine import TrainingSettings


def test_default_training_batch_preserves_effective_batch_sixteen() -> None:
    settings = TrainingSettings(
        prepared_path="prepared",
        prepared_dataset_id=None,
        prepared_artifact_checksum=None,
        tokenizer_path="tokenizer",
        output_path="output",
        recovery_path="recovery",
        additional_steps=1,
    )
    assert settings.sequence_length == 512
    assert settings.micro_batch_size == 16
    assert settings.gradient_accumulation == 1
    assert settings.micro_batch_size * settings.gradient_accumulation == 16


_PERFORMANCE_EVIDENCE = (
    Path(__file__).resolve().parents[1] / "validation" / "performance-r4"
)


@pytest.mark.skipif(
    not (_PERFORMANCE_EVIDENCE / "benchmark.py").is_file(),
    reason="Historical performance-r4 evidence was intentionally retired.",
)
def test_performance_harness_is_outside_the_application_database() -> None:
    harness = _PERFORMANCE_EVIDENCE
    assert (harness / "benchmark.py").is_file()
    assert "salty-potato.db" not in (harness / "benchmark.py").read_text(
        encoding="utf-8"
    )
