from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from app.backend.chat.service import ChatService  # noqa: E402
from app.backend.runtime.model_bundles import ModelBundleRegistry  # noqa: E402
from app.backend.runtime.salty_native import SaltyNativeProfile  # noqa: E402
from app.backend.runtime.salty_native_worker import SaltyNativeWorkerRuntime  # noqa: E402
from tools.gpu_validation_guard import gpu_validation_preflight  # noqa: E402


CONTEXT_PRESETS = (
    16_384,
    24_576,
    32_768,
    49_152,
    65_536,
    98_304,
    131_072,
    196_608,
    262_144,
)
OUTPUT_PRESETS = (256, 512, 1_024, 2_048, 4_096, 8_192, 16_384, 32_768)


class _GenerationConfig:
    def section(self, name: str) -> dict[str, Any]:
        if name != "generation":
            raise KeyError(name)
        return {
            "conversation_token_budget": 32_768,
            "reserved_output_tokens": 2_048,
            "maximum_output_tokens": 2_048,
            "maximum_output_mode": "automatic",
            "reasoning_mode": "instant",
            "temperature": 0.8,
            "top_p": 0.95,
            "top_k": 40,
            "repetition_penalty": 1.1,
            "seed": -1,
        }


def _normaliser(bundle: dict[str, Any], profile: SaltyNativeProfile) -> ChatService:
    service = object.__new__(ChatService)
    service.config = _GenerationConfig()
    service.model_bundle = dict(bundle)
    service.model_bundle_runtime = SimpleNamespace(profile=profile)
    return service


def _adapters(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "adapter_id": str(companion["id"]),
            "path": str(companion["artifact_path"]),
            "sha256": str(companion["checksum"]),
            "scale": float(companion.get("scale", 1.0)),
            "activation": str(companion.get("activation") or "always"),
        }
        for companion in bundle.get("companion_artifacts", [])
        if companion.get("role") in {"text_adapter", "routing_adapter"}
    ]


def _generate(
    runtime: SaltyNativeWorkerRuntime,
    *,
    context_tokens: int,
    output_tokens: int,
    output_mode: str = "manual",
) -> tuple[Any, float]:
    started = time.monotonic()
    generated = runtime.generate(
        messages=[
            {
                "role": "user",
                "content": "Reply with the single digit 7 and nothing else.",
            }
        ],
        maximum_output_tokens=output_tokens,
        reserved_output_tokens=output_tokens,
        temperature=0.0,
        top_p=1.0,
        top_k=1,
        repetition_penalty=1.0,
        seed=20260824,
        stop_sequences=("\n", ".", "!"),
        reasoning_mode="instant",
        maximum_output_mode=output_mode,
        context_window_tokens=context_tokens,
        enabled_adapter_ids=(),
    )
    return generated, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-library",
        type=Path,
        default=PROJECT_ROOT / "workspace" / "models",
    )
    parser.add_argument("--model-id", default="base-steak-2-0-9b-steak20")
    parser.add_argument(
        "--runtime",
        type=Path,
        default=(
            PROJECT_ROOT
            / "workspace"
            / "runtime"
            / "salty-native-steak20"
            / "bin"
        ),
    )
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--guard-workspace",
        type=Path,
        default=PROJECT_ROOT / "workspace",
        help="Production workspace whose active GPU operations block validation.",
    )
    arguments = parser.parse_args()

    report_path = arguments.report.resolve()
    if report_path.exists():
        raise FileExistsError(f"Refusing to overwrite {report_path}")

    preflight = gpu_validation_preflight(arguments.guard_workspace)
    if not preflight["idle"]:
        report = {
            "schema": "salty-steak-generation-matrix-v1",
            "gpu_preflight": preflight,
            "blocked": True,
            "passed": False,
            "reason": "A live user GPU operation is active; validation was not started.",
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    bundle = ModelBundleRegistry(arguments.model_library).get(arguments.model_id)
    profile = SaltyNativeProfile.from_manifest(bundle["runtime_profile"])
    adapters = _adapters(bundle)
    service = _normaliser(bundle, profile)

    contract_rows: list[dict[str, Any]] = []
    for context_tokens in CONTEXT_PRESETS:
        automatic = service._normalise_generation_settings(
            {
                "context_window_tokens": context_tokens,
                "maximum_output_mode": "automatic",
            }
        )
        contract_rows.append(
            {
                "kind": "automatic",
                "context_tokens": context_tokens,
                "output_tokens": automatic["maximum_output_tokens"],
                "accepted": True,
                "expected_output_tokens": min(32_768, context_tokens // 2),
                "passed": automatic["maximum_output_tokens"]
                == min(32_768, context_tokens // 2),
            }
        )
        for output_tokens in OUTPUT_PRESETS:
            should_accept = output_tokens < context_tokens
            try:
                normalized = service._normalise_generation_settings(
                    {
                        "context_window_tokens": context_tokens,
                        "maximum_output_mode": "manual",
                        "maximum_output_tokens": output_tokens,
                    }
                )
            except ValueError as error:
                contract_rows.append(
                    {
                        "kind": "manual",
                        "context_tokens": context_tokens,
                        "output_tokens": output_tokens,
                        "accepted": False,
                        "expected_acceptance": should_accept,
                        "error": str(error),
                        "passed": not should_accept,
                    }
                )
            else:
                contract_rows.append(
                    {
                        "kind": "manual",
                        "context_tokens": context_tokens,
                        "output_tokens": output_tokens,
                        "accepted": True,
                        "expected_acceptance": should_accept,
                        "effective_output_tokens": normalized[
                            "maximum_output_tokens"
                        ],
                        "passed": should_accept
                        and normalized["maximum_output_tokens"] == output_tokens,
                    }
                )

    runtime = SaltyNativeWorkerRuntime(
        model_path=Path(str(bundle["model_path"])),
        library_directory=arguments.runtime,
        source_sha256=str(bundle["checksum"]),
        profile=profile,
        adapters=adapters,
    )
    loaded: dict[str, Any] = {}
    final_state: dict[str, Any] = {}
    context_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    automatic_rows: list[dict[str, Any]] = []
    resident_context_tokens = int(profile.initial_context_limit)
    try:
        loaded = runtime.load()
        for context_tokens in CONTEXT_PRESETS:
            generated, duration = _generate(
                runtime,
                context_tokens=context_tokens,
                output_tokens=8,
            )
            technical = dict(generated.technical_details)
            expected_allocation = max(
                resident_context_tokens,
                context_tokens,
            )
            context_rows.append(
                {
                    "context_tokens": context_tokens,
                    "duration_seconds": round(duration, 4),
                    "output": str(generated.text),
                    "generated_tokens": len(generated.token_ids),
                    "finish_reason": generated.finish_reason,
                    "effective_context_limit": technical.get(
                        "effective_context_limit"
                    ),
                    "allocated_context_limit": technical.get(
                        "allocated_context_limit"
                    ),
                    "expected_allocated_context_limit": expected_allocation,
                    "resident_allocation_retained": (
                        context_tokens < resident_context_tokens
                    ),
                    "kv_cache_placement": technical.get("kv_cache_placement"),
                    "context_reallocated": technical.get("context_reallocated"),
                    "active_adapter_ids": technical.get("active_adapter_ids"),
                    "passed": (
                        not generated.cancelled
                        and len(generated.token_ids) > 0
                        and technical.get("effective_context_limit")
                        == context_tokens
                        and technical.get("allocated_context_limit")
                        == expected_allocation
                        and technical.get("active_adapter_ids") == []
                    ),
                }
            )

        for output_tokens in OUTPUT_PRESETS:
            generated, duration = _generate(
                runtime,
                context_tokens=65_536,
                output_tokens=output_tokens,
            )
            technical = dict(generated.technical_details)
            output_rows.append(
                {
                    "output_tokens": output_tokens,
                    "duration_seconds": round(duration, 4),
                    "output": str(generated.text),
                    "generated_tokens": len(generated.token_ids),
                    "finish_reason": generated.finish_reason,
                    "maximum_output_mode_effective": technical.get(
                        "maximum_output_mode_effective"
                    ),
                    "maximum_output_token_ceiling": technical.get(
                        "maximum_output_token_ceiling"
                    ),
                    "requested_reserved_output_tokens": technical.get(
                        "requested_reserved_output_tokens"
                    ),
                    "effective_context_limit": technical.get(
                        "effective_context_limit"
                    ),
                    "allocated_context_limit": technical.get(
                        "allocated_context_limit"
                    ),
                    "active_adapter_ids": technical.get("active_adapter_ids"),
                    "passed": (
                        not generated.cancelled
                        and len(generated.token_ids) > 0
                        and technical.get("maximum_output_mode_effective")
                        == "manual"
                        and technical.get("maximum_output_token_ceiling")
                        == output_tokens
                        and technical.get("requested_reserved_output_tokens")
                        == output_tokens
                        and technical.get("effective_context_limit") == 65_536
                        and technical.get("allocated_context_limit") == 65_536
                        and technical.get("active_adapter_ids") == []
                    ),
                }
            )

        for context_tokens in (16_384, 32_768, 262_144):
            automatic_settings = service._normalise_generation_settings(
                {
                    "context_window_tokens": context_tokens,
                    "maximum_output_mode": "automatic",
                }
            )
            generated, duration = _generate(
                runtime,
                context_tokens=context_tokens,
                output_tokens=int(automatic_settings["maximum_output_tokens"]),
                output_mode="automatic",
            )
            technical = dict(generated.technical_details)
            expected_allocation = max(
                resident_context_tokens,
                context_tokens,
            )
            automatic_rows.append(
                {
                    "context_tokens": context_tokens,
                    "normalized_output_ceiling": automatic_settings[
                        "maximum_output_tokens"
                    ],
                    "duration_seconds": round(duration, 4),
                    "generated_tokens": len(generated.token_ids),
                    "finish_reason": generated.finish_reason,
                    "effective_context_limit": technical.get(
                        "effective_context_limit"
                    ),
                    "allocated_context_limit": technical.get(
                        "allocated_context_limit"
                    ),
                    "expected_allocated_context_limit": expected_allocation,
                    "maximum_output_mode_effective": technical.get(
                        "maximum_output_mode_effective"
                    ),
                    "passed": (
                        not generated.cancelled
                        and len(generated.token_ids) > 0
                        and technical.get("effective_context_limit")
                        == context_tokens
                        and technical.get("allocated_context_limit")
                        == expected_allocation
                        and technical.get("maximum_output_mode_effective")
                        == "automatic"
                    ),
                }
            )
        final_state = runtime.describe()
    finally:
        runtime.unload()

    expected_adapter_hashes = {
        value["adapter_id"]: value["sha256"] for value in adapters
    }
    loaded_adapter_hashes = {
        value.get("adapter_id"): value.get("verified_sha256")
        for value in loaded.get("adapters", [])
    }
    gates = {
        "manifest_default_is_32768": bundle.get("default_context_tokens")
        == 32_768,
        "manifest_context_presets_exact": tuple(bundle.get("context_presets") or ())
        == CONTEXT_PRESETS,
        "manifest_maximum_is_262144": bundle.get("configured_context_tokens")
        == 262_144,
        "runtime_starts_resident_at_32768": loaded.get("allocated_context_limit")
        == 32_768,
        "model_hash_verified": loaded.get("verified_source_sha256")
        == bundle.get("checksum"),
        "all_adapter_hashes_verified": loaded_adapter_hashes
        == expected_adapter_hashes,
        "all_contract_combinations_pass": all(
            row["passed"] for row in contract_rows
        ),
        "all_nine_contexts_generate_with_exact_effective_limits": len(context_rows) == 9
        and all(row["passed"] for row in context_rows),
        "all_eight_output_ceilings_generate_and_apply_exactly": len(output_rows)
        == 8
        and all(row["passed"] for row in output_rows),
        "automatic_mode_generates_at_low_default_and_maximum_context": len(
            automatic_rows
        )
        == 3
        and all(row["passed"] for row in automatic_rows),
        "maximum_context_uses_host_kv": next(
            row for row in context_rows if row["context_tokens"] == 262_144
        )["kv_cache_placement"]
        == "host",
        "conditional_adapters_remain_disabled": all(
            row.get("active_adapter_ids") == []
            for row in [*context_rows, *output_rows]
        ),
        "private_pipe_transport": loaded.get("ipc_transport")
        == "anonymous_pipes",
        "no_network_listener": loaded.get("network_listener_created") is False,
        "runtime_remains_loaded_after_matrix": final_state.get("loaded") is True,
    }
    report = {
        "schema": "salty-steak-generation-matrix-v1",
        "model_id": bundle.get("id"),
        "model_sha256": bundle.get("checksum"),
        "runtime_profile": bundle.get("runtime_profile"),
        "gpu_preflight": preflight,
        "context_presets": list(CONTEXT_PRESETS),
        "output_presets": list(OUTPUT_PRESETS),
        "loaded": loaded,
        "contract_rows": contract_rows,
        "context_rows": context_rows,
        "output_rows": output_rows,
        "automatic_rows": automatic_rows,
        "final_state": final_state,
        "gates": gates,
        "passed": all(gates.values()),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print(
        json.dumps(
            {
                "report": str(report_path),
                "context_rows": context_rows,
                "output_rows": output_rows,
                "automatic_rows": automatic_rows,
                "gates": gates,
                "passed": report["passed"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
