"""Administrator-side worker for one audited terminal invocation.

This file intentionally depends only on the Python standard library.  The
desktop broker starts it through Windows ``runas`` after hashing both this
source file and the exact request.  The worker never accepts a shell string: it
executes the supplied argv directly, captures bounded output, enforces the
deadline, and writes one nonce-bound result for the non-elevated broker.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO


PROTOCOL = "salty-steak-elevated-terminal-v1"
MAX_REQUEST_BYTES = 128 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARACTERS = 32_768
MAX_OUTPUT_BYTES = 1024 * 1024
POLL_SECONDS = 0.05


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_request(path: Path, expected_sha256: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("Elevated terminal request size is invalid")
    if _sha256(raw) != expected_sha256:
        raise PermissionError("Elevated terminal request integrity check failed")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("Elevated terminal request protocol is invalid")
    return value


def _validate_request(value: dict[str, Any]) -> dict[str, Any]:
    accepted = {
        "protocol",
        "nonce",
        "argv",
        "working_directory",
        "timeout_seconds",
        "max_output_bytes",
        "result_path",
        "cancel_path",
    }
    unknown = sorted(str(key) for key in value if key not in accepted)
    if unknown:
        raise ValueError(f"Unknown elevated terminal request fields: {unknown}")
    nonce = str(value.get("nonce") or "")
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        raise ValueError("Elevated terminal nonce is invalid")
    argv = value.get("argv")
    if (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= MAX_ARGUMENTS
        or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
        or sum(len(item) for item in argv) > MAX_ARGUMENT_CHARACTERS
    ):
        raise ValueError("Elevated terminal argv is invalid")
    working_directory = Path(str(value.get("working_directory") or "")).resolve(strict=True)
    if not working_directory.is_dir():
        raise ValueError("Elevated terminal working directory is invalid")
    timeout_seconds = float(value.get("timeout_seconds"))
    if not 0.05 <= timeout_seconds <= 14_400:
        raise ValueError("Elevated terminal timeout is invalid")
    max_output_bytes = int(value.get("max_output_bytes"))
    if not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError("Elevated terminal output limit is invalid")
    result_path = Path(str(value.get("result_path") or ""))
    cancel_path = Path(str(value.get("cancel_path") or ""))
    if not result_path.is_absolute() or not cancel_path.is_absolute():
        raise ValueError("Elevated terminal coordination paths must be absolute")
    return {
        "nonce": nonce,
        "argv": list(argv),
        "working_directory": working_directory,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "result_path": result_path.resolve(),
        "cancel_path": cancel_path.resolve(),
    }


def _drain(stream: BinaryIO, limit: int, target: dict[str, Any]) -> None:
    retained = bytearray()
    total = 0
    digest = hashlib.sha256()
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
        payload = bytes(retained)
        target.update(
            {
                "text": payload.decode("utf-8", errors="replace"),
                "retained_base64": base64.b64encode(payload).decode("ascii"),
                "encoding": "utf-8_with_replacement",
                "bytes_total": total,
                "bytes_retained": len(payload),
                "truncated": total > len(payload),
                "sha256": digest.hexdigest(),
            }
        )
    except BaseException as exc:
        target["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stream.close()


def _empty_output() -> dict[str, Any]:
    return {
        "text": "",
        "retained_base64": "",
        "encoding": "utf-8_with_replacement",
        "bytes_total": 0,
        "bytes_retained": 0,
        "truncated": False,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }


class _WindowsJob:
    """Kill the whole elevated command tree when the worker exits or cancels."""

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> _WindowsJob | None:
        if os.name != "nt" or not hasattr(process, "_handle"):
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        configured = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        )
        assigned = kernel32.AssignProcessToJobObject(
            handle, ctypes.c_void_p(int(process._handle))
        )
        if not configured or not assigned:
            kernel32.CloseHandle(handle)
            return None
        return cls(int(handle))

    def terminate(self) -> None:
        if self.handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1)

    def close(self) -> None:
        if self.handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = 0


def _is_administrator() -> bool:
    try:
        return bool(ctypes.WinDLL("shell32", use_last_error=True).IsUserAnAdmin())
    except OSError:
        return False


def _run(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    process = subprocess.Popen(
        request["argv"],
        cwd=str(request["working_directory"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=creationflags,
        env=environment,
    )
    job = _WindowsJob.attach(process)
    stdout: dict[str, Any] = {}
    stderr: dict[str, Any] = {}
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_thread = threading.Thread(
        target=_drain,
        args=(process.stdout, request["max_output_bytes"], stdout),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(process.stderr, request["max_output_bytes"], stderr),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    status = "running"
    deadline = started + request["timeout_seconds"]
    try:
        while process.poll() is None:
            if request["cancel_path"].exists():
                status = "revoked"
                break
            if time.monotonic() >= deadline:
                status = "timed_out"
                break
            time.sleep(POLL_SECONDS)
        if status != "running":
            if job is not None:
                job.terminate()
            else:
                process.kill()
        try:
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            exit_code = process.wait(timeout=5)
        if status == "running":
            status = "succeeded" if exit_code == 0 else "failed"
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if job is not None:
            job.close()
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise RuntimeError("Elevated terminal output pipes did not close")
    if stdout.get("error") or stderr.get("error"):
        raise RuntimeError(str(stdout.get("error") or stderr.get("error")))
    return {
        "protocol": PROTOCOL,
        "nonce": request["nonce"],
        "status": status,
        "exit_code": exit_code,
        "timed_out": status == "timed_out",
        "process_elevated": _is_administrator(),
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "stdout": stdout or _empty_output(),
        "stderr": stderr or _empty_output(),
    }


def _write_result(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    request: dict[str, Any] | None = None
    result_path: Path | None = None
    nonce = ""
    try:
        own_bytes = Path(__file__).resolve().read_bytes()
        if _sha256(own_bytes) != args.worker_sha256:
            raise PermissionError("Elevated terminal worker integrity check failed")
        loaded = _read_request(Path(args.request).resolve(strict=True), args.request_sha256)
        request = loaded
        result_path = Path(str(loaded.get("result_path") or ""))
        nonce = str(loaded.get("nonce") or "")
        validated = _validate_request(loaded)
        result_path = validated["result_path"]
        nonce = validated["nonce"]
        result = _run(validated)
        _write_result(result_path, result)
        return 0
    except BaseException as exc:
        if result_path is None and request is not None:
            result_path = request.get("result_path")
        if result_path is not None:
            _write_result(
                Path(result_path),
                {
                    "protocol": PROTOCOL,
                    "nonce": nonce,
                    "status": "failed",
                    "worker_error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
