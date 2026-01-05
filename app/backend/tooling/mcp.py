"""Minimal dependency-free MCP client for explicitly configured stdio servers.

This transport performs MCP lifecycle negotiation and tool discovery. Tool
execution is denied unless the caller explicitly enables it for the client
instance; the application does not yet expose that switch through its API.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({MCP_PROTOCOL_VERSION, "2025-06-18"})
MAX_MCP_MESSAGE_BYTES = 4 * 1024 * 1024


class McpProtocolError(RuntimeError):
    """Raised when an MCP peer violates the negotiated JSON-RPC contract."""


class McpStdioClient:
    """Small synchronous MCP stdio transport with secure defaults."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        allow_tool_calls: bool = False,
    ) -> None:
        checked_command = tuple(str(part) for part in command)
        if not checked_command or not checked_command[0].strip():
            raise ValueError("An MCP stdio command is required")
        checked_cwd = Path(cwd).resolve() if cwd is not None else None
        if checked_cwd is not None and not checked_cwd.is_dir():
            raise ValueError("The MCP working directory does not exist")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("MCP timeout must be between 0 and 120 seconds")
        self.command = checked_command
        self.cwd = checked_cwd
        self.environment = _safe_environment(environment)
        self.timeout_seconds = float(timeout_seconds)
        self.allow_tool_calls = bool(allow_tool_calls)
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._initialized = False
        self.server_info: dict[str, Any] | None = None
        self.server_capabilities: dict[str, Any] = {}

    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            list(self.command),
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            shell=False,
        )

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {
                "serverInfo": self.server_info or {},
                "capabilities": self.server_capabilities,
            }
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "salty-steak",
                    "title": "Salty Steak",
                    "version": "2.0.0",
                },
            },
        )
        protocol_version = str(result.get("protocolVersion") or "")
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            self.close()
            raise McpProtocolError(
                f"The MCP server selected unsupported protocol {protocol_version!r}"
            )
        self.server_info = _mapping(result.get("serverInfo"), "serverInfo")
        self.server_capabilities = _mapping(
            result.get("capabilities"), "capabilities"
        )
        self._notify("notifications/initialized")
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise McpProtocolError("MCP tools/list returned an invalid tools array")
        return [dict(item) for item in tools]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not self.allow_tool_calls:
            raise PermissionError("MCP tool calls require an explicit host permission")
        checked_name = str(name).strip()
        if not checked_name:
            raise ValueError("MCP tool name cannot be empty")
        self.initialize()
        return self._request(
            "tools/call", {"name": checked_name, "arguments": dict(arguments)}
        )

    def close(self) -> None:
        process, self._process = self._process, None
        self._initialized = False
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        response = self._read_message()
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise McpProtocolError("MCP response identity does not match its request")
        if "error" in response:
            raise McpProtocolError(f"MCP request {method} failed: {response['error']!r}")
        return _mapping(response.get("result"), "result")

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _send(self, message: Mapping[str, Any]) -> None:
        self.start()
        assert self._process is not None
        if self._process.poll() is not None or self._process.stdin is None:
            raise McpProtocolError("The MCP server exited before receiving a message")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if "\n" in encoded or len(encoded.encode("utf-8")) > MAX_MCP_MESSAGE_BYTES:
            raise McpProtocolError("The MCP message exceeds the stdio transport limit")
        self._process.stdin.write(encoded + "\n")
        self._process.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        results: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                results.put(self._process.stdout.readline(MAX_MCP_MESSAGE_BYTES + 1))
            except BaseException as error:
                results.put(error)

        threading.Thread(target=read, daemon=True).start()
        try:
            received = results.get(timeout=self.timeout_seconds)
        except queue.Empty as error:
            self.close()
            raise TimeoutError("MCP server did not respond before the timeout") from error
        if isinstance(received, BaseException):
            raise McpProtocolError("MCP server response could not be read") from received
        if not received:
            raise McpProtocolError("MCP server closed stdout without a response")
        if len(received.encode("utf-8")) > MAX_MCP_MESSAGE_BYTES:
            raise McpProtocolError("MCP server response exceeds the transport limit")
        try:
            message = json.loads(received)
        except json.JSONDecodeError as error:
            raise McpProtocolError("MCP server returned invalid JSON") from error
        if not isinstance(message, dict):
            raise McpProtocolError("MCP server response must be a JSON object")
        return message


def _safe_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    allow = {"COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allow}
    for key, value in (overrides or {}).items():
        checked_key = str(key)
        if not checked_key or "=" in checked_key or "\x00" in checked_key:
            raise ValueError("Invalid MCP environment variable name")
        checked_value = str(value)
        if "\x00" in checked_value:
            raise ValueError("Invalid MCP environment variable value")
        environment[checked_key] = checked_value
    return environment


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class McpHttpClient:
    """Minimal MCP Streamable HTTP client with redirects denied by default."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        allow_tool_calls: bool = False,
    ) -> None:
        parsed = urllib.parse.urlsplit(str(endpoint).strip())
        localhost = (parsed.hostname or "").casefold() in {
            "127.0.0.1", "::1", "localhost"
        }
        if parsed.scheme != "https" and not (parsed.scheme == "http" and localhost):
            raise ValueError("MCP HTTP requires HTTPS, except for loopback endpoints")
        if parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("MCP endpoint credentials must not be embedded in the URL")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("MCP timeout must be between 0 and 120 seconds")
        self.endpoint = urllib.parse.urlunsplit(parsed)
        self.headers = _safe_http_headers(headers)
        self.timeout_seconds = float(timeout_seconds)
        self.allow_tool_calls = bool(allow_tool_calls)
        self.session_id: str | None = None
        self._next_id = 1
        self._initialized = False
        self.server_info: dict[str, Any] | None = None
        self.server_capabilities: dict[str, Any] = {}
        self._opener = urllib.request.build_opener(_NoRedirects())

    def __enter__(self) -> "McpHttpClient":
        self.initialize()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {
                "serverInfo": self.server_info or {},
                "capabilities": self.server_capabilities,
            }
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "salty-steak",
                    "title": "Salty Steak",
                    "version": "2.0.0",
                },
            },
        )
        protocol_version = str(result.get("protocolVersion") or "")
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            self.close()
            raise McpProtocolError(
                f"The MCP server selected unsupported protocol {protocol_version!r}"
            )
        self.server_info = _mapping(result.get("serverInfo"), "serverInfo")
        self.server_capabilities = _mapping(
            result.get("capabilities"), "capabilities"
        )
        self._notification("notifications/initialized")
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise McpProtocolError("MCP tools/list returned an invalid tools array")
        return [dict(item) for item in tools]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not self.allow_tool_calls:
            raise PermissionError("MCP tool calls require an explicit host permission")
        checked_name = str(name).strip()
        if not checked_name:
            raise ValueError("MCP tool name cannot be empty")
        self.initialize()
        return self._request(
            "tools/call", {"name": checked_name, "arguments": dict(arguments)}
        )

    def close(self) -> None:
        if self.session_id:
            request = urllib.request.Request(
                self.endpoint,
                method="DELETE",
                headers=self._request_headers(),
            )
            try:
                self._opener.open(request, timeout=self.timeout_seconds).close()
            except (OSError, urllib.error.HTTPError):
                pass
        self.session_id = None
        self._initialized = False

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
            raise McpProtocolError("MCP response identity does not match its request")
        if "error" in response:
            raise McpProtocolError(f"MCP request {method} failed: {response['error']!r}")
        return _mapping(response.get("result"), "result")

    def _notification(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}, response_required=False)

    def _post(
        self, message: Mapping[str, Any], *, response_required: bool = True
    ) -> dict[str, Any]:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_MCP_MESSAGE_BYTES:
            raise McpProtocolError("The MCP message exceeds the HTTP transport limit")
        request = urllib.request.Request(
            self.endpoint,
            data=encoded,
            method="POST",
            headers=self._request_headers(),
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    if not all(0x21 <= ord(character) <= 0x7E for character in session_id):
                        raise McpProtocolError("MCP session id contains invalid characters")
                    self.session_id = session_id
                body = response.read(MAX_MCP_MESSAGE_BYTES + 1)
                content_type = str(response.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as error:
            raise McpProtocolError(f"MCP HTTP request failed with status {error.code}") from error
        except OSError as error:
            raise McpProtocolError("MCP HTTP transport failed") from error
        if not response_required and not body:
            return {}
        if len(body) > MAX_MCP_MESSAGE_BYTES:
            raise McpProtocolError("MCP HTTP response exceeds the transport limit")
        return _decode_http_message(body, content_type)

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers


def _safe_http_headers(values: Mapping[str, str] | None) -> dict[str, str]:
    blocked = {"content-length", "host", "mcp-session-id", "mcp-protocol-version"}
    headers: dict[str, str] = {}
    for key, value in (values or {}).items():
        checked_key, checked_value = str(key).strip(), str(value).strip()
        if (
            not checked_key
            or checked_key.casefold() in blocked
            or any(character in checked_key + checked_value for character in "\r\n\x00")
        ):
            raise ValueError("Invalid or host-controlled MCP HTTP header")
        headers[checked_key] = checked_value
    return headers


def _decode_http_message(body: bytes, content_type: str) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise McpProtocolError("MCP HTTP response is not UTF-8") from error
    if "text/event-stream" in content_type.casefold():
        data_lines = [
            line[5:].lstrip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            raise McpProtocolError("MCP event stream did not contain a data event")
        text = "\n".join(data_lines)
    try:
        message = json.loads(text)
    except json.JSONDecodeError as error:
        raise McpProtocolError("MCP HTTP server returned invalid JSON") from error
    if not isinstance(message, dict):
        raise McpProtocolError("MCP HTTP response must be a JSON object")
    return message


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpProtocolError(f"MCP {label} must be an object")
    return dict(value)
