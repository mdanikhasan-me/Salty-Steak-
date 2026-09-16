"""Bind file deletion selections to a task's compiled absence outcomes."""
from __future__ import annotations

import os
import re
import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


def bind_delete_selection(capability: str, arguments: Mapping[str, Any], task: Any) -> dict[str, Any]:
    checked = dict(arguments)
    scopes = getattr(task, "deletion_targets", None)
    if capability != "files.manage" or str(checked.get("operation") or "").strip().casefold() != "delete" or scopes is None:
        return checked
    if not scopes:
        raise PermissionError("No deletion target was established in this task's goal")
    from .broker import _existing_path, _explicit_paths, _matching_paths
    root = _existing_path(checked.get("path"))
    pattern = str(checked.get("pattern") or "")
    if root.is_dir() and not pattern and checked.get("paths") is None:
        raise ValueError("Deleting inside a folder needs a pattern or an explicit paths set")
    if checked.get("paths") is not None:
        if pattern:
            raise ValueError("Use either pattern or paths, not both")
        selected = _explicit_paths(root, checked["paths"])
    else:
        selected = _matching_paths(root, pattern, recursive=bool(checked.get("recursive")))
    def allowed(candidate: Path) -> bool:
        resolved = candidate.resolve()
        for scope in scopes:
            raw = str(scope)
            if not Path(raw).is_absolute() or re.search(r"\$|%[^%]+%|\{\{", raw):
                continue
            if any(char in raw for char in "*?["):
                # Resolve the non-pattern prefix so a junction cannot turn an
                # authorized folder into a different physical scope.
                parts = Path(raw).parts
                split = next(index for index, part in enumerate(parts) if any(c in part for c in "*?["))
                prefix = Path(*parts[:split]).resolve()
                if not resolved.is_relative_to(prefix):
                    continue
                relative_parts = resolved.relative_to(prefix).parts
                pattern_parts = parts[split:]
                @lru_cache(None)
                def matches(part_index: int, pattern_index: int) -> bool:
                    if pattern_index == len(pattern_parts):
                        return part_index == len(relative_parts)
                    expected = pattern_parts[pattern_index]
                    if expected == "**":
                        return matches(part_index, pattern_index + 1) or (
                            part_index < len(relative_parts) and matches(part_index + 1, pattern_index)
                        )
                    return part_index < len(relative_parts) and fnmatch.fnmatchcase(
                        os.path.normcase(relative_parts[part_index]), os.path.normcase(expected)
                    ) and matches(part_index + 1, pattern_index + 1)
                if matches(0, 0):
                    return True
            else:
                target = Path(raw).resolve()
                if os.path.normcase(str(resolved)) == os.path.normcase(str(target)):
                    return True
                if target.is_dir() and resolved.is_relative_to(target):
                    return True
        return False
    if not selected or any(not allowed(path) for path in selected):
        raise PermissionError("The selected deletion target does not match this task's compiled goal")
    if root.is_dir():
        checked.pop("pattern", None)
        checked["paths"] = [str(path.resolve()) for path in selected]
    return checked
