"""Salty Steak Native Desktop AI Platform — image generation as a capability."""

from .brief import BRIEF_SCHEMA, IMAGE_TYPES, RenderBrief, build_brief, infer_image_type
from .guard import GuardReport, inspect
from .job import ImageGenerationJob, ImageJobRegistry
from .orchestrator import ImageOrchestrationError, ImageOrchestrator

__all__ = [
    "BRIEF_SCHEMA",
    "GuardReport",
    "IMAGE_TYPES",
    "ImageGenerationJob",
    "ImageJobRegistry",
    "ImageOrchestrationError",
    "ImageOrchestrator",
    "RenderBrief",
    "build_brief",
    "infer_image_type",
    "inspect",
]
