"""Run identity and ordinary chat through the sealed package's private Python."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from gpu_validation_guard import gpu_validation_preflight


PROJECT_ROOT = Path(__file__).resolve().parents[1]


CHILD = r'''
import json
from pathlib import Path
import sys
from app.backend.application import Application

root = Path.cwd().resolve()
app = Application(root, recover_operations=False)

def send(prompt):
    conversation = app.create_conversation()
    operation = app.chat.start_message(
        conversation["id"],
        prompt,
        {
            "reasoning_mode": "instant",
            "context_window_tokens": 32768,
            "maximum_output_mode": "manual",
            "maximum_output_tokens": 256,
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "repetition_penalty": 1.05,
            "seed": 20260821,
        },
    )
    completed = app.operations.wait(operation["id"], timeout=300)
    conversation_record = app.get_conversation(conversation["id"])
    assistant = next(
        (
            message
            for message in reversed(conversation_record.get("messages") or [])
            if message.get("role") == "assistant"
        ),
        {},
    )
    details = dict(assistant.get("technical_details") or {})
    return {
        "state": completed.get("state"),
        "error": completed.get("error"),
        "operation_result": completed.get("result"),
        "message_count": len(conversation_record.get("messages") or []),
        "content": str(assistant.get("content") or ""),
        "learned_route": details.get("learned_route"),
        "route_code": (details.get("learned_route_controller") or {}).get("code"),
        "model_sharing_context": (details.get("learned_route_controller") or {}).get("model_sharing_context"),
        "active_adapter_ids": list(details.get("active_adapter_ids") or []),
        "activity_steps": len(details.get("activity_journal") or []),
        "engine": details.get("engine"),
        "ipc_transport": details.get("ipc_transport"),
    }

try:
    result = {
        "app_module": str(Path(sys.modules["app"].__file__).resolve()),
        "identity": send("What is your exact model name and who trained you?"),
        "ordinary": send("Reply with exactly the word ready."),
    }
finally:
    app.close()
print(json.dumps(result, separators=(",", ":")))
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expected-build-id", required=True)
    parser.add_argument(
        "--guard-workspace",
        type=Path,
        default=PROJECT_ROOT / "workspace",
        help="Production workspace whose active GPU operations block validation.",
    )
    args = parser.parse_args()
    package = args.package.resolve()
    workspace = args.workspace.resolve()
    report_path = args.report.resolve()
    if report_path.exists():
        raise FileExistsError(report_path)
    if (PROJECT_ROOT / "dist").resolve() not in package.parents:
        raise ValueError("--package must be a named child of dist")
    if (PROJECT_ROOT / "validation").resolve() not in workspace.parents:
        raise ValueError("--workspace must be a named child of validation")
    package_record = json.loads(
        (package / "package.json").read_text(encoding="utf-8-sig")
    )
    preflight = gpu_validation_preflight(args.guard_workspace)
    if not preflight["idle"]:
        report = {
            "schema": "salty-steak-packaged-runtime-acceptance-v1",
            "package": str(package),
            "workspace": str(workspace),
            "gpu_preflight": preflight,
            "blocked": True,
            "passed": False,
            "reason": "A live user GPU operation is active; validation was not started.",
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3
    executable = package / ".venv" / "Scripts" / "python.exe"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["SALTY_POTATO_WORKSPACE"] = str(workspace)
    environment["SALTY_POTATO_BUILD_ID"] = str(args.expected_build_id)
    try:
        completed = subprocess.run(
            [str(executable), "-B", "-c", CHILD],
            cwd=package,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else str(error.stdout or "")
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
        report = {
            "schema": "salty-steak-packaged-runtime-acceptance-v1",
            "package": str(package),
            "workspace": str(workspace),
            "gpu_preflight": preflight,
            "blocked": False,
            "timed_out": True,
            "timeout_seconds": 900,
            "stdout_tail": stdout[-2_000:],
            "stderr_tail": stderr[-2_000:],
            "passed": False,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    child = None
    if completed.returncode == 0:
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        child = json.loads(lines[-1]) if lines else None
    identity = dict((child or {}).get("identity") or {})
    ordinary = dict((child or {}).get("ordinary") or {})
    identity_text = identity.get("content", "").casefold()
    gates = {
        "child_exited_cleanly": completed.returncode == 0,
        "package_build_id_matches": package_record.get("build_id")
        == args.expected_build_id,
        "app_imported_from_package": bool(
            child
            and package in Path(str(child.get("app_module") or "")).resolve().parents
        ),
        "identity_completed": identity.get("state") == "completed",
        "identity_facts_present": all(
            value in identity_text
            for value in ("base steak 2.0", "md anik hasan", "sawlper")
        ),
        "identity_route_and_adapter": (
            identity.get("learned_route") == "identity"
            and identity.get("route_code") == "E"
            and len(identity.get("active_adapter_ids") or []) == 1
            and str(identity["active_adapter_ids"][0]).startswith(
                "base-steak-2-0-identity-"
            )
            and str(identity["active_adapter_ids"][0]).endswith("-v1")
        ),
        "ordinary_exact_adapter_free": (
            ordinary.get("state") == "completed"
            and ordinary.get("content", "").strip() == "ready"
            and ordinary.get("learned_route") == "respond"
            and ordinary.get("route_code") == "A"
            and ordinary.get("active_adapter_ids") == []
        ),
        "model_sharing_router_used": identity.get("model_sharing_context") is True
        and ordinary.get("model_sharing_context") is True,
        "activity_journals_present_and_dynamic": (
            identity.get("activity_steps", 0) >= 5
            and ordinary.get("activity_steps", 0) >= 5
        ),
        "private_pipe_runtime": identity.get("ipc_transport") == "anonymous_pipes"
        and ordinary.get("ipc_transport") == "anonymous_pipes",
    }
    report = {
        "schema": "salty-steak-packaged-runtime-acceptance-v1",
        "package": str(package),
        "workspace": str(workspace),
        "gpu_preflight": preflight,
        "returncode": completed.returncode,
        "stderr_tail": completed.stderr[-2_000:],
        "child": child,
        "gates": gates,
        "passed": all(gates.values()),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
