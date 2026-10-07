from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pytest

from app.backend.training.identity_promotion import promote_identity_candidate


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_identity_promotion_is_hash_bound_and_preserves_rollback(tmp_path: Path) -> None:
    library = tmp_path / "models"
    bundle_root = library / "text-generation" / "base-steak"
    bundle_root.mkdir(parents=True)
    model = bundle_root / "Base-Steak.gguf"
    model.write_bytes(b"model")
    old_adapter = bundle_root / "Base-Steak-2.0-Identity-LoRA-v1-F32.gguf"
    old_adapter.write_bytes(b"old-adapter")
    manifest_path = bundle_root / "model.json"
    manifest_path.write_text(
        json.dumps(
            {
                "display_name": "Base Steak 2.0",
                "architecture": "steak20",
                "artifact": {"filename": model.name, "sha256": _sha(b"model")},
                "companion_artifacts": [
                    {
                        "id": "identity",
                        "role": "text_adapter",
                        "filename": old_adapter.name,
                    }
                ],
                "identity": {},
            }
        ),
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate.gguf"
    candidate.write_bytes(b"new-adapter")
    result = {
        "passed": True,
        "gates": {"identity": True, "retention": True},
        "model_sha256": _sha(b"model"),
        "adapter": {
            "path": str(candidate),
            "sha256": _sha(b"new-adapter"),
            "size_bytes": len(b"new-adapter"),
        },
        "adapter_scale": 0.5,
        "training_report_sha256": "a" * 64,
        "evaluation_report_sha256": "b" * 64,
        "metrics": {
            "identity_count": 25,
            "identity_pass_count": 25,
            "retention_count": 20,
            "retention_exact_baseline_count": 20,
        },
    }
    bundle = {
        "technical_details": {"manifest_path": str(manifest_path)},
    }

    promoted = promote_identity_candidate(
        bundle=bundle,
        workflow_result=result,
        model_library_root=library,
        backup_root=tmp_path / "rollback",
        operation_id="00000000-0000-0000-0000-000000000001",
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert old_adapter.read_bytes() == b"new-adapter"
    assert manifest["companion_artifacts"][0]["sha256"] == _sha(b"new-adapter")
    assert manifest["companion_artifacts"][0]["scale"] == 0.5
    assert manifest["companion_artifacts"][0]["activation"] == "identity_intent"
    assert manifest["identity"]["trainer"] == "MD Anik Hasan (Sawlper)"
    assert manifest["identity"]["identity_adapter_activation"] == "identity_intent"
    rollback = Path(promoted["rollback_directory"])
    assert (rollback / old_adapter.name).read_bytes() == b"old-adapter"
    assert (rollback / "model.json").is_file()


def test_identity_promotion_rejects_any_failed_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="every recorded gate"):
        promote_identity_candidate(
            bundle={},
            workflow_result={"passed": True, "gates": {"identity": False}},
            model_library_root=tmp_path,
            backup_root=tmp_path / "rollback",
            operation_id="operation",
        )
