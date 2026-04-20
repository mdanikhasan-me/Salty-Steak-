"""Salty Steak Native Desktop AI Platform — render briefs.

What the image model is actually told, and the proof that it still says what
the user asked for.

The old path sent the latest user message straight to the image model. That
works exactly once. The moment someone says "that's wrong, generate the logo",
the entire original brief — brand, deliverables, forbidden motifs — is gone,
and the image model is asked to produce a logo it knows nothing about.

A render brief is the durable form of the request. It is built once from what
the user actually said, carried across revisions, and merged with feedback
rather than replaced by it. It is also checked before any GPU time is spent:
a brief whose subject has drifted away from the goal is a broken handoff, and
generating from it would waste minutes to produce something nobody wanted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

BRIEF_SCHEMA = "salty-steak-render-brief-v1"



IMAGE_TYPES = (
    "logo",
    "icon",
    "illustration",
    "photograph",
    "diagram",
    "poster",
    "pattern",
    "concept_art",
    "ui_mockup",
    "other",
)


TYPE_HINTS = {
    "logo": ("logo", "wordmark", "brand identity", "brandmark", "monogram", "lockup"),
    "icon": ("icon", "app icon", "favicon", "glyph", "pictogram"),
    "photograph": ("photo", "photograph", "photorealistic", "portrait", "headshot"),
    "illustration": ("illustration", "drawing", "artwork", "painting", "sketch"),
    "diagram": ("diagram", "chart", "flowchart", "schematic", "infographic"),
    "poster": ("poster", "flyer", "banner", "cover"),
    "ui_mockup": ("mockup", "wireframe", "interface", "screen design"),
    "pattern": ("pattern", "texture", "background tile"),
    "concept_art": ("concept art", "character design", "environment art"),
}



NEGATION_LEAD = re.compile(
    r"\b(?:no|not|never|avoid|without|exclude|excluding|don'?t(?:\s+use)?|"
    r"do\s+not(?:\s+use)?|forbidden|banned|refrain\s+from)\b[:\s]*",
    re.IGNORECASE,
)

MAX_BRIEF_CHARACTERS = 6_000
MAX_ITEMS = 40










REQUEST_WRAPPER = re.compile(
    r"^\s*(?:please\s+)?(?:\w+[\s,]+){0,3}?"
    r"\b(?:an?|the|some)\s+"
    r"(?:image|picture|photo|photograph|drawing|illustration|render|rendering|"
    r"visual|visualisation|visualization|graphic|artwork|painting|sketch)s?\s+"
    r"(?:of|showing|depicting|featuring|with)\s+",
    re.IGNORECASE,
)


def subject_from_request(text: str) -> str:
    """What a request is asking for, without the asking."""

    cleaned = _clean(text)
    stripped = REQUEST_WRAPPER.sub("", cleaned, count=1)
    stripped = stripped.strip().rstrip(".!").strip()
    return stripped or cleaned


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _unique(values: Iterable[str], *, limit: int = MAX_ITEMS) -> list[str]:
    """Keep order, drop repeats, and cap the list."""

    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        text = _clean(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(text)
        if len(kept) >= limit:
            break
    return kept


def infer_image_type(text: str) -> str:
    """Work out what sort of image is wanted from how it was described.

    A fallback for when the model does not say. It is a hint, never an
    override: an explicit type from the model always wins.
    """

    lowered = str(text or "").casefold()
    best, best_position = "other", len(lowered) + 1
    for kind, hints in TYPE_HINTS.items():
        for hint in hints:
            position = lowered.find(hint)
            if position >= 0 and position < best_position:
                best, best_position = kind, position
    return best


def extract_negatives(text: str) -> list[str]:
    """Pull prohibitions out of a request written in prose.

    Deliberately generous. A forbidden motif that slips through is the exact
    failure this exists to prevent, and a slightly over-eager negative costs
    only a little prompt space.
    """

    found: list[str] = []
    for line in re.split(r"[\n;•\-–]+|(?<=[.!?])\s+", str(text or "")):
        stripped = _clean(line)
        if not stripped:
            continue
        match = NEGATION_LEAD.search(stripped)
        if match is None:
            continue
        remainder = _clean(stripped[match.end() :])

        if len(remainder.split()) < 1 or len(remainder) < 2:
            continue
        found.append(remainder[:120])
    return _unique(found)


@dataclass
class RenderBrief:
    """A self-sufficient description of one image to produce.

    Self-sufficient on purpose: the image model is never assumed to remember
    anything. Every generation carries the whole task, so a revision cannot
    quietly become a blank request.
    """

    subject: str
    image_type: str = "other"
    goal: str = ""
    brand: str = ""
    style: str = ""
    composition: str = ""
    colour: str = ""
    background: str = ""
    text_content: str = ""
    aspect_ratio: str = ""
    deliverables: list[str] = field(default_factory=list)
    required_elements: list[str] = field(default_factory=list)
    optional_elements: list[str] = field(default_factory=list)
    negative_constraints: list[str] = field(default_factory=list)


    original_request: str = ""
    revision_note: str = ""
    brief_id: str = field(default_factory=lambda: f"brief-{uuid.uuid4().hex[:10]}")
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.subject = _clean(self.subject)
        if self.image_type not in IMAGE_TYPES:
            self.image_type = infer_image_type(
                f"{self.image_type} {self.subject} {self.goal} {self.original_request}"
            )
        self.deliverables = _unique(self.deliverables)
        self.required_elements = _unique(self.required_elements)
        self.optional_elements = _unique(self.optional_elements)
        self.negative_constraints = _unique(self.negative_constraints)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": BRIEF_SCHEMA,
            "brief_id": self.brief_id,
            "subject": self.subject,
            "image_type": self.image_type,
            "goal": self.goal,
            "brand": self.brand,
            "style": self.style,
            "composition": self.composition,
            "colour": self.colour,
            "background": self.background,
            "text_content": self.text_content,
            "aspect_ratio": self.aspect_ratio,
            "deliverables": list(self.deliverables),
            "required_elements": list(self.required_elements),
            "optional_elements": list(self.optional_elements),
            "negative_constraints": list(self.negative_constraints),
            "revision_note": self.revision_note,
        }

    def render(self) -> str:
        """The positive text handed to the image model.

        Structured rather than conversational: the model gets the task, not a
        transcript.

        Prohibitions are deliberately absent. A diffusion model has no negation
        in its positive conditioning — every token in this text is something to
        draw towards. Writing "DO NOT INCLUDE: text" into it does not remove
        text from the picture; it asks for it, and the first live render put the
        words DO NOT INCLUDE across the middle of the image. What the user ruled
        out belongs in ``render_negative``, which goes to the negative
        conditioning where it can actually subtract.
        """

        lines: list[str] = []


        headline = (
            "image" if self.image_type == "other" else self.image_type.replace("_", " ")
        )
        lines.append(f"TASK: {headline} — {self.subject}")
        if self.brand:
            lines.append(f"BRAND: {self.brand}")
        if self.goal:
            lines.append(f"PURPOSE: {self.goal}")
        if self.deliverables:
            lines.append("DELIVERABLES:")
            lines.extend(f"  {index}. {item}" for index, item in enumerate(self.deliverables, 1))
        if self.required_elements:
            lines.append("MUST INCLUDE: " + "; ".join(self.required_elements))
        if self.style:
            lines.append(f"STYLE: {self.style}")
        if self.composition:
            lines.append(f"COMPOSITION: {self.composition}")
        if self.colour:
            lines.append(f"COLOUR: {self.colour}")
        if self.background:
            lines.append(f"BACKGROUND: {self.background}")
        if self.text_content:
            lines.append(f"TEXT IN IMAGE: {self.text_content}")
        if self.aspect_ratio:
            lines.append(f"ASPECT RATIO: {self.aspect_ratio}")
        if self.optional_elements:
            lines.append("MAY INCLUDE: " + "; ".join(self.optional_elements))
        if self.revision_note:
            lines.append(f"THIS REVISION CHANGES: {self.revision_note}")
        text = "\n".join(lines)
        return text[:MAX_BRIEF_CHARACTERS]

    def render_negative(self) -> str:
        """What the image model should steer away from.

        Kept separate from ``render`` so it reaches negative conditioning
        instead of being drawn. A backend without negative conditioning simply
        ignores it, which is honest: the constraint was never going to be
        honoured there, and putting it in the positive prompt made it worse
        rather than merely ineffective.
        """

        if not self.negative_constraints:
            return ""
        return ", ".join(self.negative_constraints)[:MAX_BRIEF_CHARACTERS]



    def revise(self, feedback: str, *, changes: Mapping[str, Any] | None = None) -> "RenderBrief":
        """Produce the next brief, keeping everything not explicitly changed.

        The user should never have to restate a brief they already gave. A
        revision inherits the whole task and overlays only what the feedback
        actually alters.
        """

        payload = self.to_dict()
        payload.pop("schema", None)
        payload.pop("brief_id", None)
        merged = dict(payload)

        for key, value in dict(changes or {}).items():
            if key not in merged or value in (None, "", [], {}):
                continue
            if isinstance(merged[key], list) and isinstance(value, list):


                merged[key] = _unique([*merged[key], *value])
            else:
                merged[key] = value

        extra_negatives = extract_negatives(feedback)
        if extra_negatives:
            merged["negative_constraints"] = _unique(
                [*merged["negative_constraints"], *extra_negatives]
            )

        revised = RenderBrief(**merged)
        revised.original_request = self.original_request
        revised.revision_note = _clean(feedback)[:600]
        return revised


def build_brief(
    payload: Mapping[str, Any] | None,
    *,
    original_request: str,
    fallback_subject: str = "",
) -> RenderBrief:
    """Turn the model's structured brief into a validated one.

    Whatever the model omits is recovered from what the user actually wrote.
    The model is good at structuring a request and unreliable at carrying every
    prohibition across, so the user's own words remain the backstop.
    """

    payload = dict(payload or {})
    request = _clean(original_request)



    subject = _clean(payload.get("subject"))
    if not subject:
        subject = subject_from_request(fallback_subject or request)[:200]
    declared = str(payload.get("image_type") or "").strip().casefold()



    image_type = declared if declared in IMAGE_TYPES else infer_image_type(
        f"{declared} {subject} {payload.get('goal') or ''} {request}"
    )




    negatives = _unique(
        [
            *(payload.get("negative_constraints") or []),
            *extract_negatives(request),
        ]
    )

    brief = RenderBrief(
        subject=subject,
        image_type=image_type,
        goal=_clean(payload.get("goal")),
        brand=_clean(payload.get("brand")),
        style=_clean(payload.get("style")),
        composition=_clean(payload.get("composition")),
        colour=_clean(payload.get("colour") or payload.get("color")),
        background=_clean(payload.get("background")),
        text_content=_clean(payload.get("text_content")),
        aspect_ratio=_clean(payload.get("aspect_ratio")),
        deliverables=list(payload.get("deliverables") or []),
        required_elements=list(payload.get("required_elements") or []),
        optional_elements=list(payload.get("optional_elements") or []),
        negative_constraints=negatives,
        original_request=request,
    )
    return brief


__all__ = [
    "BRIEF_SCHEMA",
    "IMAGE_TYPES",
    "MAX_BRIEF_CHARACTERS",
    "RenderBrief",
    "build_brief",
    "extract_negatives",
    "infer_image_type",
]
