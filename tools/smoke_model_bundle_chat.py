"""Exercise one selected local model bundle through Application and ChatService.

This is an acceptance probe, not a performance benchmark.  It verifies the
model identity, correlated operation result, persisted message provenance,
database integrity, and normal worker shutdown in an isolated workspace.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psutil


def _resolved_path(value: str) -> Path | None:
    try:
        return Path(value or os.getcwd()).resolve()
    except (OSError, RuntimeError):
        return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _module_locations(module: object) -> list[Path]:
    locations: list[Path] = []
    module_file = getattr(module, "__file__", None)
    if module_file:
        resolved = _resolved_path(str(module_file))
        if resolved is not None:
            locations.append(resolved)
    for item in getattr(module, "__path__", ()):
        resolved = _resolved_path(str(item))
        if resolved is not None:
            locations.append(resolved)
    return locations


def import_application_from_project_root(
    project_root: Path,
) -> tuple[type[Any], dict[str, Any]]:
    """Import ``Application`` exclusively from ``project_root``.

    The smoke probe may itself live in a source checkout while validating a
    sealed package.  Importing the backend at module-load time would therefore
    silently exercise the checkout instead of the package.  This boundary
    rejects any preloaded ``app`` modules, removes every other import path that
    exposes an ``app`` package, imports from the requested root, and verifies
    every loaded ``app`` module resolves beneath that exact root.
    """

    project_root = project_root.resolve()
    expected_app_root = (project_root / "app").resolve()
    expected_application = (
        expected_app_root / "backend" / "application.py"
    ).resolve()
    if not expected_application.is_file():
        raise RuntimeError(
            "requested project root does not contain "
            f"app/backend/application.py: {project_root}"
        )

    preloaded = sorted(
        name for name in sys.modules if name == "app" or name.startswith("app.")
    )
    if preloaded:
        details = {
            name: [str(path) for path in _module_locations(sys.modules[name])]
            for name in preloaded
        }
        raise RuntimeError(
            "refusing already-imported app module leakage before isolated "
            f"project-root import: {json.dumps(details, sort_keys=True)}"
        )

    original_sys_path = list(sys.path)
    isolated_sys_path: list[str] = [str(project_root)]
    removed_app_paths: list[str] = []
    for entry in original_sys_path:
        resolved = _resolved_path(entry)
        if resolved is not None and resolved == project_root:
            continue
        if resolved is not None and (resolved / "app").is_dir():
            removed_app_paths.append(str(resolved))
            continue
        isolated_sys_path.append(entry)

    imported_before = set(sys.modules)
    # A release smoke must be observational.  Do not let a caller that omitted
    # ``python -B`` populate the sealed package with unmanifested bytecode while
    # importing its backend.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.path[:] = isolated_sys_path
    importlib.invalidate_caches()
    try:
        module = importlib.import_module("app.backend.application")
        application = getattr(module, "Application")
        module_file = _resolved_path(str(getattr(module, "__file__", "")))
        if module_file != expected_application:
            raise RuntimeError(
                "Application module resolved outside requested project root: "
                f"expected {expected_application}, got {module_file}"
            )

        loaded_app_modules: dict[str, list[str]] = {}
        for name, loaded_module in sorted(sys.modules.items()):
            if name != "app" and not name.startswith("app."):
                continue
            locations = _module_locations(loaded_module)
            loaded_app_modules[name] = [str(path) for path in locations]
            if not locations or any(
                not _is_within(path, expected_app_root) for path in locations
            ):
                raise RuntimeError(
                    "app module resolved outside requested project root: "
                    f"{name} -> {loaded_app_modules[name]}"
                )

        defining_module = sys.modules.get(getattr(application, "__module__", ""))
        defining_locations = (
            _module_locations(defining_module) if defining_module is not None else []
        )
        if not defining_locations or any(
            not _is_within(path, expected_app_root) for path in defining_locations
        ):
            raise RuntimeError(
                "Application class was not defined by the requested project root"
            )
        return application, {
            "requested_project_root": str(project_root),
            "application_module": str(module_file),
            "application_class_module": getattr(application, "__module__", None),
            "removed_competing_app_paths": removed_app_paths,
            "loaded_app_modules": loaded_app_modules,
            "isolated_import_verified": True,
        }
    except BaseException:
        for name in set(sys.modules) - imported_before:
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.path[:] = original_sys_path
        importlib.invalidate_caches()
        raise


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _final_answer(value: object) -> str:
    text = str(value or "")
    if "<think>" not in text:
        return text
    if "</think>" not in text:
        return ""
    return text.rsplit("</think>", 1)[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--expected-model-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-profile-id", required=True)
    parser.add_argument("--context-window-tokens", type=int, default=32_768)
    parser.add_argument("--expected-runtime-context-limit", type=int, default=65_536)
    parser.add_argument("--maximum-output-mode", choices=("automatic", "manual"), default="manual")
    parser.add_argument("--maximum-output-tokens", type=int, default=32)
    parser.add_argument("--reasoning-mode", choices=("instant", "cooking"), default="instant")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--top-k", type=int, default=40)
    parser.add_argument("--repetition-penalty", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--prompt",
        default=None,
        help="Exact user prompt. Omit to retain the historical exact-response prompt.",
    )
    parser.add_argument("--expected-response", default="READY")
    parser.add_argument(
        "--response-check",
        choices=("exact", "contains", "nonempty", "final_exact"),
        default="exact",
    )
    parser.add_argument("--operation-timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    workspace = args.workspace.resolve()
    result_path = args.result.resolve()
    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    os.environ["SALTY_POTATO_BUILD_ID"] = "source-model-bundle-chat-acceptance"

    started = time.perf_counter()
    application: Any | None = None
    application_import: dict[str, Any] = {
        "requested_project_root": str(project_root),
        "isolated_import_verified": False,
    }
    worker_pid: int | None = None
    close_completed = False
    close_error: str | None = None
    result: dict[str, Any]
    checks: dict[str, bool] = {}
    try:
        Application, application_import = import_application_from_project_root(
            project_root
        )
        application = Application(project_root, recover_operations=True)
        schema = application.database.fetch_one("PRAGMA user_version")
        integrity = application.database.fetch_one("PRAGMA integrity_check")
        foreign_keys = application.database.fetch_all("PRAGMA foreign_key_check")
        status_before = application.chat_status()
        conversation = application.create_conversation()
        application.rename_conversation(conversation["id"], "Model bundle acceptance")
        generation_settings = {
            "context_window_tokens": args.context_window_tokens,
            "maximum_output_mode": args.maximum_output_mode,
            "maximum_output_tokens": args.maximum_output_tokens,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "repetition_penalty": args.repetition_penalty,
            "seed": args.seed,
            "system_prompt": "",
            "stop_sequences": [],
            "reasoning_mode": args.reasoning_mode,
        }
        user_prompt = args.prompt or (
            f"Reply with exactly {args.expected_response}. Do not add any other text."
        )
        operation = application.send_message(
            conversation["id"],
            user_prompt,
            request_key=None,
            generation_settings=generation_settings,
        )
        completed = application.operations.wait(
            operation["id"], timeout=args.operation_timeout_seconds
        )
        saved = application.get_conversation(conversation["id"])
        assistant_messages = [
            message for message in saved["messages"] if message["role"] == "assistant"
        ]
        assistant = assistant_messages[-1] if assistant_messages else None
        details = dict((assistant or {}).get("technical_details") or {})
        status_after = application.chat_status()
        runtime = (
            application.model_bundle_runtime.describe()
            if application.model_bundle_runtime is not None
            else None
        )
        worker_pid = int((runtime or {}).get("worker_pid") or 0) or None
        active = application.database.fetch_one(
            """
            SELECT runtime_id, target_kind, target_id, profile_id, source_sha256
            FROM active_chat_runtime WHERE singleton = 1
            """
        )
        response_text = _normalise_text((assistant or {}).get("content"))
        expected_text = _normalise_text(args.expected_response)
        response_matches = (
            _normalise_text(_final_answer((assistant or {}).get("content")))
            == expected_text
            if args.response_check == "final_exact"
            else bool(response_text)
            if args.response_check == "nonempty"
            else expected_text in response_text
            if args.response_check == "contains"
            else response_text == expected_text
        )
        checks = {
            "application_import_isolated": application_import.get(
                "isolated_import_verified"
            )
            is True,
            "operation_completed": completed.get("state") == "completed",
            "single_assistant_message": len(assistant_messages) == 1,
            "response_matches": response_matches,
            "runtime_ready": status_after.get("runtime_ready") is True,
            "status_target_matches": status_after.get("active_target_id")
            == args.expected_model_id,
            "runtime_source_matches": (runtime or {}).get("verified_source_sha256")
            == args.expected_source_sha256,
            "runtime_profile_matches": ((runtime or {}).get("profile") or {}).get("profile_id")
            == args.expected_profile_id,
            "runtime_context_matches": int(
                ((runtime or {}).get("profile") or {}).get("context_limit") or 0
            )
            == args.expected_runtime_context_limit,
            "message_target_matches": (assistant or {}).get("target_id")
            == args.expected_model_id,
            "message_source_matches": (assistant or {}).get("source_sha256")
            == args.expected_source_sha256,
            "message_profile_matches": (assistant or {}).get("runtime_profile_id")
            == args.expected_profile_id,
            "details_target_matches": details.get("active_target_id")
            == args.expected_model_id,
            "details_source_matches": details.get("source_sha256")
            == args.expected_source_sha256,
            "effective_context_matches": int(
                details.get("effective_context_limit")
                or details.get("effective_context_window_tokens")
                or 0
            )
            == args.context_window_tokens,
            "active_pointer_matches": bool(
                active
                and active.get("target_kind") == "model_bundle"
                and active.get("target_id") == args.expected_model_id
                and active.get("profile_id") == args.expected_profile_id
                and active.get("source_sha256") == args.expected_source_sha256
            ),
            "database_integrity_ok": bool(
                integrity and next(iter(integrity.values()), None) == "ok"
            ),
            "database_foreign_keys_clean": not foreign_keys,
        }
        result = {
            "schema": "salty-steak-model-bundle-chat-acceptance-v1",
            "success": all(checks.values()),
            "checks": checks,
            "duration_seconds": round(time.perf_counter() - started, 4),
            "project_root": str(project_root),
            "workspace": str(workspace),
            "application_import": application_import,
            "database_schema_version": next(iter(schema.values())) if schema else None,
            "status_before": status_before,
            "operation": completed,
            "conversation_id": conversation["id"],
            "assistant_message": assistant,
            "status_after": status_after,
            "runtime": runtime,
            "active_chat_runtime": active,
            "requested_generation_settings": generation_settings,
            "response_check": args.response_check,
            "expected_response": args.expected_response,
            "user_prompt": user_prompt,
        }
    except BaseException as error:
        result = {
            "schema": "salty-steak-model-bundle-chat-acceptance-v1",
            "success": False,
            "checks": checks,
            "duration_seconds": round(time.perf_counter() - started, 4),
            "project_root": str(project_root),
            "workspace": str(workspace),
            "application_import": application_import,
            "error_type": type(error).__name__,
            "error": str(error),
        }
    finally:
        if application is not None:
            try:
                application.close()
                close_completed = True
            except BaseException as error:
                close_error = f"{type(error).__name__}: {error}"

    deadline = time.monotonic() + 15.0
    while worker_pid and psutil.pid_exists(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    worker_survived = bool(worker_pid and psutil.pid_exists(worker_pid))
    result["normal_close_completed"] = close_completed
    result["close_error"] = close_error
    result["worker_pid"] = worker_pid
    result["worker_survived_close"] = worker_survived
    result["success"] = (
        bool(result.get("success")) and close_completed and not worker_survived
    )

    result_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_path.with_suffix(result_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(result_path)
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
