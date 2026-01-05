"""One-request isolated worker for Steak Gen image generation."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .steak_gen import CancellationSignal, SteakGenPaths, SteakGenRequest


def _write(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve_once() -> int:
    line = sys.stdin.readline()
    if not line:
        _write(
            {
                "ok": False,
                "error": {
                    "type": "ValueError",
                    "message": "Steak Gen worker received no request",
                },
            }
        )
        return 2
    try:
        payload = json.loads(line)
        paths = SteakGenPaths.from_workspace(payload["workspace_root"])
        request = SteakGenRequest(**dict(payload["request"]))
        cancellation = CancellationSignal(Path(payload["cancellation_path"]))
        from .steak_gen_engine import generate_one_image

        result = generate_one_image(
            paths=paths,
            request=request,
            cancellation=cancellation,
            on_event=lambda event: _write({"event": "progress", **event}),
        )
        _write({"ok": True, "result": asdict(result)})
        return 0
    except BaseException as error:
        traceback.print_exc(file=sys.stderr)
        _write(
            {
                "ok": False,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve-once", action="store_true")
    args = parser.parse_args()
    if not args.serve_once:
        parser.error("--serve-once is required")
    return serve_once()


if __name__ == "__main__":
    raise SystemExit(main())
