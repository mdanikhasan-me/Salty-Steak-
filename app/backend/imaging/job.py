"""Salty Steak Native Desktop AI Platform — image generation jobs.

The task object that makes an image request survive past its first attempt.

Without one, every generation is an island: the brief exists only as the string
that was sent, so "that's wrong, try again" has nothing to be a revision of.
A job holds the chain — the original request, the brief, the result, and every
revision — so a follow-up is always resolved against the work it refers to.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .brief import RenderBrief

JOB_SCHEMA = "salty-steak-image-job-v1"

QUEUED = "queued"
BRIEFING = "briefing"
GENERATING = "generating"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
DETACHED = "detached"

TERMINAL_JOB_STATES = frozenset({COMPLETED, FAILED, CANCELLED, DETACHED})


@dataclass
class ImageGenerationJob:
    """One image task, across every revision of it."""

    brief: RenderBrief
    conversation_id: str = ""
    task_id: str = ""
    job_id: str = field(default_factory=lambda: f"imgjob-{uuid.uuid4().hex[:12]}")
    source_message_ids: list[str] = field(default_factory=list)
    revision: int = 1
    parent_job_id: str | None = None
    previous_artifact: str | None = None
    user_feedback: str = ""
    status: str = QUEUED
    backend: str = ""
    artifact: str | None = None
    dimensions: str | None = None
    failure: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_JOB_STATES

    @property
    def seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.completed_at or time.time()) - self.started_at

    def revise(
        self,
        feedback: str,
        *,
        changes: Mapping[str, Any] | None = None,
        message_id: str = "",
    ) -> "ImageGenerationJob":
        """Start the next attempt, carrying this one's whole task forward.

        The linkage is the point. A revision that lost its parent would be a
        fresh request whose prompt is the word "again".
        """

        child = ImageGenerationJob(
            brief=self.brief.revise(feedback, changes=changes),
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            source_message_ids=[*self.source_message_ids, message_id]
            if message_id
            else list(self.source_message_ids),
            revision=self.revision + 1,
            parent_job_id=self.job_id,
            previous_artifact=self.artifact,
            user_feedback=str(feedback or "").strip(),
            backend=self.backend,
        )
        return child

    def to_dict(self, *, include_brief: bool = True) -> dict[str, Any]:
        """What the interface and the model may see.

        The rendered prompt is deliberately absent: the model reasons about the
        job, and a whole brief pasted into every turn is how context fills up
        with something nobody is reading.
        """

        payload: dict[str, Any] = {
            "schema": JOB_SCHEMA,
            "job_id": self.job_id,
            "status": self.status,
            "revision": self.revision,
            "parent_job_id": self.parent_job_id,
            "subject": self.brief.subject,
            "image_type": self.brief.image_type,
            "brand": self.brief.brand,
            "artifact": self.artifact,
            "dimensions": self.dimensions,
            "backend": self.backend,
            "seconds": round(self.seconds, 3) if self.seconds is not None else None,
            "failure": self.failure,
        }
        if include_brief:
            payload["brief"] = self.brief.to_dict()
        return payload

    def activity(self) -> dict[str, Any]:
        """A one-line description for the activity timeline.

        Says what is happening without dumping the prompt into the interface.
        """

        what = self.brief.brand or self.brief.subject
        label = f"{self.brief.image_type.replace('_', ' ').title()}: {what}"
        if self.revision > 1:
            label += f" (revision {self.revision})"
        return {
            "job_id": self.job_id,
            "label": label[:120],
            "status": self.status,
            "revision": self.revision,
            "seconds": round(self.seconds, 1) if self.seconds is not None else None,
        }


class ImageJobRegistry:
    """The image jobs belonging to a conversation, newest first.

    Small and in-memory: this is task state, not durable knowledge about the
    user, and it has no business in semantic memory.
    """

    def __init__(self, *, limit: int = 20) -> None:
        self._jobs: list[ImageGenerationJob] = []
        self._limit = limit

    def add(self, job: ImageGenerationJob) -> ImageGenerationJob:
        self._jobs.insert(0, job)
        del self._jobs[self._limit :]
        return job

    def get(self, job_id: str) -> ImageGenerationJob | None:
        return next((job for job in self._jobs if job.job_id == job_id), None)

    def latest(self, *, conversation_id: str | None = None) -> ImageGenerationJob | None:
        """The job a follow-up most likely refers to.

        The most recent one that actually produced something, falling back to
        the most recent attempt: someone saying "that's wrong" is talking about
        the picture they were just shown.
        """

        candidates = [
            job
            for job in self._jobs
            if conversation_id is None or job.conversation_id == conversation_id
        ]
        completed = [job for job in candidates if job.status == COMPLETED]
        return (completed or candidates or [None])[0]

    def chain(self, job_id: str) -> list[ImageGenerationJob]:
        """A job and everything it was revised from, oldest first."""

        found: list[ImageGenerationJob] = []
        current = self.get(job_id)
        while current is not None:
            found.append(current)
            current = (
                self.get(current.parent_job_id) if current.parent_job_id else None
            )
        return list(reversed(found))

    def __len__(self) -> int:
        return len(self._jobs)


__all__ = [
    "BRIEFING",
    "CANCELLED",
    "COMPLETED",
    "DETACHED",
    "FAILED",
    "GENERATING",
    "ImageGenerationJob",
    "ImageJobRegistry",
    "JOB_SCHEMA",
    "QUEUED",
    "TERMINAL_JOB_STATES",
]
