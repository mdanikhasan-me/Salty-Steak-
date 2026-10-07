"""Checksum-bound model acceptance probe for the app-owned private worker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backend.runtime.salty_native import SaltyNativeProfile
from app.backend.runtime.salty_native_worker import SaltyNativeWorkerRuntime


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    # A probe must be bound to an explicit artifact identity.  Defaults here
    # previously pointed at a removed 27B model and made a stale command look
    # like a current acceptance test.
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--library-directory", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=float, default=240.0)
    parser.add_argument("--gpu-layers", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--micro-batch-size", type=int, default=128)
    parser.add_argument("--context-window-tokens", type=int, default=7_680)
    parser.add_argument("--maximum-output-tokens", type=int, default=96)
    parser.add_argument(
        "--response-format",
        choices=("text", "json"),
        default="text",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "In two concise sentences, explain why running an AI model "
            "locally can improve privacy."
        ),
    )
    args = parser.parse_args()
    runtime = SaltyNativeWorkerRuntime(
        model_path=args.model_path.resolve(),
        library_directory=args.library_directory.resolve(),
        source_sha256=args.source_sha256,
        profile=SaltyNativeProfile(
            profile_id=(
                f"probe_ngl{args.gpu_layers}_b{args.batch_size}_"
                f"ub{args.micro_batch_size}_ctx{args.context_window_tokens}"
            ),
            gpu_layers=args.gpu_layers,
            context_limit=args.context_window_tokens,
            batch_size=args.batch_size,
            micro_batch_size=args.micro_batch_size,
        ),
    )
    payload: dict = {
        "schema_version": 1,
        "success": False,
        "model_path": str(args.model_path.resolve()),
        "source_sha256": args.source_sha256,
        "profile": runtime.profile.__dict__,
        "started_at_unix": time.time(),
        "controller_pid": os.getpid(),
    }
    worker_pid = None
    try:
        warm_started = time.monotonic()
        identity = runtime.warmup()
        worker_pid = identity.get("worker_pid")
        payload["warmup"] = {
            "wall_seconds": time.monotonic() - warm_started,
            "identity": identity,
        }
        generation_started = time.monotonic()
        response = runtime.generate(
            messages=[{"role": "user", "content": args.prompt}],
            maximum_output_tokens=args.maximum_output_tokens,
            temperature=0.0,
            top_p=1.0,
            top_k=1,
            repetition_penalty=1.0,
            seed=20260810,
            context_window_tokens=args.context_window_tokens,
            reserved_output_tokens=args.maximum_output_tokens,
            response_format=args.response_format,
            should_stop=lambda: (
                time.monotonic() - generation_started > args.deadline_seconds
            ),
        )
        payload["generation"] = {
            "wall_seconds": time.monotonic() - generation_started,
            "text": response.text,
            "token_ids": response.token_ids,
            "finish_reason": response.finish_reason,
            "cancelled": response.cancelled,
            "technical_details": response.technical_details,
        }
        json_valid = None
        if args.response_format == "json":
            try:
                json.loads(response.text)
                json_valid = True
            except (TypeError, json.JSONDecodeError):
                json_valid = False
        payload["generation"]["response_format"] = args.response_format
        payload["generation"]["json_valid"] = json_valid
        payload["success"] = (
            bool(response.token_ids)
            and not response.cancelled
            and json_valid is not False
        )
    except BaseException as error:
        payload["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        unload_started = time.monotonic()
        runtime.unload()
        payload["unload_seconds"] = time.monotonic() - unload_started
        if worker_pid:
            deadline = time.monotonic() + 10
            while psutil.pid_exists(int(worker_pid)) and time.monotonic() < deadline:
                time.sleep(0.1)
        payload["worker_pid"] = worker_pid
        payload["worker_survived_unload"] = bool(
            worker_pid and psutil.pid_exists(int(worker_pid))
        )
        payload["success"] = bool(payload["success"]) and not payload["worker_survived_unload"]
        payload["finished_at_unix"] = time.time()
        payload["wall_seconds"] = payload["finished_at_unix"] - payload["started_at_unix"]
        atomic_json(args.output.resolve(), payload)
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
