from __future__ import annotations

import pytest

from app.backend.runtime.planner import GIB, plan_local_runtime


def _hardware(*, ram: int = 16, available: int = 12, vram: int = 0, disk: int = 400):
    return {
        "installed_ram_bytes": ram * GIB,
        "available_ram_bytes": available * GIB,
        "gpu_memory_bytes": vram * GIB,
        "drive_free_bytes": disk * GIB,
    }


def test_planner_selects_highest_quality_candidate_that_fits() -> None:
    result = plan_local_runtime(
        {"parameter_count": 7_000_000_000, "context_tokens": 8192},
        _hardware(),
    )

    assert result["format"] == "salty-steak-runtime-plan-v1"
    assert result["recommendation"]["id"] == "q8_0"
    assert result["recommendation"]["runnable"] is True
    assert result["requires_new_backend"] is True
    assert result["recommendation"]["working_set_bytes"] > result["recommendation"]["weight_bytes"]


def test_planner_counts_context_kv_cache_and_gpu_offload() -> None:
    smaller = plan_local_runtime(
        {"parameter_count": 7_000_000_000, "context_tokens": 4096},
        _hardware(vram=4),
    )
    larger = plan_local_runtime(
        {"parameter_count": 7_000_000_000, "context_tokens": 32768},
        _hardware(vram=4),
    )

    assert larger["candidates"][0]["kv_cache_bytes"] == 8 * smaller["candidates"][0]["kv_cache_bytes"]
    assert any(item["strategy"] == "cpu_gpu_hybrid" for item in smaller["candidates"])


def test_storage_is_not_misreported_as_runnable_memory() -> None:
    result = plan_local_runtime(
        {"parameter_count": 300_000_000_000, "context_tokens": 8192},
        _hardware(ram=16, available=14, disk=1000),
    )

    assert result["recommendation"] is None
    assert result["runnable_candidate_count"] == 0
    assert all(not item["runnable"] for item in result["candidates"])
    assert any(item["strategy"] == "storage_paging_required" for item in result["candidates"])


@pytest.mark.parametrize(
    "payload",
    [
        {"parameter_count": 0},
        {"context_tokens": 128},
        {"priority": "magic"},
    ],
)
def test_planner_rejects_invalid_contracts(payload) -> None:
    with pytest.raises(ValueError):
        plan_local_runtime(payload, _hardware())
