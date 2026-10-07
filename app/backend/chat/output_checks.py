"""Bounded checks for generated artifacts; never execute arbitrary Python/shell."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable

from ..automation.browser_client import find_browser_host


def check_output(answer: str, *, request: str = "", package_root: Path | None = None,
                 should_stop: Callable[[], bool] = lambda: False) -> dict:
    blocks = re.findall(r"```([^\n`]*)\n([\s\S]*?)(?:```|\Z)", answer)
    html = [body for language, body in blocks if language.strip().lower() in {"html", "htm"}
            or re.search(r"<!doctype\s+html|<html\b", body, re.I)]
    if not html and re.match(r"\s*(?:<!doctype\s+html|<html\b)", answer, re.I):
        html = [answer]
    issues: list[str] = []
    if answer.count("```") % 2:
        issues.append("An output code fence is unfinished.")
    for index, body in enumerate(html):
        full_document = bool(re.search(r"<!doctype\s+html|<html\b", body, re.I) or re.search(r"\bgame\b", request, re.I))
        if full_document and not re.search(r"</html\s*>\s*$", body.strip(), re.I):
            issues.append(f"HTML document {index+1} is incomplete: missing final </html>.")
        if len(re.findall(r"<script\b", body, re.I)) != len(re.findall(r"</script\s*>", body, re.I)):
            issues.append(f"HTML document {index+1} has an unclosed script.")
    for language, body in blocks:
        if language.strip().lower() in {"python", "py"}:
            try:
                ast.parse(body)
            except SyntaxError as error:
                issues.append(f"Python syntax error at line {error.lineno}: {error.msg}")
    base = {"artifact_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "issues": issues, "scope": "Structural and syntax checks only; Python is not executed."}
    if issues:
        return {**base, "status": "failed", "kind": "structure"}
    if len(html) > 1:
        return {**base, "status": "unverified", "kind": "html", "issues": ["Multiple HTML documents need a project-aware test; no runtime test was run."]}
    if html:
        host = find_browser_host(package_root or Path(__file__).resolve().parents[3])
        if host is None:
            return {**base, "status": "unverified", "kind": "html", "issues": ["The isolated browser verifier is unavailable."]}
        result = _run_html(host, html[0], bool(re.search(r"\bgame\b", request, re.I)), should_stop)
        return {**base, **result, "kind": "html"}
    return {**base, "status": "unverified", "kind": "other",
            "scope": "Syntax checked where supported. No general-purpose runtime or factual verification was performed."}


def _run_html(host: Path, html: str, require_start: bool, should_stop: Callable[[], bool]) -> dict:
    if should_stop():
        return {"status": "cancelled", "issues": []}
    request = json.dumps({"id": "verify", "command": "verify_html", "payload": {
        "html": html, "require_start": require_start, "timeout_ms": 20000}}) + "\n"
    try:
        process = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as error:
        return {"status": "unverified", "issues": [f"Browser verifier could not start: {error}"]}
    deadline = time.monotonic() + 30
    first = True
    try:
        while True:
            if should_stop():
                return {"status": "cancelled", "issues": []}
            if time.monotonic() > deadline:
                return {"status": "unverified", "issues": ["Browser verification timed out; completion is not proven."]}
            try:
                output, _ = process.communicate(input=request if first else None, timeout=.2)
                break
            except subprocess.TimeoutExpired:
                first = False
        envelope = next((json.loads(line) for line in output.splitlines() if line.startswith('{')), {})
        if not envelope.get("ok"):
            return {"status": "unverified", "issues": [str(envelope.get("error", {}).get("message", "Browser verifier failed"))[:500]]}
        result = dict(envelope["result"])
        if result.get("status") not in {"passed", "failed", "unverified"}:
            raise ValueError("Invalid verifier status")
        return result
    except (OSError, ValueError, KeyError) as error:
        return {"status": "unverified", "issues": [f"Browser verifier unavailable: {error}"]}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def verify_and_repair(answer: str, *, check: Callable[[str], dict],
                      repair: Callable[[str, dict, int], str],
                      should_stop: Callable[[], bool], publish: Callable[[dict], None],
                      maximum_repairs: int = 2) -> tuple[str, dict]:
    """At most three checks; failed repairs cannot erase the previous draft."""
    attempts = []
    for attempt in range(maximum_repairs + 1):
        if should_stop():
            return answer, {"status": "cancelled", "attempts": attempts}
        publish({"status": "checking", "attempt": attempt+1})
        result = check(answer)
        attempts.append(result)
        if result["status"] != "failed" or attempt == maximum_repairs:
            return answer, {**result, "attempts": attempts, "repairs": attempt}
        if should_stop():
            return answer, {"status": "cancelled", "attempts": attempts, "repairs": attempt}
        publish({"status": "repairing", "attempt": attempt+1, "issues": result.get("issues", [])})
        revised = repair(answer, result, attempt+1)
        if revised.strip():
            answer = revised
    raise AssertionError("Unreachable")
