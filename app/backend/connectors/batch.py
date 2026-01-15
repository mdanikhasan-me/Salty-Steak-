"""Salty Steak Native Desktop AI Platform — paged reads and batched writes.

The difference between an assistant that can tidy a mailbox and one that can
only talk about tidying a mailbox.

A task over five thousand messages must not become five thousand model calls.
The model decides the rule; the runtime applies it. Reading is paged so a large
result never has to exist in memory or in a prompt all at once, and writing is
batched so one decision covers many records.

Partial failure is the normal case at this size, not an exception. A batch that
fails on item 172 keeps items 1 to 171 — throwing away confirmed work because a
later item failed would make every large task unrepeatable.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contract import ConnectorError, Page

BATCH_SCHEMA = "salty-steak-batch-v1"



DEFAULT_PAGE_SIZE = 100
DEFAULT_BATCH_SIZE = 50

MAX_PAGES = 500


class BatchCancelled(RuntimeError):
    """Raised when a batch stopped early, carrying what was already done."""

    def __init__(self, message: str, report: "BatchReport") -> None:
        super().__init__(message)
        self.report = report


@dataclass
class BatchReport:
    """What a batch actually achieved, including when it went wrong."""

    operation: str
    connector: str
    succeeded: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    batches: int = 0
    cancelled: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def total(self) -> int:
        return len(self.succeeded) + len(self.failed) + len(self.skipped)

    @property
    def complete(self) -> bool:
        return not self.failed and not self.skipped and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BATCH_SCHEMA,
            "connector": self.connector,
            "operation": self.operation,
            "succeeded_count": len(self.succeeded),
            "failed_count": len(self.failed),
            "skipped_count": len(self.skipped),
            "batches": self.batches,
            "cancelled": self.cancelled,
            "complete": self.complete,


            "succeeded": self.succeeded[:50],
            "failed": self.failed[:50],
            "seconds": round((self.finished_at or time.time()) - self.started_at, 3),
        }

    def summary(self) -> str:
        parts = [f"{len(self.succeeded)} done"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.skipped:
            parts.append(f"{len(self.skipped)} not attempted")
        return ", ".join(parts)


def iterate_pages(
    manager: Any,
    connector_id: str,
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_items: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_page: Callable[[int, Page], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Walk a paged read one record at a time, holding one page at a time.

    A generator rather than a list: the caller decides how much to keep, so a
    mailbox-sized result never has to exist in memory at once.
    """

    arguments = dict(arguments or {})
    cursor: str | None = None
    seen = 0
    for page_number in range(1, MAX_PAGES + 1):
        if should_stop is not None and should_stop():
            return
        payload = dict(arguments)
        payload["limit"] = page_size
        if cursor:
            payload["cursor"] = cursor

        result = manager.invoke(connector_id, operation, payload, should_stop=should_stop)
        data = result.data if hasattr(result, "data") else dict(result)
        page = Page(
            items=list(data.get("items") or []),
            next_cursor=data.get("next_cursor"),
            total_estimate=data.get("total_estimate"),
        )
        if on_page is not None:
            on_page(page_number, page)

        for item in page.items:
            yield item
            seen += 1
            if max_items is not None and seen >= max_items:
                return

        if page.exhausted:
            return
        cursor = page.next_cursor
    raise ConnectorError(
        f"{operation!r} kept returning pages past the {MAX_PAGES} page limit.",
        kind="runaway_pagination",
    )


def collect(
    manager: Any,
    connector_id: str,
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Every record from a paged read, for when the caller really needs them all."""

    return list(iterate_pages(manager, connector_id, operation, arguments, **kwargs))


def run_batch(
    manager: Any,
    connector_id: str,
    operation: str,
    identifiers: Sequence[str],
    arguments: Mapping[str, Any] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[BatchReport], None] | None = None,
    stop_on_failure: bool = False,
) -> BatchReport:
    """Apply one decision to many records.

    The model chose the rule once. This applies it, in bounded batches, with
    progress and cancellation between them, and it never discards work that
    already succeeded because something later did not.
    """

    arguments = dict(arguments or {})
    identifiers = list(identifiers)
    report = BatchReport(operation=operation, connector=connector_id)

    for start in range(0, len(identifiers), max(1, batch_size)):
        chunk = identifiers[start : start + max(1, batch_size)]
        if should_stop is not None and should_stop():


            report.skipped.extend(identifiers[start:])
            report.cancelled = True
            break

        report.batches += 1
        try:
            result = manager.invoke(
                connector_id,
                operation,
                {**arguments, "ids": chunk},
                should_stop=should_stop,
            )
        except Exception as error:



            if getattr(error, "kind", None) == "cancelled":
                report.skipped.extend(identifiers[start:])
                report.cancelled = True
                break


            for identifier in chunk:
                report.failed.append(
                    {"id": identifier, "error": f"{type(error).__name__}: {error}"}
                )
            if stop_on_failure:
                report.skipped.extend(identifiers[start + len(chunk) :])
                break
            continue

        data = result.data if hasattr(result, "data") else dict(result)
        succeeded = data.get("succeeded")
        failed = data.get("failed") or []


        report.succeeded.extend(succeeded if succeeded is not None else chunk)
        for entry in failed:
            report.failed.append(
                entry if isinstance(entry, dict) else {"id": entry, "error": "failed"}
            )

        if on_progress is not None:
            on_progress(report)

    report.finished_at = time.time()
    return report


def summarise_for_model(
    items: Iterable[Mapping[str, Any]],
    fields: Sequence[str],
    *,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Reduce records to the few fields a decision actually turns on.

    Handing whole records to the model is what makes large tasks impossible:
    the prompt fills with data nobody is reasoning about.
    """

    reduced = []
    for index, item in enumerate(items):
        if index >= limit:
            break
        reduced.append({key: item.get(key) for key in fields if key in item})
    return reduced


__all__ = [
    "BATCH_SCHEMA",
    "BatchCancelled",
    "BatchReport",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PAGE_SIZE",
    "collect",
    "iterate_pages",
    "run_batch",
    "summarise_for_model",
]
