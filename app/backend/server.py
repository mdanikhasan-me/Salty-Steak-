"""Loopback-only HTTP and static-file server for Salty Steak."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import logging
import mimetypes
import shutil
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from .api import ApiRouter, HTTPProblem
from .api.responses import BinaryFileResponse
from .system.config import default_project_root


LOGGER = logging.getLogger("salty-potato.server")
COPY_CHUNK_SIZE = 1024 * 1024


class ApplicationLike(Protocol):
    """The application surface used by the HTTP adapter."""

    config: Any
    paths: Any
    operations: Any
    datasets: Any


@dataclass(frozen=True, slots=True)
class PendingUpload:
    token: str
    path: Path
    filename: str


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class SaltyHTTPServer(ThreadingHTTPServer):
    """Threaded local server with explicit application and upload ownership."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        application: ApplicationLike,
        *,
        project_root: Path,
        static_root: Path,
        upload_root: Path,
        owns_application: bool,
    ) -> None:
        self.application = application
        self.project_root = project_root.resolve()
        self.static_root = static_root.resolve()
        self.upload_root = upload_root.resolve()
        self.owns_application = owns_application
        self._uploads: dict[str, PendingUpload] = {}
        self._uploads_lock = threading.RLock()
        self._closed_once = False
        self.upload_root.mkdir(parents=True, exist_ok=False)
        try:
            super().__init__(server_address, SaltyRequestHandler)
        except Exception:
            shutil.rmtree(self.upload_root, ignore_errors=True)
            raise

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        rendered_host = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{rendered_host}:{port}"

    def remember_upload(self, path: Path, filename: str) -> PendingUpload:
        token = uuid.uuid4().hex
        pending = PendingUpload(token=token, path=path.resolve(), filename=filename)
        with self._uploads_lock:
            self._uploads[token] = pending
        return pending

    def promote_upload(
        self, token: str, claimed_path: str | None
    ) -> tuple[Path, PendingUpload]:
        with self._uploads_lock:
            pending = self._uploads.get(token)
            if pending is None:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_upload_token",
                    "The selected upload is no longer available. Inspect the file again.",
                )
            if claimed_path and Path(claimed_path).resolve() != pending.path:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "upload_path_mismatch",
                    "The upload token does not belong to the supplied source path.",
                )
            if not pending.path.is_file():
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "upload_missing",
                    "The inspected upload is missing. Inspect the file again.",
                )
            destination = (
                Path(self.application.paths.datasets)
                / "sources"
                / pending.token
                / pending.filename
            ).resolve()
            dataset_root = Path(self.application.paths.datasets).resolve()
            if dataset_root not in destination.parents:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST,
                    "unsafe_upload_path",
                    "The upload destination is not safe.",
                )
            destination.parent.mkdir(parents=True, exist_ok=False)
            pending.path.replace(destination)
            return destination, pending

    def finish_upload(self, pending: PendingUpload) -> None:
        with self._uploads_lock:
            self._uploads.pop(pending.token, None)
        with contextlib.suppress(OSError):
            pending.path.parent.rmdir()

    def restore_upload(self, destination: Path, pending: PendingUpload) -> None:
        if destination.exists() and not pending.path.exists():
            pending.path.parent.mkdir(parents=True, exist_ok=True)
            destination.replace(pending.path)
        with contextlib.suppress(OSError):
            destination.parent.rmdir()

    def server_close(self) -> None:
        if self._closed_once:
            return
        self._closed_once = True
        try:
            super().server_close()
        finally:
            shutil.rmtree(self.upload_root, ignore_errors=True)
            if self.owns_application:
                if hasattr(self.application, "close"):
                    self.application.close()
                else:
                    operations = getattr(self.application, "operations", None)
                    runtime = getattr(self.application, "runtime", None)
                    try:
                        if operations is not None:
                            operations.shutdown(wait=True, request_stop=True)
                    finally:
                        if runtime is not None:
                            runtime.unload()


class SaltyRequestHandler(BaseHTTPRequestHandler):
    """Routes a small JSON API and serves the compiled React application."""

    server: SaltyHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:
        self._run_request(include_body=True)

    def do_HEAD(self) -> None:
        self._run_request(include_body=False)

    def do_POST(self) -> None:
        self._run_request(include_body=True)

    def do_DELETE(self) -> None:
        self._run_request(include_body=True)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _run_request(self, *, include_body: bool) -> None:
        request_id = uuid.uuid4().hex
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if "\x00" in path or "\\" in path:
                raise HTTPProblem(
                    HTTPStatus.BAD_REQUEST, "invalid_path", "The request path is invalid."
                )
            if path == "/api" or path.startswith("/api/"):
                if self.command == "HEAD":
                    raise HTTPProblem(
                        HTTPStatus.METHOD_NOT_ALLOWED,
                        "method_not_allowed",
                        "HEAD is not available for API routes.",
                    )
                payload = ApiRouter(self).dispatch(path, parse_qs(parsed.query))
                if isinstance(payload, BinaryFileResponse):
                    self._send_binary_file(
                        payload,
                        include_body=include_body,
                        request_id=request_id,
                    )
                else:
                    self._send_json(
                        HTTPStatus.OK,
                        {"data": payload},
                        include_body=include_body,
                        request_id=request_id,
                    )
            elif self.command in {"GET", "HEAD"}:
                self._serve_static(path, include_body=include_body)
            else:
                raise HTTPProblem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "This method is not available for static content.",
                )
        except (BrokenPipeError, ConnectionResetError):
            return
        except HTTPProblem as exc:
            self._send_error_json(exc.status, exc.code, exc.message, request_id)
        except KeyError as exc:
            message = str(exc.args[0]) if exc.args else "The requested item does not exist."
            self._send_error_json(
                HTTPStatus.NOT_FOUND, "not_found", message, request_id
            )
        except FileNotFoundError as exc:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "file_not_found",
                str(exc) or "The requested file does not exist.",
                request_id,
            )
        except ValueError as exc:
            self._send_error_json(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                str(exc) or "The request is invalid.",
                request_id,
            )
        except PermissionError as exc:
            self._send_error_json(
                HTTPStatus.FORBIDDEN,
                "permission_denied",
                str(exc) or "The requested action is not permitted.",
                request_id,
            )
        except TimeoutError as exc:
            self._send_error_json(
                HTTPStatus.GATEWAY_TIMEOUT,
                "operation_timeout",
                str(exc) or "The operation did not finish in time.",
                request_id,
            )
        except RuntimeError as exc:
            error_name = type(exc).__name__
            if error_name in {
                "DatasetError",
                "DatasetFormatError",
                "DatasetValidationError",
                "DatasetPreparationError",
            }:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_dataset",
                    str(exc) or "The dataset request is invalid.",
                    request_id,
                )
                return
            code = "state_conflict" if error_name == "StateConflict" else "operation_rejected"
            self._send_error_json(
                HTTPStatus.CONFLICT,
                code,
                str(exc) or "The operation could not be completed.",
                request_id,
            )
        except Exception:
            LOGGER.exception("Unhandled request failure (request_id=%s)", request_id)
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "The local service encountered an unexpected error.",
                request_id,
            )

    def _serve_static(self, raw_path: str, *, include_body: bool) -> None:
        root = self.server.static_root
        if not root.is_dir():
            raise HTTPProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "frontend_not_built",
                "The frontend has not been built.",
            )
        relative = raw_path.lstrip("/")
        candidate = (root / relative).resolve() if relative else root / "index.html"
        if candidate != root and root not in candidate.parents:
            raise HTTPProblem(
                HTTPStatus.FORBIDDEN,
                "unsafe_static_path",
                "The requested static path is not safe.",
            )
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            final_name = Path(relative).name
            if relative and "." in final_name:
                raise HTTPProblem(
                    HTTPStatus.NOT_FOUND,
                    "static_not_found",
                    "The requested asset does not exist.",
                )
            candidate = root / "index.html"
        content_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        size = candidate.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'",
        )
        self.end_headers()
        if include_body:
            with candidate.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, COPY_CHUNK_SIZE)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        include_body: bool = True,
        request_id: str | None = None,
    ) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header('Connection', 'close')
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        if include_body:
            self.wfile.write(encoded)

    def _send_binary_file(
        self,
        response: BinaryFileResponse,
        *,
        include_body: bool,
        request_id: str,
    ) -> None:
        path = Path(response.path).resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError("The generated image artifact no longer exists")
        content_type = str(response.content_type or "application/octet-stream")
        if not content_type.startswith("image/"):
            raise ValueError("Only generated image artifacts may use this response")
        filename = Path(str(response.filename or path.name)).name
        if filename != str(response.filename or path.name) or any(
            character in filename for character in ('"', "\r", "\n")
        ):
            raise ValueError("The generated image filename is invalid")
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if response.sha256:
            self.send_header("ETag", f'"sha256-{response.sha256}"')
        self.send_header("X-Request-ID", request_id)
        self.end_headers()
        if include_body:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, COPY_CHUNK_SIZE)

    def _send_error_json(
        self, status: int, code: str, message: str, request_id: str
    ) -> None:
        # Routing/maintenance may reject before reading a request body. Never
        # parse those leftover bytes as the next method on a keep-alive socket.
        # Closing avoids draining an untrusted or potentially enormous body.
        self.close_connection = True
        self._send_json(
            status,
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": request_id,
                }
            },
            request_id=request_id,
        )


@dataclass(slots=True)
class ServerHandle:
    """A background HTTP server that can be stopped deterministically."""

    server: SaltyHTTPServer
    thread: threading.Thread

    @property
    def url(self) -> str:
        return self.server.url

    def stop(self, *, timeout: float = 30.0) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("The local HTTP server did not stop in time")

    def __enter__(self) -> "ServerHandle":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()


def create_server(
    *,
    application: ApplicationLike | None = None,
    project_root: str | Path | None = None,
    host: str | None = None,
    port: int | None = None,
    static_root: str | Path | None = None,
    upload_root: str | Path | None = None,
    owns_application: bool | None = None,
    startup_mark: Callable[[str], None] | None = None,
) -> SaltyHTTPServer:
    """Construct, but do not start, the local server."""

    root = (
        Path(project_root).resolve()
        if project_root is not None
        else default_project_root().resolve()
    )
    created_application = application is None
    if application is None:
        if startup_mark:
            startup_mark("python_application_module_import_started")
        from .application import Application

        if startup_mark:
            startup_mark("python_application_module_import_completed")
            if "torch" in sys.modules:
                startup_mark("torch_import_completed")
            else:
                startup_mark("torch_import_not_required")
            if "transformers" in sys.modules:
                startup_mark("model_framework_import_completed")
            else:
                startup_mark("model_framework_import_not_required")
        application = Application(root, startup_mark=startup_mark)
    if startup_mark:
        startup_mark("local_server_binding_started")
    server_config = application.config.section("server")
    selected_host = str(host if host is not None else server_config["host"])
    selected_port = int(port if port is not None else server_config["port"])
    if not _is_loopback(selected_host):
        if created_application and hasattr(application, "close"):
            application.close()
        raise ValueError("The Salty Steak service may only bind to a loopback address")
    if not 0 <= selected_port <= 65535:
        if created_application and hasattr(application, "close"):
            application.close()
        raise ValueError("server port must be between 0 and 65535")
    frontend = (
        Path(static_root).resolve()
        if static_root is not None
        else (root / "app" / "frontend" / "dist").resolve()
    )
    session_uploads = (
        Path(upload_root).resolve()
        if upload_root is not None
        else (
            Path(application.paths.cache)
            / "http-uploads"
            / uuid.uuid4().hex
        ).resolve()
    )
    owns = created_application if owns_application is None else owns_application
    try:
        server = SaltyHTTPServer(
            (selected_host, selected_port),
            application,
            project_root=root,
            static_root=frontend,
            upload_root=session_uploads,
            owns_application=owns,
        )
        if startup_mark:
            startup_mark("local_server_bound")
        return server
    except Exception:
        if owns and hasattr(application, "close"):
            application.close()
        raise


def start_server(**kwargs: Any) -> ServerHandle:
    """Start the local server in a background thread."""

    server = create_server(**kwargs)
    thread = threading.Thread(
        target=server.serve_forever,
        name="salty-http",
        daemon=True,
    )
    thread.start()
    return ServerHandle(server=server, thread=thread)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Salty Steak local service")
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    arguments = parser.parse_args(argv)
    server = create_server(
        project_root=arguments.project_root,
        host=arguments.host,
        port=arguments.port,
    )
    LOGGER.warning("Salty Steak is available at %s", server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.warning("Stopping Salty Steak")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
