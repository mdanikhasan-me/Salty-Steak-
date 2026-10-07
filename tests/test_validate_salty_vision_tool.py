from pathlib import Path


def test_validator_supports_exact_isolated_workspace_stage_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "validate_salty_vision_candidate.py"
    ).read_text(encoding="utf-8")

    assert '"--workspace-root"' in source
    assert '"--output"' in source
    assert 'expected_stage = (workspace_root / "runtime/salty-vision").resolve()' in source
    assert "if stage != expected_stage" in source
    assert "if stage.exists()" in source
    assert "Refusing to overwrite" in source
    assert "VISION_LICENSE_FILE" in source
    assert "VISION_RUNTIME_SHA256" in source
    assert "staged_broker.verify_staged_runtime()" in source
    assert "--output must remain inside the project root" in source
