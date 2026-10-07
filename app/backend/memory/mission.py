"""Durable structured memory for long-running automation missions.

Semantic memory remembers facts worth saying tomorrow. Automation needs a
different shape: which account/workspace was active, which location was
checked, which recurring item was observed, and whether an action was verified
already. This ledger keeps that state outside the model context and injects
only a compact relevant slice into each planning turn.

The schema is service-neutral. Discord is the first observational adapter used
for acceptance, while the same principal/container/location/item/action model
serves user-owned applications without adding another memory system.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..automation.credentials import redact

MISSION_MEMORY_SCHEMA = "salty-steak-automation-mission-memory-v1"

ITEM_STATES = frozenset(
    {"unknown", "available", "active", "ended", "completed", "unavailable"}
)
ACTION_OUTCOMES = frozenset(
    {"planned", "attempted", "verified", "failed", "skipped_duplicate"}
)
ACTION_AUTHORITIES = frozenset(
    {"observed", "user_confirmed", "official_oauth", "official_bot"}
)
MAX_EVIDENCE_CHARACTERS = 2_000
MAX_BRIEFING_ITEMS = 20

_DISCORD_LOCATION = re.compile(
    r"^(?P<channel>.+?),\s*(?:Announcement|Text|Voice)\s+Channel,\s*(?P<server>.+)$",
    re.IGNORECASE,
)
_DISCORD_WINDOW = re.compile(
    r"^#?(?P<channel>.+?)\s*\|\s*(?P<server>.+?)\s*-\s*Discord$",
    re.IGNORECASE,
)
_ABSOLUTE_END = re.compile(
    r"(?:Ends|Ended):\s*(?:[^()]*\()?"
    r"(?P<end>[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\s+[^)\n]{3,40})",
    re.IGNORECASE,
)
_HOST = re.compile(r"Hosted by:\s*(?P<host><@!?\d+>|[^\n,]+)", re.IGNORECASE)
_TITLE = re.compile(
    r"(?:Verified App\s*,\s*|(?:AM|PM))(?P<title>[^\n]{3,180}?)"
    r"(?:~?\s*1 entry|\s+Ends?:|\s+Ended:)",
    re.IGNORECASE,
)


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _digest(*parts: Any) -> str:
    payload = "\x1f".join(_normalise(part).casefold() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bounded_evidence(value: Any) -> Any:
    safe = redact(value)
    encoded = json.dumps(safe, sort_keys=True, default=str)
    if len(encoded) <= MAX_EVIDENCE_CHARACTERS:
        return safe
    return {"summary": encoded[:MAX_EVIDENCE_CHARACTERS], "truncated": True}


class AutomationMissionMemory:
    """Account-aware item and idempotency memory shared by automation tasks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._task_locations: dict[str, dict[str, str]] = {}
        self._task_principals: dict[str, str] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS mission_principals (
                    principal_key TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    stable_id TEXT,
                    label TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS mission_principal_stable
                    ON mission_principals(service, stable_id)
                    WHERE stable_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS mission_locations (
                    location_key TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    principal_key TEXT NOT NULL
                        REFERENCES mission_principals(principal_key),
                    container_name TEXT NOT NULL,
                    location_name TEXT NOT NULL,
                    location_kind TEXT NOT NULL,
                    cursor TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    last_checked_at REAL,
                    UNIQUE(service, principal_key, container_name, location_name)
                );

                CREATE TABLE IF NOT EXISTS mission_items (
                    item_key TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    principal_key TEXT NOT NULL
                        REFERENCES mission_principals(principal_key),
                    location_key TEXT NOT NULL
                        REFERENCES mission_locations(location_key),
                    item_kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recurrence TEXT NOT NULL DEFAULT '',
                    requirements_json TEXT NOT NULL DEFAULT '[]',
                    eligibility_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    last_verified_at REAL,
                    UNIQUE(service, principal_key, location_key, item_kind, fingerprint)
                );

                CREATE TABLE IF NOT EXISTS mission_actions (
                    action_id TEXT PRIMARY KEY,
                    item_key TEXT NOT NULL REFERENCES mission_items(item_key),
                    action_name TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    audit_id TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS mission_verified_action_once
                    ON mission_actions(item_key, action_name)
                    WHERE outcome = 'verified';

                CREATE TABLE IF NOT EXISTS mission_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    principal_key TEXT,
                    event_type TEXT NOT NULL,
                    resource_key TEXT,
                    capability TEXT,
                    outcome TEXT,
                    audit_id TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mission_events_task
                    ON mission_events(task_id, created_at);
                CREATE INDEX IF NOT EXISTS mission_items_recent
                    ON mission_items(service, principal_key, last_seen_at DESC);
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(mission_items)"
                ).fetchall()
            }
            if "requirements_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE mission_items ADD COLUMN "
                    "requirements_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "eligibility_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE mission_items ADD COLUMN "
                    "eligibility_json TEXT NOT NULL DEFAULT '{}'"
                )



    def identify_principal(
        self,
        *,
        service: str,
        label: str,
        stable_id: str | None = None,
        source: str = "observed_ui",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        service_name = _normalise(service).casefold()
        label_text = _normalise(label) or "Current signed-in account"
        stable = _normalise(stable_id) or None
        principal_key = _digest(service_name, stable or label_text)[:32]
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO mission_principals(
                    principal_key, service, stable_id, label, source,
                    metadata_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(principal_key) DO UPDATE SET
                    label=excluded.label,
                    source=excluded.source,
                    metadata_json=excluded.metadata_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    principal_key,
                    service_name,
                    stable,
                    label_text,
                    _normalise(source) or "observed_ui",
                    json.dumps(_bounded_evidence(metadata or {}), sort_keys=True),
                    now,
                    now,
                ),
            )
        return {
            "principal_key": principal_key,
            "service": service_name,
            "label": label_text,
            "stable_id": stable,
        }

    def checkpoint_location(
        self,
        *,
        service: str,
        principal_key: str,
        container: str,
        location: str,
        location_kind: str = "location",
        cursor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        checked: bool = True,
    ) -> dict[str, Any]:
        service_name = _normalise(service).casefold()
        container_name = _normalise(container) or "Unknown container"
        location_name = _normalise(location) or "Unknown location"
        location_key = _digest(
            service_name, principal_key, container_name, location_name
        )[:40]
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO mission_locations(
                    location_key, service, principal_key, container_name,
                    location_name, location_kind, cursor, metadata_json,
                    first_seen_at, last_seen_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(location_key) DO UPDATE SET
                    cursor=COALESCE(excluded.cursor, mission_locations.cursor),
                    metadata_json=excluded.metadata_json,
                    last_seen_at=excluded.last_seen_at,
                    last_checked_at=COALESCE(
                        excluded.last_checked_at, mission_locations.last_checked_at
                    )
                """,
                (
                    location_key,
                    service_name,
                    principal_key,
                    container_name,
                    location_name,
                    _normalise(location_kind) or "location",
                    _normalise(cursor) or None,
                    json.dumps(_bounded_evidence(metadata or {}), sort_keys=True),
                    now,
                    now,
                    now if checked else None,
                ),
            )
        return {
            "location_key": location_key,
            "container": container_name,
            "location": location_name,
        }

    def observe_item(
        self,
        *,
        service: str,
        principal_key: str,
        location_key: str,
        item_kind: str,
        fingerprint_source: str,
        title: str,
        state: str = "unknown",
        recurrence: str = "",
        requirements: Sequence[Mapping[str, Any]] = (),
        eligibility: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | str | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        checked_state = _normalise(state).casefold()
        if checked_state not in ITEM_STATES:
            raise ValueError(f"Unknown mission item state: {state}")
        kind = _normalise(item_kind).casefold() or "item"
        fingerprint = _digest(fingerprint_source)
        item_key = _digest(service, principal_key, location_key, kind, fingerprint)[:48]
        now = time.time()
        safe_evidence = _bounded_evidence(
            evidence if isinstance(evidence, Mapping) else {"text": evidence or ""}
        )
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT state FROM mission_items WHERE item_key=?", (item_key,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO mission_items(
                    item_key, service, principal_key, location_key, item_kind,
                    fingerprint, title, state, recurrence, evidence_json,
                    requirements_json, eligibility_json,
                    first_seen_at, last_seen_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    title=excluded.title,
                    state=excluded.state,
                    recurrence=excluded.recurrence,
                    requirements_json=excluded.requirements_json,
                    eligibility_json=excluded.eligibility_json,
                    evidence_json=excluded.evidence_json,
                    last_seen_at=excluded.last_seen_at,
                    last_verified_at=COALESCE(
                        excluded.last_verified_at, mission_items.last_verified_at
                    )
                """,
                (
                    item_key,
                    _normalise(service).casefold(),
                    principal_key,
                    location_key,
                    kind,
                    fingerprint,
                    _normalise(title)[:300] or "Observed item",
                    checked_state,
                    _normalise(recurrence).casefold(),
                    json.dumps(safe_evidence, sort_keys=True),
                    json.dumps(_bounded_evidence(list(requirements)), sort_keys=True),
                    json.dumps(_bounded_evidence(eligibility or {}), sort_keys=True),
                    now,
                    now,
                    now if verified else None,
                ),
            )
        return {
            "item_key": item_key,
            "fingerprint": fingerprint,
            "state": checked_state,
            "already_known": existing is not None,
            "state_changed": existing is not None and existing["state"] != checked_state,
        }

    @staticmethod
    def evaluate_requirements(
        requirements: Sequence[Mapping[str, Any]],
        facts: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Evaluate generic structured preconditions without inventing evidence."""

        checks: list[dict[str, Any]] = []
        for raw in requirements:
            requirement = dict(raw)
            key = _normalise(requirement.get("key"))
            operator = _normalise(requirement.get("operator") or "equals").casefold()
            expected = requirement.get("expected")
            observed = facts.get(key) if key else None
            if not key or key not in facts:
                status = "unknown"
            elif operator == "equals":
                status = "met" if observed == expected else "unmet"
            elif operator == "minimum":
                try:
                    status = "met" if float(observed) >= float(expected) else "unmet"
                except (TypeError, ValueError):
                    status = "unknown"
            elif operator == "maximum":
                try:
                    status = "met" if float(observed) <= float(expected) else "unmet"
                except (TypeError, ValueError):
                    status = "unknown"
            elif operator == "contains":
                try:
                    status = "met" if expected in observed else "unmet"
                except TypeError:
                    status = "unknown"
            elif operator == "present":
                status = "met" if observed not in (None, "", [], {}) else "unmet"
            else:
                status = "unknown"
            checks.append(
                {
                    "key": key,
                    "operator": operator,
                    "expected": expected,
                    "observed": observed,
                    "status": status,
                    "description": _normalise(requirement.get("description")),
                }
            )
        statuses = {check["status"] for check in checks}
        disposition = (
            "ineligible"
            if "unmet" in statuses
            else "incomplete_evidence"
            if "unknown" in statuses
            else "eligible"
        )
        return {"disposition": disposition, "checks": checks}

    def preflight_item(
        self, item_key: str, *, facts: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT requirements_json FROM mission_items WHERE item_key=?",
                (str(item_key),),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown mission item: {item_key}")
        requirements = json.loads(row["requirements_json"] or "[]")
        result = self.evaluate_requirements(requirements, facts)
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE mission_items SET eligibility_json=? WHERE item_key=?",
                (json.dumps(_bounded_evidence(result), sort_keys=True), str(item_key)),
            )
        return result

    def record_action(
        self,
        *,
        item_key: str,
        action: str,
        outcome: str,
        authority: str,
        audit_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action_name = _normalise(action).casefold()
        checked_outcome = _normalise(outcome).casefold()
        checked_authority = _normalise(authority).casefold()
        if checked_outcome not in ACTION_OUTCOMES:
            raise ValueError(f"Unknown mission action outcome: {outcome}")
        if checked_authority not in ACTION_AUTHORITIES:
            raise ValueError(f"Unknown mission action authority: {authority}")
        with self._lock, self._connection:
            item = self._connection.execute(
                "SELECT requirements_json, eligibility_json FROM mission_items WHERE item_key=?",
                (str(item_key),),
            ).fetchone()
            if item is None:
                raise KeyError(f"Unknown mission item: {item_key}")
            requirements = json.loads(item["requirements_json"] or "[]")
            eligibility = json.loads(item["eligibility_json"] or "{}")
            if (
                checked_outcome == "verified"
                and requirements
                and eligibility.get("disposition") != "eligible"
            ):
                raise PermissionError(
                    "A verified action cannot be recorded until every observed "
                    "requirement has passed the mission preflight"
                )
            verified = self._connection.execute(
                """SELECT * FROM mission_actions
                   WHERE item_key=? AND action_name=? AND outcome='verified'""",
                (item_key, action_name),
            ).fetchone()
            if verified is not None:
                return {
                    "action_id": verified["action_id"],
                    "already_verified": True,
                    "skip": True,
                }
            action_id = str(uuid.uuid4())
            self._connection.execute(
                """
                INSERT INTO mission_actions(
                    action_id, item_key, action_name, outcome, authority,
                    audit_id, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    item_key,
                    action_name,
                    checked_outcome,
                    checked_authority,
                    _normalise(audit_id) or None,
                    json.dumps(_bounded_evidence(evidence or {}), sort_keys=True),
                    time.time(),
                ),
            )
        return {"action_id": action_id, "already_verified": False, "skip": False}

    def should_skip_action(self, item_key: str, action: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """SELECT 1 FROM mission_actions
                   WHERE item_key=? AND action_name=? AND outcome='verified'""",
                (str(item_key), _normalise(action).casefold()),
            ).fetchone()
        return row is not None



    def briefing(self, query: str, *, limit: int = 12) -> str:
        raw_words = [
            word
            for word in re.findall(r"[\w'-]+", str(query or "").casefold())
            if len(word) >= 3
        ][:12]
        words = list(
            dict.fromkeys(
                variant
                for word in raw_words
                for variant in (word, word[:-1] if word.endswith("s") else word)
                if len(variant) >= 3
            )
        )
        clauses = []
        parameters: list[Any] = []
        for word in words:
            clauses.append(
                "lower(p.service || ' ' || p.label || ' ' || l.container_name || "
                "' ' || l.location_name || ' ' || i.title) LIKE ?"
            )
            parameters.append(f"%{word}%")
        where = "WHERE " + " OR ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), MAX_BRIEFING_ITEMS)))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT i.*, p.label AS principal_label,
                       l.container_name, l.location_name,
                       (SELECT GROUP_CONCAT(action_name, ', ')
                          FROM mission_actions a
                         WHERE a.item_key=i.item_key AND a.outcome='verified')
                         AS verified_actions
                FROM mission_items i
                JOIN mission_principals p ON p.principal_key=i.principal_key
                JOIN mission_locations l ON l.location_key=i.location_key
                {where}
                ORDER BY i.last_seen_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            actions = row["verified_actions"] or "none"
            lines.append(
                "- "
                f"{row['service']} account {row['principal_label']} | "
                f"{row['container_name']} / {row['location_name']} | "
                f"{row['title']} | state={row['state']} | "
                f"eligibility={json.loads(row['eligibility_json'] or '{}').get('disposition', 'unknown')} | "
                f"verified_actions={actions} | item_key={row['item_key']}"
            )
        return (
            "Durable automation mission memory (full history is in SQLite; "
            "only relevant current records are shown):\n"
            + "\n".join(lines)
            + "\nA verified action is never repeated for the same item_key; a new "
            "recurrence must have a new fingerprint."
        )

    def observe_step(
        self,
        *,
        task_id: str,
        capability: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        world_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        service = self._infer_service(capability, arguments, result, world_state or {})
        account = result.get("account")
        observed_account = (
            dict(account)
            if isinstance(account, Mapping) and account.get("observed")
            else None
        )
        if observed_account is not None:
            label = _normalise(observed_account.get("label")) or (
                f"Current {service} account/workspace"
            )
            stable_id = _normalise(observed_account.get("handle")) or label
            principal = self.identify_principal(
                service=service,
                label=label,
                stable_id=stable_id,
                source="observed_ui",
                metadata={"account": observed_account},
            )
            self._task_principals[task_id] = principal["principal_key"]
        elif task_id not in self._task_principals:
            principal = self.identify_principal(
                service=service,
                label=f"Current {service} account/workspace",
                source="observed_runtime",
            )
            self._task_principals[task_id] = principal["principal_key"]
        updates: dict[str, Any] = {
            "schema": MISSION_MEMORY_SCHEMA,
            "service": service,
            "principal_key": self._task_principals[task_id],
            "locations_observed": [],
            "items_observed": [],
        }

        if service == "discord":
            observed = self._observe_discord(
                task_id=task_id,
                principal_key=self._task_principals[task_id],
                capability=capability,
                arguments=arguments,
                result=result,
                world_state=world_state or {},
            )
            updates["locations_observed"] = observed["locations"]
            updates["items_observed"] = observed["items"]

        audit_id = _normalise(result.get("audit_record_id")) or None
        self._record_event(
            task_id=task_id,
            service=service,
            principal_key=self._task_principals[task_id],
            event_type="tool_observation",
            resource_key=(
                (updates["items_observed"][-1] or {}).get("item_key")
                if updates["items_observed"]
                else (updates["locations_observed"][-1] or {}).get("location_key")
                if updates["locations_observed"]
                else None
            ),
            capability=capability,
            outcome=_normalise(result.get("status")) or "unknown",
            audit_id=audit_id,
            evidence={
                "arguments": dict(arguments),
                "status": result.get("status"),
                "update_counts": {
                    "locations": len(updates["locations_observed"]),
                    "items": len(updates["items_observed"]),
                },
            },
        )
        return updates

    def _observe_discord(
        self,
        *,
        task_id: str,
        principal_key: str,
        capability: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        world_state: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        locations: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        name = _normalise(arguments.get("name"))
        batch_scans = result.get("scans")
        if isinstance(batch_scans, Sequence) and not isinstance(
            batch_scans, (str, bytes, bytearray)
        ):
            for scan in batch_scans:
                if not isinstance(scan, Mapping):
                    continue
                selected_scan = scan.get("selected_channel")
                if not isinstance(selected_scan, Mapping):
                    continue
                server = _normalise(
                    selected_scan.get("readback_server")
                    or selected_scan.get("server")
                )
                channel = _normalise(
                    selected_scan.get("readback_channel")
                    or selected_scan.get("channel")
                )
                if not server or not channel:
                    continue
                message_coverage = dict(
                    ((scan.get("coverage") or {}).get("messages") or {})
                )
                location = self.checkpoint_location(
                    service="discord",
                    principal_key=principal_key,
                    container=server,
                    location=channel,
                    location_kind="channel",
                    checked=bool(message_coverage.get("history_boundary_reached")),
                    metadata={
                        "channel_key": selected_scan.get("channel_key"),
                        "message_coverage": message_coverage,
                        "candidate_classification": scan.get(
                            "candidate_classification"
                        ),
                    },
                )
                self._task_locations[task_id] = location
                if len(locations) < 50:
                    locations.append(location)
                structured_items = scan.get("giveaway_items")
                if isinstance(structured_items, Sequence) and not isinstance(
                    structured_items, (str, bytes, bytearray)
                ):
                    evidence_records = [
                        dict(value)
                        for value in structured_items
                        if isinstance(value, Mapping)
                    ]
                else:
                    scan_messages = scan.get("messages")
                    if not isinstance(scan_messages, Sequence) or isinstance(
                        scan_messages, (str, bytes, bytearray)
                    ):
                        continue
                    evidence_records = [
                        {"evidence": value}
                        for value in dict.fromkeys(
                            _normalise(value) for value in scan_messages
                        )
                    ]
                for evidence_record in evidence_records:
                    evidence = _normalise(evidence_record.get("evidence"))
                    lowered = evidence.casefold()
                    if "giveaway" not in lowered and not any(
                        marker in lowered
                        for marker in (
                            "ended:",
                            "ends:",
                            "entries:",
                            "time remaining",
                        )
                    ):
                        continue
                    state = _normalise(evidence_record.get("state")).casefold() or (
                        "ended"
                        if "ended:" in lowered or "winners:" in lowered
                        else "active"
                        if "ends:" in lowered or "time remaining" in lowered
                        else "unknown"
                    )
                    requirements = (
                        list(evidence_record.get("requirements") or [])
                        if isinstance(evidence_record.get("requirements"), Sequence)
                        and not isinstance(
                            evidence_record.get("requirements"),
                            (str, bytes, bytearray),
                        )
                        else []
                    )
                    item = self.observe_item(
                        service="discord",
                        principal_key=principal_key,
                        location_key=location["location_key"],
                        item_kind="recurring_event",
                        fingerprint_source=self._event_fingerprint_source(evidence),
                        title=self._event_title(evidence),
                        state=state,
                        recurrence=next(
                            (
                                word
                                for word in ("daily", "weekly", "monthly")
                                if word in lowered
                            ),
                            "",
                        ),
                        requirements=requirements,
                        eligibility={
                            "disposition": _normalise(
                                evidence_record.get("disposition")
                                or scan.get("disposition")
                            )
                            or "unknown"
                        },
                        evidence={"accessible_name": evidence},
                        verified=state in {"active", "ended"},
                    )
                    items.append(item)
        inventory_channels = result.get("channels")
        if isinstance(inventory_channels, Sequence) and not isinstance(
            inventory_channels, (str, bytes, bytearray)
        ):
            per_server = dict(
                (((result.get("coverage") or {}).get("channels") or {}).get("per_server") or {})
            )
            for channel_record in inventory_channels:
                if not isinstance(channel_record, Mapping):
                    continue
                server = _normalise(channel_record.get("server"))
                channel = _normalise(channel_record.get("channel"))
                if not server or not channel:
                    continue
                coverage = dict(per_server.get(server) or {})
                location = self.checkpoint_location(
                    service="discord",
                    principal_key=principal_key,
                    container=server,
                    location=channel,
                    location_kind="channel",
                    checked=bool(coverage.get("scroll_boundary_reached")),
                    metadata={
                        "channel_type": channel_record.get("channel_type"),
                        "inventory_coverage": coverage,
                    },
                )


                if len(locations) < 50:
                    locations.append(location)
        parsed = _DISCORD_LOCATION.match(name)
        if parsed and str(arguments.get("command") or "").casefold() in {
            "invoke",
            "select",
        }:
            location = self.checkpoint_location(
                service="discord",
                principal_key=principal_key,
                container=parsed.group("server"),
                location=parsed.group("channel"),
                location_kind="channel",
                checked=False,
                metadata={"observed_result_name": name},
            )
            self._task_locations[task_id] = location
            locations.append(location)

        selected = result.get("selected_channel")
        if isinstance(selected, Mapping):
            server = _normalise(
                selected.get("readback_server") or selected.get("server")
            )
            channel = _normalise(
                selected.get("readback_channel") or selected.get("channel")
            )
            if server and channel:
                location = self.checkpoint_location(
                    service="discord",
                    principal_key=principal_key,
                    container=server,
                    location=channel,
                    location_kind="channel",
                    checked=False,
                    metadata={"selected_channel": dict(selected)},
                )
                self._task_locations[task_id] = location
                locations.append(location)

        active = world_state.get("active_window")
        if isinstance(active, Mapping):
            title = _normalise(active.get("name") or active.get("title"))
            active_match = _DISCORD_WINDOW.match(title)
            if active_match:
                location = self.checkpoint_location(
                    service="discord",
                    principal_key=principal_key,
                    container=active_match.group("server"),
                    location=active_match.group("channel"),
                    location_kind="channel",
                    checked=False,
                    metadata={"window_readback": title},
                )
                self._task_locations[task_id] = location
                locations.append(location)

        current = self._task_locations.get(task_id)
        if current is None:
            return {"locations": locations, "items": items}
        nodes = result.get("nodes")
        evidence_values: list[str] = []
        if isinstance(nodes, Sequence) and not isinstance(
            nodes, (str, bytes, bytearray)
        ):
            evidence_values.extend(
                _normalise(node.get("name"))
                for node in nodes
                if isinstance(node, Mapping) and node.get("control_type") == "ListItem"
            )
        messages = result.get("messages")
        if isinstance(messages, Sequence) and not isinstance(
            messages, (str, bytes, bytearray)
        ):
            evidence_values.extend(_normalise(value) for value in messages)
        structured_items = result.get("giveaway_items")
        if isinstance(structured_items, Sequence) and not isinstance(
            structured_items, (str, bytes, bytearray)
        ):
            evidence_records = [
                dict(value) for value in structured_items if isinstance(value, Mapping)
            ]
        else:
            evidence_records = [
                {"evidence": value}
                for value in dict.fromkeys(evidence_values)
            ]
        if not evidence_records:
            return {"locations": locations, "items": items}
        for evidence_record in evidence_records:
            evidence = _normalise(evidence_record.get("evidence"))
            lowered = evidence.casefold()
            if "giveaway" not in lowered and not any(
                marker in lowered
                for marker in ("ended:", "ends:", "entries:", "time remaining")
            ):
                continue
            state = _normalise(evidence_record.get("state")).casefold() or (
                "ended"
                if "ended:" in lowered or "winners:" in lowered
                else "active"
                if "ends:" in lowered or "time remaining" in lowered
                else "unknown"
            )
            title = self._event_title(evidence)
            recurrence = next(
                (
                    word
                    for word in ("daily", "weekly", "monthly")
                    if word in lowered
                ),
                "",
            )
            item = self.observe_item(
                service="discord",
                principal_key=principal_key,
                location_key=current["location_key"],
                item_kind="recurring_event",
                fingerprint_source=self._event_fingerprint_source(evidence),
                title=title,
                state=state,
                recurrence=recurrence,
                requirements=(
                    list(
                        evidence_record.get("requirements")
                        or result.get("requirements")
                        or []
                    )
                    if isinstance(
                        evidence_record.get("requirements")
                        or result.get("requirements"),
                        Sequence,
                    )
                    and not isinstance(
                        evidence_record.get("requirements")
                        or result.get("requirements"),
                        (str, bytes, bytearray),
                    )
                    else []
                ),
                eligibility={
                    "disposition": _normalise(
                        evidence_record.get("disposition")
                        or result.get("disposition")
                    )
                    or "unknown"
                },
                evidence={"accessible_name": evidence},
                verified=state in {"active", "ended"},
            )
            items.append(item)
        if items:
            self.checkpoint_location(
                service="discord",
                principal_key=principal_key,
                container=current["container"],
                location=current["location"],
                location_kind="channel",
                cursor=items[-1]["fingerprint"],
                checked=True,
                metadata={"last_observed_item_count": len(items)},
            )
        return {"locations": locations, "items": items}

    @staticmethod
    def _event_title(evidence: str) -> str:
        match = _TITLE.search(evidence)
        if match:
            return _normalise(match.group("title"))[:300]
        return evidence[:300] or "Observed recurring event"

    @staticmethod
    def _event_fingerprint_source(evidence: str) -> str:
        title = AutomationMissionMemory._event_title(evidence)
        end = _ABSOLUTE_END.search(evidence)
        host = _HOST.search(evidence)
        stable = "|".join(
            [
                title,
                _normalise(end.group("end")) if end else "",
                _normalise(host.group("host")) if host else "",
            ]
        )
        return stable if stable.strip("|") else evidence

    @staticmethod
    def _infer_service(
        capability: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        world_state: Mapping[str, Any],
    ) -> str:
        values = [
            arguments.get("target"),
            arguments.get("window"),
            arguments.get("name"),
            result.get("target"),
            (world_state.get("active_window") or {}).get("name")
            if isinstance(world_state.get("active_window"), Mapping)
            else None,
            (world_state.get("active_window") or {}).get("title")
            if isinstance(world_state.get("active_window"), Mapping)
            else None,
        ]
        haystack = " ".join(_normalise(value) for value in values).casefold()
        if capability == "discord.inspect":
            return "discord"
        if "discord" in haystack:
            return "discord"
        if capability == "browser.control":
            return "browser"
        if capability == "terminal.execute":
            return "terminal"
        return "local-computer"

    def _record_event(
        self,
        *,
        task_id: str,
        service: str,
        principal_key: str | None,
        event_type: str,
        resource_key: str | None,
        capability: str | None,
        outcome: str | None,
        audit_id: str | None,
        evidence: Mapping[str, Any],
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO mission_events(
                    event_id, task_id, service, principal_key, event_type,
                    resource_key, capability, outcome, audit_id, evidence_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(task_id),
                    str(service),
                    principal_key,
                    str(event_type),
                    resource_key,
                    capability,
                    outcome,
                    audit_id,
                    json.dumps(_bounded_evidence(evidence), sort_keys=True),
                    time.time(),
                ),
            )

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                table: int(
                    self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "mission_principals",
                    "mission_locations",
                    "mission_items",
                    "mission_actions",
                    "mission_events",
                )
            }
        return {"schema": MISSION_MEMORY_SCHEMA, **counts}

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def cleanup_revision(self) -> tuple[int, int]:
        """Detect writes since a reviewed cleanup, including other connections."""
        with self._lock:
            return (self._connection.total_changes,
                    self._connection.execute('PRAGMA data_version').fetchone()[0])

    def clear(self) -> None:
        """Erase app-owned automation memory at the user's explicit request."""
        with self._lock, self._connection:
            self._connection.execute('PRAGMA secure_delete=ON')
            for table in ('mission_events','mission_actions','mission_items','mission_locations','mission_principals'):
                self._connection.execute(f'DELETE FROM {table}')
            self._task_locations.clear()
            self._task_principals.clear()
        with self._lock:
            self._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self._connection.execute('VACUUM')


__all__ = ["AutomationMissionMemory", "MISSION_MEMORY_SCHEMA"]
