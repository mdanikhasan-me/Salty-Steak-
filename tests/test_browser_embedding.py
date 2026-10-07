from types import SimpleNamespace
from pathlib import Path
import os
import pytest

from app.backend.application import Application


@pytest.mark.skipif(os.name != "nt", reason="Native window embedding is Windows-only")
def test_embedding_only_accepts_a_window_owned_by_this_application():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetDesktopWindow.restype = wintypes.HWND
    with pytest.raises(PermissionError, match="this application window"):
        Application.dock_browser_surface(SimpleNamespace(), {
            "parent_window": user32.GetDesktopWindow(), "visible": True,
            "width": 600, "height": 400,
        })


@pytest.mark.skipif(os.name != "nt", reason="Native window embedding is Windows-only")
def test_embedding_grant_dimensions_hide_and_private_command_contract():
    import ctypes
    from ctypes import wintypes
    from app.backend.automation.browser_client import BROWSER_COMMANDS
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    handle = user32.CreateWindowExW(0, "STATIC", "Embedding test", 0, 0, 0, 600, 400, None, None, None, None)
    assert handle
    calls = []
    allowed = [True]
    client = SimpleNamespace(call=lambda command, args: calls.append((command, args)) or {"embedded": True})
    broker = SimpleNamespace(_browser_client=client, status=lambda: {"capabilities": [
        {"capability": "browser.control", "effective_enabled": allowed[0]}]})
    app = SimpleNamespace(automation=broker)
    args = {"parent_window": int(handle), "visible": True, "width": 600, "height": 400}
    try:
        assert "dock_surface" not in BROWSER_COMMANDS  # model cannot select arbitrary native parents
        result = Application.dock_browser_surface(app, args)
        assert result["embedded"] and calls[0][0] == "dock_surface"
        assert calls[0][1]["parent_pid"] == os.getpid()
        with pytest.raises(ValueError, match="bounds"):
            Application.dock_browser_surface(app, {**args, "width": 0})
        allowed[0] = False
        with pytest.raises(PermissionError, match="Enable browser access"):
            Application.dock_browser_surface(app, args)
        Application.dock_browser_surface(app, {**args, "visible": False})
        assert calls[-1][1]["visible"] is False
    finally:
        user32.DestroyWindow(handle)
