"""SQLite control plane for the Salty Steak application.

The database contains application records and file references only. Model
weights, prepared arrays, reports, and conversation exports stay as normal
files under the configured workspace.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar


SCHEMA_VERSION = 12
OPERATION_STATES = frozenset(
    {"queued", "running", "stop_requested", "completed", "interrupted", "failed"}
)
ACTIVE_OPERATION_STATES = frozenset({"queued", "running", "stop_requested"})
TERMINAL_OPERATION_STATES = frozenset({"completed", "interrupted", "failed"})

_T = TypeVar("_T")

_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS application_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    type TEXT NOT NULL,
    target_id TEXT,
    dedupe_key TEXT,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK(state IN ('queued','running','stop_requested','completed','interrupted','failed')),
    phase TEXT NOT NULL DEFAULT 'Waiting',
    current_progress REAL NOT NULL DEFAULT 0 CHECK(current_progress >= 0),
    total_progress REAL CHECK(total_progress IS NULL OR total_progress >= 0),
    worker_pid INTEGER,
    started_at TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT,
    error_json TEXT,
    CHECK(total_progress IS NULL OR current_progress <= total_progress),
    CHECK(
        (state IN ('queued','running','stop_requested') AND finished_at IS NULL)
        OR (state IN ('completed','interrupted','failed') AND finished_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_operations_state_updated
    ON operations(state, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_operations_target
    ON operations(type, target_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_operations_active_dedupe
    ON operations(type, dedupe_key)
    WHERE dedupe_key IS NOT NULL
      AND state IN ('queued','running','stop_requested');

CREATE TABLE IF NOT EXISTS operation_requests (
    request_key TEXT PRIMARY KEY CHECK(length(request_key) BETWEEN 8 AND 200),
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_operation_requests_operation
    ON operation_requests(operation_id);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    source_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('txt','jsonl','json','csv','parquet')),
    encoding TEXT,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    source_mtime_ns INTEGER NOT NULL,
    source_checksum TEXT NOT NULL CHECK(length(source_checksum) = 64),
    language TEXT NOT NULL,
    purpose TEXT NOT NULL,
    description TEXT,
    mapping_json TEXT NOT NULL,
    record_count INTEGER CHECK(record_count IS NULL OR record_count >= 0),
    token_count INTEGER CHECK(token_count IS NULL OR token_count >= 0),
    validation_status TEXT NOT NULL DEFAULT 'not_validated'
        CHECK(validation_status IN ('not_validated','validating','valid','invalid','stale','failed')),
    validation_summary_json TEXT,
    prepared_status TEXT NOT NULL DEFAULT 'not_prepared'
        CHECK(prepared_status IN ('not_prepared','preparing','ready','stale','failed')),
    source_changed INTEGER NOT NULL DEFAULT 0 CHECK(source_changed IN (0,1)),
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_datasets_created ON datasets(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_datasets_source ON datasets(source_path);

CREATE TABLE IF NOT EXISTS prepared_datasets (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL,
    source_checksum TEXT NOT NULL CHECK(length(source_checksum) = 64),
    path TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    tokenizer_reference TEXT,
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    token_count INTEGER NOT NULL CHECK(token_count >= 0),
    train_record_count INTEGER NOT NULL CHECK(train_record_count >= 0),
    validation_record_count INTEGER NOT NULL CHECK(validation_record_count >= 0),
    artifact_checksum TEXT NOT NULL CHECK(length(artifact_checksum) = 64),
    verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
    status TEXT NOT NULL CHECK(status IN ('preparing','ready','stale','failed')),
    created_at TEXT NOT NULL,
    verified_at TEXT,
    UNIQUE(dataset_id, id)
);
CREATE INDEX IF NOT EXISTS ix_prepared_dataset_status
    ON prepared_datasets(dataset_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS training_operations (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
    prepared_dataset_id TEXT REFERENCES prepared_datasets(id) ON DELETE SET NULL,
    starting_version_id TEXT REFERENCES saved_versions(id) ON DELETE SET NULL,
    result_version_id TEXT REFERENCES saved_versions(id) ON DELETE SET NULL,
    requested_steps INTEGER NOT NULL CHECK(requested_steps > 0),
    completed_steps INTEGER NOT NULL DEFAULT 0 CHECK(completed_steps >= 0),
    starting_total_steps INTEGER CHECK(starting_total_steps IS NULL OR starting_total_steps >= 0),
    settings_json TEXT NOT NULL,
    recovery_path TEXT,
    post_training_policy TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(post_training_policy IN ('save_only','evaluate','evaluate_and_activate','legacy_unknown')),
    post_training_state TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(post_training_state IN ('not_started','saving','saved','evaluating','evaluated','activating','completed','needs_attention','legacy_unknown')),
    post_training_recovery_state TEXT NOT NULL DEFAULT 'legacy_unknown'
        CHECK(post_training_recovery_state IN ('active','resume_pending','complete','manual_only','legacy_unknown')),
    required_evaluation_id TEXT REFERENCES evaluations(id) ON DELETE SET NULL,
    activation_operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL,
    activation_attempt_id TEXT,
    confirmed_runtime_id TEXT,
    post_training_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_training_post_training
    ON training_operations(post_training_state, post_training_recovery_state, updated_at DESC);

CREATE TABLE IF NOT EXISTS saved_versions (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    training_operation_id TEXT REFERENCES training_operations(id) ON DELETE SET NULL,
    prepared_dataset_id TEXT REFERENCES prepared_datasets(id) ON DELETE SET NULL,
    label TEXT NOT NULL,
    checkpoint_path TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL CHECK(length(checksum) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    total_trained_steps INTEGER CHECK(total_trained_steps IS NULL OR total_trained_steps >= 0),
    additional_steps INTEGER NOT NULL DEFAULT 0 CHECK(additional_steps >= 0),
    architecture_revision INTEGER NOT NULL CHECK(architecture_revision > 0),
    context_tokens INTEGER NOT NULL CHECK(context_tokens > 0),
    tokenizer_checksum TEXT NOT NULL CHECK(length(tokenizer_checksum) = 64),
    integrity TEXT NOT NULL CHECK(integrity IN ('verified','failed')),
    imported INTEGER NOT NULL DEFAULT 0 CHECK(imported IN (0,1)),
    created_at TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_versions_verified
    ON saved_versions(verified_at DESC);

CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
    saved_version_id TEXT NOT NULL REFERENCES saved_versions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','interrupted','failed')),
    records_completed INTEGER NOT NULL DEFAULT 0 CHECK(records_completed >= 0),
    total_records INTEGER CHECK(total_records IS NULL OR total_records >= 0),
    metrics_json TEXT,
    result_path TEXT,
    result_checksum TEXT CHECK(result_checksum IS NULL OR length(result_checksum) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_evaluations_version
    ON evaluations(saved_version_id, created_at DESC);

CREATE TABLE IF NOT EXISTS runtime_states (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT UNIQUE REFERENCES operations(id) ON DELETE SET NULL,
    saved_version_id TEXT NOT NULL REFERENCES saved_versions(id) ON DELETE CASCADE,
    artifact_path TEXT NOT NULL,
    artifact_checksum TEXT NOT NULL CHECK(length(artifact_checksum) = 64),
    state TEXT NOT NULL CHECK(state IN ('preparing','verified','loaded','failed')),
    worker_pid INTEGER,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    loaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_runtime_version
    ON runtime_states(saved_version_id, created_at DESC);

CREATE TABLE IF NOT EXISTS active_runtime (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    saved_version_id TEXT NOT NULL REFERENCES saved_versions(id) ON DELETE RESTRICT,
    runtime_id TEXT NOT NULL REFERENCES runtime_states(id) ON DELETE RESTRICT,
    activated_by_operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_runtime_states (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('saved_version','model_bundle')),
    target_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('preparing','verified','loaded','failed','unloaded')),
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    loaded_at TEXT,
    unloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_chat_runtime_target
    ON chat_runtime_states(target_kind, target_id, created_at DESC);

CREATE TABLE IF NOT EXISTS active_chat_runtime (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    runtime_id TEXT NOT NULL REFERENCES chat_runtime_states(id) ON DELETE RESTRICT,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('saved_version','model_bundle')),
    target_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activation_attempts (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    saved_version_id TEXT NOT NULL REFERENCES saved_versions(id) ON DELETE CASCADE,
    runtime_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('preparing','loaded','verified','committed','failed')),
    expected_json TEXT NOT NULL,
    actual_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_activation_attempts_version
    ON activation_attempts(saved_version_id, created_at DESC);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversations_updated
    ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    saved_version_id TEXT REFERENCES saved_versions(id) ON DELETE SET NULL,
    runtime_id TEXT REFERENCES runtime_states(id) ON DELETE SET NULL,
    target_kind TEXT CHECK(target_kind IS NULL OR target_kind IN ('saved_version','model_bundle')),
    target_id TEXT,
    runtime_profile_id TEXT,
    runtime_instance_id TEXT,
    source_sha256 TEXT CHECK(source_sha256 IS NULL OR length(source_sha256) = 64),
    technical_details_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation
    ON messages(conversation_id, sequence);

CREATE TABLE IF NOT EXISTS chat_artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind = 'image'),
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type = 'image/png'),
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    width INTEGER NOT NULL CHECK(width > 0),
    height INTEGER NOT NULL CHECK(height > 0),
    provenance_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_artifacts_conversation
    ON chat_artifacts(conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY
        CHECK(length(id) = 36 AND substr(id, 9, 1) = '-'
              AND substr(id, 14, 1) = '-' AND substr(id, 19, 1) = '-'
              AND substr(id, 24, 1) = '-'),
    operation_id TEXT REFERENCES operations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('success','information','warning','error')),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    duration_seconds INTEGER CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_created
    ON notifications(created_at DESC);

CREATE TABLE IF NOT EXISTS configuration_references (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    checksum TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operation_events (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    event_type TEXT NOT NULL,
    state TEXT,
    phase TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(operation_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_operation_events_operation
    ON operation_events(operation_id, sequence);

CREATE TABLE IF NOT EXISTS finalisation_attempts (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    checkpoint_id TEXT NOT NULL,
    checkpoint_path TEXT NOT NULL,
    result_path TEXT NOT NULL UNIQUE,
    package_build_id TEXT NOT NULL,
    worker_pid INTEGER,
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    phase TEXT NOT NULL,
    finished_at TEXT,
    result_status TEXT NOT NULL
        CHECK(result_status IN ('running','passed','failed','timed_out','invalidated')),
    invalidated_at TEXT,
    invalidation_reason TEXT,
    UNIQUE(operation_id, id)
);
CREATE INDEX IF NOT EXISTS ix_finalisation_attempts_operation
    ON finalisation_attempts(operation_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_finalisation_attempt_current
    ON finalisation_attempts(operation_id)
    WHERE result_status = 'running' AND invalidated_at IS NULL;

CREATE TABLE IF NOT EXISTS dataset_lineages (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE RESTRICT,
    prepared_dataset_id TEXT NOT NULL REFERENCES prepared_datasets(id) ON DELETE RESTRICT,
    dataset_fingerprint TEXT NOT NULL,
    prepared_fingerprint TEXT NOT NULL,
    tokenizer_fingerprint TEXT NOT NULL,
    architecture_identity TEXT NOT NULL,
    baseline_version_id TEXT REFERENCES saved_versions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE(
        dataset_fingerprint, prepared_fingerprint, tokenizer_fingerprint,
        architecture_identity, baseline_version_id
    )
);

CREATE TABLE IF NOT EXISTS version_lineage (
    saved_version_id TEXT NOT NULL REFERENCES saved_versions(id) ON DELETE CASCADE,
    lineage_id TEXT NOT NULL REFERENCES dataset_lineages(id) ON DELETE RESTRICT,
    parent_version_id TEXT,
    role TEXT NOT NULL CHECK(role IN ('baseline','trained')),
    verification_attempt_id TEXT REFERENCES finalisation_attempts(id) ON DELETE RESTRICT,
    recovered_after_restart INTEGER NOT NULL DEFAULT 0 CHECK(recovered_after_restart IN (0,1)),
    user_protected INTEGER NOT NULL DEFAULT 0 CHECK(user_protected IN (0,1)),
    cleanup_status TEXT NOT NULL DEFAULT 'retained'
        CHECK(cleanup_status IN ('retained','pending','failed')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(lineage_id, saved_version_id)
);
CREATE INDEX IF NOT EXISTS ix_version_lineage_sequence
    ON version_lineage(lineage_id, created_at DESC);

CREATE TABLE IF NOT EXISTS retention_journal (
    id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL REFERENCES dataset_lineages(id) ON DELETE RESTRICT,
    saved_version_id TEXT NOT NULL,
    operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK(state IN ('planned','moving','deleted','failed')),
    paths_json TEXT NOT NULL,
    expected_bytes INTEGER NOT NULL CHECK(expected_bytes >= 0),
    actual_bytes INTEGER,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_retention_journal_lineage
    ON retention_journal(lineage_id, created_at DESC);

CREATE TABLE IF NOT EXISTS dataset_semantics (
    dataset_id TEXT PRIMARY KEY REFERENCES datasets(id) ON DELETE CASCADE,
    object_kind TEXT NOT NULL
        CHECK(object_kind IN ('raw_source','training_source','evaluation_source')),
    display_name TEXT NOT NULL,
    preparation_adapter TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prepared_artifact_policies (
    prepared_dataset_id TEXT PRIMARY KEY
        REFERENCES prepared_datasets(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK(classification IN (
            'verified_training','training_only_replay','evaluation_only',
            'blocked_legacy'
        )),
    display_name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    training_allowed INTEGER NOT NULL CHECK(training_allowed IN (0,1)),
    evaluation_allowed INTEGER NOT NULL CHECK(evaluation_allowed IN (0,1)),
    selectable INTEGER NOT NULL CHECK(selectable IN (0,1)),
    deletion_protected INTEGER NOT NULL CHECK(deletion_protected IN (0,1)),
    evidence_path TEXT,
    evidence_sha256 TEXT
        CHECK(evidence_sha256 IS NULL OR length(evidence_sha256) = 64),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prepared_artifact_policy_classification
    ON prepared_artifact_policies(classification, updated_at DESC);

CREATE TABLE IF NOT EXISTS saved_version_policies (
    saved_version_id TEXT PRIMARY KEY
        REFERENCES saved_versions(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK(classification IN (
            'normal','recommended_fallback','recovered_candidate',
            'experimental','regressed_forensic','rollback'
        )),
    friendly_name TEXT NOT NULL,
    activation_allowed INTEGER NOT NULL CHECK(activation_allowed IN (0,1)),
    continuation_allowed INTEGER NOT NULL CHECK(continuation_allowed IN (0,1)),
    deletion_protected INTEGER NOT NULL CHECK(deletion_protected IN (0,1)),
    recommended INTEGER NOT NULL CHECK(recommended IN (0,1)),
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saved_version_policy_classification
    ON saved_version_policies(classification, recommended DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS training_operation_policies (
    training_operation_id TEXT PRIMARY KEY
        REFERENCES training_operations(id) ON DELETE CASCADE,
    classification TEXT NOT NULL
        CHECK(classification IN (
            'normal','recovery_experiment','regressed_forensic'
        )),
    visibility TEXT NOT NULL CHECK(visibility IN ('primary','history')),
    action_policy TEXT NOT NULL
        CHECK(action_policy IN ('normal','forensic_read_only')),
    display_name TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_training_operation_policy_visibility
    ON training_operation_policies(visibility, classification, updated_at DESC);

CREATE TABLE IF NOT EXISTS version_quality_evidence (
    saved_version_id TEXT NOT NULL
        REFERENCES saved_versions(id) ON DELETE CASCADE,
    family TEXT NOT NULL,
    loss REAL,
    baseline_loss REAL,
    regression_percent REAL,
    passed INTEGER CHECK(passed IS NULL OR passed IN (0,1)),
    result_path TEXT NOT NULL,
    result_sha256 TEXT NOT NULL CHECK(length(result_sha256) = 64),
    metrics_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY(saved_version_id, family)
);
CREATE INDEX IF NOT EXISTS ix_version_quality_family
    ON version_quality_evidence(family, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS recovery_artifacts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL
        CHECK(kind IN (
            'corrected_instruction','english_anchor','english_replay',
            'training_candidate','compatibility_report'
        )),
    display_name TEXT NOT NULL,
    path TEXT NOT NULL,
    artifact_digest TEXT NOT NULL CHECK(length(artifact_digest) = 64),
    training_allowed INTEGER NOT NULL CHECK(training_allowed IN (0,1)),
    evaluation_only INTEGER NOT NULL CHECK(evaluation_only IN (0,1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_recovery_artifact_kind
    ON recovery_artifacts(kind, updated_at DESC);

CREATE TABLE IF NOT EXISTS plugin_connectors (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL
        CHECK(provider IN ('gmail','google_calendar','icloud_calendar','mcp')),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'disconnected'
        CHECK(status IN ('disconnected','configured','connected','degraded','error','disabled')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    transport TEXT NOT NULL,
    permission_scopes_json TEXT NOT NULL DEFAULT '[]',
    granted_scopes_json TEXT NOT NULL DEFAULT '[]',
    configuration_json TEXT NOT NULL DEFAULT '{}',
    credential_storage TEXT NOT NULL DEFAULT 'none'
        CHECK(credential_storage IN (
            'none','windows_credential_manager','dpapi_protected_file','environment'
        )),
    credential_reference TEXT,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plugin_connectors_status
    ON plugin_connectors(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS automation_grants (
    id TEXT PRIMARY KEY,
    capability TEXT NOT NULL UNIQUE
        CHECK(capability IN (
            'terminal.execute','screen.capture','input.control',
            'application.launch','window.control','ui.automation'
        )),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    constraints_json TEXT NOT NULL DEFAULT '{}',
    granted_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_automation_grants_enabled
    ON automation_grants(enabled, capability);

CREATE TABLE IF NOT EXISTS automation_audit_records (
    id TEXT PRIMARY KEY,
    event TEXT NOT NULL CHECK(event IN ('grant','revoke','invoke')),
    capability TEXT NOT NULL
        CHECK(capability IN (
            'terminal.execute','screen.capture','input.control',
            'application.launch','window.control','ui.automation'
        )),
    outcome TEXT NOT NULL
        CHECK(outcome IN (
            'pending','granted','revoked','succeeded','failed','timed_out','denied'
        )),
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_automation_audit_created
    ON automation_audit_records(created_at DESC, capability);
CREATE TRIGGER IF NOT EXISTS trg_automation_audit_append_only_update
BEFORE UPDATE ON automation_audit_records
FOR EACH ROW
WHEN NEW.id IS NOT OLD.id
  OR NEW.event IS NOT OLD.event
  OR NEW.capability IS NOT OLD.capability
  OR NEW.request_json IS NOT OLD.request_json
  OR OLD.outcome <> 'pending'
  OR NEW.outcome = 'pending'
  OR NEW.completed_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'automation audit records are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_automation_audit_append_only_delete
BEFORE DELETE ON automation_audit_records
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'automation audit records are append-only');
END;
"""

_IMMUTABLE_TABLES = (
    "operations",
    "datasets",
    "prepared_datasets",
    "training_operations",
    "saved_versions",
    "evaluations",
    "runtime_states",
    "activation_attempts",
    "conversations",
    "messages",
    "chat_artifacts",
    "notifications",
    "operation_events",
    "finalisation_attempts",
    "dataset_lineages",
    "version_lineage",
    "retention_journal",
    "plugin_connectors",
    "automation_grants",
    "automation_audit_records",
)


class DatabaseError(RuntimeError):
    """Raised for an invalid or incompatible control database."""


class StateConflict(DatabaseError):
    """Raised when an atomic state transition loses a race."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _immutable_triggers() -> str:
    statements: list[str] = []
    for table in _IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_identity
            BEFORE UPDATE OF id, created_at ON {table}
            FOR EACH ROW
            WHEN NEW.id <> OLD.id OR NEW.created_at <> OLD.created_at
            BEGIN
                SELECT RAISE(ABORT, 'immutable identity');
            END;
            """
        )
    statements.append(
        """
        CREATE TRIGGER IF NOT EXISTS trg_saved_versions_immutable_steps
        BEFORE UPDATE OF total_trained_steps ON saved_versions
        FOR EACH ROW
        WHEN NEW.total_trained_steps IS NOT OLD.total_trained_steps
        BEGIN
            SELECT RAISE(ABORT, 'immutable total trained steps');
        END;
        """
    )
    return "\n".join(statements)


class Database:
    """Short-lived SQLite connections with explicit transactional helpers."""

    def __init__(self, path: str | Path, *, initialize: bool = True) -> None:
        self.path = Path(path).resolve()
        if initialize:
            self.initialize()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if not readonly:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextlib.contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect(readonly=readonly)
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            existing = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if existing and "application_metadata" not in existing:
                raise DatabaseError(
                    "Refusing to reuse a database not created by the clean application"
                )
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, SCHEMA_VERSION):
                raise DatabaseError(
                    f"Unsupported database schema {version}; expected {SCHEMA_VERSION}"
                )







            if (
                version in (1, 2)
                and "training_operations" in existing
                and "operations" in existing
            ):
                self._migrate_training_policy(connection)
            if version == 6 and "plugin_connectors" in existing:
                self._migrate_plugin_connector_secret_storage(connection)
            rebuild_automation = (
                "automation_grants" in existing
                and self._automation_capabilities_are_stale(connection)
            )
            if rebuild_automation:
                self._rename_legacy_automation_tables(connection)
            connection.executescript(_SCHEMA + _immutable_triggers())
            if rebuild_automation:
                self._restore_legacy_automation_rows(connection)
            self._migrate_training_policy(connection)
            self._migrate_chat_runtime_provenance(connection)
            now = utc_now()
            connection.execute(
                "INSERT OR IGNORE INTO application_metadata(key, value) VALUES (?, ?)",
                ("installation_id", new_id()),
            )
            connection.execute(
                "INSERT OR REPLACE INTO application_metadata(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _automation_capabilities_are_stale(connection: sqlite3.Connection) -> bool:
        """Report whether the stored automation tables predate the new capabilities."""

        definition = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'automation_grants'"
        ).fetchone()
        return definition is not None and "ui.automation" not in str(definition[0])

    @staticmethod
    def _rename_legacy_automation_tables(connection: sqlite3.Connection) -> None:
        """Move the narrow-constraint automation tables aside for rebuilding.

        SQLite cannot alter a CHECK constraint in place.  The tables are renamed
        here so the ordinary schema pass recreates them with the widened
        constraint, and the retained rows are copied back afterwards.
        """





        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall():
            escaped = str(row[0]).replace('"', '""')
            connection.execute(f'DROP TRIGGER "{escaped}"')
        connection.execute("DROP INDEX IF EXISTS ix_automation_grants_enabled")
        connection.execute("DROP INDEX IF EXISTS ix_automation_audit_created")
        connection.execute(
            "ALTER TABLE automation_grants RENAME TO automation_grants_superseded"
        )
        connection.execute(
            "ALTER TABLE automation_audit_records "
            "RENAME TO automation_audit_records_superseded"
        )

    @staticmethod
    def _restore_legacy_automation_rows(connection: sqlite3.Connection) -> None:
        """Copy retained grants and audit history into the rebuilt tables."""

        connection.execute(
            """
            INSERT INTO automation_grants(
                id, capability, enabled, constraints_json,
                granted_at, revoked_at, created_at, updated_at
            )
            SELECT id, capability, enabled, constraints_json,
                   granted_at, revoked_at, created_at, updated_at
            FROM automation_grants_superseded
            """
        )
        connection.execute(
            """
            INSERT INTO automation_audit_records(
                id, event, capability, outcome, request_json,
                result_json, error_json, created_at, completed_at
            )
            SELECT id, event, capability, outcome, request_json,
                   result_json, error_json, created_at, completed_at
            FROM automation_audit_records_superseded
            """
        )
        connection.execute("DROP TABLE automation_grants_superseded")
        connection.execute("DROP TABLE automation_audit_records_superseded")

    @staticmethod
    def _migrate_plugin_connector_secret_storage(
        connection: sqlite3.Connection,
    ) -> None:
        """Rebuild the v6 connector table with the DPAPI storage value."""

        connection.execute("BEGIN IMMEDIATE")
        try:



            trigger_names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            ]
            for trigger_name in trigger_names:
                escaped = trigger_name.replace('"', '""')
                connection.execute(f'DROP TRIGGER "{escaped}"')
            connection.execute("DROP INDEX IF EXISTS ix_plugin_connectors_status")
            connection.execute(
                "ALTER TABLE plugin_connectors RENAME TO plugin_connectors_v6"
            )
            connection.execute(
                """
                CREATE TABLE plugin_connectors (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL
                        CHECK(provider IN (
                            'gmail','google_calendar','icloud_calendar','mcp'
                        )),
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'disconnected'
                        CHECK(status IN (
                            'disconnected','configured','connected','degraded',
                            'error','disabled'
                        )),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    transport TEXT NOT NULL,
                    permission_scopes_json TEXT NOT NULL DEFAULT '[]',
                    granted_scopes_json TEXT NOT NULL DEFAULT '[]',
                    configuration_json TEXT NOT NULL DEFAULT '{}',
                    credential_storage TEXT NOT NULL DEFAULT 'none'
                        CHECK(credential_storage IN (
                            'none','windows_credential_manager',
                            'dpapi_protected_file','environment'
                        )),
                    credential_reference TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO plugin_connectors(
                    id, provider, display_name, status, enabled, transport,
                    permission_scopes_json, granted_scopes_json,
                    configuration_json, credential_storage,
                    credential_reference, last_error_json, created_at, updated_at
                )
                SELECT
                    id, provider, display_name, status, enabled, transport,
                    permission_scopes_json, granted_scopes_json,
                    configuration_json, credential_storage,
                    credential_reference, last_error_json, created_at, updated_at
                FROM plugin_connectors_v6
                """
            )
            connection.execute("DROP TABLE plugin_connectors_v6")
            connection.execute(
                """
                CREATE INDEX ix_plugin_connectors_status
                ON plugin_connectors(status, updated_at DESC)
                """
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _migrate_chat_runtime_provenance(
        connection: sqlite3.Connection,
    ) -> None:
        """Add general Chat target identity without faking training lineage."""

        message_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        additions = {
            "target_kind": (
                "TEXT CHECK(target_kind IS NULL OR "
                "target_kind IN ('saved_version','model_bundle'))"
            ),
            "target_id": "TEXT",
            "runtime_profile_id": "TEXT",
            "runtime_instance_id": "TEXT",
            "source_sha256": (
                "TEXT CHECK(source_sha256 IS NULL OR length(source_sha256) = 64)"
            ),
        }
        for name, declaration in additions.items():
            if name not in message_columns:
                connection.execute(
                    f"ALTER TABLE messages ADD COLUMN {name} {declaration}"
                )

        connection.execute(
            """
            INSERT OR IGNORE INTO chat_runtime_states(
                id, operation_id, target_kind, target_id, profile_id,
                artifact_path, source_sha256, state, metadata_json,
                created_at, verified_at, loaded_at
            )
            SELECT id, operation_id, 'saved_version', saved_version_id,
                   'legacy_saved_version', artifact_path, artifact_checksum,
                   state, metadata_json, created_at, verified_at, loaded_at
            FROM runtime_states
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO active_chat_runtime(
                singleton, runtime_id, target_kind, target_id, profile_id,
                source_sha256, updated_at
            )
            SELECT 1, a.runtime_id, 'saved_version', a.saved_version_id,
                   'legacy_saved_version', r.artifact_checksum, a.updated_at
            FROM active_runtime a
            JOIN runtime_states r ON r.id = a.runtime_id
            WHERE a.singleton = 1
            """
        )

    @staticmethod
    def _migrate_training_policy(connection: sqlite3.Connection) -> None:
        """Add the post-training state machine without rewriting model files.

        v2 databases predate the durable policy selector.  Migration is
        additive and deliberately conservative: a historical operation with
        no explicit policy is marked ``legacy_unknown`` and can only be
        resumed by an explicit user action.
        """

        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(training_operations)"
            ).fetchall()
        }
        additions = {
            "post_training_policy": (
                "TEXT NOT NULL DEFAULT 'legacy_unknown' "
                "CHECK(post_training_policy IN "
                "('save_only','evaluate','evaluate_and_activate','legacy_unknown'))"
            ),
            "post_training_state": (
                "TEXT NOT NULL DEFAULT 'legacy_unknown' "
                "CHECK(post_training_state IN "
                "('not_started','saving','saved','evaluating','evaluated',"
                "'activating','completed','needs_attention','legacy_unknown'))"
            ),
            "post_training_recovery_state": (
                "TEXT NOT NULL DEFAULT 'legacy_unknown' "
                "CHECK(post_training_recovery_state IN "
                "('active','resume_pending','complete','manual_only','legacy_unknown'))"
            ),
            "required_evaluation_id": (
                "TEXT REFERENCES evaluations(id) ON DELETE SET NULL"
            ),
            "activation_operation_id": (
                "TEXT REFERENCES operations(id) ON DELETE SET NULL"
            ),
            "activation_attempt_id": "TEXT",
            "confirmed_runtime_id": "TEXT",
            "post_training_error_json": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE training_operations ADD COLUMN {name} {declaration}"
                )




        rows = connection.execute(
            """
            SELECT t.id, t.settings_json, t.result_version_id,
                   o.result_json, o.state
            FROM training_operations t
            JOIN operations o ON o.id = t.operation_id
            WHERE t.post_training_policy = 'legacy_unknown'
            """
        ).fetchall()
        def _safe(value: str | None) -> Any:
            try:
                return parse_json(value, {})
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
        for row in rows:
            settings = _safe(row["settings_json"])
            result = _safe(row["result_json"])
            if not isinstance(settings, dict):
                settings = {}
            if not isinstance(result, dict):
                result = {}
            raw = settings.get("post_training_policy")
            if isinstance(raw, str) and raw in {
                "save_only",
                "evaluate",
                "evaluate_and_activate",
            }:
                policy = raw
            elif isinstance(settings.get("use_completed_version_in_chat"), bool):
                policy = (
                    "evaluate_and_activate"
                    if settings["use_completed_version_in_chat"]
                    else "save_only"
                )
            elif isinstance(result.get("use_completed_version_in_chat"), bool):
                policy = (
                    "evaluate_and_activate"
                    if result["use_completed_version_in_chat"]
                    else "save_only"
                )
            else:
                policy = "legacy_unknown"

            if policy == "legacy_unknown":
                state = "legacy_unknown"
                recovery = "legacy_unknown"
            elif policy == "save_only":
                state = "saved" if row["result_version_id"] else "not_started"
                recovery = "manual_only" if row["result_version_id"] else "legacy_unknown"
            else:


                state = "needs_attention" if row["result_version_id"] else "not_started"
                recovery = "manual_only" if row["result_version_id"] else "legacy_unknown"
            connection.execute(
                """
                UPDATE training_operations
                SET post_training_policy = ?, post_training_state = ?,
                    post_training_recovery_state = ?
                WHERE id = ?
                """,
                (policy, state, recovery, row["id"]),
            )

    def append_operation_event(
        self,
        operation_id: str,
        event_type: str,
        *,
        state: str | None = None,
        phase: str | None = None,
        evidence: Any = None,
        connection: sqlite3.Connection | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Append one immutable, ordered lifecycle event."""

        def append(target: sqlite3.Connection) -> dict[str, Any]:
            timestamp = created_at or utc_now()
            sequence = int(
                target.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM operation_events WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()[0]
            )
            identifier = new_id()
            target.execute(
                """
                INSERT INTO operation_events(
                    id, operation_id, sequence, event_type, state, phase,
                    evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    operation_id,
                    sequence,
                    event_type,
                    state,
                    phase,
                    json_text(evidence) if evidence is not None else None,
                    timestamp,
                ),
            )
            return {
                "id": identifier,
                "operation_id": operation_id,
                "sequence": sequence,
                "event_type": event_type,
                "state": state,
                "phase": phase,
                "evidence": evidence,
                "created_at": timestamp,
            }

        if connection is not None:
            return append(connection)
        with self.transaction() as target:
            return append(target)

    def run_transaction(
        self,
        callback: Callable[[sqlite3.Connection], _T],
        *,
        immediate: bool = True,
    ) -> _T:
        with self.transaction(immediate=immediate) as connection:
            return callback(connection)

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def fetch_one(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> dict[str, Any] | None:
        with self.connection(readonly=True) as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row is not None else None

    def fetch_all(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        with self.connection(readonly=True) as connection:
            return [
                dict(row) for row in connection.execute(sql, parameters).fetchall()
            ]

    def create_operation(
        self,
        operation_type: str,
        *,
        target_id: str | None = None,
        dedupe_key: str | None = None,
        phase: str = "Waiting",
        total_progress: float | None = None,
        operation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an operation, or return its active deduplicated peer."""

        identifier = operation_id or new_id()
        now = utc_now()
        with self.transaction() as connection:
            if dedupe_key is not None:
                existing = connection.execute(
                    """
                    SELECT * FROM operations
                    WHERE type = ? AND dedupe_key = ?
                      AND state IN ('queued','running','stop_requested')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (operation_type, dedupe_key),
                ).fetchone()
                if existing is not None:
                    return self.operation_record(existing), False
            try:
                connection.execute(
                    """
                    INSERT INTO operations(
                        id, type, target_id, dedupe_key, state, phase,
                        current_progress, total_progress, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?, ?)
                    """,
                    (
                        identifier,
                        operation_type,
                        target_id,
                        dedupe_key,
                        phase,
                        total_progress,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if dedupe_key is None:
                    raise
                existing = connection.execute(
                    """
                    SELECT * FROM operations
                    WHERE type = ? AND dedupe_key = ?
                      AND state IN ('queued','running','stop_requested')
                    LIMIT 1
                    """,
                    (operation_type, dedupe_key),
                ).fetchone()
                if existing is None:
                    raise
                return self.operation_record(existing), False
            created = connection.execute(
                "SELECT * FROM operations WHERE id = ?", (identifier,)
            ).fetchone()
            assert created is not None
            self.append_operation_event(
                identifier,
                "operation_queued",
                state="queued",
                phase=phase,
                evidence={
                    "operation_type": operation_type,
                    "target_id": target_id,
                },
                connection=connection,
                created_at=now,
            )
            return self.operation_record(created), True

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT * FROM operations WHERE id = ?", (operation_id,))
        return self.operation_record(row) if row else None

    def list_operations(
        self,
        *,
        states: Sequence[str] | None = None,
        operation_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if states:
            invalid = set(states) - OPERATION_STATES
            if invalid:
                raise ValueError(f"Unknown operation states: {sorted(invalid)}")
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            parameters.extend(states)
        if operation_type:
            clauses.append("type = ?")
            parameters.append(operation_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(limit, 1000)))
        rows = self.fetch_all(
            f"SELECT * FROM operations {where} ORDER BY created_at DESC LIMIT ?",
            parameters,
        )
        return [self.operation_record(row) for row in rows]

    @staticmethod
    def operation_record(row: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(row)
        record["result"] = parse_json(record.pop("result_json", None))
        record["error"] = parse_json(record.pop("error_json", None))
        phase = str(record.get("phase") or "").casefold()
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        error = record.get("error") if isinstance(record.get("error"), dict) else {}
        declared_outcome = result.get("completion_outcome") or result.get("outcome")
        if declared_outcome in {
            "saved",
            "evaluated",
            "completed",
            "needs_attention",
            "cancelled",
            "recovered_and_completed",
        }:
            record["outcome"] = str(declared_outcome)
        elif result.get("recovered_after_restart") or "recovered and completed" in phase:
            record["outcome"] = "recovered_and_completed"
        elif record.get("state") == "completed":
            record["outcome"] = "completed_normally"
        elif error.get("code") in {"finalisation_timeout", "interpreter_bootstrap_timeout"}:
            record["outcome"] = "timed_out"
        elif "attention" in phase:
            record["outcome"] = "needs_attention"
        elif record.get("state") == "interrupted":
            record["outcome"] = "interrupted"
        else:
            record["outcome"] = record.get("state")
        return record

    def set_config_reference(
        self,
        key: str,
        value: str,
        *,
        checksum: str | None = None,
    ) -> None:
        now = utc_now()
        self.execute(
            """
            INSERT INTO configuration_references(key, value, checksum, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                checksum = excluded.checksum,
                updated_at = excluded.updated_at
            """,
            (key, value, checksum, now),
        )

    def foreign_key_violations(self) -> list[dict[str, Any]]:
        return self.fetch_all("PRAGMA foreign_key_check")
