"""Deterministic memory planning for local transformer inference.

The planner never claims that a backend exists.  It answers a narrower and
useful question: which precision is the highest-quality representation that
fits the measured hardware budget, including the KV cache and runtime
headroom.  Execution backends consume this contract in a later step.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


MIB = 1024**2
GIB = 1024**3


@dataclass(frozen=True, slots=True)
class QuantizationProfile:
    id: str
    label: str
    effective_bits_per_weight: float
    quality_rank: int
    execution_family: str


PROFILES = (
    QuantizationProfile("bf16", "BF16", 16.0, 100, "native_or_salty"),
    QuantizationProfile("fp16", "FP16", 16.0, 99, "native_or_salty"),
    QuantizationProfile("q8_0", "Q8_0", 8.5, 97, "salty_native"),
    QuantizationProfile("q6_k", "Q6_K", 6.6, 94, "salty_native"),
    QuantizationProfile("q5_k_m", "Q5_K_M", 5.7, 90, "salty_native"),
    QuantizationProfile("q4_k_m", "Q4_K_M", 4.8, 84, "salty_native"),
    QuantizationProfile("q3_k_m", "Q3_K_M", 3.7, 74, "salty_native"),
    QuantizationProfile("q2_k", "Q2_K", 2.8, 58, "salty_native"),
)


def _integer(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(payload.get(name, default))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _architecture_defaults(parameter_count: int) -> dict[str, int]:
    billions = parameter_count / 1_000_000_000
    if billions <= 4:
        return {"layer_count": 32, "kv_head_count": 8, "head_dimension": 128}
    if billions <= 10:
        return {"layer_count": 36, "kv_head_count": 8, "head_dimension": 128}
    if billions <= 35:
        return {"layer_count": 60, "kv_head_count": 8, "head_dimension": 128}
    if billions <= 80:
        return {"layer_count": 80, "kv_head_count": 8, "head_dimension": 128}
    if billions <= 200:
        return {"layer_count": 96, "kv_head_count": 12, "head_dimension": 128}
    return {"layer_count": 128, "kv_head_count": 16, "head_dimension": 128}


def _strategy(
    *,
    working_set: int,
    weight_bytes: int,
    kv_cache_bytes: int,
    ram_budget: int,
    vram_budget: int,
    installed_ram: int,
    disk_free: int,
) -> tuple[str, bool, int, str]:
    if vram_budget > 0 and working_set <= vram_budget:
        return (
            "gpu_resident",
            True,
            100,
            "Weights and KV cache fit inside the safe GPU budget.",
        )
    if vram_budget > kv_cache_bytes + 256 * MIB and working_set <= ram_budget + vram_budget:
        offload_bytes = max(0, vram_budget - kv_cache_bytes - 256 * MIB)
        offload_percent = min(99, int(offload_bytes * 100 / max(1, weight_bytes)))
        return (
            "cpu_gpu_hybrid",
            True,
            offload_percent,
            "Keep the full model local and offload the largest safe layer share to the GPU.",
        )
    if working_set <= ram_budget:
        return (
            "cpu_resident",
            True,
            0,
            "The complete working set fits the safe physical-memory budget.",
        )
    if working_set <= int(installed_ram * 0.90):
        return (
            "cpu_memory_mapped",
            True,
            0,
            "The model fits installed RAM but not the current free-memory budget; close other applications before loading.",
        )
    if weight_bytes <= disk_free:
        return (
            "storage_paging_required",
            False,
            0,
            "Weights fit on disk but not in usable RAM/VRAM. Per-token paging would be extremely slow and is not accepted as a runnable plan.",
        )
    return (
        "insufficient_storage",
        False,
        0,
        "The estimated weight file does not fit the measured free storage.",
    )


def plan_local_runtime(
    request: Mapping[str, Any],
    hardware: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a stable, JSON-safe model-fit plan for measured local hardware."""

    parameter_count = _integer(
        request,
        "parameter_count",
        default=7_000_000_000,
        minimum=1_000_000,
        maximum=2_000_000_000_000,
    )
    context_tokens = _integer(
        request,
        "context_tokens",
        default=8192,
        minimum=256,
        maximum=1_048_576,
    )
    batch_size = _integer(
        request,
        "batch_size",
        default=1,
        minimum=1,
        maximum=64,
    )
    defaults = _architecture_defaults(parameter_count)
    layer_count = _integer(
        request,
        "layer_count",
        default=defaults["layer_count"],
        minimum=1,
        maximum=512,
    )
    kv_head_count = _integer(
        request,
        "kv_head_count",
        default=defaults["kv_head_count"],
        minimum=1,
        maximum=256,
    )
    head_dimension = _integer(
        request,
        "head_dimension",
        default=defaults["head_dimension"],
        minimum=16,
        maximum=512,
    )
    priority = str(request.get("priority", "maximum_quality")).strip().casefold()
    if priority not in {"maximum_quality", "balanced", "maximum_capacity"}:
        raise ValueError(
            "priority must be maximum_quality, balanced, or maximum_capacity"
        )

    installed_ram = max(0, int(hardware.get("installed_ram_bytes") or 0))
    available_ram = max(0, int(hardware.get("available_ram_bytes") or 0))
    total_vram = max(0, int(hardware.get("gpu_memory_bytes") or 0))
    disk_free = max(0, int(hardware.get("drive_free_bytes") or 0))
    if not installed_ram:
        raise ValueError("installed RAM could not be measured")
    if not available_ram:
        available_ram = installed_ram

    ram_budget = max(0, min(int(installed_ram * 0.75), int(available_ram * 0.90)))
    vram_budget = int(total_vram * 0.85)
    kv_element_bytes = 2
    kv_cache_bytes = (
        2
        * layer_count
        * context_tokens
        * kv_head_count
        * head_dimension
        * kv_element_bytes
        * batch_size
    )

    candidates: list[dict[str, Any]] = []
    for profile in PROFILES:
        weight_bytes = int(
            parameter_count * profile.effective_bits_per_weight / 8
        )
        runtime_overhead_bytes = max(512 * MIB, int(weight_bytes * 0.08))
        working_set = weight_bytes + kv_cache_bytes + runtime_overhead_bytes
        strategy, runnable, gpu_offload_percent, explanation = _strategy(
            working_set=working_set,
            weight_bytes=weight_bytes,
            kv_cache_bytes=kv_cache_bytes,
            ram_budget=ram_budget,
            vram_budget=vram_budget,
            installed_ram=installed_ram,
            disk_free=disk_free,
        )
        candidates.append(
            {
                **asdict(profile),
                "weight_bytes": weight_bytes,
                "kv_cache_bytes": kv_cache_bytes,
                "runtime_overhead_bytes": runtime_overhead_bytes,
                "working_set_bytes": working_set,
                "strategy": strategy,
                "runnable": runnable,
                "gpu_offload_percent": gpu_offload_percent,
                "explanation": explanation,
            }
        )

    runnable = [candidate for candidate in candidates if candidate["runnable"]]
    if priority == "maximum_quality":
        ordered = runnable
    elif priority == "balanced":
        ordered = sorted(
            runnable,
            key=lambda item: (
                0 if item["id"] in {"q8_0", "q6_k", "q5_k_m"} else 1,
                -item["quality_rank"],
            ),
        )
    else:
        ordered = sorted(runnable, key=lambda item: item["working_set_bytes"])
    recommendation = ordered[0] if ordered else None

    return {
        "format": "salty-steak-runtime-plan-v1",
        "request": {
            "parameter_count": parameter_count,
            "context_tokens": context_tokens,
            "batch_size": batch_size,
            "layer_count": layer_count,
            "kv_head_count": kv_head_count,
            "head_dimension": head_dimension,
            "priority": priority,
        },
        "hardware": {
            "installed_ram_bytes": installed_ram,
            "available_ram_bytes": available_ram,
            "ram_safe_budget_bytes": ram_budget,
            "gpu_memory_bytes": total_vram,
            "vram_safe_budget_bytes": vram_budget,
            "drive_free_bytes": disk_free,
        },
        "assumptions": {
            "kv_cache_precision": "fp16",
            "ram_headroom_percent": 25,
            "vram_headroom_percent": 15,
            "architecture_defaults_inferred": not any(
                name in request
                for name in ("layer_count", "kv_head_count", "head_dimension")
            ),
            "estimates_not_benchmarks": True,
        },
        "recommendation": recommendation,
        "candidates": candidates,
        "runnable_candidate_count": len(runnable),
        "requires_new_backend": bool(
            recommendation and recommendation["execution_family"] == "salty_native"
        ),
    }
