"""Measure the real local interactive path across repeated warm-state turns."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-id", required=True)
    arguments = parser.parse_args()
    project_root = arguments.project_root.resolve()
    sys.path.insert(0, str(project_root))
    workspace = arguments.workspace.resolve()
    output = arguments.output.resolve()
    if output.exists():
        raise FileExistsError(output)

    os.environ["SALTY_POTATO_WORKSPACE"] = str(workspace)
    os.environ["SALTY_POTATO_BUILD_ID"] = arguments.build_id

    from app.backend.application import Application
    from app.backend.imaging.orchestrator import (
        BRIEF_AUTHOR_INSTRUCTION,
        BRIEF_AUTHOR_MAX_TOKENS,
    )

    started = time.perf_counter()
    application = Application(project_root, recover_operations=False)
    constructed_seconds = time.perf_counter() - started

    settings = {
        "reasoning_mode": "instant",
        "context_window_tokens": 32_768,
        "maximum_output_mode": "manual",
        "maximum_output_tokens": 256,
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 40,
        "repetition_penalty": 1.05,
        "seed": 20260826,
    }

    def send(prompt: str) -> dict[str, object]:
        conversation = application.create_conversation()
        turn_started = time.perf_counter()
        operation = application.chat.start_message(
            conversation["id"], prompt, settings
        )
        terminal = application.operations.wait(operation["id"], timeout=180)
        wall_seconds = time.perf_counter() - turn_started
        transcript = application.get_conversation(conversation["id"])
        assistant = next(
            (
                message
                for message in reversed(transcript.get("messages") or [])
                if message.get("role") == "assistant"
            ),
            {},
        )
        details = dict(assistant.get("technical_details") or {})
        return {
            "prompt": prompt,
            "state": terminal.get("state"),
            "error": terminal.get("error"),
            "wall_seconds": round(wall_seconds, 4),
            "content": str(assistant.get("content") or ""),
            "route": details.get("learned_route"),
            "route_seconds": dict(
                details.get("learned_route_controller") or {}
            ).get("duration_seconds"),
            "generated_output_tokens": details.get("generated_output_tokens"),
            "prefill_seconds": details.get("prefill_duration_seconds"),
            "first_token_seconds": details.get("time_to_first_token_seconds"),
            "host_action_kind": dict(
                details.get("host_action_proposal") or {}
            ).get("kind"),
        }

    report: dict[str, object] = {
        "schema": "salty-steak-interactive-latency-acceptance-v1",
        "build_id": arguments.build_id,
        "project_root": str(project_root),
        "workspace": str(workspace),
        "application_constructed_seconds": round(constructed_seconds, 4),
        "passed": False,
    }
    try:
        warm_started = time.perf_counter()
        warmed = application.chat.warm_selected_model_bundle() or {}
        report["warmup_seconds"] = round(time.perf_counter() - warm_started, 4)
        report["runtime_warmup_seconds"] = warmed.get("warmup_seconds")
        report["warmup"] = warmed
        turns = [
            send("hi"),
            send("hola"),
            send("what is your name"),
        ]
        report["turns"] = turns

        image_started = time.perf_counter()
        raw_image = application.chat._agent_generate(
            [
                {
                    "role": "system",
                    "content": BRIEF_AUTHOR_INSTRUCTION,
                },
                {
                    "role": "user",
                    "content": (
                        "REQUEST:\nCreate an image of a copper lighthouse at blue hour."
                        "\n\nBRIEF SO FAR:\n"
                        '{"subject":"a copper lighthouse at blue hour",'
                        '"image_type":"illustration"}'
                    ),
                },
            ],
            context=SimpleNamespace(
                operation_id="interactive-image-protocol-acceptance",
                stop_requested=lambda: False,
            ),
            generation_settings={
                **settings,
                "maximum_output_tokens": BRIEF_AUTHOR_MAX_TOKENS,
            },
            response_format="json",
        )
        image_seconds = time.perf_counter() - image_started
        try:
            parsed_image = json.loads(raw_image)
            image_brief = parsed_image if isinstance(parsed_image, dict) else {}
            image_valid = (
                isinstance(parsed_image, dict)
                and isinstance(image_brief, dict)
                and bool(image_brief.get("subject"))
                and "copper lighthouse"
                in str(image_brief.get("subject") or "").casefold()
            )
        except (TypeError, json.JSONDecodeError):
            parsed_image = None
            image_valid = False
        report["image_protocol"] = {
            "wall_seconds": round(image_seconds, 4),
            "raw": raw_image,
            "parsed": parsed_image,
            "valid": image_valid,
        }

        protocol_started = time.perf_counter()
        raw_protocol = application.chat._agent_generate(
            [
                {
                    "role": "system",
                    "content": (
                        "Return one JSON object with action answer and answer ready. "
                        "No prose outside the object."
                    ),
                },
                {"role": "user", "content": "Confirm the protocol."},
            ],
            context=SimpleNamespace(
                operation_id="interactive-json-acceptance",
                stop_requested=lambda: False,
            ),
            generation_settings=settings,
            response_format="json",
        )
        protocol_seconds = time.perf_counter() - protocol_started
        try:
            parsed_protocol = json.loads(raw_protocol)
            protocol_valid = isinstance(parsed_protocol, dict)
        except (TypeError, json.JSONDecodeError):
            parsed_protocol = None
            protocol_valid = False
        report["json_protocol"] = {
            "wall_seconds": round(protocol_seconds, 4),
            "raw": raw_protocol,
            "parsed": parsed_protocol,
            "valid": protocol_valid,
        }

        gates = {
            "runtime_ready": bool(warmed.get("ready")),
            "warmup_under_45_seconds": (
                float(warmed.get("warmup_seconds") or 999) <= 45
            ),
            "short_turns_completed": all(
                turn["state"] == "completed" and bool(turn["content"])
                for turn in turns[:3]
            ),
            "first_short_turn_under_12_seconds": turns[0]["wall_seconds"] <= 12,
            "warm_short_turns_under_8_seconds": all(
                turn["wall_seconds"] <= 8 for turn in turns[1:3]
            ),
            "warm_routes_under_2_seconds": all(
                float(turn["route_seconds"] or 999) <= 2 for turn in turns[1:3]
            ),
            "identity_correct": "base steak 2.0" in turns[2]["content"].casefold(),
            "image_protocol_under_30_seconds": image_valid and image_seconds <= 30,
            "json_protocol_valid_under_15_seconds": (
                protocol_valid and protocol_seconds <= 15
            ),
        }
        report["gates"] = gates
        report["passed"] = all(gates.values())
    finally:
        application.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
