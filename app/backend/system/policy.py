"""Salty Steak Native Desktop AI Platform — product identity and policy baseline.

One source of truth for how the product names itself and what it claims. UI
copy, manifests, diagnostics, and any future legal document should read from
here rather than restating a product name or a policy position inline, so a
single edit changes every surface at once.

Nothing in this module is legal advice or a finished agreement. It is the
structured baseline that formal terms will be authored on top of.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any


POLICY_SCHEMA = "salty-steak-policy-baseline-v1"

PRODUCT_NAME = "Salty Steak"
PRODUCT_TAGLINE = "Private offline desktop AI"
PLATFORM_NAME = "Salty Steak Native Desktop AI Platform"
VENDOR_NAME = "Salty Steak"



COMPONENT_NAMES = MappingProxyType(
    {
        "text_model": "Base Steak 2.0 (9B)",
        "vision_system": "Salty Multi-Modal Vision",
        "image_model": "Steak Gen 1",
        "fine_tuning_model": "Salty Potato (95M)",
        "core_runtime": "Salty Native Engine",
        "tensor_runtime": "Salty Tensor Engine",
        "inference_engine": "Salty Native Inference Engine",
        "vision_encoder": "Salty Visual Embedder",
        "web_search": "Web Index Search",
        "automation": "Computer Access",
    }
)



PRODUCT_CLAIMS = MappingProxyType(
    {
        "runs_offline": True,
        "external_inference_service_required": False,
        "creates_network_listener": False,
        "telemetry_collected": False,
        "conversations_stored_locally_only": True,


        "outbound_network_requests": "web_index_search_only_when_enabled",
        "automation_default_state": "every_capability_denied_until_granted",
        "automation_audited": True,
    }
)



OWNERSHIP = MappingProxyType(
    {
        "ownership_claim": "user_declared",
        "provenance_state": "user_declared_trained_and_fine_tuned",
        "weights_license": "USER_DECLARED_NOT_EMBEDDED_IN_ARTIFACT",
        "certified_by_platform": False,
    }
)






THIRD_PARTY_NOTICE_FILES = (
    "workspace/runtime/salty-native-steak35/THIRD_PARTY_LICENSE.txt",
    "workspace/runtime/salty-vision/THIRD_PARTY_LICENSE.txt",
)

LEGAL_NOTICES_DOCUMENT = "LEGAL_NOTICES.md"


def product_identity() -> dict[str, Any]:
    """Return the public identity block for diagnostics and about surfaces."""

    return {
        "schema": POLICY_SCHEMA,
        "product_name": PRODUCT_NAME,
        "tagline": PRODUCT_TAGLINE,
        "platform_name": PLATFORM_NAME,
        "vendor": VENDOR_NAME,
        "components": dict(COMPONENT_NAMES),
        "claims": dict(PRODUCT_CLAIMS),
        "ownership": dict(OWNERSHIP),
        "third_party_notices": list(THIRD_PARTY_NOTICE_FILES),
        "legal_notices_document": LEGAL_NOTICES_DOCUMENT,
    }


def component_name(key: str) -> str:
    """Return the approved public name for one component."""

    try:
        return COMPONENT_NAMES[key]
    except KeyError as error:
        raise KeyError(f"No approved public name for component: {key}") from error
