from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.backend.server import start_server


class _Config:
    def section(self, name: str) -> dict[str, Any]:
        assert name == "server"
        return {"host": "127.0.0.1", "port": 0}


class _Operations:
    def __init__(self) -> None:
        self.records = [
            {
                "id": "operation-1",
                "type": "training",
                "state": "running",
                "target_id": "dataset-1",
            }
        ]

    def list(self, *, states=None, operation_type=None, limit=100):
        records = self.records
        if states:
            records = [record for record in records if record["state"] in states]
        if operation_type:
            records = [
                record for record in records if record["type"] == operation_type
            ]
        return records[:limit]

    def get(self, operation_id: str):
        return next(
            (record for record in self.records if record["id"] == operation_id),
            None,
        )

    def request_stop(self, operation_id: str):
        record = self.get(operation_id)
        if record is None:
            raise KeyError(f"Operation does not exist: {operation_id}")
        record["state"] = "stop_requested"
        return record


class _Application:
    def __init__(self, root: Path) -> None:
        self.config = _Config()
        self.paths = SimpleNamespace(
            cache=root / "cache",
            datasets=root / "datasets",
        )
        self.paths.cache.mkdir()
        self.paths.datasets.mkdir()
        self.operations = _Operations()
        self.added_payload: dict[str, Any] | None = None
        self.close_count = 0
        self.mcp_configure_request: dict[str, Any] | None = None
        self.automation_requests: list[tuple[str, dict[str, Any]]] = []
        self.image_confirmations: list[dict[str, Any]] = []
        self.conversations = {
            "conversation-1": {
                "id": "conversation-1",
                "title": "New chat",
                "messages": [],
            }
        }

    def close(self) -> None:
        self.close_count += 1

    def list_datasets(self):
        return []

    def scientific_recovery_state(self):
        return {"state": "accepted", "read_only": True}

    def inspect_dataset(self, path):
        source = Path(path)
        return {
            "path": str(source),
            "source_filename": source.name,
            "format": source.suffix.lstrip("."),
            "encoding": "utf-8",
            "size_bytes": source.stat().st_size,
            "record_estimate": 1,
            "detected_fields": ["__text__"],
            "samples": [{"__text__": source.read_text(encoding="utf-8")}],
            "inspection_errors": [],
        }

    def add_dataset(self, payload):
        source = Path(payload["path"])
        assert source.is_file()
        self.added_payload = dict(payload)
        return {"id": "dataset-1", "source_path": str(source)}

    def operation_for_request(self, request_key):
        if request_key == "known":
            return self.operations.records[0]
        return None

    def operation_events(self, operation_id, *, after_sequence=0, limit=500):
        if self.operations.get(operation_id) is None:
            raise KeyError(f"Operation does not exist: {operation_id}")
        events = [
            {
                "id": "event-2",
                "operation_id": operation_id,
                "sequence": 2,
                "event_type": "training_telemetry",
                "state": "running",
                "phase": "Training",
                "evidence": {
                    "current_progress": 1.0,
                    "total_progress": 10.0,
                    "details": {"training_loss": 2.5},
                },
                "created_at": "2026-07-30T00:00:00.000Z",
            }
        ]
        selected = [
            event for event in events if event["sequence"] > after_sequence
        ][:limit]
        return {
            "operation_id": operation_id,
            "events": selected,
            "after_sequence": after_sequence,
            "next_after_sequence": (
                selected[-1]["sequence"] if selected else after_sequence
            ),
            "has_more": False,
        }

    def rename_conversation(self, conversation_id, title):
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise KeyError(f"Conversation does not exist: {conversation_id}")
        conversation["title"] = str(title).strip()
        return dict(conversation)

    def delete_conversation(self, conversation_id):
        conversation = self.conversations.pop(conversation_id, None)
        if conversation is None:
            raise KeyError(f"Conversation does not exist: {conversation_id}")
        return {
            "deleted": True,
            "conversation_id": conversation_id,
            "title": conversation["title"],
        }

    def plugin_capabilities(self):
        return {
            "architecture": "salty_steak_native_plugin_registry_v1",
            "terminology": "plugins",
            "plugins": [{"id": "web_search", "availability": "available"}],
            "capabilities": [
                {"id": "web_search", "availability": "available"}
            ],
            "connectors": self.plugin_connectors(),
        }

    def tool_capabilities(self):
        return self.plugin_capabilities()

    def plugin_connectors(self):
        return [
            {
                "id": "gmail",
                "status": "disconnected",
                "enabled": False,
                "granted_scopes": [],
            }
        ]

    def configure_plugin_connector(self, connector_id, request):
        self.mcp_configure_request = dict(request)
        return {
            "id": connector_id,
            "status": "configured",
            "enabled": True,
            "credentials_present": bool(request.get("bearer_token")),
            "configuration": {
                "streamable_http_endpoint": request["endpoint"],
                "allowed_tools": list(request.get("allowed_tools") or []),
            },
        }

    def test_plugin_connector(self, connector_id):
        return {
            "connector": {"id": connector_id, "status": "connected"},
            "server_info": {"name": "fixture"},
            "tools": [{"name": "search"}],
            "discovered_tool_names": ["search"],
            "missing_allowed_tools": [],
        }

    def call_plugin_connector(self, connector_id, request):
        return {
            "connector_id": connector_id,
            "tool_name": request["name"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }

    def disconnect_plugin_connector(self, connector_id):
        return {
            "id": connector_id,
            "status": "disconnected",
            "enabled": False,
            "credentials_present": False,
        }

    def automation_status(self):
        return {
            "schema": "salty-steak-windows-automation-v1",
            "default_enabled": False,
            "capabilities": [],
        }

    def grant_automation(self, request):
        self.automation_requests.append(("grant", dict(request)))
        return {"granted": list(request["capabilities"])}

    def revoke_automation(self, request):
        self.automation_requests.append(("revoke", dict(request)))
        return {"revoked": list(request.get("capabilities") or [])}

    def invoke_automation(self, request):
        self.automation_requests.append(("invoke", dict(request)))
        return {
            "capability": request["capability"],
            "status": "succeeded",
            "audit_record_id": "audit-fixture",
        }

    def automation_audit(self, *, limit=100):
        return [{"id": "audit-fixture", "limit": limit}]

    def confirm_host_action(
        self,
        conversation_id,
        proposal_id,
        assistant_message_id,
        request_key=None,
        generation_settings=None,
        confirmation_text=None,
    ):
        record = {
            "conversation_id": conversation_id,
            "proposal_id": proposal_id,
            "assistant_message_id": assistant_message_id,
            "request_key": request_key,
            "generation_settings": dict(generation_settings or {}),
            "confirmation_text": confirmation_text,
        }
        self.image_confirmations.append(record)
        return {"id": "image-operation-1", "type": "chat_image_generation", **record}


def _request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(
        f"{url}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def _json_request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    status, raw, _headers = _request(
        url, path, method=method, body=body, headers=headers
    )
    return status, json.loads(raw)


def test_server_serves_spa_json_routes_and_safe_errors(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<main>Salty Steak</main>", encoding="utf-8")
    application = _Application(tmp_path)
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=tmp_path / "uploads",
        owns_application=False,
    )
    try:
        status, payload = _json_request(handle.url, "/api/health")
        assert status == 200
        assert payload["data"]["status"] == "ok"

        status, payload = _json_request(handle.url, "/api/scientific-recovery")
        assert status == 200
        assert payload["data"] == {"state": "accepted", "read_only": True}

        status, payload = _json_request(
            handle.url, "/api/operations?active=true&type=training"
        )
        assert status == 200
        assert payload["data"][0]["id"] == "operation-1"

        status, body, headers = _request(handle.url, "/chat/conversation")
        assert status == 200
        assert body == b"<main>Salty Steak</main>"
        assert headers["X-Frame-Options"] == "DENY"

        status, payload = _json_request(handle.url, "/api/not-a-route")
        assert status == 404
        assert payload["error"]["code"] == "route_not_found"

        status, payload = _json_request(handle.url, "/api/operations/missing")
        assert status == 404
        assert payload["error"]["code"] == "not_found"

        status, payload = _json_request(handle.url, "/api/operations?active=perhaps")
        assert status == 400
        assert payload["error"]["code"] == "invalid_query"

        status, payload = _json_request(handle.url, "/api/operations/reconcile?request_key=known")
        assert status == 200
        assert payload["data"]["id"] == "operation-1"

        status, payload = _json_request(
            handle.url,
            "/api/operations/operation-1/events?after_sequence=1&limit=10",
        )
        assert status == 200
        assert payload["data"]["next_after_sequence"] == 2
        assert payload["data"]["events"][0]["evidence"]["details"] == {
            "training_loss": 2.5
        }

        status, payload = _json_request(
            handle.url,
            "/api/chat/conversations/conversation-1/host-actions/proposal-1/confirm",
            method="POST",
            payload={
                "assistant_message_id": "assistant-1",
                "user_confirmed": True,
                "request_key": "image-key-1",
                "generation_settings": {"width": 512, "height": 512, "steps": 8, "seed": 42},
            },
        )
        assert status == 200
        assert payload["data"]["type"] == "chat_image_generation"
        assert application.image_confirmations == [
            {
                "conversation_id": "conversation-1",
                "proposal_id": "proposal-1",
                "assistant_message_id": "assistant-1",
                "request_key": "image-key-1",
                "generation_settings": {"width": 512, "height": 512, "steps": 8, "seed": 42},
                "confirmation_text": None,
            }
        ]

        status, payload = _json_request(
            handle.url,
            "/api/chat/conversations/conversation-1/host-actions/proposal-1/confirm",
            method="POST",
            payload={"assistant_message_id": "assistant-1", "user_confirmed": False},
        )
        assert status == 403
        assert payload["error"]["code"] == "permission_denied"
    finally:
        handle.stop()


def test_owned_server_closes_the_complete_application(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    application = _Application(tmp_path)
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=tmp_path / "uploads",
        owns_application=True,
    )
    handle.stop()
    assert application.close_count == 1


def test_multipart_inspection_token_promotes_exact_file(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    application = _Application(tmp_path)
    upload_root = tmp_path / "uploads"
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=upload_root,
        owns_application=False,
    )
    promoted_path: Path | None = None
    try:
        boundary = "salty-potato-test-boundary"
        content = b"first training record\n"
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="../tiny.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        status, raw, _headers = _request(
            handle.url,
            "/api/datasets/inspect",
            method="POST",
            body=multipart,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert status == 200
        inspection = json.loads(raw)["data"]
        assert inspection["source_filename"] == "tiny.txt"
        assert inspection["upload_token"]
        temporary = Path(inspection["path"])
        assert temporary.read_bytes() == content

        status, payload = _json_request(
            handle.url,
            "/api/datasets",
            method="POST",
            payload={
                "path": inspection["path"],
                "upload_token": inspection["upload_token"],
                "name": "Tiny",
                "language": "English",
                "purpose": "Test",
                "mapping": {"type": "plain_text", "plain_text": "__text__"},
            },
        )
        assert status == 200
        promoted_path = Path(payload["data"]["source_path"])
        assert promoted_path.read_bytes() == content
        assert application.paths.datasets.resolve() in promoted_path.resolve().parents
        assert not temporary.exists()

        status, payload = _json_request(
            handle.url,
            "/api/datasets",
            method="POST",
            payload={
                "path": inspection["path"],
                "upload_token": inspection["upload_token"],
            },
        )
        assert status == 400
        assert payload["error"]["code"] == "invalid_upload_token"
    finally:
        handle.stop()
    assert not upload_root.exists()
    assert promoted_path is not None and promoted_path.is_file()


def test_conversation_rename_and_delete_routes(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    application = _Application(tmp_path)
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=tmp_path / "uploads",
        owns_application=False,
    )
    try:
        status, payload = _json_request(
            handle.url,
            "/api/conversations/conversation-1/rename",
            method="POST",
            payload={"title": "  Research notes  "},
        )
        assert status == 200
        assert payload["data"]["title"] == "Research notes"

        status, payload = _json_request(
            handle.url,
            "/api/conversations/conversation-1",
            method="DELETE",
        )
        assert status == 200
        assert payload["data"] == {
            "deleted": True,
            "conversation_id": "conversation-1",
            "title": "Research notes",
        }

        status, payload = _json_request(
            handle.url,
            "/api/conversations/conversation-1",
            method="DELETE",
        )
        assert status == 404
        assert payload["error"]["code"] == "not_found"
    finally:
        handle.stop()


def test_plugins_routes_and_removed_calculator_api(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    application = _Application(tmp_path)
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=tmp_path / "uploads",
        owns_application=False,
    )
    try:
        status, plugins = _json_request(handle.url, "/api/plugins")
        assert status == 200
        assert plugins["data"]["terminology"] == "plugins"
        assert plugins["data"]["plugins"][0]["id"] == "web_search"

        status, compatibility = _json_request(handle.url, "/api/tools")
        assert status == 200
        assert compatibility["data"]["architecture"] == (
            "salty_steak_native_plugin_registry_v1"
        )

        status, connectors = _json_request(
            handle.url, "/api/plugins/connectors"
        )
        assert status == 200
        assert connectors["data"][0]["status"] == "disconnected"

        status, removed = _json_request(
            handle.url,
            "/api/tools/calculator",
            method="POST",
            payload={"expression": "1 + 1"},
        )
        assert status == 404
        assert removed["error"]["code"] == "route_not_found"

        status, configured = _json_request(
            handle.url,
            "/api/plugins/connectors/mcp/configure",
            method="POST",
            payload={
                "endpoint": "https://mcp.example.test/service",
                "allowed_tools": ["search"],
                "bearer_token": "secret-never-returned",
            },
        )
        assert status == 200
        assert configured["data"]["status"] == "configured"
        assert application.mcp_configure_request is not None
        assert application.mcp_configure_request["bearer_token"] == (
            "secret-never-returned"
        )
        assert "bearer_token" not in configured["data"]

        status, tested = _json_request(
            handle.url,
            "/api/plugins/connectors/mcp/test",
            method="POST",
            payload={},
        )
        assert status == 200
        assert tested["data"]["discovered_tool_names"] == ["search"]

        status, called = _json_request(
            handle.url,
            "/api/plugins/connectors/mcp/call",
            method="POST",
            payload={"name": "search", "arguments": {"q": "local AI"}},
        )
        assert status == 200
        assert called["data"]["tool_name"] == "search"

        status, disconnected = _json_request(
            handle.url,
            "/api/plugins/connectors/mcp/disconnect",
            method="POST",
            payload={},
        )
        assert status == 200
        assert disconnected["data"]["status"] == "disconnected"

        for connector_id in ("gmail", "google-calendar", "icloud-calendar"):
            status, bridged = _json_request(
                handle.url,
                f"/api/plugins/connectors/{connector_id}/configure",
                method="POST",
                payload={
                    "endpoint": "https://mcp.example.test/service",
                    "allowed_tools": [],
                },
            )
            assert status == 200
            assert bridged["data"]["id"] == connector_id
    finally:
        handle.stop()


def test_windows_automation_status_grant_revoke_invoke_and_audit_routes(
    tmp_path: Path,
) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    application = _Application(tmp_path)
    handle = start_server(
        application=application,
        project_root=tmp_path,
        host="127.0.0.1",
        port=0,
        static_root=static,
        upload_root=tmp_path / "uploads",
        owns_application=False,
    )
    try:
        status, response = _json_request(handle.url, "/api/automation/status")
        assert status == 200
        assert response["data"]["default_enabled"] is False

        status, response = _json_request(
            handle.url,
            "/api/automation/grant",
            method="POST",
            payload={
                "capabilities": ["terminal.execute"],
                "user_confirmed": True,
            },
        )
        assert status == 200
        assert response["data"]["granted"] == ["terminal.execute"]

        status, response = _json_request(
            handle.url,
            "/api/automation/invoke",
            method="POST",
            payload={
                "capability": "terminal.execute",
                "arguments": {"argv": ["whoami.exe"]},
            },
        )
        assert status == 200
        assert response["data"]["audit_record_id"] == "audit-fixture"

        status, response = _json_request(
            handle.url,
            "/api/automation/audit?limit=7",
        )
        assert status == 200
        assert response["data"] == [{"id": "audit-fixture", "limit": 7}]

        status, response = _json_request(
            handle.url,
            "/api/automation/revoke",
            method="POST",
            payload={"capabilities": ["terminal.execute"]},
        )
        assert status == 200
        assert response["data"]["revoked"] == ["terminal.execute"]
        assert [name for name, _request in application.automation_requests] == [
            "grant",
            "invoke",
            "revoke",
        ]

        status, response = _json_request(
            handle.url,
            "/api/automation/audit?limit=all",
        )
        assert status == 400
        assert response["error"]["code"] == "invalid_query"
    finally:
        handle.stop()
