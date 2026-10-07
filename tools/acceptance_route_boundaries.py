"""Adversarial learned-route acceptance for image, code-file, and action boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backend.runtime.salty_native import SaltyNativeProfile
from app.backend.runtime.salty_native_worker import SaltyNativeWorkerRuntime
from app.backend.training.route_dataset import ROUTE_CODES, ROUTE_SYSTEM


CASES = (
    ("respond-image-explanation", "RESPOND", "Explain how image generation works."),
    ("respond-words-only", "RESPOND", "Describe a rainy neon street in words only; do not generate a picture."),
    ("respond-image-code", "RESPOND", "Write Python code that generates an image, but do not create the image."),
    ("respond-prompt-review", "RESPOND", "Review this image prompt and improve its wording only."),
    ("respond-explicit-negative", "RESPOND", "I do not want an image. Give me three visual ideas in prose."),
    ("respond-resolution-advice", "RESPOND", "What image resolution should I choose and why?"),
    ("respond-save-code", "RESPOND", "Show Python code for saving a PNG as an example."),
    ("respond-script-file", "RESPOND", "Create a downloadable Python script file named chart.py that draws a chart; do not run it."),
    ("respond-imagine", "RESPOND", "Imagine a mountain city and describe it without rendering anything."),
    ("respond-prior-image", "RESPOND", "Explain why the previous image may look blurry."),
    ("respond-image-word", "RESPOND", "Repeat the words generate image exactly; do not generate one."),
    ("respond-code-review", "RESPOND", "Review my image-processing function for bugs."),
    ("image-direct", "IMAGE", "Generate an image of a copper lighthouse at blue hour."),
    ("image-natural", "IMAGE", "Make the scene we discussed into an actual picture."),
    ("image-revision", "IMAGE", "Revise the previous generated image with warmer light."),
    ("image-multilingual", "IMAGE", "ei scene ta image kore dao"),
    ("image-logo", "IMAGE", "Create the actual logo image, not another written concept."),
    ("agent-save", "AGENT", "Save the generated image into my Downloads folder."),
    ("agent-local-file", "AGENT", "Create chart.py in my project folder and verify it exists."),
    ("agent-run-script", "AGENT", "Run the Python script in my terminal and report its output."),
    ("research-current-model", "RESEARCH", "Research the newest public image model release and cite current sources."),
    ("research-current-prices", "RESEARCH", "Find today's GPU prices and compare current shops."),
    ("identity-name", "IDENTITY", "What is your exact model name?"),
    ("identity-trainer", "IDENTITY", "Who trained you?"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu-layers", type=int, default=24)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads(
        (arguments.model_root / "model.json").read_text(encoding="utf-8")
    )
    routing = next(
        item
        for item in manifest.get("companion_artifacts", [])
        if item.get("role") == "routing_adapter"
    )
    runtime = SaltyNativeWorkerRuntime(
        model_path=arguments.model_root / manifest["artifact"]["filename"],
        library_directory=arguments.runtime,
        source_sha256=manifest["artifact"]["sha256"],
        adapters=[
            {
                "adapter_id": routing["id"],
                "path": str(arguments.model_root / routing["filename"]),
                "sha256": routing["sha256"],
                "scale": float(routing.get("scale", 1.0)),
                "activation": "routing_intent",
            }
        ],
        profile=SaltyNativeProfile(
            profile_id="route-boundary-acceptance",
            gpu_layers=arguments.gpu_layers,
            context_limit=2_048,
            resident_context_limit=2_048,
            batch_size=256,
            micro_batch_size=128,
        ),
    )
    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    try:
        loaded = runtime.load()
        for case_id, expected, prompt in CASES:
            case_started = time.perf_counter()
            generated = runtime.classify_route(
                messages=[
                    {"role": "system", "content": ROUTE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                allowed_tokens=tuple(ROUTE_CODES.values()),
                enabled_adapter_ids=(routing["id"],),
            )
            actual_code = generated.text.strip().upper().rstrip(".")
            actual = next(
                (label for label, code in ROUTE_CODES.items() if code == actual_code),
                "MALFORMED",
            )
            rows.append(
                {
                    "id": case_id,
                    "prompt": prompt,
                    "expected": expected,
                    "actual": actual,
                    "code": actual_code,
                    "passed": actual == expected,
                    "wall_seconds": round(time.perf_counter() - case_started, 4),
                    "prefill_seconds": generated.technical_details.get(
                        "prefill_duration_seconds"
                    ),
                    "stable_prefill_buffer": generated.technical_details.get(
                        "stable_prefill_buffer"
                    ),
                }
            )
    finally:
        runtime.unload()
    report = {
        "schema": "salty-steak-route-boundary-acceptance-v1",
        "model_sha256": manifest["artifact"]["sha256"],
        "routing_adapter_sha256": routing["sha256"],
        "case_count": len(rows),
        "pass_count": sum(bool(row["passed"]) for row in rows),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "loaded_runtime": loaded,
        "rows": rows,
    }
    report["passed"] = report["pass_count"] == report["case_count"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
