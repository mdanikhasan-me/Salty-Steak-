"""Package-pinned launcher for disposable private-Python workers."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("worker module is required")
    package_root = Path(__file__).resolve().parents[2]
    module = sys.argv[1]
    sys.argv = [module, *sys.argv[2:]]
    sys.path.insert(0, str(package_root))
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
