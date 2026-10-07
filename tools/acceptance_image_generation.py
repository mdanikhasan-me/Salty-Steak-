"""Validate the complete Chat -> Steak Gen -> Chat lifecycle."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.gpu_validation_guard import gpu_validation_preflight  # noqa: E402


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_report(path: Path, value: dict) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-root", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--guard-workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expected-build-id", required=True)
    parser.add_argument("--resolution", type=int, default=512)
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--maximum-seconds", type=float, default=180.0)
    arguments = parser.parse_args()

    root = arguments.application_root.resolve()
    workspace = arguments.workspace.resolve()
    report_path = arguments.report.resolve()
    preflight = gpu_validation_preflight(arguments.guard_workspace)
    if not preflight["idle"]:
        report = {
            "schema": "salty-steak-image-generation-acceptance-v1",
            "passed": False,
            "blocked": True,
            "gpu_preflight": preflight,
        }
        write_report(report_path, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    os.environ["SALTY_POTATO_BUILD_ID"] = arguments.expected_build_id
    prior_path = list(sys.path)
    sys.path = [str(root), *[value for value in sys.path if Path(value or ".").resolve() != PROJECT_ROOT]]
    for key in tuple(sys.modules):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]
    from app.backend.application import Application
    from app.backend.runtime.steak_gen import PUBLIC_MODEL_ID

    started_at = utc_now()
    started = time.perf_counter()
    app = Application(root, recover_operations=False)
    report: dict = {
        "schema": "salty-steak-image-generation-acceptance-v1",
        "application_root": str(root),
        "workspace": str(workspace),
        "expected_build_id": arguments.expected_build_id,
        "started_at": started_at,
        "gpu_preflight": preflight,
        "request": {
            "resolution": arguments.resolution,
            "aspect_ratio": arguments.aspect_ratio,
            "steps": arguments.steps,
        },
        "passed": False,
    }
    try:
        conversation = app.create_conversation()
        operation = app.chat.start_message(
            conversation["id"],
            "Generate an image of a copper lighthouse at blue hour.",
            {
                "reasoning_mode": "instant",
                "context_window_tokens": 32_768,
                "maximum_output_mode": "manual",
                "maximum_output_tokens": 512,
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 40,
                "repetition_penalty": 1.05,
                "seed": 20260825,
                "image_mode": True,
                "image_model_id": PUBLIC_MODEL_ID,
                "image_resolution": arguments.resolution,
                "image_aspect_ratio": arguments.aspect_ratio,
                "image_steps": arguments.steps,
            },
        )
        chat = app.operations.wait(operation["id"], timeout=300)
        image_id = str((chat.get("result") or {}).get("image_operation_id") or "")
        if not image_id:
            raise RuntimeError("The Chat turn did not start its image operation")
        image = app.operations.wait(image_id, timeout=1_800)
        image_visible_elapsed = time.perf_counter() - started
        rewarm_completed = app.chat.wait_for_deferred_rewarm(timeout=120)
        full_recovery_elapsed = time.perf_counter() - started
        saved = app.get_conversation(conversation["id"])
        assistant = next(
            message
            for message in reversed(saved.get("messages") or [])
            if message.get("role") == "assistant"
        )
        details = dict(assistant.get("technical_details") or {})
        artifact = dict(details.get("generated_image") or {})
        artifact_id = str(artifact.get("id") or "")
        delivered = app.image_artifact_content(artifact_id)
        breakdown = dict(details.get("turn_duration_breakdown") or {})
        result = dict(image.get("result") or {})
        transition = dict(
            details.get("text_runtime_transition")
            or result.get("text_runtime_transition")
            or {}
        )
        gates = {
            "chat_completed": chat.get("state") == "completed",
            "image_completed": image.get("state") == "completed",
            "artifact_registered": bool(artifact_id),
            "artifact_exists": delivered.path.is_file(),
            "artifact_hash_matches": sha256(delivered.path) == delivered.sha256,
            "dimensions_match": (
                int(artifact.get("width") or 0),
                int(artifact.get("height") or 0),
            )
            == (
                arguments.resolution,
                arguments.resolution,
            )
            if arguments.aspect_ratio == "1:1"
            else bool(artifact.get("width") and artifact.get("height")),
            "model_matches": str((result.get("artifact") or {}).get("model_name") or "").startswith("Steak Gen"),
            "text_unloaded": transition.get("text_runtime_unloaded") is True,
            "text_rewarm_scheduled": transition.get("text_runtime_rewarm_scheduled") is True,
            "text_rewarm_completed": rewarm_completed,
            "text_rewarm_ready": transition.get("text_runtime_rewarm_ready") is True,
            "end_to_end_timing_recorded": (
                int(breakdown.get("total_ms") or 0)
                >= int(breakdown.get("image_operation_ms") or 0)
                > 0
            ),
            "within_performance_ceiling": image_visible_elapsed <= arguments.maximum_seconds,
        }
        report.update(
            {
                "finished_at": utc_now(),
                "elapsed_seconds": round(image_visible_elapsed, 6),
                "image_visible_elapsed_seconds": round(image_visible_elapsed, 6),
                "full_recovery_elapsed_seconds": round(full_recovery_elapsed, 6),
                "chat_operation_id": operation["id"],
                "image_operation_id": image_id,
                "artifact": artifact,
                "artifact_path": str(delivered.path),
                "turn_duration_breakdown": breakdown,
                "text_runtime_transition": transition,
                "gates": gates,
                "passed": all(gates.values()),
            }
        )
    except BaseException as error:
        report.update(
            {
                "finished_at": utc_now(),
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        )
    finally:
        app.close()
        sys.path = prior_path

    write_report(report_path, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
