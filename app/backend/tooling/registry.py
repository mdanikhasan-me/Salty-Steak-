"""Application-owned Plugins registry and durable connector boundary.

The registry describes what the host can do; model output never grants a
permission or activates a connector.  Account secrets are deliberately not
stored in SQLite.  Existing ``/tools`` callers receive the same top-level
``capabilities`` shape while new code can use the Plugins terminology.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..database.control import Database, json_text, parse_json, utc_now
from .mcp import McpHttpClient
from .secrets import DpapiSecretStore, SecretStore


@dataclass(frozen=True, slots=True)
class PluginCapability:
    id: str
    name: str
    category: str
    privacy: str
    availability: str
    permission: str
    description: str
    unavailable_reason: str | None = None
    invocation: str = "user_selected"


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    id: str
    provider: str
    display_name: str
    transport: str
    permission_scopes: tuple[dict[str, Any], ...]
    configuration: dict[str, Any]
    credential_storage: str


_PLUGIN_CAPABILITIES = (
    PluginCapability(
        "web_search",
        "Web search",
        "built_in_plugin",
        "network_required",
        "available",
        "automatic_read_only",
        "Search the web automatically when a response needs current information.",
        None,
        "automatic_when_needed",
    ),
    PluginCapability(
        "text_files",
        "Text files",
        "attachment_plugin",
        "local_only",
        "available",
        "file_picker",
        "Read user-selected text files into the current prompt only.",
    ),
    PluginCapability(
        "images",
        "Images",
        "attachment_plugin",
        "local_only",
        "model_required",
        "file_picker",
        "Attach local images when a vision-capable model is installed.",
        "The current runtime has no vision encoder.",
    ),
    PluginCapability(
        "terminal",
        "Terminal",
        "computer_control_plugin",
        "local_only",
        "planned",
        "ask_every_time",


        "Run a reviewed command with its working directory confined to the "
        "project, after you allow it.",
        "The command broker is not part of this build.",
    ),
    PluginCapability(
        "screen_capture",
        "Screen capture",
        "computer_control_plugin",
        "local_only",
        "planned",
        "ask_every_time",
        "Capture a selected screen or window after visible consent.",
        "The capture broker has not been installed yet.",
    ),
    PluginCapability(
        "screen_recording",
        "Screen recording",
        "computer_control_plugin",
        "local_only",
        "planned",
        "ask_every_time",
        "Record a selected screen with a persistent recording indicator.",
        "The recording broker has not been installed yet.",
    ),
    PluginCapability(
        "app_control",
        "App control",
        "computer_control_plugin",
        "local_only",
        "planned",
        "ask_every_time",
        "Inspect and control allow-listed Windows applications with an audit trail.",
        "The Windows automation broker has not been installed yet.",
    ),
)


_CONNECTOR_DEFINITIONS = (
    ConnectorDefinition(
        "gmail",
        "gmail",
        "Gmail",
        "google_oauth2",
        (
            {
                "id": "mail.metadata.read",
                "provider_scope": "https://www.googleapis.com/auth/gmail.metadata",
                "access": "read_metadata",
            },
            {
                "id": "mail.message.read",
                "provider_scope": "https://www.googleapis.com/auth/gmail.readonly",
                "access": "read_content",
            },
            {
                "id": "mail.draft.write",
                "provider_scope": "https://www.googleapis.com/auth/gmail.compose",
                "access": "write_drafts",
                "confirmation": "always",
            },
            {
                "id": "mail.send",
                "provider_scope": "https://www.googleapis.com/auth/gmail.send",
                "access": "send",
                "confirmation": "always",
            },
        ),
        {
            "oauth_client_configured": False,
            "direct_provider_transport_status": "not_implemented",
            "mcp_streamable_http_bridge_available": True,
            "secrets_in_database": False,
            "default_grant": [],
        },
        "windows_credential_manager",
    ),
    ConnectorDefinition(
        "google-calendar",
        "google_calendar",
        "Google Calendar",
        "google_oauth2",
        (
            {
                "id": "calendar.events.read",
                "provider_scope": "https://www.googleapis.com/auth/calendar.events.readonly",
                "access": "read",
            },
            {
                "id": "calendar.events.write",
                "provider_scope": "https://www.googleapis.com/auth/calendar.events",
                "access": "write",
                "confirmation": "always",
            },
        ),
        {
            "oauth_client_configured": False,
            "direct_provider_transport_status": "not_implemented",
            "mcp_streamable_http_bridge_available": True,
            "secrets_in_database": False,
            "default_grant": [],
        },
        "windows_credential_manager",
    ),
    ConnectorDefinition(
        "icloud-calendar",
        "icloud_calendar",
        "iCloud Calendar",
        "caldav",
        (
            {"id": "calendar.events.read", "access": "read"},
            {
                "id": "calendar.events.write",
                "access": "write",
                "confirmation": "always",
            },
        ),
        {
            "server_configured": False,
            "requires_app_specific_password": True,
            "direct_provider_transport_status": "not_implemented",
            "mcp_streamable_http_bridge_available": True,
            "secrets_in_database": False,
            "default_grant": [],
        },
        "windows_credential_manager",
    ),
    ConnectorDefinition(
        "mcp",
        "mcp",
        "Model Context Protocol",
        "mcp",
        (
            {"id": "mcp.tools.list", "access": "discover"},
            {
                "id": "mcp.tools.call",
                "access": "execute",
                "confirmation": "always",
            },
            {"id": "mcp.resources.list", "access": "discover"},
            {"id": "mcp.resources.read", "access": "read"},
        ),
        {
            "command": [],
            "cwd": None,
            "streamable_http_endpoint": None,
            "timeout_seconds": 10.0,
            "allowed_tools": [],
            "verified_allowed_tools": [],
            "supported_transports": ["stdio", "streamable_http"],
            "protocol_versions": ["2025-11-25", "2025-06-18"],
            "transport_ready": True,
            "secrets_in_database": False,
            "default_grant": [],
        },
        "dpapi_protected_file",
    ),
)


_MAX_ALLOWED_MCP_TOOLS = 128


class PluginRegistry:
    """Durable Plugins catalogue with fail-closed connector defaults."""

    def __init__(
        self,
        database: Database,
        *,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.database = database
        self.secret_store = secret_store or DpapiSecretStore(
            self.database.path.parent / "plugin-secrets"
        )
        self._seed_connectors()

    def _seed_connectors(self) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            for definition in _CONNECTOR_DEFINITIONS:
                connection.execute(
                    """
                    INSERT INTO plugin_connectors(
                        id, provider, display_name, status, enabled, transport,
                        permission_scopes_json, granted_scopes_json,
                        configuration_json, credential_storage,
                        credential_reference, last_error_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'disconnected', 0, ?, ?, '[]', ?, ?, NULL, NULL, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        provider = excluded.provider,
                        display_name = excluded.display_name,
                        permission_scopes_json = excluded.permission_scopes_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        definition.id,
                        definition.provider,
                        definition.display_name,
                        definition.transport,
                        json_text(definition.permission_scopes),
                        json_text(definition.configuration),
                        definition.credential_storage,
                        now,
                        now,
                    ),
                )



            row = connection.execute(
                "SELECT configuration_json FROM plugin_connectors WHERE id = 'mcp'"
            ).fetchone()
            connection.execute(
                """
                UPDATE plugin_connectors
                SET credential_storage = 'dpapi_protected_file'
                WHERE id = 'mcp' AND credential_reference IS NULL
                """
            )
            configuration = parse_json(row["configuration_json"], {})
            defaults = next(
                definition.configuration
                for definition in _CONNECTOR_DEFINITIONS
                if definition.id == "mcp"
            )
            changed = False
            for key, value in defaults.items():
                if key not in configuration:
                    configuration[key] = value
                    changed = True
            if changed:
                connection.execute(
                    """
                    UPDATE plugin_connectors
                    SET configuration_json = ?, updated_at = ?
                    WHERE id = 'mcp'
                    """,
                    (json_text(configuration), now),
                )

    def connectors(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM plugin_connectors ORDER BY display_name COLLATE NOCASE"
        )
        return [self._public_connector(row) for row in rows]

    def configure_connector(
        self, connector_id: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist a Streamable HTTP endpoint and explicit tool grant.

        The bearer credential is protected by Windows DPAPI before SQLite is
        changed. It is never included in connector configuration or responses.
        """

        endpoint = str(request.get("endpoint") or "").strip()
        if not endpoint:
            raise ValueError("An MCP Streamable HTTP endpoint is required")
        timeout_seconds = float(request.get("timeout_seconds", 10.0))


        McpHttpClient(endpoint, timeout_seconds=timeout_seconds)
        allowed_tools = _tool_names(request.get("allowed_tools", []))

        current = self._connector_row(connector_id)
        old_configuration = parse_json(current["configuration_json"], {})
        credential_reference = current.get("credential_reference")
        protected_reference = (
            f"{connector_id}-streamable-http-bearer-v1"
        )
        supplied_secret = "bearer_token" in request
        bearer_token = str(request.get("bearer_token") or "").strip()
        endpoint_changed = (
            str(old_configuration.get("streamable_http_endpoint") or "")
            != endpoint
        )
        if supplied_secret:
            if bearer_token:
                self.secret_store.set(protected_reference, bearer_token)
                credential_reference = protected_reference
            else:
                if credential_reference:
                    self.secret_store.delete(str(credential_reference))
                credential_reference = None
        elif endpoint_changed and credential_reference:

            self.secret_store.delete(str(credential_reference))
            credential_reference = None

        configuration = {
            **old_configuration,
            "streamable_http_endpoint": endpoint,
            "timeout_seconds": timeout_seconds,
            "allowed_tools": allowed_tools,
            "verified_allowed_tools": [],
            "connection_mode": "mcp_streamable_http_bridge",
            "direct_provider_transport_available": False,
            "secrets_in_database": False,
        }
        granted_scopes = ["mcp.tools.list"]
        if allowed_tools:
            granted_scopes.append("mcp.tools.call")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE plugin_connectors
                SET status = 'configured', enabled = 1,
                    transport = 'streamable_http',
                    granted_scopes_json = ?, configuration_json = ?,
                    credential_storage = 'dpapi_protected_file',
                    credential_reference = ?, last_error_json = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json_text(granted_scopes),
                    json_text(configuration),
                    credential_reference,
                    now,
                    connector_id,
                ),
            )
        return self._public_connector(self._connector_row(connector_id))

    def test_connector(self, connector_id: str) -> dict[str, Any]:
        """Negotiate MCP and prove tools/list against the configured server."""

        row = self._connector_row(connector_id)
        configuration = self._configured_http_connector(row)
        try:
            with self._http_connector_client(row, configuration) as client:
                tools = client.list_tools()
                server_info = dict(client.server_info or {})
        except Exception as error:
            self._record_connector_error(
                connector_id, "connection_test_failed", str(error)
            )
            raise

        discovered = sorted(
            {
                str(tool.get("name") or "").strip()
                for tool in tools
                if str(tool.get("name") or "").strip()
            }
        )
        allowed = _tool_names(configuration.get("allowed_tools", []))
        missing = sorted(set(allowed) - set(discovered))
        verified = sorted(set(allowed) & set(discovered))
        configuration["discovered_tool_names"] = discovered
        configuration["verified_allowed_tools"] = verified
        status = "degraded" if missing else "connected"
        last_error = (
            {
                "code": "allowed_tools_not_discovered",
                "tool_names": missing,
            }
            if missing
            else None
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE plugin_connectors
                SET status = ?, configuration_json = ?, last_error_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json_text(configuration),
                    json_text(last_error) if last_error else None,
                    utc_now(),
                    connector_id,
                ),
            )
        return {
            "connector": self._public_connector(
                self._connector_row(connector_id)
            ),
            "server_info": server_info,
            "tools": tools,
            "discovered_tool_names": discovered,
            "missing_allowed_tools": missing,
        }

    def call_connector(
        self, connector_id: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Call one previously discovered, explicitly allow-listed MCP tool."""

        row = self._connector_row(connector_id)
        configuration = self._configured_http_connector(row)
        if row["status"] != "connected":
            raise PermissionError(
                "The MCP connector must pass its connection test before tool calls"
            )
        name = str(request.get("name") or "").strip()
        if not name:
            raise ValueError("An MCP tool name is required")
        verified = set(_tool_names(configuration.get("verified_allowed_tools", [])))
        if name not in verified:
            raise PermissionError(
                "The MCP tool is not in the connector's verified allowlist"
            )
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be a JSON object")
        try:
            with self._http_connector_client(
                row, configuration, allow_tool_calls=True
            ) as client:
                result = client.call_tool(name, arguments)
        except Exception as error:
            self._record_connector_error(
                connector_id, "tool_call_failed", str(error)
            )
            raise
        return {
            "connector_id": connector_id,
            "tool_name": name,
            "result": result,
        }

    def disconnect_connector(self, connector_id: str) -> dict[str, Any]:
        row = self._connector_row(connector_id)
        credential_reference = row.get("credential_reference")
        if credential_reference:
            self.secret_store.delete(str(credential_reference))
        definition = self._connector_definition(connector_id)
        defaults = dict(definition.configuration)
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE plugin_connectors
                SET status = 'disconnected', enabled = 0, transport = ?,
                    granted_scopes_json = '[]', configuration_json = ?,
                    credential_storage = ?, credential_reference = NULL,
                    last_error_json = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    definition.transport,
                    json_text(defaults),
                    definition.credential_storage,
                    utc_now(),
                    connector_id,
                ),
            )
        return self._public_connector(self._connector_row(connector_id))

    def _configured_http_connector(
        self, row: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not bool(row["enabled"]):
            raise PermissionError("The plugin connector is disconnected")
        configuration = parse_json(str(row["configuration_json"]), {})
        if not configuration.get("streamable_http_endpoint"):
            raise ValueError("The MCP Streamable HTTP endpoint is not configured")
        return configuration

    def _http_connector_client(
        self,
        row: Mapping[str, Any],
        configuration: Mapping[str, Any],
        *,
        allow_tool_calls: bool = False,
    ) -> McpHttpClient:
        headers: dict[str, str] = {}
        reference = row.get("credential_reference")
        if reference:
            token = self.secret_store.get(str(reference))
            if not token:
                raise PermissionError("The protected MCP credential is missing")
            headers["Authorization"] = f"Bearer {token}"
        return McpHttpClient(
            str(configuration["streamable_http_endpoint"]),
            headers=headers,
            timeout_seconds=float(configuration.get("timeout_seconds", 10.0)),
            allow_tool_calls=allow_tool_calls,
        )

    def _record_connector_error(
        self, connector_id: str, code: str, message: str
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE plugin_connectors
                SET status = 'error', last_error_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json_text({"code": code, "message": str(message)[:1000]}),
                    utc_now(),
                    connector_id,
                ),
            )

    @staticmethod
    def _connector_definition(connector_id: str) -> ConnectorDefinition:
        for definition in _CONNECTOR_DEFINITIONS:
            if definition.id == connector_id:
                return definition
        raise KeyError(f"Plugin connector does not exist: {connector_id}")


    def configure_mcp(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.configure_connector("mcp", request)

    def test_mcp(self) -> dict[str, Any]:
        return self.test_connector("mcp")

    def call_mcp(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.call_connector("mcp", request)

    def disconnect_mcp(self) -> dict[str, Any]:
        return self.disconnect_connector("mcp")

    def _connector_row(self, connector_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT * FROM plugin_connectors WHERE id = ?", (connector_id,)
        )
        if row is None:
            raise KeyError(f"Plugin connector does not exist: {connector_id}")
        return row

    @staticmethod
    def _public_connector(row: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["permission_scopes"] = parse_json(
            payload.pop("permission_scopes_json"), []
        )
        payload["granted_scopes"] = parse_json(
            payload.pop("granted_scopes_json"), []
        )
        payload["configuration"] = parse_json(
            payload.pop("configuration_json"), {}
        )
        payload["granted_tool_names"] = list(
            payload["configuration"].get("allowed_tools") or []
        )
        payload["verified_tool_names"] = list(
            payload["configuration"].get("verified_allowed_tools") or []
        )
        payload["last_error"] = parse_json(payload.pop("last_error_json"), None)
        payload["enabled"] = bool(payload["enabled"])
        payload["credentials_present"] = bool(
            payload.pop("credential_reference", None)
        )
        return payload

    def connected_apps(self) -> list[dict[str, Any]]:
        """The external services Salty Steak can be connected to.

        These are the plugins in the product's sense: another company's
        account, reached over a network, with an authorisation the user grants
        and can take back. Web search, the terminal and the screen are not
        that — they are this application's own capabilities, and calling them
        plugins told the user they had installed something they had not.
        """

        rows = []
        for connector in self.connectors():
            connected = bool(
                connector.get("enabled") and connector.get("credentials_present")
            )
            rows.append(
                {
                    "id": connector.get("connector") or connector.get("provider"),
                    "provider": connector.get("provider"),
                    "name": connector.get("display_name"),
                    "description": connector.get("description") or "",
                    "account": connector.get("account") or "",
                    "state": "connected" if connected else "not_connected",
                    "scopes": connector.get("permission_scopes") or [],
                    "last_error": connector.get("last_error"),
                }
            )
        return rows

    def describe(
        self,
        *,
        vision_status: Mapping[str, Any] | None = None,
        automation_status: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        plugins = [asdict(item) for item in _PLUGIN_CAPABILITIES]
        if automation_status is not None:
            _apply_automation_state(plugins, automation_status)
        if vision_status is not None:
            for plugin in plugins:
                if plugin["id"] != "images":
                    continue
                available = bool(vision_status.get("application_available"))
                plugin["availability"] = "available" if available else "model_required"
                plugin["unavailable_reason"] = (
                    None if available else str(vision_status.get("reason") or "Vision is unavailable.")
                )
                plugin["runtime_status"] = dict(vision_status)
        return {
            "architecture": "salty_steak_native_plugin_registry_v1",


            "terminology": "tools_and_connected_apps",
            "tools": plugins,
            "connected_apps": self.connected_apps(),
            "execution_policy": "deny_unregistered_and_require_declared_permission",
            "network_default": "off_except_built_in_read_only_web_search",
            "automatic_invocation_default": "host_intent_gated_read_only",
            "plugins": plugins,

            "capabilities": plugins,
            "connectors": self.connectors(),
        }







_PLUGIN_CAPABILITY_IDS = {
    "terminal": "terminal.execute",
    "screen_capture": "screen.capture",
    "app_control": "ui.automation",
}


def _apply_automation_state(
    plugins: list[dict[str, Any]],
    automation_status: Mapping[str, Any],
) -> None:
    """Report each computer-control plugin from the broker's own status."""

    by_capability = {
        str(entry.get("capability")): entry
        for entry in automation_status.get("capabilities") or []
        if isinstance(entry, Mapping)
    }
    for plugin in plugins:
        capability = _PLUGIN_CAPABILITY_IDS.get(str(plugin["id"]))
        entry = by_capability.get(capability or "")
        if entry is None:
            continue
        runnable = bool(entry.get("runtime_available"))
        granted = bool(entry.get("effective_enabled"))
        plugin["availability"] = "available" if runnable else "unavailable"
        plugin["unavailable_reason"] = (
            None
            if runnable
            else str(
                entry.get("runtime_unavailable_reason")
                or "This capability is not part of this build."
            )
        )
        plugin["capability"] = capability
        plugin["granted"] = granted


def _tool_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("MCP allowed_tools must be an array of tool names")
    if len(value) > _MAX_ALLOWED_MCP_TOOLS:
        raise ValueError(
            f"MCP allowed_tools cannot exceed {_MAX_ALLOWED_MCP_TOOLS} entries"
        )
    names: list[str] = []
    for item in value:
        name = str(item).strip()
        if (
            not name
            or len(name) > 200
            or any(character.isspace() for character in name)
        ):
            raise ValueError("MCP tool names must be non-empty and contain no whitespace")
        if name not in names:
            names.append(name)
    return names



ToolRegistry = PluginRegistry
