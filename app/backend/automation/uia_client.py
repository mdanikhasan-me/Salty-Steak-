"""Salty Steak Native Desktop AI Platform — UI Automation helper client.

The helper runs as its own process. The backend is embedded in the desktop
host through pythonnet, so it shares that process and its UI thread; UI
Automation calls block on unresponsive applications and can throw from inside
COM callbacks. Keeping that work behind a process boundary means a hung or
crashed probe costs a failed tool call instead of the whole application.

The wire format matches the native model worker already in this codebase:
one JSON request per line in, one JSON response per line out.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

UIA_HOST_EXECUTABLE = "SaltyUiaHost.exe"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 120.0



UIA_COMMANDS = frozenset(
    {
        "ping",
        "get_windows",
        "get_active_window",
        "get_tree",
        "find_control",
        "collect_list",
        "get_properties",
        "get_text",
        "focus",
        "invoke",
        "set_value",
        "select",
        "toggle",
        "expand",
        "collapse",
        "scroll",
    }
)


UIA_MUTATING_COMMANDS = frozenset(
    {
        "focus",
        "invoke",
        "set_value",
        "select",
        "toggle",
        "expand",
        "collapse",
        "scroll",
    }
)




UIA_DIAGNOSTIC_COMMANDS = frozenset({"stall"})


class UiAutomationError(RuntimeError):
    """Raised when a UI Automation request cannot be completed."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)


        self.kind = kind


def find_uia_host(project_root: str | Path) -> Path | None:
    """Locate the helper next to the application or in the build output."""

    root = Path(project_root).resolve()
    candidates = [
        root / UIA_HOST_EXECUTABLE,
        root / "uia" / UIA_HOST_EXECUTABLE,
        root
        / "app"
        / "desktop"
        / "uia"
        / "bin"
        / "Release"
        / "net8.0-windows"
        / "win-x64"
        / UIA_HOST_EXECUTABLE,
        root
        / "app"
        / "desktop"
        / "uia"
        / "bin"
        / "Debug"
        / "net8.0-windows"
        / "win-x64"
        / UIA_HOST_EXECUTABLE,
    ]
    return next((path for path in candidates if path.is_file()), None)


class UiAutomationClient:
    """Own one helper process and speak its request/response protocol."""

    def __init__(
        self,
        executable: str | Path,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.executable = Path(executable).resolve()
        if not self.executable.is_file():
            raise UiAutomationError(
                f"The UI Automation host is missing: {self.executable}",
                kind="unavailable",
            )
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()


        self.retirements = 0



    def _ensure_started(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        try:
            self._process = subprocess.Popen(
                [str(self.executable)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as error:
            raise UiAutomationError(
                f"The UI Automation host could not start: {error}", kind="unavailable"
            ) from error
        return self._process

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write(
                    json.dumps({"id": "shutdown", "command": "shutdown", "payload": {}})
                    + "\n"
                )
                process.stdin.flush()
            process.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass



    def call(
        self,
        command: str,
        payload: Mapping[str, Any] | None = None,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run one command and return its structured result."""

        if command not in UIA_COMMANDS and command not in UIA_DIAGNOSTIC_COMMANDS:
            raise UiAutomationError(
                f"Unknown UI Automation command: {command!r}", kind="invalid_request"
            )


        if should_stop is not None and should_stop():
            raise UiAutomationError(
                "The task was stopped before the UI Automation call started.",
                kind="cancelled",
            )

        body = dict(payload or {})
        body.setdefault("timeout_ms", int(self.timeout_seconds * 1000))
        request = {
            "id": uuid.uuid4().hex,
            "command": command,
            "payload": body,
        }
        with self._lock:
            process = self._ensure_started()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                self._discard()
                raise UiAutomationError(
                    f"The UI Automation host stopped accepting requests: {error}",
                    kind="unavailable",
                ) from error

            line = self._read_line(process)

        try:
            response = json.loads(line)
        except (TypeError, ValueError) as error:
            self._discard()
            raise UiAutomationError(
                f"The UI Automation host returned unreadable output: {line[:200]!r}",
                kind="failed",
            ) from error

        if not response.get("ok"):
            failure = dict(response.get("error") or {})





            if failure.get("retire_host"):
                self._discard()
                self.retirements += 1
            raise UiAutomationError(
                str(failure.get("message") or "The UI Automation request failed"),
                kind=str(failure.get("kind") or "failed"),
            )
        return dict(response.get("result") or {})

    def _read_line(self, process: subprocess.Popen[str]) -> str:
        assert process.stdout is not None


        line = process.stdout.readline()
        if not line:
            detail = ""
            if process.stderr is not None:
                try:
                    detail = (process.stderr.read() or "")[:500]
                except (OSError, ValueError):
                    detail = ""
            self._discard()
            raise UiAutomationError(
                "The UI Automation host exited unexpectedly"
                + (f": {detail}" if detail else ""),
                kind="unavailable",
            )
        return line

    def _discard(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()

    def available(self) -> bool:
        """Report whether the helper starts and answers."""

        try:
            return bool(self.call("ping").get("ready"))
        except UiAutomationError:
            return False
