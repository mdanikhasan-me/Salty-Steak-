"""Central enforcement for production recovery classifications.

Rows absent from the additive policy tables retain the legacy supported
behaviour.  Explicit recovery classifications always win and are enforced by
backend actions, not merely hidden by the frontend.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..database.control import Database, parse_json


class ProductionStatePolicy:
    def __init__(self, database: Database) -> None:
        self.database = database

    def prepared(self, prepared_dataset_id: str | None) -> dict[str, Any]:
        if not prepared_dataset_id:
            return {
                "classification": "unregistered",
                "display_name": None,
                "purpose": None,
                "training_allowed": True,
                "evaluation_allowed": True,
                "selectable": True,
                "deletion_protected": False,
                "evidence_path": None,
                "evidence_sha256": None,
                "metadata": {},
                "explicit": False,
            }
        row = self.database.fetch_one(
            """
            SELECT * FROM prepared_artifact_policies
            WHERE prepared_dataset_id = ?
            """,
            (prepared_dataset_id,),
        )
        if row is None:
            return {
                "prepared_dataset_id": prepared_dataset_id,
                "classification": "unregistered",
                "display_name": None,
                "purpose": None,
                "training_allowed": True,
                "evaluation_allowed": True,
                "selectable": True,
                "deletion_protected": False,
                "evidence_path": None,
                "evidence_sha256": None,
                "metadata": {},
                "explicit": False,
            }
        result = dict(row)
        result["metadata"] = parse_json(result.pop("metadata_json"), {})
        for key in (
            "training_allowed",
            "evaluation_allowed",
            "selectable",
            "deletion_protected",
        ):
            result[key] = bool(result[key])
        result["explicit"] = True
        return result

    def version(self, saved_version_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT * FROM saved_version_policies WHERE saved_version_id = ?",
            (saved_version_id,),
        )
        if row is None:
            return {
                "saved_version_id": saved_version_id,
                "classification": "unregistered",
                "friendly_name": None,
                "activation_allowed": True,
                "continuation_allowed": True,
                "deletion_protected": False,
                "recommended": False,
                "rationale": None,
                "evidence": {},
                "explicit": False,
            }
        result = dict(row)
        result["evidence"] = parse_json(result.pop("evidence_json"), {})
        for key in (
            "activation_allowed",
            "continuation_allowed",
            "deletion_protected",
            "recommended",
        ):
            result[key] = bool(result[key])
        result["explicit"] = True
        result["quality_evidence"] = self.quality(saved_version_id)
        return result

    def training(self, training_operation_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            """
            SELECT * FROM training_operation_policies
            WHERE training_operation_id = ?
            """,
            (training_operation_id,),
        )
        if row is None:
            return {
                "training_operation_id": training_operation_id,
                "classification": "unregistered",
                "visibility": "primary",
                "action_policy": "normal",
                "display_name": None,
                "evidence": {},
                "explicit": False,
            }
        result = dict(row)
        result["evidence"] = parse_json(result.pop("evidence_json"), {})
        result["explicit"] = True
        return result

    def training_for_operation(self, operation_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT id FROM training_operations WHERE operation_id = ?",
            (operation_id,),
        )
        if row is None:
            return self.training(operation_id)
        return self.training(str(row["id"]))

    def dataset_semantics(self, dataset_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM dataset_semantics WHERE dataset_id = ?",
            (dataset_id,),
        )
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = parse_json(result.pop("metadata_json"), {})
        return result

    def quality(self, saved_version_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """
            SELECT * FROM version_quality_evidence
            WHERE saved_version_id = ?
            ORDER BY family
            """,
            (saved_version_id,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["passed"] = (
                None if item["passed"] is None else bool(item["passed"])
            )
            item["metrics"] = parse_json(item.pop("metrics_json"), {})
            result.append(item)
        return result

    def enrich_dataset(self, record: dict[str, Any]) -> dict[str, Any]:
        semantics = self.dataset_semantics(str(record["id"]))
        prepared_id = record.get("prepared_dataset_id") or record.get("prepared_id")
        prepared = self.prepared(
            str(prepared_id) if prepared_id is not None else None
        )
        record["semantics"] = semantics
        record["prepared_policy"] = prepared
        if semantics and semantics.get("display_name"):
            record["display_name"] = semantics["display_name"]
            record["object_kind"] = semantics["object_kind"]
        else:
            record["display_name"] = record.get("name")
            record["object_kind"] = "training_source"
        if not prepared["training_allowed"] or not prepared["selectable"]:
            record["training_ready"] = False
            record["training_block_reason"] = prepared["purpose"]
        else:
            record["training_block_reason"] = None
        return record

    def enrich_version(self, record: dict[str, Any]) -> dict[str, Any]:
        policy = self.version(str(record["id"]))
        record["production_policy"] = policy
        record["friendly_name"] = policy.get("friendly_name") or record.get("label")
        record["quality_evidence"] = policy.get("quality_evidence", [])
        return record

    def assert_prepared_training_allowed(self, prepared_dataset_id: str) -> None:
        policy = self.prepared(prepared_dataset_id)
        if not policy["training_allowed"] or not policy["selectable"]:
            raise ValueError(
                "This prepared artifact is preserved for forensic evidence and "
                "is permanently blocked from training."
            )

    def assert_dataset_preparation_allowed(self, dataset_id: str) -> None:
        semantics = self.dataset_semantics(dataset_id)
        if semantics and semantics["object_kind"] == "raw_source":
            raise ValueError(
                "This registry entry preserves the raw OASST2 source identity. "
                "Use the corrected English OASST2 dataset, or register the raw "
                "source with the corrected OASST2 adapter."
            )

    def assert_prepared_evaluation_allowed(self, prepared_dataset_id: str) -> None:
        policy = self.prepared(prepared_dataset_id)
        if not policy["evaluation_allowed"]:
            raise ValueError(
                "This prepared artifact is preserved for forensic evidence and "
                "is permanently blocked from evaluation."
            )

    def assert_version_activation_allowed(self, saved_version_id: str) -> None:
        policy = self.version(saved_version_id)
        if not policy["activation_allowed"]:
            raise ValueError(
                "This scientifically regressed forensic version cannot be used "
                "in Chat. View its evidence or technical details instead."
            )

    def assert_version_continuation_allowed(self, saved_version_id: str) -> None:
        policy = self.version(saved_version_id)
        if not policy["continuation_allowed"]:
            raise ValueError(
                "This scientifically regressed forensic version cannot be "
                "continued. Start from the recommended verified parent."
            )

    def assert_training_action_allowed(self, operation_id: str) -> None:
        policy = self.training_for_operation(operation_id)
        if policy["action_policy"] == "forensic_read_only":
            raise ValueError(
                "This historical malformed-data run is read-only forensic "
                "evidence and cannot be resumed or finalised."
            )

    def current_state(self) -> dict[str, Any]:
        active = self.database.fetch_one(
            """
            SELECT a.saved_version_id,v.checksum,v.label,r.state AS runtime_state,
                   r.id AS runtime_id
            FROM active_runtime a
            JOIN saved_versions v ON v.id=a.saved_version_id
            JOIN runtime_states r ON r.id=a.runtime_id
            """
        )
        recommended = self.database.fetch_one(
            """
            SELECT v.id,v.label,v.checksum,p.friendly_name,p.rationale
            FROM saved_version_policies p
            JOIN saved_versions v ON v.id=p.saved_version_id
            WHERE p.recommended=1
            ORDER BY p.updated_at DESC LIMIT 1
            """
        )
        blocked_versions = self.database.fetch_all(
            """
            SELECT v.id,v.label,p.friendly_name,p.rationale
            FROM saved_version_policies p
            JOIN saved_versions v ON v.id=p.saved_version_id
            WHERE p.classification='regressed_forensic'
            ORDER BY v.created_at
            """
        )
        artifacts = self.database.fetch_all(
            "SELECT * FROM recovery_artifacts ORDER BY kind,id"
        )
        for artifact in artifacts:
            artifact["training_allowed"] = bool(artifact["training_allowed"])
            artifact["evaluation_only"] = bool(artifact["evaluation_only"])
            artifact["metadata"] = parse_json(
                artifact.pop("metadata_json"), {}
            )
        return {
            "active_model": dict(active) if active else None,
            "recommended_model": dict(recommended) if recommended else None,
            "blocked_versions": blocked_versions,
            "recovery_artifacts": artifacts,
        }
