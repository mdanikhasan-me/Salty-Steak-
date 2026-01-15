"""Salty Steak Native Desktop AI Platform — deterministic calendar connector.

A calendar with a local store behind it, implementing the same contract a
Google or Microsoft calendar provider will implement.

The parts worth getting right are the ones that cause real harm when wrong:
overlap detection, so nothing is silently double-booked, and recurrence, so a
weekly class is one event with a rule rather than fourteen copies nobody can
edit.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from .contract import (
    RISK_DESTRUCTIVE,
    RISK_READ,
    RISK_WRITE_LOCAL,
    SERVICE_CALENDAR,
    BaseConnector,
    ConnectorDescriptor,
    ConnectorError,
    OperationSpec,
)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise ConnectorError(
            f"{field} must be an ISO date and time, not {value!r}",
            kind="invalid_request",
        ) from error


@dataclass
class Occurrence:
    """One actual appearance of an event in time."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    recurring: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event_id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "recurring": self.recurring,
        }


class LocalCalendarConnector(BaseConnector):
    """A calendar that behaves like a service, overlaps and recurrence included."""

    def __init__(
        self,
        descriptor: ConnectorDescriptor | None = None,
        *,
        vault: Any = None,
    ) -> None:
        super().__init__(
            descriptor
            or ConnectorDescriptor(
                connector_id="calendar.local",
                service_type=SERVICE_CALENDAR,
                display_name="Calendar",
                account="local@salty.test",
            ),
            vault=vault,
        )
        self._events: dict[str, dict[str, Any]] = {}

    def operations(self) -> Sequence[OperationSpec]:
        return (
            OperationSpec(
                name="list_events",
                summary="List events in a date range",
                risk=RISK_READ,
                arguments={"start": "ISO start", "end": "ISO end"},
                required=("start", "end"),
                paginated=True,
            ),
            OperationSpec(
                name="get",
                summary="Read one event",
                risk=RISK_READ,
                arguments={"id": "event id"},
                required=("id",),
            ),
            OperationSpec(
                name="free_busy",
                summary="Find free and busy time in a range",
                risk=RISK_READ,
                arguments={
                    "start": "ISO start",
                    "end": "ISO end",
                    "minimum_minutes": "smallest useful gap",
                },
                required=("start", "end"),
            ),
            OperationSpec(
                name="create_event",
                summary="Create an event",
                risk=RISK_WRITE_LOCAL,
                arguments={
                    "title": "what it is",
                    "start": "ISO start",
                    "end": "ISO end",
                    "repeat_weekly_on": "weekday names for a weekly series",
                    "repeat_until": "ISO date the series ends",
                    "allow_overlap": "true to book over an existing event",
                },
                required=("title", "start", "end"),
                verify_with="list_events",
            ),
            OperationSpec(
                name="update_event",
                summary="Change an event",
                risk=RISK_WRITE_LOCAL,
                arguments={"id": "event id"},
                required=("id",),
                verify_with="get",
            ),
            OperationSpec(
                name="delete_event",
                summary="Delete an event",
                risk=RISK_DESTRUCTIVE,
                arguments={"ids": "event ids"},
                required=("ids",),
                batchable=True,
                verify_with="list_events",
            ),
        )



    def seed(self, events: Sequence[Mapping[str, Any]]) -> int:
        for event in events:
            self.op_create_event({**dict(event), "allow_overlap": True})
        return len(self._events)

    @property
    def event_count(self) -> int:
        return len(self._events)



    def op_list_events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = _parse(arguments.get("start"), field="start")
        end = _parse(arguments.get("end"), field="end")
        occurrences = self._occurrences(start, end)
        return {
            "items": [occurrence.to_dict() for occurrence in occurrences],
            "next_cursor": None,
            "total_estimate": len(occurrences),
        }

    def op_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        event = self._events.get(str(arguments.get("id")))
        if event is None:
            raise ConnectorError(f"No event {arguments.get('id')!r}", kind="not_found")
        return {"event": dict(event)}

    def op_free_busy(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = _parse(arguments.get("start"), field="start")
        end = _parse(arguments.get("end"), field="end")
        minimum = timedelta(minutes=int(arguments.get("minimum_minutes") or 30))

        busy = sorted(
            ((item.start, item.end) for item in self._occurrences(start, end)),
        )
        merged: list[list[datetime]] = []
        for window_start, window_end in busy:
            if merged and window_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], window_end)
            else:
                merged.append([window_start, window_end])

        free = []
        cursor = start
        for window_start, window_end in merged:
            if window_start - cursor >= minimum:
                free.append((cursor, window_start))
            cursor = max(cursor, window_end)
        if end - cursor >= minimum:
            free.append((cursor, end))

        return {
            "busy": [
                {"start": item[0].isoformat(), "end": item[1].isoformat()}
                for item in merged
            ],
            "free": [
                {"start": item[0].isoformat(), "end": item[1].isoformat()}
                for item in free
            ],
        }



    def op_create_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        start = _parse(arguments.get("start"), field="start")
        end = _parse(arguments.get("end"), field="end")
        if end <= start:
            raise ConnectorError(
                "An event has to end after it starts.", kind="invalid_request"
            )

        repeat_on = [
            str(day).strip().casefold()
            for day in (arguments.get("repeat_weekly_on") or [])
        ]
        for day in repeat_on:
            if day not in WEEKDAYS:
                raise ConnectorError(f"{day!r} is not a weekday.", kind="invalid_request")
        repeat_until = (
            _parse(arguments["repeat_until"], field="repeat_until")
            if arguments.get("repeat_until")
            else None
        )
        if repeat_on and repeat_until is None:
            raise ConnectorError(
                "A repeating event needs a date to stop repeating.",
                kind="invalid_request",
            )

        event = {
            "id": arguments.get("id") or f"evt-{uuid.uuid4().hex[:10]}",
            "title": str(arguments.get("title") or "").strip(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "location": arguments.get("location"),
            "repeat_weekly_on": repeat_on,
            "repeat_until": repeat_until.isoformat() if repeat_until else None,
        }
        if not event["title"]:
            raise ConnectorError("An event needs a title.", kind="invalid_request")




        if not arguments.get("allow_overlap"):
            clashes = self._clashes(event)
            if clashes:
                raise ConnectorError(
                    "That time is already taken by: "
                    + "; ".join(
                        f"{item.title} {item.start.isoformat()} to {item.end.isoformat()}"
                        for item in clashes[:3]
                    ),
                    kind="conflict",
                )

        self._events[event["id"]] = event
        return {"event": event["id"], **event}

    def op_update_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        identifier = str(arguments.get("id"))
        event = self._events.get(identifier)
        if event is None:
            raise ConnectorError(f"No event {identifier!r}", kind="not_found")
        for key in ("title", "start", "end", "location"):
            if key in arguments and arguments[key] is not None:
                event[key] = (
                    _parse(arguments[key], field=key).isoformat()
                    if key in {"start", "end"}
                    else arguments[key]
                )
        return {"event": identifier, **event}

    def op_delete_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = arguments.get("ids") or arguments.get("id")
        identifiers = [raw] if isinstance(raw, str) else list(raw or [])
        succeeded, failed = [], []
        for identifier in identifiers:
            if str(identifier) in self._events:
                del self._events[str(identifier)]
                succeeded.append(str(identifier))
            else:
                failed.append({"id": str(identifier), "error": "no such event"})
        return {"succeeded": succeeded, "failed": failed}



    def _occurrences(self, start: datetime, end: datetime) -> list[Occurrence]:
        found: list[Occurrence] = []
        for event in self._events.values():
            found.extend(self._expand(event, start, end))
        found.sort(key=lambda item: (item.start, item.event_id))
        return found

    def _expand(
        self, event: Mapping[str, Any], start: datetime, end: datetime
    ) -> list[Occurrence]:
        first_start = datetime.fromisoformat(event["start"])
        first_end = datetime.fromisoformat(event["end"])
        length = first_end - first_start
        repeat_on = event.get("repeat_weekly_on") or []

        if not repeat_on:
            if first_start < end and first_end > start:
                return [
                    Occurrence(event["id"], event["title"], first_start, first_end, False)
                ]
            return []

        until = datetime.fromisoformat(event["repeat_until"])
        wanted = {WEEKDAYS.index(day) for day in repeat_on}
        found: list[Occurrence] = []


        cursor = max(first_start.date(), start.date())
        last = min(until.date(), end.date())
        while cursor <= last:
            if cursor.weekday() in wanted:
                occurrence_start = datetime.combine(cursor, first_start.time())
                occurrence_end = occurrence_start + length
                if occurrence_start < end and occurrence_end > start:
                    found.append(
                        Occurrence(
                            event["id"], event["title"], occurrence_start, occurrence_end, True
                        )
                    )
            cursor += timedelta(days=1)
        return found

    def _clashes(self, candidate: Mapping[str, Any]) -> list[Occurrence]:
        start = datetime.fromisoformat(candidate["start"])
        end = datetime.fromisoformat(candidate["end"])
        if candidate.get("repeat_until"):
            end = datetime.fromisoformat(candidate["repeat_until"])


        proposed = self._expand(candidate, start, end + timedelta(days=1))
        existing = self._occurrences(start, end + timedelta(days=1))
        clashes = []
        for wanted in proposed:
            for booked in existing:
                if booked.event_id == candidate.get("id"):
                    continue
                if wanted.start < booked.end and wanted.end > booked.start:
                    clashes.append(booked)
        return clashes


__all__ = ["LocalCalendarConnector", "Occurrence", "WEEKDAYS"]
