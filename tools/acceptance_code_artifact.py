"""Prove that the real local model authors a downloadable code file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.gpu_validation_guard import gpu_validation_preflight  # noqa: E402


FENCE = re.compile(r"```(?P<header>[^\n`]*)\r?\n(?P<content>[\s\S]*?)```")
FILE_TOKEN = re.compile(r"(?:^|\s)(?:file|filename)=['\"]?(?P<name>[^\s'\"]+)", re.I)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--application-root", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--guard-workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--expected-build-id", required=True)
    arguments = parser.parse_args()
    root = arguments.application_root.resolve()
    workspace = arguments.workspace.resolve()
    report_path = arguments.report.resolve()
    if report_path.exists():
        raise FileExistsError(report_path)
    preflight = gpu_validation_preflight(arguments.guard_workspace)
    if not preflight["idle"]:
        report = {"schema": "salty-steak-code-artifact-acceptance-v1", "passed": False, "blocked": True, "gpu_preflight": preflight}
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 3

    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    os.environ["SALTY_POTATO_BUILD_ID"] = arguments.expected_build_id
    prior_path = list(sys.path)
    sys.path = [str(root), *[value for value in sys.path if Path(value or ".").resolve() != PROJECT_ROOT]]
    for key in tuple(sys.modules):
        if key == "app" or key.startswith("app."):
            del sys.modules[key]
    from app.backend.application import Application

    app = Application(root, recover_operations=False)
    report = {
        "schema": "salty-steak-code-artifact-acceptance-v1",
        "application_root": str(root),
        "workspace": str(workspace),
        "gpu_preflight": preflight,
        "passed": False,
    }
    try:
        conversation = app.create_conversation()
        operation = app.chat.start_message(
            conversation["id"],
            (
                "Create a Python script file named marker_writer.py that prints "
                "exactly SALTY_FILE_ARTIFACT_OK. Give me the complete file."
            ),
            {
                "reasoning_mode": "instant",
                "context_window_tokens": 32_768,
                "maximum_output_mode": "manual",
                "maximum_output_tokens": 512,
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": 1,
                "repetition_penalty": 1.0,
                "seed": 20260825,
            },
        )
        completed = app.operations.wait(operation["id"], timeout=300)
        saved = app.get_conversation(conversation["id"])
        assistant = next(message for message in reversed(saved["messages"]) if message["role"] == "assistant")
        content = str(assistant.get("content") or "")
        assistant_details = dict(assistant.get("technical_details") or {})
        artifacts = []
        for match in FENCE.finditer(content):
            header = match.group("header").strip()
            named = FILE_TOKEN.search(header)
            artifacts.append(
                {
                    "header": header,
                    "filename": Path(named.group("name")).name if named else None,
                    "content": match.group("content").replace("\r\n", "\n"),
                }
            )
        selected = next(
            (
                artifact
                for artifact in artifacts
                if artifact.get("filename") == "marker_writer.py"
            ),
            None,
        )
        nonfile_conversation = app.create_conversation()
        nonfile_operation = app.chat.start_message(
            nonfile_conversation["id"],
            "Explain in two sentences why Python examples use code fences.",
            {
                "reasoning_mode": "instant",
                "context_window_tokens": 32_768,
                "maximum_output_mode": "manual",
                "maximum_output_tokens": 256,
                "temperature": 0.0,
                "top_p": 1.0,
                "top_k": 1,
                "repetition_penalty": 1.0,
                "seed": 20260826,
            },
        )
        nonfile_completed = app.operations.wait(nonfile_operation["id"], timeout=120)
        nonfile_saved = app.get_conversation(nonfile_conversation["id"])
        nonfile_assistant = next(
            message
            for message in reversed(nonfile_saved["messages"])
            if message["role"] == "assistant"
        )
        nonfile_content = str(nonfile_assistant.get("content") or "")
        nonfile_details = dict(nonfile_assistant.get("technical_details") or {})
        image_route, image_route_details = app.chat._learned_route_decision(
            "Generate an image of a copper lighthouse at blue hour.",
            context=SimpleNamespace(stop_requested=lambda: False),
        )
        gates = {
            "operation_completed": completed.get("state") == "completed",
            "assistant_response_present": bool(content.strip()),
            "named_file_fence_present": selected is not None,
            "python_language_present": bool(
                selected and selected["header"].casefold().startswith("python")
            ),
            "exact_marker_present": bool(
                selected and "SALTY_FILE_ARTIFACT_OK" in selected["content"]
            ),
            "file_artifact_controller_enabled": (
                assistant_details.get("code_file_artifacts_allowed") is True
            ),
            "nonfile_turn_completed": nonfile_completed.get("state") == "completed",
            "image_request_selected_image_route": image_route == "image",
            "image_route_code_is_C": image_route_details.get("code") == "C",
            "nonfile_turn_file_artifacts_disabled": (
                nonfile_details.get("code_file_artifacts_allowed") is False
            ),
            "nonfile_turn_has_no_named_file_fence": FILE_TOKEN.search(nonfile_content) is None,
            "no_execution_claim": not any(
                claim in content.casefold()
                for claim in ("i executed", "i ran the script", "saved on your computer")
            ),
        }
        report.update(
            {
                "operation_id": operation["id"],
                "assistant_content": content,
                "artifacts": artifacts,
                "nonfile_operation_id": nonfile_operation["id"],
                "nonfile_assistant_content": nonfile_content,
                "image_route": image_route,
                "image_route_details": image_route_details,
                "gates": gates,
                "passed": all(gates.values()),
            }
        )
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        app.close()
        sys.path = prior_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
