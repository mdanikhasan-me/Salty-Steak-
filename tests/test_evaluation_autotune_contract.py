from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tomllib

import pytest


_PERFORMANCE_EVIDENCE = (
    Path(__file__).resolve().parents[1] / "validation" / "performance-r4"
)
pytestmark = pytest.mark.skipif(
    not (_PERFORMANCE_EVIDENCE / "evaluation_harness.py").is_file(),
    reason="Historical performance-r4 evidence was intentionally retired.",
)


def _module():
    path = (
        Path(__file__).resolve().parents[1]
        / "validation"
        / "performance-r4"
        / "evaluation_harness.py"
    )
    spec = importlib.util.spec_from_file_location("evaluation_harness_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through sys.modules when a
    # module is loaded from a file rather than imported normally.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_candidate_matrix_is_the_requested_safe_sequence() -> None:
    module = _module()
    assert [candidate.batch_size for candidate in module.candidate_matrix()] == [
        1,
        2,
        4,
        8,
        16,
        32,
    ]


def test_autotune_rejects_metric_mismatch_and_selects_fastest_equivalent() -> None:
    module = _module()
    candidates = [
        {
            "name": f"eval_batch{size}",
            "status": "completed",
            "configuration": {"batch_size": size},
            "records_evaluated": 10,
            "tokens_evaluated": 100,
            "mean_loss": 2.0 if size != 4 else 2.2,
            "tokens_per_second": float(size),
            "finite_and_stable": True,
        }
        for size in (1, 2, 4, 8)
    ]
    selected = module.compare_candidates(candidates, tolerance=1e-6)
    assert selected["selected_batch_size"] == 8
    assert "eval_batch4" not in selected["accepted_candidates"]
    assert all(
        item["accepted"] is (item["configuration"]["batch_size"] != 4)
        for item in candidates
    )


def test_autotune_requires_exact_record_identity_perplexity_and_tokens() -> None:
    module = _module()
    baseline = {
        "status": "completed",
        "records_evaluated": 12,
        "tokens_evaluated": 600,
        "record_ids_digest": "a" * 64,
        "mean_loss": 2.0,
        "perplexity": 7.3890560989,
        "tokens_per_second": 100.0,
        "finite_and_stable": True,
        "repetition_coverage_exact": True,
        "repetitions": 3,
        "throughput_coefficient_of_variation": 0.01,
        "vram_headroom_safe": True,
        "shared_memory_telemetry_available": True,
        "shared_memory_spill_detected": False,
    }
    candidates = [
        {
            **baseline,
            "name": "eval_batch1",
            "configuration": {"batch_size": 1},
        },
        {
            **baseline,
            "name": "eval_batch2",
            "configuration": {"batch_size": 2},
            "tokens_per_second": 200.0,
            "record_ids_digest": "b" * 64,
        },
        {
            **baseline,
            "name": "eval_batch4",
            "configuration": {"batch_size": 4},
            "tokens_per_second": 300.0,
            "perplexity": 7.5,
        },
        {
            **baseline,
            "name": "eval_batch8",
            "configuration": {"batch_size": 8},
            "tokens_per_second": 250.0,
        },
    ]

    selected = module.compare_candidates(
        candidates,
        tolerance=1e-6,
        perplexity_tolerance=1e-6,
        minimum_repetitions=3,
        require_resource_evidence=True,
    )

    assert selected["selected_batch_size"] == 8
    assert candidates[1]["accepted"] is False
    assert "record IDs differ" in candidates[1]["decision_reason"]
    assert candidates[2]["accepted"] is False
    assert "perplexity exceeds tolerance" in candidates[2]["decision_reason"]


def test_autotune_rejects_shared_memory_spill_unstable_gain_and_low_headroom() -> None:
    module = _module()
    common = {
        "status": "completed",
        "records_evaluated": 10,
        "tokens_evaluated": 100,
        "record_ids_digest": "c" * 64,
        "mean_loss": 1.5,
        "perplexity": 4.4816890703,
        "finite_and_stable": True,
        "repetition_coverage_exact": True,
        "repetitions": 3,
        "shared_memory_telemetry_available": True,
    }
    candidates = [
        {
            **common,
            "name": "eval_batch1",
            "configuration": {"batch_size": 1},
            "tokens_per_second": 10.0,
            "throughput_coefficient_of_variation": 0.01,
            "shared_memory_spill_detected": False,
            "vram_headroom_safe": True,
        },
        {
            **common,
            "name": "eval_batch2",
            "configuration": {"batch_size": 2},
            "tokens_per_second": 20.0,
            "throughput_coefficient_of_variation": 0.01,
            "shared_memory_spill_detected": True,
            "vram_headroom_safe": True,
        },
        {
            **common,
            "name": "eval_batch4",
            "configuration": {"batch_size": 4},
            "tokens_per_second": 30.0,
            "throughput_coefficient_of_variation": 0.20,
            "shared_memory_spill_detected": False,
            "vram_headroom_safe": True,
        },
        {
            **common,
            "name": "eval_batch8",
            "configuration": {"batch_size": 8},
            "tokens_per_second": 40.0,
            "throughput_coefficient_of_variation": 0.01,
            "shared_memory_spill_detected": False,
            "vram_headroom_safe": False,
        },
    ]

    selected = module.compare_candidates(
        candidates,
        tolerance=1e-6,
        perplexity_tolerance=1e-6,
        minimum_repetitions=3,
        maximum_throughput_cv=0.08,
        require_resource_evidence=True,
    )

    assert selected["selected_batch_size"] == 1
    assert "shared GPU memory spill detected" in candidates[1]["decision_reason"]
    assert "not repeatably stable" in candidates[2]["decision_reason"]
    assert "VRAM headroom is unsafe" in candidates[3]["decision_reason"]


def test_accepted_full_split_evidence_matches_the_configured_batch() -> None:
    root = Path(__file__).resolve().parents[1]
    report = json.loads(
        (
            root
            / "validation"
            / "performance-r4"
            / "evaluation-autotune-r4.json"
        ).read_text(encoding="utf-8")
    )
    with (root / "config" / "defaults.toml").open("rb") as handle:
        configured_batch = int(
            tomllib.load(handle)["evaluation"]["batch_size"]
        )

    assert report["completed"] is True
    assert report["production_database_used"] is False
    assert report["all_benchmark_inputs_within_disposable_root"] is True
    assert configured_batch == report["recommended_batch_size"] == 32
    finalists = report["finalist_candidates"]
    selected = next(
        item
        for item in finalists
        if item["configuration"]["batch_size"] == configured_batch
    )
    previous = next(
        item
        for item in finalists
        if item["configuration"]["batch_size"]
        == report["current_application_batch_size"]
    )
    assert selected["repetitions"] >= 3
    assert selected["repetition_coverage_exact"] is True
    assert selected["shared_memory_telemetry_available"] is True
    assert selected["shared_memory_spill_detected"] is False
    assert selected["vram_headroom_safe"] is True
    assert selected["tokens_per_second"] > previous["tokens_per_second"]
    assert (
        selected["median_total_duration_seconds"]
        < previous["median_total_duration_seconds"]
    )
    assert len(
        {item["record_ids_digest"] for item in finalists}
    ) == 1
    assert len({item["records_evaluated"] for item in finalists}) == 1
    assert len({item["tokens_evaluated"] for item in finalists}) == 1
