"""Local, cancellable build/test processes for explicitly selected Lock In turns.

The working folder separates artifacts; it is not an OS security sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence


def run_check(argv: Sequence[str], cwd: Path, *, should_stop: Callable[[], bool],
              timeout_seconds: float = 60, output_limit: int = 16000) -> dict:
    from .broker import _WindowsJob, _drain_output

    if should_stop():
        return {"status": "cancelled", "exit_code": None, "argv": list(argv)}
    started = time.monotonic()
    # Keep toolchain variables, not API keys or unrelated credentials.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "INCLUDE", "LIB",
            "LIBPATH", "JAVA_HOME", "DOTNET_ROOT", "VCTOOLSINSTALLDIR", "WINDOWSSDKDIR"}
    environment = {k: v for k, v in os.environ.items() if k.upper() in keep}
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                        "PYTHONUTF8": "1", "CI": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1", "DOTNET_CLI_HOME": str(cwd),
                        "npm_config_cache": str(cwd / ".npm-cache"), "npm_config_offline": "true",
                        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false", "DOTNET_ADD_GLOBAL_TOOLS_TO_PATH": "false",
                        "USERPROFILE": str(cwd), "HOME": str(cwd),
                        "APPDATA": str(cwd / ".appdata"), "LOCALAPPDATA": str(cwd / ".localappdata"),
                        "NUGET_PACKAGES": str(cwd / ".nuget"),
                        "GOTOOLCHAIN": "local", "GOPROXY": "off", "CARGO_NET_OFFLINE": "true"})
    for folder in (".appdata", ".localappdata", ".nuget"):
        (cwd / folder).mkdir(exist_ok=True)
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   env=environment, start_new_session=os.name != "nt",
                                   creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0)
    except OSError as error:
        return {"status": "unavailable", "exit_code": None, "argv": list(argv), "error": str(error)}
    job = _WindowsJob.attach(process)
    captures = [{}, {}]
    threads = [threading.Thread(target=_drain_output, args=(stream, output_limit, capture), daemon=True)
               for stream, capture in zip((process.stdout, process.stderr), captures)]
    for thread in threads:
        thread.start()
    status = "completed"
    try:
        while process.poll() is None:
            if should_stop():
                status = "cancelled"
                break
            if time.monotonic() - started >= timeout_seconds:
                status = "timed_out"
                break
            time.sleep(.05)
    finally:
        # Always retire descendants, including servers left behind by a test.
        if job is not None:
            job.terminate()
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=3)
    if any(thread.is_alive() for thread in threads):
        status = "unverified"
    return {"status": status, "exit_code": process.returncode, "argv": list(argv),
            "working_directory": str(cwd), "duration_seconds": round(time.monotonic()-started, 3),
            "stdout": captures[0].get("text", ""), "stderr": captures[1].get("text", ""),
            "output_truncated": any(c.get("truncated") for c in captures),
            "descendant_cleanup": "best_effort_windows_job_after_start" if job else "process_group" if os.name != "nt" else "best_effort",
            "shell": False, "elevated_requested": False}
