"""Request parsing and route dispatch for the loopback JSON API."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import warnings
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping

from ..database.control import ACTIVE_OPERATION_STATES
from .errors import HTTPProblem


JSON_BODY_LIMIT = 2 * 1024 * 1024
UPLOAD_BODY_LIMIT = 32 * 1024 * 1024 * 1024
VISION_UPLOAD_BODY_LIMIT = 25 * 1024 * 1024 + 64 * 1024
CHAT_ATTACHMENT_UPLOAD_BODY_LIMIT = 512 * 1024 * 1024 + 64 * 1024
COPY_CHUNK_SIZE = 1024 * 1024
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _clean_filename(value: str | None) -> str:
    name = Path((value or "dataset").replace("\\", "/")).name.strip()
    name = SAFE_FILENAME.sub("_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = "dataset"
    if Path(name).stem.upper() in WINDOWS_DEVICE_NAMES:
        name = f"_{name}"
    return name[:240]


class ApiRouter:
    """Translate one local HTTP request into an application facade call."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    @property
    def server(self) -> Any:
        return self.handler.server

    @property
    def application(self) -> Any:
        return self.server.application

    @property
    def method(self) -> str:
        return str(self.handler.command)

    def dispatch(
        self, raw_path: str, query: Mapping[str, list[str]]
    ) -> Any:
        app = self.application
        parts = tuple(part for part in raw_path.split("/") if part)[1:]
        method = self.method

        if method == "GET" and parts == ("health",):
            return {
                "status": "ok",
                "product_name": "Salty Steak",
                "app_version": "2.0.0",
                "api_version": 1,
                "build_id": os.environ.get("SALTY_POTATO_BUILD_ID", "development"),
            }

        if method == "GET" and parts == ("scientific-recovery",):
            return app.scientific_recovery_state()

        if method == "GET" and parts == ("tools",):
            return app.tool_capabilities()
        if method == "GET" and parts == ("plugins",):
            return app.plugin_capabilities()
        if method == "GET" and parts == ("plugins", "connectors"):
            return app.plugin_connectors()
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("plugins", "connectors")
            and parts[3] == "configure"
        ):
            return app.configure_plugin_connector(parts[2], self._read_json())
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("plugins", "connectors")
            and parts[3] == "test"
        ):
            return app.test_plugin_connector(parts[2])
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("plugins", "connectors")
            and parts[3] == "call"
        ):
            return app.call_plugin_connector(parts[2], self._read_json())
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("plugins", "connectors")
            and parts[3] == "disconnect"
        ):
            return app.disconnect_plugin_connector(parts[2])

        if method == "GET" and parts == ("automation", "status"):
            return app.automation_status()
        if method == "POST" and parts == ("automation", "grant"):
            return app.grant_automation(self._read_json())
        if method == "POST" and parts == ("automation", "revoke"):
            return app.revoke_automation(self._read_json())
        if method == "POST" and parts == ("automation", "invoke"):
            return app.invoke_automation(self._read_json())
        if method == "GET" and parts == ("automation", "audit"):
            raw_limit = self._query_first(query, "limit")
            try:
                limit = 100 if raw_limit is None else int(raw_limit)
            except ValueError as exc:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "The query parameter 'limit' must be an integer.",
                ) from exc
            if not 1 <= limit <= 500:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_query",
                    "The query parameter 'limit' must be between 1 and 500.",
                )
            return app.automation_audit(limit=limit)

        if method == "POST" and parts == ("runtime", "plan"):
            return app.runtime_plan(self._read_json())
        if method == "GET" and parts == ("models", "roles"):
            return app.model_roles()
        if method == "GET" and len(parts) == 2 and parts[0] == "models":
            return app.get_model_bundle(parts[1])
        if (
            method == "GET"
            and len(parts) == 3
            and parts[:2] == ("chat", "image-artifacts")
        ):
            return app.image_artifact_content(parts[2])

        if method == "GET" and parts == ("datasets",):
            return app.list_datasets()
        if method == "GET" and len(parts) == 2 and parts[0] == "datasets":
            dataset_id = parts[1]
            for dataset in app.list_datasets():
                if str(dataset.get("id")) == dataset_id:
                    return dataset
            raise KeyError(f"Dataset does not exist: {dataset_id}")
        if method == "POST" and parts == ("datasets", "inspect"):
            return self._inspect_dataset()
        if method == "POST" and parts == ("datasets", "preview"):
            body = self._read_json()
            return app.preview_dataset(
                self._required_string(body, "path"),
                dict(body.get("mapping") or {}),
            )
        if method == "POST" and parts == ("datasets",):
            return self._add_dataset()
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "datasets"
            and parts[2] == "validate"
        ):
            body = self._read_json()
            return app.validate_dataset(parts[1], self._request_key(body))
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "datasets"
            and parts[2] == "preflight"
        ):
            body = self._read_json()
            return app.preflight_dataset_preparation(parts[1], body)
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "datasets"
            and parts[2] == "prepare"
        ):
            body = self._read_json()
            return app.prepare_dataset(parts[1], body, self._request_key(body))

        if method == "GET" and parts == ("training", "setup"):
            return app.training_setup()
        if method == "GET" and parts == ("training", "status"):
            return app.training_status()
        if method == "GET" and parts == ("training", "history"):
            limit_text = self._query_first(query, "limit")
            return app.training_history(
                limit=int(limit_text) if limit_text else 100
            )
        if method == "POST" and parts == ("training",):
            body = self._read_json()
            return app.start_training(body, self._request_key(body))
        if method == "POST" and parts == ("training", "identity"):
            body = self._read_json()
            return app.start_identity_post_training(body, self._request_key(body))
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "training"
            and parts[2] == "retry-finalisation"
        ):
            body = self._read_json()
            return app.retry_training_finalisation(
                parts[1], self._request_key(body)
            )
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "training"
            and parts[2] == "resume-post-training"
        ):
            body = self._read_json()
            return app.resume_post_training(parts[1], self._request_key(body))

        if method == "GET" and parts == ("evaluations",):
            return app.list_evaluations()
        if method == "POST" and parts == ("evaluations",):
            body = self._read_json()
            version_id = self._required_string(body, "saved_version_id")
            return app.start_evaluation(version_id, self._request_key(body))

        if method == "GET" and parts == ("system", "recovery-state"):
            return app.production_recovery_state()

        if method == "GET" and parts == ("versions",):
            return app.list_versions()
        if method == "GET" and len(parts) == 2 and parts[0] == "versions":
            return app.get_version(parts[1])
        if (
            method == "GET"
            and len(parts) == 3
            and parts[0] == "versions"
            and parts[2] == "technical-details"
        ):
            return app.version_technical_details(parts[1])
        if (
            method == "GET"
            and len(parts) == 3
            and parts[0] == "versions"
            and parts[2] == "deletion-preview"
        ):
            return app.deletion_preview(parts[1])
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "versions"
            and parts[2] == "activate"
        ):
            body = self._read_json()
            return app.activate_version(parts[1], self._request_key(body))
        if method == "DELETE" and len(parts) == 2 and parts[0] == "versions":
            body = self._read_json(optional=True)
            return app.delete_version(parts[1], self._request_key(body))

        if method == "GET" and parts == ("operations",):
            active = self._query_bool(query, "active", default=False)
            operation_type = self._query_first(query, "type")
            limit_text = self._query_first(query, "limit")
            limit = int(limit_text) if limit_text else 100
            states = tuple(ACTIVE_OPERATION_STATES) if active else None
            return app.operations.list(
                states=states, operation_type=operation_type, limit=limit
            )
        if method == "GET" and parts == ("operations", "reconcile"):
            return app.operation_for_request(self._query_first(query, "request_key"))
        if (
            method == "GET"
            and len(parts) == 3
            and parts[0] == "operations"
            and parts[2] == "events"
        ):
            after_text = self._query_first(query, "after_sequence")
            limit_text = self._query_first(query, "limit")
            return app.operation_events(
                parts[1],
                after_sequence=int(after_text) if after_text else 0,
                limit=int(limit_text) if limit_text else 500,
            )
        if method == "GET" and len(parts) == 2 and parts[0] == "operations":
            operation = app.operations.get(parts[1])
            if operation is None:
                raise KeyError(f"Operation does not exist: {parts[1]}")
            return operation
        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "operations"
            and parts[2] == "stop"
        ):
            self._read_json()
            return app.operations.request_stop(parts[1])

        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "conversations"
            and parts[2] == "rename"
        ):
            body = self._read_json()
            return app.rename_conversation(
                parts[1], self._required_string(body, "title", strip=False)
            )
        if method == "DELETE" and len(parts) == 2 and parts[0] == "conversations":
            return app.delete_conversation(parts[1])

        if method == "GET" and parts == ("chat", "status"):
            return app.chat_status()
        if method == "POST" and parts == ("chat", "attachments", "inspect"):
            return self._inspect_chat_attachment()
        if method == "POST" and parts == ("chat", "vision-input"):
            return self._stage_vision_input()
        if method == "POST" and parts == ("chat", "vision-input", "screen-capture"):
            return app.stage_screen_capture_for_vision(self._read_json())
        if method == "GET" and parts == ("chat", "memory"):
            limit_text = self._query_first(query, "limit")
            return app.list_chat_memories(int(limit_text) if limit_text else 200)
        if method == "POST" and parts == ("chat", "memory"):
            body = self._read_json()
            return app.save_chat_memory(self._required_string(body, "note", strip=False))
        if method == "DELETE" and parts == ("chat", "memory"):
            return app.clear_chat_memories()
        if (
            method == "DELETE"
            and len(parts) == 3
            and parts[:2] == ("chat", "memory")
        ):
            return app.forget_chat_memory(parts[2])
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "vision-analyses")
            and parts[3] == "retry-input"
        ):
            self._read_json()
            return app.retry_vision_analysis(parts[2])
        if method == "GET" and parts == ("chat", "conversations"):
            return app.list_conversations()
        if method == "POST" and parts == ("chat", "conversations"):
            self._read_json()
            return app.create_conversation()
        if (
            method == "GET"
            and len(parts) == 3
            and parts[:2] == ("chat", "conversations")
        ):
            return app.get_conversation(parts[2])
        if (
            method == "DELETE"
            and len(parts) == 3
            and parts[:2] == ("chat", "conversations")
        ):
            return app.delete_conversation(parts[2])
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "rename"
        ):
            body = self._read_json()
            return app.rename_conversation(
                parts[2], self._required_string(body, "title", strip=False)
            )
        if method == "POST" and parts == ("chat", "open-external"):
            return app.open_external(self._read_json())


        if method == "GET" and parts == ("chat", "labels"):
            return app.list_conversation_labels()
        if method == "POST" and parts == ("chat", "labels"):
            return app.create_conversation_label(self._read_json())
        if method == "POST" and len(parts) == 3 and parts[:2] == ("chat", "labels"):
            return app.update_conversation_label(parts[2], self._read_json())
        if method == "DELETE" and len(parts) == 3 and parts[:2] == ("chat", "labels"):
            return app.delete_conversation_label(parts[2])
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "pin"
        ):
            return app.set_conversation_pinned(parts[2], self._read_json())
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "labels"
        ):
            return app.set_conversation_label(parts[2], parts[4], self._read_json())

        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "messages"
        ):
            body = self._read_json()
            return app.send_message(
                parts[2],
                self._required_string(body, "content", strip=False),
                self._request_key(body),
                body.get("generation_settings"),
                body.get("attachments"),
            )
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "memory"
        ):
            body = self._read_json()
            return app.save_conversation_memory(parts[2], body.get("note"))
        if (
            method == "POST"
            and len(parts) == 6
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "host-actions"
            and parts[5] == "confirm"
        ):
            body = self._read_json()
            if body.get("user_confirmed") is not True:
                raise PermissionError(
                    "Host actions require an explicit confirmation"
                )
            return app.confirm_host_action(
                parts[2],
                parts[4],
                self._required_string(body, "assistant_message_id"),
                self._request_key(body),
                body.get("generation_settings"),
                body.get("confirmation_text"),
            )
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "agent-tasks"
        ):
            body = self._read_json()
            return app.start_agent_task(
                parts[2],
                self._required_string(body, "instruction", strip=False),
                self._request_key(body),
                body.get("generation_settings"),
            )
        if (
            method == "POST"
            and len(parts) == 4
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "vision-analyses"
        ):
            body = self._read_json()
            return app.analyze_vision_input(
                parts[2],
                self._required_string(body, "prompt", strip=False),
                self._required_string(body, "vision_input_token"),
                self._request_key(body),
                int(body.get("maximum_output_tokens", 128)),
                body.get("generation_settings"),
                body.get("attachments"),
                bool(body.get("continue_with_chat", False)),
            )
        if (
            method == "POST"
            and len(parts) == 5
            and parts[:2] == ("chat", "conversations")
            and parts[3] == "messages"
            and parts[4] == "retry"
        ):
            body = self._read_json()
            return app.retry_message(
                parts[2],
                self._required_string(body, "user_message_id"),
                self._request_key(body),
                body.get("generation_settings"),
            )

        if method == "GET" and parts == ("project",):
            return app.project_state()
        if method == "GET" and parts == ("about",):
            return app.about_state()
        if method == "GET" and parts == ("about", "storage"):
            return app.about_storage(
                refresh=self._query_bool(query, "refresh", default=False)
            )
        if method == "POST" and parts == ("project", "verify"):
            body = self._read_json()
            return app.verify_project(self._request_key(body))
        if method == "POST" and parts == ("project", "cache", "clear"):
            body = self._read_json()
            return app.clear_cache(self._request_key(body))
        if method == "POST" and parts == ("project", "open-path"):
            body = self._read_json()
            path = self._required_string(body, "path")
            app.open_path(path)
            return {"opened": True, "path": path}

        if parts and parts[0] in {
            "datasets",
            "training",
            "evaluations",
            "versions",
            "operations",
            "conversations",
            "chat",
            "runtime",
            "project",
            "about",
        }:
            raise HTTPProblem(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                f"{method} is not available for this API route.",
            )
        raise HTTPProblem(
            HTTPStatus.NOT_FOUND, "route_not_found", "The API route does not exist."
        )

    def _inspect_dataset(self) -> dict[str, Any]:
        content_type = self.handler.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            return self._inspect_uploaded_dataset()
        body = self._read_json()
        return self.application.inspect_dataset(
            self._required_string(body, "path")
        )

    def _stage_vision_input(self) -> dict[str, Any]:
        length = self._content_length(limit=VISION_UPLOAD_BODY_LIMIT)
        content_type = self.handler.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise HTTPProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "multipart_required",
                "Vision input requires one multipart image upload.",
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                import cgi
            except ImportError as exc:
                raise HTTPProblem(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "multipart_unavailable",
                    "This Python runtime cannot process local image uploads.",
                ) from exc
            form = cgi.FieldStorage(
                fp=self.handler.rfile,
                headers=self.handler.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
                keep_blank_values=True,
            )
        if "file" not in form:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose one image to analyze.",
            )
        field = form["file"]
        if isinstance(field, list):
            if len(field) != 1:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "too_many_files",
                    "Analyze one image at a time.",
                )
            field = field[0]
        if not getattr(field, "file", None) or not getattr(field, "filename", None):
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose one image to analyze.",
            )
        confirmed = str(form.getfirst("user_confirmed", "")).casefold() == "true"
        if not confirmed:
            raise PermissionError(
                "Image upload requires an explicit user_confirmed=true field"
            )
        filename = _clean_filename(getattr(field, "filename", None))
        media_type = str(getattr(field, "type", "") or "")
        token = uuid.uuid4().hex
        destination_dir = self.server.upload_root / f"vision-{token}"
        destination_dir.mkdir(parents=True, exist_ok=False)
        destination = destination_dir / filename
        try:
            size = 0
            with destination.open("xb") as output:
                while True:
                    chunk = field.file.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 25 * 1024 * 1024:
                        raise HTTPProblem(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            "vision_image_too_large",
                            "Vision images are limited to 25 MiB.",
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "empty_upload",
                    "The selected image is empty.",
                )
            return self.application.stage_vision_input(
                destination,
                filename=filename,
                media_type=media_type,
                source="user_attachment",
                consent_evidence={
                    "user_confirmed": True,
                    "upload_request_id": token,
                    "selection_scope": "one_local_image",
                },
            )
        finally:
            shutil.rmtree(destination_dir, ignore_errors=True)

    def _inspect_chat_attachment(self) -> dict[str, Any]:
        """Inspect one explicitly selected file without retaining its raw bytes."""

        length = self._content_length(limit=CHAT_ATTACHMENT_UPLOAD_BODY_LIMIT)
        content_type = self.handler.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise HTTPProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "multipart_required",
                "Chat attachments require a multipart file upload.",
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                import cgi
            except ImportError as exc:
                raise HTTPProblem(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "multipart_unavailable",
                    "This Python runtime cannot process local file uploads.",
                ) from exc
            form = cgi.FieldStorage(
                fp=self.handler.rfile,
                headers=self.handler.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
                keep_blank_values=True,
            )
        if "file" not in form:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose a file to attach.",
            )
        field = form["file"]
        if isinstance(field, list):
            if len(field) != 1:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "one_file_per_inspection",
                    "Each selected file is inspected independently.",
                )
            field = field[0]
        if not getattr(field, "file", None) or not getattr(field, "filename", None):
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose a file to attach.",
            )
        filename = _clean_filename(getattr(field, "filename", None))
        media_type = str(getattr(field, "type", "") or "")
        token = uuid.uuid4().hex
        destination_dir = self.server.upload_root / f"chat-attachment-{token}"
        destination_dir.mkdir(parents=True, exist_ok=False)
        destination = destination_dir / filename
        try:
            size = 0
            with destination.open("xb") as output:
                while True:
                    chunk = field.file.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise HTTPProblem(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            "chat_attachment_too_large",
                            "Chat attachments are limited to 512 MiB per file.",
                        )
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "empty_upload",
                    "The selected file is empty.",
                )
            return self.application.inspect_chat_attachment(
                destination,
                filename=filename,
                media_type=media_type,
            )
        finally:
            shutil.rmtree(destination_dir, ignore_errors=True)

    def _inspect_uploaded_dataset(self) -> dict[str, Any]:
        length = self._content_length(limit=UPLOAD_BODY_LIMIT)
        content_type = self.handler.headers.get("Content-Type", "")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                import cgi
            except ImportError as exc:
                raise HTTPProblem(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "multipart_unavailable",
                    "This Python runtime cannot process local file uploads.",
                ) from exc
            form = cgi.FieldStorage(
                fp=self.handler.rfile,
                headers=self.handler.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
                keep_blank_values=True,
            )
        if "file" not in form:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose a local dataset file to inspect.",
            )
        field = form["file"]
        if isinstance(field, list):
            if len(field) != 1:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "too_many_files",
                    "Inspect one dataset file at a time.",
                )
            field = field[0]
        if not getattr(field, "file", None) or not getattr(field, "filename", None):
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_file",
                "Choose a local dataset file to inspect.",
            )
        filename = _clean_filename(getattr(field, "filename", None))
        token = uuid.uuid4().hex
        destination_dir = self.server.upload_root / token
        destination_dir.mkdir(parents=True, exist_ok=False)
        destination = destination_dir / filename
        try:
            with destination.open("xb") as output:
                while True:
                    chunk = field.file.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not destination.stat().st_size:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "empty_upload",
                    "The selected dataset file is empty.",
                )
            inspection = dict(self.application.inspect_dataset(destination))
            pending = self.server.remember_upload(destination, filename)
            inspection["path"] = str(destination)
            inspection["upload_token"] = pending.token
            inspection["source_filename"] = filename
            return inspection
        except Exception:
            shutil.rmtree(destination_dir, ignore_errors=True)
            raise

    def _add_dataset(self) -> dict[str, Any]:
        body = self._read_json()
        token_value = body.get("upload_token")
        if not token_value:
            return self.application.add_dataset(body)
        token = str(token_value).strip()
        if not token:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_upload_token",
                "The upload token is empty.",
            )
        destination, pending = self.server.promote_upload(
            token, str(body.get("path")) if body.get("path") else None
        )
        promoted = dict(body)
        promoted["path"] = str(destination)
        promoted.pop("upload_token", None)
        try:
            result = self.application.add_dataset(promoted)
        except Exception:
            self.server.restore_upload(destination, pending)
            raise
        self.server.finish_upload(pending)
        return result

    def _read_json(self, *, optional: bool = False) -> dict[str, Any]:
        length_header = self.handler.headers.get("Content-Length")
        if length_header is None:
            if optional:
                return {}
            raise HTTPProblem(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "Content-Length is required.",
            )
        length = self._content_length(limit=JSON_BODY_LIMIT)
        if length == 0 and optional:
            return {}
        content_type = self.handler.headers.get("Content-Type", "")
        if not content_type.lower().startswith("application/json"):
            raise HTTPProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "json_required",
                "This endpoint requires a JSON request body.",
            )
        try:
            payload = json.loads(self.handler.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "The request body is not valid UTF-8 JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "object_required",
                "The JSON request body must be an object.",
            )
        return payload

    def _content_length(self, *, limit: int) -> int:
        value = self.handler.headers.get("Content-Length")
        if value is None:
            raise HTTPProblem(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "Content-Length is required.",
            )
        try:
            length = int(value)
        except ValueError as exc:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length must be an integer.",
            ) from exc
        if length < 0:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length cannot be negative.",
            )
        if length > limit:
            raise HTTPProblem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "The request body is larger than the local service accepts.",
            )
        return length

    def _request_key(self, body: Mapping[str, Any]) -> str | None:
        value = self.handler.headers.get("Idempotency-Key") or body.get("request_key")
        if value is None:
            return None
        key = str(value).strip()
        if not key:
            return None
        if len(key) > 200 or any(ord(character) < 32 for character in key):
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_request_key",
                "The request key is invalid.",
            )
        return key

    @staticmethod
    def _required_string(
        body: Mapping[str, Any], key: str, *, strip: bool = True
    ) -> str:
        if key not in body or body[key] is None:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_field",
                f"The field {key!r} is required.",
            )
        value = str(body[key])
        checked = value.strip() if strip else value
        if not checked:
            raise HTTPProblem(
                HTTPStatus.BAD_REQUEST,
                "missing_field",
                f"The field {key!r} cannot be empty.",
            )
        return checked

    @staticmethod
    def _query_first(
        query: Mapping[str, list[str]], key: str
    ) -> str | None:
        values = query.get(key)
        return values[0] if values else None

    def _query_bool(
        self, query: Mapping[str, list[str]], key: str, *, default: bool
    ) -> bool:
        value = self._query_first(query, key)
        if value is None:
            return default
        normalized = value.casefold()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
        raise HTTPProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_query",
            f"The query parameter {key!r} must be true or false.",
        )
