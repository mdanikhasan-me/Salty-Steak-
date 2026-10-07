from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from app.backend.automation.browser_client import (
    BROWSER_COMMANDS,
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

PAGE = """<!doctype html><html><head><title>Salty Steak Test Page</title></head>
<body>
<h1>Structured Browser Proof</h1>
<label for="q">Search</label>
<input id="q" type="text" placeholder="Search" />
<button id="go" onclick="document.getElementById('out').innerText='submitted:'+document.getElementById('q').value">Submit</button>
<button id="d1">Delete</button>
<button id="d2">Delete</button>
<a href="/watch?v=1">Watch video</a>
<div id="out">nothing yet</div>
</body></html>"""


class _StubClient:
    def __init__(self, result=None, error: BrowserError | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"count": 0, "matches": []}
        self.error = error

    def call(self, command, payload=None, **_kwargs):
        self.calls.append((command, dict(payload or {})))
        if self.error is not None:
            raise self.error
        return dict(self.result)

    def close(self) -> None:
        return None


def test_wait_for_contract_is_read_only_and_has_separate_wait_timeout(tmp_path):
    from app.backend.automation.capability_registry import get_capability_descriptor
    assert 'wait_for' in BROWSER_READ_COMMANDS and 'wait_for' not in BROWSER_MUTATING_COMMANDS
    descriptor=get_capability_descriptor('browser.control')
    assert 'wait_timeout_ms' in descriptor.argument_types
    assert 'timeout_ms' not in descriptor.argument_types
    client=_StubClient({'matches':[],'count':0,'timed_out':True,'waited_ms':200})
    broker=_broker(tmp_path,client)
    broker.grant({'capabilities':['browser.control'],'user_confirmed':True})
    result=broker.invoke({'capability':'browser.control','arguments':{
        'command':'wait_for','name':'Ready','wait_timeout_ms':200},'user_confirmed':True})
    assert result['timed_out'] and not result['mutating']
    assert client.calls[-1][1]['wait_timeout_ms']==200


def test_browser_post_action_evidence_survives_model_observation():
    from app.backend.chat.agent_loop import summarise_observation
    result={'status':'succeeded','command':'click','action_dispatched':True,
            'after_state':{'url':'https://example.org/result','title':'Confirmed'},
            'observation_error':None}
    observation=summarise_observation('browser.control',result)
    assert observation['action_dispatched'] and observation['after_state']['title']=='Confirmed'


def _broker(tmp_path: Path, client):
    from app.backend.automation import AutomationBroker
    from app.backend.database.control import Database

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    database = Database(project / "control.db")
    return AutomationBroker(
        database,
        project_root=project,
        artifact_root=project / "workspace" / "automation",
        platform_name="nt",
        browser_client=client,
    )


@pytest.fixture()
def served_page():
    """Serve the fixture over loopback: file: URLs are refused by design."""

    directory = Path(tempfile.mkdtemp(prefix="salty-browser-test-"))
    (directory / "page.html").write_text(PAGE, encoding="utf-8")
    class _Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            return

    handler = functools.partial(_Quiet, directory=str(directory))
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/page.html"
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------- unit coverage


def test_reading_a_page_is_separated_from_changing_it() -> None:
    # The permission model needs to tell these apart: reading a page is not
    # the same risk as clicking something on it.
    assert "read_page" in BROWSER_READ_COMMANDS
    assert "query" in BROWSER_READ_COMMANDS
    assert "click" in BROWSER_MUTATING_COMMANDS
    assert "open_url" in BROWSER_MUTATING_COMMANDS
    assert not (BROWSER_READ_COMMANDS & BROWSER_MUTATING_COMMANDS)
    assert BROWSER_COMMANDS == BROWSER_READ_COMMANDS | BROWSER_MUTATING_COMMANDS


@WINDOWS_ONLY
@NEEDS_HOST
def test_preview_matches_the_owned_tab_and_changes_after_dom_action(served_page, tmp_path):
    import base64
    import hashlib
    from app.backend.application import Application
    from types import SimpleNamespace

    client = BrowserClient(HOST, profile_directory=tmp_path / "profile")
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})
    def invoke(command, **args):
        return broker.invoke({"capability": "browser.control", "arguments": {
            "command": command, **args}, "user_confirmed": True})
    try:
        page = invoke("open_url", url=served_page)
        first = invoke("capture_preview", tab=page["tab"])
        assert first["status"] == "succeeded", first
        assert "png_base64" not in first
        assert first["artifact"]["sha256"] == hashlib.sha256(Path(first["artifact"]["path"]).read_bytes()).hexdigest()
        assert 0 < first["width"] <= 1280 and 0 < first["height"] <= 900
        facade = SimpleNamespace(automation=broker)
        response = Application.browser_preview_content(facade, first["audit_record_id"])
        assert response.content_type == "image/png" and response.path.is_file()
        box = invoke("query", role="textbox")["matches"][0]["element"]
        invoke("set_value", element=box, value="preview proof")
        button = invoke("query", role="button", name="Submit")["matches"][0]["element"]
        invoke("click", element=button)
        assert "submitted:preview proof" in invoke("read_page")["summary"]
        second = invoke("capture_preview", tab=page["tab"])
        assert second["artifact"]["sha256"] != first["artifact"]["sha256"]
        broker.revoke({"capabilities": ["browser.control"]})
        with pytest.raises(PermissionError, match="not enabled"):
            Application.browser_preview_content(facade, first["audit_record_id"])
    finally:
        broker.close()


def test_preview_rejects_malformed_or_oversized_pixels(tmp_path):
    import base64
    import struct
    for payload in ["invalid-base64", base64.b64encode(b"not PNG").decode(),
                    base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0"*8 + struct.pack(">II", 9000, 9000)).decode()]:
        broker = _broker(tmp_path, _StubClient({"png_base64": payload}))
        broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})
        try:
            result = broker.invoke({"capability": "browser.control", "arguments": {
                "command": "capture_preview"}, "user_confirmed": True})
            assert result["status"] == "failed"
            assert not list((broker.artifact_root / "browser-previews").glob("*.png"))
        finally:
            broker.close()


def test_browser_requires_a_grant(tmp_path: Path) -> None:
    client = _StubClient()
    broker = _broker(tmp_path, client)

    with pytest.raises(PermissionError, match="has not been granted"):
        broker.invoke(
            {
                "capability": "browser.control",
                "arguments": {"command": "read_page"},
                "user_confirmed": True,
            }
        )
    assert client.calls == []
    broker.close()


def test_browser_rejects_unknown_commands(tmp_path: Path) -> None:
    client = _StubClient()
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})

    with pytest.raises(ValueError, match="must be one of"):
        broker.invoke(
            {
                "capability": "browser.control",
                "arguments": {"command": "eval_javascript"},
                "user_confirmed": True,
            }
        )
    assert client.calls == []
    broker.close()


def test_a_query_result_is_reported_and_marked_read_only(tmp_path: Path) -> None:
    client = _StubClient(
        {"matches": [{"element": "web-el-3", "role": "button"}], "count": 1}
    )
    broker = _broker(tmp_path, client)
    broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "browser.control",
            "arguments": {"command": "query", "role": "button", "name": "Submit"},
            "user_confirmed": True,
        }
    )

    assert result["status"] == "succeeded"
    assert result["mutating"] is False
    assert result["matches"][0]["element"] == "web-el-3"
    assert broker.audit_records(limit=1)[0]["capability"] == "browser.control"
    broker.close()


def test_a_click_is_recorded_as_mutating(tmp_path: Path) -> None:
    broker = _broker(tmp_path, _StubClient({"element": "web-el-3"}))
    broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "browser.control",
            "arguments": {"command": "click", "element": "web-el-3"},
            "user_confirmed": True,
        }
    )
    assert result["mutating"] is True
    broker.close()


@pytest.mark.parametrize(
    "kind", ["stale_element", "navigation_failed", "timeout", "not_editable"]
)
def test_browser_failures_are_classified(tmp_path: Path, kind: str) -> None:
    broker = _broker(tmp_path, _StubClient(error=BrowserError("no", kind=kind)))
    broker.grant({"capabilities": ["browser.control"], "user_confirmed": True})

    result = broker.invoke(
        {
            "capability": "browser.control",
            "arguments": {"command": "click", "element": "web-el-1"},
            "user_confirmed": True,
        }
    )
    assert result["status"] == "failed"
    assert result["failure_kind"] == kind
    broker.close()


def test_the_browser_sits_above_terminal_and_below_native() -> None:
    from app.backend.automation.routing import (
        TIER_API,
        TIER_NATIVE,
        TIER_TERMINAL,
        ordered_capabilities,
    )

    assert TIER_NATIVE < TIER_API < TIER_TERMINAL
    assert ordered_capabilities(
        ["input.control", "browser.control", "application.launch", "terminal.execute"]
    ) == [
        "application.launch",
        "browser.control",
        "terminal.execute",
        "input.control",
    ]


def test_a_stopped_task_never_starts_a_browser_call() -> None:
    client = BrowserClient.__new__(BrowserClient)

    with pytest.raises(BrowserError) as failure:
        BrowserClient.call(client, "click", {"element": "x"}, should_stop=lambda: True)
    assert failure.value.kind == "cancelled"


def test_repeated_browser_failures_reach_the_stagnation_detector() -> None:
    from app.backend.chat.agent_loop import AgentLoop
    from tests.test_agent_loop import _RecordingBroker

    broker = _RecordingBroker(
        [{"status": "failed", "failure_kind": "not_found", "command": "query"}] * 10
    )
    offered: list[list[str]] = []

    def generate(messages: list[dict[str, str]]) -> str:
        last = messages[-1]["content"]
        if "not making progress" in last:
            offered.append(json.loads(last)["available"])
            return '{"action":"respond","answer":"That element is not on the page."}'
        return (
            '{"action":"browser.control","reason":"look",'
            '"arguments":{"command":"query","role":"button","name":"Buy"}}'
        )

    outcome = AgentLoop(
        broker=broker,
        generate=generate,
        capabilities=["browser.control", "screen.capture", "application.launch"],
        max_iterations=15,
    ).run("buy the thing")

    assert outcome["state"] == "completed"
    assert len(broker.calls) == 3
    assert outcome["metrics"]["api_calls"] == 3
    assert outcome["metrics"]["stagnation_breaks"] == 1
    assert "screen.capture" in offered[0]


# ------------------------------------------------------- real browser coverage


@WINDOWS_ONLY
@NEEDS_HOST
def test_a_real_page_is_read_and_operated_structurally(served_page: str) -> None:
    """End-to-end proof against a real Chromium page.

    Navigates, finds controls by role and accessible name, types, clicks, and
    verifies the result from the DOM. No screenshots and no coordinates.
    """

    profile = Path(tempfile.mkdtemp(prefix="salty-browser-profile-"))
    client = BrowserClient(HOST, profile_directory=profile)
    try:
        assert client.available() is True

        page = client.call("open_url", {"url": served_page})
        assert page["title"] == "Salty Steak Test Page"

        summary = client.call("read_page", {"limit": 20})
        assert summary["control_count"] >= 5

        # Found by what it is, not by where it sits on screen.
        boxes = client.call("query", {"role": "textbox", "editable": True})
        assert boxes["count"] == 1
        box = boxes["matches"][0]
        assert box["name"] == "Search"

        client.call("set_value", {"element": box["element"], "value": "salty steak"})
        assert client.call("get_element", {"element": box["element"]})["value"] == (
            "salty steak"
        )

        submit = client.call("query", {"role": "button", "name": "Submit"})
        assert submit["count"] == 1
        client.call("click", {"element": submit["matches"][0]["element"]})

        # Verified from the page's own state, not from a picture of it.
        assert "submitted:salty steak" in client.call("read_page", {"limit": 5})["summary"]

        # Two identical buttons are reported, never silently chosen between.
        duplicates = client.call("query", {"role": "button", "name": "Delete"})
        assert duplicates["count"] == 2
        assert duplicates["ambiguous"] is True

        links = client.call("query", {"href": "/watch"})
        assert links["matches"][0]["name"] == "Watch video"

        # A re-render invalidates handles; the old one must not be acted on.
        client.call("open_url", {"url": served_page})
        with pytest.raises(BrowserError) as failure:
            client.call("click", {"element": box["element"]})
        assert failure.value.kind == "stale_element"
    finally:
        client.close()


@WINDOWS_ONLY
@NEEDS_HOST
def test_the_session_only_opens_web_addresses() -> None:
    """A browser capability must not become arbitrary local file access."""

    profile = Path(tempfile.mkdtemp(prefix="salty-browser-scheme-"))
    client = BrowserClient(HOST, profile_directory=profile)
    try:
        for target in (
            "file:///C:/Windows/System32/drivers/etc/hosts",
            "javascript:alert(1)",
            "ms-settings:privacy",
        ):
            with pytest.raises(BrowserError) as failure:
                client.call("open_url", {"url": target})
            assert failure.value.kind == "invalid_request"
    finally:
        client.close()
