from __future__ import annotations

from app.backend.scientific_recovery import scientific_recovery_state


def test_packaged_scientific_recovery_state_is_read_only_and_truthful() -> None:
    payload = scientific_recovery_state()

    assert payload["state"] == "accepted"
    assert payload["read_only"] is True
    assert payload["production_activation"] is False
    assert payload["classification"] == "PIPELINE ACCEPTED -- QUALITY TRAINING STILL REQUIRED"
    assert payload["data"]["legacy_malformed_v1"]["training_selectable"] is False
    assert payload["data"]["corrected_oasst2"]["accepted_target_windows"] == 7037
    assert payload["bounded_experiment"]["corrected_dataset_passes"] < (
        payload["bounded_experiment"]["maximum_corrected_dataset_passes"]
    )
    assert payload["bounded_experiment"]["activated"] is False
    assert payload["recommended_next_stage"]["started"] is False

    payload["comparison"][0]["label"] = "mutated"
    assert scientific_recovery_state()["comparison"][0]["label"] == "Pre-OASST2 parent"
