"""Cohesive application workflows over one database and one operation system."""
from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping

from .automation import AutomationBroker
from .api.responses import BinaryFileResponse
from .chat import ChatService
from .chat.vision_inputs import VisionInputStore
from .database.control import (
    ACTIVE_OPERATION_STATES,
    Database,
    json_text,
    new_id,
    parse_json,
    utc_now,
)
from .datasets.library import DatasetLibrary
from .evaluation.evaluator import EvaluationSettings, run_evaluation
from .operations.manager import (
    Notification,
    OperationContext,
    OperationInterrupted,
    OperationManager,
    OperationNeedsAttention,
    OperationPhaseError,
)
from .recovery.production_state import ProductionStatePolicy
from .runtime.manager import RuntimeIdentity, RuntimeManager
from .runtime.model_bundles import ModelBundleRegistry
from .runtime.model_roles import language_checkpoint_role, model_role_catalog
from .runtime.planner import plan_local_runtime
from .runtime.salty_native import SaltyNativeProfile
from .runtime.salty_native_worker import SaltyNativeWorkerRuntime
from .runtime.salty_vision import SaltyVisionBroker
from .runtime.steak_gen import SteakGenWorkerClient
from .scientific_recovery import scientific_recovery_state
from .system.config import AppConfig, load_config
from .system.files import create_storage_layout, ensure_within, sha256_file
from .system.timestamps import utc_timestamp
from .training.engine import (
    TrainingSettings,
    inspect_recovery_state,
    run_training,
)
from .training.identity_promotion import promote_identity_candidate
from .training.identity_workflow import run_identity_post_training
from .training.identity_dialogue_dataset import (
    holdout_examples as identity_holdout_examples,
    training_examples as identity_training_examples,
)
from .training.policy import (
    completion_outcome_for_policy,
    initial_recovery_state,
    normalise_post_training_policy,
)
from .tooling import PluginRegistry
from .versions.checkpoint import (
    IsolatedWorkerError,
    inspect_checkpoint,
)
from .versions.finalisation import FinalisationCoordinator
from .versions.model import ModelConfig
from .versions.tokenizer import SaltyTokenizer


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        candidate.stat().st_size
        for candidate in path.rglob("*")
        if candidate.is_file()
    )


def _directory_measure(path: Path) -> tuple[int, int]:
    """Return stable best-effort bytes and file count for an About measurement."""
    if not path.exists():
        return 0, 0
    candidates = [path] if path.is_file() else path.rglob("*")
    total = 0
    count = 0
    for candidate in candidates:
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
                count += 1
        except OSError:

            continue
    return total, count


def _process_environment(name: str) -> str | None:
    """Read the live host environment, including values set after Python starts."""
    value = os.environ.get(name)
    if value or os.name != "nt":
        return value
    try:
        import ctypes

        size = ctypes.windll.kernel32.GetEnvironmentVariableW(name, None, 0)
        if not size:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        copied = ctypes.windll.kernel32.GetEnvironmentVariableW(
            name,
            buffer,
            size,
        )
        return buffer.value if copied else None
    except (AttributeError, OSError, ValueError):
        return None


def _windows_version_without_process() -> str | None:
    """Describe Windows without letting an About-page read launch ``ver``."""
    getter = getattr(sys, "getwindowsversion", None)
    if callable(getter):
        version = getter()
        return f"Windows {version.major}.{version.minor}.{version.build}"
    return _process_environment("OS")


class _Progress:
    def __init__(
        self,
        context: OperationContext,
        on_update: Any = None,
    ) -> None:
        self.context = context
        self.on_update = on_update

    def update(
        self,
        phase: str,
        *,
        current: int | float | None = None,
        total: int | float | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self.context.update(
            phase=phase,
            current_progress=float(current) if current is not None else None,
            total_progress=float(total) if total is not None else None,
            details=result,
        )
        if self.on_update is not None:
            self.on_update(phase, current, total, result)

    def stop_requested(self) -> bool:
        return self.context.stop_requested()


class Application:
    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        recover_operations: bool = True,
        startup_mark: Callable[[str], None] | None = None,
    ) -> None:
        mark = startup_mark or (lambda _phase: None)
        mark("configuration_loading_started")
        self.config: AppConfig = load_config(project_root)
        mark("configuration_loaded")
        mark("workspace_layout_started")
        self.paths = create_storage_layout(self.config)
        self.model_bundles = ModelBundleRegistry(self.paths.workspace / "models")
        mark("workspace_layout_ready")
        mark("database_opening_and_schema_migration_started")
        self.database = Database(self.paths.database)
        self.production_policy = ProductionStatePolicy(self.database)
        mark("database_open_and_schema_migration_completed")
        operation_settings = dict(self.config.section("operations"))
        self.finalisation = FinalisationCoordinator(
            self.database,
            self.paths.training / "finalisation-results",
            bootstrap_timeout_seconds=float(
                operation_settings.get("interpreter_bootstrap_timeout_seconds", 10)
            ),
            worker_start_timeout_seconds=float(
                operation_settings.get("worker_start_timeout_seconds", 30)
            ),
            integrity_timeout_seconds=float(
                operation_settings.get("checkpoint_integrity_timeout_seconds", 180)
            ),
            model_load_timeout_seconds=float(
                operation_settings.get("isolated_model_load_timeout_seconds", 900)
            ),
            verification_timeout_seconds=float(
                operation_settings.get("checkpoint_verification_timeout_seconds", 1080)
            ),
        )
        mark("operation_manager_reconciliation_started")
        self.operations = OperationManager(
            self.database,
            max_workers=2,
            recover_incomplete=recover_operations,
        )
        mark("operation_manager_reconciliation_completed")
        self._request_lock = threading.Lock()
        self._evaluation_claim_lock = threading.Lock()
        self._about_storage_lock = threading.Lock()
        self._about_storage_cache: dict[str, Any] | None = None
        mark("tokenizer_identity_resolution_started")
        self.tokenizer = (
            SaltyTokenizer(self.paths.tokenizer)
            if (self.paths.tokenizer / "tokenizer.model").is_file()
            else None
        )
        mark("tokenizer_identity_resolution_completed")
        self.datasets = DatasetLibrary.from_config(
            self.database,
            self.config,
            encoder=self.tokenizer.encode if self.tokenizer else None,
            operations=self.operations,
            tokenizer_reference=str(self.paths.tokenizer),
            tokenizer_fingerprint=(
                self.tokenizer.fingerprint if self.tokenizer else None
            ),
            bos_token_id=self.tokenizer.bos_id if self.tokenizer else None,
            eos_token_id=self.tokenizer.eos_id if self.tokenizer else None,
            pad_token_id=self.tokenizer.pad_id if self.tokenizer else None,
            tokenizer=self.tokenizer,
        )
        runtime = self.config.section("training")
        self.runtime = RuntimeManager(
            self.paths.runtime,
            device=str(runtime["device"]),
            precision=str(runtime["precision"]),
        )
        self.model_bundle_runtime: SaltyNativeWorkerRuntime | None = None
        self.vision_broker: SaltyVisionBroker | None = None
        self._vision_verification_thread: threading.Thread | None = None
        self.vision_inputs = VisionInputStore(
            self.paths.conversations / "vision-inputs"
        )
        self.vision_inputs.purge_expired_unclaimed()
        self._native_warmup_thread: threading.Thread | None = None
        selected_bundle = self.model_bundles.selected_base("text_generation")
        self.model_bundle_runtime = self._runtime_for_model_bundle(selected_bundle)
        vision_bundle = selected_bundle
        if vision_bundle is not None:
            projector = next(
                (
                    companion
                    for companion in vision_bundle.get("companion_artifacts", [])
                    if companion.get("role") == "vision_projector"
                ),
                None,
            )
            if projector is not None:
                self.vision_broker = SaltyVisionBroker(
                    text_model_path=vision_bundle["model_path"],
                    projector_path=projector["artifact_path"],
                    runtime_directory=self.paths.runtime / "salty-vision",
                    temporary_root=self.paths.cache / "salty-vision-execution",
                    text_model_sha256=str(vision_bundle["checksum"]),
                    projector_sha256=str(projector["checksum"]),
                    text_adapters=[
                        {
                            "id": companion["id"],
                            "path": companion["artifact_path"],
                            "sha256": companion["checksum"],
                            "scale": float(companion.get("scale", 1.0)),
                        }
                        for companion in vision_bundle.get(
                            "companion_artifacts", []
                        )
                        if companion.get("role") == "text_adapter"
                        and companion.get("activation", "always") == "always"
                    ],
                    timeout_seconds=900,
                    device="CUDA0",
                    gpu_layers=24,
                    mmproj_offload=False,
                    require_staged_manifest=True,
                )
        image_generation_model = next(
            iter(self.model_bundles.list("image_generation")),
            None,
        )
        self.image_generation_runtime = (
            SteakGenWorkerClient(
                project_root=self.config.project_root,
                workspace_root=self.paths.workspace,
            )
            if image_generation_model is not None
            and image_generation_model.get("activation_allowed") is True
            and image_generation_model.get("external_service_required") is False
            else None
        )
        self.automation = AutomationBroker(
            self.database,



            project_root=self.paths.workspace.parent,
            artifact_root=self.paths.workspace / "automation",

            package_root=self.config.project_root,
        )
        self.chat = ChatService(
            database=self.database,
            operations=self.operations,
            runtime=self.runtime,
            config=self.config,
            start_activation=self._restore_version_for_chat,
            model_bundle_runtime=self.model_bundle_runtime,
            model_bundle=selected_bundle,
            image_generation_model=image_generation_model,
            image_generation_runtime=self.image_generation_runtime,
            image_artifact_root=self.paths.conversations / "artifacts",
            automation=self.automation,
            vision_broker=self.vision_broker,
            vision_inputs=self.vision_inputs,
        )
        self.plugins = PluginRegistry(self.database)

        self.tools = self.plugins
        mark("restart_reconciliation_started")
        self.finalisation.invalidate_orphaned_attempts()
        self._reconcile_recovery_slot()
        self._reconcile_restart_state()
        self._reconcile_completed_checkpoints()
        self._reconcile_post_training_workflows()
        mark("restart_reconciliation_completed")
        self._record_configuration()
        mark("application_configuration_recorded")
        if self.model_bundle_runtime is not None:
            self._native_warmup_thread = threading.Thread(
                target=self._warm_selected_model_bundle,
                name="salty-native-background-warmup",
                daemon=True,
            )
            self._native_warmup_thread.start()
        if (
            self.vision_broker is not None
            and self.vision_broker.stage_manifest_path.is_file()
        ):
            self._vision_verification_thread = threading.Thread(
                target=self._verify_staged_vision_runtime,
                name="salty-vision-integrity-verification",
                daemon=True,
            )
            self._vision_verification_thread.start()

    def _runtime_for_model_bundle(
        self,
        bundle: Mapping[str, Any] | None,
    ) -> SaltyNativeWorkerRuntime | None:
        if bundle is None:
            return None
        native_library_directory = (
            self.paths.workspace / "runtime" / "salty-native-steak20" / "bin"
        )
        declared_adapters = [
            companion
            for companion in bundle.get("companion_artifacts", [])
            if companion.get("role") in {"text_adapter", "routing_adapter"}
        ]
        adapters_ready = (
            bool(declared_adapters)
            or not bool(bundle.get("identity_adapter_required"))
        ) and all(
            companion.get("current_size_matches") for companion in declared_adapters
        )
        if not (
            bundle.get("runtime_family") == "salty_native_steak20"
            and bundle.get("current_size_matches")
            and adapters_ready
            and native_library_directory.is_dir()
        ):
            return None
        return SaltyNativeWorkerRuntime(
            model_path=bundle["model_path"],
            library_directory=native_library_directory,
            source_sha256=str(bundle["checksum"]),
            profile=SaltyNativeProfile.from_manifest(bundle.get("runtime_profile")),
            adapters=[
                {
                    "adapter_id": companion["id"],
                    "path": companion["artifact_path"],
                    "sha256": companion["checksum"],
                    "scale": float(companion.get("scale", 1.0)),
                    "activation": str(companion.get("activation") or "always"),
                }
                for companion in declared_adapters
            ],
        )

    def _warm_selected_model_bundle(self) -> None:
        try:
            self.chat.warm_selected_model_bundle()
        except BaseException:


            return

    def _verify_staged_vision_runtime(self) -> None:
        broker = self.vision_broker
        if broker is None:
            return
        text_warmup = self._native_warmup_thread
        if text_warmup is not None and text_warmup is not threading.current_thread():
            text_warmup.join()
        try:
            broker.verify_staged_runtime()
        except BaseException:

            return

    def close(self) -> None:
        self.automation.close()
        for operation in self.operations.list(
            states=tuple(ACTIVE_OPERATION_STATES), limit=1000
        ):
            if operation["type"] in {
                "training",
                "training_identity",
                "evaluation",
                "evaluation_activation",
                "post_training_recovery",
                "training_finalisation_retry",
                "dataset_validation",
                "dataset_preparation",
            }:
                self.operations.request_stop(operation["id"])
        self.operations.shutdown(wait=True, request_stop=False)
        if self._vision_verification_thread is not None:
            self._vision_verification_thread.join(timeout=30)
        if self.model_bundle_runtime is not None:
            runtime_identity = self.model_bundle_runtime.describe()
            self.model_bundle_runtime.unload()
            if self._native_warmup_thread is not None:
                self._native_warmup_thread.join(timeout=30)
            runtime_id = runtime_identity.get("runtime_id")
            if runtime_id:
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE chat_runtime_states
                        SET state = 'unloaded', unloaded_at = COALESCE(unloaded_at, ?)
                        WHERE id = ? AND state = 'loaded'
                        """,
                        (utc_now(), str(runtime_id)),
                    )
        self.runtime.unload()

    def _record_configuration(self) -> None:
        self.database.set_config_reference(
            "defaults", str(self.config.defaults_path), checksum=sha256_file(self.config.defaults_path)
        )
        self.database.set_config_reference(
            "local",
            str(self.config.local_path),
            checksum=(
                sha256_file(self.config.local_path)
                if self.config.local_path.is_file()
                else None
            ),
        )
        if self.tokenizer:
            self.database.set_config_reference(
                "tokenizer",
                str(self.paths.tokenizer),
                checksum=self.tokenizer.fingerprint,
            )

    def _reconcile_recovery_slot(self) -> None:
        """Finish or clean only the app's interrupted atomic directory swap."""

        recovery = self.paths.training / "recovery"
        previous = self.paths.training / ".recovery-previous"
        if not recovery.exists() and previous.is_dir():
            previous.replace(recovery)
        elif recovery.exists() and previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        for temporary in self.paths.training.glob(".recovery-next-*"):
            if temporary.is_dir():
                shutil.rmtree(temporary, ignore_errors=True)

    def _reconcile_restart_state(self) -> None:
        """Repair only new-schema domain rows after abandoned in-process work."""

        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE evaluations
                SET status = (
                        SELECT CASE o.state
                            WHEN 'completed' THEN 'completed'
                            WHEN 'failed' THEN 'failed'
                            ELSE 'interrupted'
                        END
                        FROM operations o WHERE o.id = evaluations.operation_id
                    ),
                    updated_at = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE status IN ('queued','running')
                  AND EXISTS (
                      SELECT 1 FROM operations o
                      WHERE o.id = evaluations.operation_id
                        AND o.state IN ('completed','interrupted','failed')
                  )
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE datasets
                SET validation_status = 'failed', updated_at = ?
                WHERE validation_status = 'validating'
                  AND NOT EXISTS (
                      SELECT 1 FROM operations o
                      WHERE o.type = 'dataset_validation'
                        AND o.target_id = datasets.id
                        AND o.state IN ('queued','running','stop_requested')
                  )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE datasets
                SET prepared_status = CASE
                        WHEN EXISTS (
                            SELECT 1 FROM prepared_datasets p
                            WHERE p.dataset_id = datasets.id
                              AND p.status = 'ready' AND p.verified = 1
                              AND p.source_checksum = datasets.source_checksum
                        ) THEN 'ready'
                        ELSE 'failed'
                    END,
                    updated_at = ?
                WHERE prepared_status = 'preparing'
                  AND NOT EXISTS (
                      SELECT 1 FROM operations o
                      WHERE o.type = 'dataset_preparation'
                        AND o.target_id = datasets.id
                        AND o.state IN ('queued','running','stop_requested')
                  )
                """,
                (now,),
            )



    def operation_for_request(self, request_key: str | None) -> dict[str, Any] | None:
        if not request_key:
            return None
        row = self.database.fetch_one(
            """
            SELECT o.* FROM operation_requests r
            JOIN operations o ON o.id = r.operation_id
            WHERE r.request_key = ?
            """,
            (request_key,),
        )
        return self.database.operation_record(row) if row else None

    def operation_events(
        self,
        operation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Return a bounded, ordered page of durable operation events."""

        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if self.operations.get(operation_id) is None:
            raise KeyError(f"Operation does not exist: {operation_id}")
        rows = self.database.fetch_all(
            """
            SELECT id, operation_id, sequence, event_type, state, phase,
                   evidence_json, created_at
            FROM operation_events
            WHERE operation_id = ? AND sequence > ?
            ORDER BY sequence
            LIMIT ?
            """,
            (operation_id, after_sequence, limit + 1),
        )
        has_more = len(rows) > limit
        selected = rows[:limit]
        events = []
        for row in selected:
            event = dict(row)
            event["evidence"] = parse_json(event.pop("evidence_json", None))
            events.append(event)
        return {
            "operation_id": operation_id,
            "events": events,
            "after_sequence": after_sequence,
            "next_after_sequence": (
                int(events[-1]["sequence"]) if events else after_sequence
            ),
            "has_more": has_more,
        }

    def bind_request(self, request_key: str | None, operation_id: str) -> None:
        if not request_key:
            return
        self.database.execute(
            """
            INSERT OR IGNORE INTO operation_requests(request_key, operation_id, created_at)
            VALUES (?, ?, ?)
            """,
            (request_key, operation_id, utc_now()),
        )

    def _idempotent_operation(
        self, request_key: str | None, launch
    ) -> dict[str, Any]:



        with self._request_lock:
            existing = self.operation_for_request(request_key)
            if existing:
                return existing
            operation = launch()
            self.bind_request(request_key, operation["id"])
            mapped = self.operation_for_request(request_key)
            if request_key and (not mapped or mapped["id"] != operation["id"]):
                raise RuntimeError("request key is already bound to another operation")
            return operation



    def inspect_dataset(self, path: str | Path) -> dict[str, Any]:
        return self.datasets.inspect_source(path)

    def preview_dataset(
        self, path: str | Path, mapping: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.datasets.preview_mapping(path, mapping)

    def add_dataset(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.datasets.add_dataset(
            str(payload["path"]),
            name=str(payload["name"]),
            language=str(payload["language"]),
            purpose=str(payload["purpose"]),
            description=(
                str(payload["description"]) if payload.get("description") else None
            ),
            mapping=dict(payload["mapping"]),
        )

    def list_datasets(self) -> list[dict[str, Any]]:
        records = self.datasets.list_datasets()
        for record in records:
            prepared = self.datasets.latest_ready_prepared(record["id"])
            if prepared:
                record["prepared_dataset_id"] = prepared["id"]
                record["prepared_id"] = prepared["id"]
                record["prepared_path"] = prepared["path"]
                record["prepared_record_count"] = prepared["record_count"]
                record["prepared_token_count"] = prepared["token_count"]
                record["prepared_settings"] = prepared["settings"]
            self.production_policy.enrich_dataset(record)
        return records

    def scientific_recovery_state(self) -> dict[str, Any]:
        """Expose packaged, non-operative scientific recovery evidence."""

        return scientific_recovery_state()

    def production_recovery_state(self) -> dict[str, Any]:
        """Expose live, policy-enforced production recovery semantics."""

        return self.production_policy.current_state()

    def validate_dataset(
        self, dataset_id: str, request_key: str | None
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key, lambda: self.datasets.start_validation(dataset_id)
        )

    def prepare_dataset(
        self,
        dataset_id: str,
        settings: Mapping[str, Any],
        request_key: str | None,
    ) -> dict[str, Any]:
        self.production_policy.assert_dataset_preparation_allowed(dataset_id)
        return self._idempotent_operation(
            request_key,
            lambda: self.datasets.start_preparation(dataset_id, settings),
        )

    def preflight_dataset_preparation(
        self, dataset_id: str, settings: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.production_policy.assert_dataset_preparation_allowed(dataset_id)
        return self.datasets.preparation_preflight(dataset_id, settings)



    def _version_rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
	            SELECT v.*, d.name AS dataset_name, p.dataset_id,
	                   p.source_checksum AS dataset_fingerprint,
	                   p.artifact_checksum AS prepared_fingerprint,
	                   p.record_count AS prepared_record_count,
	                   p.token_count AS prepared_token_count,
	                   p.validation_record_count AS prepared_validation_record_count,
	                   p.verified AS prepared_verified,
	                   p.status AS prepared_status,
	                   t.starting_version_id AS legacy_parent_version_id,
                   t.post_training_policy,
                   t.post_training_state,
                   t.post_training_recovery_state,
                   t.required_evaluation_id,
                   CASE WHEN a.saved_version_id = v.id THEN 1 ELSE 0 END AS active,
                   e.id AS evaluation_id,
                   e.status AS evaluation_status,
                   e.metrics_json AS evaluation_metrics
            FROM saved_versions v
            LEFT JOIN prepared_datasets p ON p.id = v.prepared_dataset_id
            LEFT JOIN datasets d ON d.id = p.dataset_id
            LEFT JOIN training_operations t ON t.id = v.training_operation_id
            LEFT JOIN active_runtime a ON a.saved_version_id = v.id
            LEFT JOIN evaluations e ON e.id = (
                SELECT e2.id FROM evaluations e2
                WHERE e2.saved_version_id = v.id
                ORDER BY COALESCE(e2.finished_at, e2.created_at) DESC
                LIMIT 1
            )
            WHERE v.integrity = 'verified'
            ORDER BY v.verified_at DESC, v.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        records = [self._version_record(row) for row in rows]
        for record in records:
            memberships = self.database.fetch_all(
                """
                SELECT vl.*, l.dataset_id, l.prepared_dataset_id,
                       l.dataset_fingerprint, l.prepared_fingerprint,
                       l.baseline_version_id,
                       d.name AS lineage_dataset_name
                FROM version_lineage vl
                JOIN dataset_lineages l ON l.id = vl.lineage_id
                JOIN datasets d ON d.id = l.dataset_id
                WHERE vl.saved_version_id = ?
                ORDER BY CASE vl.role WHEN 'baseline' THEN 0 ELSE 1 END,
                         vl.created_at DESC
                """,
                (record["id"],),
            )
            if memberships:
                primary = memberships[0]
                record["lineages"] = memberships
                record["lineage_id"] = primary["lineage_id"]
                record["parent_version_id"] = primary["parent_version_id"]
                record["baseline_version_id"] = primary["baseline_version_id"]
                record["dataset_id"] = (
                    record.get("dataset_id") or primary["dataset_id"]
                )
                record["dataset_name"] = (
                    record.get("dataset_name")
                    or primary["lineage_dataset_name"]
                )
                record["role"] = primary["role"]
                record["baseline_status"] = "protected"
                record["retention_state"] = primary["cleanup_status"]
                record["recovered_after_restart"] = bool(
                    primary["recovered_after_restart"]
                )
            else:
                legacy_key = "\0".join(
                    str(record.get(key) or "unknown")
                    for key in (
                        "dataset_fingerprint",
                        "prepared_fingerprint",
                        "tokenizer_checksum",
                        "architecture_revision",
                    )
                )
                record["lineages"] = []
                record["lineage_id"] = (
                    "legacy-" + hashlib.sha256(
                        legacy_key.encode("utf-8")
                    ).hexdigest()[:16]
                )
                record["parent_version_id"] = record.pop(
                    "legacy_parent_version_id", None
                )
                record["baseline_version_id"] = None
                record["role"] = "trained"
                record["baseline_status"] = "missing_historical_baseline"
                record["retention_state"] = "retained"
                record["recovered_after_restart"] = bool(
                    (record.get("id") and self.database.fetch_one(
                        """
                        SELECT 1 FROM operations o
                        JOIN training_operations t ON t.operation_id = o.id
                        WHERE t.result_version_id = ?
                          AND json_extract(o.result_json, '$.reconciled_after_restart') = 1
                        """,
                        (record["id"],),
                    ))
                )
            if record["role"] == "baseline":
                record["display_label"] = "Baseline"
            elif record["total_trained_steps"] is None:
                record["display_label"] = (
                    f"After +{record['additional_steps']:,} steps · total unknown"
                )
            else:
                record["display_label"] = (
                    f"After +{record['additional_steps']:,} steps · "
                    f"{record['total_trained_steps']:,} total"
                )
        return records

    @staticmethod
    def _version_record(row: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record.update(language_checkpoint_role())
        raw_metrics = record.pop("evaluation_metrics", None)
        try:
            metrics = parse_json(raw_metrics, None)
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = None
        record["saved_at"] = record["created_at"]
        record["total_steps"] = record["total_trained_steps"]
        record["context_limit_tokens"] = record["context_tokens"]
        record["architectural_context_tokens"] = record["context_tokens"]
        record["folder_path"] = record["checkpoint_path"]
        record["path"] = record["checkpoint_path"]
        selected_for_chat = bool(record.pop("active"))
        record["selected_for_chat"] = selected_for_chat
        record["chat_status"] = "active" if selected_for_chat else "not_active"
        evaluation_status = str(record.get("evaluation_status") or "")
        summary = "Not evaluated"
        if evaluation_status == "completed" and isinstance(metrics, dict):
            try:
                summary = f"Loss {float(metrics['loss']):.4f}"
            except (KeyError, TypeError, ValueError):
                summary = "Evaluation evidence invalid"
        elif evaluation_status == "failed":
            summary = "Evaluation failed"
        record["evaluation_summary"] = summary
        record["evaluation_state"] = (
            "completed"
            if evaluation_status == "completed" and isinstance(metrics, dict)
            else evaluation_status or "not_evaluated"
        )
        record["post_training_policy"] = record.get(
            "post_training_policy", "legacy_unknown"
        )
        record["post_training_state"] = record.get(
            "post_training_state", "legacy_unknown"
        )
        record["post_training_recovery_state"] = record.get(
            "post_training_recovery_state", "legacy_unknown"
        )
        record["required_evaluation_id"] = record.get("required_evaluation_id")
        return record

    def list_versions(self) -> list[dict[str, Any]]:
        records = self._version_rows(limit=100)
        runtime_identity = self.runtime.identity
        for record in records:
            self.production_policy.enrich_version(record)
            version_policy = record["production_policy"]
            prepared_policy = self.production_policy.prepared(
                record.get("prepared_dataset_id")
            )
            record["runtime_loaded"] = bool(
                runtime_identity
                and runtime_identity.checkpoint_id == record["id"]
                and runtime_identity.weight_sha256 == record["checksum"]
            )
            record["runtime_id"] = (
                runtime_identity.runtime_id
                if record["runtime_loaded"] and runtime_identity
                else None
            )
            record["chat_status"] = (
                "active"
                if record["chat_status"] == "active" and record["runtime_loaded"]
                else "selected_not_loaded"
                if record["chat_status"] == "active"
                else "not_active"
            )
            evaluation = self._valid_evaluation_evidence(record["id"])
            evaluation_valid = evaluation is not None
            validation_available = bool(
                record.get("prepared_status") == "ready"
                and bool(record.get("prepared_verified"))
                and int(record.get("prepared_validation_record_count") or 0) > 0
                and prepared_policy["evaluation_allowed"]
            )
            delete_code, delete_reason = self._version_usage_block(record["id"])
            record["evaluation_state"] = (
                "completed" if evaluation_valid else record["evaluation_state"]
            )
            record["evaluation_evidence"] = (
                {
                    "evaluation_id": evaluation["evaluation_id"],
                    "report_path": evaluation["report_path"],
                    "report_checksum": evaluation["report_checksum"],
                    "metrics": evaluation["metrics"],
                }
                if evaluation
                else None
            )
            record["available_actions"] = {
                "use_in_chat": {
                    "enabled": bool(
                        not record["runtime_loaded"]
                        and (evaluation_valid or validation_available)
                        and version_policy["activation_allowed"]
                    ),
                    "requires_evaluation": bool(not evaluation_valid),
                    "mode": (
                        "activation_only"
                        if evaluation_valid
                        else "evaluation_and_activation"
                    ),
                    "reason": (
                        version_policy["rationale"]
                        if not version_policy["activation_allowed"]
                        else None
                        if evaluation_valid or validation_available
                        else "No permitted validation split is available for this version."
                    ),
                },
                "evaluate": {
                    "enabled": validation_available,
                    "reason": (
                        None
                        if validation_available
                        else "No permitted validation split is available for this version."
                    ),
                },
                "continue_training": {
                    "enabled": bool(
                        record.get("role") != "baseline"
                        and version_policy["continuation_allowed"]
                    ),
                    "reason": (
                        version_policy["rationale"]
                        if not version_policy["continuation_allowed"]
                        else
                        None
                        if record.get("role") != "baseline"
                        else "The protected baseline cannot be continued directly."
                    ),
                },
                "open_folder": {"enabled": True, "reason": None},
                "technical_details": {"enabled": True, "reason": None},
                "delete": {
                    "enabled": delete_code is None,
                    "reason": delete_reason,
                    "block_code": delete_code,
                },
            }
            record["lifecycle_status"] = (
                "active_loaded"
                if record["runtime_loaded"]
                else "selected_not_loaded"
                if record.get("selected_for_chat")
                else "evaluation_failed"
                if record.get("evaluation_status") == "failed"
                else "evaluation_unavailable"
                if not evaluation_valid and not validation_available
                else "evaluated_inactive"
                if evaluation_valid
                else "verified_unevaluated"
            )
            record["primary_action"] = {
                "key": (
                    "in_chat_now"
                    if record["runtime_loaded"]
                    else "retry_activation"
                    if record.get("selected_for_chat")
                    else "retry_evaluation_and_use_in_chat"
                    if (
                        record.get("evaluation_status") == "failed"
                        and validation_available
                    )
                    else "evaluate_and_use_in_chat"
                    if not evaluation_valid and validation_available
                    else "evaluation_unavailable"
                    if not evaluation_valid and not validation_available
                    else "use_in_chat"
                ),
                "label": (
                    "In Chat now"
                    if record["runtime_loaded"]
                    else "Retry activation"
                    if record.get("selected_for_chat")
                    else "Retry evaluation and use in Chat"
                    if (
                        record.get("evaluation_status") == "failed"
                        and validation_available
                    )
                    else "Evaluate and use in Chat"
                    if not evaluation_valid and validation_available
                    else "Evaluation unavailable"
                    if not evaluation_valid and not validation_available
                    else "Use in Chat"
                ),
            }
            if not version_policy["activation_allowed"]:
                record["primary_action"] = {
                    "key": "forensic_read_only",
                    "label": "View evidence",
                }
        return records

    def model_roles(self) -> dict[str, Any]:
        language_count = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM saved_versions WHERE integrity = 'verified'"
        )
        bundles = self.model_bundles.list()
        counts: dict[str, int] = {
            "text_generation": int(language_count["count"] if language_count else 0)
        }
        for bundle in bundles:
            role = str(bundle["model_role"])
            counts[role] = counts.get(role, 0) + 1
        vision_models: list[dict[str, Any]] = []
        try:
            base_steak = self.model_bundles.get("base-steak-2-0-9b")
        except KeyError:
            base_steak = None
        if base_steak is not None:
            projector = next(
                (
                    companion
                    for companion in base_steak.get("companion_artifacts", [])
                    if companion.get("role") == "vision_projector"
                ),
                None,
            )
            if projector is not None:
                vision_status = self.chat.vision_status()
                vision_models.append(
                    {
                        "id": projector["id"],
                        "display_name": projector["display_name"],
                        "friendly_name": projector["display_name"],
                        "model_role": "vision_language",
                        "model_role_label": "Vision",
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                        "library_kind": "paired_companion_artifact",
                        "paired_text_model_id": base_steak["id"],
                        "independent_chat_selection": False,
                        "artifact_size_bytes": projector["artifact_size_bytes"],
                        "expected_size_bytes": projector["expected_size_bytes"],
                        "checksum": projector["checksum"],
                        "checksum_algorithm": "sha256",
                        "integrity": (
                            "verified"
                            if projector["current_size_matches"]
                            else "needs_attention"
                        ),
                        "model_format": projector["format"],
                        "quantization": projector["quantization"],
                        "runtime_family": "salty_vision",
                        "runtime_profile": dict(
                            vision_status["analysis_profile"]
                        ),
                        "runtime_state": (
                            "ready"
                            if vision_status["application_available"]
                            else "runtime_required"
                        ),
                        "runtime_reason": vision_status["reason"],
                        "activation_allowed": bool(
                            vision_status["application_available"]
                        ),
                        "selected_as_base": False,
                        "runtime_loaded": False,
                        "available_actions": {
                            "analyze_one_image": {
                                "enabled": bool(
                                    vision_status["application_available"]
                                ),
                                "reason": (
                                    None
                                    if vision_status["application_available"]
                                    else vision_status["reason"]
                                ),
                            },
                            "use_in_chat": {
                                "enabled": False,
                                "reason": (
                                    "This projector is paired with Base Steak 2.0 "
                                    "and is not an independent Chat model."
                                ),
                            },
                        },
                    }
                )
        counts["vision_language"] = len(vision_models)
        catalog = model_role_catalog(counts)
        for role in catalog["roles"]:
            role["models"] = (
                vision_models
                if role["id"] == "vision_language"
                else [
                    bundle
                    for bundle in bundles
                    if bundle["model_role"] == role["id"]
                ]
            )
            if role["id"] == "vision_language" and vision_models:
                role["selectable"] = False
                role["implementation_state"] = (
                    "available"
                    if vision_models[0]["activation_allowed"]
                    else "runtime_required"
                )
        return catalog

    def get_model_bundle(self, model_id: str) -> dict[str, Any]:
        return self.model_bundles.get(model_id)

    def get_version(self, version_id: str) -> dict[str, Any]:
        rows = [
            row for row in self._version_rows(limit=100) if row["id"] == version_id
        ]
        if not rows:
            raise KeyError(f"Saved version does not exist: {version_id}")
        return rows[0]

    def version_technical_details(self, version_id: str) -> dict[str, Any]:
        version = self.get_version(version_id)
        root = ensure_within(version["checkpoint_path"], self.paths.versions)
        if root == self.paths.versions.resolve():
            raise ValueError("A saved version cannot use the versions root")
        config = ModelConfig.from_file(root / "config.json")
        return {
            "weight_path": str(root / "model.safetensors"),
            "checksum": version["checksum"],
            "tensor_format": "SafeTensors",
            "architecture_revision": version["architecture_revision"],
            "architectural_context_tokens": version["context_tokens"],
            "training_sequence_length": int(
                self.config.section("training")["sequence_length"]
            ),
            "tokenizer_checksum": version["tokenizer_checksum"],
            "verified_at": version["verified_at"],
            "parameter_count": config.expected_parameter_count(),
            "context_note": (
                "This rotary-position checkpoint passed deterministic generation "
                "beyond 4,096 tokens and supports an 8,192-token architectural "
                "context. Long-range quality still depends on continued training."
            ),
        }

    def _version_usage_block(
        self,
        version_id: str,
        *,
        exclude_operation_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        production_policy = self.production_policy.version(version_id)
        if production_policy["deletion_protected"]:
            return (
                "recovery_evidence_protected",
                "This version is protected recovery evidence.",
            )
        baseline = self.database.fetch_one(
            "SELECT 1 FROM dataset_lineages WHERE baseline_version_id = ? LIMIT 1",
            (version_id,),
        )
        if baseline:
            return "protected_baseline", "A dataset lineage baseline is always protected."
        protected = self.database.fetch_one(
            """
            SELECT 1 FROM version_lineage
            WHERE saved_version_id = ? AND user_protected = 1 LIMIT 1
            """,
            (version_id,),
        )
        if protected:
            return "user_protected", "This saved version is protected."
        active = self.database.fetch_one(
            "SELECT 1 FROM active_runtime WHERE saved_version_id = ?",
            (version_id,),
        )
        if active:
            return "active_in_chat", "Use the other version in chat first."
        count = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM saved_versions WHERE integrity = 'verified'"
        )
        if int((count or {}).get("count", 0)) <= 1:
            return "only_valid_version", "The only valid completed version must remain."
        operation = self.database.fetch_one(
            """
            SELECT 1 FROM operations
            WHERE target_id = ? AND state IN ('queued','running','stop_requested')
              AND (? IS NULL OR id <> ?)
            LIMIT 1
            """,
            (version_id, exclude_operation_id, exclude_operation_id),
        )
        if operation:
            return "operation_uses_version", "A running operation is using this version."
        dependent = self.database.fetch_one(
            """
            SELECT 1 FROM operations
            WHERE target_id = ?
              AND type IN (
                  'evaluation',
                  'evaluation_activation',
                  'post_training_recovery',
                  'export',
                  'gguf_conversion',
                  'version_activation'
              )
              AND state IN ('queued','running','stop_requested')
              AND (? IS NULL OR id <> ?)
            LIMIT 1
            """,
            (version_id, exclude_operation_id, exclude_operation_id),
        )
        if dependent:
            return "dependent_operation", "An unfinished operation requires this version."
        finalisation = self.database.fetch_one(
            """
            SELECT 1
            FROM operations retry
            JOIN training_operations t ON t.operation_id = retry.target_id
            WHERE retry.type = 'training_finalisation_retry'
              AND retry.state IN ('queued','running','stop_requested')
              AND t.result_version_id = ?
              AND (? IS NULL OR retry.id <> ?)
            LIMIT 1
            """,
            (version_id, exclude_operation_id, exclude_operation_id),
        )
        if finalisation:
            return (
                "dependent_operation",
                "A training finalisation retry still requires this version.",
            )
        training = self.database.fetch_one(
            """
            SELECT 1 FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            WHERE t.starting_version_id = ?
              AND t.result_version_id IS NULL
              AND o.state IN ('queued','running','stop_requested')
              AND (? IS NULL OR o.id <> ?)
            LIMIT 1
            """,
            (version_id, exclude_operation_id, exclude_operation_id),
        )
        if training:
            return "operation_uses_version", "A running operation is using this version."
        return None, None

    def deletion_preview(
        self,
        version_id: str,
        *,
        exclude_operation_id: str | None = None,
    ) -> dict[str, Any]:
        version = self.get_version(version_id)
        block_code, reason = self._version_usage_block(
            version_id,
            exclude_operation_id=exclude_operation_id,
        )
        root = Path(version["checkpoint_path"])
        files = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
        ]
        evaluation_paths = [
            ensure_within(row["result_path"], self.paths.evaluations)
            for row in self.database.fetch_all(
                "SELECT result_path FROM evaluations WHERE saved_version_id = ?",
                (version_id,),
            )
            if row.get("result_path")
        ]
        runtime_paths = [
            ensure_within(row["artifact_path"], self.paths.runtime)
            for row in self.database.fetch_all(
                "SELECT artifact_path FROM runtime_states WHERE saved_version_id = ?",
                (version_id,),
            )
        ]
        reclaimed = _directory_size(root) + sum(
            _directory_size(path) for path in evaluation_paths + runtime_paths
        )
        return {
            "allowed": block_code is None,
            "block_code": block_code,
            "reason": reason or "Deletion checks passed.",
            "files_removed": files,
            "storage_reclaimed_bytes": reclaimed,
        }

    def delete_version(
        self, version_id: str, request_key: str | None
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "version_deletion",
                lambda context: self._delete_version_worker(version_id, context),
                target_id=version_id,
                dedupe_key=f"version-deletion:{version_id}",
                success_notification=Notification(
                    "success", "Saved version deleted", "The selected files were removed."
                ),
                failure_notification=Notification(
                    "error", "Version deletion failed", "No other version was renamed."
                ),
            ),
        )

    def _delete_version_worker(
        self,
        version_id: str,
        context: OperationContext,
        *,
        retention_lineage_id: str | None = None,
    ) -> dict[str, Any]:
        preview = self.deletion_preview(
            version_id,
            exclude_operation_id=context.operation_id,
        )
        if not preview["allowed"]:
            raise ValueError(preview["reason"])
        version = self.get_version(version_id)
        context.update(phase="Deleting", total_progress=1)
        root = ensure_within(version["checkpoint_path"], self.paths.versions)
        if root == self.paths.versions.resolve():
            raise ValueError("A saved version cannot use the versions root")
        related = [root]
        related.extend(
            ensure_within(row["result_path"], self.paths.evaluations)
            for row in self.database.fetch_all(
                "SELECT result_path FROM evaluations WHERE saved_version_id = ?",
                (version_id,),
            )
            if row.get("result_path")
        )
        related.extend(
            ensure_within(row["artifact_path"], self.paths.runtime)
            for row in self.database.fetch_all(
                "SELECT artifact_path FROM runtime_states WHERE saved_version_id = ?",
                (version_id,),
            )
        )
        if any(
            path in {self.paths.evaluations.resolve(), self.paths.runtime.resolve()}
            for path in related[1:]
        ):
            raise ValueError("A related artifact cannot use a storage root")
        journal_id = None
        if retention_lineage_id is not None:
            journal_id = new_id()
            now = utc_now()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO retention_journal(
                        id, lineage_id, saved_version_id, operation_id, state,
                        paths_json, expected_bytes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'planned', ?, ?, ?, ?)
                    """,
                    (
                        journal_id,
                        retention_lineage_id,
                        version_id,
                        context.operation_id,
                        json_text([str(path) for path in related]),
                        int(preview["storage_reclaimed_bytes"]),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE version_lineage SET cleanup_status = 'pending'
                    WHERE lineage_id = ? AND saved_version_id = ?
                    """,
                    (retention_lineage_id, version_id),
                )
        staging = self.paths.cache / f"delete-{uuid.uuid4()}"
        staging.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        database_deleted = False
        physically_deleted = False
        try:
            if journal_id:
                self.database.execute(
                    """
                    UPDATE retention_journal
                    SET state = 'moving', updated_at = ? WHERE id = ?
                    """,
                    (utc_now(), journal_id),
                )
            for source in related:
                if not source.exists():
                    continue
                destination = staging / f"{len(moved):03d}-{source.name}"
                source.replace(destination)
                moved.append((source, destination))
            remaining = [str(path) for path in related if path.exists()]
            if remaining:
                raise OSError(
                    "deletion staging verification failed: " + ", ".join(remaining)
                )
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    "DELETE FROM saved_versions WHERE id = ?", (version_id,)
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "saved-version identity disappeared before deletion commit"
                    )
            database_deleted = True
            shutil.rmtree(staging)
            if staging.exists():
                raise OSError(
                    f"physical deletion verification failed: {staging}"
                )
            physically_deleted = True
            if journal_id:
                finished = utc_now()
                self.database.execute(
                    """
                    UPDATE retention_journal
                    SET state = 'deleted', actual_bytes = ?,
                        updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(preview["storage_reclaimed_bytes"]),
                        finished,
                        finished,
                        journal_id,
                    ),
                )
        except Exception as error:
            if not database_deleted:
                for source, destination in reversed(moved):
                    if destination.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        destination.replace(source)
                shutil.rmtree(staging, ignore_errors=True)
            if journal_id:
                failed = utc_now()
                with self.database.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE retention_journal
                        SET state = 'failed', error_json = ?,
                            updated_at = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_text(
                                {
                                    "type": type(error).__name__,
                                    "message": str(error),
                                    "database_identity_deleted": database_deleted,
                                    "physical_files_deleted": physically_deleted,
                                    "recovery_staging_path": (
                                        str(staging) if staging.exists() else None
                                    ),
                                }
                            ),
                            failed,
                            failed,
                            journal_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE version_lineage SET cleanup_status = 'failed'
                        WHERE lineage_id = ? AND saved_version_id = ?
                        """,
                        (retention_lineage_id, version_id),
                    )
            raise
        context.update(phase="Completed", current_progress=1, total_progress=1)
        return {
            "version_id": version_id,
            "files_removed": preview["files_removed"],
            "storage_reclaimed_bytes": preview["storage_reclaimed_bytes"],
            "retention_journal_id": journal_id,
            "physical_deletion_verified": True,
        }



    def activate_version(
        self, version_id: str, request_key: str | None
    ) -> dict[str, Any]:
        self.get_version(version_id)
        self.production_policy.assert_version_activation_allowed(version_id)
        has_valid_evaluation = (
            self._valid_evaluation_evidence(version_id) is not None
        )
        operation_type = (
            "version_activation" if has_valid_evaluation else "evaluation_activation"
        )
        dedupe_key = (
            f"version-activation:{version_id}"
            if has_valid_evaluation
            else f"evaluation-activation:{version_id}"
        )
        worker = (
            (
                lambda context: self._activation_only_worker(version_id, context)
            )
            if has_valid_evaluation
            else (lambda context: self._evaluation_activation_worker(version_id, context))
        )
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                operation_type,
                worker,
                target_id=version_id,
                dedupe_key=dedupe_key,
                success_notification=Notification(
                    "success", "Chat version ready", "Salty Steak is ready in chat."
                ),
                failure_notification=Notification(
                    "error",
                    "Chat version preparation failed",
                    "The previous working version remains active.",
                    None,
                ),
            ),
        )

    def _restore_version_for_chat(
        self, version_id: str, request_key: str | None
    ) -> dict[str, Any]:
        """Cold-chat restore is activation-only; it never starts evaluation."""

        self.get_version(version_id)
        if self._valid_evaluation_evidence(version_id) is None:
            raise ValueError(
                "Chat restore requires an existing completed evaluation; "
                "use Evaluate or Use in chat first."
            )
        return self.activate_version(version_id, request_key)

    def _activation_only_worker(
        self, version_id: str, context: OperationContext
    ) -> dict[str, Any]:
        version = self.get_version(version_id)
        identity = self._activate_worker(
            version_id, context, require_evaluation=True
        )
        evidence = self._valid_evaluation_evidence(version_id)
        if evidence is None:
            raise RuntimeError("Activation lost its required evaluation evidence.")
        self._confirm_runtime_identity(
            version,
            identity,
            expected_evaluation_id=str(evidence["evaluation_id"]),
        )
        return {
            "saved_version_id": version_id,
            "evaluation_id": evidence["evaluation_id"],
            "evaluation_metrics": evidence["metrics"],
            "runtime_identity": identity,
            "active_in_chat": True,
            "completion_outcome": "completed",
        }

    def _evaluation_activation_worker(
        self, version_id: str, context: OperationContext
    ) -> dict[str, Any]:
        """Evaluate first, then activate, as one durable parent operation."""

        context.update(phase="Preparing evaluation")
        try:
            existing = self._wait_for_active_evaluation(version_id, context)
            if existing is not None:
                evaluation = existing
            else:
                evaluation = self._evaluation_worker(version_id, context)
            context.update(phase="Confirming evaluation evidence")
            evidence = self._valid_evaluation_evidence(
                version_id,
                evaluation_id=str(evaluation["evaluation_id"]),
                allow_active_operation_id=context.operation_id,
            )
            if evidence is None:
                raise RuntimeError(
                    "Evaluation report did not pass durable identity checks."
                )
            context.update(phase="Activating evaluated version")
            identity = self._activate_worker(
                version_id, context, require_evaluation=True
            )
            version = self.get_version(version_id)
            self._confirm_runtime_identity(
                version,
                identity,
                expected_evaluation_id=str(evaluation["evaluation_id"]),
            )
            return {
                "saved_version_id": version_id,
                "evaluation_id": evaluation["evaluation_id"],
                "evaluation": evaluation,
                "evaluation_metrics": evidence["metrics"],
                "runtime_identity": identity,
                "active_in_chat": True,
                "completion_outcome": "completed",
            }
        except OperationInterrupted:
            raise
        except BaseException as exc:
            raise OperationNeedsAttention(
                "Evaluation or activation needs attention; the previous Chat runtime remains active.",
                code="evaluation_activation_failed",
                phase="Evaluation and activation needs attention",
                technical_details=f"{type(exc).__name__}: {exc}",
                partial_result={
                    "saved_version_id": version_id,
                    "active_in_chat": False,
                    "completion_outcome": "needs_attention",
                    "post_training_error": {
                        "code": "evaluation_activation_failed",
                        "message": str(exc),
                        "type": type(exc).__name__,
                    },
                },
            ) from exc

    def _wait_for_active_evaluation(
        self, version_id: str, context: OperationContext
    ) -> dict[str, Any] | None:
        """Join an already-running manual evaluation instead of duplicating it."""

        row = self.database.fetch_one(
            """
            SELECT o.id AS operation_id, o.state, e.id AS evaluation_id,
                   e.metrics_json, e.result_path, e.result_checksum
            FROM operations o
            JOIN evaluations e ON e.operation_id = o.id
            WHERE e.saved_version_id = ?
              AND o.state IN ('queued','running','stop_requested')
              AND o.id <> ?
            ORDER BY o.created_at DESC LIMIT 1
            """,
            (version_id, context.operation_id),
        )
        if not row:
            return None
        while True:
            context.raise_if_stop_requested()
            operation = self.operations.get(str(row["operation_id"]))
            if operation is None or operation["state"] in {
                "completed",
                "failed",
                "interrupted",
            }:
                break
            time.sleep(0.05)
        evidence = self._valid_evaluation_evidence(version_id)
        if evidence is None:
            return None
        return {
            "evaluation_id": evidence["evaluation_id"],
            "operation_id": evidence["operation_id"],
            "saved_version_id": version_id,
            "metrics": evidence["metrics"],
            "saved": True,
            "report_readable": True,
        }

    def _activate_worker(
        self,
        version_id: str,
        context: OperationContext,
        *,
        require_evaluation: bool = False,
    ) -> dict[str, Any]:
        version = self.get_version(version_id)
        self.production_policy.assert_version_activation_allowed(version_id)
        if require_evaluation and self._valid_evaluation_evidence(
            version_id,
            allow_active_operation_id=context.operation_id,
        ) is None:
            raise ValueError(
                "Activation requires a completed, identity-matched validation evaluation."
            )
        progress = _Progress(context)
        activation_attempt_id = new_id()
        expected_activation = {
            "saved_version_id": version_id,
            "checkpoint_id": version_id,
            "checkpoint_sha256": version["checksum"],
            "tokenizer_sha256": version["tokenizer_checksum"],
            "architecture_revision": version["architecture_revision"],
            "context_tokens": version["context_tokens"],
            "device": self.config.section("training")["device"],
            "precision": self.config.section("training")["precision"],
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO activation_attempts(
                    id, operation_id, saved_version_id, state,
                    expected_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'preparing', ?, ?, ?)
                """,
                (
                    activation_attempt_id,
                    context.operation_id,
                    version_id,
                    json_text(expected_activation),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute(
                """
                UPDATE training_operations
                SET activation_operation_id = ?, activation_attempt_id = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    context.operation_id,
                    activation_attempt_id,
                    utc_now(),
                    context.operation_id,
                ),
            )

        def commit(identity: RuntimeIdentity) -> None:
            if require_evaluation:
                evidence = self._valid_evaluation_evidence(
                    version_id,
                    allow_active_operation_id=context.operation_id,
                )
                if evidence is None:
                    raise RuntimeError(
                        "activation lost its durable evaluation evidence before commit"
                    )
                self._confirm_runtime_identity(
                    version,
                    identity.to_dict(),
                    expected_evaluation_id=str(evidence["evaluation_id"]),
                )
            runtime_file = Path(identity.artifact_path) / "runtime.json"
            artifact_checksum = sha256_file(runtime_file)
            now = utc_now()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_states(
                        id, operation_id, saved_version_id, artifact_path,
                        artifact_checksum, state, worker_pid, metadata_json,
                        created_at, verified_at, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, 'loaded', ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.runtime_id,
                        context.operation_id,
                        version_id,
                        identity.artifact_path,
                        artifact_checksum,
                        os.getpid(),
                        json_text(
                            {
                                **identity.to_dict(),
                                "activation_attempt_id": activation_attempt_id,
                            }
                        ),
                        now,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO active_runtime(
                        singleton, saved_version_id, runtime_id,
                        activated_by_operation_id, updated_at
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        saved_version_id = excluded.saved_version_id,
                        runtime_id = excluded.runtime_id,
                        activated_by_operation_id = excluded.activated_by_operation_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        version_id,
                        identity.runtime_id,
                        context.operation_id,
                        now,
                    ),
                )
                connection.execute(
                    "DELETE FROM runtime_states WHERE id <> ?",
                    (identity.runtime_id,),
                )
                connection.execute(
                    """
                    UPDATE activation_attempts
                    SET state = 'committed', runtime_id = ?, actual_json = ?,
                        updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        identity.runtime_id,
                        json_text(identity.to_dict()),
                        now,
                        now,
                        activation_attempt_id,
                    ),
                )

        try:
            identity = self.runtime.activate(
                checkpoint_id=version_id,
                checkpoint_path=version["checkpoint_path"],
                label=version["label"],
                total_trained_steps=version["total_trained_steps"],
                context_limit=int(version["context_tokens"]),
                expected_weight_sha256=version["checksum"],
                expected_tokenizer_sha256=str(version["tokenizer_checksum"]),
                expected_architecture_revision=int(version["architecture_revision"]),
                expected_model_config_sha256=(
                    hashlib.sha256(
                        (Path(version["checkpoint_path"]) / "config.json")
                        .read_bytes()
                    ).hexdigest()
                    if (Path(version["checkpoint_path"]) / "config.json").is_file()
                    else None
                ),
                progress=progress,
                commit_active=commit,
            )
        except BaseException as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE activation_attempts
                    SET state = 'failed', error_json = ?, updated_at = ?,
                        finished_at = ?
                    WHERE id = ? AND state <> 'committed'
                    """,
                    (
                        json_text(
                            {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            }
                        ),
                        utc_now(),
                        utc_now(),
                        activation_attempt_id,
                    ),
                )
            raise
        return {
            **identity.to_dict(),
            "activation_attempt_id": activation_attempt_id,
        }



    def identity_post_training_setup(self) -> dict[str, Any]:
        bundle = self.model_bundles.selected_base("text_generation")
        latest = self.operations.list(
            operation_type="training_identity",
            limit=1,
        )
        adapter = next(
            (
                companion
                for companion in (bundle or {}).get("companion_artifacts", [])
                if companion.get("role") == "text_adapter"
            ),
            None,
        )
        routing_adapter = next(
            (
                companion
                for companion in (bundle or {}).get("companion_artifacts", [])
                if companion.get("role") == "routing_adapter"
            ),
            None,
        )
        available = bool(
            bundle
            and bundle.get("display_name") == "Base Steak 2.0"
            and bundle.get("architecture") == "steak20"
            and bundle.get("runtime_family") == "salty_native_steak20"
            and bundle.get("current_size_matches")
            and (
                self.paths.workspace / "runtime" / "salty-native-steak20" / "bin"
            ).is_dir()
        )
        metrics = dict((bundle or {}).get("identity_evaluation_metrics") or {})
        evidence_complete = bool(
            adapter
            and adapter.get("current_size_matches")
            and adapter.get("activation") == "identity_intent"
            and len(str((bundle or {}).get("identity_training_report_sha256") or ""))
            == 64
            and len(
                str(
                    (bundle or {}).get(
                        "identity_free_generation_report_sha256"
                    )
                    or ""
                )
            )
            == 64
            and metrics.get("identity_pass_count") == metrics.get("identity_count")
            and float(metrics.get("retention_exact_baseline_rate") or 0.0) == 1.0
            and metrics.get("identity_route_activation_pass_count")
            == metrics.get("identity_count")
            and metrics.get("retention_route_disable_pass_count")
            == metrics.get("retention_count")
            and metrics.get("retention_output_clean_count")
            == metrics.get("retention_count")
        )
        return {
            "available": available,
            "model_id": bundle.get("id") if bundle else None,
            "model_name": bundle.get("display_name") if bundle else "Base Steak 2.0",
            "trainer": (
                bundle.get("identity_trainer")
                if bundle and bundle.get("identity_trainer")
                else "MD Anik Hasan (Sawlper)"
            ),
            "method": "native_rank_64_output_projection_lora_with_learned_identity_route",
            "hardcoded_response_used": False,
            "training_examples": len(identity_training_examples()),
            "unseen_identity_prompts": len(identity_holdout_examples()),
            "capability_retention_prompts": 40,
            "adapter": adapter,
            "routing_adapter": routing_adapter,
            "routing": {
                "training_examples": int(
                    (bundle or {}).get("routing_training_example_count") or 0
                ),
                "holdout_count": int(
                    (bundle or {}).get("routing_holdout_count") or 0
                ),
                "holdout_pass_count": int(
                    (bundle or {}).get("routing_holdout_pass_count") or 0
                ),
                "per_route_pass_count": dict(
                    (bundle or {}).get("routing_per_route_pass_count") or {}
                ),
                "report_sha256": str(
                    (bundle or {}).get("routing_post_training_report_sha256") or ""
                ),
            },
            "metrics": metrics,
            "evidence_complete": evidence_complete,
            "latest_operation": latest[0] if latest else None,
            "reason": (
                "The selected steak20 weights can run the protected learned-identity recipe."
                if available
                else "Select the verified steak20 Base Steak 2.0 bundle before identity post-training."
            ),
        }

    def training_setup(self) -> dict[str, Any]:
        settings = dict(self.config.section("training"))
        recovery = self.paths.training / "recovery"
        resume_state = None
        recovery_report = inspect_recovery_state(recovery, verify_checksums=False)
        if recovery_report["valid"]:
            manifest = recovery_report["metadata"]
            assert isinstance(manifest, dict)
            prepared = self.database.fetch_one(
                """
                SELECT id, path, artifact_checksum FROM prepared_datasets
                WHERE id = ? AND status = 'ready' AND verified = 1
                """,
                (manifest["prepared_dataset_id"],),
            )
            prepared_matches = bool(
                prepared
                and str(Path(prepared["path"]).resolve())
                == str(Path(manifest["prepared_path"]).resolve())
                and prepared["artifact_checksum"]
                == manifest["prepared_artifact_checksum"]
                and (Path(prepared["path"]) / "manifest.json").is_file()
                and sha256_file(Path(prepared["path"]) / "manifest.json")
                == prepared["artifact_checksum"]
            )
            tokenizer_matches = bool(
                self.tokenizer
                and self.tokenizer.fingerprint == manifest["tokenizer_fingerprint"]
            )
            if prepared_matches and tokenizer_matches:
                resume_state = {
                    "complete": True,
                    "saved_at": utc_timestamp(manifest.get("committed_at"))
                    or utc_timestamp(manifest.get("committed_at_unix")),
                    "path": str(recovery),
                    "prepared_dataset_id": manifest["prepared_dataset_id"],
                    "starting_version_id": manifest.get("starting_version_id"),
                    "remaining_steps": max(
                        0,
                        int(manifest["additional_steps_target"])
                        - int(manifest["additional_steps_completed"]),
                    ),
                }
        return {
            "defaults": {
                "sequence_length": settings["sequence_length"],
                "micro_batch_size": settings["micro_batch_size"],
                "gradient_accumulation": settings["gradient_accumulation"],
                "learning_rate": settings["learning_rate"],
                "scheduler": settings["scheduler"],
                "warmup_steps": settings["warmup_steps"],
                "weight_decay": settings["weight_decay"],
                "gradient_clip": settings["gradient_clip"],
                "precision": settings["precision"],
                "validation_interval": settings["validation_interval"],
                "seed": settings["seed"],
                "device": settings["device"],
            },
            "post_training_policy": "evaluate_and_activate",
            "post_training_policies": [
                {
                    "value": "evaluate_and_activate",
                    "label": "Evaluate and use in Chat",
                    "description": (
                        "Save the verified version, run the full validation "
                        "evaluation, then switch Chat only after the evidence passes."
                    ),
                    "recommended": True,
                },
            ],
            "identity_post_training": self.identity_post_training_setup(),
            "resume_state": resume_state,
            "datasets": [
                record
                for record in self.list_datasets()
                if record.get("training_ready")
            ],
            "starting_versions": [
                record
                for record in self.list_versions()
                if record.get("production_policy", {}).get(
                    "continuation_allowed", True
                )
            ],
        }

    def start_identity_post_training(
        self,
        payload: Mapping[str, Any],
        request_key: str | None,
    ) -> dict[str, Any]:
        setup = self.identity_post_training_setup()
        if not setup["available"]:
            raise ValueError(str(setup["reason"]))
        if payload.get("user_confirmed") is not True:
            raise PermissionError(
                "Identity post-training requires confirmation of the fixed recipe and evaluation gates"
            )
        model_id = str(setup["model_id"])
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "training_identity",
                lambda context: self._identity_post_training_worker(
                    model_id,
                    context,
                ),
                target_id=model_id,
                dedupe_key="training-identity:single-active",
                initial_phase="Checking exact model and recipe",
                initial_details={
                    "model_name": "Base Steak 2.0",
                    "trainer": "MD Anik Hasan (Sawlper)",
                    "hardcoded_response_used": False,
                    "training_examples": len(identity_training_examples()),
                    "unseen_identity_prompts": len(identity_holdout_examples()),
                    "capability_retention_prompts": 40,
                },
                success_notification=Notification(
                    "success",
                    "Learned identity accepted",
                    "The learned adapter and model-driven controller passed identity and exact retention gates and are active in Chat.",
                    12,
                ),
                failure_notification=Notification(
                    "error",
                    "Identity post-training was not promoted",
                    "The current Chat adapter remains unchanged unless every native gate passes.",
                    12,
                ),
            ),
        )

    def _identity_post_training_worker(
        self,
        model_id: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        bundle = self.model_bundles.get(model_id)
        if not (
            bundle.get("architecture") == "steak20"
            and bundle.get("runtime_family") == "salty_native_steak20"
            and bundle.get("current_size_matches")
        ):
            raise ValueError("The selected Base Steak model changed before training began")
        conflicting = [
            operation
            for operation in self.operations.list(
                states=tuple(ACTIVE_OPERATION_STATES),
                limit=100,
            )
            if operation["id"] != context.operation_id
            and operation["type"]
            in {
                "training",
                "evaluation",
                "evaluation_activation",
                "chat_generation",
                "chat_vision_analysis",
                "chat_image_generation",
            }
        ]
        if conflicting:
            raise ValueError(
                "Finish the active Chat, vision, image, training, or evaluation operation first"
            )

        runtime_directory = (
            self.paths.workspace / "runtime" / "salty-native-steak20" / "bin"
        )
        output_directory = (
            self.paths.training / "identity-post-training" / context.operation_id
        )

        def update(
            phase: str,
            current: int,
            total: int,
            details: dict[str, Any],
        ) -> None:
            context.checkpoint(
                phase=phase,
                current_progress=float(current),
                total_progress=float(total),
                details=details,
            )

        with self.chat._bundle_lifecycle_lock:
            previous_runtime = self.model_bundle_runtime
            if previous_runtime is not None:
                context.update(phase="Freeing Chat model for protected training")
                previous_runtime.unload()
            self.model_bundle_runtime = None
            self.chat.model_bundle_runtime = None
            try:
                try:
                    result = run_identity_post_training(
                        model_path=bundle["model_path"],
                        runtime_directory=runtime_directory,
                        model_sha256=str(bundle["checksum"]),
                        output_directory=output_directory,
                        on_progress=update,
                        should_stop=context.stop_requested,
                    )
                except InterruptedError as error:
                    raise OperationInterrupted(str(error)) from error
                context.update(
                    phase="Promoting evaluated identity adapter",
                    current_progress=0,
                    total_progress=1,
                    details={
                        "adapter_sha256": result["adapter"]["sha256"],
                        "all_gates_passed": True,
                    },
                )
                promotion = promote_identity_candidate(
                    bundle=bundle,
                    workflow_result=result,
                    model_library_root=self.paths.workspace / "models",
                    backup_root=(
                        self.paths.training / "identity-post-training" / "rollback"
                    ),
                    operation_id=context.operation_id,
                )
                selected = self.model_bundles.selected_base("text_generation")
                refreshed_runtime = self._runtime_for_model_bundle(selected)
                if refreshed_runtime is None:
                    raise RuntimeError(
                        "Promoted identity adapter did not produce a loadable private runtime"
                    )
                refreshed_runtime.warmup()
                self.model_bundle_runtime = refreshed_runtime
                self.chat.model_bundle_runtime = refreshed_runtime
                self.chat.model_bundle = dict(selected) if selected else None
                context.update(
                    phase="Learned identity active",
                    current_progress=1,
                    total_progress=1,
                    details={
                        "promotion": promotion,
                        "runtime": refreshed_runtime.describe(),
                    },
                )
                return {**result, "promotion": promotion, "active_in_chat": True}
            finally:
                if self.model_bundle_runtime is None:
                    selected = self.model_bundles.selected_base("text_generation")
                    restored_runtime = self._runtime_for_model_bundle(selected)
                    self.model_bundle_runtime = restored_runtime
                    self.chat.model_bundle_runtime = restored_runtime
                    self.chat.model_bundle = dict(selected) if selected else None

    def training_status(self) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """
            SELECT o.* FROM operations o
            LEFT JOIN training_operations t ON t.operation_id = o.id
            LEFT JOIN training_operation_policies policy
              ON policy.training_operation_id = t.id
            WHERE o.type = 'training'
              AND COALESCE(policy.visibility, 'primary') = 'primary'
            ORDER BY o.updated_at DESC, o.created_at DESC
            LIMIT 1
            """
        )
        if not row:
            return None
        record = self.database.operation_record(row)
        training = self.database.fetch_one(
            """
            SELECT post_training_policy, post_training_state,
                   post_training_recovery_state, required_evaluation_id,
                   activation_attempt_id, confirmed_runtime_id,
                   post_training_error_json
            FROM training_operations WHERE operation_id = ?
            """,
            (row["id"],),
        )
        if training:
            record.update(training)
            record["post_training_error"] = parse_json(
                training.pop("post_training_error_json", None), None
            )
        return record

    def training_history(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT o.*,t.id AS training_operation_id,
                   policy.classification AS history_classification,
                   policy.action_policy,policy.display_name
            FROM operations o
            JOIN training_operations t ON t.operation_id=o.id
            LEFT JOIN training_operation_policies policy
              ON policy.training_operation_id=t.id
            WHERE o.type='training'
            ORDER BY o.updated_at DESC,o.created_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )
        records: list[dict[str, Any]] = []
        for row in rows:
            record = self.database.operation_record(row)
            record["training_operation_id"] = row["training_operation_id"]
            record["history_classification"] = (
                row.get("history_classification") or "normal"
            )
            record["action_policy"] = row.get("action_policy") or "normal"
            record["display_name"] = (
                row.get("display_name") or "Training run"
            )
            records.append(record)
        return records

    def start_training(
        self, payload: Mapping[str, Any], request_key: str | None
    ) -> dict[str, Any]:
        normalised_payload = dict(payload)
        if normalised_payload.get("prepared_dataset_id"):
            self.production_policy.assert_prepared_training_allowed(
                str(normalised_payload["prepared_dataset_id"])
            )
        if normalised_payload.get("starting_version_id"):
            self.production_policy.assert_version_continuation_allowed(
                str(normalised_payload["starting_version_id"])
            )
        policy = "evaluate_and_activate"
        normalised_payload["post_training_policy"] = policy
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "training",
                lambda context: self._training_worker(normalised_payload, context),
                target_id=(
                    str(normalised_payload["prepared_dataset_id"])
                    if normalised_payload.get("prepared_dataset_id")
                    else "recovery"
                ),
                dedupe_key="training:single-active",
                success_notification=lambda result: Notification(
                    "success",
                    (
                        "Training, evaluation, and Chat activation completed"
                        if result.get("completion_outcome") == "completed"
                        else "Training and evaluation completed"
                        if result.get("completion_outcome") == "evaluated"
                        else "Model saved and verified"
                    ),
                    (
                        "The new evaluated version is active in Chat."
                        if result.get("completion_outcome") == "completed"
                        else "The evaluation evidence was saved; the current Chat runtime remains active."
                        if result.get("completion_outcome") == "evaluated"
                        else "Evaluation and Chat activation were not requested."
                    ),
                    10,
                ),
                initial_details={
                    "post_training_policy": policy,
                    "post_training_state": "not_started",
                    "post_training_recovery_state": initial_recovery_state(policy),
                },
                failure_notification=Notification(
                    "error",
                    "Training workflow needs attention",
                    (
                        "Review the operation details. Any verified saved version "
                        "remains protected, and Chat changes require the requested "
                        "workflow to complete."
                    ),
                    None,
                ),
            ),
        )

    def retry_training_finalisation(
        self, operation_id: str, request_key: str | None
    ) -> dict[str, Any]:
        """Resume from a preserved checkpoint without repeating training.

        The retry owns a new finalisation attempt, while verification and
        registration remain bound to the original training operation.  A
        known durable post-training policy then continues from its next
        incomplete phase; a legacy-unknown policy stops after registration.
        """

        original = self.operations.get(operation_id)
        training = self.database.fetch_one(
            """
            SELECT t.*, o.state, o.phase
            FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            WHERE t.operation_id = ?
            """,
            (operation_id,),
        )
        if original is None or training is None:
            raise KeyError(f"Training operation does not exist: {operation_id}")
        self.production_policy.assert_training_action_allowed(operation_id)
        if original["state"] not in {"failed", "interrupted"}:
            raise ValueError(
                "Finalisation retry is available only for training that needs attention"
            )
        if training.get("result_version_id"):
            raise ValueError("This training operation already has a saved version")
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "training_finalisation_retry",
                lambda context: self._retry_training_finalisation_worker(
                    operation_id, context
                ),
                target_id=operation_id,
                dedupe_key=f"training-finalisation-retry:{operation_id}",
                initial_phase="Waiting to retry finalisation",
                success_notification=lambda result: Notification(
                    "information",
                    "Finalisation recovered",
                    (
                        "The preserved checkpoint was verified, evaluated, and confirmed active in Chat."
                        if result.get("active_in_chat")
                        else "The preserved checkpoint was verified and registered; no Chat switch was claimed."
                    ),
                    10,
                ),
                failure_notification=Notification(
                    "error",
                    "Finalisation still needs attention",
                    "The checkpoint was preserved and the previous Chat runtime was not changed.",
                    None,
                ),
            ),
        )

    def _retry_training_finalisation_worker(
        self, operation_id: str, context: OperationContext
    ) -> dict[str, Any]:
        context.update(phase="Locating preserved checkpoint")
        training = self.database.fetch_one(
            """
            SELECT * FROM training_operations
            WHERE operation_id = ? AND result_version_id IS NULL
            """,
            (operation_id,),
        )
        if training is None:
            raise ValueError(
                "The training operation is already registered or no longer retryable"
            )
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for candidate in self.paths.versions.iterdir():
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            try:
                manifest = json.loads(
                    (candidate / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                manifest.get("operation_id") == operation_id
                and manifest.get("prepared_dataset_id")
                == training["prepared_dataset_id"]
                and int(manifest.get("additional_steps", -1))
                == int(training["requested_steps"])
            ):
                candidates.append((candidate.resolve(), manifest))
        if len(candidates) != 1:
            raise ValueError(
                "Finalisation retry requires exactly one complete checkpoint owned by this operation"
            )
        checkpoint_path, manifest = candidates[0]
        self.database.append_operation_event(
            operation_id,
            "finalisation_retry_requested",
            state="failed",
            phase="Retrying finalisation",
            evidence={
                "retry_operation_id": context.operation_id,
                "checkpoint_id": checkpoint_path.name,
            },
        )
        context.update(phase="Verifying preserved checkpoint")
        report = inspect_checkpoint(checkpoint_path, calculate_checksum=True)
        if not report.valid or not report.weight_sha256:
            raise ValueError("; ".join(report.errors))
        smoke_result, attempt = self.finalisation.verify(
            operation_id=operation_id,
            checkpoint_path=checkpoint_path,
            context_limit=8_192,
            device="cpu",
        )
        if Path(smoke_result["checkpoint_path"]).resolve() != checkpoint_path:
            raise ValueError(
                "verification worker returned a different checkpoint identity"
            )
        config = ModelConfig.from_file(checkpoint_path / "config.json")
        context.update(phase="Registering preserved checkpoint")
        registration = self._register_verified_training_checkpoint(
            version_id=checkpoint_path.name,
            operation_id=operation_id,
            prepared_dataset_id=training["prepared_dataset_id"],
            checkpoint_path=checkpoint_path,
            total_steps=manifest.get("total_steps"),
            additional_steps=int(manifest["additional_steps"]),
            config=config,
            manifest=manifest,
            weight_sha256=report.weight_sha256,
            verification_attempt_id=attempt["id"],
            recovered_after_restart=True,
        )
        base = {
            "original_training_operation_id": operation_id,
            "saved_version_id": registration["id"],
            "verification_attempt_id": attempt["id"],
            "activation_attempted": False,
            "retention_attempted": False,
        }
        refreshed = self.database.fetch_one(
            "SELECT * FROM training_operations WHERE operation_id = ?",
            (operation_id,),
        )
        if not refreshed:
            raise RuntimeError("registered training ownership disappeared")
        with self.database.transaction() as connection:
            original = connection.execute(
                "SELECT result_json FROM operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            original_result = parse_json(
                original["result_json"] if original else None, {}
            )
            if not isinstance(original_result, dict):
                original_result = {}
            original_result.update(
                {
                    "saved_version_id": registration["id"],
                    "latest_saved_version_label": registration["label"],
                    "checksum": report.weight_sha256,
                    "verification_attempt_id": attempt["id"],
                    "lineage_id": registration.get("lineage_id"),
                    "recovered_after_restart": True,
                    "post_training_policy": refreshed["post_training_policy"],
                }
            )
            connection.execute(
                """
                UPDATE operations
                SET result_json = ?,
                    phase = 'Saved version registered; recovery pending',
                    updated_at = ?, heartbeat_at = ?
                WHERE id = ?
                """,
                (
                    json_text(original_result),
                    utc_now(),
                    utc_now(),
                    operation_id,
                ),
            )
        if refreshed.get("post_training_policy") == "legacy_unknown":
            self._set_post_training_state(
                operation_id,
                state="needs_attention",
                recovery_state="manual_only",
                error={
                    "code": "legacy_post_training_policy_unknown",
                    "message": (
                        "The checkpoint was registered, but the historical "
                        "post-training policy cannot be proved."
                    ),
                },
            )
            return {
                **base,
                "completion_outcome": "saved",
                "post_training_state": "needs_attention",
                "post_training_recovery_state": "manual_only",
            }
        self._set_post_training_state(
            operation_id,
            state="saved",
            recovery_state="resume_pending",
        )
        recovered = self._post_training_recovery_worker(
            operation_id,
            refreshed,
            context,
        )
        return {
            **base,
            **recovered,
            "activation_attempted": bool(recovered.get("active_in_chat")),
            "retention_attempted": True,
        }

    def _resolve_prepared(self, identifier: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT * FROM prepared_datasets WHERE id = ? AND status = 'ready' AND verified = 1",
            (identifier,),
        )
        if row:
            self.production_policy.assert_prepared_training_allowed(
                str(row["id"])
            )
            row["settings"] = parse_json(row.pop("settings_json"))
            return row
        prepared = self.datasets.latest_ready_prepared(identifier)
        if not prepared:
            raise ValueError("Choose a verified prepared dataset")
        self.production_policy.assert_prepared_training_allowed(
            str(prepared["id"])
        )
        return prepared

    def _training_worker(
        self, payload: dict[str, Any], context: OperationContext
    ) -> dict[str, Any]:
        policy = normalise_post_training_policy(payload)
        version_id = new_id()
        output_path = self.paths.versions / version_id
        resume = bool(payload.get("resume_recovery_state"))
        recovery = self.paths.training / "recovery"
        resume_file = recovery / "training_state.pt" if resume else None

        recovery_manifest: dict[str, Any] | None = None
        if resume:
            recovery_report = inspect_recovery_state(
                recovery, verify_checksums=True
            )
            if not recovery_report["valid"]:
                raise ValueError(
                    "No complete exact recovery state exists: "
                    + "; ".join(recovery_report["errors"])
                )
            recovery_manifest = recovery_report["metadata"]
            assert isinstance(recovery_manifest, dict)
            prepared = self._resolve_prepared(
                str(recovery_manifest["prepared_dataset_id"])
            )
            if (
                str(Path(prepared["path"]).resolve())
                != str(Path(recovery_manifest["prepared_path"]).resolve())
                or prepared["artifact_checksum"]
                != recovery_manifest["prepared_artifact_checksum"]
                or prepared["dataset_id"] != recovery_manifest["dataset_id"]
            ):
                raise ValueError(
                    "Prepared data identity differs from the interrupted run"
                )
            prepared_verification = self.datasets.verify_prepared(prepared["id"])
            if not prepared_verification["verified"]:
                raise ValueError("Interrupted training data no longer verifies")
            if not prepared_verification.get("training_eligible", False):
                raise ValueError(
                    "PREPARED_DATA_CONTRACT_LEGACY: exact resume cannot use "
                    "legacy prepared data without durable target labels"
                )
            settings_payload = dict(recovery_manifest["training_settings"])
            additional_steps = int(recovery_manifest["additional_steps_target"])
            completed_before_resume = int(
                recovery_manifest["additional_steps_completed"]
            )
            starting_version_id = recovery_manifest.get("starting_version_id")
            starting_version = None
            if starting_version_id:
                try:
                    candidate = self.get_version(str(starting_version_id))
                except KeyError:
                    candidate = None
                if candidate is not None:
                    if (
                        candidate["checksum"]
                        != recovery_manifest.get("starting_weight_sha256")
                    ):
                        raise ValueError(
                            "Starting saved-version identity changed after interruption"
                        )
                    starting_version = candidate
                    self.production_policy.assert_version_continuation_allowed(
                        str(candidate["id"])
                    )
            base_total_steps = recovery_manifest.get("base_total_steps")
            starting_checkpoint = recovery_manifest.get(
                "starting_checkpoint_path"
            )
            starting_weight_sha256 = recovery_manifest.get(
                "starting_weight_sha256"
            )
        else:
            prepared = self._resolve_prepared(str(payload["prepared_dataset_id"]))
            settings_payload = {
                **dict(self.config.section("training")),
                **dict(payload.get("settings") or {}),
            }
            settings_payload["context_limit"] = int(
                self.config.section("model")["architectural_context_tokens"]
            )
            starting_version = None
            if payload.get("starting_version_id"):
                starting_version = self.get_version(
                    str(payload["starting_version_id"])
                )
                self.production_policy.assert_version_continuation_allowed(
                    str(starting_version["id"])
                )
            if not starting_version and not payload.get("start_from_initial"):
                versions = [
                    version
                    for version in self.list_versions()
                    if version.get("production_policy", {}).get(
                        "continuation_allowed", True
                    )
                ]
                starting_version = versions[0] if versions else None
            if not starting_version:
                raise ValueError(
                    "Choose a verified saved version so this dataset lineage has an exact protected baseline."
                )
            self.production_policy.assert_version_continuation_allowed(
                str(starting_version["id"])
            )
            starting_version_id = (
                starting_version["id"] if starting_version else None
            )
            starting_checkpoint = (
                starting_version["checkpoint_path"] if starting_version else None
            )
            starting_weight_sha256 = (
                starting_version["checksum"] if starting_version else None
            )
            base_total_steps = (
                starting_version["total_trained_steps"]
                if starting_version
                else 0
            )
            additional_steps = int(payload["additional_steps"])
            completed_before_resume = 0

        if additional_steps < 1:
            raise ValueError("Additional training steps must be positive")
        freshness = self.datasets.check_source_changed(
            prepared["dataset_id"], force_checksum=True
        )
        if freshness["changed"]:
            raise ValueError(
                "The source changed; prepare the dataset again before training"
            )
        prepared_verification = self.datasets.verify_prepared(prepared["id"])
        if not prepared_verification["verified"]:
            raise ValueError("The selected prepared dataset failed verification")
        if not prepared_verification.get("training_eligible", False):
            raise ValueError(
                "PREPARED_DATA_CONTRACT_LEGACY: prepare a v2 dataset with "
                "durable labels and provenance before training"
            )
        prepared_sequence_length = int(
            prepared["settings"]["sequence_length"]
        )
        if int(settings_payload["sequence_length"]) != prepared_sequence_length:
            raise ValueError(
                "TRAINING_SEQUENCE_MISMATCH: the training sequence length must "
                f"match prepared data ({prepared_sequence_length})"
            )
        if not resume and recovery.exists():
            raise ValueError(
                "RECOVERY_STATE_REQUIRES_DECISION: an exact recovery state "
                "already exists; resume or preserve it before starting a new run"
            )
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO training_operations(
                    id, operation_id, prepared_dataset_id, starting_version_id,
                    requested_steps, completed_steps, starting_total_steps,
                    settings_json, recovery_path, post_training_policy,
                    post_training_state, post_training_recovery_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id(),
                    context.operation_id,
                    prepared["id"],
                    starting_version["id"] if starting_version else None,
                    additional_steps,
                    completed_before_resume,
                    base_total_steps,
                    json_text(settings_payload),
                    str(recovery),
                    policy,
                    "not_started",
                    initial_recovery_state(policy),
                    now,
                    now,
                ),
            )
        training = TrainingSettings(
            prepared_path=prepared["path"],
            prepared_dataset_id=prepared["id"],
            prepared_artifact_checksum=prepared["artifact_checksum"],
            tokenizer_path=str(self.paths.tokenizer),
            output_path=str(output_path),
            recovery_path=str(recovery),
            additional_steps=additional_steps,
            operation_id=context.operation_id,
            starting_checkpoint=starting_checkpoint,
            starting_version_id=starting_version_id,
            starting_weight_sha256=starting_weight_sha256,
            base_total_steps=base_total_steps,
            dataset_id=prepared["dataset_id"],
            sequence_length=int(settings_payload["sequence_length"]),
            micro_batch_size=int(settings_payload["micro_batch_size"]),
            gradient_accumulation=int(settings_payload["gradient_accumulation"]),
            learning_rate=float(settings_payload["learning_rate"]),
            scheduler=str(settings_payload["scheduler"]),
            warmup_steps=int(settings_payload["warmup_steps"]),
            weight_decay=float(settings_payload["weight_decay"]),
            gradient_clip=float(settings_payload["gradient_clip"]),
            precision=str(settings_payload["precision"]),
            validation_interval=int(settings_payload["validation_interval"]),
            recovery_interval=int(settings_payload["recovery_interval"]),
            seed=int(settings_payload["seed"]),
            device=str(settings_payload["device"]),
            context_limit=int(settings_payload["context_limit"]),
            resume_state=str(resume_file) if resume_file else None,
        )
        def persist_training_progress(
            _phase: str,
            current: int | float | None,
            _total: int | float | None,
            details: dict[str, Any] | None,
        ) -> None:
            completed_value = (
                details.get("current_training_step")
                if details
                else current
            )
            if completed_value is None:
                return
            completed_steps = min(
                additional_steps,
                max(0, int(completed_value)),
            )
            self.database.execute(
                """
                UPDATE training_operations
                SET completed_steps = MAX(completed_steps, ?), updated_at = ?
                WHERE operation_id = ?
                """,
                (completed_steps, utc_now(), context.operation_id),
            )

        try:
            result = run_training(
                training,
                _Progress(context, persist_training_progress),
            )
        except BaseException as exc:
            current_phase = str(context.record().get("phase") or "")
            if current_phase == "Saving version":
                raise OperationPhaseError(
                    "Training steps finished, but the saved version could not be created.",
                    code="saving_version_failed",
                    phase="Saving version failed",
                    technical_details=f"{type(exc).__name__}: {exc}",
                ) from exc
            raise
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE training_operations
                SET completed_steps = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    result.additional_steps_completed,
                    utc_now(),
                    context.operation_id,
                ),
            )
        if result.interrupted or not result.saved_path:
            raise OperationInterrupted("Training stopped safely at a recovery boundary")
        context.update(phase="Verifying version")
        try:
            report = inspect_checkpoint(result.saved_path, calculate_checksum=True)
            if not report.valid or not report.weight_sha256:
                raise RuntimeError("; ".join(report.errors))
            smoke_result, verification_attempt = self.finalisation.verify(
                operation_id=context.operation_id,
                checkpoint_path=result.saved_path,
                context_limit=8_192,
                device="cpu",
            )
            if (
                Path(smoke_result["checkpoint_path"]).resolve()
                != Path(result.saved_path).resolve()
            ):
                raise RuntimeError(
                    "verification worker returned a different checkpoint identity"
                )
            manifest_path = Path(result.saved_path) / "manifest.json"
            raw_manifest = manifest_path.read_text(encoding="utf-8")
            if not raw_manifest.strip():
                raise ValueError("saved-version manifest is empty")
            manifest = json.loads(raw_manifest)
            required_manifest = {
                "format",
                "model_type",
                "weights_sha256",
                "tokenizer_fingerprint",
                "parameter_count",
                "context_tokens",
                "additional_steps",
            }
            missing_manifest = required_manifest - set(manifest)
            if missing_manifest:
                raise ValueError(
                    "saved-version manifest is missing: "
                    + ", ".join(sorted(missing_manifest))
                )
            if manifest.get("operation_id") != context.operation_id:
                raise ValueError(
                    "saved-version manifest operation identity is inconsistent"
                )
            config = ModelConfig.from_file(Path(result.saved_path) / "config.json")
        except BaseException as exc:
            details = (
                exc.technical_details()
                if isinstance(exc, IsolatedWorkerError)
                else f"{type(exc).__name__}: {exc}"
            )
            timed_out_phase = (
                exc.diagnostics.get("timed_out_phase")
                if isinstance(exc, IsolatedWorkerError)
                else None
            )
            raise OperationPhaseError(
                (
                    "Training steps finished, but version finalisation timed out "
                    f"during {str(timed_out_phase).replace('_', ' ')}."
                    if timed_out_phase
                    else "Training steps finished, but the saved version could not be verified."
                ),
                code=(
                    "interpreter_bootstrap_timeout"
                    if timed_out_phase == "interpreter_bootstrap"
                    else "finalisation_timeout"
                    if timed_out_phase
                    else "version_verification_failed"
                ),
                phase=(
                    "Finalisation requires attention"
                    if timed_out_phase
                    else "Version verification failed"
                ),
                technical_details=details,
            ) from exc
        context.update(phase="Registering version")
        try:
            registration = self._register_verified_training_checkpoint(
                version_id=version_id,
                operation_id=context.operation_id,
                prepared_dataset_id=prepared["id"],
                checkpoint_path=Path(result.saved_path),
                total_steps=result.total_trained_steps,
                additional_steps=result.additional_steps_completed,
                config=config,
                manifest=manifest,
                weight_sha256=report.weight_sha256,
                verification_attempt_id=verification_attempt["id"],
            )
        except BaseException as exc:
            raise OperationPhaseError(
                "Training steps finished, but the verified version could not be registered.",
                code="version_registration_failed",
                phase="Version registration failed",
                technical_details=f"{type(exc).__name__}: {exc}",
            ) from exc
        context.update(
            phase="Registering version",
            details={
                "saved_version_id": registration["id"],
                "latest_saved_version_label": registration["label"],
            },
        )
        shutil.rmtree(recovery, ignore_errors=True)
        self._set_post_training_state(
            context.operation_id,
            state="saved",
            recovery_state="active",
        )
        base_result = {
            **result.to_dict(),
            "saved_version_id": registration["id"],
            "latest_saved_version_label": registration["label"],
            "checksum": report.weight_sha256,
            "verification_attempt_id": verification_attempt["id"],
            "lineage_id": registration["lineage_id"],
            "baseline_version_id": registration["baseline_version_id"],
            "parent_version_id": registration["parent_version_id"],
            "post_training_policy": policy,
            "active_in_chat": False,
            "runtime_identity": None,
        }
        if policy == "save_only":
            try:
                self._apply_post_training_retention(context)
            except BaseException as exc:
                return self._post_training_attention(
                    context,
                    base_result,
                    code="post_training_retention_failed",
                    message="The verified version was saved, but retention cleanup needs attention.",
                    exc=exc,
                )
            self._finish_post_training_success(
                context.operation_id,
                state="completed",
                outcome="saved",
                result=base_result,
                context=context,
            )
            return {
                **base_result,
                "completion_outcome": "saved",
                "post_training_state": "completed",
                "post_training_recovery_state": "complete",
            }

        evaluation_result: dict[str, Any] | None = None
        try:
            self._set_post_training_state(
                context.operation_id,
                state="evaluating",
                recovery_state="active",
            )
            context.update(phase="Preparing evaluation")
            evaluation_result = self._evaluation_worker(registration["id"], context)
            evaluation_id = str(evaluation_result["evaluation_id"])
            context.update(phase="Confirming evaluation evidence")
            evidence = self._valid_evaluation_evidence(
                registration["id"],
                evaluation_id=evaluation_id,
                allow_active_operation_id=context.operation_id,
            )
            if evidence is None:
                raise RuntimeError(
                    "Evaluation completed without durable identity, checksum, "
                    "and finite validation evidence."
                )
            self._set_post_training_state(
                context.operation_id,
                state="evaluated",
                recovery_state="active",
                evaluation_id=evaluation_id,
            )
            base_result.update(
                {
                    "evaluation_id": evaluation_id,
                    "evaluation": evaluation_result,
                    "evaluation_metrics": evidence["metrics"],
                }
            )
        except OperationInterrupted:
            self._set_post_training_state(
                context.operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error={"code": "evaluation_interrupted"},
            )
            raise
        except BaseException as exc:
            error = {
                "code": "post_training_evaluation_failed",
                "message": str(exc),
                "type": type(exc).__name__,
            }
            self._set_post_training_state(
                context.operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error=error,
            )
            raise OperationNeedsAttention(
                "The verified saved version was created, but evaluation did not complete.",
                code="post_training_evaluation_failed",
                phase="Post-training evaluation needs attention",
                technical_details=f"{type(exc).__name__}: {exc}",
                partial_result={
                    **base_result,
                    "completion_outcome": "needs_attention",
                    "post_training_state": "needs_attention",
                    "post_training_recovery_state": "resume_pending",
                    "post_training_error": error,
                },
            ) from exc

        if policy == "evaluate":
            try:
                self._apply_post_training_retention(context)
            except BaseException as exc:
                return self._post_training_attention(
                    context,
                    base_result,
                    code="post_training_retention_failed",
                    message="Evaluation passed, but retention cleanup needs attention.",
                    exc=exc,
                )
            self._finish_post_training_success(
                context.operation_id,
                state="completed",
                outcome="evaluated",
                result=base_result,
                context=context,
            )
            return {
                **base_result,
                "completion_outcome": "evaluated",
                "post_training_state": "completed",
                "post_training_recovery_state": "complete",
            }

        activation_result: dict[str, Any] | None = None
        try:
            context.update(phase="Selecting saved version")
            self._set_post_training_state(
                context.operation_id,
                state="activating",
                recovery_state="active",
            )
            context.update(phase="Activating evaluated version")
            activation_result = self._activate_worker(
                registration["id"], context, require_evaluation=True
            )
            self._confirm_runtime_identity(
                self.get_version(str(registration["id"])),
                activation_result,
                expected_evaluation_id=str(base_result["evaluation_id"]),
            )
            self._set_post_training_state(
                context.operation_id,
                state="completed",
                recovery_state="active",
                runtime_id=str(activation_result["runtime_id"]),
            )
        except OperationInterrupted:
            self._set_post_training_state(
                context.operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error={"code": "activation_interrupted"},
            )
            raise
        except BaseException as exc:
            error = {
                "code": "post_training_activation_failed",
                "message": str(exc),
                "type": type(exc).__name__,
            }
            self._set_post_training_state(
                context.operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error=error,
            )
            return self._post_training_attention(
                context,
                {
                    **base_result,
                    "evaluation_id": base_result.get("evaluation_id"),
                    "active_in_chat": False,
                    "runtime_identity": None,
                },
                code="post_training_activation_failed",
                message="Evaluation passed, but the Chat runtime was not switched.",
                exc=exc,
            )

        base_result.update(
            {
                "active_in_chat": True,
                "runtime_identity": activation_result,
            }
        )
        try:
            self._apply_post_training_retention(context)
        except BaseException as exc:
            return self._post_training_attention(
                context,
                base_result,
                code="post_training_retention_failed",
                message="The evaluated version is active, but retention cleanup needs attention.",
                exc=exc,
            )
        self._finish_post_training_success(
            context.operation_id,
            state="completed",
            outcome="completed",
            result=base_result,
            context=context,
        )
        return {
            **base_result,
            "completion_outcome": "completed",
            "post_training_state": "completed",
            "post_training_recovery_state": "complete",
        }

    def _set_post_training_state(
        self,
        operation_id: str,
        *,
        state: str,
        recovery_state: str,
        evaluation_id: str | None = None,
        runtime_id: str | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist one monotonic post-training transition."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE training_operations
                SET post_training_state = ?,
                    post_training_recovery_state = ?,
                    required_evaluation_id = COALESCE(?, required_evaluation_id),
                    confirmed_runtime_id = COALESCE(?, confirmed_runtime_id),
                    post_training_error_json = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    state,
                    recovery_state,
                    evaluation_id,
                    runtime_id,
                    json_text(error) if error is not None else None,
                    utc_now(),
                    operation_id,
                ),
            )

    def _apply_post_training_retention(
        self,
        context: OperationContext,
        *,
        owner_operation_id: str | None = None,
    ) -> dict[str, Any]:
        owner_id = owner_operation_id or context.operation_id
        completed = self.database.fetch_one(
            """
            SELECT evidence_json FROM operation_events
            WHERE operation_id = ?
              AND event_type = 'post_training_retention_completed'
            ORDER BY sequence DESC LIMIT 1
            """,
            (owner_id,),
        )
        if completed:
            evidence = parse_json(completed.get("evidence_json"), {})
            summary = (
                dict(evidence.get("retention") or {})
                if isinstance(evidence, dict)
                else {}
            )
            summary["completed"] = True
            summary["reused_durable_attempt"] = True
            context.update(
                phase="Applying retention",
                details={
                    "retention_completed": True,
                    "retention_attempt_id": (
                        evidence.get("retention_attempt_id")
                        if isinstance(evidence, dict)
                        else None
                    ),
                    "retention": summary,
                },
            )
            return summary

        attempt_id = new_id()
        context.update(phase="Applying retention")
        self.database.append_operation_event(
            owner_id,
            "post_training_retention_started",
            state="running",
            phase="Applying retention",
            evidence={
                "retention_attempt_id": attempt_id,
                "executed_by_operation_id": context.operation_id,
            },
        )
        try:
            summary = self._rotate_versions()
        except BaseException as exc:
            self.database.append_operation_event(
                owner_id,
                "post_training_retention_failed",
                state="failed",
                phase="Applying retention",
                evidence={
                    "retention_attempt_id": attempt_id,
                    "executed_by_operation_id": context.operation_id,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
            )
            raise
        self.database.append_operation_event(
            owner_id,
            "post_training_retention_completed",
            state="completed",
            phase="Applying retention",
            evidence={
                "retention_attempt_id": attempt_id,
                "executed_by_operation_id": context.operation_id,
                "retention": summary,
            },
        )
        context.update(
            phase="Applying retention",
            details={
                "retention_completed": True,
                "retention_attempt_id": attempt_id,
                "retention": summary,
            },
        )
        return summary

    def _finish_post_training_success(
        self,
        operation_id: str,
        *,
        state: str,
        outcome: str,
        result: Mapping[str, Any],
        context: OperationContext,
    ) -> None:
        current = context.record()
        persisted = (
            dict(current["result"])
            if isinstance(current.get("result"), dict)
            else {}
        )
        persisted.pop("queue_reason", None)
        final_result = {
            **persisted,
            **dict(result),
            "completion_outcome": outcome,
            "post_training_state": state,
            "post_training_recovery_state": "complete",
        }
        try:
            self._validate_training_completion_result(
                final_result,
                operation_id=operation_id,
            )
        except BaseException as exc:
            self._post_training_attention(
                context,
                final_result,
                code="post_training_completion_evidence_invalid",
                message=(
                    "The saved version remains protected, but the requested "
                    "post-training completion evidence is incomplete."
                ),
                exc=exc,
            )
        self._set_post_training_state(
            operation_id,
            state=state,
            recovery_state="complete",
        )
        context.update(
            phase="Completed",
            current_progress=1,
            total_progress=1,
            result=final_result,
        )

    def _validate_training_completion_result(
        self,
        result: Mapping[str, Any],
        *,
        operation_id: str,
    ) -> None:
        """Reject a terminal success that lacks the requested durable proof."""

        policy = str(result.get("post_training_policy") or "")
        outcome = str(result.get("completion_outcome") or "")
        if outcome != completion_outcome_for_policy(policy):
            raise ValueError("post-training outcome does not match the saved policy")
        version_id = str(result.get("saved_version_id") or "")
        if not version_id:
            raise ValueError("terminal training result is missing a saved version")
        version = self.get_version(version_id)
        if (
            version.get("integrity") != "verified"
            or result.get("checksum") != version.get("checksum")
        ):
            raise ValueError("terminal training result lacks exact checkpoint proof")
        if result.get("retention_completed") is not True:
            raise ValueError("retention completion was not durably recorded")
        retention_owner_id = str(
            result.get("original_training_operation_id") or operation_id
        )
        retention_event = self.database.fetch_one(
            """
            SELECT evidence_json
            FROM operation_events
            WHERE operation_id = ?
              AND event_type = 'post_training_retention_completed'
              AND state = 'completed'
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (retention_owner_id,),
        )
        if retention_event is None:
            raise ValueError("terminal training result lacks durable retention evidence")
        retention_evidence = parse_json(
            retention_event.get("evidence_json"), {}
        )
        if (
            not isinstance(retention_evidence, dict)
            or not retention_evidence.get("retention_attempt_id")
            or not isinstance(retention_evidence.get("retention"), dict)
            or retention_evidence["retention"].get("completed") is not True
        ):
            raise ValueError("terminal training retention evidence is incomplete")

        if policy == "save_only":
            if result.get("evaluation_id") or result.get("active_in_chat"):
                raise ValueError("save-only result contains unrequested activation evidence")
            return

        evaluation_id = str(result.get("evaluation_id") or "")
        evidence = self._valid_evaluation_evidence(
            version_id,
            evaluation_id=evaluation_id or None,
            allow_active_operation_id=operation_id,
        )
        if evidence is None or evidence["evaluation_id"] != evaluation_id:
            raise ValueError("terminal training result lacks exact evaluation evidence")
        if policy == "evaluate":
            if result.get("active_in_chat") or result.get("runtime_identity"):
                raise ValueError("evaluate-only result unexpectedly claims Chat activation")
            return

        identity = result.get("runtime_identity")
        if not result.get("active_in_chat") or not isinstance(identity, Mapping):
            raise ValueError("terminal training result lacks Chat runtime evidence")
        self._confirm_runtime_identity(
            version,
            identity,
            expected_evaluation_id=evaluation_id,
        )

    def _post_training_attention(
        self,
        context: OperationContext,
        result: Mapping[str, Any],
        *,
        code: str,
        message: str,
        exc: BaseException,
    ) -> None:
        error = {
            "code": code,
            "message": str(exc),
            "type": type(exc).__name__,
        }
        self._set_post_training_state(
            context.operation_id,
            state="needs_attention",
            recovery_state="resume_pending",
            error=error,
        )
        raise OperationNeedsAttention(
            message,
            code=code,
            phase="Post-training workflow needs attention",
            technical_details=f"{type(exc).__name__}: {exc}",
            partial_result={
                **dict(result),
                "completion_outcome": "needs_attention",
                "post_training_state": "needs_attention",
                "post_training_recovery_state": "resume_pending",
                "post_training_error": error,
            },
        ) from exc

    def _evaluation_identity(
        self,
        *,
        prepared_dataset_id: str,
        prepared_artifact_checksum: str,
        tokenizer_checksum: str,
        context_tokens: int,
    ) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    str(prepared_dataset_id),
                    str(prepared_artifact_checksum),
                    str(tokenizer_checksum),
                    str(context_tokens),
                    str(self.config.section("training")["sequence_length"]),
                    "validation",
                    "token_natural_log_cross_entropy",
                )
            ).encode("utf-8")
        ).hexdigest()

    def _valid_evaluation_evidence(
        self,
        version_id: str,
        *,
        evaluation_id: str | None = None,
        allow_active_operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return only a completed report that proves exact version identity."""

        version = self.get_version(version_id)
        where = "e.saved_version_id = ?"
        parameters: list[Any] = [version_id]
        if evaluation_id:
            where += " AND e.id = ?"
            parameters.append(evaluation_id)
        rows = self.database.fetch_all(
            f"""
            SELECT e.*, o.state AS operation_state
            FROM evaluations e
            JOIN operations o ON o.id = e.operation_id
            WHERE {where}
            ORDER BY e.finished_at DESC, e.created_at DESC
            """,
            parameters,
        )
        for row in rows:
            if row.get("status") != "completed":
                continue
            report_path = Path(str(row.get("result_path") or ""))
            if (
                not report_path.is_file()
                or not row.get("result_checksum")
                or sha256_file(report_path) != row["result_checksum"]
            ):
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue





            checkpoint_id = report.get("checkpoint_id")
            if (
                report.get("operation_id") != row["operation_id"]
                or report.get("saved_version_id") != version_id
                or (checkpoint_id is not None and checkpoint_id != version_id)
                or report.get("state") != "completed"
                or report.get("weight_sha256") != version["checksum"]
            ):
                continue
            metrics = parse_json(row.get("metrics_json"), {})
            if not isinstance(metrics, dict):
                continue
            try:
                loss = float(metrics["loss"])
                perplexity = float(metrics["perplexity"])
                records = int(metrics["records_evaluated"])
                tokens = int(metrics["tokens_evaluated"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not all(math.isfinite(value) for value in (loss, perplexity))
                or records <= 0
                or tokens <= 0
            ):
                continue
            try:
                report_loss = float(report["mean_loss"])
                report_perplexity = float(report["perplexity"])
                report_records = int(
                    report.get("records_completed", report["total_records"])
                )
                report_tokens = int(report["tokens_evaluated"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(report_loss)
                or not math.isfinite(report_perplexity)
                or abs(report_loss - loss) > 1e-6
                or abs(report_perplexity - perplexity) > 1e-5
                or report_records != records
                or report_tokens != tokens
            ):
                continue
            prepared_id = metrics.get("prepared_dataset_id") or version.get(
                "prepared_dataset_id"
            )
            prepared = (
                self.database.fetch_one(
                    """
                    SELECT id, artifact_checksum, status, verified
                    FROM prepared_datasets WHERE id = ?
                    """,
                    (prepared_id,),
                )
                if prepared_id
                else None
            )
            if not prepared or prepared["status"] != "ready" or not prepared["verified"]:
                continue
            if metrics.get("format") == "salty-potato-evaluation-v3":
                from app.backend.evaluation.contract import (
                    evaluation_contract_digest_is_valid,
                )

                report_contract = report.get("evaluation_contract")
                expected_identity = (
                    report_contract.get("contract_digest")
                    if isinstance(report_contract, dict)
                    and evaluation_contract_digest_is_valid(report_contract)
                    else None
                )
                if (
                    not expected_identity
                    or report.get("evaluation_contract_digest")
                    != expected_identity
                    or report_contract
                    != metrics.get("evaluation_contract")
                    or metrics.get("evaluation_contract_digest")
                    != expected_identity
                ):
                    continue
            else:
                expected_identity = self._evaluation_identity(
                    prepared_dataset_id=str(prepared["id"]),
                    prepared_artifact_checksum=str(prepared["artifact_checksum"]),
                    tokenizer_checksum=str(version["tokenizer_checksum"]),
                    context_tokens=int(version["context_tokens"]),
                )
            if metrics.get("evaluation_identity") != expected_identity:
                continue
            return {
                "evaluation_id": row["id"],
                "operation_id": row["operation_id"],
                "metrics": metrics,
                "report_path": str(report_path),
                "report_checksum": row["result_checksum"],
            }
        return None

    def _confirm_runtime_identity(
        self,
        version: Mapping[str, Any],
        identity: Mapping[str, Any],
        *,
        expected_evaluation_id: str,
    ) -> None:
        current = self.runtime.identity
        config_path = Path(str(version["checkpoint_path"])) / "config.json"
        expected_config_sha = (
            hashlib.sha256(config_path.read_bytes()).hexdigest()
            if config_path.is_file()
            else None
        )
        expected_precision = str(self.config.section("training")["precision"])
        expected_device = str(self.config.section("training")["device"])
        if (
            current is None
            or current.runtime_id != identity.get("runtime_id")
            or identity.get("checkpoint_id") != version["id"]
            or identity.get("weight_sha256") != version["checksum"]
            or int(identity.get("context_limit") or 0)
            != int(version["context_tokens"])
            or current.checkpoint_id != version["id"]
            or current.weight_sha256 != version["checksum"]
            or current.context_limit != int(version["context_tokens"])
            or not current.device
            or not current.precision
            or (
                expected_device == "cuda"
                and not current.device.startswith("cuda")
            )
            or (
                expected_device == "cpu"
                and not current.device.startswith("cpu")
            )
            or current.precision != expected_precision
            or current.tokenizer_sha256 != version.get("tokenizer_checksum")
            or current.architecture_revision
            != int(version["architecture_revision"])
            or (
                expected_config_sha is not None
                and current.model_config_sha256 != expected_config_sha
            )
        ):
            raise RuntimeError(
                "runtime identity did not confirm the exact evaluated saved version"
            )
        if not expected_evaluation_id:
            raise RuntimeError("activation is missing its required evaluation identity")

    def _next_imported_number(self) -> int:
        row = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM saved_versions WHERE imported = 1 OR total_trained_steps IS NULL"
        )
        return int((row or {}).get("count", 0)) + 1

    def _ensure_lineage_for_registration(
        self,
        connection: Any,
        *,
        training_row: Mapping[str, Any],
        prepared_dataset_id: str,
        config: ModelConfig,
        tokenizer_fingerprint: str,
        saved_version_id: str,
        verification_attempt_id: str,
        recovered_after_restart: bool,
        created_at: str,
    ) -> dict[str, Any]:
        prepared = connection.execute(
            """
            SELECT p.id, p.dataset_id, p.source_checksum, p.artifact_checksum
            FROM prepared_datasets p WHERE p.id = ?
            """,
            (prepared_dataset_id,),
        ).fetchone()
        if prepared is None:
            raise ValueError("prepared dataset lineage identity is missing")
        parent_version_id = training_row["starting_version_id"]
        if not parent_version_id:
            raise ValueError("an exact saved-version baseline is required")
        architecture_identity = (
            f"salty_potato:r{config.architecture_version}:"
            f"context-{config.max_position_embeddings}"
        )
        inherited = connection.execute(
            """
            SELECT l.*
            FROM version_lineage vl
            JOIN dataset_lineages l ON l.id = vl.lineage_id
            WHERE vl.saved_version_id = ? AND vl.role = 'trained'
              AND l.dataset_fingerprint = ?
              AND l.prepared_fingerprint = ?
              AND l.tokenizer_fingerprint = ?
              AND l.architecture_identity = ?
            ORDER BY vl.created_at DESC LIMIT 1
            """,
            (
                parent_version_id,
                prepared["source_checksum"],
                prepared["artifact_checksum"],
                tokenizer_fingerprint,
                architecture_identity,
            ),
        ).fetchone()
        if inherited is not None:
            lineage_id = inherited["id"]
            baseline_version_id = inherited["baseline_version_id"]
        else:
            baseline_version_id = parent_version_id
            identity_text = "\0".join(
                (
                    prepared["source_checksum"],
                    prepared["artifact_checksum"],
                    tokenizer_fingerprint,
                    architecture_identity,
                    baseline_version_id,
                )
            )
            lineage_id = "lin-" + hashlib.sha256(
                identity_text.encode("utf-8")
            ).hexdigest()[:24]
            connection.execute(
                """
                INSERT OR IGNORE INTO dataset_lineages(
                    id, dataset_id, prepared_dataset_id, dataset_fingerprint,
                    prepared_fingerprint, tokenizer_fingerprint,
                    architecture_identity, baseline_version_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lineage_id,
                    prepared["dataset_id"],
                    prepared["id"],
                    prepared["source_checksum"],
                    prepared["artifact_checksum"],
                    tokenizer_fingerprint,
                    architecture_identity,
                    baseline_version_id,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO version_lineage(
                    saved_version_id, lineage_id, parent_version_id, role,
                    verification_attempt_id, recovered_after_restart,
                    user_protected, cleanup_status, created_at
                ) VALUES (?, ?, NULL, 'baseline', NULL, 0, 1, 'retained', ?)
                """,
                (baseline_version_id, lineage_id, created_at),
            )
        connection.execute(
            """
            INSERT INTO version_lineage(
                saved_version_id, lineage_id, parent_version_id, role,
                verification_attempt_id, recovered_after_restart,
                user_protected, cleanup_status, created_at
            ) VALUES (?, ?, ?, 'trained', ?, ?, 0, 'retained', ?)
            """,
            (
                saved_version_id,
                lineage_id,
                parent_version_id,
                verification_attempt_id,
                1 if recovered_after_restart else 0,
                created_at,
            ),
        )
        return {
            "lineage_id": lineage_id,
            "baseline_version_id": baseline_version_id,
            "parent_version_id": parent_version_id,
        }

    def _register_verified_training_checkpoint(
        self,
        *,
        version_id: str,
        operation_id: str,
        prepared_dataset_id: str | None,
        checkpoint_path: Path,
        total_steps: int | None,
        additional_steps: int,
        config: ModelConfig,
        manifest: dict[str, Any],
        weight_sha256: str,
        verification_attempt_id: str,
        recovered_after_restart: bool = False,
    ) -> dict[str, Any]:
        checkpoint_path = checkpoint_path.resolve()
        self.finalisation.require_passed(
            attempt_id=verification_attempt_id,
            operation_id=operation_id,
            checkpoint_path=checkpoint_path,
        )
        training_row = self.database.fetch_one(
            """
            SELECT id, result_version_id, starting_version_id
            FROM training_operations
            WHERE operation_id = ?
            """,
            (operation_id,),
        )
        if not training_row:
            raise ValueError("training registration row is missing")
        existing = self.database.fetch_one(
            "SELECT id, label FROM saved_versions WHERE checkpoint_path = ?",
            (str(checkpoint_path),),
        )
        if existing:
            lineage = self.database.fetch_one(
                """
                SELECT vl.lineage_id, vl.parent_version_id,
                       dl.baseline_version_id
                FROM version_lineage vl
                JOIN dataset_lineages dl ON dl.id = vl.lineage_id
                WHERE vl.saved_version_id = ?
                ORDER BY vl.created_at DESC LIMIT 1
                """,
                (existing["id"],),
            )
            return {**existing, **(lineage or {})}
        label = (
            f"After +{additional_steps:,} steps · {total_steps:,} total"
            if total_steps is not None
            else f"After +{additional_steps:,} steps · total unknown"
        )
        created = utc_now()
        with self.database.transaction() as connection:
            current_attempt = connection.execute(
                """
                SELECT 1 FROM finalisation_attempts
                WHERE id = ? AND operation_id = ? AND checkpoint_path = ?
                  AND result_status = 'passed' AND invalidated_at IS NULL
                """,
                (
                    verification_attempt_id,
                    operation_id,
                    str(checkpoint_path),
                ),
            ).fetchone()
            if current_attempt is None:
                raise ValueError(
                    "registration lost ownership of the verification attempt"
                )
            connection.execute(
                """
                INSERT INTO saved_versions(
                    id, training_operation_id, prepared_dataset_id, label,
                    checkpoint_path, checksum, size_bytes, total_trained_steps,
                    additional_steps, architecture_revision, context_tokens,
                    tokenizer_checksum, integrity, imported, created_at, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', 0, ?, ?)
                """,
                (
                    version_id,
                    training_row["id"],
                    prepared_dataset_id,
                    label,
                    str(checkpoint_path),
                    weight_sha256,
                    _directory_size(checkpoint_path),
                    total_steps,
                    additional_steps,
                    config.architecture_version,
                    config.max_position_embeddings,
                    manifest["tokenizer_fingerprint"],
                    created,
                    created,
                ),
            )
            lineage = self._ensure_lineage_for_registration(
                connection,
                training_row=training_row,
                prepared_dataset_id=str(prepared_dataset_id),
                config=config,
                tokenizer_fingerprint=str(manifest["tokenizer_fingerprint"]),
                saved_version_id=version_id,
                verification_attempt_id=verification_attempt_id,
                recovered_after_restart=recovered_after_restart,
                created_at=created,
            )
            connection.execute(
                """
                UPDATE training_operations
                SET result_version_id = ?, completed_steps = MAX(completed_steps, ?),
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (version_id, additional_steps, created, operation_id),
            )
        return {"id": version_id, "label": label, **lineage}

    def _reconcile_completed_checkpoints(self) -> None:
        """Surface preserved checkpoints without inventing terminal success."""

        registered_paths = {
            str(Path(row["checkpoint_path"]).resolve())
            for row in self.database.fetch_all(
                "SELECT checkpoint_path FROM saved_versions"
            )
        }
        registered_incomplete = self.database.fetch_all(
            """
            SELECT o.id AS operation_id, o.state, o.result_json, o.error_json,
                   t.result_version_id, t.post_training_policy,
                   t.post_training_recovery_state,
                   v.checkpoint_path, v.checksum
            FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            JOIN saved_versions v ON v.id = t.result_version_id
            LEFT JOIN training_operation_policies policy
              ON policy.training_operation_id=t.id
            WHERE o.state <> 'completed'
              AND COALESCE(policy.action_policy,'normal') <> 'forensic_read_only'
            """
        )
        for row in registered_incomplete:



            self._mark_finalisation_needs_attention(
                row["operation_id"],
                "A saved version exists; post-training completion requires an explicit recovery action.",
            )
            if (
                row.get("post_training_policy")
                in {"save_only", "evaluate", "evaluate_and_activate"}
                and row.get("post_training_recovery_state") != "manual_only"
            ):
                self._set_post_training_state(
                    row["operation_id"],
                    state="needs_attention",
                    recovery_state="resume_pending",
                    error={
                        "code": "post_training_recovery_pending",
                        "message": "Evaluation and activation were not proven after restart.",
                    },
                )

        for candidate in self.paths.versions.iterdir():
            if (
                not candidate.is_dir()
                or candidate.name.startswith(".")
                or str(candidate.resolve()) in registered_paths
            ):
                continue
            operation_id: str | None = None
            try:
                uuid.UUID(candidate.name)
                manifest_path = candidate / "manifest.json"
                raw_manifest = manifest_path.read_text(encoding="utf-8")
                if not raw_manifest.strip():
                    continue
                manifest = json.loads(raw_manifest)
                operation_id = manifest.get("operation_id")
                if not isinstance(operation_id, str) or not operation_id:
                    continue
                training = self.database.fetch_one(
                    """
                    SELECT t.*, o.state AS operation_state
                    FROM training_operations t
                    JOIN operations o ON o.id = t.operation_id
                    WHERE t.operation_id = ? AND t.result_version_id IS NULL
                    """,
                    (operation_id,),
                )
                if (
                    not training
                    or manifest.get("prepared_dataset_id")
                    != training["prepared_dataset_id"]
                    or int(manifest.get("additional_steps", -1))
                    != int(training["requested_steps"])
                ):
                    continue
                attempt = self.database.fetch_one(
                    """
                    SELECT * FROM finalisation_attempts
                    WHERE operation_id = ? AND checkpoint_path = ?
                      AND result_status = 'passed' AND invalidated_at IS NULL
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (operation_id, str(candidate.resolve())),
                )
                if not attempt:
                    self._mark_finalisation_needs_attention(
                        operation_id,
                        "A complete checkpoint folder exists, but no current passed verification attempt permits registration.",
                    )
                    continue
                self._mark_finalisation_needs_attention(
                    operation_id,
                    "A preserved checkpoint requires an explicit post-training recovery action before registration is considered complete.",
                )
            except (OSError, ValueError, KeyError, TypeError, IsolatedWorkerError):
                if isinstance(operation_id, str):
                    self._mark_finalisation_needs_attention(
                        operation_id,
                        "Restart reconciliation could not prove this checkpoint safely.",
                    )

    def _mark_finalisation_needs_attention(
        self, operation_id: str, reason: str
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            exists = connection.execute(
                """
                SELECT 1 FROM operation_events
                WHERE operation_id = ? AND event_type = 'finalisation_needs_attention'
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if exists:
                return
            connection.execute(
                """
                UPDATE operations
                SET phase = 'Finalisation requires attention',
                    heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND state IN ('failed','interrupted')
                """,
                (now, now, operation_id),
            )
            self.database.append_operation_event(
                operation_id,
                "finalisation_needs_attention",
                state="failed",
                phase="Finalisation requires attention",
                evidence={"reason": reason},
                connection=connection,
                created_at=now,
            )

    def _reconcile_post_training_workflows(self) -> None:
        """Make interrupted post-training work explicit and resumable.

        No historical/unknown-policy row is started automatically.  New
        policy-bearing rows retain their saved checkpoint and are marked for
        an idempotent user-visible recovery operation.
        """

        rows = self.database.fetch_all(
            """
            SELECT t.operation_id, t.result_version_id, t.post_training_policy,
                   t.post_training_state, t.post_training_recovery_state,
                   o.state AS operation_state
            FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            LEFT JOIN training_operation_policies policy
              ON policy.training_operation_id=t.id
            WHERE t.result_version_id IS NOT NULL
              AND t.post_training_policy <> 'legacy_unknown'
              AND o.state IN ('interrupted','failed')
              AND t.post_training_recovery_state IN ('active','resume_pending')
              AND COALESCE(policy.action_policy,'normal') <> 'forensic_read_only'
            """
        )
        for row in rows:
            error = {
                "code": "post_training_recovery_pending",
                "message": (
                    "The saved version is durable, but post-training evaluation "
                    "or activation was interrupted and needs an explicit resume."
                ),
            }
            self._set_post_training_state(
                row["operation_id"],
                state="needs_attention",
                recovery_state="resume_pending",
                error=error,
            )

    def resume_post_training(
        self, operation_id: str, request_key: str | None
    ) -> dict[str, Any]:
        """Resume evaluation/activation for one already-saved training result."""

        training = self.database.fetch_one(
            """
            SELECT t.*, o.state AS operation_state
            FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            WHERE t.operation_id = ?
            """,
            (operation_id,),
        )
        if not training or not training.get("result_version_id"):
            raise ValueError("No durable saved version is available to resume")
        self.production_policy.assert_training_action_allowed(operation_id)
        self.production_policy.assert_version_activation_allowed(
            str(training["result_version_id"])
        )
        policy = str(training.get("post_training_policy") or "legacy_unknown")
        if policy == "legacy_unknown":
            raise ValueError(
                "This historical training run has no trustworthy post-training policy; "
                "choose an explicit evaluation or activation action."
            )
        if training.get("post_training_recovery_state") != "resume_pending":
            raise ValueError("This post-training workflow does not need a resume")
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "post_training_recovery",
                lambda context: self._post_training_recovery_worker(
                    operation_id, training, context
                ),
                target_id=str(training["result_version_id"]),
                dedupe_key=f"post-training-recovery:{operation_id}",
                initial_phase="Waiting to resume post-training workflow",
                initial_details={
                    "original_training_operation_id": operation_id,
                    "post_training_policy": policy,
                },
                success_notification=lambda result: Notification(
                    "success",
                    (
                        "Evaluation and Chat activation recovered"
                        if result.get("completion_outcome") == "completed"
                        else "Evaluation recovered"
                        if result.get("completion_outcome") == "evaluated"
                        else "Saved-version workflow recovered"
                    ),
                    (
                        "The evaluated version is now confirmed active in Chat."
                        if result.get("completion_outcome") == "completed"
                        else "Evaluation evidence is complete; the Chat runtime was not changed."
                        if result.get("completion_outcome") == "evaluated"
                        else "Retention completed; evaluation and Chat activation were not requested."
                    ),
                    10,
                ),
                failure_notification=Notification(
                    "error",
                    "Post-training recovery needs attention",
                    "The saved version remains protected and the previous Chat runtime is unchanged.",
                    None,
                ),
            ),
        )

    def _post_training_recovery_worker(
        self,
        original_operation_id: str,
        training: Mapping[str, Any],
        context: OperationContext,
    ) -> dict[str, Any]:
        version_id = str(training["result_version_id"])
        policy = str(training["post_training_policy"])
        version = self.get_version(version_id)
        base: dict[str, Any] = {
            "saved_version_id": version_id,
            "checksum": version["checksum"],
            "post_training_policy": policy,
            "original_training_operation_id": original_operation_id,
            "recovered_by_operation_id": context.operation_id,
            "active_in_chat": False,
            "runtime_identity": None,
        }
        try:
            evaluation: dict[str, Any] | None = None
            evidence: dict[str, Any] | None = None
            if policy in {"evaluate", "evaluate_and_activate"}:
                evidence = self._valid_evaluation_evidence(version_id)
                self._set_post_training_state(
                    original_operation_id,
                    state="evaluating" if evidence is None else "evaluated",
                    recovery_state="active",
                )
                if evidence is None:
                    context.update(phase="Preparing evaluation")
                    evaluation = self._evaluation_worker(version_id, context)
                    context.update(phase="Confirming evaluation evidence")
                    evidence = self._valid_evaluation_evidence(
                        version_id,
                        evaluation_id=str(evaluation["evaluation_id"]),
                        allow_active_operation_id=context.operation_id,
                    )
                if evidence is None:
                    raise RuntimeError("No valid evaluation evidence is available")
                self._set_post_training_state(
                    original_operation_id,
                    state="evaluated",
                    recovery_state="active",
                    evaluation_id=str(evidence["evaluation_id"]),
                )
                base.update(
                    {
                        "evaluation_id": evidence["evaluation_id"],
                        "evaluation_metrics": evidence["metrics"],
                    }
                )
            if policy == "evaluate_and_activate":
                context.update(phase="Selecting saved version")
                self._set_post_training_state(
                    original_operation_id,
                    state="activating",
                    recovery_state="active",
                    evaluation_id=str(evidence["evaluation_id"]),
                )
                context.update(phase="Activating evaluated version")
                identity = self._activate_worker(
                    version_id, context, require_evaluation=True
                )
                self._confirm_runtime_identity(
                    version,
                    identity,
                    expected_evaluation_id=str(evidence["evaluation_id"]),
                )
                base.update({"runtime_identity": identity, "active_in_chat": True})
            retention = self._apply_post_training_retention(
                context,
                owner_operation_id=original_operation_id,
            )
            base.update(
                {
                    "retention_completed": True,
                    "retention": retention,
                }
            )
            base["completion_outcome"] = (
                "completed" if policy == "evaluate_and_activate" else "evaluated"
                if policy == "evaluate"
                else "saved"
            )
            self._validate_training_completion_result(
                base,
                operation_id=context.operation_id,
            )
            self._set_post_training_state(
                original_operation_id,
                state="completed",
                recovery_state="complete",
                evaluation_id=(
                    str(evidence["evaluation_id"]) if evidence is not None else None
                ),
                runtime_id=(
                    str(base["runtime_identity"]["runtime_id"])
                    if isinstance(base.get("runtime_identity"), Mapping)
                    else None
                ),
            )
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT result_json FROM operations WHERE id = ?",
                    (original_operation_id,),
                ).fetchone()
                previous = parse_json(row["result_json"] if row else None, {})
                if not isinstance(previous, dict):
                    previous = {}
                previous.update(
                    {
                        **base,
                        "recovered_after_restart": True,
                        "completion_outcome": "recovered_and_completed",
                    }
                )
                connection.execute(
                    """
                    UPDATE operations
                    SET result_json = ?, phase = 'Recovered post-training workflow',
                        updated_at = ?, heartbeat_at = ?
                    WHERE id = ?
                    """,
                    (json_text(previous), utc_now(), utc_now(), original_operation_id),
                )
            context.update(phase="Completed", current_progress=1, total_progress=1, result=base)
            return base
        except OperationInterrupted:
            self._set_post_training_state(
                original_operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error={"code": "post_training_recovery_interrupted"},
            )
            raise
        except BaseException as exc:
            self._set_post_training_state(
                original_operation_id,
                state="needs_attention",
                recovery_state="resume_pending",
                error={
                    "code": "post_training_recovery_failed",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            )
            raise OperationNeedsAttention(
                (
                    "Post-training recovery did not complete. The saved version "
                    "remains protected, and Chat remains on the last confirmed runtime."
                ),
                code="post_training_recovery_failed",
                phase="Post-training recovery needs attention",
                technical_details=f"{type(exc).__name__}: {exc}",
                partial_result={
                    **base,
                    "completion_outcome": "needs_attention",
                },
            ) from exc

    def _rotate_versions(self) -> dict[str, Any]:
        lineages = self.database.fetch_all(
            "SELECT id, baseline_version_id FROM dataset_lineages"
        )
        summary: dict[str, Any] = {
            "completed": True,
            "policy": "protected baseline plus two newest verified trained versions",
            "lineages_checked": len(lineages),
            "versions_deleted": [],
            "versions_skipped_as_in_use_or_shared": [],
        }
        for lineage in lineages:
            trained = self.database.fetch_all(
                """
                SELECT v.id, v.verified_at, v.created_at
                FROM version_lineage vl
                JOIN saved_versions v ON v.id = vl.saved_version_id
                WHERE vl.lineage_id = ? AND vl.role = 'trained'
                  AND v.integrity = 'verified'
                ORDER BY v.verified_at DESC, v.created_at DESC
                """,
                (lineage["id"],),
            )
            retained = {
                lineage["baseline_version_id"],
                *(row["id"] for row in trained[:2]),
            }
            for removable in trained[2:]:
                if removable["id"] in retained:
                    continue
                shared = self.database.fetch_one(
                    """
                    SELECT 1 FROM version_lineage
                    WHERE saved_version_id = ? AND lineage_id <> ? LIMIT 1
                    """,
                    (removable["id"], lineage["id"]),
                )
                if shared or self._version_usage_block(removable["id"])[0] is not None:
                    summary["versions_skipped_as_in_use_or_shared"].append(
                        removable["id"]
                    )
                    continue
                class _Context:
                    operation_id = None

                    def update(self, **_kwargs):
                        return None
                self._delete_version_worker(
                    removable["id"],
                    _Context(),
                    retention_lineage_id=lineage["id"],
                )
                summary["versions_deleted"].append(removable["id"])
        summary["versions_deleted_count"] = len(summary["versions_deleted"])
        return summary



    def list_evaluations(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT e.*, v.label AS version_label,
                   v.checksum AS saved_version_checksum,
                   v.checkpoint_path AS checkpoint_path,
                   v.prepared_dataset_id AS prepared_dataset_id
            FROM evaluations e JOIN saved_versions v ON v.id = e.saved_version_id
            ORDER BY e.created_at DESC
            """
        )
        records = []
        for row in rows:
            record = dict(row)
            raw_metrics = record.pop("metrics_json", None)
            try:
                metrics = parse_json(raw_metrics, None)
            except (TypeError, ValueError, json.JSONDecodeError):
                metrics = None
            record["metrics"] = metrics
            record["state"] = record["status"]
            record["completed_at"] = record["finished_at"]
            report_path = record.get("result_path")
            report_file = Path(report_path) if report_path else None
            record["report_path"] = report_path
            record["report_readable"] = bool(report_file and report_file.is_file())
            report_matches = bool(
                record["report_readable"]
                and record.get("result_checksum")
                and sha256_file(report_file) == record["result_checksum"]
            )
            record["integrity_state"] = (
                "verified"
                if report_matches
                else "checksum_mismatch"
                if record["report_readable"] and record.get("result_checksum")
                else "unavailable"
            )
            evidence = (
                self._valid_evaluation_evidence(
                    str(row["saved_version_id"]),
                    evaluation_id=str(row["id"]),
                )
                if record["status"] == "completed"
                else None
            )
            record["evidence_valid"] = bool(
                evidence and evidence["evaluation_id"] == record["id"]
            )
            if record["status"] == "completed" and not record["evidence_valid"]:
                record["integrity_state"] = "invalid_evidence"
                record["state"] = "invalid"
            record["contract_version"] = (
                metrics.get("format")
                if isinstance(metrics, dict) and metrics.get("format")
                else "salty-potato-evaluation-v2"
            )
            if record["evidence_valid"] and isinstance(metrics, dict):
                try:
                    record["summary"] = f"Loss {float(metrics['loss']):.4f}"
                except (KeyError, TypeError, ValueError):
                    record["summary"] = "Invalid evidence"
            else:
                record["summary"] = record["status"].title()
            records.append(record)
        canonical_by_identity: dict[tuple[str, str], str] = {}
        for record in records:
            metrics = record.get("metrics")
            identity = (
                metrics.get("evaluation_identity")
                if isinstance(metrics, dict)
                else None
            )
            if identity and record.get("evidence_valid"):
                key = (str(record["saved_version_id"]), str(identity))
                canonical_by_identity.setdefault(key, str(record["id"]))
        for record in records:
            metrics = record.get("metrics")
            identity = (
                metrics.get("evaluation_identity")
                if isinstance(metrics, dict)
                else None
            )
            canonical = canonical_by_identity.get(
                (str(record["saved_version_id"]), str(identity))
            )
            record["primary_presentation"] = canonical == str(record["id"])
            record["superseded_by_evaluation_id"] = (
                canonical if canonical and canonical != str(record["id"]) else None
            )
        return records

    def start_evaluation(
        self, version_id: str, request_key: str | None
    ) -> dict[str, Any]:
        version = self.get_version(version_id)
        if version.get("prepared_dataset_id"):
            self.production_policy.assert_prepared_evaluation_allowed(
                str(version["prepared_dataset_id"])
            )
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "evaluation",
                lambda context: self._evaluation_worker(version_id, context),
                target_id=version_id,
                dedupe_key=f"evaluation:{version_id}",
                initial_phase="Queued",
                success_notification=Notification(
                    "success", "Evaluation completed", "The result was saved."
                ),
                failure_notification=Notification(
                    "error", "Evaluation failed", "No stale result was reused.", None
                ),
            ),
        )

    def _evaluation_worker(
        self, version_id: str, context: OperationContext
    ) -> dict[str, Any]:
        version = self.get_version(version_id)
        if version.get("prepared_dataset_id"):
            self.production_policy.assert_prepared_evaluation_allowed(
                str(version["prepared_dataset_id"])
            )
        prepared = None
        if version.get("prepared_dataset_id"):
            prepared = self.database.fetch_one(
                """
                SELECT * FROM prepared_datasets
                WHERE id = ? AND status = 'ready' AND verified = 1
                """,
                (version["prepared_dataset_id"],),
            )
        if not prepared or int(prepared.get("validation_record_count") or 0) < 1:
            raise ValueError(
                "Evaluation unavailable: this saved version has no verified "
                "validation split."
            )
        prepared["settings"] = parse_json(
            prepared.pop("settings_json"), {}
        )
        freshness = self.datasets.check_source_changed(
            prepared["dataset_id"], force_checksum=True
        )
        if freshness["changed"]:
            raise ValueError(
                "The dataset source changed; prepare it again before evaluation"
            )
        prepared_verification = self.datasets.verify_prepared(prepared["id"])
        if not prepared_verification["verified"]:
            raise ValueError("The selected prepared evaluation data failed verification")
        if not prepared_verification.get("training_eligible", False):
            raise ValueError(
                "EVALUATION_CONTRACT_LEGACY: legacy prepared data has no durable "
                "target labels; create a v2 evaluation split"
            )
        while True:
            claimed = False
            with self._evaluation_claim_lock:
                active_evaluation = self.database.fetch_one(
                    """
                    SELECT 1
                    FROM evaluations e
                    JOIN operations o ON o.id = e.operation_id
                    WHERE e.saved_version_id = ?
                      AND o.id <> ?
                      AND o.state IN ('queued','running','stop_requested')
                    LIMIT 1
                    """,
                    (version_id, context.operation_id),
                )
                if active_evaluation is None:
                    claimed = True
                    evaluation_id = new_id()
                    report_path = self.paths.evaluations / f"{evaluation_id}.json"
                    now = utc_now()
                    with self.database.transaction() as connection:
                        connection.execute(
                            """
                            INSERT INTO evaluations(
                                id, operation_id, saved_version_id, status,
                                records_completed, created_at, updated_at
                            ) VALUES (?, ?, ?, 'running', 0, ?, ?)
                            """,
                            (evaluation_id, context.operation_id, version_id, now, now),
                        )
            if claimed:
                break
            joined = self._wait_for_active_evaluation(version_id, context)
            if joined is not None:
                return joined
        try:
            evaluation = run_evaluation(
                EvaluationSettings(
                    operation_id=context.operation_id,
                    version_id=version_id,
                    checkpoint_path=version["checkpoint_path"],
                    expected_weight_sha256=version["checksum"],
                    expected_tokenizer_fingerprint=version[
                        "tokenizer_checksum"
                    ],
                    prepared_path=prepared["path"],
                    report_path=str(report_path),
                    context_limit=int(version["context_tokens"]),
                    sequence_length=int(
                        prepared["settings"]["sequence_length"]
                    ),
                    batch_size=int(self.config.section("evaluation")["batch_size"]),
                    max_records=int(self.config.section("evaluation")["max_records"]),
                    device=str(self.config.section("training")["device"]),
                    precision=str(self.config.section("training")["precision"]),
                ),
                _Progress(context),
            )
        except BaseException:
            report_path.unlink(missing_ok=True)
            failed = utc_now()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE evaluations
                    SET status = 'failed', updated_at = ?, finished_at = ?
                    WHERE id = ? AND status IN ('queued','running')
                    """,
                    (failed, failed, evaluation_id),
                )
            raise
        if evaluation is None:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE evaluations SET status = 'interrupted',
                        updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), utc_now(), evaluation_id),
                )
            raise OperationInterrupted("Evaluation stopped safely")
        payload = evaluation.to_dict()
        try:
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                report_payload.get("operation_id") != context.operation_id
                or report_payload.get("saved_version_id") != version_id
                or report_payload.get("checkpoint_id") != version_id
                or report_payload.get("state") != "completed"
                or report_payload.get("weight_sha256") != version["checksum"]
            ):
                raise RuntimeError("Saved evaluation report identity is inconsistent")
        except BaseException as exc:
            report_path.unlink(missing_ok=True)
            failed = utc_now()
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE evaluations
                    SET status = 'failed', updated_at = ?, finished_at = ?
                    WHERE id = ? AND status IN ('queued','running')
                    """,
                    (failed, failed, evaluation_id),
                )
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Saved evaluation report is unreadable: {exc}") from exc
        origin = self.database.fetch_one(
            """
            SELECT o.result_json FROM saved_versions v
            LEFT JOIN training_operations t ON t.id = v.training_operation_id
            LEFT JOIN operations o ON o.id = t.operation_id
            WHERE v.id = ?
            """,
            (version_id,),
        )
        origin_result = parse_json(
            origin.get("result_json") if origin else None, {}
        )
        if not isinstance(origin_result, dict):
            origin_result = {}
        evaluation_identity = evaluation.evaluation_contract_digest
        if not evaluation_identity or not evaluation.evaluation_contract:
            raise RuntimeError("Evaluation completed without an identity contract")
        metrics = {
            "format": "salty-potato-evaluation-v3",
            "loss": evaluation.mean_loss,
            "perplexity": evaluation.perplexity,
            "records_evaluated": evaluation.records_completed,
            "tokens_evaluated": evaluation.tokens_evaluated,
            "tokens_per_second": evaluation.tokens_per_second,
            "duration_seconds": evaluation.duration_seconds,
            "batch_size": payload.get("batch_size"),
            "timings_seconds": payload.get("timings_seconds"),
            "dataset_id": prepared["dataset_id"],
            "prepared_dataset_id": prepared["id"],
            "evaluation_split": "validation",
            "context_length": int(
                evaluation.evaluation_contract["context_length"]
            ),
            "sequence_length": int(
                evaluation.evaluation_contract["sequence_length"]
            ),
            "training_loss_at_checkpoint": origin_result.get("training_loss"),
            "loss_contract": "token_natural_log_cross_entropy",
            "evaluation_identity": evaluation_identity,
            "evaluation_contract_digest": evaluation_identity,
            "evaluation_contract": evaluation.evaluation_contract,
            "lineage_id": version.get("lineage_id"),
        }
        context.raise_if_stop_requested()
        finished = utc_now()
        stale = False
        database_write_started = time.perf_counter()
        with self.database.transaction() as connection:
            operation_state = connection.execute(
                "SELECT state FROM operations WHERE id = ?",
                (context.operation_id,),
            ).fetchone()
            if not operation_state or operation_state["state"] == "stop_requested":
                connection.execute(
                    """
                    UPDATE evaluations SET status = 'interrupted',
                        updated_at = ?, finished_at = ?
                    WHERE id = ? AND status IN ('queued','running')
                    """,
                    (finished, finished, evaluation_id),
                )
                stale = True
            else:
                cursor = connection.execute(
                    """
                    UPDATE evaluations
                    SET status = 'completed', records_completed = ?,
                        total_records = ?, metrics_json = ?, result_path = ?,
                        result_checksum = ?, updated_at = ?, finished_at = ?
                    WHERE id = ? AND status IN ('queued','running')
                    """,
                    (
                        evaluation.records_completed,
                        evaluation.total_records,
                        json_text(metrics),
                        evaluation.report_path,
                        evaluation.report_sha256,
                        finished,
                        finished,
                        evaluation_id,
                    ),
                )
                stale = cursor.rowcount != 1
        database_write_seconds = time.perf_counter() - database_write_started
        if stale:
            report_path.unlink(missing_ok=True)
            raise OperationInterrupted("Evaluation completed too late to commit safely")
        metrics_timings = metrics.get("timings_seconds")
        if not isinstance(metrics_timings, dict):
            metrics_timings = {}
            metrics["timings_seconds"] = metrics_timings
        metrics_timings["database_write_seconds"] = database_write_seconds
        metrics_timings[
            "database_write_measurement_scope"
        ] = "first durable evaluation-result commit"
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE evaluations
                SET metrics_json = ?, updated_at = ?
                WHERE id = ? AND status = 'completed'
                """,
                (json_text(metrics), utc_now(), evaluation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Completed evaluation metrics could not retain database timing"
                )
        payload_timings = payload.get("timings_seconds")
        if isinstance(payload_timings, dict):
            payload_timings["database_write_seconds"] = database_write_seconds
        return {
            **payload,
            "evaluation_id": evaluation_id,
            "operation_id": context.operation_id,
            "saved_version_id": version_id,
            "metrics": metrics,
            "saved": True,
            "report_readable": report_path.is_file(),
        }



    def chat_status(self) -> dict[str, Any]:
        status = self.chat.status()
        base = self.model_bundles.selected_base("text_generation")
        if base is None:
            return status
        status.update(
            {
                "base_model_id": base["id"],
                "base_model_label": base["display_name"],
                "base_model_architecture": base["architecture"],
                "base_model_format": base["model_format"],
                "base_model_quantization": base["quantization"],
                "base_model_integrity": base["integrity"],
                "selected_model_id": base["id"],
                "selected_model_label": base["display_name"],
                "selected_model_kind": "model_bundle",
                "configured_context_tokens": base["configured_context_tokens"],
                "architectural_context_tokens": base[
                    "architectural_context_tokens"
                ],
            }
        )
        if not status.get("runtime_ready") and not status.get(
            "active_saved_version_id"
        ):
            native_registered = self.model_bundle_runtime is not None
            configured_context = int(
                status["generation_defaults"]["context_window_tokens"]
            )
            status.update(
                {
                    "readiness": (
                        "native_runtime_cold"
                        if native_registered
                        else base["runtime_state"]
                    ),
                    "message": (
                        "The private native runtime will load this model on the first message."
                        if native_registered
                        else base["runtime_reason"]
                    ),
                    "runtime_ready": False,
                    "generation_defaults": {
                        **dict(status["generation_defaults"]),
                        "context_window_tokens": configured_context,
                    },
                    "runtime_controls": {
                        **dict(status["runtime_controls"]),
                        "quantisation": {
                            "available": True,
                            "formats": [base["quantization"]],
                            "selected": base["quantization"],
                            "runtime_ready": False,
                            "reason": (
                                "The imported weights remain at their exact registered "
                                f"{base['quantization']} quantisation and load directly "
                                "inside Salty Steak on the first message."
                                if native_registered
                                else "The native runtime has not passed activation."
                            ),
                        },
                    },
                }
            )
        return status

    def list_conversations(self) -> list[dict[str, Any]]:
        return self.chat.list_conversations()

    def create_conversation(self) -> dict[str, Any]:
        return self.chat.create_conversation()

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.chat.get_conversation(conversation_id)

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> dict[str, Any]:
        return self.chat.rename_conversation(conversation_id, title)

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.chat.delete_conversation(conversation_id)



    def set_conversation_pinned(
        self, conversation_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.chat.set_conversation_pinned(
            conversation_id, bool(payload.get("pinned"))
        )

    def list_conversation_labels(self) -> dict[str, Any]:
        return {"labels": self.chat.list_labels()}

    def create_conversation_label(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "label": self.chat.create_label(
                payload.get("name"), str(payload.get("tone") or "neutral")
            )
        }

    def update_conversation_label(
        self, label_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "label": self.chat.update_label(
                label_id,
                name=payload.get("name") if "name" in payload else None,
                tone=payload.get("tone") if "tone" in payload else None,
            )
        }

    def delete_conversation_label(self, label_id: str) -> dict[str, Any]:
        return self.chat.delete_label(label_id)

    def set_conversation_label(
        self, conversation_id: str, label_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.chat.set_conversation_label(
            conversation_id, label_id, bool(payload.get("applied"))
        )

    def send_message(
        self,
        conversation_id: str,
        content: str,
        request_key: str | None = None,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.start_message(
                conversation_id, content, generation_settings
            ),
        )

    def confirm_image_generation(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        request_key: str | None = None,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.confirm_image_generation(
                conversation_id,
                proposal_id,
                assistant_message_id,
                generation_settings,
            ),
        )

    def confirm_host_action(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        request_key: str | None = None,
        generation_settings: dict[str, Any] | None = None,
        confirmation_text: str | None = None,
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.confirm_host_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
                generation_settings,
                confirmation_text,
            ),
        )

    def image_artifact_content(self, artifact_id: str) -> BinaryFileResponse:
        identifier = str(artifact_id or "").strip()
        if not identifier or len(identifier) > 128 or not all(
            character.isalnum() or character in "_-" for character in identifier
        ):
            raise ValueError("The image artifact identifier is invalid")
        row = self.database.fetch_one(
            """
            SELECT id, relative_path, media_type, size_bytes, sha256
            FROM chat_artifacts WHERE id = ? AND kind = 'image'
            """,
            (identifier,),
        )
        if row is None:
            raise KeyError("Generated image artifact does not exist")
        root = (self.paths.conversations / "artifacts").resolve()
        candidate = (root / str(row["relative_path"])).resolve(strict=True)
        if candidate == root or root not in candidate.parents or not candidate.is_file():
            raise RuntimeError("Generated image artifact escaped its owned storage root")
        if candidate.is_symlink():
            raise RuntimeError("Generated image artifact cannot be a symbolic link")
        size = candidate.stat().st_size
        if size != int(row["size_bytes"]):
            raise RuntimeError("Generated image artifact size no longer matches")
        expected_hash = str(row["sha256"]).casefold()
        if sha256_file(candidate).casefold() != expected_hash:
            raise RuntimeError("Generated image artifact hash no longer matches")
        return BinaryFileResponse(
            path=candidate,
            content_type=str(row["media_type"]),
            filename=f"salty-steak-{identifier}.png",
            sha256=expected_hash,
        )

    def stage_vision_input(
        self,
        path: str | Path,
        *,
        filename: str,
        media_type: str,
        source: str,
        consent_evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = self.chat.vision_status()
        if not status["application_available"]:
            raise RuntimeError(str(status["reason"]))
        return self.vision_inputs.stage(
            path,
            filename=filename,
            declared_media_type=media_type,
            source=source,
            consent_evidence=consent_evidence,
        )

    def analyze_vision_input(
        self,
        conversation_id: str,
        prompt: str,
        vision_input_token: str,
        request_key: str | None,
        maximum_output_tokens: int = 128,
    ) -> dict[str, Any]:
        if request_key is None:
            raise PermissionError(
                "Image analysis requires an idempotency key as its permission identity"
            )
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.start_vision_analysis(
                conversation_id,
                prompt,
                vision_input_token,
                permission_request_id=request_key,
                maximum_output_tokens=maximum_output_tokens,
            ),
        )

    def retry_vision_analysis(
        self,
        operation_id: str,
    ) -> dict[str, Any]:
        operation = self.operations.get(operation_id)
        if operation is None or operation.get("type") != "chat_vision_analysis":
            raise KeyError("Vision analysis operation does not exist")
        if operation.get("state") not in {"completed", "failed", "interrupted"}:
            raise ValueError("Vision analysis is still running")
        return self.vision_inputs.issue_retry(operation_id)

    def stage_screen_capture_for_vision(
        self,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        if request.get("user_confirmed") is not True:
            raise PermissionError(
                "Screen-capture analysis requires an explicit user_confirmed=true request"
            )
        audit_id = str(request.get("screen_capture_audit_id") or "").strip()
        if not audit_id:
            raise PermissionError("Screen-capture audit identity is required")
        record = next(
            (
                item
                for item in self.automation.audit_records(limit=500)
                if item["id"] == audit_id
            ),
            None,
        )
        if (
            record is None
            or record.get("event") != "invoke"
            or record.get("capability") != "screen.capture"
            or record.get("outcome") != "succeeded"
        ):
            raise PermissionError("Screen-capture grant/audit boundary did not pass")
        artifact = dict((record.get("result") or {}).get("artifact") or {})
        path = str(artifact.get("path") or "")
        if not path or not Path(path).is_file():
            raise RuntimeError("The approved screen-capture artifact is missing")
        if sha256_file(path) != artifact.get("sha256"):
            raise RuntimeError("The approved screen-capture artifact changed")
        return self.stage_vision_input(
            path,
            filename=Path(path).name,
            media_type="image/bmp",
            source="screen_capture",
            consent_evidence={
                "user_confirmed": True,
                "screen_capture_audit_id": audit_id,
                "screen_capture_capability": "screen.capture",
            },
        )

    def start_agent_task(
        self,
        conversation_id: str,
        instruction: str,
        request_key: str | None = None,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.start_agent_task(
                conversation_id, instruction, generation_settings
            ),
        )

    def retry_message(
        self,
        conversation_id: str,
        user_message_id: str,
        request_key: str | None = None,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.chat.start_retry(
                conversation_id, user_message_id, generation_settings
            ),
        )

    def tool_capabilities(self) -> dict[str, Any]:
        return self._describe_plugins()

    def plugin_capabilities(self) -> dict[str, Any]:
        return self._describe_plugins()

    def _describe_plugins(self) -> dict[str, Any]:



        return self.plugins.describe(
            vision_status=self.chat.vision_status(),
            automation_status=self.automation.status(),
        )

    def plugin_connectors(self) -> list[dict[str, Any]]:
        return self.plugins.connectors()

    def configure_plugin_connector(
        self, connector_id: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.plugins.configure_connector(connector_id, request)

    def test_plugin_connector(self, connector_id: str) -> dict[str, Any]:
        return self.plugins.test_connector(connector_id)

    def call_plugin_connector(
        self, connector_id: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self.plugins.call_connector(connector_id, request)

    def disconnect_plugin_connector(
        self, connector_id: str
    ) -> dict[str, Any]:
        return self.plugins.disconnect_connector(connector_id)

    def automation_status(self) -> dict[str, Any]:
        return self.automation.status()

    def grant_automation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.automation.grant(request)

    def revoke_automation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.automation.revoke(request)

    def invoke_automation(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self.automation.invoke(request)

    def automation_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.automation.audit_records(limit=limit)

    def runtime_plan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Plan the highest-quality local representation that fits this PC."""
        system = dict(self.about_state().get("system") or {})
        try:
            import torch

            if torch.cuda.is_available():
                properties = torch.cuda.get_device_properties(torch.cuda.current_device())
                system["gpu_model"] = properties.name
                system["gpu_memory_bytes"] = int(properties.total_memory)
        except (AttributeError, RuntimeError):

            system["gpu_memory_bytes"] = 0
        return plan_local_runtime(request, system)



    def about_state(self) -> dict[str, Any]:
        """Truthful lightweight diagnostics; multi-gigabyte scans live elsewhere."""
        package_root = self.config.project_root.resolve()
        package_record_path = package_root / "package.json"
        package_record: dict[str, Any] = {}
        if package_record_path.is_file():
            try:
                package_record = json.loads(
                    package_record_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError, TypeError):
                package_record = {}

        executable_text = _process_environment("SALTY_POTATO_EXECUTABLE_PATH")
        packaged_executable = package_root / "Salty Steak.exe"
        executable = (
            Path(executable_text).resolve()
            if executable_text
            else packaged_executable.resolve()
            if packaged_executable.is_file()
            else None
        )
        executable_exists = bool(executable and executable.is_file())
        executable_stat = executable.stat() if executable_exists and executable else None
        manifest_path = package_root / "integrity-manifest.json"
        manifest_exists = manifest_path.is_file()
        manifest_document: dict[str, Any] = {}
        if manifest_exists:
            try:
                manifest_document = json.loads(
                    manifest_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, ValueError, TypeError):
                manifest_document = {}
        executable_manifest_entry = next(
            (
                item
                for item in manifest_document.get("files", [])
                if isinstance(item, dict)
                and executable is not None
                and Path(str(item.get("path", ""))).name.casefold()
                == executable.name.casefold()
            ),
            None,
        )
        build_id = _process_environment(
            "SALTY_POTATO_BUILD_ID"
        ) or str(
            str(package_record.get("build_id") or "development"),
        )
        if "native-r2" in build_id:
            build_status = "Candidate"
        elif package_record:
            build_status = "Release package"
        else:
            build_status = "Development source"

        active_version = self.database.fetch_one(
            """
            SELECT v.*, vl.lineage_id, vl.parent_version_id, vl.role,
                   vl.user_protected
            FROM active_runtime a
            JOIN saved_versions v ON v.id = a.saved_version_id
            LEFT JOIN version_lineage vl ON vl.saved_version_id = v.id
            WHERE a.singleton = 1
            ORDER BY vl.created_at DESC
            LIMIT 1
            """
        )
        if active_version:
            runtime_identity = self.runtime.identity
            active_version["chat_status"] = (
                "active"
                if runtime_identity
                and runtime_identity.saved_version_id == active_version["id"]
                else "selected_not_loaded"
            )
            active_version["runtime_loaded"] = (
                active_version["chat_status"] == "active"
            )
        runtime_identity = self.runtime.identity
        runtime_row = None
        if active_version:
            runtime_row = self.database.fetch_one(
                """
                SELECT r.loaded_at, r.verified_at, r.state, a.updated_at
                FROM active_runtime a
                JOIN runtime_states r ON r.id = a.runtime_id
                WHERE a.singleton = 1 AND a.saved_version_id = ?
                """,
                (active_version["id"],),
            )

        model: dict[str, Any] | None = None
        if active_version:
            checkpoint = Path(active_version["checkpoint_path"]).resolve()
            config_path = checkpoint / "config.json"
            config_document: dict[str, Any] = {}
            if config_path.is_file():
                try:
                    config_document = json.loads(
                        config_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError):
                    config_document = {}
            weight_files = sorted(checkpoint.glob("*.safetensors"))
            weight_bytes = sum(
                file.stat().st_size for file in weight_files if file.is_file()
            )



            evaluation = None
            metrics = None
            user_protected = bool(active_version.get("user_protected"))
            model = {
                "display_name": config_document.get("display_name") or "Salty Steak",
                "architecture": config_document.get("model_type"),
                "saved_version_id": active_version.get("id"),
                "checkpoint_id": active_version.get("id"),
                "checkpoint_sha256": active_version.get("checksum"),
                "lineage_id": active_version.get("lineage_id"),
                "parent_version_id": active_version.get("parent_version_id"),
                "baseline_role": active_version.get("role"),
                "protection_state": (
                    "User protected"
                    if user_protected
                    else "Protected baseline"
                    if active_version.get("role") == "baseline"
                    else "Retained"
                ),
                "active_version_state": active_version.get("chat_status"),
                "loaded_runtime_state": (
                    "Loaded"
                    if active_version.get("runtime_loaded")
                    else "Not currently loaded"
                ),
                "cumulative_training_steps": active_version.get("total_trained_steps"),
                "additional_steps": active_version.get("additional_steps"),
                "parameter_count": self.config.section("model").get("parameter_count"),
                "trainable_parameter_count": None,
                "non_trainable_parameter_count": None,
                "context_limit": active_version.get("context_tokens"),
                "vocabulary_size": config_document.get("vocab_size"),
                "hidden_size": config_document.get("hidden_size"),
                "layer_count": config_document.get("num_hidden_layers"),
                "attention_head_count": config_document.get("num_attention_heads"),
                "precision": (
                    runtime_identity.precision if runtime_identity else None
                ),
                "quantisation_state": None,
                "weight_format": "SafeTensors" if weight_files else None,
                "weight_file_count": len(weight_files),
                "weight_file_total_bytes": weight_bytes,
                "tokenizer_size_bytes": _directory_size(self.paths.tokenizer),
                "configuration_file_size_bytes": (
                    config_path.stat().st_size if config_path.is_file() else None
                ),
                "saved_version_directory_size_bytes": active_version.get("size_bytes"),
                "last_activation_time": (
                    (runtime_row or {}).get("loaded_at")
                    or (runtime_row or {}).get("updated_at")
                ),
                "last_successful_evaluation": (
                    evaluation.get("finished_at") if evaluation else None
                ),
                "validation_loss": (
                    metrics.get("loss") if isinstance(metrics, dict) else None
                ),
                "perplexity": (
                    metrics.get("perplexity") if isinstance(metrics, dict) else None
                ),
                "evaluation_records": (
                    evaluation.get("records_completed") if evaluation else None
                ),
                "evaluation_tokens": (
                    metrics.get("tokens") if isinstance(metrics, dict) else None
                ),
            }

        active_operations = self.operations.list(
            states=ACTIVE_OPERATION_STATES,
            limit=1,
        )
        chat_state = self.chat.status()
        gpu_name = (
            runtime_identity.device
            if runtime_identity
            and str(runtime_identity.device).casefold().startswith("cuda")
            else None
        )
        gpu_memory_bytes = None

        installed_memory = None
        available_memory = None
        if os.name == "nt":
            try:
                import ctypes

                class MemoryStatus(ctypes.Structure):
                    _fields_ = [
                        ("length", ctypes.c_ulong),
                        ("memory_load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong),
                        ("available_physical", ctypes.c_ulonglong),
                        ("total_page_file", ctypes.c_ulonglong),
                        ("available_page_file", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended_virtual", ctypes.c_ulonglong),
                    ]

                memory_status = MemoryStatus()
                memory_status.length = ctypes.sizeof(MemoryStatus)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(
                    ctypes.byref(memory_status)
                ):
                    installed_memory = int(memory_status.total_physical)
                    available_memory = int(memory_status.available_physical)
            except (AttributeError, OSError, ValueError):
                pass

        drive = Path(package_root.anchor or self.paths.workspace.anchor)
        drive_usage = shutil.disk_usage(drive)
        build_date = package_record.get("packaged_at_utc")
        if not build_date and executable_stat:
            build_date = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(executable_stat.st_mtime),
            )
        return {
            "application": {
                "product_name": "Salty Steak",
                "app_version": "2.0.0",
                "build_id": build_id,
                "build_type": build_status,
                "build_date": build_date,
                "native_host_version": _process_environment(
                    "SALTY_POTATO_NATIVE_VERSION"
                )
                or package_record.get("native_host_version"),
                "frontend_version": "2.0.0",
                "backend_version": "2.0.0",
                "release_status": build_status,
            },
            "package": {
                "executable_path": str(executable) if executable else None,
                "executable_sha256": (
                    executable_manifest_entry.get("sha256")
                    if isinstance(executable_manifest_entry, dict)
                    else None
                ),
                "executable_size_bytes": (
                    executable_stat.st_size if executable_stat else None
                ),
                "package_path": str(package_root),
                "manifest_sha256": (
                    sha256_file(manifest_path) if manifest_exists else None
                ),
                "integrity_state": (
                    "Integrity manifest present"
                    if manifest_exists
                    else "No integrity manifest in development source"
                ),
                "repository_source_isolation": (
                    "Isolated from repository source"
                    if package_record.get("source_dependency") is False
                    else "Development source tree"
                ),
            },
            "runtime": {
                "runtime_state": chat_state.get("readiness"),
                "backend_state": "Ready",
                "active_operation": active_operations[0] if active_operations else None,
                "private_python_version": platform.python_version(),
                "private_python_executable": str(
                    (
                        package_root / ".venv" / "Scripts" / "python.exe"
                    ).resolve()
                )
                if (package_root / ".venv" / "Scripts" / "python.exe").is_file()
                else sys.executable,
                "webview2_runtime_version": _process_environment(
                    "SALTY_POTATO_WEBVIEW2_VERSION"
                ),
                "cuda_available": gpu_name is not None,
                "device": runtime_identity.device if runtime_identity else None,
                "precision": runtime_identity.precision if runtime_identity else None,
                "last_runtime_load_time": (
                    (runtime_row or {}).get("loaded_at") if runtime_row else None
                ),
                "workspace_path": str(self.paths.workspace),
                "logs_path": str(self.paths.logs),
            },
            "system": {
                "windows_version": _windows_version_without_process(),
                "cpu_model": _process_environment("PROCESSOR_IDENTIFIER"),
                "logical_processor_count": os.cpu_count(),
                "installed_ram_bytes": installed_memory,
                "available_ram_bytes": available_memory,
                "gpu_model": gpu_name,
                "gpu_memory_bytes": gpu_memory_bytes,
                "current_drive": drive.anchor,
                "drive_free_bytes": drive_usage.free,
            },
            "model": model,
            "paths": {
                "logs": str(self.paths.logs),
                "workspace": str(self.paths.workspace),
                "data": str(self.paths.datasets),
                "package": str(package_root),
            },
        }

    def about_storage(self, *, refresh: bool = False) -> dict[str, Any]:
        """Measure storage on a request worker and cache the expensive result."""
        with self._about_storage_lock:
            if (
                not refresh
                and self._about_storage_cache
                and time.time() - float(
                    self._about_storage_cache.get("measured_at_unix", 0)
                ) < 300
            ):
                return dict(self._about_storage_cache)

            package_root = self.config.project_root.resolve()
            if package_root.parent.name == "dist":
                rollback = package_root.parent / "Salty-Potato-AI-final"
            else:
                rollback = package_root / "dist" / "Salty-Potato-AI-final"
            workspace_bytes, workspace_files = _directory_measure(self.paths.workspace)
            package_bytes, package_files = _directory_measure(package_root)
            rollback_bytes, rollback_files = _directory_measure(rollback)
            conversation_payload = self.database.fetch_one(
                """
                SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) AS bytes
                FROM messages
                """
            )
            drive_usage = shutil.disk_usage(package_root.anchor)
            measured_at_unix = time.time()
            result = {
                "measured_at": utc_now(),
                "measured_at_unix": measured_at_unix,
                "workspace_total_bytes": workspace_bytes,
                "workspace_file_count": workspace_files,
                "dataset_bytes": _directory_size(self.paths.datasets),
                "prepared_data_bytes": _directory_size(self.paths.prepared),
                "saved_version_bytes": _directory_size(self.paths.versions),
                "checkpoint_bytes": _directory_size(
                    self.paths.workspace / "checkpoints"
                ),
                "tokenizer_and_configuration_bytes": (
                    _directory_size(self.paths.tokenizer)
                    + _directory_size(package_root / "config")
                ),
                "database_bytes": (
                    self.paths.database.stat().st_size
                    if self.paths.database.is_file()
                    else 0
                ),
                "conversation_payload_bytes": int(
                    (conversation_payload or {}).get("bytes", 0)
                ),
                "evaluation_data_bytes": _directory_size(self.paths.evaluations),
                "logs_bytes": _directory_size(self.paths.logs),
                "cache_bytes": _directory_size(self.paths.cache),
                "disposable_temporary_data_bytes": _directory_size(self.paths.cache),
                "current_package_bytes": package_bytes,
                "current_package_file_count": package_files,
                "rollback_package_bytes": rollback_bytes if rollback.exists() else None,
                "rollback_package_file_count": (
                    rollback_files if rollback.exists() else None
                ),
                "free_disk_bytes": drive_usage.free,
            }
            self._about_storage_cache = dict(result)
            return result

    def project_state(self) -> dict[str, Any]:
        versions = self.database.fetch_all(
            """
            SELECT id, checkpoint_path
            FROM saved_versions
            WHERE integrity = 'verified'
            ORDER BY created_at DESC
            LIMIT 2
            """
        )
        last_verified_at = (
            (
                self.database.fetch_one(
                    "SELECT value FROM application_metadata WHERE key = 'last_verified_at'"
                )
                or {}
            ).get("value")
        )
        verification_issue_count_value = (
            (
                self.database.fetch_one(
                    """
                    SELECT value FROM application_metadata
                    WHERE key = 'last_verification_issue_count'
                    """
                )
                or {}
            ).get("value")
        )
        verification_issue_count = (
            int(verification_issue_count_value)
            if verification_issue_count_value is not None
            else None
        )
        if last_verified_at is None:
            integrity_status = "not_verified"
            integrity_label = "Verification not run yet"
        elif verification_issue_count:
            integrity_status = "needs_attention"
            integrity_label = (
                f"{verification_issue_count} verification "
                f"{'issue' if verification_issue_count == 1 else 'issues'}"
            )
        else:
            integrity_status = "ready"
            integrity_label = "Project verified"
        active = self.database.fetch_one(
            """
            SELECT v.checkpoint_path, r.artifact_path
            FROM active_runtime a
            JOIN saved_versions v ON v.id = a.saved_version_id
            JOIN runtime_states r ON r.id = a.runtime_id
            WHERE a.singleton = 1
            """
        )
        recovery = self.paths.training / "recovery"
        return {
            "product_name": "Salty Steak",
            "integrity_status": integrity_status,
            "integrity_label": integrity_label,
            "model": {
                "parameter_count": int(self.config.section("model")["parameter_count"]),
                "vocab_size": int(self.config.section("model")["vocab_size"]),
                "architecture_revision": int(
                    self.config.section("model")["architecture_revision"]
                ),
                "architectural_context_tokens": int(
                    self.config.section("model")["architectural_context_tokens"]
                ),
            },
            "training": {
                "sequence_length": int(
                    self.config.section("training")["sequence_length"]
                ),
                "device": self.config.section("training")["device"],
                "precision": self.config.section("training")["precision"],
            },
            "paths": {
                "package": str(self.config.project_root),
                "workspace": str(self.paths.workspace),
                "model_architecture": str(
                    self.config.project_root
                    / "app"
                    / "backend"
                    / "versions"
                    / "model.py"
                ),
                "tokenizer": str(self.paths.tokenizer),
                "datasets": str(self.paths.datasets),
                "latest_saved_version": (
                    versions[0]["checkpoint_path"] if versions else None
                ),
                "previous_saved_version": (
                    versions[1]["checkpoint_path"] if len(versions) > 1 else None
                ),
                "training_recovery_state": str(recovery)
                if recovery.is_dir()
                else None,
                "active_chat_version": active["checkpoint_path"] if active else None,
                "runtime_artifact": active["artifact_path"] if active else None,
                "database": str(self.paths.database),
                "cache": str(self.paths.cache),
                "logs": str(self.paths.logs),
            },
            "cache": {
                "disposable_size_bytes": (
                    self._about_storage_cache.get("cache_bytes")
                    if self._about_storage_cache
                    else None
                )
            },
            "last_verified_at": last_verified_at,
            "verification_issue_count": verification_issue_count,
        }

    def verify_project(self, request_key: str | None) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "project_verification",
                self._verify_project_worker,
                target_id="project",
                dedupe_key="project-verification",
                success_notification=Notification(
                    "information", "Project files verified", "Review any reported issues."
                ),
            ),
        )

    def _verify_project_worker(self, context: OperationContext) -> dict[str, Any]:
        versions = self.list_versions()
        datasets = self.list_datasets()
        active = self.database.fetch_one(
            """
            SELECT a.saved_version_id, a.runtime_id, r.artifact_path,
                   v.checksum AS checkpoint_checksum, v.context_tokens
            FROM active_runtime a
            JOIN runtime_states r ON r.id = a.runtime_id
            JOIN saved_versions v ON v.id = a.saved_version_id
            WHERE a.singleton = 1
            """
        )
        required_files = (
            self.paths.database,
            self.paths.tokenizer / "tokenizer.model",
            self.paths.tokenizer / "tokenizer.json",
            self.config.defaults_path,
            self.config.local_path,
            self.config.project_root
            / "app"
            / "backend"
            / "versions"
            / "model.py",
            self.config.project_root / "app" / "frontend" / "dist" / "index.html",
        )
        total = len(required_files) + len(versions) + len(datasets) + bool(active)
        context.update(phase="Verifying", total_progress=total)
        issues: list[str] = []
        checked = 0
        for required in required_files:
            if not required.is_file():
                issues.append(f"Missing {required}")
            checked += 1
            context.checkpoint(current_progress=checked, total_progress=total)
        for version in versions:
            report = inspect_checkpoint(
                version["checkpoint_path"], calculate_checksum=True
            )
            if not report.valid:
                issues.extend(report.errors)
            if report.weight_sha256 != version["checksum"]:
                issues.append(
                    f"Saved-version checksum differs from its database identity: "
                    f"{version['id']}"
                )
            checked += 1
            context.checkpoint(current_progress=checked, total_progress=total)
        for dataset in datasets:
            source = Path(dataset["source_path"])
            if not source.is_file():
                issues.append(f"Missing dataset source {source}")
            elif sha256_file(source) != dataset["source_checksum"]:
                issues.append(f"Dataset source checksum changed: {source}")
            checked += 1
            context.checkpoint(current_progress=checked, total_progress=total)
        if active:
            runtime_file = Path(active["artifact_path"]) / "runtime.json"
            if not runtime_file.is_file():
                issues.append("Active runtime identity file is missing")
            else:
                runtime_identity = json.loads(runtime_file.read_text(encoding="utf-8"))
                expected = {
                    "checkpoint_id": active["saved_version_id"],
                    "runtime_id": active["runtime_id"],
                    "weight_sha256": active["checkpoint_checksum"],
                    "context_limit": active["context_tokens"],
                }
                for key, value in expected.items():
                    if runtime_identity.get(key) != value:
                        issues.append(f"Active runtime identity differs for {key}")
            checked += 1
            context.checkpoint(current_progress=checked, total_progress=total)
        issues.extend(
            f"Foreign key violation: {row}"
            for row in self.database.foreign_key_violations()
        )
        verified = utc_now()
        with self.database.transaction() as connection:
            for key, value in (
                ("last_verified_at", verified),
                ("last_verification_issue_count", str(len(issues))),
                ("last_verification_issues", json_text(issues)),
            ):
                connection.execute(
                    """
                    INSERT INTO application_metadata(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
        return {
            "files_checked": checked,
            "issue_count": len(issues),
            "issues": issues,
            "verified_at": verified,
        }

    def clear_cache(self, request_key: str | None) -> dict[str, Any]:
        return self._idempotent_operation(
            request_key,
            lambda: self.operations.submit(
                "cache_clear",
                self._clear_cache_worker,
                target_id="cache",
                dedupe_key="cache-clear",
                success_notification=Notification(
                    "information", "Disposable cache cleared", "Regenerable files were removed."
                ),
            ),
        )

    def _clear_cache_worker(self, context: OperationContext) -> dict[str, Any]:
        root = self.paths.cache.resolve()
        children = list(root.iterdir()) if root.exists() else []
        total = len(children)
        context.update(phase="Clearing cache", total_progress=total or None)
        reclaimed = 0
        files = 0
        for index, child in enumerate(children, 1):
            reclaimed += _directory_size(child)
            files += sum(1 for path in child.rglob("*") if path.is_file()) if child.is_dir() else 1
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            context.checkpoint(
                current_progress=index,
                total_progress=total,
                details={
                    "storage_reclaimed_bytes": reclaimed,
                    "files_removed_count": files,
                },
            )
        return {
            "storage_reclaimed_bytes": reclaimed,
            "files_removed_count": files,
        }

    def open_external(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Open a web address the user clicked in their own browser.

        The WebView cancels every navigation that is not the application
        itself, which is correct — a source link must not replace Salty Steak
        with a retailer's page — but it left the links inert. This hands the
        address to Windows, which opens whatever browser the user has chosen.

        It is deliberately not the automation capability: the model is not
        doing this, the person clicking is, so it needs no grant. It is also
        deliberately narrow — http and https only, so nothing here can be
        talked into launching a file, a UNC path or a custom scheme.
        """

        address = str(payload.get("url") or "").strip()
        parsed = urllib.parse.urlsplit(address)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only http and https addresses can be opened.")
        if any(character in address for character in (chr(13), chr(10), chr(0))):
            raise ValueError("That address is not a single line.")
        if os.name == "nt":
            os.startfile(address)
        else:
            subprocess.Popen(["xdg-open", address])
        return {"opened": True, "url": address}

    def open_path(self, value: str | Path) -> None:
        path = Path(value).resolve()
        allowed = {
            candidate.resolve()
            for candidate in (
                self.config.project_root,
                *self.paths.directories(),
            )
            if candidate.exists()
        }
        allowed.update(
            Path(version["checkpoint_path"]).resolve()
            for version in self.list_versions()
        )
        allowed.update(
            Path(dataset["source_path"]).resolve()
            for dataset in self.list_datasets()
            if Path(dataset["source_path"]).exists()
        )
        if path not in allowed and not any(parent in path.parents for parent in allowed):
            raise ValueError("Path is not part of this Salty Steak project")
        target = path if path.is_dir() else path.parent
        if os.name == "nt":
            os.startfile(str(target))
        else:
            subprocess.Popen(["xdg-open", str(target)])
