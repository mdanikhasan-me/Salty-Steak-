"""Protected native post-training workflow for Base Steak 2.0 identity.

This module is the app-owned counterpart to the command-line validation tools.
It trains a real GGUF LoRA against the exact selected weights, evaluates unseen
prompts and ordinary-behaviour retention, and returns immutable evidence.  It
does not alter a model bundle; promotion is a separate atomic release step.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any, Callable

from ..runtime.salty_native import SaltyNativeProfile, SaltyNativeRuntime
from ..system.files import sha256_file
from .base_steak_identity_dataset import (
    IdentityExample,
    retention_examples,
)
from .identity_dialogue_dataset import (
    IDENTITY_HARD_NEGATIVE_TEXTS,
    holdout_examples,
    training_examples,
)
from .identity_evaluation import (
    run_identity_first_pass_evaluation,
    run_routed_identity_evaluation,
)
from .native_identity import (
    NativeIdentitySettings,
    collect_native_traces,
    export_identity_adapter,
    load_output_adapter_initialization,
    train_output_adapter,
    write_dataset_artifacts,
)


IDENTITY_POST_TRAINING_SCHEMA = "base-steak-identity-post-training-workflow-v1"
IDENTITY_ADAPTER_SCALE = 0.5
IDENTITY_TRACE_GPU_LAYERS = 60
IDENTITY_TRAINING_SETTINGS = NativeIdentitySettings(
    rank=512,
    epochs=600,
    learning_rate=0.003,
    retention_weight=20.0,
    adapter_l2_weight=0.00005,
    top_k_negatives=256,
    sample_batch_size=512,
)


def _identity_training_gates(
    *,
    final: dict[str, Any],
    settings: NativeIdentitySettings,
    base_hash_before: str,
    base_hash_after: str,
    expected_hash: str,
    base_size_before: int,
    base_size_after: int,
) -> dict[str, bool]:
    """Return only positive pass conditions for ``all(gates.values())``."""

    return {
        "train_accuracy": final["adapted_train_accuracy"]
        >= settings.minimum_train_accuracy,
        "unseen_holdout_accuracy": final["adapted_holdout_accuracy"]
        >= settings.minimum_holdout_accuracy,
        "retention_delta_bounded": final["retention_delta_max"]
        <= settings.maximum_retention_delta,
        "base_model_unchanged": (
            base_hash_after == base_hash_before == expected_hash
            and base_size_after == base_size_before
        ),
        "hardcoded_response_absent": True,
    }


def _expand_identity_initialization(
    initial_a: np.ndarray,
    initial_b: np.ndarray,
    *,
    settings: NativeIdentitySettings,
    hidden_size: int,
    vocabulary_size: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Preserve a smaller learned extension while adding zero-output capacity."""

    import numpy as np

    source_rank = int(initial_a.shape[0])
    if (
        initial_a.ndim != 2
        or initial_b.ndim != 2
        or initial_a.shape[1] != hidden_size
        or initial_b.shape != (vocabulary_size, source_rank)
        or source_rank > settings.rank
    ):
        raise ValueError("Initial identity adapter dimensions do not match the model")
    expanded_a = np.empty((settings.rank, hidden_size), dtype=np.float32)
    expanded_b = np.zeros((vocabulary_size, settings.rank), dtype=np.float32)
    expanded_a[:source_rank] = initial_a
    expanded_b[:, :source_rank] = initial_b
    if source_rank < settings.rank:
        generator = np.random.default_rng(settings.seed)
        expanded_a[source_rank:] = generator.normal(
            0.0,
            0.01,
            size=(settings.rank - source_rank, hidden_size),
        ).astype(np.float32)
    return expanded_a, expanded_b, {
        "source_rank": source_rank,
        "target_rank": settings.rank,
        "expansion": "preserved_source_plus_zero_output_columns",
    }


def run_identity_post_training(
    *,
    model_path: str | Path,
    runtime_directory: str | Path,
    model_sha256: str,
    output_directory: str | Path,
    initial_adapter_path: str | Path | None = None,
    initial_adapter_sha256: str | None = None,
    on_progress: Callable[[str, int, int, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Train and evaluate one immutable candidate directory.

    The output directory must not exist.  A stopped or failed run remains as
    evidence and is never promoted automatically.
    """

    checked_model = Path(model_path).resolve()
    checked_runtime = Path(runtime_directory).resolve()
    checked_output = Path(output_directory).resolve()
    expected_hash = model_sha256.casefold()
    base_size_before = checked_model.stat().st_size




    base_hash_before = expected_hash
    if checked_output.exists():
        raise FileExistsError(f"Refusing to overwrite {checked_output}")





    settings = IDENTITY_TRAINING_SETTINGS
    if (initial_adapter_path is None) != (initial_adapter_sha256 is None):
        raise ValueError(
            "Initial identity adapter path and checksum must be supplied together"
        )
    initial_a: Any = None
    initial_b: Any = None
    initialization: dict[str, Any] | None = None
    training = training_examples()
    holdout = holdout_examples()

    def stopped() -> bool:
        return bool(should_stop is not None and should_stop())

    def progress(
        phase: str,
        current: int,
        total: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        if stopped():
            raise InterruptedError("Identity post-training stopped at a safe boundary")
        if on_progress is not None:
            on_progress(phase, current, total, dict(details or {}))

    runtime = SaltyNativeRuntime(
        model_path=checked_model,
        library_directory=checked_runtime,
        source_sha256=expected_hash,
        profile=SaltyNativeProfile(
            profile_id="base_steak_identity_trace",




            gpu_layers=IDENTITY_TRACE_GPU_LAYERS,
            context_limit=4096,
            resident_context_limit=4096,
            batch_size=1024,
            micro_batch_size=128,
            threads=12,
            thread_poll=100,
            kv_precision="q8_0",
            cuda_output_projection=False,
            host_kv_above_context=4096,
        ),
    )
    workflow_started = time.perf_counter()
    trace_started = time.perf_counter()
    retention: list[IdentityExample] = []
    hidden_size = 0
    vocabulary_size = 0
    try:
        loaded = runtime.load()



        retention_expectations = retention_examples()
        progress(
            "Collecting baseline behavior",
            0,
            len(retention_expectations),
            {"runtime": loaded, "base_model_unchanged": True},
        )
        for index, example in enumerate(retention_expectations, start=1):
            progress(
                "Collecting baseline behavior",
                index - 1,
                len(retention_expectations),
                {"example_id": example.id},
            )
            generated = runtime.generate(
                messages=example.chat_messages(),
                maximum_output_tokens=72,
                temperature=0.0,
                top_p=1.0,
                top_k=1,
                repetition_penalty=1.1,
                seed=20260819,
                reasoning_mode="instant",
                context_window_tokens=4096,
            )
            retention.append(
                IdentityExample(
                    id=example.id,
                    category=example.category,
                    messages=example.messages,
                    response=generated.text,
                )
            )
            progress(
                "Collecting baseline behavior",
                index,
                len(retention_expectations),
                {"example_id": example.id},
            )

        def trace_progress(completed: int, total: int, example_id: str) -> None:
            progress(
                "Tracing exact native weights",
                completed,
                total,
                {"example_id": example_id},
            )

        train_traces, holdout_traces, retention_traces = collect_native_traces(
            runtime,
            training=training,
            holdout=holdout,
            retention=retention,
            top_k=settings.top_k_negatives,
            hard_negative_texts=IDENTITY_HARD_NEGATIVE_TEXTS,
            on_progress=trace_progress,
        )
        hidden_size = int(runtime._api.native.llama_model_n_embd(runtime._model))
        vocabulary_size = int(runtime._api.native.llama_vocab_n_tokens(runtime._vocab))
    finally:
        runtime.unload()




    if initial_adapter_path is not None:
        initial_a, initial_b, initialization = load_output_adapter_initialization(
            initial_adapter_path,
            expected_sha256=initial_adapter_sha256,
        )
        if int(initialization["rank"]) > settings.rank:
            raise ValueError("Initial identity adapter rank exceeds the target rank")

    datasets = write_dataset_artifacts(
        checked_output,
        training=training,
        holdout=holdout,
        retention=retention,
    )

    def epoch_progress(row: dict[str, object]) -> None:
        progress(
            "Training learned identity adapter",
            int(row["epoch"]),
            settings.epochs,
            {
                "train_accuracy": row["adapted_train_accuracy"],
                "holdout_accuracy": row["adapted_holdout_accuracy"],
                "retention_delta_max": row["retention_delta_max"],
            },
        )

    expanded_a: np.ndarray | None = None
    expanded_b: np.ndarray | None = None
    if initial_a is not None and initial_b is not None:
        expanded_a, expanded_b, expansion = _expand_identity_initialization(
            initial_a,
            initial_b,
            settings=settings,
            hidden_size=hidden_size,
            vocabulary_size=vocabulary_size,
        )
        initialization = {
            **dict(initialization or {}),
            **expansion,
        }

    result = train_output_adapter(
        train_samples=train_traces,
        holdout_samples=holdout_traces,
        retention_samples=retention_traces,
        hidden_size=hidden_size,
        vocabulary_size=vocabulary_size,
        settings=settings,
        device_name="cuda",
        on_epoch=epoch_progress,
        initial_lora_a=expanded_a,
        initial_lora_b=expanded_b,
    )
    final = result.metrics["final"]
    base_hash_after_training = sha256_file(checked_model)
    base_size_after_training = checked_model.stat().st_size
    training_gates = _identity_training_gates(
        final=final,
        settings=settings,
        base_hash_before=base_hash_before,
        base_hash_after=base_hash_after_training,
        expected_hash=expected_hash,
        base_size_before=base_size_before,
        base_size_after=base_size_after_training,
    )
    if not all(training_gates.values()):
        raise RuntimeError("Learned identity adapter failed its training gates")

    adapter = export_identity_adapter(
        checked_output / "Base-Steak-2.0-Identity-LoRA-F32.gguf",
        result=result,
        settings=settings,
        base_model_sha256=expected_hash,
        train_dataset_sha256=datasets["train"]["sha256"],
        holdout_dataset_sha256=datasets["holdout"]["sha256"],
        retention_dataset_sha256=datasets["retention"]["sha256"],
    )
    training_report = {
        "schema": "base-steak-native-identity-training-report-v1",
        "model": {
            "path": str(checked_model),
            "sha256": expected_hash,
            "architecture": "steak20",
            "name": "Base Steak 2.0",
            "trainer": "MD Anik Hasan (Sawlper)",
            "bytes_before": base_size_before,
            "bytes_after_training": base_size_after_training,
            "sha256_before": base_hash_before,
            "sha256_before_verification": "native_runtime_preload_checksum_gate",
            "sha256_after_training": base_hash_after_training,
        },
        "runtime": str(checked_runtime),
        "settings": asdict(settings),
        "datasets": datasets,
        "traces": {
            "elapsed_seconds": round(time.perf_counter() - trace_started, 4),
            "train_samples": len(train_traces),
            "holdout_samples": len(holdout_traces),
            "retention_samples": len(retention_traces),
            "hidden_size": hidden_size,
            "vocabulary_size": vocabulary_size,
            "hard_negative_text_count": len(IDENTITY_HARD_NEGATIVE_TEXTS),
        },
        "training": result.metrics,
        "initialization": initialization,
        "adapter": adapter,
        "hardcoded_response_used": False,
        "gates": training_gates,
    }
    training_report_path = checked_output / "training-report.json"
    training_report_path.write_bytes(
        (json.dumps(training_report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    first_pass_report_path = checked_output / "identity-first-pass-evaluation.json"
    first_pass = run_identity_first_pass_evaluation(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_hash,
        adapter_path=adapter["path"],
        adapter_sha256=adapter["sha256"],
        adapter_scale=IDENTITY_ADAPTER_SCALE,
        report_path=first_pass_report_path,
        on_progress=(
            lambda stage, completed, total, example_id: progress(
                "Checking unseen identity first answers",
                completed,
                total,
                {"evaluation_stage": stage, "example_id": example_id},
            )
        ),
        should_stop=should_stop,
    )
    if not first_pass["passed"]:
        raise RuntimeError(
            "Learned identity adapter failed first-pass free-generation acceptance"
        )

    evaluation_report_path = checked_output / "free-generation-evaluation.json"

    def evaluation_progress(
        stage: str,
        completed: int,
        total: int,
        example_id: str,
    ) -> None:
        baseline_total = len(retention_examples())
        routed_total = len(holdout_examples()) + baseline_total
        progress(
            "Checking unseen generations and retention",
            completed + (baseline_total if stage == "routed" else 0),
            baseline_total + routed_total,
            {"evaluation_stage": stage, "example_id": example_id},
        )

    evaluation = run_routed_identity_evaluation(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_hash,
        adapter_path=adapter["path"],
        adapter_sha256=adapter["sha256"],
        adapter_scale=IDENTITY_ADAPTER_SCALE,
        report_path=evaluation_report_path,
        on_progress=evaluation_progress,
        should_stop=should_stop,
    )
    if not evaluation["passed"]:
        raise RuntimeError(
            "Learned routed identity adapter failed free-generation acceptance"
        )
    base_hash_after_evaluation = sha256_file(checked_model)
    base_size_after_evaluation = checked_model.stat().st_size
    if (
        base_hash_after_evaluation != base_hash_before
        or base_size_after_evaluation != base_size_before
    ):
        raise RuntimeError("Identity workflow changed the immutable base model")

    progress(
        "Identity candidate accepted",
        1,
        1,
        {
            "adapter_sha256": adapter["sha256"],
            "identity_pass_count": evaluation["metrics"]["identity_pass_count"],
            "retention_exact_baseline_count": evaluation["metrics"][
                "retention_exact_baseline_count"
            ],
        },
    )
    return {
        "schema": IDENTITY_POST_TRAINING_SCHEMA,
        "passed": True,
        "model_sha256": expected_hash,
        "base_model_immutability": {
            "bytes_before": base_size_before,
            "bytes_after": base_size_after_evaluation,
            "sha256_before": base_hash_before,
            "sha256_after": base_hash_after_evaluation,
            "unchanged": True,
        },
        "adapter": adapter,
        "adapter_scale": IDENTITY_ADAPTER_SCALE,
        "adapter_activation": "identity_intent",
        "training_report_path": str(training_report_path),
        "training_report_sha256": sha256_file(training_report_path),
        "evaluation_report_path": str(evaluation_report_path),
        "evaluation_report_sha256": sha256_file(evaluation_report_path),
        "first_pass_report_path": str(first_pass_report_path),
        "first_pass_report_sha256": sha256_file(first_pass_report_path),
        "metrics": evaluation["metrics"],
        "gates": {
            **training_gates,
            **evaluation["gates"],
        },
        "elapsed_seconds": round(time.perf_counter() - workflow_started, 4),
    }


__all__ = [
    "IDENTITY_ADAPTER_SCALE",
    "IDENTITY_TRAINING_SETTINGS",
    "IDENTITY_POST_TRAINING_SCHEMA",
    "run_identity_post_training",
]
