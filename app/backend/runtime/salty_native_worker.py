"""App-owned process boundary for the private Salty native model engine."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Sequence

from ctypes import wintypes

from .salty_native import (
    SaltyNativeGeneration,
    SaltyNativeProfile,
    SaltyNativeRuntime,
    SaltyNativeRuntimeError,
)


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_CANCELLATION_GRACE_SECONDS = 5.0


class _WorkerCancellationTimeout(SaltyNativeRuntimeError):
    """The native worker did not acknowledge cancellation within its grace."""


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


def _create_kill_on_close_job() -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    information = _JobObjectExtendedLimitInformation()
    information.basic_limit_information.limit_flags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if not kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return int(handle)


def _assign_process_to_job(job_handle: int | None, process: subprocess.Popen[str]) -> None:
    if job_handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    process_handle = wintypes.HANDLE(int(process._handle))
    if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), process_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_job(job_handle: int | None) -> None:
    if job_handle is None or os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(job_handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _resolve_worker_python(
    project_root: Path,
    current_executable: str | Path | None = None,
) -> Path:
    """Select an actual private Python executable, never the desktop host."""




    candidates = (
        project_root / ".python" / "python.exe",
        project_root / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    fallback = Path(current_executable or sys.executable).resolve()
    if fallback.name.casefold() not in {"python.exe", "pythonw.exe", "python"}:
        raise SaltyNativeRuntimeError(
            "The private native worker could not find a packaged Python interpreter"
        )
    return fallback


class SaltyNativeWorkerRuntime:
    """Run the native library in a private child process with pipe-only IPC."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        library_directory: str | Path,
        profile: SaltyNativeProfile | None = None,
        source_sha256: str,
        adapters: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.library_directory = Path(library_directory).resolve()
        self.profile = profile or SaltyNativeProfile()
        self.source_sha256 = source_sha256
        self.adapters = tuple(dict(value) for value in adapters)
        self._process: subprocess.Popen[str] | None = None
        self._job_handle: int | None = None
        self._stderr_handle: Any = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._warmup_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._warmed = False
        self._warming = False
        self._warmup_seconds: float | None = None
        self._warmup_error: str | None = None
        self._warmup_passes = 0
        self._warmup_generated_tokens = 0
        self._last_description = self._cold_description()

    @property
    def loaded(self) -> bool:
        process = self._process
        return bool(
            process is not None
            and process.poll() is None
            and self._last_description.get("loaded")
        )

    @property
    def ready(self) -> bool:
        return self.loaded and self._warmed

    def conditional_adapter_ids(self, activation: str) -> tuple[str, ...]:
        """Return registered adapters assigned to one conditional activation lane."""

        checked = str(activation).strip().casefold()
        return tuple(
            str(value.get("adapter_id") or value.get("id") or "")
            for value in self.adapters
            if str(value.get("activation") or "always").strip().casefold() == checked
            and str(value.get("adapter_id") or value.get("id") or "")
        )

    def _cold_description(self) -> dict[str, Any]:
        return {
            "loaded": False,
            "ready": False,
            "warming": self._warming,
            "warmup_seconds": self._warmup_seconds,
            "warmup_error": self._warmup_error,
            "warmup_passes": self._warmup_passes,
            "warmup_generated_tokens": self._warmup_generated_tokens,
            "runtime_id": None,
            "worker_pid": None,
            "runtime_family": "salty_native_steak20",
            "engine": "app_owned_private_native_worker",
            "profile": asdict(self.profile),
            "source_sha256": self.source_sha256,
            "verified_source_sha256": None,
            "model_path": str(self.model_path),
            "adapters": [dict(value) for value in self.adapters],
            "architectural_context_limit": 262_144,
            "configured_context_limit": self.profile.context_limit,
            "resident_context_limit": self.profile.initial_context_limit,
            "allocated_context_limit": None,
            "load_seconds": None,
            "external_service_required": False,
            "network_listener_created": False,
            "ipc_transport": "anonymous_pipes",
        }

    def _status_description(self) -> dict[str, Any]:
        process = self._process
        return {
            **dict(self._last_description),
            "worker_pid": (
                process.pid if process is not None and process.poll() is None else None
            ),
            "ready": self.ready,
            "warming": self._warming,
            "warmup_seconds": self._warmup_seconds,
            "warmup_error": self._warmup_error,
            "warmup_passes": self._warmup_passes,
            "warmup_generated_tokens": self._warmup_generated_tokens,
        }

    def _start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        project_root = Path(__file__).resolve().parents[3]
        log_directory = self.library_directory.parent / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"native-worker-{uuid.uuid4().hex}.log"
        self._stderr_handle = log_path.open("a", encoding="utf-8")
        environment = os.environ.copy()
        prior_python_path = environment.get("PYTHONPATH")
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        worker_python = _resolve_worker_python(project_root)
        python_paths = [str(project_root)]
        private_site_packages = project_root / ".venv" / "Lib" / "site-packages"
        if private_site_packages.is_dir():
            python_paths.append(str(private_site_packages))
        if prior_python_path:
            python_paths.append(prior_python_path)
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        private_python = (project_root / ".python" / "python.exe").resolve()
        if worker_python == private_python:
            environment["PYTHONHOME"] = str(private_python.parent)
            environment["VIRTUAL_ENV"] = str(project_root / ".venv")
            environment["PYTHONNOUSERSITE"] = "1"
        self._job_handle = _create_kill_on_close_job()
        try:
            self._process = subprocess.Popen(
                [
                    str(worker_python),
                    "-B",
                    "-m",
                    "app.backend.runtime.salty_native_worker",
                    "--serve",
                ],
                cwd=project_root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_handle,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=creation_flags,
            )
            _assign_process_to_job(self._job_handle, self._process)
        except BaseException:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
                self._process.wait(timeout=10)
            _close_job(self._job_handle)
            self._job_handle = None
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None
            self._process = None
            raise
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._responses = response_queue
        self._reader = threading.Thread(
            target=self._read_responses,
            args=(self._process, response_queue),
            name="salty-native-worker-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_responses(
        self,
        process: subprocess.Popen[str],
        response_queue: queue.Queue[dict[str, Any]],
    ) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                response = {
                    "id": None,
                    "ok": False,
                    "error": {
                        "type": "invalid_worker_response",
                        "message": line.rstrip(),
                    },
                }




            response_queue.put(response)

    def _terminate_process_locked(self) -> None:
        """Stop and forget the current private worker while ``_lock`` is held."""

        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):


                    pass
        if process is not None:
            for stream_name in ("stdin", "stdout"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self._stderr_handle is not None:
            try:
                self._stderr_handle.close()
            except OSError:
                pass
        try:
            _close_job(self._job_handle)
        except OSError:
            pass
        self._process = None
        self._job_handle = None
        self._stderr_handle = None
        self._reader = None
        self._responses = queue.Queue()
        self._warmed = False
        self._warming = False
        self._last_description = self._cold_description()

    def _request(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 900,
        should_stop: Callable[[], bool] | None = None,
        cancellation_path: Path | None = None,
        on_preview: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        with self._lock:
            self._start()
            process = self._process
            if process is None or process.stdin is None:
                raise SaltyNativeRuntimeError("The private native worker did not start")
            request_id = str(uuid.uuid4())
            process.stdin.write(
                json.dumps(
                    {"id": request_id, "command": command, "payload": payload or {}},
                    separators=(",", ":"),
                )
                + "\n"
            )
            process.stdin.flush()
            deadline = time.monotonic() + timeout
            cancellation_requested = False
            cancellation_deadline: float | None = None
            while time.monotonic() < deadline:
                if (
                    should_stop is not None
                    and should_stop()
                    and cancellation_path is not None
                    and not cancellation_requested
                ):
                    cancellation_path.parent.mkdir(parents=True, exist_ok=True)
                    cancellation_path.touch()
                    cancellation_requested = True
                    cancellation_deadline = (
                        time.monotonic() + _CANCELLATION_GRACE_SECONDS
                    )
                if (
                    cancellation_deadline is not None
                    and time.monotonic() >= cancellation_deadline
                ):
                    self._terminate_process_locked()
                    raise _WorkerCancellationTimeout(
                        "The private native worker was restarted after it did not "
                        "acknowledge cancellation within "
                        f"{_CANCELLATION_GRACE_SECONDS:g} seconds"
                    )
                try:
                    response = self._responses.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        if cancellation_requested:
                            self._terminate_process_locked()
                            raise _WorkerCancellationTimeout(
                                "The private native worker exited while cancelling"
                            )
                        raise SaltyNativeRuntimeError(
                            f"The private native worker exited with code {process.returncode}"
                        )
                    continue
                if response.get("id") != request_id:






                    continue
                if response.get("event") == "generation_preview":
                    preview = response.get("generation_preview")
                    if on_preview is not None and isinstance(preview, dict):
                        try:
                            on_preview(dict(preview))
                        except BaseException:


                            pass
                    continue
                if not response.get("ok"):
                    error = response.get("error") or {}
                    raise SaltyNativeRuntimeError(
                        str(error.get("message") or "The private native worker failed")
                    )
                return response.get("result")
            raise TimeoutError(f"The private native worker timed out during {command}")

    def load(self) -> dict[str, Any]:
        if self.loaded:
            return self._status_description()
        result = self._request(
            "load",
            {
                "model_path": str(self.model_path),
                "library_directory": str(self.library_directory),
                "source_sha256": self.source_sha256,
                "profile": asdict(self.profile),
                "adapters": [dict(value) for value in self.adapters],
            },
        )
        self._last_description = {
            **dict(result),
            "engine": "app_owned_private_native_worker",
            "ipc_transport": "anonymous_pipes",
        }
        return self._status_description()

    def describe(self) -> dict[str, Any]:
        if not self.loaded:
            return self._cold_description()
        return self._status_description()

    def warmup(self) -> dict[str, Any]:
        """Load and prime private autoregressive decode before first chat.

        A one-token request exercises prompt evaluation but never feeds a
        generated token back through the model. On CUDA that leaves graph and
        kernel setup on the first user-visible reply. Two short, discarded
        Instant passes move that deterministic setup into background startup
        without changing the model, sampler, or any persisted conversation.
        """

        with self._warmup_lock:
            if self.ready:
                return self._status_description()
            self._stop_event.clear()
            self._warming = True
            self._warmup_error = None
            self._warmup_passes = 0
            self._warmup_generated_tokens = 0
            started = time.perf_counter()
            cancellation_path = (
                self.library_directory.parent
                / "control"
                / f"warmup-cancel-{uuid.uuid4().hex}.signal"
            )
            try:
                self.load()
                for prompt in (
                    "Count from one to eight using words only.",
                    "List eight primary colors or common colors using words only.",
                ):
                    result = self._request(
                        "generate",
                        {
                            "messages": [{"role": "user", "content": prompt}],
                            "maximum_output_tokens": 8,
                            "temperature": 0.0,
                            "top_p": 1.0,
                            "top_k": 1,
                            "repetition_penalty": 1.0,
                            "seed": 0,
                            "stop_sequences": [],
                            "context_window_tokens": min(
                                512, self.profile.context_limit
                            ),
                            "reserved_output_tokens": 8,
                            "reasoning_mode": "instant",
                            "maximum_output_mode": "manual",
                            "cancellation_path": str(cancellation_path),
                        },
                        should_stop=self._stop_event.is_set,
                        cancellation_path=cancellation_path,
                    )
                    if self._stop_event.is_set() or bool(result.get("cancelled")):
                        raise SaltyNativeRuntimeError(
                            "Native runtime warm-up was cancelled"
                        )
                    self._warmup_passes += 1
                    self._warmup_generated_tokens += len(result.get("token_ids") or [])
                self._warmed = True
                self._warmup_seconds = round(time.perf_counter() - started, 6)
            except BaseException as error:
                self._warmed = False
                self._warmup_seconds = round(time.perf_counter() - started, 6)
                self._warmup_error = str(error)
                raise
            finally:
                cancellation_path.unlink(missing_ok=True)
                self._warming = False
            return self._status_description()

    def generate(
        self,
        *,
        messages: Sequence[dict[str, str]],
        maximum_output_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        seed: int | None,
        stop_sequences: Sequence[str] = (),
        should_stop: Callable[[], bool] | None = None,
        on_preview: Callable[[dict[str, Any]], None] | None = None,
        context_window_tokens: int | None = None,
        reserved_output_tokens: int | None = None,
        reasoning_mode: str = "cooking",
        maximum_output_mode: str = "manual",
        enabled_adapter_ids: Sequence[str] | None = None,
        allowed_first_tokens: Sequence[str] = (),
    ) -> SaltyNativeGeneration:
        self.warmup()
        cancellation_path = (
            self.library_directory.parent
            / "control"
            / f"cancel-{uuid.uuid4().hex}.signal"
        )
        try:
            try:
                result = self._request(
                    "generate",
                    {
                        "messages": list(messages),
                        "maximum_output_tokens": maximum_output_tokens,
                        "temperature": temperature,
                        "top_p": top_p,
                        "top_k": top_k,
                        "repetition_penalty": repetition_penalty,
                        "seed": seed,
                        "stop_sequences": list(stop_sequences),
                        "context_window_tokens": context_window_tokens,
                        "reserved_output_tokens": reserved_output_tokens,
                        "reasoning_mode": reasoning_mode,
                        "maximum_output_mode": maximum_output_mode,
                        "enabled_adapter_ids": (
                            list(enabled_adapter_ids)
                            if enabled_adapter_ids is not None
                            else None
                        ),
                        "allowed_first_tokens": list(allowed_first_tokens),
                        "cancellation_path": str(cancellation_path),
                    },
                    should_stop=should_stop,
                    cancellation_path=cancellation_path,
                    on_preview=on_preview,
                )
            except _WorkerCancellationTimeout as error:
                return SaltyNativeGeneration(
                    text="",
                    token_ids=[],
                    omitted_turns=0,
                    cancelled=True,
                    finish_reason="cancelled",
                    technical_details={
                        **self._cold_description(),
                        "forced_worker_restart": True,
                        "cancellation_grace_seconds": _CANCELLATION_GRACE_SECONDS,
                        "cancellation_detail": str(error),
                    },
                )
            technical = dict(result["technical_details"])
            technical.update(
                {
                    "engine": "app_owned_private_native_worker",
                    "ipc_transport": "anonymous_pipes",
                }
            )
            return SaltyNativeGeneration(
                text=str(result["text"]),
                token_ids=[int(token) for token in result["token_ids"]],
                omitted_turns=int(result["omitted_turns"]),
                cancelled=bool(result["cancelled"]),
                finish_reason=str(result["finish_reason"]),
                technical_details=technical,
            )
        finally:
            cancellation_path.unlink(missing_ok=True)

    def classify_route(
        self,
        *,
        messages: Sequence[dict[str, str]],
        allowed_tokens: Sequence[str],
        enabled_adapter_ids: Sequence[str],
        should_stop: Callable[[], bool] | None = None,
    ) -> SaltyNativeGeneration:
        """Use the worker's model-sharing auxiliary routing context."""

        self.warmup()
        cancellation_path = (
            self.library_directory.parent
            / "control"
            / f"cancel-{uuid.uuid4().hex}.signal"
        )
        try:
            result = self._request(
                "classify_route",
                {
                    "messages": list(messages),
                    "allowed_tokens": list(allowed_tokens),
                    "enabled_adapter_ids": list(enabled_adapter_ids),
                    "cancellation_path": str(cancellation_path),
                },
                should_stop=should_stop,
                cancellation_path=cancellation_path,
            )
            technical = dict(result["technical_details"])
            technical.update(
                {
                    "engine": "app_owned_private_native_worker",
                    "ipc_transport": "anonymous_pipes",
                }
            )
            return SaltyNativeGeneration(
                text=str(result["text"]),
                token_ids=[int(token) for token in result["token_ids"]],
                omitted_turns=int(result["omitted_turns"]),
                cancelled=bool(result["cancelled"]),
                finish_reason=str(result["finish_reason"]),
                technical_details=technical,
            )
        finally:
            cancellation_path.unlink(missing_ok=True)

    def unload(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._process
            if process is None:
                _close_job(self._job_handle)
                self._job_handle = None
                self._last_description = self._cold_description()
                return
            if process.poll() is None:
                try:
                    self._request("shutdown", timeout=120)
                except (OSError, RuntimeError, TimeoutError):
                    process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if self._stderr_handle is not None:
                self._stderr_handle.close()
            _close_job(self._job_handle)
            self._process = None
            self._job_handle = None
            self._stderr_handle = None
            self._reader = None
            self._warmed = False
            self._warming = False
            self._last_description = self._cold_description()


class _CancellationWatcher:
    """Sample the host's cancellation signal away from the token hot path.

    The decode loop asks whether it should stop once per generated token.
    Answering that directly from the filesystem costs one syscall per token, so
    a background thread samples the signal file and the loop reads a memory
    flag instead. The file remains the cross-process contract with the host.
    """

    def __init__(self, path: Path, interval: float = 0.05) -> None:
        self.path = path
        self.interval = float(interval)
        self.requested = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_CancellationWatcher":
        self._thread = threading.Thread(
            target=self._watch,
            name="salty-native-cancellation-watch",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._finished.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _watch(self) -> None:
        while not self._finished.is_set():
            if self.path.is_file():
                self.requested.set()
                return
            self._finished.wait(self.interval)


def _serve() -> int:
    runtime: SaltyNativeRuntime | None = None
    for line in sys.stdin:
        request_id: str | None = None
        should_exit = False
        try:
            request = json.loads(line)
            request_id = str(request["id"])
            command = str(request["command"])
            payload = dict(request.get("payload") or {})
            if command == "load":
                if runtime is None:
                    runtime = SaltyNativeRuntime(
                        model_path=payload["model_path"],
                        library_directory=payload["library_directory"],
                        source_sha256=str(payload["source_sha256"]),
                        profile=SaltyNativeProfile(**dict(payload["profile"])),
                        adapters=list(payload.get("adapters") or []),
                    )
                result: Any = runtime.load()
            elif command == "generate":
                if runtime is None:
                    raise SaltyNativeRuntimeError("The private native worker is not loaded")
                cancellation_path = Path(str(payload.pop("cancellation_path")))
                preview = _GenerationPreviewAccumulator()
                last_preview_at = 0.0

                def emit_preview(piece: str) -> None:
                    nonlocal last_preview_at
                    if not piece:
                        return
                    current_preview = preview.feed(piece)
                    now = time.monotonic()
                    if now - last_preview_at < 0.25:
                        return
                    last_preview_at = now
                    event = {
                        "id": request_id,
                        "event": "generation_preview",
                        "generation_preview": current_preview,
                    }
                    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
                    sys.stdout.flush()

                with _CancellationWatcher(cancellation_path) as cancellation:
                    generation = runtime.generate(
                        **payload,
                        should_stop=cancellation.requested.is_set,
                        on_text=emit_preview,
                    )
                result = asdict(generation)
            elif command == "classify_route":
                if runtime is None:
                    raise SaltyNativeRuntimeError("The private native worker is not loaded")
                cancellation_path = Path(str(payload.pop("cancellation_path")))
                with _CancellationWatcher(cancellation_path) as cancellation:
                    generation = runtime.classify_route(
                        **payload,
                        should_stop=cancellation.requested.is_set,
                    )
                result = asdict(generation)
            elif command == "shutdown":
                if runtime is not None:
                    runtime.unload()
                    runtime = None
                result = {"shutdown": True}
                should_exit = True
            else:
                raise ValueError(f"Unknown private native worker command: {command}")
            response = {"id": request_id, "ok": True, "result": result}
        except BaseException as error:
            response = {
                "id": request_id,
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                },
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if should_exit:
            return 0
    if runtime is not None:
        runtime.unload()
    return 0


def _generation_preview_kind(value: str) -> str:
    """Classify only model-emitted structured trace boundaries.

    Ordinary prose about 'reasoning' is output, not a hidden trace. The UI can
    therefore avoid falsely presenting a model answer as private reasoning.
    """

    content = str(value or "").lstrip()
    start = content.find("<think>")
    if start >= 0 and content.find("</think>", start) < 0:
        return "reasoning"
    return "output"


class _GenerationPreviewAccumulator:
    """Keep a bounded, tag-aware preview without rebuilding full output."""

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"
    _TAG_LOOKBEHIND = len(_CLOSE_TAG) - 1

    def __init__(self, limit: int = 1200) -> None:
        if limit < 1:
            raise ValueError("preview limit must be positive")
        self.limit = int(limit)
        self.token_count = 0
        self.character_count = 0
        self._raw_tail = ""
        self._tag_tail = ""
        self._reasoning_open = False

    def feed(self, piece: str) -> dict[str, Any]:
        value = str(piece or "")
        if value:
            self.token_count += 1
            self.character_count += len(value)
            self._raw_tail = (self._raw_tail + value)[-self.limit :]
            self._scan_tags(value)
        kind = "reasoning" if self._reasoning_open else "output"
        visible_tail = self._visible_tail(kind)
        return {
            "kind": kind,
            "tail_text": visible_tail[-self.limit :],
            "token_count": self.token_count,
            "character_count": self.character_count,
        }

    def _scan_tags(self, piece: str) -> None:
        scan = self._tag_tail + piece
        cursor = 0
        while cursor < len(scan):
            open_at = scan.find(self._OPEN_TAG, cursor)
            close_at = scan.find(self._CLOSE_TAG, cursor)
            candidates = [
                (position, tag, state)
                for position, tag, state in (
                    (open_at, self._OPEN_TAG, True),
                    (close_at, self._CLOSE_TAG, False),
                )
                if position >= 0
            ]
            if not candidates:
                break
            position, tag, state = min(candidates, key=lambda item: item[0])
            self._reasoning_open = state
            cursor = position + len(tag)
        self._tag_tail = scan[-self._TAG_LOOKBEHIND :]

    def _visible_tail(self, kind: str) -> str:
        if kind == "reasoning":
            if self._OPEN_TAG in self._raw_tail:
                return self._raw_tail.rsplit(self._OPEN_TAG, 1)[-1]
            return self._raw_tail
        if self._CLOSE_TAG in self._raw_tail:
            return self._raw_tail.rsplit(self._CLOSE_TAG, 1)[-1]
        return self._raw_tail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    return _serve()


if __name__ == "__main__":
    raise SystemExit(main())
