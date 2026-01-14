"""Salty Steak Native Desktop AI Platform — persistent semantic memory.

What the assistant is allowed to still know tomorrow.

An operator that forgets everything between tasks makes the user re-explain
their own life every morning: which address is the work one, which of two
people called Sam is the client, that invoices go out on the last Friday. This
holds that kind of knowledge, and only that kind.

Retrieval is SQLite's own full-text index with BM25 ranking, which needs no
model, no embedding pass and no dependency. It is weaker than a vector search
at matching paraphrase and much stronger at being instant, inspectable and
offline — and a memory the user cannot read back is not one they can correct.

Two rules are enforced rather than intended. Secrets never enter: a credential
belongs in the DPAPI store behind a reference, and a memory row is refused
outright if it looks like one. And nothing is silently overwritten: a fact that
stops being true is superseded, leaving the change visible instead of rewriting
history under the user.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..automation.credentials import REDACTED, SECRET_FIELDS, redact

MEMORY_SCHEMA = "salty-steak-semantic-memory-v1"



MEMORY_KINDS = (

    "preference",

    "fact",

    "procedure",

    "entity",

    "outcome",
)

MAX_SUBJECT_CHARACTERS = 200
MAX_BODY_CHARACTERS = 2_000




SECRET_TEXT_PATTERNS = (
    re.compile(r"\b(pass(word|phrase)|secret|api[ _-]?key|token|otp|pin)\b\s*[:=]", re.I),

    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I),
    re.compile(r"\b(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}"),

    re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b"),
)




STOP_WORDS = frozenset(
    """
    the and for are was were you your our its his her their they them this that
    these those with from into onto over under about after before been being
    have has had not but all any can could would should will shall may might
    who whom what when where why how which while than then there here does did
    doing done get got make made use used using need needs want wants
    """.split()
)


class MemoryRefused(RuntimeError):
    """Raised when something must not be written to durable memory."""


@dataclass
class MemoryRecord:
    """One durable thing the assistant knows."""

    memory_id: str
    kind: str
    subject: str
    body: str
    confidence: float
    source: str
    created_at: float
    last_used_at: float | None
    use_count: int
    superseded_by: str | None
    tags: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "subject": self.subject,
            "body": self.body,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": self.use_count,
            "superseded_by": self.superseded_by,
            "active": self.active,
        }

    def for_model(self) -> str:
        """The one-line form a memory takes inside a prompt."""

        return f"[{self.kind}] {self.subject}: {self.body}"


def _reject_secrets(subject: str, body: str, tags: Iterable[str]) -> None:
    """Refuse anything credential-shaped before it reaches the disk.

    The check is on the way in, not on the way out. A secret that has already
    been written is a secret that has leaked, whatever the reader does next.
    """

    haystack = " ".join([subject, body, " ".join(tags)])
    for pattern in SECRET_TEXT_PATTERNS:
        if pattern.search(haystack):
            raise MemoryRefused(
                "That looks like a credential. Passwords, tokens and keys are "
                "kept in the protected credential store and referenced by name; "
                "they are never written to memory."
            )
    lowered = subject.casefold().replace("-", "_").strip()
    if lowered in SECRET_FIELDS:
        raise MemoryRefused(
            f"{subject!r} names a credential field and cannot be a memory subject."
        )


def _escape_query(text: str) -> str:
    """Turn user words into an FTS5 query that cannot be a syntax error.

    Each word becomes a quoted term, so punctuation in a name or an address
    matches literally instead of being read as query operators.

    Common words are dropped first. Matching them with OR makes every query
    match every memory — a task about compiling a toolchain would recall the
    user's invoicing schedule purely because both sentences contain "the".
    """

    words = [
        word
        for word in re.findall(r"[\w'@.]+", text.casefold())
        if len(word) > 2 and word not in STOP_WORDS
    ]
    return " OR ".join(f'"{word}"' for word in words)


class SemanticMemory:
    """Durable, inspectable knowledge that survives a task.

    Kept in its own database file rather than the control schema: it is
    append-mostly, it carries a full-text index, and the user must be able to
    erase everything the assistant remembers about them without disturbing
    application state.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id     TEXT PRIMARY KEY,
                    kind          TEXT NOT NULL,
                    subject       TEXT NOT NULL,
                    body          TEXT NOT NULL,
                    confidence    REAL NOT NULL DEFAULT 0.7,
                    source        TEXT NOT NULL DEFAULT 'user',
                    tags_json     TEXT NOT NULL DEFAULT '[]',
                    created_at    REAL NOT NULL,
                    last_used_at  REAL,
                    use_count     INTEGER NOT NULL DEFAULT 0,
                    superseded_by TEXT REFERENCES memories(memory_id)
                );

                CREATE INDEX IF NOT EXISTS memories_active
                    ON memories(superseded_by, kind);

                -- The porter tokenizer stems on the way in and on the way out,
                -- so a task about an "invoice" recalls a memory about
                -- "invoices". Without it the store misses the obvious match.
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    subject, body, tags, content='',
                    tokenize='porter unicode61'
                );

                CREATE TABLE IF NOT EXISTS memory_fts_map (
                    rowid     INTEGER PRIMARY KEY,
                    memory_id TEXT NOT NULL UNIQUE
                        REFERENCES memories(memory_id) ON DELETE CASCADE
                );
                """
            )



    def remember(
        self,
        *,
        kind: str,
        subject: str,
        body: str,
        confidence: float = 0.7,
        source: str = "user",
        tags: Iterable[str] = (),
        supersedes: str | None = None,
    ) -> MemoryRecord:
        """Write something worth knowing tomorrow."""

        if kind not in MEMORY_KINDS:
            raise MemoryRefused(
                f"{kind!r} is not a memory kind. Use one of: {', '.join(MEMORY_KINDS)}"
            )
        subject = str(subject).strip()
        body = str(body).strip()
        if not subject or not body:
            raise MemoryRefused("A memory needs both a subject and a body.")
        if len(subject) > MAX_SUBJECT_CHARACTERS or len(body) > MAX_BODY_CHARACTERS:
            raise MemoryRefused(
                "A memory must be a short durable fact, not a transcript."
            )
        tags = tuple(str(tag).strip().casefold() for tag in tags if str(tag).strip())
        _reject_secrets(subject, body, tags)

        record = MemoryRecord(
            memory_id=str(uuid.uuid4()),
            kind=kind,
            subject=subject,
            body=body,
            confidence=max(0.0, min(1.0, float(confidence))),
            source=str(source),
            created_at=time.time(),
            last_used_at=None,
            use_count=0,
            superseded_by=None,
            tags=tags,
        )

        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO memories (memory_id, kind, subject, body, confidence,
                                         source, tags_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.memory_id,
                    record.kind,
                    record.subject,
                    record.body,
                    record.confidence,
                    record.source,
                    json.dumps(list(record.tags)),
                    record.created_at,
                ),
            )
            cursor = self._connection.execute(
                "INSERT INTO memories_fts (subject, body, tags) VALUES (?, ?, ?)",
                (record.subject, record.body, " ".join(record.tags)),
            )
            self._connection.execute(
                "INSERT INTO memory_fts_map (rowid, memory_id) VALUES (?, ?)",
                (cursor.lastrowid, record.memory_id),
            )
            if supersedes:


                self._connection.execute(
                    "UPDATE memories SET superseded_by = ? WHERE memory_id = ?",
                    (record.memory_id, supersedes),
                )
        return record

    def forget(self, memory_id: str) -> bool:
        """Erase a memory outright, at the user's request."""

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT rowid FROM memory_fts_map WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            if row is not None:
                self._connection.execute(
                    "INSERT INTO memories_fts (memories_fts, rowid, subject, body, tags) "
                    "VALUES ('delete', ?, '', '', '')",
                    (row["rowid"],),
                )
                self._connection.execute(
                    "DELETE FROM memory_fts_map WHERE memory_id = ?", (memory_id,)
                )
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
            )
        return cursor.rowcount > 0

    def forget_all(self) -> int:
        """Erase everything. The user owns this and must be able to empty it."""

        with self._lock, self._connection:
            count = self._connection.execute(
                "SELECT COUNT(*) AS n FROM memories"
            ).fetchone()["n"]
            self._connection.execute("DELETE FROM memory_fts_map")
            self._connection.execute("DELETE FROM memories")
            self._connection.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
        return int(count)



    def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        kinds: Iterable[str] | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        """Find what is worth putting in front of the model for this task."""

        terms = _escape_query(str(query or ""))
        if not terms:
            return []
        wanted = tuple(kinds) if kinds else ()

        sql = """
            SELECT m.*, bm25(memories_fts) AS relevance
            FROM memories_fts
            JOIN memory_fts_map map ON map.rowid = memories_fts.rowid
            JOIN memories m ON m.memory_id = map.memory_id
            WHERE memories_fts MATCH ?
        """
        parameters: list[Any] = [terms]
        if not include_superseded:
            sql += " AND m.superseded_by IS NULL"
        if wanted:
            sql += f" AND m.kind IN ({','.join('?' * len(wanted))})"
            parameters.extend(wanted)




        sql += """
            ORDER BY (relevance - (m.confidence * 0.15) - (MIN(m.use_count, 10) * 0.01)) ASC
            LIMIT ?
        """
        parameters.append(max(1, int(limit)))

        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        found = [self._record(row) for row in rows]
        self._note_use(found)
        return found

    def recent(self, *, limit: int = 20, kind: str | None = None) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE superseded_by IS NULL"
        parameters: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            parameters.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._record(row) for row in rows]

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return self._record(row) if row is not None else None

    def briefing(self, query: str, *, limit: int = 5) -> str:
        """The block of remembered context a task starts from.

        Empty when nothing matches, so a task about something new carries no
        memory preamble at all rather than an irrelevant one.
        """

        found = self.recall(query, limit=limit)
        if not found:
            return ""
        lines = "\n".join(f"- {record.for_model()}" for record in found)
        return (
            "What you already know about this user, remembered from earlier "
            f"work:\n{lines}"
        )

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT kind, COUNT(*) AS n FROM memories
                   WHERE superseded_by IS NULL GROUP BY kind"""
            ).fetchall()
            superseded = self._connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE superseded_by IS NOT NULL"
            ).fetchone()["n"]
        return {
            "schema": MEMORY_SCHEMA,
            "by_kind": {row["kind"]: row["n"] for row in rows},
            "active": sum(row["n"] for row in rows),
            "superseded": int(superseded),
        }



    def _note_use(self, records: list[MemoryRecord]) -> None:
        """Record that a memory earned its place, so useful ones surface."""

        if not records:
            return
        now = time.time()
        with self._lock, self._connection:
            self._connection.executemany(
                """UPDATE memories SET use_count = use_count + 1, last_used_at = ?
                   WHERE memory_id = ?""",
                [(now, record.memory_id) for record in records],
            )

    @staticmethod
    def _record(row: Mapping[str, Any]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            kind=row["kind"],
            subject=row["subject"],
            body=row["body"],
            confidence=row["confidence"],
            source=row["source"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=row["use_count"],
            superseded_by=row["superseded_by"],
            tags=tuple(json.loads(row["tags_json"] or "[]")),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def safe_to_remember(payload: Mapping[str, Any]) -> bool:
    """Whether a structured payload could be written without losing anything.

    Used to decide that a task result is not worth remembering because the
    only interesting part of it was a secret.
    """

    return json.dumps(redact(payload), sort_keys=True, default=str).find(REDACTED) < 0
