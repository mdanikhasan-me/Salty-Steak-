"""Local-only Windows automation with durable grants and an exact audit trail.

This module is deliberately separate from Plugins and MCP.  A model response
cannot grant a capability, and invoking a capability never starts a network
service.  Terminal execution remains a powerful local-user action: the broker
confines and validates the working directory, but it is not a filesystem or
network sandbox for the executable chosen by the user.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO

from ..database.control import Database, json_text, new_id, parse_json, utc_now
from ..system.files import atomic_write_bytes, ensure_within, sha256_file
from .browser_client import (
    BROWSER_COMMANDS,
    BROWSER_MUTATING_COMMANDS,
    BrowserClient,
    BrowserError,
    find_browser_host,
)
from .uia_client import (
    UIA_COMMANDS,
    UIA_MUTATING_COMMANDS,
    UiAutomationClient,
    UiAutomationError,
    find_uia_host,
)
from .filesystem import (
    clean_temp_root_contents,
    recycle_confirmed_file,
    snapshot_regular_file,
    windows_temp_roots,
)


AUTOMATION_SCHEMA = "salty-steak-windows-automation-v1"
TERMINAL_CAPABILITY = "terminal.execute"
SCREEN_CAPTURE_CAPABILITY = "screen.capture"
INPUT_CONTROL_CAPABILITY = "input.control"
APPLICATION_LAUNCH_CAPABILITY = "application.launch"
WINDOW_CONTROL_CAPABILITY = "window.control"
UI_AUTOMATION_CAPABILITY = "ui.automation"
BROWSER_CAPABILITY = "browser.control"
CAPABILITIES = (
    TERMINAL_CAPABILITY,
    SCREEN_CAPTURE_CAPABILITY,
    INPUT_CONTROL_CAPABILITY,
    APPLICATION_LAUNCH_CAPABILITY,
    WINDOW_CONTROL_CAPABILITY,
    UI_AUTOMATION_CAPABILITY,
    BROWSER_CAPABILITY,
)
CAPABILITY_DISPLAY_NAMES = {
    TERMINAL_CAPABILITY: "Terminal command",
    SCREEN_CAPTURE_CAPABILITY: "Primary-screen screenshot",
    INPUT_CONTROL_CAPABILITY: "Mouse and keyboard control",
    APPLICATION_LAUNCH_CAPABILITY: "Application and link launch",
    WINDOW_CONTROL_CAPABILITY: "Window listing and focus",
    UI_AUTOMATION_CAPABILITY: "Semantic control of application interfaces",
    BROWSER_CAPABILITY: "Structured web page reading and interaction",
}
MAX_WINDOW_TITLE_CHARACTERS = 512
MAX_ENUMERATED_WINDOWS = 400
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
DEFAULT_OUTPUT_BYTES = 1024 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARACTERS = 32_768
MAX_SCREEN_PIXELS = 33_177_600



MAX_SCREENSHOT_BYTES = 24 * 1024 * 1024
DEFAULT_INPUT_DELAY_MS = 80
MAX_INPUT_DELAY_MS = 5_000
MAX_TYPED_CHARACTERS = 4_096
MAX_SCROLL_CLICKS = 100
DEFAULT_LAUNCH_WAIT_MS = 0
MAX_LAUNCH_WAIT_MS = 10_000
MAX_TARGET_CHARACTERS = 2_048


class AutomationBroker:
    """Persist explicit grants and execute only the two local capabilities."""

    def __init__(
        self,
        database: Database,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
        max_timeout_seconds: float = MAX_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
        platform_name: str | None = None,
        screen_capturer: Callable[[Path], dict[str, Any]] | None = None,
        file_recycler: Callable[[Path], None] | None = None,
        temp_roots_provider: Callable[[], list[dict[str, Any]]] | None = None,
        uia_client: Any | None = None,
        browser_client: Any | None = None,
    ) -> None:
        self.database = database
        self.project_root = Path(project_root).resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("Automation project root must be a directory")
        self.artifact_root = Path(artifact_root).resolve()
        self.max_timeout_seconds = float(max_timeout_seconds)
        if not 0.05 <= self.max_timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"Automation timeout limit must be between 0.05 and {MAX_TIMEOUT_SECONDS:g} seconds"
            )
        self.max_output_bytes = int(max_output_bytes)
        if not 1 <= self.max_output_bytes <= DEFAULT_OUTPUT_BYTES:
            raise ValueError(
                f"Automation output limit must be between 1 and {DEFAULT_OUTPUT_BYTES} bytes"
            )
        self.platform_name = platform_name or os.name
        self._screen_capturer = screen_capturer or _capture_primary_screen_bmp
        self._file_recycler = file_recycler
        self._temp_roots_provider = temp_roots_provider or windows_temp_roots


        self._uia_client = uia_client
        if self._uia_client is None and self.platform_name == "nt":
            host = find_uia_host(self.project_root)
            if host is not None:
                try:
                    self._uia_client = UiAutomationClient(host)
                except UiAutomationError:
                    self._uia_client = None
        self._browser_client = browser_client
        if self._browser_client is None and self.platform_name == "nt":
            browser_host = find_browser_host(self.project_root)
            if browser_host is not None:
                try:
                    self._browser_client = BrowserClient(
                        browser_host,
                        profile_directory=self.artifact_root / "browser-profile",
                    )
                except BrowserError:
                    self._browser_client = None
        self._lock = threading.RLock()
        self._active: dict[str, tuple[str, subprocess.Popen[bytes] | None]] = {}
        self._revoked_invocations: set[str] = set()
        self._closed = False
        self._ensure_default_grants()

    def _ensure_default_grants(self) -> None:
        now = utc_now()
        defaults = {
            TERMINAL_CAPABILITY: {
                "working_directory_root": str(self.project_root),
            },
            SCREEN_CAPTURE_CAPABILITY: {
                "screen": "primary",
                "output_root": str(self.artifact_root / "screenshots"),
            },
            INPUT_CONTROL_CAPABILITY: {"scope": "primary_screen_and_focused_window"},
            APPLICATION_LAUNCH_CAPABILITY: {"scope": "installed_applications_and_links"},
            WINDOW_CONTROL_CAPABILITY: {"scope": "visible_top_level_windows"},
            UI_AUTOMATION_CAPABILITY: {"scope": "accessible_application_controls"},
            BROWSER_CAPABILITY: {"scope": "salty_owned_browser_session"},
        }
        with self.database.transaction() as connection:
            for capability in CAPABILITIES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO automation_grants(
                        id, capability, enabled, constraints_json,
                        granted_at, revoked_at, created_at, updated_at
                    ) VALUES (?, ?, 0, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        capability,
                        capability,
                        json_text(defaults[capability]),
                        now,
                        now,
                    ),
                )

    def status(self) -> dict[str, Any]:
        rows = {
            str(row["capability"]): self._public_grant(row)
            for row in self.database.fetch_all(
                "SELECT * FROM automation_grants ORDER BY capability"
            )
        }
        with self._lock:
            active = [
                {"audit_record_id": identifier, "capability": capability}
                for identifier, (capability, _process) in sorted(self._active.items())
            ]
        audit_count = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM automation_audit_records"
        )
        supported = self.platform_name == "nt"
        capabilities = []
        for capability in CAPABILITIES:
            grant = rows[capability]
            constraint_valid, constraint_error = self._constraints_valid(
                capability,
                grant["constraints"],
            )



            helper_missing = (
                capability == UI_AUTOMATION_CAPABILITY and self._uia_client is None
            ) or (capability == BROWSER_CAPABILITY and self._browser_client is None)
            capabilities.append(
                {
                    **grant,
                    "display_name": CAPABILITY_DISPLAY_NAMES[capability],
                    "platform_supported": supported,
                    "runtime_available": bool(supported and not helper_missing),
                    "runtime_unavailable_reason": (
                        "The helper host for this capability is not part of this "
                        "build."
                        if helper_missing
                        else None
                    ),
                    "constraint_valid": constraint_valid,
                    "constraint_error": constraint_error,
                    "effective_enabled": bool(
                        supported and grant["granted"] and constraint_valid
                    ),
                }
            )
        return {
            "schema": AUTOMATION_SCHEMA,
            "platform": "windows" if supported else self.platform_name,
            "local_only": True,
            "external_service_required": False,
            "starts_network_service": False,
            "default_enabled": False,
            "explicit_persisted_user_grant_required": True,
            "capabilities": capabilities,
            "active_invocations": active,
            "audit_record_count": int(audit_count["count"] if audit_count else 0),
            "limits": {
                "max_timeout_seconds": self.max_timeout_seconds,
                "max_output_bytes_per_stream": self.max_output_bytes,
                "max_arguments": MAX_ARGUMENTS,
                "max_argument_characters": MAX_ARGUMENT_CHARACTERS,
                "screenshot_screen_count": 1,
                "max_screenshot_pixels": MAX_SCREEN_PIXELS,
            },
            "risk_boundaries": {
                "argv_only": True,
                "subprocess_shell_enabled": False,
                "shell_string_concatenation": False,
                "shell_executables_blocked": False,



                "working_directory_confined": "ask_every_time_only",
                "working_directory_confinement_is_not_a_filesystem_sandbox": True,
                "full_access_lifts_working_directory_confinement": True,
                "filesystem_sandbox_enforced": False,
                "executable_allowlist_enforced": False,
                "command_network_isolation_enforced": False,
                "requested_process_runs_as_current_user": True,
                "requested_process_inherits_host_environment": True,
                "process_tree_termination": "windows_job_object_when_available_otherwise_parent_process",
                "screen_capture_scope": "primary_screen_only",
                "screen_capture_visibility_indicator": False,
                "audit_storage": "local_sqlite",
                "audit_redaction": False,
                "screenshot_artifacts_persist_until_removed": True,
            },
        }

    def grant(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._require_open()
        self._only_fields(
            request,
            {"capabilities", "user_confirmed", "working_directory_root"},
        )
        capabilities = self._capability_list(request.get("capabilities"))
        if request.get("user_confirmed") is not True:
            raise PermissionError(
                "Automation grants require an explicit user_confirmed=true request"
            )
        if self.platform_name != "nt":
            raise RuntimeError("The Windows automation broker is unavailable on this platform")

        terminal_root = self.project_root
        if TERMINAL_CAPABILITY in capabilities:
            terminal_root = self._project_directory(
                request.get("working_directory_root", self.project_root)
            )
        constraints = {
            TERMINAL_CAPABILITY: {"working_directory_root": str(terminal_root)},
            SCREEN_CAPTURE_CAPABILITY: {
                "screen": "primary",
                "output_root": str(self.artifact_root / "screenshots"),
            },
            INPUT_CONTROL_CAPABILITY: {"scope": "primary_screen_and_focused_window"},
            APPLICATION_LAUNCH_CAPABILITY: {"scope": "installed_applications_and_links"},
            WINDOW_CONTROL_CAPABILITY: {"scope": "visible_top_level_windows"},
            UI_AUTOMATION_CAPABILITY: {"scope": "accessible_application_controls"},
            BROWSER_CAPABILITY: {"scope": "salty_owned_browser_session"},
        }
        now = utc_now()
        request_record = {
            "capabilities": capabilities,
            "user_confirmed": True,
            "working_directory_root": (
                str(terminal_root) if TERMINAL_CAPABILITY in capabilities else None
            ),
        }
        with self.database.transaction() as connection:
            for capability in capabilities:
                connection.execute(
                    """
                    UPDATE automation_grants
                    SET enabled = 1, constraints_json = ?, granted_at = ?,
                        revoked_at = NULL, updated_at = ?
                    WHERE capability = ?
                    """,
                    (json_text(constraints[capability]), now, now, capability),
                )
                self._insert_completed_audit(
                    connection,
                    event="grant",
                    capability=capability,
                    outcome="granted",
                    request=request_record,
                    result={
                        "granted": True,
                        "constraints": constraints[capability],
                    },
                    created_at=now,
                )
        return self.status()

    def revoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._require_open()
        self._only_fields(request, {"capabilities"})
        raw_capabilities = request.get("capabilities")
        capabilities = (
            list(CAPABILITIES)
            if raw_capabilities is None
            else self._capability_list(raw_capabilities)
        )
        processes: list[subprocess.Popen[bytes]] = []
        now = utc_now()
        with self._lock:
            with self.database.transaction() as connection:
                for capability in capabilities:
                    existing = connection.execute(
                        "SELECT enabled FROM automation_grants WHERE capability = ?",
                        (capability,),
                    ).fetchone()
                    was_granted = bool(existing and existing["enabled"])
                    connection.execute(
                        """
                        UPDATE automation_grants
                        SET enabled = 0, revoked_at = ?, updated_at = ?
                        WHERE capability = ?
                        """,
                        (now, now, capability),
                    )
                    self._insert_completed_audit(
                        connection,
                        event="revoke",
                        capability=capability,
                        outcome="revoked",
                        request={"capabilities": capabilities},
                        result={"granted": False, "was_granted": was_granted},
                        created_at=now,
                    )
                for identifier, (capability, process) in self._active.items():
                    if capability in capabilities:
                        self._revoked_invocations.add(identifier)
                        if process is not None:
                            processes.append(process)
        for process in processes:
            _terminate_process(process)
        return self.status()

    def invoke(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._require_open()
        self._only_fields(
            request,
            {"capability", "arguments", "user_confirmed", "authority_mode"},
        )
        authority_mode = str(request.get("authority_mode") or "ask_every_time").strip()
        if authority_mode not in {"ask_every_time", "full_access"}:
            raise ValueError("Invalid computer authority mode")
        capability_value = request.get("capability")
        if not isinstance(capability_value, str) or capability_value not in CAPABILITIES:
            raise ValueError(f"Unknown automation capability: {capability_value!r}")
        capability = capability_value
        arguments_value = request.get("arguments", {})
        if not isinstance(arguments_value, Mapping):
            raise ValueError("Automation arguments must be an object")
        arguments = dict(arguments_value)
        audit_id = self._start_audit(
            event="invoke",
            capability=capability,
            request={
                "capability": capability,
                "arguments": arguments,
                "user_confirmed": request.get("user_confirmed") is True,
                "authority_mode": authority_mode,
            },
        )
        try:
            if request.get("user_confirmed") is not True:
                raise PermissionError(
                    "Automation invocation requires an explicit user_confirmed=true request"
                )
            if self.platform_name != "nt":
                raise RuntimeError(
                    "The Windows automation broker is unavailable on this platform"
                )
            with self._lock:
                grant = self._grant_row(capability)
                if not bool(grant["enabled"]):
                    raise PermissionError(
                        f"Automation capability {capability} has not been granted"
                    )
                constraints = parse_json(str(grant["constraints_json"]), {})
                valid, error = self._constraints_valid(capability, constraints)
                if not valid:
                    raise PermissionError(
                        f"Automation capability {capability} has invalid persisted constraints: {error}"
                    )
                self._active[audit_id] = (capability, None)

            if capability == TERMINAL_CAPABILITY:
                result = self._invoke_terminal(
                    audit_id, arguments, constraints, authority_mode
                )
            elif capability == SCREEN_CAPTURE_CAPABILITY:
                result = self._invoke_screen_capture(audit_id, arguments)
            elif capability == INPUT_CONTROL_CAPABILITY:
                result = self._invoke_input_control(audit_id, arguments)
            elif capability == WINDOW_CONTROL_CAPABILITY:
                result = self._invoke_window_control(audit_id, arguments)
            elif capability == UI_AUTOMATION_CAPABILITY:
                result = self._invoke_ui_automation(audit_id, arguments)
            elif capability == BROWSER_CAPABILITY:
                result = self._invoke_browser(audit_id, arguments)
            else:
                result = self._invoke_application_launch(audit_id, arguments)
            outcome = str(result["status"])
            self._finish_audit(audit_id, outcome=outcome, result=result)
            return result
        except BaseException as exc:
            outcome = (
                "denied"
                if isinstance(exc, PermissionError)
                else "timed_out"
                if isinstance(exc, TimeoutError)
                else "failed"
            )
            self._finish_audit(
                audit_id,
                outcome=outcome,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        finally:
            with self._lock:
                self._active.pop(audit_id, None)
                self._revoked_invocations.discard(audit_id)

    def prepare_file_trash(self, path: str | Path) -> dict[str, Any]:
        """Inspect one exact file without changing it."""

        self._require_open()
        return snapshot_regular_file(path)

    def prepare_temp_cleanup(self) -> list[dict[str, Any]]:
        """Resolve the exact bounded Windows roots for a cleanup review."""

        self._require_open()
        roots = self._temp_roots_provider()
        if not isinstance(roots, list) or not roots:
            raise RuntimeError("No Windows temporary-data roots are configured")
        return [dict(root) for root in roots]

    def invoke_confirmed_temp_cleanup(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Clean reviewed Windows temp roots under an explicit authority policy."""

        self._require_open()
        self._only_fields(
            request,
            {"proposal_id", "roots", "authority_mode", "user_confirmed"},
        )
        proposal_id = str(request.get("proposal_id") or "").strip()
        authority_mode = str(request.get("authority_mode") or "ask_every_time").strip()
        requested_roots = request.get("roots")
        if not proposal_id or authority_mode not in {"ask_every_time", "full_access"}:
            raise ValueError("A proposal identifier and valid authority mode are required")
        if not isinstance(requested_roots, list) or not requested_roots:
            raise ValueError("Reviewed temporary-data roots are required")
        current_roots = self.prepare_temp_cleanup()
        requested_identity = [
            (str(root.get("name") or ""), os.path.normcase(str(root.get("path") or "")))
            for root in requested_roots
            if isinstance(root, Mapping)
        ]
        current_identity = [
            (str(root.get("name") or ""), os.path.normcase(str(root.get("path") or "")))
            for root in current_roots
        ]
        if requested_identity != current_identity:
            raise PermissionError(
                "Windows temporary-data roots changed after review; the proposal is no longer valid"
            )
        user_confirmed = request.get("user_confirmed") is True
        audit_request = {
            "host_action_kind": "system.clean_temp",
            "proposal_id": proposal_id,
            "roots": current_roots,
            "authority_mode": authority_mode,
            "user_confirmed": user_confirmed,
            "execution_backend": "salty_steak_bounded_filesystem_cleanup",
        }
        audit_id = self._start_audit(
            event="invoke",
            capability=TERMINAL_CAPABILITY,
            request=audit_request,
        )
        try:
            if authority_mode != "full_access" and not user_confirmed:
                raise PermissionError(
                    "Temporary-data cleanup requires confirmation in Ask every time mode"
                )
            if self.platform_name != "nt":
                raise RuntimeError("Windows temporary-data cleanup is unavailable")
            result = clean_temp_root_contents(current_roots)
            public_result = {
                **result,
                "audit_record_id": audit_id,
                "proposal_id": proposal_id,
                "host_action_kind": "system.clean_temp",
                "authority_mode": authority_mode,
            }
            self._finish_audit(audit_id, outcome="succeeded", result=public_result)
            return public_result
        except BaseException as exc:
            self._finish_audit(
                audit_id,
                outcome="denied" if isinstance(exc, PermissionError) else "failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def invoke_confirmed_file_trash(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Execute one proposal-bound Recycle Bin action.

        This intentionally does not consume the broad persisted terminal
        grant. The caller must first validate a durable, one-use Chat proposal;
        this method revalidates the exact file snapshot immediately before the
        recoverable Windows action and records it in the local automation log.
        """

        self._require_open()
        self._only_fields(
            request,
            {
                "proposal_id",
                "path",
                "expected_size_bytes",
                "expected_modified_ns",
                "user_confirmed",
                "authority_mode",
            },
        )
        proposal_id = str(request.get("proposal_id") or "").strip()
        path = str(request.get("path") or "").strip()
        if not proposal_id or not path:
            raise ValueError("A proposal identifier and exact file path are required")
        audit_request = {
            "host_action_kind": "filesystem.trash_file",
            "proposal_id": proposal_id,
            "path": path,
            "expected_size_bytes": request.get("expected_size_bytes"),
            "expected_modified_ns": request.get("expected_modified_ns"),
            "user_confirmed": request.get("user_confirmed") is True,
            "authority_mode": str(
                request.get("authority_mode") or "ask_every_time"
            ),
            "execution_backend": "windows_shell_recycle_bin",
        }
        audit_id = self._start_audit(
            event="invoke",
            capability=TERMINAL_CAPABILITY,
            request=audit_request,
        )
        try:
            authority_mode = str(
                request.get("authority_mode") or "ask_every_time"
            ).strip()
            if authority_mode not in {"ask_every_time", "full_access"}:
                raise ValueError("Invalid computer authority mode")
            if authority_mode != "full_access" and request.get("user_confirmed") is not True:
                raise PermissionError(
                    "Moving a file to the Recycle Bin requires explicit confirmation"
                )
            if self.platform_name != "nt":
                raise RuntimeError(
                    "The Windows Recycle Bin is unavailable on this platform"
                )
            result = recycle_confirmed_file(
                path,
                expected_size_bytes=int(request.get("expected_size_bytes")),
                expected_modified_ns=int(request.get("expected_modified_ns")),
                recycler=self._file_recycler,
            )
            public_result = {
                **result,
                "audit_record_id": audit_id,
                "proposal_id": proposal_id,
                "host_action_kind": "filesystem.trash_file",
            }
            self._finish_audit(audit_id, outcome="succeeded", result=public_result)
            return public_result
        except BaseException as exc:
            self._finish_audit(
                audit_id,
                outcome="denied" if isinstance(exc, PermissionError) else "failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
            raise

    def audit_records(self, *, limit: int = 100) -> list[dict[str, Any]]:
        checked_limit = max(1, min(int(limit), 500))
        rows = self.database.fetch_all(
            """
            SELECT * FROM automation_audit_records
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (checked_limit,),
        )
        return [self._public_audit(row) for row in rows]

    def close(self) -> None:
        for client in (self._uia_client, getattr(self, "_browser_client", None)):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        with self._lock:
            self._closed = True
            processes = [
                process
                for _capability, process in self._active.values()
                if process is not None
            ]
        for process in processes:
            _terminate_process(process)

    def _invoke_terminal(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
        constraints: Mapping[str, Any],
        authority_mode: str = "ask_every_time",
    ) -> dict[str, Any]:
        self._only_fields(arguments, {"argv", "working_directory", "timeout_seconds"})
        argv = self._argv(arguments.get("argv"))
        allowed_root = self._project_directory(constraints["working_directory_root"])




        unconfined = authority_mode == "full_access"
        working_directory = (
            self._any_directory(arguments.get("working_directory", allowed_root))
            if unconfined
            else self._working_directory(
                arguments.get("working_directory", allowed_root),
                allowed_root,
            )
        )
        timeout_value = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout_value, bool):
            raise ValueError("Terminal timeout_seconds must be numeric")
        try:
            timeout_seconds = float(timeout_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Terminal timeout_seconds must be numeric") from exc
        if not 0.05 <= timeout_seconds <= self.max_timeout_seconds:
            raise ValueError(
                f"Terminal timeout_seconds must be between 0.05 and {self.max_timeout_seconds:g}"
            )
        executable = self._resolve_executable(argv[0], working_directory, allowed_root)
        executed_argv = [str(executable), *argv[1:]]
        child_environment = os.environ.copy()



        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child_environment["PYTHONPYCACHEPREFIX"] = str(
            (self.artifact_root / "pycache").resolve()
        )

        started = time.monotonic()
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        with self._lock:
            if audit_id in self._revoked_invocations:
                return self._revoked_terminal_result(
                    audit_id,
                    argv,
                    executed_argv,
                    working_directory,
                    timeout_seconds,
                    started,
                )
            process = subprocess.Popen(
                executed_argv,
                cwd=str(working_directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                env=child_environment,
            )
            self._active[audit_id] = (TERMINAL_CAPABILITY, process)
        job = _WindowsJob.attach(process)
        stdout_capture: dict[str, Any] = {}
        stderr_capture: dict[str, Any] = {}
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_thread = threading.Thread(
            target=_drain_output,
            args=(process.stdout, self.max_output_bytes, stdout_capture),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_output,
            args=(process.stderr, self.max_output_bytes, stderr_capture),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                if job is not None:
                    job.terminate()
                else:
                    _terminate_process(process)
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    _terminate_process(process)
                    raise TimeoutError(
                        "Terminal process did not stop after its timeout"
                    ) from exc
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            if job is not None:
                job.close()
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            raise RuntimeError("Terminal output pipes did not close after process termination")
        if "error" in stdout_capture:
            raise RuntimeError(f"Terminal stdout capture failed: {stdout_capture['error']}")
        if "error" in stderr_capture:
            raise RuntimeError(f"Terminal stderr capture failed: {stderr_capture['error']}")
        with self._lock:
            revoked = audit_id in self._revoked_invocations
        status = (
            "revoked"
            if revoked
            else "timed_out"
            if timed_out
            else "succeeded"
            if exit_code == 0
            else "failed"
        )
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": TERMINAL_CAPABILITY,
            "status": status,
            "requested_argv": argv,
            "executed_argv": executed_argv,
            "working_directory": str(working_directory),
            "shell": False,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "exit_code": exit_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": _public_output(stdout_capture),
            "stderr": _public_output(stderr_capture),
        }

    def _revoked_terminal_result(
        self,
        audit_id: str,
        argv: list[str],
        executed_argv: list[str],
        working_directory: Path,
        timeout_seconds: float,
        started: float,
    ) -> dict[str, Any]:
        empty = _empty_output()
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": TERMINAL_CAPABILITY,
            "status": "revoked",
            "requested_argv": argv,
            "executed_argv": executed_argv,
            "working_directory": str(working_directory),
            "shell": False,
            "timeout_seconds": timeout_seconds,
            "timed_out": False,
            "exit_code": None,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "stdout": empty,
            "stderr": dict(empty),
        }

    def _invoke_screen_capture(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._only_fields(arguments, {"screen"})
        screen = arguments.get("screen", "primary")
        if screen != "primary":
            raise ValueError("Screen capture supports only screen='primary'")
        destination_root = self.artifact_root / "screenshots"
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = ensure_within(
            destination_root / f"screenshot-{new_id()}.bmp",
            destination_root,
        )
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": SCREEN_CAPTURE_CAPABILITY,
                    "status": "revoked",
                    "screen": "primary",
                    "artifact": None,
                }
        captured = self._screen_capturer(destination)
        if not destination.is_file():
            raise RuntimeError("Screen capturer did not create the screenshot artifact")
        width = int(captured.get("width") or 0)
        height = int(captured.get("height") or 0)
        if width <= 0 or height <= 0 or width * height > MAX_SCREEN_PIXELS:
            destination.unlink(missing_ok=True)
            raise RuntimeError("Screen capturer returned invalid or oversized dimensions")
        with self._lock:
            revoked = audit_id in self._revoked_invocations
        if revoked:
            destination.unlink(missing_ok=True)
            return {
                "schema": AUTOMATION_SCHEMA,
                "audit_record_id": audit_id,
                "capability": SCREEN_CAPTURE_CAPABILITY,
                "status": "revoked",
                "screen": "primary",
                "artifact": None,
            }
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": SCREEN_CAPTURE_CAPABILITY,
            "status": "succeeded",
            "screen": "primary",
            "artifact": {
                "path": str(destination),
                "format": "BMP",
                "width": width,
                "height": height,


                "source_width": int(captured.get("source_width") or width),
                "source_height": int(captured.get("source_height") or height),
                "scale_divisor": int(captured.get("scale_divisor") or 1),
                "size_bytes": destination.stat().st_size,
                "sha256": sha256_file(destination),
            },
        }

    def _invoke_input_control(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Inject one reviewed mouse or keyboard action into the local session."""

        self._only_fields(
            arguments,
            {
                "action",
                "x",
                "y",
                "button",
                "double",
                "clicks",
                "key",
                "text",
                "combo",
                "post_action_delay_ms",
            },
        )
        action = str(arguments.get("action") or "").strip().casefold()
        delay_ms = self._input_delay(arguments.get("post_action_delay_ms"))
        started = time.monotonic()
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": INPUT_CONTROL_CAPABILITY,
                    "status": "revoked",
                    "action": action,
                }

        detail: dict[str, Any] = {"action": action}
        if action == "mouse_move":
            x, y = self._point(arguments)
            normalized_x, normalized_y = _absolute_point(x, y)
            _send_input_events(
                [
                    _mouse_event(
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                        normalized_x,
                        normalized_y,
                    )
                ]
            )
            detail.update({"x": x, "y": y, "normalized": [normalized_x, normalized_y]})
        elif action == "mouse_click":
            x, y = self._point(arguments)
            button = str(arguments.get("button") or "left").strip().casefold()
            if button not in MOUSE_BUTTONS:
                raise ValueError("Mouse button must be left, right, or middle")
            if not isinstance(arguments.get("double", False), bool):
                raise ValueError("Mouse double must be true or false")
            double = bool(arguments.get("double", False))
            press, release = MOUSE_BUTTONS[button]
            normalized_x, normalized_y = _absolute_point(x, y)
            events = [
                _mouse_event(
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                    normalized_x,
                    normalized_y,
                ),
                _mouse_event(press),
                _mouse_event(release),
            ]
            if double:
                events.extend([_mouse_event(press), _mouse_event(release)])
            _send_input_events(events)
            detail.update(
                {
                    "x": x,
                    "y": y,
                    "button": button,
                    "double": double,
                    "normalized": [normalized_x, normalized_y],
                }
            )
        elif action == "mouse_scroll":
            x, y = self._point(arguments)
            clicks = arguments.get("clicks", 0)
            if isinstance(clicks, bool) or not isinstance(clicks, int):
                raise ValueError("Scroll clicks must be a whole number")
            if not -MAX_SCROLL_CLICKS <= clicks <= MAX_SCROLL_CLICKS or clicks == 0:
                raise ValueError(
                    f"Scroll clicks must be a non-zero value within {MAX_SCROLL_CLICKS}"
                )
            normalized_x, normalized_y = _absolute_point(x, y)
            _send_input_events(
                [
                    _mouse_event(
                        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                        normalized_x,
                        normalized_y,
                    ),
                    _mouse_event(MOUSEEVENTF_WHEEL, data=clicks * WHEEL_DELTA),
                ]
            )
            detail.update({"x": x, "y": y, "clicks": clicks})
        elif action == "key_press":
            virtual_key, name = self._virtual_key(arguments.get("key"))
            _send_input_events(
                [
                    _virtual_key_events(virtual_key, release=False),
                    _virtual_key_events(virtual_key, release=True),
                ]
            )
            detail.update({"key": name})
        elif action == "type_text":
            text = arguments.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError("Typed text must be non-empty")
            if len(text) > MAX_TYPED_CHARACTERS:
                raise ValueError(
                    f"Typed text cannot exceed {MAX_TYPED_CHARACTERS} characters"
                )
            if "\x00" in text:
                raise ValueError("Typed text cannot contain NUL characters")
            events: list[_Input] = []
            for character in text:


                if character == "\n":
                    events.append(_virtual_key_events(VIRTUAL_KEYS["enter"], release=False))
                    events.append(_virtual_key_events(VIRTUAL_KEYS["enter"], release=True))
                elif character != "\r":
                    events.extend(_unicode_key_events(character))
            _send_input_events(events)
            detail.update({"character_count": len(text), "event_count": len(events)})
        elif action == "key_combo":
            modifiers, virtual_key, rendered = self._key_combo(arguments.get("combo"))
            events = [
                _virtual_key_events(code, release=False) for code in modifiers
            ]
            events.append(_virtual_key_events(virtual_key, release=False))
            events.append(_virtual_key_events(virtual_key, release=True))


            events.extend(
                _virtual_key_events(code, release=True) for code in reversed(modifiers)
            )
            _send_input_events(events)
            detail.update({"combo": rendered})
        else:
            raise ValueError(f"Unsupported input action: {action!r}")



        time.sleep(delay_ms / 1000)
        with self._lock:
            revoked = audit_id in self._revoked_invocations
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": INPUT_CONTROL_CAPABILITY,
            "status": "revoked" if revoked else "succeeded",
            "post_action_delay_ms": delay_ms,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            **detail,
        }

    def _invoke_browser(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read and operate a web page structurally.

        This runs in a browser session Salty Steak owns, with its own profile.
        It never attaches to the browser the user is signed in to, and it only
        opens http and https addresses.
        """

        self._only_fields(
            arguments,
            {
                "command",
                "url",
                "element",
                "role",
                "name",
                "text",
                "href",
                "selector",
                "value",
                "exact",
                "editable",
                "visible",
                "enabled",
                "limit",
                "text_limit",
            },
        )
        if self._browser_client is None:
            raise RuntimeError(
                "The browser host is not available in this build, so web pages "
                "cannot be read structurally."
            )
        command = str(arguments.get("command") or "").strip().casefold()
        if command not in BROWSER_COMMANDS:
            raise ValueError(
                "Browser command must be one of: " + ", ".join(sorted(BROWSER_COMMANDS))
            )
        payload = {
            key: value
            for key, value in arguments.items()
            if key != "command" and value is not None
        }

        started = time.monotonic()
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": BROWSER_CAPABILITY,
                    "status": "revoked",
                    "command": command,
                }

        try:
            result = self._browser_client.call(command, payload)
        except BrowserError as error:
            return {
                "schema": AUTOMATION_SCHEMA,
                "audit_record_id": audit_id,
                "capability": BROWSER_CAPABILITY,
                "status": "failed",
                "command": command,
                "failure_kind": error.kind,
                "error": str(error),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }

        with self._lock:
            revoked = audit_id in self._revoked_invocations
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": BROWSER_CAPABILITY,
            "status": "revoked" if revoked else "succeeded",
            "command": command,
            "mutating": command in BROWSER_MUTATING_COMMANDS,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            **result,
        }

    def _invoke_ui_automation(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read and operate application controls through Windows accessibility.

        This is the rung above vision: a named button can be found and pressed
        as a control rather than located in an image and clicked at a
        coordinate, which is both faster and far less likely to hit the wrong
        thing.
        """

        self._only_fields(
            arguments,
            {
                "command",
                "window",
                "process_id",
                "window_handle",
                "element",
                "name",
                "automation_id",
                "control_type",
                "class_name",
                "pattern",
                "exact",
                "enabled_only",
                "visible_only",
                "limit",
                "depth",
                "max_nodes",
                "value",
                "amount",
                "horizontal",
            },
        )
        if self._uia_client is None:
            raise RuntimeError(
                "The UI Automation host is not available in this build, so "
                "application controls cannot be read semantically."
            )
        command = str(arguments.get("command") or "").strip().casefold()
        if command not in UIA_COMMANDS:
            raise ValueError(
                "UI Automation command must be one of: "
                + ", ".join(sorted(UIA_COMMANDS))
            )
        payload = {
            key: value
            for key, value in arguments.items()
            if key != "command" and value is not None
        }

        started = time.monotonic()
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": UI_AUTOMATION_CAPABILITY,
                    "status": "revoked",
                    "command": command,
                }

        try:
            result = self._uia_client.call(command, payload)
        except UiAutomationError as error:



            return {
                "schema": AUTOMATION_SCHEMA,
                "audit_record_id": audit_id,
                "capability": UI_AUTOMATION_CAPABILITY,
                "status": "failed",
                "command": command,
                "failure_kind": error.kind,
                "error": str(error),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }

        with self._lock:
            revoked = audit_id in self._revoked_invocations
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": UI_AUTOMATION_CAPABILITY,
            "status": "revoked" if revoked else "succeeded",
            "command": command,
            "mutating": command in UIA_MUTATING_COMMANDS,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            **result,
        }

    def _invoke_window_control(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """List, focus, or close real windows without touching the screen.

        This is the structured rung between running a command and resorting to
        pixels: Windows already knows what is open and what it is called, so a
        task that only needs to reach an application should ask rather than
        look.
        """

        self._only_fields(arguments, {"action", "title", "handle"})
        action = str(arguments.get("action") or "list").strip().casefold()
        if action not in {"list", "focus", "close"}:
            raise ValueError("Window action must be list, focus, or close")
        started = time.monotonic()
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": WINDOW_CONTROL_CAPABILITY,
                    "status": "revoked",
                    "action": action,
                }

        windows = _enumerate_windows()
        if action == "list":
            return {
                "schema": AUTOMATION_SCHEMA,
                "audit_record_id": audit_id,
                "capability": WINDOW_CONTROL_CAPABILITY,
                "status": "succeeded",
                "action": action,
                "window_count": len(windows),
                "windows": windows,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }

        handle_value = arguments.get("handle")
        title = arguments.get("title")
        if handle_value is not None:
            if isinstance(handle_value, bool) or not isinstance(handle_value, int):
                raise ValueError("Window handle must be a whole number")
            matches = [item for item in windows if item["handle"] == int(handle_value)]
            if not matches:
                raise ValueError(f"No open window has handle {handle_value}")
        else:
            if not isinstance(title, str) or not title.strip():
                raise ValueError("A window title or handle is required")
            matches = _match_windows(windows, title)
            if not matches:
                raise ValueError(f"No open window title contains: {title!r}")
            if len(matches) > 1:


                raise ValueError(
                    f"{len(matches)} windows match {title!r}: "
                    + "; ".join(item["title"] for item in matches[:5])
                    + ". Use an exact title or a handle."
                )
        target = matches[0]
        if action == "focus":
            _focus_window(target["handle"])
        else:
            _close_window(target["handle"])
        time.sleep(DEFAULT_INPUT_DELAY_MS / 1000)
        with self._lock:
            revoked = audit_id in self._revoked_invocations
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": WINDOW_CONTROL_CAPABILITY,
            "status": "revoked" if revoked else "succeeded",
            "action": action,
            "window": target,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def _invoke_application_launch(
        self,
        audit_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Open an installed application, a link, or a file with its handler."""

        self._only_fields(arguments, {"target", "arguments", "wait_ms"})
        target, resolution = _resolve_launch_target(arguments.get("target"))
        launch_arguments = arguments.get("arguments")
        if launch_arguments is not None:
            if not isinstance(launch_arguments, str):
                raise ValueError("Launch arguments must be text")
            if len(launch_arguments) > MAX_ARGUMENT_CHARACTERS:
                raise ValueError("Launch arguments are too long")
            if any(character in launch_arguments for character in ("\x00", "\r", "\n")):
                raise ValueError("Launch arguments cannot contain control characters")
        wait_value = arguments.get("wait_ms", DEFAULT_LAUNCH_WAIT_MS)
        if isinstance(wait_value, bool) or not isinstance(wait_value, int):
            raise ValueError("Launch wait_ms must be a whole number")
        if not 0 <= wait_value <= MAX_LAUNCH_WAIT_MS:
            raise ValueError(f"Launch wait_ms must be between 0 and {MAX_LAUNCH_WAIT_MS}")

        started = time.monotonic()
        with self._lock:
            if audit_id in self._revoked_invocations:
                return {
                    "schema": AUTOMATION_SCHEMA,
                    "audit_record_id": audit_id,
                    "capability": APPLICATION_LAUNCH_CAPABILITY,
                    "status": "revoked",
                    "target": target,
                }
        outcome = _shell_execute(target, launch_arguments)


        if outcome <= 32:
            raise RuntimeError(
                SHELL_EXECUTE_ERRORS.get(outcome, f"Windows could not open the target ({outcome})")
            )
        if wait_value:
            time.sleep(wait_value / 1000)
        with self._lock:
            revoked = audit_id in self._revoked_invocations
        return {
            "schema": AUTOMATION_SCHEMA,
            "audit_record_id": audit_id,
            "capability": APPLICATION_LAUNCH_CAPABILITY,
            "status": "revoked" if revoked else "succeeded",
            "target": target,
            "target_resolution": resolution,
            "arguments": launch_arguments,
            "wait_ms": wait_value,
            "shell_execute_result": outcome,
            "waited_for_exit": False,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    @staticmethod
    def _input_delay(value: object) -> int:
        if value is None:
            return DEFAULT_INPUT_DELAY_MS
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("post_action_delay_ms must be a whole number")
        if not 0 <= value <= MAX_INPUT_DELAY_MS:
            raise ValueError(
                f"post_action_delay_ms must be between 0 and {MAX_INPUT_DELAY_MS}"
            )
        return int(value)

    @staticmethod
    def _point(arguments: Mapping[str, Any]) -> tuple[int, int]:
        point: list[int] = []
        for axis in ("x", "y"):
            value = arguments.get(axis)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Screen coordinate {axis} must be a whole number")
            if not -32_768 <= value <= 32_767:
                raise ValueError(f"Screen coordinate {axis} is outside the screen range")
            point.append(int(value))
        return point[0], point[1]

    @staticmethod
    def _virtual_key(value: object) -> tuple[int, str]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A key name is required")
        name = value.strip().casefold()
        if name not in VIRTUAL_KEYS:
            raise ValueError(f"Unsupported key name: {value!r}")
        return VIRTUAL_KEYS[name], name

    @staticmethod
    def _key_combo(value: object) -> tuple[list[int], int, str]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A key combination is required")
        parts = [piece.strip().casefold() for piece in value.split("+") if piece.strip()]
        if len(parts) < 2:
            raise ValueError("A key combination needs at least one modifier and one key")
        modifiers: list[int] = []
        for piece in parts[:-1]:
            if piece not in MODIFIER_KEYS:
                raise ValueError(f"Unsupported combination modifier: {piece!r}")
            modifiers.append(MODIFIER_KEYS[piece])
        final = parts[-1]
        if final in VIRTUAL_KEYS:
            key = VIRTUAL_KEYS[final]
        elif len(final) == 1:


            key = ord(final.upper())
        else:
            raise ValueError(f"Unsupported combination key: {final!r}")
        return modifiers, key, "+".join(parts)

    def _constraints_valid(
        self,
        capability: str,
        constraints: object,
    ) -> tuple[bool, str | None]:
        try:
            if not isinstance(constraints, Mapping):
                raise ValueError("constraints are not an object")
            if capability == TERMINAL_CAPABILITY:
                root = constraints.get("working_directory_root")
                if not isinstance(root, str) or not root:
                    raise ValueError("working directory root is missing")
                self._project_directory(root)
            elif capability == SCREEN_CAPTURE_CAPABILITY:
                if constraints.get("screen") != "primary":
                    raise ValueError("screen is not primary")
                output_root = Path(str(constraints.get("output_root") or "")).resolve()
                ensure_within(output_root, self.artifact_root)
            else:
                scope = constraints.get("scope")
                if not isinstance(scope, str) or not scope:
                    raise ValueError("capability scope is missing")
        except (OSError, TypeError, ValueError) as exc:
            return False, str(exc)
        return True, None

    def _project_directory(self, value: object) -> Path:
        if not isinstance(value, (str, Path)):
            raise ValueError("Working-directory root must be a path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve(strict=True)
        ensure_within(resolved, self.project_root)
        if not resolved.is_dir():
            raise ValueError(f"Working-directory root is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _any_directory(value: object) -> Path:
        """Resolve an existing directory anywhere, for full-access execution."""

        if not isinstance(value, (str, Path)):
            raise ValueError("Terminal working_directory must be a path")
        resolved = Path(value).resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"Terminal working directory is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _working_directory(value: object, allowed_root: Path) -> Path:
        if not isinstance(value, (str, Path)):
            raise ValueError("Terminal working_directory must be a path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = allowed_root / candidate
        resolved = candidate.resolve(strict=True)
        ensure_within(resolved, allowed_root)
        if not resolved.is_dir():
            raise ValueError(f"Terminal working directory is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _argv(value: object) -> list[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("Terminal argv must be a JSON array, not a shell string")
        argv = list(value)
        if not argv or len(argv) > MAX_ARGUMENTS:
            raise ValueError(f"Terminal argv must contain 1 to {MAX_ARGUMENTS} items")
        if any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("Every terminal argv item must be non-empty text")
        if any("\x00" in item for item in argv):
            raise ValueError("Terminal argv items cannot contain NUL characters")
        if sum(len(item) for item in argv) > MAX_ARGUMENT_CHARACTERS:
            raise ValueError(
                f"Terminal argv exceeds {MAX_ARGUMENT_CHARACTERS} characters"
            )
        return argv

    @staticmethod
    def _resolve_executable(
        value: str,
        working_directory: Path,
        allowed_root: Path,
    ) -> Path:
        candidate = Path(value)
        has_path = candidate.is_absolute() or bool(candidate.drive) or any(
            separator in value for separator in ("/", "\\")
        )
        if has_path:
            if candidate.drive and not candidate.is_absolute():
                raise ValueError("Drive-relative executable paths are not allowed")
            relative = not candidate.is_absolute()
            resolved = (
                candidate if candidate.is_absolute() else working_directory / candidate
            ).resolve(strict=True)
            if relative:
                ensure_within(resolved, allowed_root)
        else:
            located = shutil.which(value)
            if located is None:
                raise FileNotFoundError(f"Terminal executable does not exist: {value}")
            resolved = Path(located).resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"Terminal executable is not a file: {resolved}")
        return resolved

    def _grant_row(self, capability: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT * FROM automation_grants WHERE capability = ?",
            (capability,),
        )
        if row is None:
            raise RuntimeError(f"Automation grant record is missing: {capability}")
        return row

    @staticmethod
    def _public_grant(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "capability": str(row["capability"]),
            "granted": bool(row["enabled"]),
            "constraints": parse_json(str(row["constraints_json"]), {}),
            "granted_at": row.get("granted_at"),
            "revoked_at": row.get("revoked_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _public_audit(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "event": str(row["event"]),
            "capability": str(row["capability"]),
            "outcome": str(row["outcome"]),
            "request": parse_json(str(row["request_json"]), {}),
            "result": parse_json(row.get("result_json"), None),
            "error": parse_json(row.get("error_json"), None),
            "created_at": str(row["created_at"]),
            "completed_at": row.get("completed_at"),
        }

    def _start_audit(
        self,
        *,
        event: str,
        capability: str,
        request: Mapping[str, Any],
    ) -> str:
        identifier = new_id()
        now = utc_now()
        self.database.execute(
            """
            INSERT INTO automation_audit_records(
                id, event, capability, outcome, request_json,
                result_json, error_json, created_at, completed_at
            ) VALUES (?, ?, ?, 'pending', ?, NULL, NULL, ?, NULL)
            """,
            (identifier, event, capability, json_text(dict(request)), now),
        )
        return identifier

    def _finish_audit(
        self,
        identifier: str,
        *,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        updated = self.database.execute(
            """
            UPDATE automation_audit_records
            SET outcome = ?, result_json = ?, error_json = ?, completed_at = ?
            WHERE id = ? AND outcome = 'pending'
            """,
            (
                outcome,
                json_text(dict(result)) if result is not None else None,
                json_text(dict(error)) if error is not None else None,
                utc_now(),
                identifier,
            ),
        )
        if updated != 1:
            raise RuntimeError(f"Automation audit record could not be completed: {identifier}")

    @staticmethod
    def _insert_completed_audit(
        connection: Any,
        *,
        event: str,
        capability: str,
        outcome: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        created_at: str,
    ) -> str:
        identifier = new_id()
        connection.execute(
            """
            INSERT INTO automation_audit_records(
                id, event, capability, outcome, request_json,
                result_json, error_json, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                identifier,
                event,
                capability,
                outcome,
                json_text(dict(request)),
                json_text(dict(result)),
                created_at,
                created_at,
            ),
        )
        return identifier

    @staticmethod
    def _capability_list(value: object) -> list[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("Automation capabilities must be an array")
        capabilities = list(value)
        if not capabilities:
            raise ValueError("At least one automation capability is required")
        if any(not isinstance(item, str) or item not in CAPABILITIES for item in capabilities):
            raise ValueError("Automation capabilities contain an unknown value")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("Automation capabilities cannot contain duplicates")
        return capabilities

    @staticmethod
    def _only_fields(request: Mapping[str, Any], allowed: set[str]) -> None:
        unknown = sorted(str(key) for key in request if key not in allowed)
        if unknown:
            raise ValueError(f"Unknown automation fields: {unknown}")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Automation broker is closed")


def _drain_output(
    stream: BinaryIO,
    limit: int,
    target: dict[str, Any],
) -> None:
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


def _public_output(capture: Mapping[str, Any]) -> dict[str, Any]:
    if not capture:
        return _empty_output()
    return {
        "text": str(capture["text"]),
        "retained_base64": str(capture["retained_base64"]),
        "encoding": str(capture["encoding"]),
        "bytes_total": int(capture["bytes_total"]),
        "bytes_retained": int(capture["bytes_retained"]),
        "truncated": bool(capture["truncated"]),
        "sha256": str(capture["sha256"]),
    }


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        return


class _WindowsJob:
    """Best-effort kill-on-close Windows job for command process trees."""

    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> _WindowsJob | None:
        if os.name != "nt" or not hasattr(process, "_handle"):
            return None
        try:
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
            configured = kernel32.SetInformationJobObject(
                handle,
                9,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            assigned = kernel32.AssignProcessToJobObject(
                handle,
                ctypes.c_void_p(int(process._handle)),
            )
            if not configured or not assigned:
                kernel32.CloseHandle(handle)
                return None
            return cls(int(handle))
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def terminate(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1)

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = 0


_ULONG_PTR = (
    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
)
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WHEEL_DELTA = 120
_ABSOLUTE_RANGE = 65_535

MOUSE_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}



VIRTUAL_KEYS = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13,
    "capslock": 0x14, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "numlock": 0x90, "scrolllock": 0x91,
}


_EXTENDED_KEYS = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x90}
)
MODIFIER_KEYS = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B, "windows": 0x5B,
}

SHELL_EXECUTE_ERRORS = {
    0: "The operating system is out of memory or resources.",
    2: "The specified file was not found.",
    3: "The specified path was not found.",
    5: "Windows denied access to the specified file.",
    8: "There is not enough memory to complete the operation.",
    26: "A sharing violation occurred.",
    27: "The file name association is incomplete or invalid.",
    28: "The DDE transaction timed out.",
    29: "The DDE transaction failed.",
    30: "The DDE transaction could not be completed because other transactions were busy.",
    31: "No application is associated with this file type.",
    32: "The associated application could not be found.",
}


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(_ULONG_PTR)),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(_ULONG_PTR)),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", wintypes.DWORD), ("payload", _InputUnion)]


def _mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _Input:
    return _Input(
        type=INPUT_MOUSE,
        payload=_InputUnion(
            mi=_MouseInput(
                dx=int(dx),
                dy=int(dy),
                mouseData=ctypes.c_uint32(int(data) & 0xFFFFFFFF).value,
                dwFlags=int(flags),
                time=0,
                dwExtraInfo=ctypes.pointer(_ULONG_PTR(0)),
            )
        ),
    )


def _key_event(*, virtual_key: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(
        type=INPUT_KEYBOARD,
        payload=_InputUnion(
            ki=_KeyboardInput(
                wVk=int(virtual_key),
                wScan=int(scan),
                dwFlags=int(flags),
                time=0,
                dwExtraInfo=ctypes.pointer(_ULONG_PTR(0)),
            )
        ),
    )


def _virtual_key_events(virtual_key: int, *, release: bool) -> _Input:
    flags = KEYEVENTF_KEYUP if release else 0
    if virtual_key in _EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return _key_event(virtual_key=virtual_key, flags=flags)


def _utf16_units(character: str) -> tuple[int, ...]:
    """Split one character into UTF-16 code units.

    Characters outside the Basic Multilingual Plane occupy a surrogate pair,
    and each half must be injected as its own keyboard event.
    """

    encoded = character.encode("utf-16-le")
    return tuple(
        int.from_bytes(encoded[index : index + 2], "little")
        for index in range(0, len(encoded), 2)
    )


def _unicode_key_events(character: str) -> list[_Input]:
    """Return press/release events for one character, layout independently."""

    events: list[_Input] = []
    for unit in _utf16_units(character):
        events.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE))
        events.append(_key_event(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def _send_input_events(events: Sequence[_Input]) -> None:
    """Inject a prepared event batch, failing closed when Windows rejects it."""

    if not events:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(_Input),
        ctypes.c_int,
    ]
    user32.SendInput.restype = wintypes.UINT
    batch = (_Input * len(events))(*events)
    injected = int(
        user32.SendInput(len(events), batch, ctypes.sizeof(_Input))
    )
    if injected != len(events):
        error = ctypes.get_last_error()
        raise RuntimeError(
            "Windows rejected the input events "
            f"({injected} of {len(events)} accepted): {ctypes.WinError(error).strerror}"
        )


WM_CLOSE = 0x0010
SW_RESTORE = 9
SW_MINIMIZE = 6


def _enumerate_windows() -> list[dict[str, Any]]:
    """List visible, titled top-level windows.

    Windows exposes real window identity through documented user32 calls, so a
    task that only needs to find or focus an application never has to look at
    pixels to do it.
    """

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    windows: list[dict[str, Any]] = []

    def visit(handle: int, _parameter: int) -> bool:
        if len(windows) >= MAX_ENUMERATED_WINDOWS:
            return False
        if not user32.IsWindowVisible(handle):
            return True
        length = int(user32.GetWindowTextLengthW(handle))
        if length < 1 or length > MAX_WINDOW_TITLE_CHARACTERS:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        title = buffer.value.strip()
        if not title:
            return True
        process_id = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        windows.append(
            {
                "handle": int(handle),
                "title": title,
                "process_id": int(process_id.value),
            }
        )
        return True

    if not user32.EnumWindows(callback_type(visit), 0) and not windows:
        raise RuntimeError(
            "Windows could not enumerate open windows: "
            + ctypes.WinError(ctypes.get_last_error()).strerror
        )
    return windows


def _match_windows(windows: Sequence[Mapping[str, Any]], title: str) -> list[dict[str, Any]]:
    """Rank windows against a title fragment, exact matches first."""

    needle = str(title).strip().casefold()
    exact = [item for item in windows if str(item["title"]).casefold() == needle]
    if exact:
        return [dict(item) for item in exact]
    return [
        dict(item)
        for item in windows
        if needle and needle in str(item["title"]).casefold()
    ]


def _focus_window(handle: int) -> None:
    """Bring one window to the foreground.

    Windows restricts foreground changes to the active input thread, so the
    calling thread is attached to the target's before the request.
    """

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    if user32.IsIconic(handle):
        user32.ShowWindow(handle, SW_RESTORE)
    foreground = user32.GetForegroundWindow()
    current = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(handle, None)
    other = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = bool(other) and other != current and user32.AttachThreadInput(current, other, True)
    try:
        user32.SetForegroundWindow(handle)
    finally:
        if attached:
            user32.AttachThreadInput(current, other, False)
    _ = target


def _close_window(handle: int) -> None:
    """Ask a window to close the same way its own close button does."""

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL


    if not user32.PostMessageW(handle, WM_CLOSE, 0, 0):
        raise RuntimeError(
            "Windows refused the close request: "
            + ctypes.WinError(ctypes.get_last_error()).strerror
        )


def _app_paths_executable(name: str) -> str | None:
    """Resolve an application by its registered Windows App Paths entry."""

    if os.name != "nt":
        return None
    import winreg

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        resolved = str(value or "").strip().strip('"')
        if resolved:
            return resolved
    return None


def _resolve_launch_target(value: object) -> tuple[str, str]:
    """Resolve a launch request to an exact target and how it was found.

    Unlike terminal execution this deliberately has no working-directory
    confinement, so the accepted shapes are kept narrow and explicit.
    """

    if not isinstance(value, str):
        raise ValueError("Launch target must be text")
    target = value.strip()
    if not target or len(target) > MAX_TARGET_CHARACTERS:
        raise ValueError(
            f"Launch target must contain 1 to {MAX_TARGET_CHARACTERS} characters"
        )
    if any(character in target for character in ("\x00", "\r", "\n")):
        raise ValueError("Launch target cannot contain control characters")

    lowered = target.casefold()
    if lowered.startswith(("http://", "https://")):
        return target, "url"
    if target.startswith("\\\\"):
        raise ValueError("Network share paths cannot be launched")



    separator = target.find(":")
    if separator >= 0 and not (separator == 1 and target[0].isalpha()):
        raise ValueError("Only http and https links can be opened")

    candidate = Path(target)
    if candidate.is_absolute() or candidate.drive:
        resolved = candidate.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Launch target does not exist: {resolved}")
        return str(resolved), "path"

    if any(separator in target for separator in ("/", "\\")):
        raise ValueError("Relative launch paths are not accepted; use an absolute path")

    located = shutil.which(target)
    if located:
        return str(Path(located).resolve()), "search_path"
    registered = _app_paths_executable(target)
    if registered:
        return registered, "app_paths_registry"
    raise FileNotFoundError(f"No installed application matches: {target}")


def _shell_execute(target: str, arguments: str | None) -> int:
    """Open a target with its registered handler and return the raw result."""

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]


    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    outcome = shell32.ShellExecuteW(None, "open", target, arguments, None, 1)
    return int(ctypes.cast(outcome, ctypes.c_void_p).value or 0)


def _primary_screen_size() -> tuple[int, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    width = int(user32.GetSystemMetrics(0))
    height = int(user32.GetSystemMetrics(1))
    if width <= 0 or height <= 0:
        raise RuntimeError("Windows reported invalid primary-screen dimensions")
    return width, height


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    """Map screen pixels onto the 0..65535 space MOUSEEVENTF_ABSOLUTE expects."""

    width, height = _primary_screen_size()
    clamped_x = max(0, min(int(x), width - 1))
    clamped_y = max(0, min(int(y), height - 1))
    return (
        int(clamped_x * _ABSOLUTE_RANGE / width),
        int(clamped_y * _ABSOLUTE_RANGE / height),
    )


def _capture_dimensions(width: int, height: int) -> tuple[int, int, int]:
    """Pick capture dimensions that stay inside the artifact size budget.

    A capture is written as an uncompressed 32-bit BMP, so a 4K screen would
    produce roughly 33 MB and exceed what the vision runtime accepts. The
    smallest whole-number divisor that fits the budget is used so the aspect
    ratio is preserved and the scale is easy to reverse when reading
    coordinates back off the image.
    """

    divisor = 1
    while (width // divisor) * (height // divisor) * 4 > MAX_SCREENSHOT_BYTES:
        divisor += 1
        if divisor > 16:
            raise RuntimeError("The primary screen cannot be reduced to a usable capture")
    return max(1, width // divisor), max(1, height // divisor), divisor


def _capture_primary_screen_bmp(destination: Path) -> dict[str, Any]:
    """Capture the primary Windows screen to BMP using only Win32 GDI."""

    if os.name != "nt":
        raise RuntimeError("Primary-screen capture requires Windows")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    source_width = int(user32.GetSystemMetrics(0))
    source_height = int(user32.GetSystemMetrics(1))
    if (
        source_width <= 0
        or source_height <= 0
        or source_width * source_height > MAX_SCREEN_PIXELS
    ):
        raise RuntimeError("Primary-screen dimensions are invalid or exceed the capture limit")
    width, height, scale = _capture_dimensions(source_width, source_height)

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise RuntimeError("Windows could not open the primary-screen device context")
    memory_dc = None
    bitmap = None
    previous = None
    try:
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            raise RuntimeError("Windows could not create a capture device context")
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not bitmap:
            raise RuntimeError("Windows could not create a capture bitmap")
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        previous = gdi32.SelectObject(memory_dc, bitmap)
        if not previous:
            raise RuntimeError("Windows could not select the capture bitmap")
        if scale == 1:
            copied = gdi32.BitBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                screen_dc,
                0,
                0,
                0x00CC0020 | 0x40000000,
            )
        else:
            gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
            gdi32.SetStretchBltMode.restype = ctypes.c_int
            gdi32.StretchBlt.argtypes = [
                wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.DWORD,
            ]
            gdi32.StretchBlt.restype = wintypes.BOOL



            gdi32.SetStretchBltMode(memory_dc, 4)
            gdi32.SetBrushOrgEx(memory_dc, 0, 0, None)
            copied = gdi32.StretchBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                screen_dc,
                0,
                0,
                source_width,
                source_height,
                0x00CC0020 | 0x40000000,
            )
        if not copied:
            raise RuntimeError("Windows could not copy pixels from the primary screen")

        pixel_bytes = width * height * 4
        info_header = struct.pack(
            "<IiiHHIIiiII",
            40,
            width,
            height,
            1,
            32,
            0,
            pixel_bytes,
            0,
            0,
            0,
            0,
        )
        info_buffer = ctypes.create_string_buffer(info_header)
        pixels = ctypes.create_string_buffer(pixel_bytes)
        scanlines = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixels,
            info_buffer,
            0,
        )
        if scanlines != height:
            raise RuntimeError("Windows returned an incomplete primary-screen capture")
        offset = 14 + len(info_header)
        file_header = struct.pack(
            "<2sIHHI",
            b"BM",
            offset + pixel_bytes,
            0,
            0,
            offset,
        )
        atomic_write_bytes(destination, file_header + info_header + pixels.raw)
        return {
            "width": width,
            "height": height,
            "format": "BMP",
            "source_width": source_width,
            "source_height": source_height,
            "scale_divisor": scale,
        }
    finally:
        if previous and memory_dc:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
