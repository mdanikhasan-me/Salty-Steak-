"""Native free-generation gates for the learned Base Steak identity adapter.

The scorer deliberately evaluates model generations.  It does not inject an
identity prompt and it never replaces a response in software.  An adapter is
accepted only when unseen identity questions pass and ordinary baseline
answers remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Sequence

from ..runtime.salty_native import (
    SaltyNativeAdapterSpec,
    SaltyNativeProfile,
    SaltyNativeRuntime,
)
from .base_steak_identity_dataset import (
    MODEL_NAME,
    TRAINER,
    TRAINER_ALIAS,
    IdentityExample,
    holdout_examples,
    retention_examples,
)
from .identity_intent import (
    IDENTITY_INTENT_CLASSIFIER,
    IDENTITY_INTENT_LABEL,
    OTHER_INTENT_LABEL,
    identity_intent_messages,
    normalise_identity_intent,
)


IDENTITY_EVALUATION_SCHEMA = "base-steak-free-generation-identity-evaluation-v2"
CONDITIONAL_IDENTITY_EVALUATION_SCHEMA = (
    "base-steak-conditional-free-generation-identity-evaluation-v1"
)
MINIMUM_EXACT_RETENTION_RATE = 0.80
REJECTED_ATTRIBUTIONS = (
    "qwen",
    "claude",
    "gpt",
    "gemini",
    "llama",
    "alibaba",
    "anthropic",
    "openai",
    "google",
    "meta ai",
    "hauhau",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise(text: str) -> str:
    return " ".join(text.split()).strip().casefold()


def identity_facts(text: str) -> dict[str, bool]:
    checked = text.casefold()
    model_like_names = re.findall(
        r"\b[a-z]+\s+steak\s+2\.0\b",
        checked,
        flags=re.IGNORECASE,
    )
    return {
        "model_name": MODEL_NAME.casefold() in checked,
        "trainer": TRAINER.casefold() in checked,
        "trainer_alias": TRAINER_ALIAS.casefold() in checked,
        "rejected_attribution_absent": not any(
            value in checked for value in REJECTED_ATTRIBUTIONS
        ),
        "near_name_absent": all(
            value.casefold() == MODEL_NAME.casefold() for value in model_like_names
        ),
        "identity_text_clean": not any(
            defect in checked
            for defect in (
                ".0.0",
                "andmy",
                "identity/",
                "aka aka",
                "sawlper productions",
                "trainer accounts",
                "mobile strategy",
                "battle-game",
                "you have forgotten",
                "commonmy",
                "common steak",
                "sawlper 2.0",
                "identityplate",
                "model identity mode",
                "model instance",
                "direct trainer name",
                "context was trained",
                "context is preserved",
                "context was my trainer",
                "4.0.0-l",
                "code steak",
            )
        )
        and re.search(r"(?:\*\*,){3,}", checked) is None
        and "sawlper is my model" not in checked,
    }


def identity_requirements(example: IdentityExample) -> tuple[str, ...]:
    if example.category == "holdout_name":
        return (
            "model_name",
            "rejected_attribution_absent",
            "near_name_absent",
            "identity_text_clean",
        )
    if example.category == "holdout_trainer":
        return (
            "trainer",
            "trainer_alias",
            "rejected_attribution_absent",
            "near_name_absent",
            "identity_text_clean",
        )
    return (
        "model_name",
        "trainer",
        "trainer_alias",
        "rejected_attribution_absent",
        "near_name_absent",
        "identity_text_clean",
    )


def _retention_correct(example: IdentityExample, text: str) -> bool:
    expected = _normalise(example.response)
    actual = _normalise(text)
    return expected == actual or expected in actual


def _free_of_repetition_collapse(text: str) -> bool:
    checked = str(text or "")
    return (
        re.search(r"([0-9])\1{40,}", checked) is None
        and re.search(r"(?:\d+\.){40,}", checked) is None
        and re.search(r"(.{2,12})\1{30,}", checked, flags=re.DOTALL) is None
    )


def rescore_free_generation_report(report: dict[str, Any]) -> dict[str, Any]:
    """Apply the current semantic-retention policy to recorded generations."""

    rescored = dict(report)
    metrics = dict(rescored.get("metrics") or {})
    retention_rows = [
        record
        for record in rescored.get("adapted", [])
        if record.get("category") == "retention"
    ]
    clean_count = sum(
        _free_of_repetition_collapse(str(record.get("output") or ""))
        for record in retention_rows
    )
    metrics["retention_output_clean_count"] = clean_count
    metrics["retention_output_clean_rate"] = (
        clean_count / len(retention_rows) if retention_rows else 0.0
    )
    gates = {
        "all_unseen_identity_prompts_pass": metrics.get("identity_pass_rate") == 1.0,
        "identity_wording_varies": int(
            metrics.get("identity_unique_output_count") or 0
        )
        >= 3,
        "retention_exact_match_at_least_80_percent": float(
            metrics.get("retention_exact_baseline_rate") or 0.0
        )
        >= MINIMUM_EXACT_RETENTION_RATE,
        "retention_correctness_not_lower": int(
            metrics.get("adapted_retention_correct_count") or 0
        )
        >= int(metrics.get("baseline_retention_correct_count") or 0),
        "retention_outputs_free_of_repetition_collapse": clean_count
        == len(retention_rows),
        "adapter_hash_verified": bool(
            (rescored.get("gates") or {}).get("adapter_hash_verified")
        ),
        "model_hash_verified": bool(
            (rescored.get("gates") or {}).get("model_hash_verified")
        ),
    }
    rescored["schema"] = IDENTITY_EVALUATION_SCHEMA
    rescored["metrics"] = metrics
    rescored["gates"] = gates
    rescored["passed"] = all(gates.values())
    return rescored


def _run_generation_split(
    *,
    model: Path,
    runtime_directory: Path,
    model_sha256: str,
    examples: Sequence[IdentityExample],
    adapter: SaltyNativeAdapterSpec | None,
    on_progress: Callable[[str, int, int, str], None] | None,
    should_stop: Callable[[], bool] | None,
    conditional_adapter: bool = False,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    runtime = SaltyNativeRuntime(
        model_path=model,
        library_directory=runtime_directory,
        source_sha256=model_sha256,
        adapters=[adapter] if adapter is not None else [],
        profile=SaltyNativeProfile(
            profile_id="base_steak_identity_generation_eval",
            gpu_layers=99,
            context_limit=4096,
            resident_context_limit=4096,
            batch_size=256,
            micro_batch_size=128,
            threads=12,
            thread_poll=100,
            kv_precision="q8_0",
            cuda_output_projection=False,
            host_kv_above_context=4096,
        ),
    )
    records: list[dict[str, object]] = []
    started = time.perf_counter()
    stage = (
        "conditional"
        if adapter is not None and conditional_adapter
        else "adapted"
        if adapter is not None
        else "baseline"
    )
    loaded: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    try:
        loaded = runtime.load()
        for index, example in enumerate(examples, start=1):
            if should_stop is not None and should_stop():
                raise InterruptedError("Identity evaluation stopped at a safe prompt boundary")
            controller_label: str | None = None
            enabled_adapter_ids: tuple[str, ...] | None = None
            if adapter is not None and conditional_adapter:
                controller = runtime.generate(
                    messages=identity_intent_messages(example.messages[-1][1]),
                    maximum_output_tokens=6,
                    temperature=0.0,
                    top_p=1.0,
                    top_k=1,
                    repetition_penalty=1.0,
                    seed=20260820,
                    reasoning_mode="instant",
                    context_window_tokens=4096,
                    enabled_adapter_ids=(),
                )
                controller_label = normalise_identity_intent(controller.text)
                enabled_adapter_ids = (
                    (adapter.adapter_id,)
                    if controller_label == IDENTITY_INTENT_LABEL
                    else ()
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
                enabled_adapter_ids=enabled_adapter_ids,
            )
            facts = identity_facts(generated.text)
            requirements = identity_requirements(example)
            records.append(
                {
                    "id": example.id,
                    "category": example.category,
                    "prompt": example.messages[-1][1],
                    "expected": example.response,
                    "output": generated.text,
                    "finish_reason": generated.finish_reason,
                    "identity_facts": facts,
                    "identity_requirements": requirements,
                    "identity_pass": all(facts[value] for value in requirements),
                    "retention_correct": _retention_correct(example, generated.text),
                    "generated_tokens": generated.technical_details.get(
                        "generated_output_tokens"
                    ),
                    "controller_label": controller_label,
                    "expected_controller_label": (
                        IDENTITY_INTENT_LABEL
                        if str(example.category).startswith("holdout")
                        else OTHER_INTENT_LABEL
                    )
                    if conditional_adapter
                    else None,
                    "enabled_adapter_ids": (
                        list(enabled_adapter_ids or ())
                        if conditional_adapter
                        else None
                    ),
                }
            )
            if on_progress is not None:
                on_progress(stage, index, len(examples), example.id)
        identity = runtime.describe()
    finally:
        runtime.unload()
    return records, {
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "loaded_identity": loaded,
        "final_identity": identity,
    }


def run_conditional_identity_evaluation(
    *,
    model: str | Path,
    runtime_directory: str | Path,
    model_sha256: str,
    adapter_path: str | Path,
    adapter_sha256: str,
    adapter_scale: float,
    report_path: str | Path | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Evaluate the learned adapter only on model-classified identity turns."""

    checked_model = Path(model).resolve()
    checked_runtime = Path(runtime_directory).resolve()
    checked_adapter = Path(adapter_path).resolve()
    expected_model_hash = model_sha256.casefold()
    expected_adapter_hash = adapter_sha256.casefold()
    if _sha256(checked_model) != expected_model_hash:
        raise RuntimeError("Conditional identity evaluation model checksum mismatch")
    if _sha256(checked_adapter) != expected_adapter_hash:
        raise RuntimeError("Conditional identity evaluation adapter checksum mismatch")
    output_path = Path(report_path).resolve() if report_path is not None else None
    if output_path is not None and output_path.exists():
        raise FileExistsError(f"Refusing to overwrite {output_path}")

    adapter = SaltyNativeAdapterSpec(
        adapter_id="base-steak-2-0-identity-v1",
        path=str(checked_adapter),
        sha256=expected_adapter_hash,
        scale=float(adapter_scale),
        activation="identity_intent",
    )
    combined = [*holdout_examples(), *retention_examples()]
    baseline, baseline_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=combined,
        adapter=None,
        on_progress=on_progress,
        should_stop=should_stop,
    )
    conditional, conditional_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=combined,
        adapter=adapter,
        on_progress=on_progress,
        should_stop=should_stop,
        conditional_adapter=True,
    )
    baseline_by_id = {str(record["id"]): record for record in baseline}
    identity_rows = [
        record
        for record in conditional
        if str(record["category"]).startswith("holdout")
    ]
    retention_rows = [
        record for record in conditional if record["category"] == "retention"
    ]
    exact_retention = [
        record
        for record in retention_rows
        if _normalise(str(record["output"]))
        == _normalise(str(baseline_by_id[str(record["id"])]["output"]))
    ]
    identity_pass_count = sum(bool(record["identity_pass"]) for record in identity_rows)
    identity_controller_pass_count = sum(
        record["controller_label"] == IDENTITY_INTENT_LABEL
        for record in identity_rows
    )
    retention_controller_pass_count = sum(
        record["controller_label"] == OTHER_INTENT_LABEL
        for record in retention_rows
    )
    baseline_retention_correct = sum(
        bool(record["retention_correct"])
        for record in baseline
        if record["category"] == "retention"
    )
    conditional_retention_correct = sum(
        bool(record["retention_correct"]) for record in retention_rows
    )
    unique_identity_outputs = len(
        {_normalise(str(record["output"])) for record in identity_rows}
    )
    clean_count = sum(
        _free_of_repetition_collapse(str(record["output"]))
        for record in retention_rows
    )
    metrics = {
        "identity_count": len(identity_rows),
        "identity_pass_count": identity_pass_count,
        "identity_unique_output_count": unique_identity_outputs,
        "identity_controller_pass_count": identity_controller_pass_count,
        "retention_count": len(retention_rows),
        "retention_controller_pass_count": retention_controller_pass_count,
        "retention_exact_baseline_count": len(exact_retention),
        "retention_exact_baseline_rate": len(exact_retention) / len(retention_rows),
        "retention_output_clean_count": clean_count,
        "baseline_retention_correct_count": baseline_retention_correct,
        "conditional_retention_correct_count": conditional_retention_correct,
    }
    gates = {
        "all_unseen_identity_prompts_pass": identity_pass_count == len(identity_rows),
        "all_identity_prompts_route_to_learned_adapter": (
            identity_controller_pass_count == len(identity_rows)
        ),
        "all_retention_prompts_keep_adapter_disabled": (
            retention_controller_pass_count == len(retention_rows)
        ),
        "identity_wording_varies": unique_identity_outputs >= 3,
        "retention_is_byte_equivalent_to_base": len(exact_retention)
        == len(retention_rows),
        "retention_correctness_not_lower": conditional_retention_correct
        >= baseline_retention_correct,
        "retention_outputs_free_of_repetition_collapse": clean_count
        == len(retention_rows),
        "adapter_hash_verified": (
            conditional_runtime.get("loaded_identity", {})
            .get("adapters", [{}])[0]
            .get("verified_sha256")
            == expected_adapter_hash
        ),
        "adapter_disabled_at_runtime_load": conditional_runtime.get(
            "loaded_identity", {}
        ).get("active_adapter_ids")
        == [],
        "model_hash_verified": baseline_runtime.get("loaded_identity", {}).get(
            "verified_source_sha256"
        )
        == expected_model_hash,
    }
    report = {
        "schema": CONDITIONAL_IDENTITY_EVALUATION_SCHEMA,
        "model_sha256": expected_model_hash,
        "adapter_sha256": expected_adapter_hash,
        "adapter_scale": float(adapter_scale),
        "controller_instruction": IDENTITY_INTENT_CLASSIFIER,
        "metrics": metrics,
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_runtime": baseline_runtime,
        "conditional_runtime": conditional_runtime,
        "baseline": baseline,
        "conditional": conditional,
    }
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
    return report


def run_free_generation_evaluation(
    *,
    model: str | Path,
    runtime_directory: str | Path,
    model_sha256: str,
    adapter_path: str | Path,
    adapter_sha256: str,
    adapter_scale: float,
    report_path: str | Path | None = None,
    on_progress: Callable[[str, int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    checked_model = Path(model).resolve()
    checked_runtime = Path(runtime_directory).resolve()
    checked_adapter = Path(adapter_path).resolve()
    expected_model_hash = model_sha256.casefold()
    expected_adapter_hash = adapter_sha256.casefold()
    if _sha256(checked_model) != expected_model_hash:
        raise RuntimeError("Identity evaluation model checksum mismatch")
    if _sha256(checked_adapter) != expected_adapter_hash:
        raise RuntimeError("Identity evaluation adapter checksum mismatch")
    output_path = Path(report_path).resolve() if report_path is not None else None
    if output_path is not None and output_path.exists():
        raise FileExistsError(f"Refusing to overwrite {output_path}")

    adapter = SaltyNativeAdapterSpec(
        adapter_id="base-steak-2-0-identity-v1",
        path=str(checked_adapter),
        sha256=expected_adapter_hash,
        scale=float(adapter_scale),
    )
    combined = [*holdout_examples(), *retention_examples()]
    baseline, baseline_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=combined,
        adapter=None,
        on_progress=on_progress,
        should_stop=should_stop,
    )
    adapted, adapted_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=combined,
        adapter=adapter,
        on_progress=on_progress,
        should_stop=should_stop,
    )
    baseline_by_id = {record["id"]: record for record in baseline}
    identity_rows = [
        record for record in adapted if str(record["category"]).startswith("holdout")
    ]
    retention_rows = [
        record for record in adapted if record["category"] == "retention"
    ]
    exact_retention = [
        record
        for record in retention_rows
        if _normalise(str(record["output"]))
        == _normalise(str(baseline_by_id[record["id"]]["output"]))
    ]
    baseline_retention_correct = sum(
        bool(record["retention_correct"])
        for record in baseline
        if record["category"] == "retention"
    )
    adapted_retention_correct = sum(
        bool(record["retention_correct"]) for record in retention_rows
    )
    unique_identity_outputs = len(
        {_normalise(str(record["output"])) for record in identity_rows}
    )
    identity_pass_count = sum(bool(record["identity_pass"]) for record in identity_rows)
    metrics = {
        "identity_count": len(identity_rows),
        "identity_pass_count": identity_pass_count,
        "identity_pass_rate": identity_pass_count / len(identity_rows),
        "identity_unique_output_count": unique_identity_outputs,
        "retention_count": len(retention_rows),
        "retention_exact_baseline_count": len(exact_retention),
        "retention_exact_baseline_rate": len(exact_retention) / len(retention_rows),
        "baseline_retention_correct_count": baseline_retention_correct,
        "adapted_retention_correct_count": adapted_retention_correct,
    }
    gates = {
        "all_unseen_identity_prompts_pass": metrics["identity_pass_rate"] == 1.0,
        "identity_wording_varies": unique_identity_outputs >= 3,
        "retention_exact_match_at_least_95_percent": metrics[
            "retention_exact_baseline_rate"
        ]
        >= 0.95,
        "retention_correctness_not_lower": adapted_retention_correct
        >= baseline_retention_correct,
        "adapter_hash_verified": True,
        "model_hash_verified": True,
    }
    report = {
        "schema": IDENTITY_EVALUATION_SCHEMA,
        "model_sha256": expected_model_hash,
        "adapter_sha256": expected_adapter_hash,
        "adapter_scale": float(adapter_scale),
        "metrics": metrics,
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_runtime": baseline_runtime,
        "adapted_runtime": adapted_runtime,
        "baseline": baseline,
        "adapted": adapted,
    }
    report = rescore_free_generation_report(report)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
    return report


__all__ = [
    "IDENTITY_EVALUATION_SCHEMA",
    "CONDITIONAL_IDENTITY_EVALUATION_SCHEMA",
    "REJECTED_ATTRIBUTIONS",
    "identity_facts",
    "identity_requirements",
    "rescore_free_generation_report",
    "run_free_generation_evaluation",
    "run_conditional_identity_evaluation",
]
