from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stale_in_process_27b_smoke_is_retired() -> None:
    assert not (ROOT / "tools" / "smoke_saltnative_in_process.py").exists()


def test_worker_smoke_requires_explicit_model_runtime_and_checksum() -> None:
    source = (ROOT / "tools" / "smoke_saltnative_worker.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--model-path", type=Path, required=True)' in source
    assert 'parser.add_argument("--source-sha256", required=True)' in source
    assert 'parser.add_argument("--library-directory", type=Path, required=True)' in source
    assert "General-Steak-27B.gguf" not in source


def test_generated_cleanup_discovers_registered_model_artifacts_not_a_retired_path() -> None:
    source = (ROOT / "tools" / "clean_generated_artifacts.ps1").read_text(encoding="utf-8")

    assert "$registeredModelArtifacts" in source
    assert '"model.json"' in source
    assert "General-Steak-27B.gguf" not in source
