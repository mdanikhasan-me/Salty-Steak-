from __future__ import annotations

from pathlib import Path


def test_filesystem_model_bundle_never_advertises_checkpoint_activation() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "backend"
        / "runtime"
        / "model_bundles.py"
    ).read_text(encoding="utf-8")

    assert '"use_in_chat": {' in source
    assert '"enabled": False' in source
    assert "model-bundle cutover workflow" in source
