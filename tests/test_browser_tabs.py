"""Real multi-tab proof against a live Chromium session.

Everything here is structural: elements are found by role and accessible name,
tabs by opaque handle. No screenshots, no vision, no coordinates.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from app.backend.automation.browser_client import (
    BROWSER_MUTATING_COMMANDS,
    BROWSER_READ_COMMANDS,
    BrowserClient,
    BrowserError,
    find_browser_host,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = find_browser_host(PROJECT_ROOT)
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="WebView2 is Windows only")
NEEDS_HOST = pytest.mark.skipif(
    HOST is None, reason="SaltyBrowserHost.exe has not been built"
)

FIRST = """<!doctype html><html><head><title>Alpha Page</title></head><body>
<h1>Alpha</h1>
<label for="a">Alpha field</label><input id="a" type="text" />
<button id="mark" onclick="document.getElementById('out').innerText='alpha clicked'">Alpha button</button>
<a id="pop" href="/second.html" target="_blank">Open second</a>
<div id="out">alpha idle</div>
</body></html>"""

SECOND = """<!doctype html><html><head><title>Beta Page</title></head><body>
<h1>Beta</h1>
<label for="b">Beta field</label><input id="b" type="text" />
<button id="mark" onclick="document.getElementById('out').innerText='beta clicked'">Beta button</button>
<div id="out">beta idle</div>
</body></html>"""


@pytest.fixture()
def served():
    directory = Path(tempfile.mkdtemp(prefix="salty-tabs-"))
    (directory / "first.html").write_text(FIRST, encoding="utf-8")
    (directory / "second.html").write_text(SECOND, encoding="utf-8")

    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            return

    handler = functools.partial(_Quiet, directory=str(directory))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def browser():
    client = BrowserClient(
        HOST, profile_directory=Path(tempfile.mkdtemp(prefix="salty-tab-profile-"))
    )
    try:
        yield client
    finally:
        client.close()


def test_tab_commands_are_split_by_whether_they_change_anything() -> None:
    assert "list_tabs" in BROWSER_READ_COMMANDS
    assert "get_active_tab" in BROWSER_READ_COMMANDS
    for command in ("new_tab", "switch_tab", "close_tab"):
        assert command in BROWSER_MUTATING_COMMANDS


@WINDOWS_ONLY
@NEEDS_HOST
def test_two_tabs_are_opened_listed_switched_and_operated(served, browser) -> None:
    first = browser.call("open_url", {"url": f"{served}/first.html"})
    assert first["title"] == "Alpha Page"
    alpha = first["tab"]

    second = browser.call("new_tab", {"url": f"{served}/second.html"})
    beta = second["tab"]
    assert beta != alpha
    assert second["title"] == "Beta Page"

    listed = browser.call("list_tabs", {})
    assert listed["count"] == 2
    assert {entry["tab"] for entry in listed["tabs"]} == {alpha, beta}
    # Opening a tab makes it the active one.
    assert listed["active"] == beta
    assert browser.call("get_active_tab", {})["tab"] == beta

    # Each tab is operated independently, addressed by handle.
    beta_box = browser.call("query", {"role": "textbox"})["matches"][0]
    browser.call("set_value", {"element": beta_box["element"], "value": "beta text"})

    browser.call("switch_tab", {"tab": alpha})
    assert browser.call("get_active_tab", {})["tab"] == alpha
    alpha_box = browser.call("query", {"role": "textbox"})["matches"][0]
    browser.call("set_value", {"element": alpha_box["element"], "value": "alpha text"})

    # The two pages hold their own state; neither write leaked into the other.
    assert browser.call("get_element", {"element": alpha_box["element"]})["value"] == (
        "alpha text"
    )
    assert browser.call("get_element", {"element": beta_box["element"]})["value"] == (
        "beta text"
    )


@WINDOWS_ONLY
@NEEDS_HOST
def test_an_element_from_one_tab_can_never_act_on_another(served, browser) -> None:
    alpha = browser.call("open_url", {"url": f"{served}/first.html"})["tab"]
    alpha_button = browser.call("query", {"role": "button", "name": "Alpha button"})[
        "matches"
    ][0]["element"]

    beta = browser.call("new_tab", {"url": f"{served}/second.html"})["tab"]

    # The handle carries the tab that produced it.
    assert alpha_button.startswith(f"{alpha}/")

    # Naming a different tab is a contradiction, not a preference to resolve.
    with pytest.raises(BrowserError) as failure:
        browser.call("click", {"element": alpha_button, "tab": beta})
    assert failure.value.kind == "wrong_tab"

    # With beta active, the alpha handle still operates on alpha rather than on
    # whatever happens to be in front.
    assert browser.call("get_active_tab", {})["tab"] == beta
    browser.call("click", {"element": alpha_button})
    browser.call("switch_tab", {"tab": alpha})
    assert "alpha clicked" in browser.call("read_page", {"limit": 10})["summary"]
    assert "beta idle" in browser.call("read_page", {"limit": 10, "tab": beta})["summary"]


@WINDOWS_ONLY
@NEEDS_HOST
def test_a_target_blank_link_becomes_a_real_addressable_tab(served, browser) -> None:
    """A popup that escaped the session would be unreachable and invisible."""

    browser.call("open_url", {"url": f"{served}/first.html"})
    link = browser.call("query", {"role": "link", "name": "Open second"})["matches"][0]

    browser.call("click", {"element": link["element"]})

    listed = browser.call("list_tabs", {})
    assert listed["count"] == 2
    opened = browser.call("get_active_tab", {})
    assert opened["title"] == "Beta Page"
    assert opened["tab"] == listed["active"]


@WINDOWS_ONLY
@NEEDS_HOST
def test_closing_a_tab_updates_the_active_tab_and_kills_its_handles(
    served, browser
) -> None:
    alpha = browser.call("open_url", {"url": f"{served}/first.html"})["tab"]
    beta = browser.call("new_tab", {"url": f"{served}/second.html"})["tab"]
    beta_button = browser.call("query", {"role": "button", "name": "Beta button"})[
        "matches"
    ][0]["element"]

    closed = browser.call("close_tab", {"tab": beta})

    assert closed["closed"] == beta
    assert closed["count"] == 1
    assert closed["active"] == alpha
    assert browser.call("get_active_tab", {})["tab"] == alpha

    # A handle from a closed tab is dead. Reporting it is far better than
    # performing the click somewhere else.
    with pytest.raises(BrowserError) as failure:
        browser.call("click", {"element": beta_button})
    assert failure.value.kind == "stale_element"


@WINDOWS_ONLY
@NEEDS_HOST
def test_navigating_a_tab_invalidates_only_that_tabs_handles(served, browser) -> None:
    alpha = browser.call("open_url", {"url": f"{served}/first.html"})["tab"]
    alpha_button = browser.call("query", {"role": "button", "name": "Alpha button"})[
        "matches"
    ][0]["element"]
    beta = browser.call("new_tab", {"url": f"{served}/second.html"})["tab"]
    beta_button = browser.call("query", {"role": "button", "name": "Beta button"})[
        "matches"
    ][0]["element"]

    browser.call("reload", {"tab": alpha})

    with pytest.raises(BrowserError) as failure:
        browser.call("click", {"element": alpha_button})
    assert failure.value.kind == "stale_element"

    # The other tab was untouched and its handle still works.
    browser.call("click", {"element": beta_button})
    assert "beta clicked" in browser.call("read_page", {"limit": 10, "tab": beta})[
        "summary"
    ]


@WINDOWS_ONLY
@NEEDS_HOST
def test_the_session_always_keeps_a_page(served, browser) -> None:
    browser.call("open_url", {"url": f"{served}/first.html"})

    with pytest.raises(BrowserError, match="last tab"):
        browser.call("close_tab", {})


@WINDOWS_ONLY
@NEEDS_HOST
def test_an_unknown_tab_is_refused_rather_than_guessed(served, browser) -> None:
    browser.call("open_url", {"url": f"{served}/first.html"})

    with pytest.raises(BrowserError) as failure:
        browser.call("read_page", {"tab": "web-tab-99"})
    assert failure.value.kind == "unknown_tab"
