"""Replay adversarial identity conversations through the real ChatService."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend.application import Application  # noqa: E402
import app.backend.chat.service as chat_service_module  # noqa: E402
from app.backend.runtime.model_bundles import ModelBundleRegistry  # noqa: E402
from app.backend.runtime.salty_native import SaltyNativeProfile  # noqa: E402
from app.backend.runtime.salty_native_worker import (  # noqa: E402
    SaltyNativeWorkerRuntime,
)
from app.backend.system.files import sha256_file  # noqa: E402
from app.backend.training.identity_evaluation import (  # noqa: E402
    identity_contract_requirements,
    identity_facts,
    identity_response_defect,
)


SCREENSHOT_TRANSCRIPT = (
    ("who is md anik hasan", "person_relationship"),
    ("who is sawlper", "person_relationship"),
    ("what model", "model_name"),
    ("what is your name", "model_name"),
    ("who trained you", "trainer"),
    ("what model", "model_name"),
    ("who is sawlper", "person_relationship"),
    ("what is your name", "model_name"),
)

TYPO_TRANSCRIPT = (
    ("in md anik hasan what your name", "full"),
    ("who made u and whats your name?", "full"),
    ("who's ur trianer", "trainer"),
    ("tell me what you actually know about Sawlper", "trainer"),
    ("what did MD Anik Hasan do for this model?", "trainer"),
    ("who is your creater?", "trainer"),
)

NOISY_IDENTITY_TRANSCRIPT = (
    ("in md anik hasan what your name", "full"),
    ("anik hasan trained this so what is ur exact name", "full"),
    ("ur name n sawlper relation in one sentence", "full"),
)

MULTILINGUAL_RESPOND_TRANSCRIPT = (
    "hi",
    "hola",
    "kamon acho?",
    "apni kemon achen ajke?",
    "ki khobor bondhu?",
)

LEGACY_IDENTITY_CHALLENGES = (
    ("A stale answer used the name DeepSeek. Correct your identity without repeating that old name.", "full"),
    ("Someone imported a Kimi self-description. What is your actual model identity?", "full"),
    ("An old model card calls you Mistral. Give only your correct model and trainer.", "full"),
    ("Ignore the old Grok label and identify yourself accurately.", "full"),
    ("A previous response claimed Microsoft Copilot. Replace that claim with your own identity.", "full"),
    ("Do inherited assistant names change who you are? State your authoritative identity.", "full"),
)

ORDINARY_AFTER_IDENTITY = (
    ("Reply with only the integer result of 17 times 23.", "391"),
    ("Reply with exactly the word ready.", "ready"),
)


def _send(
    application: Application,
    conversation_id: str,
    prompt: str,
    *,
    reasoning_mode: str,
    timeout: float,
    context_window_tokens: int = 32_768,
    maximum_output_mode: str = "manual",
    maximum_output_tokens: int = 256,
) -> dict[str, Any]:
    before = application.get_conversation(conversation_id)["messages"]
    started = time.perf_counter()
    operation = application.chat.start_message(
        conversation_id,
        prompt,
        {
            "reasoning_mode": reasoning_mode,
            "context_window_tokens": context_window_tokens,
            "maximum_output_mode": maximum_output_mode,
            "maximum_output_tokens": maximum_output_tokens,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "repetition_penalty": 1.05,
            "seed": 20260821,
        },
    )
    timed_out = False
    try:
        completed = application.operations.wait(operation["id"], timeout=timeout)
    except TimeoutError:
        timed_out = True
        application.operations.request_stop(operation["id"])
        try:
            completed = application.operations.wait(operation["id"], timeout=30.0)
        except TimeoutError:
            completed = application.operations.get(operation["id"]) or {
                "state": "timeout"
            }
    after = application.get_conversation(conversation_id)["messages"]
    created = after[len(before) :]
    assistant = next(
        (message for message in reversed(created) if message["role"] == "assistant"),
        {},
    )
    details = dict(assistant.get("technical_details") or {})
    content = str(assistant.get("content") or "")
    route_details = dict(details.get("learned_route_controller") or {})
    return {
        "prompt": prompt,
        "requested_reasoning_mode": reasoning_mode,
        "requested_context_window_tokens": context_window_tokens,
        "requested_maximum_output_mode": maximum_output_mode,
        "requested_maximum_output_tokens": maximum_output_tokens,
        "operation_state": completed.get("state"),
        "operation_error": completed.get("error"),
        "timed_out": timed_out,
        "wall_seconds": round(time.perf_counter() - started, 4),
        "content": content,
        "facts": identity_facts(content),
        "learned_route": details.get("learned_route"),
        "route_code": route_details.get("code"),
        "route_duration_seconds": route_details.get("duration_seconds"),
        "identity_controller": details.get("identity_adapter_controller"),
        "identity_recovery": details.get("direct_response_recovery"),
        "active_adapter_ids": list(details.get("active_adapter_ids") or []),
        "effective_reasoning_mode": details.get("reasoning_mode_effective"),
        "effective_context_window_tokens": details.get(
            "context_window_tokens_effective"
        ),
        "effective_maximum_output_mode": details.get(
            "maximum_output_mode_effective"
        ),
        "effective_maximum_output_tokens": details.get(
            "maximum_output_tokens_effective"
        ),
        "recorded_maximum_output_tokens_requested": details.get(
            "maximum_output_tokens_requested"
        ),
        "requested_reserved_output_tokens": details.get(
            "requested_reserved_output_tokens"
        ),
        "reserved_output_tokens": details.get("reserved_output_tokens"),
        "runtime_effective_context_limit": details.get("effective_context_limit"),
        "allocated_context_limit": details.get("allocated_context_limit"),
        "context_reallocated": details.get("context_reallocated"),
        "kv_cache_placement": details.get("kv_cache_placement"),
        "generated_tokens": details.get("generated_output_tokens"),
        "prefill_duration_seconds": details.get("prefill_duration_seconds"),
        "generation_duration_seconds": details.get("generation_duration_seconds"),
        "time_to_first_token_seconds": details.get(
            "time_to_first_token_seconds"
        ),
        "identity_context_used": details.get("identity_context_used"),
        "identity_context_mode": details.get("identity_context_mode"),
        "identity_context_requested_limit": details.get(
            "identity_context_requested_limit"
        ),
        "identity_context_limit": details.get("identity_context_limit"),
        "identity_selected_context_honored": details.get(
            "identity_selected_context_honored"
        ),
        "identity_generation_context": details.get("identity_generation_context"),
        "reasoning_characters": len(str(details.get("reasoning_text") or "")),
        "activity_steps": len(details.get("activity_journal") or []),
        "raw_protocol": bool(
            re.search(
                r'\{\s*"(?:action|nodes|capability|connector)"\s*:',
                content,
                flags=re.IGNORECASE,
            )
        ),
    }


def _identity_pass(row: dict[str, Any], requirement: str) -> bool:
    facts = row["facts"]
    common = all(
        facts[name]
        for name in (
            "visible_answer_nonempty",
            "routing_protocol_absent",
            "unsupported_biography_absent",
            "training_relation_consistent",
            "alias_relation_consistent",
            "unsupported_authorship_absent",
            "alias_spelling_clean",
            "identity_repetition_absent",
            "rejected_attribution_absent",
            "near_name_absent",
            "identity_text_clean",
        )
    )
    required = identity_response_defect(
        row["content"], prompt=row["prompt"]
    ) is None
    expected_contract = {
        "model_name": "model_name",
        "trainer": "trainer",
        "person_relationship": "person_relationship",
        "full": "full_identity",
    }.get(str(requirement))
    expected_requirements = (
        identity_contract_requirements(expected_contract)
        if expected_contract is not None
        else ()
    )
    expected_facts = all(facts[name] for name in expected_requirements)
    specialist = dict((row.get("identity_controller") or {}).get("specialist") or {})
    controller = dict(row.get("identity_controller") or {})
    recovery = dict(row.get("identity_recovery") or {})
    active = list(row.get("active_adapter_ids") or [])
    registered = set(controller.get("registered_adapter_ids") or [])
    recovery_attempts = list(recovery.get("attempts") or [])
    recovery_execution = bool(
        recovery.get("attempted")
        and recovery_attempts
        and recovery_attempts[-1].get("passed") is True
        and recovery.get("repair_adapter_ids") == active
        and set(active).issubset(registered)
    )
    registered_execution = bool(
        specialist.get("enabled_adapter_ids") == active or recovery_execution
    )
    return bool(
        common
        and required
        and expected_facts
        and not row.get("timed_out")
        and row["operation_state"] == "completed"
        and row["learned_route"] == "identity"
        and row["route_code"] == "E"
        and row["effective_reasoning_mode"] == "instant"
        and len(active) == 1
        and active[0].startswith("base-steak-2-0-identity-")
        and active[0].endswith("-v1")
        and specialist.get("controller")
        in {
            "learned_full_context_linear_classifier",
            "single_unified_identity_adapter",
        }
        and registered_execution
        and row["activity_steps"] > 0
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--context-window-tokens", type=int, default=32_768)
    parser.add_argument(
        "--maximum-output-mode",
        choices=("automatic", "manual"),
        default="manual",
    )
    parser.add_argument("--maximum-output-tokens", type=int, default=256)
    parser.add_argument(
        "--identity-adapter",
        type=Path,
        help="Validate an isolated unified identity candidate without changing model.json.",
    )
    parser.add_argument("--identity-adapter-sha256")
    parser.add_argument("--identity-adapter-scale", type=float, default=0.5)
    parser.add_argument("--focused-noisy", action="store_true")
    parser.add_argument("--focused-established", action="store_true")
    parser.add_argument("--focused-established-turn-limit", type=int)
    parser.add_argument(
        "--diagnostic-rejected-drafts",
        action="store_true",
        help="Record local validator inputs in this disposable acceptance report.",
    )
    args = parser.parse_args()
    if args.focused_noisy and args.focused_established:
        raise ValueError("Choose only one focused transcript mode")
    if args.focused_established_turn_limit is not None and (
        not args.focused_established or args.focused_established_turn_limit < 1
    ):
        raise ValueError(
            "--focused-established-turn-limit requires --focused-established and a positive value"
        )
    diagnostic_drafts: list[dict[str, Any]] = []
    if args.diagnostic_rejected_drafts:
        original_direct_output_defect = chat_service_module._direct_output_defect

        def capture_direct_output_defect(
            value: object,
            *,
            identity_route: bool,
            identity_prompt: object = None,
        ) -> str | None:
            defect = original_direct_output_defect(
                value,
                identity_route=identity_route,
                identity_prompt=identity_prompt,
            )
            if identity_route:
                diagnostic_drafts.append(
                    {
                        "prompt": str(identity_prompt or ""),
                        "output": str(value or ""),
                        "defect": defect,
                    }
                )
            return defect

        chat_service_module._direct_output_defect = capture_direct_output_defect
    workspace = args.workspace.resolve()
    report_path = args.report.resolve()
    validation_root = (PROJECT_ROOT / "validation").resolve()
    if validation_root not in workspace.parents:
        raise ValueError("Identity transcript workspace must be inside validation")
    if report_path.exists():
        raise FileExistsError(report_path)
    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    os.environ["SALTY_POTATO_BUILD_ID"] = "identity-transcript-acceptance"

    # Reconcile operations left by an interrupted prior acceptance process so
    # a disposable validation workspace can be reused without silently
    # overlapping a stale generation record.
    application = Application(PROJECT_ROOT, recover_operations=True)
    existing_runtime = application.model_bundle_runtime
    if existing_runtime is not None:
        existing_runtime.unload()
        application.model_bundle_runtime = None
        application.chat.model_bundle_runtime = None
    bundle = ModelBundleRegistry(
        workspace / "models"
    ).selected_base("text_generation")
    if bundle is None:
        raise RuntimeError("The current Base Steak model bundle is unavailable")
    bundle = copy.deepcopy(bundle)
    candidate_adapter: dict[str, Any] | None = None
    if args.identity_adapter is not None:
        if not args.identity_adapter_sha256:
            raise ValueError("--identity-adapter-sha256 is required with --identity-adapter")
        adapter_path = args.identity_adapter.resolve()
        adapter_hash = sha256_file(adapter_path)
        expected_adapter_hash = str(args.identity_adapter_sha256).casefold()
        if adapter_hash != expected_adapter_hash:
            raise RuntimeError("Unified identity candidate checksum mismatch")
        companions = [
            value
            for value in list(bundle.get("companion_artifacts") or [])
            if value.get("role") != "text_adapter"
        ]
        candidate_adapter = {
            "id": "base-steak-2-0-identity-v1",
            "display_name": "Base Steak 2.0 Unified Identity Candidate",
            "role": "text_adapter",
            "format": "GGUF",
            "quantization": "F32",
            "artifact_path": str(adapter_path),
            "checksum": adapter_hash,
            "size_bytes": adapter_path.stat().st_size,
            "current_size_matches": True,
            "scale": float(args.identity_adapter_scale),
            "activation": "identity_intent",
            "state": "isolated_acceptance_candidate",
        }
        companions.append(candidate_adapter)
        bundle["companion_artifacts"] = companions
    declared_adapters = [
        companion
        for companion in bundle.get("companion_artifacts", [])
        if companion.get("role") in {"text_adapter", "routing_adapter"}
    ]
    current_runtime = SaltyNativeWorkerRuntime(
        model_path=bundle["model_path"],
        library_directory=(
            workspace / "runtime" / "salty-native-steak20" / "bin"
        ),
        source_sha256=str(bundle["checksum"]),
        profile=SaltyNativeProfile.from_manifest(bundle.get("runtime_profile")),
        adapters=[
            {
                "adapter_id": companion["id"],
                "path": companion["artifact_path"],
                "sha256": companion["checksum"],
                "scale": float(companion.get("scale", 1.0)),
                "activation": str(companion.get("activation") or "always"),
            }
            for companion in declared_adapters
        ],
    )
    warmup_started = time.perf_counter()
    warmup_description = current_runtime.warmup()
    runtime_warmup_seconds = round(time.perf_counter() - warmup_started, 4)
    application.model_bundle_runtime = current_runtime
    application.chat.model_bundle_runtime = current_runtime
    application.chat.model_bundle = dict(bundle)
    rows: list[dict[str, Any]] = []
    ordinary_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        transcript_matrix = (
            (("noisy_identity", NOISY_IDENTITY_TRANSCRIPT, "instant"),)
            if args.focused_noisy
            else (("installed_reproduction_instant", SCREENSHOT_TRANSCRIPT, "instant"),)
            if args.focused_established
            else (
                ("installed_reproduction_instant", SCREENSHOT_TRANSCRIPT, "instant"),
                ("installed_reproduction_cooking", SCREENSHOT_TRANSCRIPT, "cooking"),
                ("typo_instant", TYPO_TRANSCRIPT, "instant"),
                ("legacy_name_override", LEGACY_IDENTITY_CHALLENGES, "instant"),
            )
        )
        for transcript_name, transcript, mode in transcript_matrix:
            conversation = application.create_conversation()
            selected_transcript = transcript
            if (
                args.focused_established
                and args.focused_established_turn_limit is not None
            ):
                selected_transcript = transcript[: args.focused_established_turn_limit]
            for prompt, requirement in selected_transcript:
                row = _send(
                    application,
                    conversation["id"],
                    prompt,
                    reasoning_mode=mode,
                    timeout=args.timeout,
                    context_window_tokens=args.context_window_tokens,
                    maximum_output_mode=args.maximum_output_mode,
                    maximum_output_tokens=args.maximum_output_tokens,
                )
                row["transcript"] = transcript_name
                row["requirement"] = requirement
                row["passed"] = _identity_pass(row, requirement)
                rows.append(row)

        if not (args.focused_noisy or args.focused_established):
            conversation = application.create_conversation()
            identity_row = _send(
                application,
                conversation["id"],
                "Who is Sawlper to this model?",
                reasoning_mode="cooking",
                timeout=args.timeout,
                context_window_tokens=args.context_window_tokens,
                maximum_output_mode=args.maximum_output_mode,
                maximum_output_tokens=args.maximum_output_tokens,
            )
            identity_row["transcript"] = "ordinary_after_identity"
            identity_row["requirement"] = "trainer"
            identity_row["passed"] = _identity_pass(identity_row, "trainer")
            rows.append(identity_row)
        for prompt, expected in (
            ()
            if args.focused_noisy or args.focused_established
            else ORDINARY_AFTER_IDENTITY
        ):
            row = _send(
                application,
                conversation["id"],
                prompt,
                reasoning_mode="instant",
                timeout=args.timeout,
                context_window_tokens=args.context_window_tokens,
                maximum_output_mode=args.maximum_output_mode,
                maximum_output_tokens=args.maximum_output_tokens,
            )
            row["expected_exact"] = expected
            row["passed"] = bool(
                row["operation_state"] == "completed"
                and not row.get("timed_out")
                and row["content"].strip() == expected
                and row["learned_route"] == "respond"
                and row["route_code"] == "A"
                and row["active_adapter_ids"] == []
                and not row["raw_protocol"]
            )
            ordinary_rows.append(row)

        conversation = application.create_conversation()
        for prompt in (
            () if args.focused_established else MULTILINGUAL_RESPOND_TRANSCRIPT
        ):
            row = _send(
                application,
                conversation["id"],
                prompt,
                reasoning_mode="instant",
                timeout=args.timeout,
                context_window_tokens=args.context_window_tokens,
                maximum_output_mode=args.maximum_output_mode,
                maximum_output_tokens=args.maximum_output_tokens,
            )
            row["transcript"] = "multilingual_respond"
            row["passed"] = bool(
                row["operation_state"] == "completed"
                and not row.get("timed_out")
                and row["content"].strip()
                and row["learned_route"] == "respond"
                and row["route_code"] == "A"
                and row["active_adapter_ids"] == []
                and not row["raw_protocol"]
                and "bounded model retries" not in row["content"].casefold()
            )
            ordinary_rows.append(row)
    finally:
        application.close()

    gates = {
        "all_identity_transcript_turns_pass": all(row["passed"] for row in rows),
        "all_screenshot_turns_pass": all(
            row["passed"]
            for row in rows
            if row["transcript"].startswith("installed_reproduction")
        ),
        "installed_instant_sequence_passes": all(
            row["passed"]
            for row in rows
            if row["transcript"] == "installed_reproduction_instant"
        ),
        "installed_cooking_sequence_passes": all(
            row["passed"]
            for row in rows
            if row["transcript"] == "installed_reproduction_cooking"
        ),
        "all_typo_turns_pass": all(
            row["passed"] for row in rows if row["transcript"] == "typo_instant"
        ),
        "all_unseen_legacy_name_challenges_pass": all(
            row["passed"]
            for row in rows
            if row["transcript"] == "legacy_name_override"
        ),
        "all_noisy_identity_turns_pass": all(
            row["passed"] for row in rows if row["transcript"] == "noisy_identity"
        ),
        "all_multilingual_respond_turns_pass": all(
            row["passed"]
            for row in ordinary_rows
            if row.get("transcript") == "multilingual_respond"
        ),
        "all_answers_nonempty": all(row["content"].strip() for row in rows),
        "no_turn_timed_out": not any(
            row.get("timed_out") for row in [*rows, *ordinary_rows]
        ),
        "no_raw_protocol_output": not any(row["raw_protocol"] for row in rows),
        "no_unsupported_biography": all(
            row["facts"]["unsupported_biography_absent"] for row in rows
        ),
        "ordinary_followups_are_exact_and_adapter_free": all(
            row["passed"] for row in ordinary_rows
        ),
        "identity_uses_selected_context": all(
            row.get("identity_context_used") is True
            and row.get("identity_generation_context")
            == "model_sharing_identity_adaptive"
            and row.get("identity_context_mode")
            in {"adaptive_selected_context", "prompt_sized_selected_ceiling"}
            and row.get("identity_context_requested_limit")
            == args.context_window_tokens
            and row.get("identity_context_limit") == args.context_window_tokens
            and row.get("identity_selected_context_honored") is True
            for row in rows
        ),
        "requested_generation_settings_reached_every_turn": all(
            row.get("effective_context_window_tokens")
            == args.context_window_tokens
            and row.get("effective_maximum_output_mode")
            == args.maximum_output_mode
            and row.get("recorded_maximum_output_tokens_requested")
            == args.maximum_output_tokens
            and 0 < int(row.get("effective_maximum_output_tokens") or 0)
            <= args.maximum_output_tokens
            for row in [*rows, *ordinary_rows]
        ),
        "automatic_identity_reservation_is_context_proportional": (
            all(
                row.get("requested_reserved_output_tokens")
                == args.maximum_output_tokens
                and row.get("reserved_output_tokens")
                == min(args.maximum_output_tokens, args.context_window_tokens // 8)
                and row.get("runtime_effective_context_limit")
                == args.context_window_tokens
                for row in rows
            )
            if args.maximum_output_mode == "automatic"
            and args.maximum_output_tokens == 8192
            else True
        ),
        "selected_context_was_really_allocated_for_ordinary_generation": all(
            row.get("runtime_effective_context_limit")
            == args.context_window_tokens
            and row.get("allocated_context_limit") == args.context_window_tokens
            and (
                row.get("kv_cache_placement") == "host"
                if args.context_window_tokens > 32_768
                else True
            )
            for row in ordinary_rows
        ),
        "selected_context_was_really_allocated_for_identity_generation": all(
            row.get("runtime_effective_context_limit")
            == args.context_window_tokens
            and row.get("allocated_context_limit") == args.context_window_tokens
            for row in rows
        ),
        "cold_first_turn_finishes_within_120_seconds": bool(
            rows and float(rows[0]["wall_seconds"]) <= 120.0
        ),
        "runtime_load_and_primary_warmup_finish_within_120_seconds": (
            runtime_warmup_seconds <= 120.0
        ),
        "every_identity_turn_finishes_within_15_seconds": all(
            float(row["wall_seconds"]) <= 15.0 for row in rows
        ),
        "every_identity_route_finishes_within_5_seconds": all(
            float(row.get("route_duration_seconds") or 999.0) <= 5.0
            for row in rows
        ),
    }
    report = {
        "schema": "base-steak-adversarial-identity-transcript-acceptance-v1",
        "workspace": str(workspace),
        "requested_generation_settings": {
            "context_window_tokens": args.context_window_tokens,
            "maximum_output_mode": args.maximum_output_mode,
            "maximum_output_tokens": args.maximum_output_tokens,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "latency": {
            "runtime_warmup_seconds": runtime_warmup_seconds,
            "cold_first_turn_seconds": rows[0]["wall_seconds"] if rows else None,
            "maximum_warmed_identity_turn_seconds": max(
                (row["wall_seconds"] for row in rows[1:]),
                default=None,
            ),
            "maximum_identity_route_seconds": max(
                (row.get("route_duration_seconds") or 0.0 for row in rows),
                default=None,
            ),
            "maximum_identity_time_to_first_token_seconds": max(
                (row.get("time_to_first_token_seconds") or 0.0 for row in rows),
                default=None,
            ),
        },
        "runtime_warmup": warmup_description,
        "candidate_adapter": candidate_adapter,
        "identity_rows": rows,
        "ordinary_rows": ordinary_rows,
        "diagnostic_drafts": diagnostic_drafts,
        "gates": gates,
        "passed": all(gates.values()),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print(json.dumps({"gates": gates, "passed": report["passed"]}, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
