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
    holdout_examples,
    retention_examples,
    training_examples,
)
from .identity_evaluation import run_conditional_identity_evaluation
from .native_identity import (
    NativeIdentitySettings,
    collect_native_traces,
    export_identity_adapter,
    train_output_adapter,
    write_dataset_artifacts,
)


IDENTITY_POST_TRAINING_SCHEMA = "base-steak-identity-post-training-workflow-v1"
IDENTITY_ADAPTER_SCALE = 0.5


def run_identity_post_training(
    *,
    model_path: str | Path,
    runtime_directory: str | Path,
    model_sha256: str,
    output_directory: str | Path,
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
    if sha256_file(checked_model) != expected_hash:
        raise RuntimeError("Identity post-training model checksum mismatch")
    if checked_output.exists():
        raise FileExistsError(f"Refusing to overwrite {checked_output}")

    settings = NativeIdentitySettings(rank=64, epochs=800)
    training = training_examples()
    holdout = holdout_examples()
    retention_expectations = retention_examples()

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
            gpu_layers=99,
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
            on_progress=trace_progress,
        )
        hidden_size = int(runtime._api.native.llama_model_n_embd(runtime._model))
        vocabulary_size = int(runtime._api.native.llama_vocab_n_tokens(runtime._vocab))
    finally:
        runtime.unload()

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

    result = train_output_adapter(
        train_samples=train_traces,
        holdout_samples=holdout_traces,
        retention_samples=retention_traces,
        hidden_size=hidden_size,
        vocabulary_size=vocabulary_size,
        settings=settings,
        device_name="cuda",
        on_epoch=epoch_progress,
    )
    final = result.metrics["final"]
    training_gates = {
        "train_accuracy": final["adapted_train_accuracy"]
        >= settings.minimum_train_accuracy,
        "unseen_holdout_accuracy": final["adapted_holdout_accuracy"]
        >= settings.minimum_holdout_accuracy,
        "retention_delta_bounded": final["retention_delta_max"]
        <= settings.maximum_retention_delta,
        "base_model_unchanged": True,
        "hardcoded_response_used": False,
    }
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
        },
        "training": result.metrics,
        "adapter": adapter,
        "gates": training_gates,
    }
    training_report_path = checked_output / "training-report.json"
    training_report_path.write_bytes(
        (json.dumps(training_report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    evaluation_report_path = checked_output / "free-generation-evaluation.json"

    def evaluation_progress(
        stage: str,
        completed: int,
        total: int,
        example_id: str,
    ) -> None:
        progress(
            "Checking unseen generations and retention",
            completed + (total if stage == "conditional" else 0),
            total * 2,
            {"evaluation_stage": stage, "example_id": example_id},
        )

    evaluation = run_conditional_identity_evaluation(
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
            "Learned conditional identity adapter failed free-generation acceptance"
        )

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
        "adapter": adapter,
        "adapter_scale": IDENTITY_ADAPTER_SCALE,
        "adapter_activation": "identity_intent",
        "training_report_path": str(training_report_path),
        "training_report_sha256": sha256_file(training_report_path),
        "evaluation_report_path": str(evaluation_report_path),
        "evaluation_report_sha256": sha256_file(evaluation_report_path),
        "metrics": evaluation["metrics"],
        "gates": {
            **training_gates,
            **evaluation["gates"],
        },
        "elapsed_seconds": round(time.perf_counter() - workflow_started, 4),
    }


__all__ = [
    "IDENTITY_ADAPTER_SCALE",
    "IDENTITY_POST_TRAINING_SCHEMA",
    "run_identity_post_training",
]
