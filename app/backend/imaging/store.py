"""Salty Steak Native Desktop AI Platform — durable image job records.

Enough of an image task to pick it up again tomorrow.

A desktop assistant gets closed. If image jobs live only in memory, reopening a
conversation and saying "make that logo more geometric" produces a six-word
prompt about nothing, which is the failure this whole area exists to fix.

What is stored is deliberately narrow: the brief and its provenance, never the
picture. The image file stays where the artifact system already keeps it, and
none of this reaches semantic memory — a render brief is task state, not a
durable fact about the user.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..system.timestamps import utc_now_timestamp as utc_now
from .brief import RenderBrief
from .job import ImageGenerationJob


class ImageJobStore:
    """Read and write image job metadata alongside the conversation."""

    def __init__(self, database: Any) -> None:
        self.database = database



    def save(self, job: ImageGenerationJob) -> ImageGenerationJob:
        """Insert or update one job. Safe to call at every state change."""

        brief = job.brief
        self.database.execute(
            """
            INSERT INTO image_generation_jobs (
                job_id, conversation_id, task_id, parent_job_id, revision,
                status, subject, image_type, brand, brief_json,
                original_request, user_feedback, source_message_ids_json,
                artifact, dimensions, backend, failure, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                brief_json = excluded.brief_json,
                artifact = excluded.artifact,
                dimensions = excluded.dimensions,
                backend = excluded.backend,
                failure = excluded.failure,
                completed_at = excluded.completed_at
            """,
            (
                job.job_id,
                job.conversation_id,
                job.task_id or None,
                job.parent_job_id,
                int(job.revision),
                job.status,
                brief.subject[:500],
                brief.image_type,
                brief.brand or None,
                json.dumps(brief.to_dict()),
                brief.original_request[:8_000],
                job.user_feedback[:2_000] or None,
                json.dumps(list(job.source_message_ids)),
                job.artifact,
                job.dimensions,
                job.backend or None,
                job.failure,
                utc_now(),
                utc_now() if job.finished else None,
            ),
        )
        return job



    def get(self, job_id: str) -> ImageGenerationJob | None:
        row = self.database.fetch_one(
            "SELECT * FROM image_generation_jobs WHERE job_id = ?", (job_id,)
        )
        return self._rebuild(row) if row else None

    def latest_for_conversation(
        self, conversation_id: str
    ) -> ImageGenerationJob | None:
        """The job a follow-up in THIS conversation refers to.

        Scoped to the conversation on purpose. A global "most recent image"
        would let a revision in one conversation silently rewrite the last
        picture made in another.
        """

        row = self.database.fetch_one(
            """
            SELECT * FROM image_generation_jobs
            WHERE conversation_id = ?
            ORDER BY CASE WHEN status = 'completed' THEN 0 ELSE 1 END,
                     created_at DESC, revision DESC
            LIMIT 1
            """,
            (conversation_id,),
        )
        return self._rebuild(row) if row else None

    def for_conversation(
        self, conversation_id: str, *, limit: int = 20
    ) -> list[ImageGenerationJob]:
        rows = self.database.fetch_all(
            """
            SELECT * FROM image_generation_jobs
            WHERE conversation_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (conversation_id, int(limit)),
        )
        return [self._rebuild(row) for row in rows]

    def delete_for_conversation(self, conversation_id: str) -> int:
        cursor = self.database.execute(
            "DELETE FROM image_generation_jobs WHERE conversation_id = ?",
            (conversation_id,),
        )
        return getattr(cursor, "rowcount", 0) or 0



    @staticmethod
    def _rebuild(row: Mapping[str, Any]) -> ImageGenerationJob:
        payload = json.loads(row["brief_json"])
        payload.pop("schema", None)
        payload.pop("brief_id", None)
        brief = RenderBrief(**payload)
        brief.original_request = row["original_request"] or ""

        job = ImageGenerationJob(
            brief=brief,
            conversation_id=row["conversation_id"],
            task_id=row["task_id"] or "",
            job_id=row["job_id"],
            source_message_ids=json.loads(row["source_message_ids_json"] or "[]"),
            revision=int(row["revision"]),
            parent_job_id=row["parent_job_id"],
            user_feedback=row["user_feedback"] or "",
            status=row["status"],
            backend=row["backend"] or "",
            artifact=row["artifact"],
            dimensions=row["dimensions"],
            failure=row["failure"],
        )
        return job


__all__ = ["ImageJobStore"]
