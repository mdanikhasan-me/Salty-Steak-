"""Salty Steak Native Desktop AI Platform — structured browser client.

Salty Steak already ships and controls WebView2, so structured web interaction
needs no debugging port, no browser extension, and no connection into whatever
browser session the user happens to have open with their real accounts logged
in. The helper owns its own Chromium session in its own profile folder.

Same wire format as the other helpers: one JSON request per line in, one JSON
response per line out.
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

BROWSER_HOST_EXECUTABLE = "SaltyBrowserHost.exe"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0


BROWSER_READ_COMMANDS = frozenset(
    {
        "ping",
        "get_url",
        "get_title",
        "get_page",
        "read_page",
        "query",
        "wait_for",
        "find_element",
        "get_element",

        "list_tabs",
        "get_active_tab",



        "get_session_state",
        "get_media",
        "capture_preview",
    }
)


BROWSER_MUTATING_COMMANDS = frozenset(
    {
        "open_url",
        "navigate",
        "click",
        "set_value",
        "select",
        "focus",
        "scroll",
        "submit",
        "back",
        "forward",
        "reload",


        "show_window",
        "hide_window",
        "play_media",
        "pause_media",

        "new_tab",
        "switch_tab",
        "close_tab",
    }
)

BROWSER_COMMANDS = BROWSER_READ_COMMANDS | BROWSER_MUTATING_COMMANDS


class BrowserError(RuntimeError):
    """Raised when a structured browser request cannot be completed."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def find_browser_host(project_root: str | Path) -> Path | None:
    """Locate the helper next to the application or in the build output."""

    root = Path(project_root).resolve()
    candidates = [
        root / BROWSER_HOST_EXECUTABLE,
        root / "browser" / BROWSER_HOST_EXECUTABLE,
        root
        / "app"
        / "desktop"
        / "browser"
        / "bin"
        / "Release"
        / "net8.0-windows"
        / "win-x64"
        / BROWSER_HOST_EXECUTABLE,
        root
        / "app"
        / "desktop"
        / "browser"
        / "bin"
        / "Debug"
        / "net8.0-windows"
        / "win-x64"
        / BROWSER_HOST_EXECUTABLE,
    ]
    return next((path for path in candidates if path.is_file()), None)


class BrowserClient:
    """Own one browser session process and speak its protocol."""

    def __init__(
        self,
        executable: str | Path,
        *,
        profile_directory: str | Path | None = None,
        visible: bool = False,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.executable = Path(executable).resolve()
        if not self.executable.is_file():
            raise BrowserError(
                f"The browser host is missing: {self.executable}", kind="unavailable"
            )
        self.profile_directory = (
            Path(profile_directory).resolve() if profile_directory else None
        )
        self.visible = bool(visible)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), MAX_TIMEOUT_SECONDS))
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self.retirements = 0

    def _ensure_started(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        command = [str(self.executable)]
        if self.profile_directory is not None:
            command.extend(["--profile", str(self.profile_directory)])
        if self.visible:
            command.append("--visible")
        if os.environ.get("SALTY_BROWSER_EMBEDDED") == "1":
            command.append("--embedded")
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
            )
        except OSError as error:
            raise BrowserError(
                f"The browser host could not start: {error}", kind="unavailable"
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
            process.wait(timeout=8)
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
        if command not in BROWSER_COMMANDS and command != "dock_surface":
            raise BrowserError(
                f"Unknown browser command: {command!r}", kind="invalid_request"
            )


        if should_stop is not None and should_stop():
            raise BrowserError(
                "The task was stopped before the browser call started.",
                kind="cancelled",
            )

        body = dict(payload or {})
        body.setdefault("timeout_ms", int(self.timeout_seconds * 1000))
        request = {"id": uuid.uuid4().hex, "command": command, "payload": body}

        with self._lock:
            process = self._ensure_started()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                self._discard()
                raise BrowserError(
                    f"The browser host stopped accepting requests: {error}",
                    kind="unavailable",
                ) from error
            line = self._read_line(process)

        try:
            response = json.loads(line)
        except (TypeError, ValueError) as error:
            self._discard()
            raise BrowserError(
                f"The browser host returned unreadable output: {line[:200]!r}",
                kind="failed",
            ) from error

        if not response.get("ok"):
            failure = dict(response.get("error") or {})



            if failure.get("retire_host"):
                self._discard()
                self.retirements += 1
            raise BrowserError(
                str(failure.get("message") or "The browser request failed"),
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
            raise BrowserError(
                "The browser host exited unexpectedly"
                + (f": {detail}" if detail else ""),
                kind="unavailable",
            )
        return line

    def _discard(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()

    def available(self) -> bool:
        try:
            return bool(self.call("ping").get("ready"))
        except BrowserError:
            return False
