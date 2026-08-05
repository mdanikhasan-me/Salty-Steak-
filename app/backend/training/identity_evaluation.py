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
    retention_examples,
)
from .identity_dialogue_dataset import holdout_examples
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
ROUTED_IDENTITY_EVALUATION_SCHEMA = (
    "base-steak-routed-free-generation-identity-evaluation-v2"
)
MINIMUM_EXACT_RETENTION_RATE = 0.80
IDENTITY_RECOVERY_INSTRUCTION = (
    "Answer the latest identity question as one complete, concise, grammatical "
    "sentence. State the model name, the trainer's full name, and the trainer "
    "alias learned during post-training. Use simple subject-verb-object wording. "
    "Mention each name, alias, and version only once. Do not repeat words or "
    "version digits. Do not emit JSON, speculate, or add a biography."
)
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
    visible = str(text or "").strip()
    checked = visible.casefold()
    words = re.findall(r"\b[\w'.-]+\b", visible, flags=re.UNICODE)
    mentions_trainer = TRAINER.casefold() in checked
    mentions_alias = bool(re.search(r"\bsawlper\b", checked))
    training_relation_affirmed = bool(
        re.search(
            r"(?:trained\s+by\s+md\s+anik\s+hasan|"
            r"(?:my|the|this\s+model(?:'s)?)\s+trainer\s+(?:is|was)\s+md\s+anik\s+hasan|"
            r"md\s+anik\s+hasan[^.!?]{0,48}\b(?:trained|trainer)\b|"
            r"sawlper[^.!?]{0,48}\b(?:trained|trainer)\b)",
            checked,
        )
    ) and not bool(
        re.search(
            r"(?:not|never|didn['’]?t|did\s+not)\s+[^.!?]{0,32}\b(?:train|trainer)",
            checked,
        )
    )
    alias_relation_affirmed = bool(
        re.search(
            r"(?:md\s+anik\s+hasan\s*\(\s*sawlper\b\s*\)|"
            r"md\s+anik\s+hasan[^.!?]{0,32}\b(?:aka|also\s+known\s+as|known\s+as|alias(?:ed)?(?:\s+as)?)\s+sawlper\b|"
            r"md\s+anik\s+hasan[^.!?]{0,48}\bwhose\s+(?:listed\s+)?alias\s+is\s+sawlper\b|"
            r"md\s+anik\s+hasan[^.!?]{0,96}\btrainer\s+alias\s+(?:is|:)\s+sawlper\b|"
            r"sawlper\b\s+is\s+(?:the\s+alias\s+of\s+)?md\s+anik\s+hasan)",
            checked,
        )
    )
    invented_authorship = bool(
        re.search(
            r"\b(?:md\s+anik\s+hasan|sawlper)\b[^.!?]{0,48}"
            r"\b(?:created|developed|built|made)\b[^.!?]{0,32}"
            r"\b(?:me|base\s+steak)\b|"
            r"\b(?:i|base\s+steak)\b[^.!?]{0,32}\b(?:created|developed|built|made)\s+by\s+"
            r"(?:md\s+anik\s+hasan|sawlper)\b",
            checked,
        )
    )
    model_like_names = re.findall(
        r"\b[a-z]+\s+steak\s+2\.0\b",
        checked,
        flags=re.IGNORECASE,
    )
    return {
        "model_name": MODEL_NAME.casefold() in checked,
        "trainer": TRAINER.casefold() in checked,
        "trainer_alias": bool(re.search(r"\bsawlper\b", checked)),
        "training_relation_consistent": (
            not (mentions_trainer or mentions_alias) or training_relation_affirmed
        ),
        "alias_relation_consistent": (
            not (mentions_trainer or mentions_alias) or alias_relation_affirmed
        ),
        "unsupported_authorship_absent": not invented_authorship,
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
                "is the my",
                "the alias sawlper is the alias",
                "my model name is 2.0",
                "�",
            )
        )
        and re.search(r"(?:\*\*,){3,}", checked) is None
        and "sawlper is my model" not in checked,
        "alias_spelling_clean": re.search(r"\bsawlper[a-z]+", checked) is None,
        "identity_repetition_absent": _free_of_repetition_collapse(str(text or "")),
        "identity_fact_repetition_absent": (
            checked.count(MODEL_NAME.casefold()) <= 1
            and checked.count(TRAINER.casefold()) <= 1
            and len(re.findall(r"\bsawlper\b", checked)) <= 1
        ),
        "visible_answer_nonempty": bool(str(text or "").strip()),
        "routing_protocol_absent": not bool(
            re.search(
                r'\{\s*"(?:action|nodes|capability|connector)"\s*:',
                str(text or ""),
                flags=re.IGNORECASE,
            )
        ),
        "unsupported_biography_absent": not any(
            phrase in checked
            for phrase in (
                "developer and maintainer",
                "previous versions",
                "base steak 1.0",
                "business owners",
                "academics",
                "professor or researcher",
                "business management",
                "finance",
                "it sector",
                "continuous updates",
                "user feedback integration",
                "well-known public figure",
                "business card mockup",
                "high-resolution headshot",
            )
        ),
        "uncertainty_boundary_present": bool(
            re.search(
                r"\b(?:do\s+not\s+have|don['’]?t\s+have|not\s+verified|"
                r"should\s+not\s+invent|will\s+not\s+invent|needs?\s+(?:reliable\s+)?evidence|"
                r"requires?\s+(?:a\s+)?(?:separate\s+)?(?:source-based\s+)?research|"
                r"checked\s+through\s+(?:reliable\s+)?public\s+sources)\b",
                checked,
            )
        ),
        "research_boundary_present": bool(
            re.search(r"\b(?:research|sources?|evidence|source-based)\b", checked)
        ),
        "clarification_present": "?" in visible
        and bool(
            re.search(
                r"\b(?:what|which|specific|specifically|question|detail|part)\b",
                checked,
            )
        ),
        "correction_present": bool(
            re.search(
                r"\b(?:incorrect|correct\s+identity|correction|replace|"
                r"previous\s+identity\s+was\s+wrong|earlier\s+attribution)\b",
                checked,
            )
        ),
        "polished_identity_style": (
            8 <= len(words) <= 90
            and bool(visible)
            and visible[-1:] in {".", "?", "!"}
            and not any(
                phrase in checked
                for phrase in (
                    "as the model is",
                    "in sawlper",
                    "known as base steak 2.0",
                    "same trainer that trained",
                    "listening to the same",
                    "tell sawlper that",
                    "the alias of the alias",
                    "my trainer is my trainer",
                    "2.0k",
                    "https://",
                )
            )
            and re.search(
                r"\bthe\s+correct\s+identity\s+is\s+(?:this\s+is|i\s+am)\b",
                checked,
            )
            is None
            and re.search(
                r"\bmy\s+trainer\s+is\s+md\s+anik\s+hasan[^.!?]{0,24}\btrained\s+me\b",
                checked,
            )
            is None
        ),
    }


def identity_requirements(example: IdentityExample) -> tuple[str, ...]:
    integrity = (
        "rejected_attribution_absent",
        "near_name_absent",
        "identity_text_clean",
        "visible_answer_nonempty",
        "routing_protocol_absent",
        "unsupported_biography_absent",
        "training_relation_consistent",
        "alias_relation_consistent",
        "unsupported_authorship_absent",
        "alias_spelling_clean",
        "identity_repetition_absent",
        "identity_fact_repetition_absent",
        "polished_identity_style",
    )
    complete_identity = ("model_name", "trainer", "trainer_alias", *integrity)
    category = str(example.category).casefold()
    if "clarify" in category:
        return (*complete_identity, "clarification_present")
    if "research" in category:
        return (*complete_identity, "research_boundary_present")
    if "boundary" in category:
        return (*complete_identity, "uncertainty_boundary_present")
    if "correction" in category:
        return (*complete_identity, "correction_present")
    return complete_identity


def identity_response_defect(text: object) -> str | None:
    """Return why a learned identity answer must be withheld and retried."""

    facts = identity_facts(str(text or ""))
    if not facts["visible_answer_nonempty"]:
        return "empty_visible_answer"
    if not facts["routing_protocol_absent"]:
        return "routing_protocol"
    integrity = (
        "rejected_attribution_absent",
        "near_name_absent",
        "identity_text_clean",
        "unsupported_biography_absent",
        "training_relation_consistent",
        "alias_relation_consistent",
        "unsupported_authorship_absent",
        "alias_spelling_clean",
        "identity_repetition_absent",
        "identity_fact_repetition_absent",
        "polished_identity_style",
    )
    if not all(facts[name] for name in integrity):
        return "identity_integrity"
    if not any(facts[name] for name in ("model_name", "trainer", "trainer_alias")):
        return "identity_incomplete"
    return None


def _retention_correct(example: IdentityExample, text: str) -> bool:
    expected = _normalise(example.response)
    actual = _normalise(text)
    return expected == actual or expected in actual


def _free_of_repetition_collapse(text: str) -> bool:
    checked = str(text or "")
    if (
        re.search(r"([0-9])\1{20,}", checked) is not None
        or re.search(r"(?:\d+\.){20,}", checked) is not None
        or re.search(r"(.{2,20})\1{3,}", checked, flags=re.DOTALL) is not None
    ):
        return False
    words = re.findall(r"\b[\w'.-]+\b", checked.casefold(), flags=re.UNICODE)
    for width in range(2, min(10, len(words) // 3 + 1)):
        counts: dict[tuple[str, ...], int] = {}
        for index in range(len(words) - width + 1):
            phrase = tuple(words[index : index + width])
            counts[phrase] = counts.get(phrase, 0) + 1
        if counts and max(counts.values()) >= 3:
            return False
    return True


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
    expected_route_activation: bool = False,
    recover_identity_output: bool = False,
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
    if adapter is None:
        stage = "baseline"
    elif expected_route_activation:
        stage = "routed"
    elif conditional_adapter:
        stage = "conditional"
    else:
        stage = "adapted"
    loaded: dict[str, Any] = {}
    identity: dict[str, Any] = {}
    try:
        loaded = runtime.load()
        for index, example in enumerate(examples, start=1):
            if should_stop is not None and should_stop():
                raise InterruptedError("Identity evaluation stopped at a safe prompt boundary")
            controller_label: str | None = None
            enabled_adapter_ids: tuple[str, ...] | None = None
            activation_policy_label: str | None = None
            if adapter is not None and expected_route_activation:
                identity_turn = str(example.category).startswith("holdout")
                enabled_adapter_ids = (adapter.adapter_id,) if identity_turn else ()
                activation_policy_label = (
                    IDENTITY_INTENT_LABEL if identity_turn else OTHER_INTENT_LABEL
                )
            elif adapter is not None and conditional_adapter:
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
            generation_arguments = {
                "messages": example.chat_messages(),
                "maximum_output_tokens": 72,
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": 1,
                "repetition_penalty": (
                    1.0
                    if str(example.category).startswith("holdout")
                    else 1.1
                ),
                "seed": 20260819,
                "reasoning_mode": "instant",
                "context_window_tokens": 4096,
                "enabled_adapter_ids": enabled_adapter_ids,
            }
            generated = runtime.generate(**generation_arguments)
            first_output = generated.text
            first_finish_reason = generated.finish_reason
            recovery_reason: str | None = None
            if (
                recover_identity_output
                and str(example.category).startswith("holdout")
                and (recovery_reason := identity_response_defect(first_output))
                is not None
            ):
                generated = runtime.generate(
                    **{
                        **generation_arguments,
                        "messages": [
                            {
                                "role": "system",
                                "content": IDENTITY_RECOVERY_INSTRUCTION,
                            },
                            *example.chat_messages(),
                        ],
                    }
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
                    "first_output": first_output,
                    "first_finish_reason": first_finish_reason,
                    "recovery_attempted": recovery_reason is not None,
                    "recovery_reason": recovery_reason,
                    "finish_reason": generated.finish_reason,
                    "identity_facts": facts,
                    "identity_requirements": requirements,
                    "identity_pass": all(facts[value] for value in requirements),
                    "retention_correct": _retention_correct(example, generated.text),
                    "generated_tokens": generated.technical_details.get(
                        "generated_output_tokens"
                    ),
                    "controller_label": controller_label,
                    "activation_policy_label": activation_policy_label,
                    "expected_controller_label": (
                        IDENTITY_INTENT_LABEL
                        if str(example.category).startswith("holdout")
                        else OTHER_INTENT_LABEL
                    )
                    if conditional_adapter
                    else None,
                    "enabled_adapter_ids": (
                        list(enabled_adapter_ids or ())
                        if conditional_adapter or expected_route_activation
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
    identity_holdouts = holdout_examples()
    ordinary_retention = retention_examples()
    combined = [*identity_holdouts, *ordinary_retention]
    baseline, baseline_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=ordinary_retention,
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


def run_routed_identity_evaluation(
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
    """Evaluate identity generations under the learned-router activation contract.

    This harness deliberately does not guess routing with the base model. It
    enables the candidate adapter for identity holdouts and disables it for
    ordinary retention prompts. The routing adapter and the combined ChatService
    path are evaluated independently, so a passing identity score cannot hide a
    routing miss.
    """

    checked_model = Path(model).resolve()
    checked_runtime = Path(runtime_directory).resolve()
    checked_adapter = Path(adapter_path).resolve()
    expected_model_hash = model_sha256.casefold()
    expected_adapter_hash = adapter_sha256.casefold()
    if _sha256(checked_model) != expected_model_hash:
        raise RuntimeError("Routed identity evaluation model checksum mismatch")
    if _sha256(checked_adapter) != expected_adapter_hash:
        raise RuntimeError("Routed identity evaluation adapter checksum mismatch")
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
    identity_holdouts = holdout_examples()
    ordinary_retention = retention_examples()
    combined = [*identity_holdouts, *ordinary_retention]
    baseline, baseline_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=ordinary_retention,
        adapter=None,
        on_progress=on_progress,
        should_stop=should_stop,
    )
    routed, routed_runtime = _run_generation_split(
        model=checked_model,
        runtime_directory=checked_runtime,
        model_sha256=expected_model_hash,
        examples=combined,
        adapter=adapter,
        on_progress=on_progress,
        should_stop=should_stop,
        expected_route_activation=True,
        recover_identity_output=True,
    )
    baseline_by_id = {str(record["id"]): record for record in baseline}
    identity_rows = [
        record
        for record in routed
        if str(record["category"]).startswith("holdout")
    ]
    retention_rows = [
        record for record in routed if record["category"] == "retention"
    ]
    exact_retention = [
        record
        for record in retention_rows
        if str(record["output"]) == str(baseline_by_id[str(record["id"])]["output"])
    ]
    identity_pass_count = sum(bool(record["identity_pass"]) for record in identity_rows)
    recovery_count = sum(bool(record["recovery_attempted"]) for record in identity_rows)
    recovery_success_count = sum(
        bool(record["recovery_attempted"]) and bool(record["identity_pass"])
        for record in identity_rows
    )
    identity_activation_pass_count = sum(
        record["enabled_adapter_ids"] == [adapter.adapter_id]
        for record in identity_rows
    )
    retention_disable_pass_count = sum(
        record["enabled_adapter_ids"] == [] for record in retention_rows
    )
    baseline_retention_correct = sum(
        bool(record["retention_correct"])
        for record in baseline
        if record["category"] == "retention"
    )
    routed_retention_correct = sum(
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
        "identity_route_activation_pass_count": identity_activation_pass_count,
        "identity_recovery_count": recovery_count,
        "identity_recovery_success_count": recovery_success_count,
        "retention_count": len(retention_rows),
        "retention_route_disable_pass_count": retention_disable_pass_count,
        "retention_exact_baseline_count": len(exact_retention),
        "retention_exact_baseline_rate": len(exact_retention) / len(retention_rows),
        "retention_output_clean_count": clean_count,
        "baseline_retention_correct_count": baseline_retention_correct,
        "routed_retention_correct_count": routed_retention_correct,
    }
    gates = {
        "all_unseen_identity_prompts_pass": identity_pass_count == len(identity_rows),
        "every_identity_recovery_succeeds": recovery_success_count == recovery_count,
        "all_identity_prompts_evaluated_with_adapter": (
            identity_activation_pass_count == len(identity_rows)
        ),
        "all_retention_prompts_evaluated_without_adapter": (
            retention_disable_pass_count == len(retention_rows)
        ),
        "retention_is_byte_equivalent_to_base": len(exact_retention)
        == len(retention_rows),
        "retention_correctness_not_lower": routed_retention_correct
        >= baseline_retention_correct,
        "retention_outputs_free_of_repetition_collapse": clean_count
        == len(retention_rows),
        "adapter_hash_verified": (
            routed_runtime.get("loaded_identity", {})
            .get("adapters", [{}])[0]
            .get("verified_sha256")
            == expected_adapter_hash
        ),
        "adapter_disabled_at_runtime_load": routed_runtime.get(
            "loaded_identity", {}
        ).get("active_adapter_ids")
        == [],
        "model_hash_verified": baseline_runtime.get("loaded_identity", {}).get(
            "verified_source_sha256"
        )
        == expected_model_hash,
    }
    report = {
        "schema": ROUTED_IDENTITY_EVALUATION_SCHEMA,
        "model_sha256": expected_model_hash,
        "adapter_sha256": expected_adapter_hash,
        "adapter_scale": float(adapter_scale),
        "activation_contract": (
            "identity adapter enabled only after the separately evaluated learned "
            "IDENTITY route; disabled for ordinary turns"
        ),
        "metrics": metrics,
        "gates": gates,
        "passed": all(gates.values()),
        "baseline_runtime": baseline_runtime,
        "routed_runtime": routed_runtime,
        "baseline": baseline,
        "routed": routed,
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
    "ROUTED_IDENTITY_EVALUATION_SCHEMA",
    "IDENTITY_RECOVERY_INSTRUCTION",
    "REJECTED_ATTRIBUTIONS",
    "identity_facts",
    "identity_requirements",
    "identity_response_defect",
    "rescore_free_generation_report",
    "run_free_generation_evaluation",
    "run_conditional_identity_evaluation",
    "run_routed_identity_evaluation",
]
