from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import torch

import app.backend.training.engine as training_engine
from app.backend.training.engine import (
    TrainingSettings,
    inspect_recovery_state,
    run_training,
)
from app.backend.system.files import sha256_file
from app.backend.versions.checkpoint import (
    inspect_checkpoint as real_inspect_checkpoint,
    load_checkpoint as real_load_checkpoint,
    save_completed_checkpoint,
)
from app.backend.versions.model import ModelConfig, create_initial_model
from tests.model_fixtures import build_test_tokenizer


class _StopAfterBoundary:
    def update(self, *_args, **_kwargs) -> None:
        pass

    def stop_requested(self) -> bool:
        return True


class _Continue:
    def update(self, *_args, **_kwargs) -> None:
        pass

    def stop_requested(self) -> bool:
        return False


class _CollectProgress:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    def update(self, phase, **kwargs) -> None:
        self.updates.append((phase, kwargs))

    def stop_requested(self) -> bool:
        return False


def _prepared(path: Path) -> None:
    path.mkdir(parents=True)
    rows = np.array(
        [
            [1, 10, 11, 12, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [1, 13, 14, 15, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [1, 16, 17, 18, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            [1, 19, 20, 21, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ],
        dtype=np.int32,
    )
    lengths = np.full((4,), 5, dtype=np.int32)
    np.save(path / "train-input-ids.npy", rows, allow_pickle=False)
    np.save(path / "train-labels.npy", rows, allow_pickle=False)
    np.save(path / "train-lengths.npy", lengths, allow_pickle=False)
    np.save(path / "validation-input-ids.npy", rows[:2], allow_pickle=False)
    np.save(path / "validation-labels.npy", rows[:2], allow_pickle=False)
    np.save(path / "validation-lengths.npy", lengths[:2], allow_pickle=False)


def test_safe_stop_recovery_resumes_exact_remaining_steps(
    tmp_path: Path, monkeypatch
) -> None:
    tokenizer = build_test_tokenizer(tmp_path / "tokenizer")
    prepared = tmp_path / "prepared"
    _prepared(prepared)
    config = ModelConfig(
        architecture_version=2,
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=128,
        use_gradient_checkpointing=False,
    )
    starting = tmp_path / "starting"
    save_completed_checkpoint(
        starting,
        create_initial_model(config, seed=41),
        tokenizer,
        total_steps=10,
        additional_steps=0,
        dataset_id="dataset",
    )

    def load_tiny(*args, **kwargs):
        return real_load_checkpoint(
            *args, **kwargs, require_production_architecture=False
        )

    monkeypatch.setattr(training_engine, "load_checkpoint", load_tiny)
    monkeypatch.setattr(
        training_engine,
        "inspect_checkpoint",
        lambda *args, **kwargs: real_inspect_checkpoint(
            *args,
            **kwargs,
            require_production_architecture=False,
        ),
    )
    common = TrainingSettings(
        prepared_path=str(prepared),
        prepared_dataset_id="prepared-id",
        prepared_artifact_checksum="a" * 64,
        tokenizer_path=str(tokenizer),
        output_path=str(tmp_path / "stopped-output"),
        recovery_path=str(tmp_path / "training" / "recovery"),
        additional_steps=2,
        starting_checkpoint=str(starting),
        starting_version_id="starting-version",
        starting_weight_sha256=sha256_file(starting / "model.safetensors"),
        base_total_steps=10,
        dataset_id="dataset",
        sequence_length=16,
        micro_batch_size=1,
        gradient_accumulation=1,
        learning_rate=1e-3,
        scheduler="cosine",
        warmup_steps=0,
        weight_decay=0.0,
        gradient_clip=1.0,
        precision="fp32",
        validation_interval=0,
        recovery_interval=1,
        seed=73,
        device="cpu",
        context_limit=128,
    )

    stopped = run_training(common, _StopAfterBoundary())
    assert stopped.interrupted
    assert stopped.additional_steps_completed == 1
    assert stopped.total_trained_steps == 11
    report = inspect_recovery_state(common.recovery_path, verify_checksums=True)
    assert report["valid"], report["errors"]
    assert report["metadata"]["additional_steps_completed"] == 1
    assert report["metadata"]["additional_steps_target"] == 2

    resumed_settings = replace(
        common,
        output_path=str(tmp_path / "resumed-output"),
        resume_state=str(Path(common.recovery_path) / "training_state.pt"),
    )
    resumed = run_training(resumed_settings, _Continue())
    assert not resumed.interrupted
    assert resumed.additional_steps_completed == 2
    assert resumed.total_trained_steps == 12

    uninterrupted_settings = replace(
        common,
        output_path=str(tmp_path / "uninterrupted-output"),
        recovery_path=str(tmp_path / "other-recovery"),
    )
    uninterrupted = run_training(uninterrupted_settings, _Continue())
    assert uninterrupted.total_trained_steps == 12

    resumed_model, _ = real_load_checkpoint(
        resumed.saved_path,
        require_production_architecture=False,
    )
    uninterrupted_model, _ = real_load_checkpoint(
        uninterrupted.saved_path,
        require_production_architecture=False,
    )
    for name, tensor in resumed_model.checkpoint_state().items():
        assert torch.equal(
            tensor,
            uninterrupted_model.checkpoint_state()[name],
        ), name

    unknown_base = run_training(
        replace(
            common,
            base_total_steps=None,
            additional_steps=1,
            output_path=str(tmp_path / "unknown-total-output"),
            recovery_path=str(tmp_path / "unknown-recovery"),
        ),
        _Continue(),
    )
    assert unknown_base.total_trained_steps is None

    final_boundary = replace(
        common,
        additional_steps=1,
        output_path=str(tmp_path / "final-boundary-unused"),
        recovery_path=str(tmp_path / "final-boundary-recovery"),
    )
    stopped_at_target = run_training(final_boundary, _StopAfterBoundary())
    assert stopped_at_target.interrupted
    assert stopped_at_target.additional_steps_completed == 1
    final_report = inspect_recovery_state(
        final_boundary.recovery_path, verify_checksums=True
    )
    assert final_report["valid"], final_report["errors"]
    assert (
        final_report["metadata"]["additional_steps_target"]
        - final_report["metadata"]["additional_steps_completed"]
        == 0
    )
    finalised = run_training(
        replace(
            final_boundary,
            output_path=str(tmp_path / "finalised-output"),
            resume_state=str(
                Path(final_boundary.recovery_path) / "training_state.pt"
            ),
        ),
        _Continue(),
    )
    assert not finalised.interrupted
    assert finalised.additional_steps_completed == 1
    assert finalised.total_trained_steps == 11


def test_amp_nonfinite_update_retries_same_step_without_counting_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tokenizer = build_test_tokenizer(tmp_path / "tokenizer")
    prepared = tmp_path / "prepared"
    _prepared(prepared)
    config = ModelConfig(
        architecture_version=2,
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=128,
        use_gradient_checkpointing=False,
    )
    starting = tmp_path / "starting"
    save_completed_checkpoint(
        starting,
        create_initial_model(config, seed=41),
        tokenizer,
        total_steps=10,
        additional_steps=0,
        dataset_id="dataset",
    )

    monkeypatch.setattr(
        training_engine,
        "load_checkpoint",
        lambda *args, **kwargs: real_load_checkpoint(
            *args,
            **kwargs,
            require_production_architecture=False,
        ),
    )
    monkeypatch.setattr(
        training_engine,
        "inspect_checkpoint",
        lambda *args, **kwargs: real_inspect_checkpoint(
            *args,
            **kwargs,
            require_production_architecture=False,
        ),
    )

    class FakeScaler:
        def __init__(self, *_args, **_kwargs) -> None:
            self.scale_value = 1024.0
            self.step_calls = 0
            self.unscale_calls = 0
            self.skipped = False

        def scale(self, loss):
            return loss

        def unscale_(self, optimizer) -> None:
            self.unscale_calls += 1
            if self.unscale_calls == 1:
                parameter = optimizer.param_groups[0]["params"][0]
                assert parameter.grad is not None
                parameter.grad.reshape(-1)[0] = float("inf")
                self.skipped = True

        def is_enabled(self) -> bool:
            return True

        def get_scale(self) -> float:
            return self.scale_value

        def step(self, optimizer) -> None:
            self.step_calls += 1
            if self.skipped:
                return
            optimizer.step()

        def update(self) -> None:
            if self.skipped:
                self.scale_value /= 2
                self.skipped = False

        def state_dict(self) -> dict:
            return {"scale": self.scale_value}

        def load_state_dict(self, state: dict) -> None:
            self.scale_value = float(state["scale"])

    scaler = FakeScaler()
    monkeypatch.setattr(
        training_engine.torch.amp,
        "GradScaler",
        lambda *_args, **_kwargs: scaler,
    )
    progress = _CollectProgress()
    result = run_training(
        TrainingSettings(
            prepared_path=str(prepared),
            prepared_dataset_id="prepared-id",
            prepared_artifact_checksum="a" * 64,
            tokenizer_path=str(tokenizer),
            output_path=str(tmp_path / "output"),
            recovery_path=str(tmp_path / "recovery"),
            additional_steps=1,
            starting_checkpoint=str(starting),
            starting_version_id="starting-version",
            starting_weight_sha256=sha256_file(
                starting / "model.safetensors"
            ),
            base_total_steps=10,
            dataset_id="dataset",
            sequence_length=16,
            micro_batch_size=1,
            gradient_accumulation=1,
            learning_rate=1e-3,
            scheduler="cosine",
            warmup_steps=0,
            weight_decay=0.0,
            gradient_clip=1.0,
            precision="fp32",
            validation_interval=0,
            recovery_interval=1,
            seed=73,
            device="cpu",
            context_limit=128,
        ),
        progress,
    )

    assert scaler.step_calls == 2
    assert result.additional_steps_completed == 1
    assert result.total_trained_steps == 11
    skipped = [
        details
        for phase, details in progress.updates
        if phase == "Retrying skipped FP16 update"
    ]
    assert len(skipped) == 1
    assert skipped[0]["current"] == 0
    final_training = [
        details["result"]
        for phase, details in progress.updates
        if phase == "Training" and details.get("result")
    ][-1]
    assert final_training["amp_skipped_updates"] == 1
    assert (
        final_training["attempted_valid_target_tokens_processed"]
        == 2 * final_training["valid_target_tokens_processed"]
    )
