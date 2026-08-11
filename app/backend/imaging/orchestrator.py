"""Salty Steak Native Desktop AI Platform — image job orchestration.

Turns a decision the text model made into a checked, self-sufficient image job.

The text model is the brain: it decides that a request is an image request and
describes what to draw. It does not render anything. This is the layer between
that decision and the generation engine — it builds the brief, resolves a
follow-up against the job it refers to, checks the result still matches what
was asked for, and only then spends GPU time.

Correction is bounded to one round. A model that produced a broken brief once
will often produce a similar one again, and an unbounded repair loop burns the
whole turn budget looking busy.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from .brief import RenderBrief, build_brief
from .guard import GuardReport, inspect
from .job import (
    BRIEFING,
    CANCELLED,
    COMPLETED,
    DETACHED,
    FAILED,
    GENERATING,
    ImageGenerationJob,
    ImageJobRegistry,
)

ORCHESTRATOR_SCHEMA = "salty-steak-image-orchestrator-v1"



MAX_BRIEF_CORRECTIONS = 1




BRIEF_AUTHOR_MAX_TOKENS = 128

CONTEXTUAL_IMAGE_REFERENCE = re.compile(
    r"\b(?:"
    r"what\s+(?:you|we)\s+(?:just\s+)?(?:described|said|wrote|mentioned|imagined|outlined|explained|discussed)"
    r"|(?:generate|create|make|draw|render|visuali[sz]e)\b[^.!?\n]{0,60}\b(?:it|that|this|those)\b"
    r"|(?:the\s+)?(?:above|earlier|previous|prior)\s+(?:answer|description|idea|scene|concept|design|conversation|discussion)"
    r"|(?:our|this|the)\s+(?:conversation|discussion)"
    r"|turn\s+(?:it|that|this)\s+into"
    r")",
    re.IGNORECASE,
)





BRIEF_AUTHOR_INSTRUCTION = """\
You are writing the render brief for a local diffusion image model.

Turn the request into concrete visual instruction. Be specific about the \
subject, the framing, the lighting, the medium or lens, materials, colour and \
the background. Say what must NOT appear in negative_constraints — the image \
model cannot read a prohibition anywhere else.

Reply with ONE JSON object and nothing else:
{"subject":"","image_type":"logo|icon|illustration|photograph|diagram|poster|\
pattern|concept_art|ui_mockup","goal":"","brand":"","style":"","composition":"",\
"colour":"","background":"","text_content":"","required_elements":[],\
"negative_constraints":[]}

Keep the subject the one that was asked for. Elaborate it; never replace it. \
Leave a field out rather than inventing a brand, a slogan or wording the \
request does not have."""




BRIEF_AUTHOR_INSTRUCTION = """\
Write one compact render brief for a local diffusion image model.

Reply with ONE JSON object and nothing else. Use short concrete phrases, not \
paragraphs. Keep the requested subject unchanged. Include only useful fields:
{"subject":"","image_type":"logo|icon|illustration|photograph|diagram|poster|\
pattern|concept_art|ui_mockup","style":"","composition":"",\
"required_elements":[],"negative_constraints":[]}

Use at most 2 short required_elements and 2 short negative_constraints. Put \
prohibitions only in negative_constraints. Omit brand, wording, or details the \
request did not provide. The complete JSON must fit within 128 tokens."""


class ImageOrchestrationError(RuntimeError):
    """Raised when an image job cannot be prepared or run."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


class ImageOrchestrator:
    """Prepare, validate and run image jobs on behalf of the text model."""

    def __init__(
        self,
        *,
        generate: Callable[[RenderBrief, ImageGenerationJob], Mapping[str, Any]] | None = None,
        registry: ImageJobRegistry | None = None,
        task: Any = None,
        rebrief: Callable[[str, RenderBrief], Mapping[str, Any] | None] | None = None,
        author: Callable[..., Mapping[str, Any] | None] | None = None,
        backend: str = "",
    ) -> None:
        self.generate = generate
        self.registry = registry or ImageJobRegistry()
        self.task = task

        self.rebrief = rebrief






        self.author = author
        self.backend = backend



    def prepare(
        self,
        decision: Mapping[str, Any],
        *,
        original_request: str,
        latest_request: str = "",
        reference_context: str = "",
        notes: str = "",
        conversation_id: str = "",
        message_id: str = "",
    ) -> ImageGenerationJob:
        """Build a new image job from a generate_image decision.

        ``original_request`` is the recent thread, so a brief keeps the task a
        follow-up refers to. ``latest_request`` is only what was just asked,
        and it is the better fallback subject: a brief with no subject of its
        own used to take the first two hundred characters of the joined
        thread, which is several turns of somebody else's request.
        """

        self._event("image_brief_started", request=original_request[:200])





        latest = str(latest_request or "")
        validation_request = _validation_request(
            latest_request=latest,
            original_request=original_request,
            reference_context=reference_context,
        )
        supplied_brief = decision.get("brief") or decision.get("arguments") or decision
        brief = build_brief(
            supplied_brief,
            original_request=original_request,
            fallback_subject=latest,
        )
        if CONTEXTUAL_IMAGE_REFERENCE.search(latest) and str(reference_context).strip():




            brief.goal = str(reference_context).strip()[:2_000]
        if not _model_brief_is_render_ready(supplied_brief, brief):
            brief = self._author_brief(
                brief,
                request=original_request,
                validation_request=validation_request,
                notes=notes,
            )





        brief = self._enforce(brief, validation_request)
        job = ImageGenerationJob(
            brief=brief,
            conversation_id=conversation_id,
            task_id=getattr(self.task, "task_id", "") or "",
            source_message_ids=[message_id] if message_id else [],
            backend=self.backend,
            status=BRIEFING,
        )
        self.registry.add(job)
        self._event("image_brief_completed", **job.activity())
        return job

    def prepare_revision(
        self,
        decision: Mapping[str, Any],
        *,
        feedback: str,
        conversation_id: str = "",
        message_id: str = "",
        parent_job_id: str | None = None,
    ) -> ImageGenerationJob:
        """Build the next revision of an existing job.

        This is the fix for the failure that started all of this. A follow-up
        such as "that's wrong, generate the logo" is resolved against the job
        it refers to, so the brand, the deliverables and the forbidden motifs
        all survive into the new attempt instead of being replaced by the six
        words the user just typed.
        """

        parent = (
            self.registry.get(parent_job_id)
            if parent_job_id
            else self.registry.latest(conversation_id=conversation_id or None)
        )
        if parent is None:


            return self.prepare(
                decision,
                original_request=feedback,
                latest_request=feedback,
                conversation_id=conversation_id,
                message_id=message_id,
            )

        self._event("image_revision_started", parent=parent.job_id, feedback=feedback[:200])
        changes = dict(decision.get("changes") or decision.get("brief") or {})


        changes.pop("original_request", None)
        job = parent.revise(feedback, changes=changes, message_id=message_id)
        job.status = BRIEFING
        job.backend = self.backend
        self.registry.add(job)
        self._event("image_brief_completed", **job.activity())
        return job



    def _author_brief(
        self,
        brief: RenderBrief,
        *,
        request: str,
        validation_request: str = "",
        notes: str = "",
    ) -> RenderBrief:
        """Let the text model write the brief out properly before rendering.

        The decision that routes a turn is made in the same breath as the
        answer, so the brief inside it is whatever fitted there. "cow" and
        "realistic" is not a description of a photograph, and a diffusion model
        given three words draws three words.

        Fails open in every direction. If the model is unreachable, replies
        with prose, or returns something that no longer describes what was
        asked for, the brief that came out of the decision is used unchanged —
        a thin picture beats no picture, and the guard still runs afterwards.
        """

        if self.author is None:
            return brief
        self._event("image_brief_authoring", subject=brief.subject[:120])
        try:
            payload = self.author(request=request, brief=brief, notes=notes)
        except Exception as error:
            self._event("image_brief_authoring_failed", error=str(error)[:200])
            return brief
        if not isinstance(payload, Mapping) or not payload:
            return brief

        merged = brief.to_dict()
        merged.pop("schema", None)
        merged.pop("brief_id", None)
        for key, value in payload.items():
            if key not in merged or value in (None, "", [], {}):
                continue
            merged[key] = value
        candidate = build_brief(merged, original_request=brief.original_request or request)



        if not inspect(candidate, request=validation_request or request).ok:
            self._event("image_brief_authoring_rejected", subject=candidate.subject[:120])
            return brief
        self._event("image_brief_authored", subject=candidate.subject[:120])
        return candidate



    def _enforce(self, brief: RenderBrief, request: str) -> RenderBrief:
        """Refuse to generate from a brief that no longer matches the request."""

        report = inspect(brief, request=request)
        if report.ok:
            return brief

        self._event("image_brief_rejected", issues=[i.kind for i in report.blocking])
        if self.rebrief is None:
            raise ImageOrchestrationError(
                report.correction_request(), kind="brief_mismatch"
            )

        for _ in range(MAX_BRIEF_CORRECTIONS):
            try:
                repaired = self.rebrief(report.correction_request(), brief)
            except Exception as error:
                raise ImageOrchestrationError(
                    f"The brief could not be corrected: {error}", kind="brief_mismatch"
                ) from error
            if not repaired:
                break
            candidate = build_brief(repaired, original_request=request)
            report = inspect(candidate, request=request)
            if report.ok:
                self._event("image_brief_corrected", subject=candidate.subject[:120])
                return candidate




        raise ImageOrchestrationError(
            report.correction_request(), kind="brief_mismatch"
        )



    def run(self, job: ImageGenerationJob) -> ImageGenerationJob:
        """Generate the image, honouring cancellation on both sides of it."""

        if self.generate is None:
            job.status = FAILED
            job.failure = "No image generation backend is configured."
            self._event("image_generation_failed", **job.activity())
            raise ImageOrchestrationError(job.failure, kind="backend_unavailable")

        if self._stopped():
            job.status = CANCELLED
            self._event("image_generation_cancelled", **job.activity())
            return job

        job.status = GENERATING
        job.started_at = time.monotonic()
        self._event("image_generation_started", **job.activity())

        try:
            result = dict(self.generate(job.brief, job) or {})
        except BaseException as error:
            job.status = FAILED
            job.completed_at = time.monotonic()
            job.failure = f"{type(error).__name__}: {error}"[:500]
            self._event("image_generation_failed", **job.activity())
            raise ImageOrchestrationError(job.failure, kind="generation_failed") from error

        job.completed_at = time.monotonic()
        if self.task is not None:
            self.task.metrics.image_model_calls += 1
            self.task.metrics.image_generation_seconds += job.seconds or 0.0

        if self._stopped():



            job.status = DETACHED
            job.artifact = result.get("artifact")
            self._event("image_generation_cancelled", **job.activity())
            return job

        job.status = COMPLETED
        job.artifact = result.get("artifact") or result.get("path")
        job.dimensions = result.get("dimensions")
        job.backend = str(result.get("backend") or job.backend or "")
        self._event("image_generation_completed", **job.activity())
        return job

    def result_for_model(self, job: ImageGenerationJob) -> dict[str, Any]:
        """What the text model is told about a finished job.

        Metadata only. The image itself goes to the interface; putting pixels
        or a whole brief back into the prompt buys nothing and costs context.
        """

        return {
            "action": "generate_image",
            "status": job.status,
            "job_id": job.job_id,
            "revision": job.revision,
            "subject": job.brief.subject,
            "image_type": job.brief.image_type,
            "artifact": job.artifact,
            "dimensions": job.dimensions,
            "backend": job.backend,
            "seconds": round(job.seconds, 2) if job.seconds is not None else None,
            "failure": job.failure,
        }



    def _event(self, kind: str, **detail: Any) -> None:
        if self.task is not None:
            self.task.record_event(kind, **detail)

    def _stopped(self) -> bool:
        return bool(self.task is not None and self.task.stop_requested)


def _validation_request(
    *,
    latest_request: str,
    original_request: str,
    reference_context: str,
) -> str:
    """Ground a deictic image request in the conversation it points at."""

    latest = str(latest_request or "").strip()
    original = str(original_request or "").strip()
    reference = str(reference_context or "").strip()
    if not latest:
        return original
    if CONTEXTUAL_IMAGE_REFERENCE.search(latest) is None:
        return latest
    parts = [latest]
    if reference:
        parts.append(reference)
    if original and original != latest:
        parts.append(original)
    return "\n\n".join(parts)


def _model_brief_is_render_ready(
    payload: object,
    brief: RenderBrief,
) -> bool:
    """Whether the routing model already supplied a useful render handoff.

    A complete first decision should not trigger a second 50-second JSON pass.
    The exact request remains in ``brief.goal``, so skipping enrichment cannot
    lose details the user supplied. Contextual placeholders and thin fallback
    briefs still use the authoring model once.
    """

    if not isinstance(payload, Mapping):
        return False
    subject = str(payload.get("subject") or "").strip()
    if not subject or CONTEXTUAL_IMAGE_REFERENCE.search(subject):
        return False
    descriptive = any(
        bool(payload.get(key))
        for key in (
            "goal",
            "style",
            "composition",
            "colour",
            "color",
            "background",
            "required_elements",
            "negative_constraints",
        )
    )
    return descriptive and inspect(brief, request=brief.original_request).ok


__all__ = [
    "ImageOrchestrationError",
    "ImageOrchestrator",
    "BRIEF_AUTHOR_MAX_TOKENS",
    "CONTEXTUAL_IMAGE_REFERENCE",
    "MAX_BRIEF_CORRECTIONS",
    "ORCHESTRATOR_SCHEMA",
]
