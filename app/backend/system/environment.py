"""Salty Steak Native Desktop AI Platform — host environment registry.

The stable half of what a task needs to know about this computer: which
applications exist and where, which folders the user's files actually live in,
how big the screen is, what the browser is.

These change on the scale of installing software, not on the scale of a task
step, so discovering them once and reusing the answer removes a whole class of
pointless work. Without this the model has to spend a reasoning turn, and often
a screenshot, to learn something the registry could have told it for free.

Discovery is read-only and best-effort throughout. A machine missing a key, a
folder or a binary is a normal machine, not an error: the slot is simply absent
and the runtime plans around it.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST_ENVIRONMENT_SCHEMA = "salty-steak-host-environment-v1"



DISCOVERY_TTL_SECONDS = 3_600.0




CANDIDATE_APPLICATIONS = (
    "msedge",
    "chrome",
    "firefox",
    "notepad",
    "explorer",
    "calc",
    "mspaint",
    "code",
    "winword",
    "excel",
    "powerpnt",
    "outlook",
    "wt",
    "powershell",
    "cmd",
)




KNOWN_FOLDER_KEYS = {
    "home": "USERPROFILE",
    "onedrive": "OneDrive",
    "appdata": "APPDATA",
    "local_appdata": "LOCALAPPDATA",
    "temp": "TEMP",
}

RELATIVE_KNOWN_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos")


def _app_paths_lookup(name: str) -> str | None:
    """Resolve an application through the Windows App Paths registry.

    This is how a bare name like ``msedge`` becomes a real executable path even
    when the installation directory is not on PATH.
    """

    if os.name != "nt":
        return None
    import winreg

    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        resolved = str(value).strip().strip('"')
        if resolved:
            return resolved
    return None


def resolve_application(name: str) -> str | None:
    """Find an application by name, PATH first and then the registry."""

    found = shutil.which(name)
    if found:
        return found
    return _app_paths_lookup(name)


def _default_browser() -> str | None:
    """Read the user's chosen browser from the shell association.

    Reported so the runtime can name it, never to silently attach to it: the
    browser capability keeps its own session in its own profile.
    """

    if os.name != "nt":
        return None
    import winreg

    key = (
        r"SOFTWARE\Microsoft\Windows\Shell\Associations"
        r"\UrlAssociations\https\UserChoice"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "ProgId")
    except OSError:
        return None
    identifier = str(value)
    for name, label in (
        ("MSEdge", "msedge"),
        ("Chrome", "chrome"),
        ("Firefox", "firefox"),
        ("Opera", "opera"),
        ("Brave", "brave"),
    ):
        if name.casefold() in identifier.casefold():
            return label
    return identifier


def _screen_geometry() -> dict[str, Any] | None:
    """Primary and virtual desktop size, straight from user32."""

    if os.name != "nt":
        return None
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        metric = user32.GetSystemMetrics
    except OSError:
        return None

    width, height = metric(0), metric(1)
    if width <= 0 or height <= 0:
        return None
    return {
        "primary_width": width,
        "primary_height": height,
        "virtual_left": metric(76),
        "virtual_top": metric(77),
        "virtual_width": metric(78) or width,
        "virtual_height": metric(79) or height,
        "monitor_count": metric(80) or 1,
    }


def _known_folders() -> dict[str, str]:
    folders: dict[str, str] = {}
    for slot, variable in KNOWN_FOLDER_KEYS.items():
        raw = os.environ.get(variable)
        if not raw:
            continue
        path = Path(raw)
        if path.is_dir():
            folders[slot] = str(path)

    home = folders.get("home")
    if home:
        for name in RELATIVE_KNOWN_FOLDERS:
            candidate = Path(home) / name
            if candidate.is_dir():
                folders[name.casefold()] = str(candidate)

        onedrive = folders.get("onedrive")
        if onedrive:
            for name in RELATIVE_KNOWN_FOLDERS:
                candidate = Path(onedrive) / name
                if candidate.is_dir():
                    folders[f"onedrive_{name.casefold()}"] = str(candidate)
    return folders


@dataclass
class HostEnvironment:
    """What this computer is, as far as the runtime can see."""

    platform: str
    applications: dict[str, str]
    known_folders: dict[str, str]
    default_browser: str | None
    screen: dict[str, Any] | None
    python_executable: str
    working_directory: str
    discovered_at: float

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.discovered_at

    def has(self, application: str) -> bool:
        return application.casefold() in self.applications

    def path_for(self, application: str) -> str | None:
        return self.applications.get(application.casefold())

    def world_state_facts(self) -> dict[str, Any]:
        """The subset worth seeding a task's world state with.

        Deliberately narrow. Handing the model every discovered path would
        spend more context than the discovery saves.
        """

        facts: dict[str, Any] = {
            "installed_applications": sorted(self.applications),
            "working_directory": self.working_directory,
        }
        if self.known_folders:
            facts["known_folders"] = dict(self.known_folders)
        if self.default_browser:
            facts["default_browser"] = self.default_browser
        if self.screen:
            facts["screen"] = {
                "width": self.screen["primary_width"],
                "height": self.screen["primary_height"],
                "monitor_count": self.screen["monitor_count"],
            }
        return facts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HOST_ENVIRONMENT_SCHEMA,
            "platform": self.platform,
            "applications": dict(self.applications),
            "known_folders": dict(self.known_folders),
            "default_browser": self.default_browser,
            "screen": dict(self.screen) if self.screen else None,
            "python_executable": self.python_executable,
            "working_directory": self.working_directory,
            "age_seconds": round(self.age_seconds, 2),
        }


class HostEnvironmentRegistry:
    """Discover the host once and serve the answer until it goes stale."""

    def __init__(
        self,
        *,
        working_directory: Path | str | None = None,
        ttl_seconds: float = DISCOVERY_TTL_SECONDS,
        candidates: tuple[str, ...] = CANDIDATE_APPLICATIONS,
    ) -> None:
        self._working_directory = str(working_directory or Path.cwd())
        self._ttl = float(ttl_seconds)
        self._candidates = tuple(candidates)
        self._cached: HostEnvironment | None = None
        self._lock = threading.Lock()

    def get(self, *, refresh: bool = False) -> HostEnvironment:
        with self._lock:
            cached = self._cached
            if not refresh and cached is not None and cached.age_seconds < self._ttl:
                return cached
            discovered = self._discover()
            self._cached = discovered
            return discovered

    def invalidate(self) -> None:
        """Force the next read to re-probe, after an install for instance."""

        with self._lock:
            self._cached = None

    def _discover(self) -> HostEnvironment:
        applications: dict[str, str] = {}
        for name in self._candidates:
            resolved = resolve_application(name)
            if resolved:
                applications[name] = resolved
        return HostEnvironment(
            platform=os.name,
            applications=applications,
            known_folders=_known_folders(),
            default_browser=_default_browser(),
            screen=_screen_geometry(),
            python_executable=sys.executable,
            working_directory=self._working_directory,
            discovered_at=time.monotonic(),
        )


def seed_world_state(context: Any, environment: HostEnvironment) -> None:
    """Give a task what the machine already knows before its first step."""

    from ..chat.task_runtime import merge_world_state

    merge_world_state(context, environment.world_state_facts(), source="host_registry")
