from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.backend.database.control import Database
from app.backend.tooling import McpHttpClient, McpStdioClient, PluginRegistry
from app.backend.tooling.secrets import DpapiSecretStore


class _MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, reference: str, value: str) -> None:
        self.values[reference] = value

    def get(self, reference: str) -> str | None:
        return self.values.get(reference)

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


def _registry(tmp_path: Path) -> PluginRegistry:
    return PluginRegistry(Database(tmp_path / "control.db"))


def test_registry_exposes_plugins_without_legacy_calculator_or_browser(
    tmp_path: Path,
) -> None:
    payload = _registry(tmp_path).describe()
    plugins = {item["id"]: item for item in payload["plugins"]}

    # Two different things, named apart: this application's own capabilities
    # are tools, and an external account is a connected app. Calling web search
    # a "plugin" told the user they had installed something they had not.
    assert payload["terminology"] == "tools_and_connected_apps"
    assert [item["id"] for item in payload["tools"]] == [
        item["id"] for item in payload["plugins"]
    ]
    assert {item["name"] for item in payload["connected_apps"]} >= {"Gmail"}
    assert payload["network_default"] == "off_except_built_in_read_only_web_search"
    assert payload["capabilities"] == payload["plugins"]
    assert "calculator" not in plugins
    assert "browser" not in plugins
    assert plugins["web_search"]["availability"] == "available"
    assert plugins["web_search"]["invocation"] == "automatic_when_needed"
    assert plugins["web_search"]["privacy"] == "network_required"
    assert plugins["text_files"]["availability"] == "available"


def test_connector_records_are_durable_fail_closed_and_scope_explicit(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "control.db")
    first = PluginRegistry(database).connectors()
    second = PluginRegistry(database).connectors()
    connectors = {item["id"]: item for item in second}

    assert len(first) == len(second) == 4
    assert set(connectors) == {
        "gmail",
        "google-calendar",
        "icloud-calendar",
        "mcp",
    }
    for connector in connectors.values():
        assert connector["status"] == "disconnected"
        assert connector["enabled"] is False
        assert connector["granted_scopes"] == []
        assert connector["credentials_present"] is False
        assert connector["configuration"]["secrets_in_database"] is False

    gmail_scopes = {
        scope["provider_scope"]
        for scope in connectors["gmail"]["permission_scopes"]
    }
    assert "https://www.googleapis.com/auth/gmail.metadata" in gmail_scopes
    assert "https://www.googleapis.com/auth/gmail.send" in gmail_scopes
    assert connectors["icloud-calendar"]["configuration"][
        "requires_app_specific_password"
    ] is True
    assert connectors["mcp"]["configuration"]["transport_ready"] is True
    assert connectors["mcp"]["configuration"]["supported_transports"] == [
        "stdio",
        "streamable_http",
    ]


def test_mcp_stdio_negotiates_discovers_and_denies_calls_by_default(
    tmp_path: Path,
) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text(
        """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ok"}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    with McpStdioClient([sys.executable, "-u", str(server)]) as client:
        assert [tool["name"] for tool in client.list_tools()] == ["echo"]
        with pytest.raises(PermissionError, match="explicit host permission"):
            client.call_tool("echo", {"text": "hello"})

    with McpStdioClient(
        [sys.executable, "-u", str(server)], allow_tool_calls=True
    ) as client:
        result = client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == "ok"


def test_mcp_streamable_http_negotiates_session_and_discovers_tools() -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            message = json.loads(self.rfile.read(length))
            if message["method"] == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if message["method"] == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-fixture", "version": "1"},
                }
            else:
                assert self.headers["Mcp-Session-Id"] == "session-1"
                assert self.headers["MCP-Protocol-Version"] == "2025-11-25"
                result = {
                    "tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]
                }
            body = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if message["method"] == "initialize":
                self.send_header("Mcp-Session-Id", "session-1")
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with McpHttpClient(f"http://127.0.0.1:{server.server_port}/mcp") as client:
            assert [tool["name"] for tool in client.list_tools()] == ["lookup"]
            assert client.session_id == "session-1"
            with pytest.raises(PermissionError, match="explicit host permission"):
                client.call_tool("lookup", {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_http_rejects_insecure_non_loopback_endpoints() -> None:
    with pytest.raises(ValueError, match="requires HTTPS"):
        McpHttpClient("http://example.com/mcp")


def test_registry_configures_tests_calls_and_disconnects_mcp_http(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            message = json.loads(self.rfile.read(length))
            requests.append(message)
            assert self.headers["Authorization"] == "Bearer protected-token"
            method = message["method"]
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "registry-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": "search", "inputSchema": {"type": "object"}},
                        {"name": "write_file", "inputSchema": {"type": "object"}},
                    ]
                }
            elif method == "tools/call":
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": f"searched:{message['params']['arguments']['q']}",
                        }
                    ]
                }
            else:  # pragma: no cover - fixture contract guard
                raise AssertionError(method)
            body = json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": result}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Mcp-Session-Id", "registry-session")
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database = Database(tmp_path / "control.db")
    secrets = _MemorySecretStore()
    registry = PluginRegistry(database, secret_store=secrets)
    try:
        configured = registry.configure_mcp(
            {
                "endpoint": f"http://127.0.0.1:{server.server_port}/mcp",
                "timeout_seconds": 5,
                "allowed_tools": ["search"],
                "bearer_token": "protected-token",
            }
        )
        assert configured["status"] == "configured"
        assert configured["credentials_present"] is True
        assert configured["configuration"]["allowed_tools"] == ["search"]
        assert "bearer_token" not in json.dumps(configured)

        raw = database.fetch_one(
            "SELECT * FROM plugin_connectors WHERE id = 'mcp'"
        )
        assert raw is not None
        assert "protected-token" not in json.dumps(raw)
        assert raw["credential_reference"] in secrets.values

        tested = registry.test_mcp()
        assert tested["connector"]["status"] == "connected"
        assert tested["discovered_tool_names"] == ["search", "write_file"]
        assert tested["missing_allowed_tools"] == []
        assert tested["connector"]["configuration"][
            "verified_allowed_tools"
        ] == ["search"]
        assert tested["connector"]["configuration"][
            "discovered_tool_names"
        ] == ["search", "write_file"]

        called = registry.call_mcp(
            {"name": "search", "arguments": {"q": "steak"}}
        )
        assert called["result"]["content"][0]["text"] == "searched:steak"
        with pytest.raises(PermissionError, match="verified allowlist"):
            registry.call_mcp({"name": "write_file", "arguments": {}})

        disconnected = registry.disconnect_mcp()
        assert disconnected["status"] == "disconnected"
        assert disconnected["enabled"] is False
        assert disconnected["credentials_present"] is False
        assert disconnected["configuration"]["streamable_http_endpoint"] is None
        assert secrets.values == {}

        for connector_id in (
            "gmail",
            "google-calendar",
            "icloud-calendar",
        ):
            bridged = registry.configure_connector(
                connector_id,
                {
                    "endpoint": f"http://127.0.0.1:{server.server_port}/mcp",
                    "allowed_tools": ["search"],
                    "bearer_token": "protected-token",
                },
            )
            assert bridged["id"] == connector_id
            assert bridged["transport"] == "streamable_http"
            assert bridged["configuration"]["connection_mode"] == (
                "mcp_streamable_http_bridge"
            )
            assert bridged["configuration"][
                "direct_provider_transport_available"
            ] is False
            assert registry.test_connector(connector_id)["connector"][
                "status"
            ] == "connected"
            assert registry.disconnect_connector(connector_id)["status"] == (
                "disconnected"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert any(request["method"] == "tools/list" for request in requests)
    assert any(request["method"] == "tools/call" for request in requests)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is required")
def test_dpapi_secret_store_writes_only_protected_bytes(tmp_path: Path) -> None:
    store = DpapiSecretStore(tmp_path / "secrets")
    store.set("mcp-test", "plaintext-token")
    payloads = list((tmp_path / "secrets").glob("*.dpapi"))
    assert len(payloads) == 1
    assert b"plaintext-token" not in payloads[0].read_bytes()
    assert store.get("mcp-test") == "plaintext-token"
    store.delete("mcp-test")
    assert store.get("mcp-test") is None


def test_connector_rows_never_contain_secret_material(tmp_path: Path) -> None:
    database = Database(tmp_path / "control.db")
    PluginRegistry(database)
    rows = database.fetch_all("SELECT * FROM plugin_connectors")

    for row in rows:
        assert row["credential_reference"] is None
        assert json.loads(row["granted_scopes_json"]) == []
        assert "token" not in row["configuration_json"].casefold()
        assert "password" not in row["configuration_json"].casefold() or row[
            "provider"
        ] == "icloud_calendar"


def test_computer_control_plugins_report_the_broker_rather_than_a_table(
    tmp_path: Path,
) -> None:
    # The picker told the user Terminal, Screen capture and App control were
    # "planned" and "not installed yet" long after the broker shipped them,
    # because the plugin table was written before they existed and never read
    # the broker again.
    registry = PluginRegistry(Database(tmp_path / "control.db"))
    status = {
        "capabilities": [
            {
                "capability": "terminal.execute",
                "runtime_available": True,
                "effective_enabled": True,
            },
            {
                "capability": "screen.capture",
                "runtime_available": True,
                "effective_enabled": False,
            },
            {
                "capability": "ui.automation",
                "runtime_available": False,
                "effective_enabled": False,
                "runtime_unavailable_reason": "The helper host is not in this build.",
            },
        ]
    }

    plugins = {
        item["id"]: item
        for item in registry.describe(automation_status=status)["plugins"]
    }

    assert plugins["terminal"]["availability"] == "available"
    assert plugins["terminal"]["granted"] is True
    # Runnable but not yet granted is still offerable: the grant is the user's.
    assert plugins["screen_capture"]["availability"] == "available"
    assert plugins["screen_capture"]["granted"] is False
    assert plugins["app_control"]["availability"] == "unavailable"
    assert "not in this build" in plugins["app_control"]["unavailable_reason"]
    # Screen recording has no capability behind it and must not claim one.
    assert plugins["screen_recording"]["availability"] == "planned"


def test_the_terminal_plugin_does_not_call_itself_a_sandbox(tmp_path: Path) -> None:
    # terminal.execute confines the working directory, not the executable, and
    # status() says so. UI copy must not claim more than the boundary gives.
    registry = PluginRegistry(Database(tmp_path / "control.db"))
    terminal = next(
        item
        for item in registry.describe()["plugins"]
        if item["id"] == "terminal"
    )
    combined = f"{terminal['description']} {terminal['unavailable_reason'] or ''}"
    assert "sandbox" not in combined.lower()
