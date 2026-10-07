"""Salty Steak Native Desktop AI Platform — legacy vision staging validator.

Superseded by ``tools/stage_vision_runtime.py``, which stages the current
Salty Multi-Modal Vision build and writes its manifest from an observed smoke.
This script is retained for its stricter workspace-boundary checks.
"""

from __future__ import annotations

import json
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backend.runtime.salty_vision import (
    VISION_LICENSE_FILE,
    VISION_RUNTIME_ID,
    VISION_RUNTIME_SHA256,
    VISION_SMOKE_IMAGE_SHA256,
    VISION_SMOKE_OUTPUT_SHA256,
    VISION_STAGE_MANIFEST,
    VISION_STAGE_SCHEMA,
    VISION_VALIDATOR_VERSION,
    SaltyVisionBroker,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_evidence(path: Path, payload: dict) -> None:
    """Write only project-contained evidence with an atomic replacement."""

    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("--output must remain inside the project root") from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, resolved)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-bin",
        type=Path,
        default=(
            PROJECT_ROOT
            / "workspace/cache/native-engine-build/salty-vision-steak20-b10333-candidate1/bin"
        ),
    )
    parser.add_argument(
        "--stage-runtime",
        type=Path,
        help=(
            "After a passing real smoke, copy the exact runtime, license, and "
            "durable gate manifest here. Must be workspace/runtime/salty-vision."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=PROJECT_ROOT / "workspace",
        help=(
            "Workspace whose exact runtime/salty-vision child may be staged. "
            "Use an isolated clone workspace before release cutover."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional project-contained JSON evidence destination.",
    )
    args = parser.parse_args()
    project = PROJECT_ROOT
    bundle = project / "workspace/models/text-generation/base-steak-2-0-9b-steak20-candidate1"
    model_manifest = json.loads((bundle / "model.json").read_text(encoding="utf-8"))
    text_adapters = [
        {
            "id": companion["id"],
            "path": str((bundle / companion["filename"]).resolve()),
            "sha256": companion["sha256"],
            "scale": float(companion.get("scale", 1.0)),
        }
        for companion in model_manifest.get("companion_artifacts", [])
        if companion.get("role") == "text_adapter"
        and companion.get("activation", "always") == "always"
    ]
    broker = SaltyVisionBroker(
        text_model_path=bundle / "Base-Steak-2.0-9B-Steak20-Q5_K_M.gguf",
        projector_path=bundle / "Base-Steak-2.0-Vision-Projector-BF16.gguf",
        text_adapters=text_adapters,
        runtime_directory=args.candidate_bin,
        temporary_root=project / "workspace/cache/vision-smoke-temp",
        timeout_seconds=900,
        device="CUDA0",
        gpu_layers=24,
        mmproj_offload=False,
    )
    broker.verify_integrity()
    result = broker.run_deterministic_smoke()
    payload = {
        "result": asdict(result),
        "status": broker.status(),
    }
    if (
        result.image_sha256 != VISION_SMOKE_IMAGE_SHA256
        or hashlib.sha256(result.text.encode("utf-8")).hexdigest()
        != VISION_SMOKE_OUTPUT_SHA256
        or result.command_exit_code != 0
        or result.runtime_files_sha256 != VISION_RUNTIME_SHA256
    ):
        raise RuntimeError("The deterministic vision gate output or identity differs")
    if args.stage_runtime is not None:
        stage = args.stage_runtime.resolve()
        workspace_root = args.workspace_root.resolve()
        expected_stage = (workspace_root / "runtime/salty-vision").resolve()
        if stage != expected_stage:
            raise ValueError(
                "--stage-runtime must be exactly <workspace-root>/runtime/salty-vision"
            )
        if workspace_root.anchor == str(workspace_root):
            raise ValueError("--workspace-root cannot be a filesystem root")
        if stage.exists():
            raise FileExistsError(
                "Refusing to overwrite an existing staged vision runtime"
            )
        stage.mkdir(parents=True, exist_ok=False)
        try:
            for name, expected_hash in VISION_RUNTIME_SHA256.items():
                source = (args.candidate_bin / name).resolve(strict=True)
                if _sha256(source) != expected_hash:
                    raise RuntimeError(f"Vision candidate identity differs for {name}")
                shutil.copy2(source, stage / name)
            license_source = (
                project / "workspace/runtime/salty-native-steak20/THIRD_PARTY_LICENSE.txt"
            ).resolve(strict=True)
            shutil.copy2(license_source, stage / VISION_LICENSE_FILE)
            manifest = {
                "schema": VISION_STAGE_SCHEMA,
                "runtime_id": VISION_RUNTIME_ID,
                "validator_version": VISION_VALIDATOR_VERSION,
                "validated_at_utc": datetime.now(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
                "text_model_sha256": result.text_model_sha256,
                "projector_sha256": result.projector_sha256,
                "text_adapters": broker._adapter_manifest_records(),
                "runtime_files_sha256": dict(VISION_RUNTIME_SHA256),
                "third_party_license": {
                    "filename": VISION_LICENSE_FILE,
                    "sha256": _sha256(stage / VISION_LICENSE_FILE),
                    "source": "salty_native_runtime_component_notice",
                },
                "runtime_profile": {
                    "device": "CUDA0",
                    "gpu_layers": 24,
                    "mmproj_offload": False,
                    "context_tokens": 4096,
                    "batch_size": 512,
                    "micro_batch_size": 256,
                },
                "deterministic_smoke": {
                    "passed": True,
                    "request_id": "base-steak-2-0-vision-smoke-v1",
                    "image_sha256": VISION_SMOKE_IMAGE_SHA256,
                    "output_sha256": VISION_SMOKE_OUTPUT_SHA256,
                    "command_exit_code": 0,
                    "validated_at_utc": datetime.now(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
            }
            (stage / VISION_STAGE_MANIFEST).write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            staged_broker = SaltyVisionBroker(
                text_model_path=broker.text_model_path,
                projector_path=broker.projector_path,
                text_adapters=text_adapters,
                runtime_directory=stage,
                temporary_root=project / "workspace/cache/vision-stage-check-temp",
                timeout_seconds=900,
                device="CUDA0",
                gpu_layers=24,
                mmproj_offload=False,
                require_staged_manifest=True,
            )
            staged_status = staged_broker.verify_staged_runtime()
            if not staged_status["application_available"]:
                raise RuntimeError("Staged vision runtime did not pass its durable gate")
            payload["staged_status"] = staged_status
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    payload["success"] = bool(broker.status()["candidate_ready"])
    if args.stage_runtime is not None:
        payload["success"] = bool(
            payload["success"] and payload.get("staged_status", {}).get("application_available")
        )
    if args.output is not None:
        _write_evidence(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
