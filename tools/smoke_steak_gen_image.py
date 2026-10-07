"""Run one checksum-bound, offline Steak Gen image smoke and save evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    workspace = Path(args.workspace).resolve()
    output = Path(args.output).resolve()
    evidence_path = Path(args.evidence).resolve()
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from app.backend.runtime.steak_gen import (
        SteakGenRequest,
        SteakGenWorkerClient,
    )

    client = SteakGenWorkerClient(
        project_root=project_root,
        workspace_root=workspace,
        python_executable=sys.executable,
    )
    request = SteakGenRequest(
        prompt=args.prompt,
        output_path=str(output),
        width=args.width,
        height=args.height,
        steps=args.steps,
        guidance_scale=0.0,
        seed=args.seed,
        max_sequence_length=512,
        text_runtime_unloaded=True,
        verify_hashes=True,
    )
    started_at = _utc_now()
    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "schema": "salty-steak-steak-gen-smoke-v1",
        "success": False,
        "started_at": started_at,
        "project_root": str(project_root),
        "workspace": str(workspace),
        "request": {
            "prompt": args.prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "guidance_scale": 0.0,
            "seed": args.seed,
            "max_sequence_length": 512,
        },
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "offline_environment": {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
            },
            "no_external_service": True,
        },
        "events": events,
    }
    hardware_samples: list[dict[str, Any]] = []
    sampling_stop = threading.Event()

    def sample_hardware() -> None:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        while not sampling_stop.is_set():
            try:
                completed = subprocess.run(
                    [
                        executable,
                        "--query-gpu=timestamp,utilization.gpu,utilization.memory,"
                        "memory.used,memory.total,power.draw,temperature.gpu",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    creationflags=creation_flags,
                )
                values = [value.strip() for value in completed.stdout.split(",")]
                if completed.returncode == 0 and len(values) == 7:
                    hardware_samples.append(
                        {
                            "elapsed_seconds": round(
                                time.perf_counter() - started, 6
                            ),
                            "timestamp": values[0],
                            "gpu_utilization_percent": float(values[1]),
                            "memory_utilization_percent": float(values[2]),
                            "memory_used_mib": float(values[3]),
                            "memory_total_mib": float(values[4]),
                            "power_watts": float(values[5]),
                            "temperature_c": float(values[6]),
                        }
                    )
            except (OSError, subprocess.SubprocessError, TypeError, ValueError):
                pass
            sampling_stop.wait(2.0)

    sampling_thread = threading.Thread(
        target=sample_hardware,
        name="steak-gen-hardware-sampler",
        daemon=True,
    )
    sampling_thread.start()

    def on_event(event: dict[str, Any]) -> None:
        record = dict(event)
        record["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        events.append(record)

    try:
        result = client.generate(
            request,
            on_event=on_event,
            timeout_seconds=args.timeout_seconds,
        )
        from PIL import Image

        with Image.open(output) as image:
            image.verify()
        with Image.open(output) as image:
            actual_dimensions = [int(image.width), int(image.height)]
            actual_format = str(image.format)
        actual_sha256 = _sha256(output)
        bundle_event = next(
            (
                dict(event)
                for event in events
                if event.get("phase") == "bundle_verified"
            ),
            {},
        )
        evidence["bundle_verification"] = bundle_event
        checks = {
            "output_exists": output.is_file(),
            "output_is_png": actual_format == "PNG",
            "dimensions_match": actual_dimensions == [args.width, args.height],
            "result_hash_matches": actual_sha256 == result.output_sha256,
            "model_id_matches": result.model_id == "steak-gen-1-scaledfp8",
            "worker_completed": bool(events and events[-1].get("phase") == "complete"),
        }
        evidence.update(
            {
                "success": all(checks.values()),
                "result": {
                    **result.__dict__,
                    "output_size_bytes": output.stat().st_size,
                    "actual_sha256": actual_sha256,
                    "actual_dimensions": actual_dimensions,
                    "actual_format": actual_format,
                },
                "checks": checks,
            }
        )
    except BaseException as error:
        evidence["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
    finally:
        sampling_stop.set()
        sampling_thread.join(timeout=6)
        evidence["hardware_samples"] = hardware_samples
        evidence["finished_at"] = _utc_now()
        evidence["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        _write_json(evidence_path, evidence)
    return 0 if evidence.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
