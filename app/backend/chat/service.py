"""Conversation persistence and runtime backed chat orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..automation import AutomationBroker
from ..automation.routing import describe_routing, requires_sight
from ..database.control import Database, json_text, new_id, parse_json, utc_now
from ..operations.manager import (
    Notification,
    OperationContext,
    OperationInterrupted,
    OperationManager,
)
from ..runtime.manager import RuntimeManager
from ..runtime.steak_gen import (
    SteakGenCancelled,
    SteakGenRequest,
    SteakGenWorkerClient,
)
from ..runtime.salty_native_worker import SaltyNativeWorkerRuntime
from ..runtime.salty_vision import (
    BASE_STEAK_PUBLIC_NAME,
    VISION_RUNTIME_ID,
    SaltyVisionBroker,
    SaltyVisionCancelled,
    VisionInputPermission,
    VisionPermissionLease,
)
from ..imaging.store import ImageJobStore
from ..memory import SemanticMemory
from ..system.config import AppConfig
from ..system.environment import HostEnvironmentRegistry, seed_world_state
from ..tooling.web_search import WebSearchClient
from ..versions.tokenizer import CHAT_TEMPLATE_VERSION
from .actions import (
    FILE_TRASH_ACTION,
    HOST_ACTION_SCHEMA,
    IMAGE_ACTION,
    TEMP_CLEANUP_ACTION,
    _image_runtime_reason,
    TEMP_CLEANUP_CONFIRMATION,
    build_file_trash_proposal,
    build_temp_cleanup_proposal,
    detect_host_action_intent,
    extract_file_trash_target,
    host_action_planning_prompt,
    normalise_host_action_response,
)
from .agent_loop import VISION_OUTPUT_TOKENS, VISION_PROMPT
from .task_runtime import TaskContext, bind_operation_stop
from .vision_inputs import VisionInputClaim, VisionInputStore


ActivationStarter = Callable[[str, str | None], dict[str, Any]]
MAX_CONVERSATION_TITLE_LENGTH = 80
MAX_LABEL_NAME_LENGTH = 60



LABEL_TONES = ("neutral", "warm", "blue", "green", "violet", "amber", "red")


_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
_THINK_UNCLOSED = re.compile(r"<think>(.*)$", re.IGNORECASE | re.DOTALL)


def _separate_reasoning(text: Any) -> tuple[str, str]:
    """Split a reply into what to show and what the model was working through.

    A turn that runs out of output while still inside the block leaves it
    unclosed; that is still reasoning and still must not be shown as the
    answer, so it is treated the same way.
    """

    body = str(text or "")
    thoughts: list[str] = []

    def take(match: re.Match[str]) -> str:
        thoughts.append(match.group(1).strip())
        return ""

    body = _THINK_BLOCK.sub(take, body)
    body = _THINK_UNCLOSED.sub(take, body)
    return body.strip(), "\n\n".join(part for part in thoughts if part).strip()


_CONTROL_TOKEN = re.compile(r"/(?:no_)?think\b", re.IGNORECASE)


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _without_control_token_echo(text: str) -> str:
    """Remove any sentence in which the model quoted our own reasoning switch.

    ``/think`` and ``/no_think`` are appended to the user's own message to
    select the reasoning lane — that is how the model's soft switch works, and
    the template boundary depends on it. On a short message the directive is a
    large fraction of what the model sees, and it sometimes reads it as
    something the person typed. Observed on the installed application, Cooking,
    to the message "hi":

        Since you included `/think`, did you have a specific image in mind?

    The token is ours and must never reach the reader. Removing just the token
    would leave "Since you included ``, did you have", so the clause goes with
    it. Every answer that does not mention one is returned untouched, which is
    almost all of them.
    """

    body = str(text or "")
    if not _CONTROL_TOKEN.search(body):
        return body

    kept = [
        part
        for part in _SENTENCE.split(body)
        if part.strip() and not _CONTROL_TOKEN.search(part)
    ]
    cleaned = " ".join(part.strip() for part in kept).strip()


    return cleaned or _CONTROL_TOKEN.sub("that", body).strip()















PRIVATE_DETAIL_FIELDS = frozenset(
    {"reasoning_text", "unperformed_claim", "route_trace"}
)
PRIVATE_ORCHESTRATION_FIELDS = frozenset({"unreadable_decision"})


def public_technical_details(details: Any) -> Any:
    """The turn's record with its private channels withheld.

    Drawn at the API rather than in the interface. Reasoning stays in the
    database, where it is legitimate diagnostics, and simply never enters the
    payload — so no later frontend change can render it back, and reopening a
    conversation written before this existed is covered too.

    What replaces it is a fact rather than a quotation: that reasoning
    happened, and how much of it. Telemetry is not the thing being removed.
    """

    if not isinstance(details, Mapping):
        return details

    public = {
        key: value for key, value in details.items() if key not in PRIVATE_DETAIL_FIELDS
    }
    orchestration = public.get("orchestration")
    if isinstance(orchestration, Mapping):
        public["orchestration"] = {
            key: value
            for key, value in orchestration.items()
            if key not in PRIVATE_ORCHESTRATION_FIELDS
        }

    reasoning = str(details.get("reasoning_text") or "")
    public["reasoned"] = bool(reasoning.strip())
    public["reasoning_characters"] = len(reasoning)
    return public


def _reads_as_unnecessary(reply: Any) -> bool:
    """Whether the necessity check clearly said the request needs no capability.

    Fails open in every direction. Getting the model to route at all took four
    recorded sessions of work, so a check that swallows good routes when it
    errors, times out or waffles would cost far more than the restraint it
    buys. Only an explicit verdict stops a route; anything else lets it run.
    """

    text = str(reply or "").strip().casefold()
    if not text:
        return False



    first = text.split()[0].strip(".,:;!\"'`*-")
    return first == "conversation"


def _visible_output_tokens(
    answer: str, generations: Sequence[tuple[str, int]]
) -> int | None:
    """The measured token count for the text the reader was actually shown.

    A multi-paragraph research answer was labelled "1 output tokens". The count
    was `generated_output_tokens` from the routing generation, whose reply was
    discarded the moment the turn routed; the visible answer came from the
    research finaliser several generations later. A number measured on text
    nobody saw, printed under text it does not describe.

    A turn can run several generations, so the count reported is the one
    belonging to the answer that was delivered. When no generation produced it —
    because the application composed the sentence itself — there is nothing to
    measure and ``None`` is returned. Nothing is estimated: a wrong number is
    worse than an absent one, which is the whole lesson of the original defect.
    """

    wanted = str(answer or "").strip()
    if not wanted:
        return None

    for text, tokens in reversed(list(generations)):
        if str(text or "").strip() == wanted:
            return max(0, int(tokens))
    return None


def _turn_completion(
    details: Mapping[str, Any],
    *,
    response: Any,
    turn: Any,
    proposal_pending: bool,
) -> str:
    """How the turn ended, in the product's own words.

    Instant and Cooking are two different promises and they keep two different
    completions: a Cooking turn that finishes says it was cooked, permanently,
    rather than being flattened back into a generic "Done". Everything else
    reports what actually happened — a cancelled turn is never "Done in 8s".
    """

    if getattr(response, "cancelled", False):
        return "stopped"
    if proposal_pending:


        return "rendering" if details.get("image_render_started") else "waiting"













    if (details.get("orchestration") or {}).get("decision_unparsable"):
        return "failed"

    status = str((getattr(turn, "details", {}) or {}).get("status") or "")
    if status in {"failed", "rejected", "declined"}:
        return "failed"
    if status in {"exhausted", "needs_review", "blocked"}:


        return "partial"
    if status == "waiting":
        return "waiting"
    if str(details.get("finish_reason") or "") == "maximum_output":
        return "partial"

    cooking = str(details.get("reasoning_mode_effective") or "").strip().lower()
    ordinary = "cooked" if cooking == "cooking" else "done"








    if not _turn_reached_outside(details):
        return ordinary




    return "zonted" if details.get("goal_verified") is True else "partial"


def _turn_reached_outside(details: Mapping[str, Any]) -> bool:
    """Whether this turn did anything beyond composing a reply.

    Research, a capability invocation, a plan and a rendered image all reach
    past the conversation. An ordinary answer does not, and is not making a
    claim about the world that needs verifying.
    """

    orchestration = details.get("orchestration") or {}
    if not isinstance(orchestration, Mapping):
        return False
    if str(orchestration.get("kind") or "") in {"research", "action", "plan"}:
        return True
    if orchestration.get("capability") or orchestration.get("steps"):
        return True
    return bool(details.get("generated_image"))


def _checked_label_name(name: Any) -> str:
    checked = str(name or "").strip()
    if not checked:
        raise ValueError("Label name cannot be empty")
    if len(checked) > MAX_LABEL_NAME_LENGTH:
        raise ValueError(
            f"Label name cannot exceed {MAX_LABEL_NAME_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in checked):
        raise ValueError("Label name must be one line")
    return checked


def _checked_tone(tone: Any) -> str:
    value = str(tone or "neutral").strip().lower()
    if value not in LABEL_TONES:
        raise ValueError(f"Label tone must be one of: {', '.join(LABEL_TONES)}")
    return value
GENERATION_LIMITS = {
    "context_window_tokens": {"minimum": 256, "maximum": 65536},
    "maximum_output_tokens": {"minimum": 1, "maximum": 8192},
    "temperature": {"minimum": 0.0, "maximum": 2.0},
    "top_p": {"minimum": 0.05, "maximum": 1.0},
    "top_k": {"minimum": 0, "maximum": 200},
    "repetition_penalty": {"minimum": 0.8, "maximum": 2.0},
    "seed": {"minimum": -1, "maximum": 2_147_483_647},
}
REASONING_MODES = ("instant", "cooking")
REASONING_MODE_ALIASES = {
    "instant": "instant",
    "off": "instant",
    "cooking": "cooking",
    "auto": "cooking",
    "deep": "cooking",
}
MAXIMUM_OUTPUT_MODES = ("automatic", "manual")
COMPUTER_AUTHORITY_MODES = ("ask_every_time", "full_access")
MANUAL_OUTPUT_PRESETS = (256, 512, 1024, 2048, 4096, 8192)
BASE_STEAK_CONTEXT_PRESETS = (16384, 24576, 32768, 40960, 49152, 65536)
BASE_STEAK_MODEL_ID = "base-steak-2-0-9b"
VISION_PROFILE_ID = "base_steak_2_vision_fixed_4k"


def _normalise_reasoning_mode(value: Any) -> tuple[str, str]:
    requested = str(value).strip().casefold()
    try:
        return requested, REASONING_MODE_ALIASES[requested]
    except KeyError as exc:
        raise ValueError(
            "reasoning_mode must be instant or cooking "
            "(legacy aliases: off, auto, deep)"
        ) from exc


def _apply_reasoning_mode(
    messages: list[dict[str, str]],
    mode: str,
) -> list[dict[str, str]]:
    """Apply the model-native per-turn switch without mutating stored chat.

    The Base Steak weights retain the native ``/think`` and
    ``/no_think`` soft switches. The rendered-template boundary remains the
    hard Instant guard, while this latest-user-turn switch makes a transition
    from Instant to Cooking explicit to the model. Only the private runtime
    copy is changed; persisted user text and conversation history stay exact.
    """

    _, canonical = _normalise_reasoning_mode(mode)
    runtime_messages = [dict(message) for message in messages]
    if canonical == "instant":
        direct_history: list[dict[str, str]] = []
        for message in runtime_messages:
            if str(message.get("role") or "").casefold() != "assistant":
                direct_history.append(message)
                continue
            content = str(message.get("content") or "")
            content = re.sub(
                r"<think>[\s\S]*?</think>",
                "",
                content,
                flags=re.IGNORECASE,
            )
            content = re.sub(
                r"<think>[\s\S]*$",
                "",
                content,
                flags=re.IGNORECASE,
            ).strip()
            if content:
                message["content"] = content
                direct_history.append(message)
        runtime_messages = direct_history
    latest_user = next(
        (
            message
            for message in reversed(runtime_messages)
            if str(message.get("role") or "").casefold() == "user"
        ),
        None,
    )
    if latest_user is None:
        raise ValueError("reasoning_mode requires a user turn")
    directive = "/think" if canonical == "cooking" else (
        "Return only the requested answer. Do not print analysis, planning, "
        "self-checks, drafts, or commentary about how you formed it.\n"
        "/no_think"
    )
    content = str(latest_user.get("content") or "").rstrip()
    latest_user["content"] = f"{content}\n\n{directive}" if content else directive
    return runtime_messages


def _generation_control_provenance(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "reasoning_mode_requested": settings["reasoning_mode_requested"],
        "reasoning_mode_effective": settings["reasoning_mode"],
        "context_window_tokens_requested": settings[
            "context_window_tokens_requested"
        ],
        "context_window_tokens_effective": settings["context_window_tokens"],
        "maximum_output_mode_requested": settings[
            "maximum_output_mode_requested"
        ],
        "maximum_output_mode_effective": settings["maximum_output_mode"],
        "maximum_output_tokens_requested": settings[
            "maximum_output_tokens_requested"
        ],
        "maximum_output_tokens_effective": settings["maximum_output_tokens"],
        "computer_authority_mode": settings["computer_authority_mode"],
    }


def _bounded_generation_preview(value: Any) -> dict[str, Any] | None:
    """Validate the private worker's optional, ephemeral preview event."""

    if not isinstance(value, dict):
        return None
    text = str(value.get("tail_text") or "")[-1200:]
    if not text:
        return None
    try:
        token_count = max(0, int(value.get("token_count") or 0))
    except (TypeError, ValueError):
        token_count = 0
    return {
        "kind": "reasoning" if value.get("kind") == "reasoning" else "output",
        "tail_text": text,
        "token_count": token_count,
    }


class ChatService:
    """Own the complete chat workflow while the application remains a facade."""

    def __init__(
        self,
        *,
        database: Database,
        operations: OperationManager,
        runtime: RuntimeManager,
        config: AppConfig,
        start_activation: ActivationStarter,
        model_bundle_runtime: SaltyNativeWorkerRuntime | None = None,
        model_bundle: dict[str, Any] | None = None,
        image_generation_model: dict[str, Any] | None = None,
        image_generation_runtime: SteakGenWorkerClient | None = None,
        image_artifact_root: str | Path | None = None,
        automation: AutomationBroker | None = None,
        web_search: WebSearchClient | None = None,
        vision_broker: SaltyVisionBroker | None = None,
        vision_inputs: VisionInputStore | None = None,
    ) -> None:
        self.database = database
        self.operations = operations
        self.runtime = runtime
        self.config = config
        self.start_activation = start_activation
        self.model_bundle_runtime = model_bundle_runtime
        self.model_bundle = dict(model_bundle) if model_bundle else None
        self.image_generation_model = (
            dict(image_generation_model) if image_generation_model else None
        )
        self.image_generation_runtime = image_generation_runtime
        self.image_artifact_root = (
            Path(image_artifact_root).resolve() if image_artifact_root else None
        )


        self._pending_capture: dict[str, Any] | None = None
        self.automation = automation



        self.host_environment = HostEnvironmentRegistry()



        self.memory = SemanticMemory(self.database.path.parent / "salty-memory.db")


        self.image_store = ImageJobStore(self.database)


        self.connectors = None
        self.web_search = web_search or WebSearchClient()
        self.vision_broker = vision_broker
        self.vision_inputs = vision_inputs
        self._generation_lock = threading.RLock()





        self._bundle_lifecycle_lock = threading.RLock()

    def warm_selected_model_bundle(self) -> dict[str, Any] | None:
        """Warm the selected text bundle without racing an image analysis."""

        if self.model_bundle_runtime is None:
            return None
        with self._bundle_lifecycle_lock:
            return self.model_bundle_runtime.warmup()

    def status(self) -> dict[str, Any]:
        if self.model_bundle_runtime is not None and self.model_bundle is not None:
            defaults = self._normalise_generation_settings(None)
            runtime = self.model_bundle_runtime.describe()
            ready = bool(runtime.get("ready"))
            bundle_limits = {
                name: dict(limits) for name, limits in GENERATION_LIMITS.items()
            }
            bundle_limits["context_window_tokens"]["maximum"] = int(
                runtime["configured_context_limit"]
            )
            bundle_limits["context_window_tokens"]["presets"] = list(
                self._bundle_context_policy()[2]
            )
            bundle_limits["maximum_output_tokens"]["presets"] = list(
                MANUAL_OUTPUT_PRESETS
            )
            return {
                "generation_defaults": {
                    **defaults,
                    "context_window_tokens": min(
                        int(defaults["context_window_tokens"]),
                        int(runtime["configured_context_limit"]),
                    ),
                },
                "generation_limits": bundle_limits,
                "runtime_controls": {
                    "execution_modes": ["auto", "cpu_cuda_hybrid"],
                    "precisions": [self.model_bundle.get("quantization", "Q4_K_M")],
                    "quantisation": {
                        "available": True,
                        "formats": [self.model_bundle.get("quantization", "Q4_K_M")],
                        "selected": self.model_bundle.get("quantization", "Q4_K_M"),
                        "runtime_ready": ready,
                    },
                    "reasoning_control": {
                        "available": True,
                        "label": "Cooking",
                        "setting": "reasoning_mode",
                        "modes": list(REASONING_MODES),
                        "selected": defaults["reasoning_mode"],
                        "enforcement": "model_soft_switch_plus_template_boundary",
                        "reason": (
                            "Instant applies the model's no-think switch and closes the "
                            "embedded empty think block. Cooking applies the model's "
                            "think switch and preserves its open think block."
                        ),
                    },
                    "maximum_output_control": {
                        "available": True,
                        "setting": "maximum_output_mode",
                        "modes": list(MAXIMUM_OUTPUT_MODES),
                        "selected": defaults["maximum_output_mode"],
                        "manual_presets": list(MANUAL_OUTPUT_PRESETS),
                        "effective_token_ceiling": defaults[
                            "maximum_output_tokens"
                        ],
                        "natural_eog_enabled": True,
                    },
                },
                "readiness": (
                    "ready"
                    if ready
                    else "preparing_native_runtime"
                    if runtime.get("warming") or runtime.get("loaded")
                    else "native_runtime_cold"
                ),
                "active_saved_version_id": None,
                "active_target_kind": "model_bundle",
                "active_target_id": self.model_bundle["id"],
                "active_version_label": self.model_bundle["display_name"],
                "runtime_id": runtime.get("runtime_id"),
                "runtime_ready": ready,
                "runtime": runtime if runtime.get("loaded") else None,
                "vision": self.vision_status(),
            }
        row = self.database.fetch_one(
            """
            SELECT a.saved_version_id, v.label, a.runtime_id
            FROM active_runtime a JOIN saved_versions v ON v.id = a.saved_version_id
            WHERE a.singleton = 1
            """
        )
        defaults = self._normalise_generation_settings(None)
        base = {
            "generation_defaults": defaults,
            "generation_limits": GENERATION_LIMITS,
            "vision": self.vision_status(),
            "runtime_controls": {
                "execution_modes": ["auto", "cpu", "cuda"],
                "precisions": ["fp32", "fp16", "bf16"],
                "quantisation": {
                    "available": False,
                    "formats": [],
                    "reason": (
                        "This native engine currently loads full-precision Salty Steak "
                        "checkpoints. INT8/INT4 kernels are not installed in this build."
                    ),
                },
                "reasoning_control": {
                    "available": False,
                    "reason": (
                        "Thinking depth requires a reasoning-trained checkpoint; changing a "
                        "label cannot make the current causal model reason more deeply."
                    ),
                },
            },
        }
        if not row:
            return {
                **base,
                "readiness": "no_saved_version_selected",
                "active_saved_version_id": None,
                "runtime_ready": False,
            }
        identity = self.runtime.identity
        ready = bool(identity and identity.checkpoint_id == row["saved_version_id"])
        return {
            **base,
            "readiness": "ready" if ready else "preparing_salty_potato",
            "active_saved_version_id": row["saved_version_id"],
            "active_version_label": row["label"],
            "runtime_id": identity.runtime_id if ready and identity else row["runtime_id"],
            "runtime_ready": ready,
            "runtime": identity.to_dict() if ready and identity else None,
        }

    def vision_status(self) -> dict[str, Any]:
        selected_id = (
            str(self.model_bundle.get("id"))
            if isinstance(self.model_bundle, dict)
            else None
        )
        selected_supported = selected_id == BASE_STEAK_MODEL_ID
        vision_broker = getattr(self, "vision_broker", None)
        if vision_broker is None:
            broker_status: dict[str, Any] = {
                "application_available": False,
                "available": False,
                "activation_allowed": False,
                "reason": "The app-owned vision adapter is not configured.",
            }
        else:
            broker_status = dict(vision_broker.status())
        available = bool(
            selected_supported
            and getattr(self, "vision_inputs", None) is not None
            and broker_status.get("application_available")
        )
        reason = (
            "Salty Multi-Modal Vision passed its exact runtime and smoke gate."
            if available
            else "Salty Multi-Modal Vision is available only when Base Steak 2.0 is the selected Chat model."
            if not selected_supported
            else str(broker_status.get("reason") or "Vision is unavailable.")
        )
        return {
            **broker_status,
            "available": available,
            "application_available": available,
            "activation_allowed": available,
            "standalone_ui_available": available,
            "selected_model_id": selected_id,
            "required_model_id": BASE_STEAK_MODEL_ID,
            "selected_model_supported": selected_supported,
            "reason": reason,
            "interaction": "explicit_one_image_analyze_action",
            "normal_text_chat_unchanged": True,
            "conversation_context_used": False,
            "system_prompt_used": False,
            "reasoning_control_used": False,
            "automatic_screen_capture": False,
            "screen_capture_requires_existing_grant_and_explicit_import": True,
            "supported_media_types": [
                "image/png",
                "image/jpeg",
                "image/webp",
                "image/bmp",
            ],
            "maximum_image_bytes": 25 * 1024 * 1024,
            "maximum_images_per_analysis": 1,
            "analysis_profile": {
                "profile_id": VISION_PROFILE_ID,
                "context_tokens": 4096,
                "maximum_output_tokens": 256,
                "temperature": 0,
                "seed": 0,
            },
            "public_model_name": BASE_STEAK_PUBLIC_NAME,
        }

    def _bundle_context_policy(self) -> tuple[int, int, tuple[int, ...]]:
        """Return the runtime maximum, manifest default, and advertised presets."""

        runtime_maximum = (
            int(self.model_bundle_runtime.profile.context_limit)
            if self.model_bundle_runtime is not None
            else int(GENERATION_LIMITS["context_window_tokens"]["maximum"])
        )
        defaults = self.config.section("generation")
        manifest_profile = (
            dict(self.model_bundle.get("runtime_profile") or {})
            if isinstance(self.model_bundle, dict)
            else {}
        )
        manifest_default = (
            int(self.model_bundle.get("default_context_tokens") or 0)
            if isinstance(self.model_bundle, dict)
            else 0
        )
        default_context = (
            manifest_default
            if manifest_profile and manifest_default > 0
            else int(defaults["conversation_token_budget"])
        )
        default_context = min(runtime_maximum, max(256, default_context))
        declared_presets = (
            self.model_bundle.get("context_presets") or []
            if isinstance(self.model_bundle, dict)
            else []
        )
        presets = tuple(
            sorted(
                {
                    int(value)
                    for value in declared_presets
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and 256 <= value <= runtime_maximum
                }
            )
        )
        if not presets and manifest_profile:
            presets = tuple(
                value
                for value in BASE_STEAK_CONTEXT_PRESETS
                if value <= runtime_maximum
            )
        return runtime_maximum, default_context, presets

    def _normalise_generation_settings(
        self, overrides: dict[str, Any] | None
    ) -> dict[str, Any]:
        defaults = self.config.section("generation")
        supplied = overrides if isinstance(overrides, dict) else {}
        configured_context_limit, default_context, _ = self._bundle_context_policy()

        def bounded(
            name: str,
            raw_value: Any,
            cast: type[int] | type[float],
        ) -> int | float:
            try:
                value = cast(raw_value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a number") from error
            limits = GENERATION_LIMITS[name]
            if value < limits["minimum"] or value > limits["maximum"]:
                raise ValueError(
                    f"{name} must be between {limits['minimum']} and {limits['maximum']}"
                )
            return value

        context_requested = supplied.get(
            "context_window_tokens_requested",
            supplied.get("context_window_tokens", default_context),
        )
        context_tokens = int(
            bounded("context_window_tokens", context_requested, int)
        )
        if context_tokens > configured_context_limit:
            raise ValueError(
                "context_window_tokens exceeds the loaded runtime profile; "
                f"the current measured maximum is {configured_context_limit}"
            )

        explicit_output = (
            "maximum_output_tokens" in supplied or "max_output_tokens" in supplied
        )
        mode_value = supplied.get(
            "maximum_output_mode_requested",
            supplied.get(
                "maximum_output_mode",
                supplied.get(
                    "max_output_mode",
                    "manual"
                    if explicit_output
                    and "maximum_output_mode" not in supplied
                    and "max_output_mode" not in supplied
                    else defaults.get("maximum_output_mode", "automatic"),
                ),
            ),
        )
        maximum_output_mode_requested = str(mode_value).strip().casefold()
        if maximum_output_mode_requested not in MAXIMUM_OUTPUT_MODES:
            raise ValueError("maximum_output_mode must be automatic or manual")
        maximum_output_mode = maximum_output_mode_requested
        supplied_output = supplied.get(
            "maximum_output_tokens_requested",
            supplied.get(
                "maximum_output_tokens",
                supplied.get("max_output_tokens"),
            ),
        )
        if maximum_output_mode == "automatic":
            maximum_output_requested = (
                None
                if supplied_output is None
                else int(
                    bounded(
                        "maximum_output_tokens",
                        supplied_output,
                        int,
                    )
                )
            )
            maximum_output = min(
                int(GENERATION_LIMITS["maximum_output_tokens"]["maximum"]),
                max(1, context_tokens // 4),
            )
        else:
            maximum_output_requested = (
                int(defaults["maximum_output_tokens"])
                if supplied_output is None
                else supplied_output
            )
            maximum_output = int(
                bounded(
                    "maximum_output_tokens",
                    maximum_output_requested,
                    int,
                )
            )
        if maximum_output >= context_tokens:
            raise ValueError("maximum_output_tokens must be smaller than the context window")

        prompt = str(supplied.get("system_prompt") or "").strip()
        if len(prompt) > 4_000:
            raise ValueError("system_prompt cannot exceed 4,000 characters")
        bundle_reasoning_default = (
            str(self.model_bundle.get("reasoning_default") or "").strip()
            if isinstance(self.model_bundle, dict)
            else ""
        )
        requested_reasoning, reasoning_mode = _normalise_reasoning_mode(
            supplied.get(
                "reasoning_mode_requested",
                supplied.get(
                    "reasoning_mode",
                    bundle_reasoning_default
                    or defaults.get("reasoning_mode", "cooking"),
                ),
            )
        )
        raw_stops = supplied.get("stop_sequences") or []
        if not isinstance(raw_stops, list):
            raise ValueError("stop_sequences must be a list")
        stops = []
        for value in raw_stops:
            checked = str(value)
            if not checked or len(checked) > 64:
                raise ValueError("each stop sequence must contain 1 to 64 characters")
            if checked not in stops:
                stops.append(checked)
        if len(stops) > 8:
            raise ValueError("no more than 8 stop sequences are supported")
        computer_authority_mode = (
            str(supplied.get("computer_authority_mode") or "ask_every_time")
            .strip()
            .casefold()
        )
        if computer_authority_mode not in COMPUTER_AUTHORITY_MODES:
            raise ValueError(
                "computer_authority_mode must be ask_every_time or full_access"
            )

        def default_number(
            name: str,
            default: int | float,
            cast: type[int] | type[float],
        ) -> int | float:
            return bounded(name, supplied.get(name, default), cast)

        return {
            "context_window_tokens_requested": int(context_requested),
            "context_window_tokens": context_tokens,
            "maximum_output_mode_requested": maximum_output_mode_requested,
            "maximum_output_mode": maximum_output_mode,
            "maximum_output_tokens_requested": (
                int(maximum_output_requested)
                if maximum_output_requested is not None
                else None
            ),
            "maximum_output_tokens": maximum_output,
            "temperature": default_number(
                "temperature", float(defaults["temperature"]), float
            ),
            "top_p": default_number("top_p", float(defaults["top_p"]), float),
            "top_k": default_number("top_k", int(defaults["top_k"]), int),
            "repetition_penalty": default_number(
                "repetition_penalty",
                float(defaults["repetition_penalty"]),
                float,
            ),
            "seed": default_number("seed", int(defaults["seed"]), int),
            "reasoning_mode_requested": requested_reasoning,
            "reasoning_mode": reasoning_mode,
            "web_search_enabled": bool(supplied.get("web_search_enabled", False)),
            "system_prompt": prompt,
            "stop_sequences": stops,
            "computer_authority_mode": computer_authority_mode,



            "agent_mode": bool(supplied.get("agent_mode", False)),




            "research_mode": bool(
                supplied.get("research_available", supplied.get("research_mode", False))
                or supplied.get("research_command", False)
            ),





            "research_available": bool(
                supplied.get("research_available", supplied.get("research_mode", False))
                or supplied.get("research_command", False)
            ),
            "research_forced": bool(supplied.get("research_command", False)),




            "image_mode": bool(supplied.get("image_mode", False)),
        }

    def list_conversations(self) -> list[dict[str, Any]]:
        conversations = self.database.fetch_all(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            -- Pinned first, then both groups by recency. Ordering here rather
            -- than in the interface means every caller sees the same order and
            -- a reload cannot rearrange the list.
            ORDER BY (c.pinned_at IS NULL), c.pinned_at DESC, c.updated_at DESC
            """
        )
        links = self.database.fetch_all(
            """
            SELECT l.conversation_id, b.id, b.name, b.tone
            FROM conversation_label_links l
            JOIN conversation_labels b ON b.id = l.label_id
            ORDER BY b.name COLLATE NOCASE
            """
        )
        applied: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            applied.setdefault(str(link["conversation_id"]), []).append(
                {
                    "id": link["id"],
                    "name": link["name"],
                    "tone": link["tone"],
                }
            )
        for conversation in conversations:
            conversation["pinned"] = bool(conversation.get("pinned_at"))
            conversation["labels"] = applied.get(str(conversation["id"]), [])
        return conversations



    def set_conversation_pinned(
        self, conversation_id: str, pinned: bool
    ) -> dict[str, Any]:
        """Pin or unpin one conversation, durably.

        The pin is a column on the conversation, so it survives a restart for
        the same reason the title does. `updated_at` is deliberately not
        touched: pinning is not activity, and bumping it would reorder the
        user's recents every time they organised them.
        """

        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Conversation does not exist: {conversation_id}")
            connection.execute(
                "UPDATE conversations SET pinned_at = ? WHERE id = ?",
                (utc_now() if pinned else None, conversation_id),
            )
        return self.conversation_summary(conversation_id)

    def conversation_summary(self, conversation_id: str) -> dict[str, Any]:
        """One conversation's row and its organisation, without its messages."""

        row = self.database.fetch_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        if not row:
            raise KeyError(f"Conversation does not exist: {conversation_id}")
        row["pinned"] = bool(row.get("pinned_at"))
        row["labels"] = self.database.fetch_all(
            """
            SELECT b.id, b.name, b.tone
            FROM conversation_label_links l
            JOIN conversation_labels b ON b.id = l.label_id
            WHERE l.conversation_id = ?
            ORDER BY b.name COLLATE NOCASE
            """,
            (conversation_id,),
        )
        return row

    def list_labels(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT b.*, COUNT(l.conversation_id) AS conversation_count
            FROM conversation_labels b
            LEFT JOIN conversation_label_links l ON l.label_id = b.id
            GROUP BY b.id ORDER BY b.name COLLATE NOCASE
            """
        )

    def create_label(self, name: str, tone: str = "neutral") -> dict[str, Any]:
        checked = _checked_label_name(name)
        identifier = new_id()
        now = utc_now()
        try:
            self.database.execute(
                "INSERT INTO conversation_labels(id, name, tone, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (identifier, checked, _checked_tone(tone), now, now),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"A label called {checked!r} already exists") from error
        return {
            "id": identifier,
            "name": checked,
            "tone": _checked_tone(tone),
            "created_at": now,
            "updated_at": now,
            "conversation_count": 0,
        }

    def update_label(
        self,
        label_id: str,
        *,
        name: str | None = None,
        tone: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM conversation_labels WHERE id = ?", (label_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Label does not exist: {label_id}")
            try:
                connection.execute(
                    "UPDATE conversation_labels SET name = ?, tone = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        _checked_label_name(name) if name is not None else current["name"],
                        _checked_tone(tone) if tone is not None else current["tone"],
                        utc_now(),
                        label_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Another label already has that name") from error
        return self.database.fetch_one(
            "SELECT * FROM conversation_labels WHERE id = ?", (label_id,)
        )

    def delete_label(self, label_id: str) -> dict[str, Any]:
        """Remove a label. The conversations it was applied to are untouched."""

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT id, name FROM conversation_labels WHERE id = ?", (label_id,)
            ).fetchone()
            if existing is None:
                raise KeyError(f"Label does not exist: {label_id}")
            connection.execute(
                "DELETE FROM conversation_label_links WHERE label_id = ?", (label_id,)
            )
            connection.execute(
                "DELETE FROM conversation_labels WHERE id = ?", (label_id,)
            )
        return {"deleted": True, "id": existing["id"], "name": existing["name"]}

    def set_conversation_label(
        self, conversation_id: str, label_id: str, applied: bool
    ) -> dict[str, Any]:
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone() is None:
                raise KeyError(f"Conversation does not exist: {conversation_id}")
            if connection.execute(
                "SELECT 1 FROM conversation_labels WHERE id = ?", (label_id,)
            ).fetchone() is None:
                raise KeyError(f"Label does not exist: {label_id}")
            if applied:
                connection.execute(
                    "INSERT OR IGNORE INTO conversation_label_links("
                    "conversation_id, label_id, created_at) VALUES (?, ?, ?)",
                    (conversation_id, label_id, utc_now()),
                )
            else:
                connection.execute(
                    "DELETE FROM conversation_label_links "
                    "WHERE conversation_id = ? AND label_id = ?",
                    (conversation_id, label_id),
                )
        return self.conversation_summary(conversation_id)

    def create_conversation(self) -> dict[str, Any]:
        identifier = new_id()
        now = utc_now()
        self.database.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, 'New chat', ?, ?)",
            (identifier, now, now),
        )
        return {
            "id": identifier,
            "title": "New chat",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self.database.fetch_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )
        if not conversation:
            raise KeyError(f"Conversation does not exist: {conversation_id}")
        messages = self.database.fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY sequence",
            (conversation_id,),
        )
        for message in messages:
            details = parse_json(message.pop("technical_details_json", None), None)


            message["technical_details"] = public_technical_details(details)
            message["context_omitted"] = bool(
                isinstance(details, dict) and details.get("context_omitted")
            )
        conversation["messages"] = messages
        return conversation

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> dict[str, Any]:
        checked = str(title).strip()
        if not checked:
            raise ValueError("Conversation title cannot be empty")
        if len(checked) > MAX_CONVERSATION_TITLE_LENGTH:
            raise ValueError(
                "Conversation title cannot exceed "
                f"{MAX_CONVERSATION_TITLE_LENGTH} characters"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in checked):
            raise ValueError("Conversation title must be one line")
        updated_at = utc_now()
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Conversation does not exist: {conversation_id}")
            connection.execute(
                """
                UPDATE conversations SET title = ?, updated_at = ?
                WHERE id = ?
                """,
                (checked, updated_at, conversation_id),
            )
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self._generation_lock:
            self._cancel_active_generation(
                conversation_id=conversation_id,
                reason="conversation_deleted",
            )
            with self.database.transaction() as connection:
                conversation = connection.execute(
                    "SELECT id, title FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conversation is None:
                    raise KeyError(
                        f"Conversation does not exist: {conversation_id}"
                    )
                connection.execute(
                    "DELETE FROM conversations WHERE id = ?",
                    (conversation_id,),
                )
        if self.vision_inputs is not None:
            self.vision_inputs.purge_conversation(conversation_id)
        return {
            "deleted": True,
            "conversation_id": conversation_id,
            "title": conversation["title"],
        }

    def _selected_target(self) -> dict[str, Any]:
        if self.model_bundle_runtime is not None and self.model_bundle is not None:
            profile = self.model_bundle_runtime.profile
            return {
                "kind": "model_bundle",
                "id": str(self.model_bundle["id"]),
                "label": str(self.model_bundle["display_name"]),
                "profile_id": profile.profile_id,
                "source_sha256": str(self.model_bundle["checksum"]),
            }
        row = self.database.fetch_one(
            "SELECT saved_version_id FROM active_runtime WHERE singleton = 1"
        )
        if not row:
            raise ValueError("No saved version selected")
        return {
            "kind": "saved_version",
            "id": str(row["saved_version_id"]),
            "label": None,
            "profile_id": "legacy_saved_version",
            "source_sha256": None,
        }

    def _ensure_bundle_runtime(
        self,
        target: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        if self.model_bundle_runtime is None:
            raise RuntimeError("The selected model bundle has no native runtime")
        identity = self.model_bundle_runtime.warmup()
        if identity.get("source_sha256") != target["source_sha256"]:
            raise RuntimeError("Native runtime source identity changed during activation")
        runtime_id = str(identity.get("runtime_id") or "")
        if not runtime_id:
            raise RuntimeError("Native runtime did not create an instance identity")
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE chat_runtime_states
                SET state = 'unloaded', unloaded_at = COALESCE(unloaded_at, ?)
                WHERE target_kind = 'model_bundle'
                  AND target_id = ?
                  AND state = 'loaded'
                  AND id <> ?
                """,
                (now, target["id"], runtime_id),
            )
            connection.execute(
                """
                INSERT INTO chat_runtime_states(
                    id, operation_id, target_kind, target_id, profile_id,
                    artifact_path, source_sha256, state, metadata_json,
                    created_at, verified_at, loaded_at, unloaded_at
                ) VALUES (?, ?, 'model_bundle', ?, ?, ?, ?, 'loaded', ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    state = 'loaded',
                    metadata_json = excluded.metadata_json,
                    verified_at = COALESCE(chat_runtime_states.verified_at, excluded.verified_at),
                    loaded_at = COALESCE(chat_runtime_states.loaded_at, excluded.loaded_at),
                    unloaded_at = NULL
                """,
                (
                    runtime_id,
                    operation_id,
                    target["id"],
                    target["profile_id"],
                    str(identity["model_path"]),
                    target["source_sha256"],
                    json_text(identity),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO active_chat_runtime(
                    singleton, runtime_id, target_kind, target_id,
                    profile_id, source_sha256, updated_at
                ) VALUES (1, ?, 'model_bundle', ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    runtime_id = excluded.runtime_id,
                    target_kind = excluded.target_kind,
                    target_id = excluded.target_id,
                    profile_id = excluded.profile_id,
                    source_sha256 = excluded.source_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    target["id"],
                    target["profile_id"],
                    target["source_sha256"],
                    now,
                ),
            )
        return identity

    def _ensure_runtime(self, active_version_id: str) -> None:
        identity = self.runtime.identity
        if identity and identity.checkpoint_id == active_version_id:
            return


        operation = self.start_activation(active_version_id, None)
        completed = self.operations.wait(operation["id"], timeout=900)
        if completed["state"] != "completed":
            raise RuntimeError(
                (completed.get("error") or {}).get(
                    "message", "Salty Steak could not finish preparing"
                )
            )

    def start_message(
        self,
        conversation_id: str,
        content: str,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        exact = str(content)
        if not exact.strip():
            raise ValueError("Message cannot be empty")
        checked_settings = self._normalise_generation_settings(generation_settings)
        with self._generation_lock:
            self.get_conversation(conversation_id)
            self._cancel_active_generation(reason="superseded_by_new_request")




            agent_mode = bool(checked_settings.get("agent_mode"))




            legacy_review = (
                checked_settings["computer_authority_mode"] != "full_access"
            )
            if agent_mode and legacy_review and extract_file_trash_target(exact):
                return self.operations.submit(
                    "chat_host_action",
                    lambda context: self._prepare_file_trash_action(
                        conversation_id=conversation_id,
                        exact=exact,
                        authority_mode=checked_settings["computer_authority_mode"],
                        context=context,
                    ),
                    target_id=conversation_id,
                    dedupe_key="chat-host-action:global",
                    initial_phase="Checking exact file",
                    initial_details={
                        "conversation_id": conversation_id,
                        "host_action_kind": FILE_TRASH_ACTION,
                        "planner_used": False,
                    },
                    success_notification=None,
                    failure_notification=Notification(
                        "error",
                        "File action not prepared",
                        "No file was changed.",
                        None,
                    ),
                )
            if (
                agent_mode
                and legacy_review
                and detect_host_action_intent(exact) == TEMP_CLEANUP_ACTION
            ):
                return self.operations.submit(
                    "chat_host_action",
                    lambda context: self._prepare_temp_cleanup_action(
                        conversation_id=conversation_id,
                        exact=exact,
                        authority_mode=checked_settings["computer_authority_mode"],
                        context=context,
                    ),
                    target_id=conversation_id,
                    dedupe_key="chat-host-action:global",
                    initial_phase="Mapping Windows temporary folders",
                    initial_details={
                        "conversation_id": conversation_id,
                        "host_action_kind": TEMP_CLEANUP_ACTION,
                        "computer_authority_mode": checked_settings[
                            "computer_authority_mode"
                        ],
                        "planner_used": False,
                    },
                    success_notification=None,
                    failure_notification=Notification(
                        "error",
                        "Temporary-data cleanup not prepared",
                        "No temporary file was changed.",
                        None,
                    ),
                )
            return self._submit_generation(
                conversation_id,
                lambda context, cancellation_token: self._generate_message(
                    conversation_id,
                    exact,
                    context,
                    cancellation_token,
                    checked_settings,
                ),
                generation_settings=checked_settings,
            )

    def _prepare_file_trash_action(
        self,
        *,
        conversation_id: str,
        exact: str,
        authority_mode: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        """Persist a direct file proposal without loading or calling the model."""

        context.raise_if_stop_requested()
        if self.automation is None:
            snapshot = None
            snapshot_error = "Local computer automation is unavailable in this build."
        else:
            target_path = extract_file_trash_target(exact)
            assert target_path is not None
            try:
                snapshot = self.automation.prepare_file_trash(target_path)
                snapshot_error = None
            except (OSError, RuntimeError, ValueError) as error:
                snapshot = None
                snapshot_error = f"The exact file could not be verified: {error}"
        action = build_file_trash_proposal(
            user_text=exact,
            proposal_id=new_id(),
            snapshot=snapshot,
            error=snapshot_error,
            authority_mode=authority_mode,
        )
        proposal = dict(action["proposal"])
        conversation = self.get_conversation(conversation_id)
        next_sequence = len(conversation["messages"])
        user_id = new_id()
        assistant_id = new_id()
        now = utc_now()
        target: dict[str, Any] | None
        try:
            target = self._selected_target()
        except (KeyError, RuntimeError, ValueError):
            target = None
        provenance = {
            "conversation_id": conversation_id,
            "generation_id": context.operation_id,
            "host_action": {
                "intent": FILE_TRASH_ACTION,
                "state": proposal["state"],
                "proposal_id": proposal["id"],
                "execution_requested": False,
                "execution_performed": False,
            },
            "planner_used": False,
            "model_runtime_used": False,
            "generation_state": "completed",
        }
        assistant_details = {
            **provenance,
            "host_action_proposal": proposal,
            "finish_reason": "host_action_proposal_prepared",
            "generated_output_tokens": 0,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    target_kind, target_id, runtime_profile_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversation_id,
                    exact,
                    next_sequence,
                    target.get("kind") if target else None,
                    target.get("id") if target else None,
                    target.get("profile_id") if target else None,
                    target.get("source_sha256") if target else None,
                    json_text({**provenance, "user_message_id": user_id}),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    target_kind, target_id, runtime_profile_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assistant_id,
                    conversation_id,
                    str(action["content"]),
                    next_sequence + 1,
                    target.get("kind") if target else None,
                    target.get("id") if target else None,
                    target.get("profile_id") if target else None,
                    target.get("source_sha256") if target else None,
                    json_text(assistant_details),
                    now,
                ),
            )
            title = conversation["title"]
            if title == "New chat":
                title = exact.strip().replace("\n", " ")[:60] or "File action"
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        result = {
            **provenance,
            "user_message_id": user_id,
            "assistant_message_id": assistant_id,
            "proposal_id": proposal["id"],
        }
        context.update(phase="Waiting for your confirmation", details=result)
        if proposal.get("state") == "pending_review" and authority_mode == "full_access":
            execution = self._confirm_file_trash_action(
                conversation_id,
                proposal["id"],
                assistant_id,
                user_confirmed=False,
                authority_mode="full_access",
            )
            result["execution_operation_id"] = execution["id"]
            context.update(phase="Full access dispatched action", details=result)
        return result

    def _prepare_temp_cleanup_action(
        self,
        *,
        conversation_id: str,
        exact: str,
        authority_mode: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        """Persist one bounded Windows temp-cleanup proposal without the model."""

        context.raise_if_stop_requested()
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable in this build")
        roots = self.automation.prepare_temp_cleanup()
        action = build_temp_cleanup_proposal(
            user_text=exact,
            proposal_id=new_id(),
            roots=roots,
            authority_mode=authority_mode,
        )
        proposal = dict(action["proposal"])
        conversation = self.get_conversation(conversation_id)
        next_sequence = len(conversation["messages"])
        user_id = new_id()
        assistant_id = new_id()
        now = utc_now()
        try:
            target = self._selected_target()
        except (KeyError, RuntimeError, ValueError):
            target = None
        provenance = {
            "conversation_id": conversation_id,
            "generation_id": context.operation_id,
            "host_action": {
                "intent": TEMP_CLEANUP_ACTION,
                "state": proposal["state"],
                "proposal_id": proposal["id"],
                "execution_requested": authority_mode == "full_access",
                "execution_performed": False,
            },
            "computer_authority_mode": authority_mode,
            "planner_used": False,
            "model_runtime_used": False,
            "generation_state": "completed",
        }
        assistant_details = {
            **provenance,
            "host_action_proposal": proposal,
            "finish_reason": "host_action_proposal_prepared",
            "generated_output_tokens": 0,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    target_kind, target_id, runtime_profile_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversation_id,
                    exact,
                    next_sequence,
                    target.get("kind") if target else None,
                    target.get("id") if target else None,
                    target.get("profile_id") if target else None,
                    target.get("source_sha256") if target else None,
                    json_text({**provenance, "user_message_id": user_id}),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    target_kind, target_id, runtime_profile_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assistant_id,
                    conversation_id,
                    str(action["content"]),
                    next_sequence + 1,
                    target.get("kind") if target else None,
                    target.get("id") if target else None,
                    target.get("profile_id") if target else None,
                    target.get("source_sha256") if target else None,
                    json_text(assistant_details),
                    now,
                ),
            )
            title = conversation["title"]
            if title == "New chat":
                title = "Clean Windows temporary files"
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        result = {
            **provenance,
            "user_message_id": user_id,
            "assistant_message_id": assistant_id,
            "proposal_id": proposal["id"],
        }
        context.update(phase="Waiting for your confirmation", details=result)
        if authority_mode == "full_access":
            execution = self._confirm_temp_cleanup_action(
                conversation_id,
                proposal["id"],
                assistant_id,
                user_confirmed=False,
                authority_mode="full_access",
            )
            result["execution_operation_id"] = execution["id"]
            context.update(phase="Full access dispatched cleanup", details=result)
        return result

    def granted_automation_capabilities(self) -> list[str]:
        """List the computer-control capabilities an agent task may use."""

        if self.automation is None:
            return []
        return [
            str(item["capability"])
            for item in self.automation.status()["capabilities"]
            if item.get("effective_enabled")
        ]

    def start_agent_task(
        self,
        conversation_id: str,
        instruction: str,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a computer-control turn.

        Retained as an entrypoint, but no longer a second brain. Agent mode is
        a property of the turn: the request goes down the same orchestration
        every other message uses, and the same generation decides whether to
        answer, act, plan or research. The agent loop still runs beneath that
        decision as the executor, which is where it belongs.
        """

        exact = str(instruction)
        if not exact.strip():
            raise ValueError("An automation task cannot be empty")
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable in this build")
        if not self.granted_automation_capabilities():
            raise PermissionError(
                "Grant at least one computer-control capability before starting an "
                "automation task"
            )
        settings = dict(generation_settings or {})
        settings["agent_mode"] = True
        return self.start_message(conversation_id, exact, settings)

    def _agent_generate(
        self,
        messages: list[dict[str, str]],
        *,
        context: OperationContext,
        generation_settings: Mapping[str, Any],
    ) -> str:
        """Produce one planning reply for the agent loop.

        This deliberately does not persist anything: only the final answer
        becomes a transcript message, so intermediate planning never pollutes
        the conversation the user reads.
        """

        if self.model_bundle_runtime is None:
            raise RuntimeError("The native model runtime is unavailable")
        target = self._selected_target()
        with self._bundle_lifecycle_lock:
            self._ensure_bundle_runtime(target, context.operation_id)
            response = self.model_bundle_runtime.generate(
                messages=list(messages),
                maximum_output_tokens=int(generation_settings["maximum_output_tokens"]),
                temperature=float(generation_settings["temperature"]),
                top_p=float(generation_settings["top_p"]),
                top_k=int(generation_settings["top_k"]),
                repetition_penalty=float(generation_settings["repetition_penalty"]),
                seed=int(generation_settings["seed"]),
                stop_sequences=[],
                should_stop=context.stop_requested,
                context_window_tokens=int(
                    generation_settings["context_window_tokens"]
                ),
                reserved_output_tokens=int(
                    generation_settings["maximum_output_tokens"]
                ),


                reasoning_mode="instant",
                maximum_output_mode="manual",
            )



        self._record_generation(response)
        return str(response.text)

    def _record_generation(self, response: Any) -> None:
        """Remember what one generation produced, for this turn only."""

        recorded = getattr(self, "_turn_generations", None)
        if recorded is None:
            return
        try:
            tokens = len(response.token_ids)
        except (AttributeError, TypeError):
            return
        raw = str(getattr(response, "text", "") or "")
        recorded.append((raw, int(tokens)))




        body, _ = _separate_reasoning(raw)
        if body and body != raw:
            recorded.append((body, int(tokens)))

        del recorded[:-16]

    def _describe_screenshot(self, path: str, *, conversation_id: str) -> str:
        """Describe one agent screenshot with the local vision runtime.

        Returns an empty string when vision is unavailable so the agent still
        runs blind rather than failing; the loop treats a missing description
        as "no sight this step", not as an error.
        """

        broker = self.vision_broker
        if broker is None or not broker.status().get("application_available"):
            return ""
        image = Path(path)
        if not image.is_file():
            return ""
        checksum = hashlib.sha256(image.read_bytes()).hexdigest()
        permission = VisionInputPermission(
            granted=True,
            request_id=f"agent-screen-{new_id()}",
            source="screen_capture",
            approved_image_sha256=checksum,
            approved_path=str(image.resolve()),
            conversation_id=conversation_id,
            target_model_id=BASE_STEAK_MODEL_ID,
        )


        with self._bundle_lifecycle_lock:
            result = broker.generate(
                prompt=VISION_PROMPT,
                permission=VisionPermissionLease(permission),
                image_path=image,
                image_suffix=image.suffix or ".bmp",
                maximum_output_tokens=VISION_OUTPUT_TOKENS,
            )
        return str(result.text).strip()

    def start_retry(
        self,
        conversation_id: str,
        user_message_id: str,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checked_settings = self._normalise_generation_settings(generation_settings)
        with self._generation_lock:
            user_message, previous_response = self._retry_target(
                conversation_id, user_message_id
            )
            details = user_message.get("technical_details")
            if isinstance(details, dict) and details.get("vision_analysis"):
                raise ValueError(
                    "Image analyses use the explicit vision retry action so the "
                    "same checksum-bound image can be re-approved."
                )
            if isinstance(details, dict) and details.get("host_action"):
                raise ValueError(
                    "Computer actions cannot be retried as model responses. Use the "
                    "action's explicit confirmation control."
                )
            self._cancel_active_generation(reason="superseded_by_retry")
            return self._submit_generation(
                conversation_id,
                lambda context, cancellation_token: self._generate_retry(
                    conversation_id,
                    user_message["id"],
                    previous_response["id"],
                    context,
                    cancellation_token,
                    checked_settings,
                ),
                generation_settings=checked_settings,
            )

    def start_vision_analysis(
        self,
        conversation_id: str,
        prompt: str,
        vision_input_token: str,
        *,
        permission_request_id: str,
        maximum_output_tokens: int = 128,
    ) -> dict[str, Any]:
        checked_prompt = str(prompt).strip()
        if not checked_prompt or len(checked_prompt) > 4_000:
            raise ValueError("Vision prompt must contain 1 to 4,000 characters")
        if not 1 <= int(maximum_output_tokens) <= 256:
            raise ValueError("Vision output must contain 1 to 256 tokens")
        if not str(vision_input_token).strip():
            raise PermissionError("A one-use vision input token is required")
        if not str(permission_request_id).strip():
            raise PermissionError("Vision analysis requires an idempotency request key")
        status = self.vision_status()
        if not status["application_available"]:
            raise RuntimeError(str(status["reason"]))
        with self._generation_lock:
            self.get_conversation(conversation_id)
            target = self._selected_target()
            if target["kind"] != "model_bundle" or target["id"] != BASE_STEAK_MODEL_ID:
                raise RuntimeError("Vision analysis is bound only to Base Steak 2.0")
            self._cancel_active_generation(reason="superseded_by_vision_analysis")
            cancellation_token = new_id()
            return self.operations.submit(
                "chat_vision_analysis",
                lambda context: self._generate_vision_analysis(
                    conversation_id=conversation_id,
                    prompt=checked_prompt,
                    vision_input_token=str(vision_input_token).strip(),
                    permission_request_id=str(permission_request_id).strip(),
                    maximum_output_tokens=int(maximum_output_tokens),
                    context=context,
                    cancellation_token=cancellation_token,
                ),
                target_id=conversation_id,
                dedupe_key="chat-vision-analysis:global",
                initial_phase="Preparing image analysis",
                initial_details={
                    "conversation_id": conversation_id,
                    "cancellation_token": cancellation_token,
                    "target_model_id": BASE_STEAK_MODEL_ID,
                    "vision_profile_id": VISION_PROFILE_ID,
                    "maximum_output_tokens": int(maximum_output_tokens),
                    "conversation_context_used": False,
                    "automatic_screen_capture": False,
                },
                success_notification=None,
                failure_notification=Notification(
                    "error",
                    "Image analysis not completed",
                    "The checksum-bound image remains available through the explicit retry action.",
                    None,
                ),
            )

    def confirm_image_generation(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        generation_settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Confirm one persisted image proposal without trusting client prompt text."""

        checked_proposal_id = str(proposal_id or "").strip()
        checked_message_id = str(assistant_message_id or "").strip()
        if not checked_proposal_id or not checked_message_id:
            raise ValueError("An image proposal and assistant message are required")
        self.get_conversation(conversation_id)
        existing_artifact = self.database.fetch_one(
            "SELECT operation_id FROM chat_artifacts WHERE proposal_id = ?",
            (checked_proposal_id,),
        )
        if existing_artifact is not None:
            operation = self.operations.get(str(existing_artifact["operation_id"]))
            if operation is None:
                raise RuntimeError("The completed image operation is unavailable")
            return operation
        row = self.database.fetch_one(
            """
            SELECT technical_details_json FROM messages
            WHERE id = ? AND conversation_id = ? AND role = 'assistant'
            """,
            (checked_message_id, conversation_id),
        )
        if row is None:
            raise KeyError("The image proposal message does not exist")
        details = parse_json(row.get("technical_details_json"), {})
        if not isinstance(details, dict):
            raise RuntimeError("The image proposal metadata is invalid")
        proposal = details.get("host_action_proposal")
        if not isinstance(proposal, dict):
            raise RuntimeError("The assistant message has no image proposal")
        if (
            proposal.get("schema") != "salty-steak-host-action-proposal-v1"
            or proposal.get("kind") != "image.generate"
            or str(proposal.get("id")) != checked_proposal_id
        ):
            raise RuntimeError("The image proposal identity does not match")
        if proposal.get("state") not in {
            "pending_review",
            "failed",
            "cancelled",
        }:
            raise RuntimeError("The image proposal cannot be confirmed in its current state")
        prompt = str((proposal.get("arguments") or {}).get("prompt") or "").strip()
        if not prompt or len(prompt) > 4_000:
            raise RuntimeError("The persisted image prompt is invalid")
        negative_prompt = str(
            (proposal.get("arguments") or {}).get("negative_prompt") or ""
        ).strip()[:4_000]
        if (
            self.image_generation_runtime is None
            or not self.image_generation_model
            or self.image_generation_model.get("activation_allowed") is not True
            or self.image_generation_model.get("external_service_required") is not False
        ):
            raise RuntimeError(
                str(
                    (self.image_generation_model or {}).get("runtime_reason")
                    or "The local image-generation runtime is unavailable"
                )
            )
        settings = dict(generation_settings or {})
        width = int(settings.get("width", 512))
        height = int(settings.get("height", 512))
        steps = int(settings.get("steps", settings.get("num_inference_steps", 8)))







        requested_seed = settings.get("seed")
        seed = (
            int(requested_seed)
            if requested_seed is not None
            else secrets.randbelow(0x1_0000_0000)
        )

        if (width, height, steps) != (512, 512, 8):
            raise ValueError("The validated Steak Gen profile is 512x512 with 8 steps")
        if not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("Image seed must fit an unsigned 64-bit integer")


        proposal["state"] = "running"
        proposal["execution_allowed"] = True
        details["host_action_proposal"] = proposal
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), checked_message_id),
        )
        operation = self.operations.submit(
            "chat_image_generation",
            lambda context: self._generate_confirmed_image(
                conversation_id=conversation_id,
                assistant_message_id=checked_message_id,
                proposal_id=checked_proposal_id,
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                steps=steps,
                seed=seed,
                context=context,
            ),
            target_id=conversation_id,
            dedupe_key=f"chat-image-generation:{checked_proposal_id}",
            initial_phase="Preparing image model",
            total_progress=float(steps),
            initial_details={
                "conversation_id": conversation_id,
                "assistant_message_id": checked_message_id,
                "proposal_id": checked_proposal_id,
                "model_id": "steak-gen-1-scaledfp8",
                "width": width,
                "height": height,
                "steps": steps,
                "seed": seed,
            },
            success_notification=None,
            failure_notification=Notification(
                "error",
                "Image not completed",
                "The reviewed prompt remains available so you can retry.",
                None,
            ),
        )
        self._attach_host_action_operation(
            checked_message_id, checked_proposal_id, operation["id"]
        )
        return operation

    def confirm_host_action(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        generation_settings: Mapping[str, Any] | None = None,
        confirmation_text: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch one explicit confirmation by its persisted proposal kind."""

        proposal, _details = self._persisted_host_action(
            conversation_id,
            proposal_id,
            assistant_message_id,
        )
        kind = str(proposal.get("kind") or "")
        if kind == "image.generate":
            return self.confirm_image_generation(
                conversation_id,
                proposal_id,
                assistant_message_id,
                generation_settings,
            )
        if kind == FILE_TRASH_ACTION:
            return self._confirm_file_trash_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
                user_confirmed=True,
                authority_mode="ask_every_time",
            )
        if kind == TEMP_CLEANUP_ACTION:
            if str(confirmation_text or "") != TEMP_CLEANUP_CONFIRMATION:
                raise PermissionError(
                    "Windows temporary-file cleanup requires the exact destructive-action confirmation"
                )
            return self._confirm_temp_cleanup_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
                user_confirmed=True,
                authority_mode="ask_every_time",
            )
        raise ValueError(f"Unsupported host action kind: {kind or 'missing'}")

    def _persisted_host_action(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        checked_proposal_id = str(proposal_id or "").strip()
        checked_message_id = str(assistant_message_id or "").strip()
        if not checked_proposal_id or not checked_message_id:
            raise ValueError("A host-action proposal and assistant message are required")
        self.get_conversation(conversation_id)
        row = self.database.fetch_one(
            """
            SELECT technical_details_json FROM messages
            WHERE id = ? AND conversation_id = ? AND role = 'assistant'
            """,
            (checked_message_id, conversation_id),
        )
        if row is None:
            raise KeyError("The host-action proposal message does not exist")
        details = parse_json(row.get("technical_details_json"), {})
        if not isinstance(details, dict):
            raise RuntimeError("The host-action proposal metadata is invalid")
        proposal = details.get("host_action_proposal")
        if not isinstance(proposal, dict):
            raise RuntimeError("The assistant message has no host-action proposal")
        if (
            proposal.get("schema") != "salty-steak-host-action-proposal-v1"
            or str(proposal.get("id")) != checked_proposal_id
        ):
            raise RuntimeError("The host-action proposal identity does not match")
        return proposal, details

    def _confirm_file_trash_action(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        *,
        user_confirmed: bool,
        authority_mode: str,
    ) -> dict[str, Any]:
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable")
        with self._generation_lock:
            proposal, details = self._persisted_host_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
            )
            if proposal.get("kind") != FILE_TRASH_ACTION:
                raise RuntimeError("The proposal is not a file Recycle Bin action")
            proposal_authority = str(
                proposal.get("authority_mode") or "ask_every_time"
            )
            if authority_mode != proposal_authority:
                raise PermissionError("Computer authority changed after proposal review")
            existing_operation_id = str(proposal.get("operation_id") or "").strip()
            if existing_operation_id:
                existing = self.operations.get(existing_operation_id)
                if existing is not None and proposal.get("state") in {"running", "completed"}:
                    return existing
            if proposal.get("state") not in {"pending_review", "failed"}:
                raise RuntimeError("The file action cannot be confirmed in its current state")
            arguments = proposal.get("arguments")
            if not isinstance(arguments, Mapping):
                raise RuntimeError("The file-action arguments are invalid")
            path = str(arguments.get("path") or "").strip()
            expected_size = arguments.get("expected_size_bytes")
            expected_modified = arguments.get("expected_modified_ns")
            if not path or not isinstance(expected_size, int) or not isinstance(expected_modified, int):
                raise RuntimeError("The reviewed file snapshot is incomplete")




            proposal["state"] = "running"
            proposal["execution_allowed"] = True
            details["host_action_proposal"] = proposal
            self.database.execute(
                "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                (json_text(details), assistant_message_id),
            )
            operation = self.operations.submit(
                "chat_host_action_execution",
                lambda context: self._execute_file_trash_action(
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    proposal_id=proposal_id,
                    path=path,
                    expected_size_bytes=expected_size,
                    expected_modified_ns=expected_modified,
                    user_confirmed=user_confirmed,
                    authority_mode=authority_mode,
                    context=context,
                ),
                target_id=conversation_id,
                dedupe_key=f"chat-host-action:{proposal_id}",
                initial_phase="Revalidating exact file",
                initial_details={
                    "conversation_id": conversation_id,
                    "assistant_message_id": assistant_message_id,
                    "proposal_id": proposal_id,
                    "host_action_kind": FILE_TRASH_ACTION,
                    "path": path,
                },
                success_notification=None,
                failure_notification=Notification(
                    "error",
                    "File was not moved",
                    "The exact action failed closed; review the recorded reason.",
                    None,
                ),
            )
            self._attach_host_action_operation(
                assistant_message_id, proposal_id, operation["id"]
            )
            return operation

    def _execute_file_trash_action(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        proposal_id: str,
        path: str,
        expected_size_bytes: int,
        expected_modified_ns: int,
        user_confirmed: bool,
        authority_mode: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable")
        try:
            context.raise_if_stop_requested()
            context.update(phase="Moving file to Recycle Bin")
            result = self.automation.invoke_confirmed_file_trash(
                {
                    "proposal_id": proposal_id,
                    "path": path,
                    "expected_size_bytes": expected_size_bytes,
                    "expected_modified_ns": expected_modified_ns,
                    "user_confirmed": user_confirmed,
                    "authority_mode": authority_mode,
                }
            )
            context.raise_if_stop_requested()
            proposal, details = self._persisted_host_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
            )
            proposal.update(
                {
                    "state": "completed",
                    "execution_allowed": False,
                    "audit_record_id": result["audit_record_id"],
                    "result": {
                        "status": "succeeded",
                        "path": result["path"],
                        "disposition": result["disposition"],
                        "recoverable": True,
                    },
                }
            )
            details["host_action_proposal"] = proposal
            details["host_action"] = {
                "intent": FILE_TRASH_ACTION,
                "state": "completed",
                "proposal_id": proposal_id,
                "execution_requested": True,
                "execution_performed": True,
                "audit_record_id": result["audit_record_id"],
            }
            details["finish_reason"] = "host_action_completed"
            content = f"Moved `{result['path']}` to the Windows Recycle Bin."
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE messages SET content = ?, technical_details_json = ?
                    WHERE id = ? AND conversation_id = ? AND role = 'assistant'
                    """,
                    (content, json_text(details), assistant_message_id, conversation_id),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (utc_now(), conversation_id),
                )
            operation_result = {
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
                "proposal_id": proposal_id,
                "host_action_kind": FILE_TRASH_ACTION,
                "execution_performed": True,
                "audit_record_id": result["audit_record_id"],
                "result": proposal["result"],
            }
            context.update(phase="Moved to Recycle Bin", details=operation_result)
            return operation_result
        except BaseException as error:
            self._mark_host_action_proposal_state(
                assistant_message_id,
                proposal_id,
                "failed",
                f"{type(error).__name__}: {error}",
            )
            raise

    def _confirm_temp_cleanup_action(
        self,
        conversation_id: str,
        proposal_id: str,
        assistant_message_id: str,
        *,
        user_confirmed: bool,
        authority_mode: str,
    ) -> dict[str, Any]:
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable")
        with self._generation_lock:
            proposal, details = self._persisted_host_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
            )
            if proposal.get("kind") != TEMP_CLEANUP_ACTION:
                raise RuntimeError("The proposal is not a Windows temp-cleanup action")
            proposal_authority = str(
                proposal.get("authority_mode") or "ask_every_time"
            )
            if authority_mode != proposal_authority:
                raise PermissionError("Computer authority changed after proposal review")
            existing_operation_id = str(proposal.get("operation_id") or "").strip()
            if existing_operation_id:
                existing = self.operations.get(existing_operation_id)
                if existing is not None and proposal.get("state") in {"running", "completed"}:
                    return existing
            if proposal.get("state") not in {"pending_review", "failed"}:
                raise RuntimeError("The cleanup cannot run in its current state")
            arguments = proposal.get("arguments")
            roots = arguments.get("roots") if isinstance(arguments, Mapping) else None
            if not isinstance(roots, list) or not roots:
                raise RuntimeError("The reviewed temporary-data roots are missing")


            proposal["state"] = "running"
            proposal["execution_allowed"] = True
            details["host_action_proposal"] = proposal
            self.database.execute(
                "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                (json_text(details), assistant_message_id),
            )
            operation = self.operations.submit(
                "chat_host_action_execution",
                lambda context: self._execute_temp_cleanup_action(
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    proposal_id=proposal_id,
                    roots=roots,
                    user_confirmed=user_confirmed,
                    authority_mode=authority_mode,
                    context=context,
                ),
                target_id=conversation_id,
                dedupe_key=f"chat-host-action:{proposal_id}",
                initial_phase="Revalidating Windows temporary folders",
                initial_details={
                    "conversation_id": conversation_id,
                    "assistant_message_id": assistant_message_id,
                    "proposal_id": proposal_id,
                    "host_action_kind": TEMP_CLEANUP_ACTION,
                    "computer_authority_mode": authority_mode,
                },
                success_notification=None,
                failure_notification=Notification(
                    "error",
                    "Temporary-data cleanup did not complete",
                    "The action stopped safely; review the recorded reason.",
                    None,
                ),
            )
            self._attach_host_action_operation(
                assistant_message_id, proposal_id, operation["id"]
            )
            return operation

    def _execute_temp_cleanup_action(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        proposal_id: str,
        roots: list[dict[str, Any]],
        user_confirmed: bool,
        authority_mode: str,
        context: OperationContext,
    ) -> dict[str, Any]:
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable")
        try:
            context.raise_if_stop_requested()
            context.update(phase="Cleaning Windows temporary folders")
            result = self.automation.invoke_confirmed_temp_cleanup(
                {
                    "proposal_id": proposal_id,
                    "roots": roots,
                    "authority_mode": authority_mode,
                    "user_confirmed": user_confirmed,
                }
            )
            context.raise_if_stop_requested()
            proposal, details = self._persisted_host_action(
                conversation_id,
                proposal_id,
                assistant_message_id,
            )
            proposal.update(
                {
                    "state": "completed",
                    "execution_allowed": False,
                    "audit_record_id": result["audit_record_id"],
                    "result": result,
                }
            )
            details["host_action_proposal"] = proposal
            details["host_action"] = {
                "intent": TEMP_CLEANUP_ACTION,
                "state": "completed",
                "proposal_id": proposal_id,
                "execution_requested": True,
                "execution_performed": True,
                "audit_record_id": result["audit_record_id"],
            }
            details["finish_reason"] = "host_action_completed"
            content = (
                f"Cleaned Windows temporary data: {result['deleted_files']} files and "
                f"{result['deleted_directories']} folders removed; "
                f"{result['skipped_entries']} in-use or inaccessible entries skipped."
            )
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE messages SET content = ?, technical_details_json = ?
                    WHERE id = ? AND conversation_id = ? AND role = 'assistant'
                    """,
                    (content, json_text(details), assistant_message_id, conversation_id),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (utc_now(), conversation_id),
                )
            operation_result = {
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
                "proposal_id": proposal_id,
                "host_action_kind": TEMP_CLEANUP_ACTION,
                "execution_performed": True,
                "audit_record_id": result["audit_record_id"],
                "result": result,
            }
            context.update(phase="Temporary-data cleanup complete", details=operation_result)
            return operation_result
        except BaseException as error:
            self._mark_host_action_proposal_state(
                assistant_message_id,
                proposal_id,
                "failed",
                f"{type(error).__name__}: {error}",
            )
            raise

    def _attach_host_action_operation(
        self,
        assistant_message_id: str,
        proposal_id: str,
        operation_id: str,
    ) -> None:
        """Record which operation is carrying out a proposal.

        Deliberately touches only the operation id.  The work runs on another
        thread and can finish — or fail closed — before this returns, so
        writing a state here would overwrite the real outcome with a stale
        ``running``.
        """

        row = self.database.fetch_one(
            "SELECT technical_details_json FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        details = parse_json(row.get("technical_details_json") if row else None, {})
        if not isinstance(details, dict):
            return
        proposal = details.get("host_action_proposal")
        if not isinstance(proposal, dict) or str(proposal.get("id")) != proposal_id:
            return
        proposal["operation_id"] = operation_id
        details["host_action_proposal"] = proposal
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), assistant_message_id),
        )

    def _mark_host_action_proposal_state(
        self,
        assistant_message_id: str,
        proposal_id: str,
        state: str,
        reason: str,
    ) -> None:
        row = self.database.fetch_one(
            "SELECT technical_details_json FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        details = parse_json(row.get("technical_details_json") if row else None, {})
        if not isinstance(details, dict):
            return
        proposal = details.get("host_action_proposal")
        if not isinstance(proposal, dict) or str(proposal.get("id")) != proposal_id:
            return
        proposal["state"] = state
        proposal["execution_allowed"] = False
        proposal["last_error"] = reason[:2_000]
        details["host_action_proposal"] = proposal
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), assistant_message_id),
        )

    def _generate_confirmed_image(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        proposal_id: str,
        prompt: str,
        width: int,
        height: int,
        steps: int,
        seed: int,
        context: OperationContext,
        negative_prompt: str = "",
    ) -> dict[str, Any]:
        runtime = self.image_generation_runtime
        artifact_root = self.image_artifact_root
        if runtime is None or artifact_root is None:
            raise RuntimeError("The local image-generation runtime is unavailable")
        artifact_id = new_id()
        staging = runtime.paths.output_root / f"{artifact_id}.png"
        final_directory = artifact_root / conversation_id
        final_path = final_directory / f"{artifact_id}.png"
        artifact_committed = False
        text_transition: dict[str, Any] = {
            "policy": "unload_text_before_image_rewarm_after",
            "text_runtime_present": self.model_bundle_runtime is not None,
            "text_runtime_unloaded": False,
            "text_runtime_rewarm_attempted": False,
            "text_runtime_rewarm_ready": None,
            "text_runtime_rewarm_error": None,
        }

        def update_event(event: dict[str, Any]) -> None:
            phase = str(event.get("phase") or "Generating image")
            completed = event.get("completed")
            total = event.get("total")
            context.update(
                phase=phase.replace("_", " ").capitalize(),
                current_progress=float(completed) if completed is not None else None,
                total_progress=float(total) if total is not None else None,
                details={
                    "conversation_id": conversation_id,
                    "assistant_message_id": assistant_message_id,
                    "proposal_id": proposal_id,
                    "model_id": "steak-gen-1-scaledfp8",
                    "image_progress": dict(event),
                    "text_runtime_transition": dict(text_transition),
                },
            )

        try:
            with self._bundle_lifecycle_lock:
                text_runtime = self.model_bundle_runtime
                if text_runtime is not None:
                    context.update(
                        phase="Switching from Chat to image generation",
                        details={"text_runtime_transition": dict(text_transition)},
                    )
                    text_runtime.unload()
                    text_transition["text_runtime_unloaded"] = True
                try:
                    result = runtime.generate(
                        SteakGenRequest(
                            prompt=prompt,
                            negative_prompt=negative_prompt,
                            output_path=str(staging),
                            width=width,
                            height=height,
                            steps=steps,
                            guidance_scale=0.0,
                            seed=seed,
                            max_sequence_length=512,
                            text_runtime_unloaded=True,
                            verify_hashes=True,
                        ),
                        should_stop=context.stop_requested,
                        on_event=update_event,
                        timeout_seconds=3600,
                    )
                finally:
                    if text_runtime is not None:
                        text_transition["text_runtime_rewarm_attempted"] = True
                        context.update(
                            phase="Restoring Chat after image generation",
                            details={"text_runtime_transition": dict(text_transition)},
                        )
                        try:
                            warmed = text_runtime.warmup()
                            text_transition["text_runtime_rewarm_ready"] = bool(
                                warmed.get("ready")
                            )
                        except BaseException as error:
                            text_transition["text_runtime_rewarm_error"] = (
                                f"{type(error).__name__}: {error}"
                            )
            context.raise_if_stop_requested()
            from PIL import Image

            with Image.open(staging) as image:
                image.verify()
            with Image.open(staging) as image:
                actual_width, actual_height = image.size
                actual_format = image.format
            if actual_format != "PNG" or (actual_width, actual_height) != (width, height):
                raise RuntimeError("The generated PNG failed its dimension check")
            size_bytes = staging.stat().st_size
            digest = hashlib.sha256(staging.read_bytes()).hexdigest()
            if digest != result.output_sha256:
                raise RuntimeError("The generated PNG hash changed before commit")
            final_directory.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_path)
            relative_path = final_path.relative_to(artifact_root).as_posix()
            generated_image = {
                "schema": "salty-steak-generated-image-v1",
                "id": artifact_id,
                "sha256": digest,
                "media_type": "image/png",
                "size_bytes": size_bytes,
                "width": width,
                "height": height,
                "prompt": prompt,
                "model_name": result.model_name,
            }
            provenance = {
                "schema": "salty-steak-image-provenance-v1",
                "proposal_id": proposal_id,
                "operation_id": context.operation_id,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "model_id": result.model_id,
                "model_name": result.model_name,
                "transformer_sha256": result.transformer_sha256,
                "support_revision": result.support_revision,
                "width": width,
                "height": height,
                "steps": steps,
                "seed": seed,
                "guidance_scale": 0.0,
                "output_sha256": digest,
                "output_size_bytes": size_bytes,
                "text_runtime_transition": text_transition,
                "no_external_service": True,
            }
            with self.database.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT technical_details_json FROM messages
                    WHERE id = ? AND conversation_id = ? AND role = 'assistant'
                    """,
                    (assistant_message_id, conversation_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("The image proposal disappeared before commit")
                details = parse_json(row["technical_details_json"], {})
                if not isinstance(details, dict):
                    details = {}
                proposal = dict(details.get("host_action_proposal") or {})
                if str(proposal.get("id")) != proposal_id:
                    raise RuntimeError("The image proposal changed before commit")
                proposal.update(
                    {
                        "state": "completed",
                        "operation_id": context.operation_id,
                        "execution_allowed": False,
                        "artifact_id": artifact_id,
                    }
                )
                details["host_action_proposal"] = proposal
                details["generated_image"] = generated_image



                if details.get("turn_completion") == "rendering":
                    details["turn_completion"] = (
                        "cooked"
                        if str(details.get("reasoning_mode_effective") or "").lower()
                        == "cooking"
                        else "done"
                    )
                    details["image_render_started"] = False
                connection.execute(
                    """
                    INSERT INTO chat_artifacts(
                        id, conversation_id, message_id, operation_id, proposal_id,
                        kind, relative_path, media_type, size_bytes, sha256,
                        width, height, provenance_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'image', ?, 'image/png', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        conversation_id,
                        assistant_message_id,
                        context.operation_id,
                        proposal_id,
                        relative_path,
                        size_bytes,
                        digest,
                        width,
                        height,
                        json_text(provenance),
                        utc_now(),
                    ),
                )
                connection.execute(
                    "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                    (json_text(details), assistant_message_id),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (utc_now(), conversation_id),
                )
            artifact_committed = True
            return {
                "conversation_id": conversation_id,
                "assistant_message_id": assistant_message_id,
                "proposal_id": proposal_id,
                "artifact": generated_image,
                "text_runtime_transition": text_transition,
            }
        except SteakGenCancelled as error:
            self._mark_image_proposal_state(
                assistant_message_id, proposal_id, "cancelled", str(error)
            )
            raise OperationInterrupted(str(error)) from error
        except OperationInterrupted:
            self._mark_image_proposal_state(
                assistant_message_id, proposal_id, "cancelled", "Stopped by user"
            )
            raise
        except BaseException as error:
            self._mark_image_proposal_state(
                assistant_message_id,
                proposal_id,
                "failed",
                f"{type(error).__name__}: {error}",
            )
            raise
        finally:
            staging.unlink(missing_ok=True)
            if not artifact_committed:
                final_path.unlink(missing_ok=True)

    def _mark_image_proposal_state(
        self,
        assistant_message_id: str,
        proposal_id: str,
        state: str,
        reason: str,
    ) -> None:
        row = self.database.fetch_one(
            "SELECT technical_details_json FROM messages WHERE id = ?",
            (assistant_message_id,),
        )
        details = parse_json(row.get("technical_details_json") if row else None, {})
        if not isinstance(details, dict):
            return
        proposal = details.get("host_action_proposal")
        if not isinstance(proposal, dict) or str(proposal.get("id")) != proposal_id:
            return
        proposal["state"] = state
        proposal["execution_allowed"] = False
        proposal["last_error"] = reason[:2_000]
        details["host_action_proposal"] = proposal
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), assistant_message_id),
        )

    def _submit_generation(
        self,
        conversation_id: str,
        worker: Callable[[OperationContext, str], dict[str, Any]],
        *,
        generation_settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        cancellation_token = new_id()
        frozen_controls = (
            _generation_control_provenance(dict(generation_settings))
            if generation_settings is not None
            else {}
        )
        return self.operations.submit(
            "chat_generation",
            lambda context: worker(context, cancellation_token),
            target_id=conversation_id,
            dedupe_key="chat-generation:global",
            initial_phase="Preparing response",
            initial_details={
                "conversation_id": conversation_id,
                "cancellation_token": cancellation_token,
                "template_version": CHAT_TEMPLATE_VERSION,
                **frozen_controls,
            },
            success_notification=None,
            failure_notification=Notification(
                "error",
                "Message not completed",
                "The draft remains available so you can try again.",
                None,
            ),
        )

    def _active_generation(self) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """
            SELECT * FROM operations
            WHERE type IN ('chat_generation','chat_vision_analysis','chat_image_generation')
              AND state IN ('queued','running','stop_requested')
            ORDER BY created_at ASC
            LIMIT 1
            """
        )
        return self.database.operation_record(row) if row else None

    def _cancel_active_generation(
        self,
        *,
        conversation_id: str | None = None,
        reason: str,
    ) -> dict[str, Any] | None:
        active = self._active_generation()
        if active is None:
            return None
        if (
            conversation_id is not None
            and str(active.get("target_id")) != str(conversation_id)
        ):
            return None
        self.operations.update_progress(
            active["id"],
            phase="Cancellation requested",
            details={
                "conversation_id": active.get("target_id"),
                "cancellation_reason": reason,
                "cancellation_state": "requested",
            },
        )
        self.operations.request_stop(active["id"])
        try:
            completed = self.operations.wait(active["id"], timeout=30)
        except TimeoutError as exc:
            raise RuntimeError(
                "GENERATION_CANCEL_TIMEOUT: the active Chat request did not "
                "acknowledge cancellation within 30 seconds"
            ) from exc
        return completed

    def _retry_target(
        self,
        conversation_id: str,
        user_message_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        conversation = self.get_conversation(conversation_id)
        messages = conversation["messages"]
        target_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message["id"] == user_message_id and message["role"] == "user"
            ),
            None,
        )
        if target_index is None:
            raise KeyError("The user message is not part of this conversation")
        if any(
            message["role"] == "user"
            for message in messages[target_index + 1 :]
        ):
            raise ValueError("Only the latest user turn can be retried safely")
        responses = [
            message
            for message in messages[target_index + 1 :]
            if message["role"] == "assistant"
        ]
        if not responses:
            raise ValueError("This user turn does not have a response to retry")
        return messages[target_index], responses[-1]

    def _generate_vision_analysis(
        self,
        *,
        conversation_id: str,
        prompt: str,
        vision_input_token: str,
        permission_request_id: str,
        maximum_output_tokens: int,
        context: OperationContext,
        cancellation_token: str,
    ) -> dict[str, Any]:
        if self.vision_broker is None or self.vision_inputs is None:
            raise RuntimeError("The app-owned vision adapter is not configured")
        context.raise_if_stop_requested()
        target = self._selected_target()
        if target["kind"] != "model_bundle" or target["id"] != BASE_STEAK_MODEL_ID:
            raise RuntimeError("Vision analysis target changed before execution")

        claim: VisionInputClaim | None = None
        user_id: str | None = None
        try:
            claim = self.vision_inputs.claim(
                vision_input_token,
                operation_id=context.operation_id,
                conversation_id=conversation_id,
                target_model_id=target["id"],
                prompt=prompt,
            )
            context.raise_if_stop_requested()
            conversation = self.get_conversation(conversation_id)
            next_sequence = len(conversation["messages"])
            user_id = new_id()
            now = utc_now()
            input_provenance = {
                "vision_analysis": True,
                "vision_input_id": claim.input_id,
                "image_sha256": claim.image_sha256,
                "image_size_bytes": claim.image_size_bytes,
                "image_media_type": claim.media_type,
                "image_source": claim.source,
                "permission_request_id": permission_request_id,
                "permission_granted": True,
                "conversation_id": conversation_id,
                "generation_id": context.operation_id,
                "active_target_kind": "model_bundle",
                "active_target_id": target["id"],
                "runtime_profile_id": VISION_PROFILE_ID,
                "source_sha256": target["source_sha256"],
                "projector_sha256": self.vision_broker.projector_sha256,
                "cancellation_token": cancellation_token,
                "cancellation_state": "active",
                "conversation_context_used": False,
                "system_prompt_used": False,
                "reasoning_control_used": False,
                "automatic_screen_capture": False,
                "maximum_output_tokens_effective": maximum_output_tokens,
                "vision_runtime_profile": dict(
                    self.vision_status()["analysis_profile"]
                ),
                "consent_evidence": dict(claim.consent_evidence),
            }
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, role, content, sequence,
                        target_kind, target_id, runtime_profile_id, source_sha256,
                        technical_details_json, created_at
                    ) VALUES (?, ?, 'user', ?, ?, 'model_bundle', ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        conversation_id,
                        prompt,
                        next_sequence,
                        target["id"],
                        VISION_PROFILE_ID,
                        target["source_sha256"],
                        json_text(input_provenance),
                        now,
                    ),
                )

            context.update(
                phase="Analyzing selected image",
                details={
                    **input_provenance,
                    "user_message_id": user_id,
                    "image_path_persisted": False,
                },
            )
            permission = VisionPermissionLease(
                VisionInputPermission(
                    granted=True,
                    request_id=permission_request_id,
                    source=claim.source,
                    approved_image_sha256=claim.image_sha256,
                    approved_path=str(claim.image_path),
                    conversation_id=conversation_id,
                    target_model_id=target["id"],
                )
            )





            text_runtime_transition: dict[str, Any] = {
                "policy": "unload_text_before_vision_rewarm_after",
                "text_runtime_present": self.model_bundle_runtime is not None,
                "text_runtime_unloaded": False,
                "text_runtime_rewarm_attempted": False,
                "text_runtime_rewarm_ready": None,
                "text_runtime_rewarm_error": None,
            }
            with self._bundle_lifecycle_lock:
                text_runtime = self.model_bundle_runtime
                if text_runtime is not None:
                    context.update(
                        phase="Switching from Chat to image analysis",
                        details={
                            **input_provenance,
                            **text_runtime_transition,
                        },
                    )
                    text_runtime.unload()
                    text_runtime_transition["text_runtime_unloaded"] = True
                try:
                    result = self.vision_broker.generate(
                        prompt=prompt,
                        permission=permission,
                        image_path=claim.image_path,
                        image_suffix=claim.suffix,
                        maximum_output_tokens=maximum_output_tokens,
                        should_stop=context.stop_requested,
                    )
                finally:
                    if text_runtime is not None:
                        text_runtime_transition["text_runtime_rewarm_attempted"] = True
                        context.update(
                            phase="Restoring Chat after image analysis",
                            details={
                                **input_provenance,
                                **text_runtime_transition,
                            },
                        )
                        try:
                            restored = text_runtime.warmup()
                            text_runtime_transition["text_runtime_rewarm_ready"] = bool(
                                restored.get("ready")
                            )
                            if not text_runtime_transition["text_runtime_rewarm_ready"]:
                                text_runtime_transition["text_runtime_rewarm_error"] = str(
                                    restored.get("warmup_error")
                                    or "Text runtime did not become ready after image analysis"
                                )
                        except BaseException as error:




                            text_runtime_transition["text_runtime_rewarm_ready"] = False
                            text_runtime_transition["text_runtime_rewarm_error"] = str(error)
            context.raise_if_stop_requested()
            if (
                result.image_sha256 != claim.image_sha256
                or result.text_model_sha256 != target["source_sha256"]
                or result.projector_sha256 != self.vision_broker.projector_sha256
            ):
                raise RuntimeError("Vision result provenance changed before commit")

            assistant_id = new_id()
            finished = utc_now()
            details = {
                **result.technical_details,
                **input_provenance,
                "text_runtime_transition": text_runtime_transition,
                "user_message_id": user_id,
                "assistant_message_id": assistant_id,
                "runtime_id": result.runtime_id,
                "runtime_instance_id": result.runtime_id,
                "runtime_files_sha256": dict(result.runtime_files_sha256),
                "generation_state": "completed",
                "finish_reason": "vision_process_completed",
                "duration_seconds": result.duration_seconds,
                "command_exit_code": result.command_exit_code,
                "cancellation_state": "not_requested",
            }
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE messages
                    SET technical_details_json = ?, runtime_instance_id = ?
                    WHERE id = ? AND conversation_id = ? AND role = 'user'
                    """,
                    (
                        json_text(
                            {
                                **input_provenance,
                                "user_message_id": user_id,
                                "runtime_id": result.runtime_id,
                                "runtime_files_sha256": dict(
                                    result.runtime_files_sha256
                                ),
                                "generation_state": "completed",
                                "cancellation_state": "not_requested",
                            }
                        ),
                        result.runtime_id,
                        user_id,
                        conversation_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, conversation_id, role, content, sequence,
                        target_kind, target_id, runtime_profile_id,
                        runtime_instance_id, source_sha256,
                        technical_details_json, created_at
                    ) VALUES (?, ?, 'assistant', ?, ?, 'model_bundle', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assistant_id,
                        conversation_id,
                        result.text,
                        next_sequence + 1,
                        target["id"],
                        VISION_PROFILE_ID,
                        result.runtime_id,
                        target["source_sha256"],
                        json_text(details),
                        finished,
                    ),
                )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (finished, conversation_id),
                )
                if conversation["title"] == "New chat":
                    title = prompt.replace("\n", " ")[:60] or "Image analysis"
                    connection.execute(
                        "UPDATE conversations SET title = ? WHERE id = ?",
                        (title, conversation_id),
                    )
            self.vision_inputs.finish(context.operation_id, "completed")
            response = {
                "conversation_id": conversation_id,
                "user_message_id": user_id,
                "assistant_message_id": assistant_id,
                "generation_id": context.operation_id,
                "active_target_id": target["id"],
                "runtime_profile_id": VISION_PROFILE_ID,
                "runtime_id": result.runtime_id,
                "image_sha256": claim.image_sha256,
                "projector_sha256": result.projector_sha256,
                "finish_reason": "vision_process_completed",
                "partial_output_saved": False,
            }
            context.update(phase="Saving image analysis", details=response)
            return response
        except (SaltyVisionCancelled, OperationInterrupted) as exc:
            if claim is not None:
                self.vision_inputs.finish(context.operation_id, "interrupted")
            if user_id is not None:
                self._mark_vision_user_message(
                    user_id,
                    conversation_id,
                    state="cancelled",
                    cancellation_state="acknowledged",
                )
            raise OperationInterrupted(str(exc)) from exc
        except BaseException:
            if claim is not None:
                self.vision_inputs.finish(context.operation_id, "failed")
            if user_id is not None:
                self.database.execute("DELETE FROM messages WHERE id = ?", (user_id,))
            raise

    def _mark_vision_user_message(
        self,
        user_message_id: str,
        conversation_id: str,
        *,
        state: str,
        cancellation_state: str,
    ) -> None:
        row = self.database.fetch_one(
            "SELECT technical_details_json FROM messages WHERE id = ?",
            (user_message_id,),
        )
        details = parse_json(row.get("technical_details_json") if row else None, {})
        if not isinstance(details, dict):
            details = {}
        details.update(
            {
                "generation_state": state,
                "cancellation_state": cancellation_state,
            }
        )
        self.database.execute(
            """
            UPDATE messages SET technical_details_json = ?
            WHERE id = ? AND conversation_id = ? AND role = 'user'
            """,
            (json_text(details), user_message_id, conversation_id),
        )

    def _generate_message(
        self,
        conversation_id: str,
        exact: str,
        context: OperationContext,
        cancellation_token: str,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        generation_settings = self._normalise_generation_settings(
            generation_settings
        )
        conversation = self.get_conversation(conversation_id)
        target = self._selected_target()
        next_sequence = len(conversation["messages"])
        user_id = new_id()
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    target_kind, target_id, runtime_profile_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    conversation_id,
                    exact,
                    next_sequence,
                    target["kind"],
                    target["id"],
                    target["profile_id"],
                    target["source_sha256"],
                    json_text(
                        {
                            "conversation_id": conversation_id,
                            "user_message_id": user_id,
                            "generation_id": context.operation_id,
                            "active_version_id": target["id"],
                            "active_target_kind": target["kind"],
                            "active_target_id": target["id"],
                            "runtime_profile_id": target["profile_id"],
                            "source_sha256": target["source_sha256"],
                            "runtime_id": None,
                            "template_version": CHAT_TEMPLATE_VERSION,
                            "cancellation_token": cancellation_token,
                            "cancellation_state": "active",
                            "generation_settings": generation_settings,
                        }
                    ),
                    now,
                ),
            )
            if conversation["title"] == "New chat":
                title = exact.strip().replace("\n", " ")[:60] or "New chat"
                connection.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (title, now, conversation_id),
                )
        try:
            history = [
                {"role": message["role"], "content": message["content"]}
                for message in conversation["messages"]
                if message["role"] in {"user", "assistant"}
            ]
            history.append({"role": "user", "content": exact})
            if generation_settings["system_prompt"]:
                history.insert(
                    0,
                    {
                        "role": "system",
                        "content": generation_settings["system_prompt"],
                    },
                )
            return self._generate_turn(
                conversation_id=conversation_id,
                user_message_id=user_id,
                history=history,
                assistant_sequence=next_sequence + 1,
                active_version_id=target["id"],
                active_target_kind=target["kind"],
                runtime_profile_id=target["profile_id"],
                source_sha256=target["source_sha256"],
                context=context,
                cancellation_token=cancellation_token,
                generation_settings=generation_settings,
            )
        except OperationInterrupted:
            row = self.database.fetch_one(
                """
                SELECT technical_details_json FROM messages
                WHERE id = ? AND conversation_id = ? AND role = 'user'
                """,
                (user_id, conversation_id),
            )
            details = parse_json(
                row.get("technical_details_json") if row else None,
                {},
            )
            if not isinstance(details, dict):
                details = {}
            legacy_identity = self.runtime.identity
            bundle_identity = (
                self.model_bundle_runtime.describe()
                if self.model_bundle_runtime is not None
                else None
            )
            runtime_id = (
                bundle_identity.get("runtime_id")
                if target["kind"] == "model_bundle" and bundle_identity
                else legacy_identity.runtime_id
                if legacy_identity
                and legacy_identity.checkpoint_id == target["id"]
                else details.get("runtime_id")
            )
            details.update(
                {
                    "conversation_id": conversation_id,
                    "user_message_id": user_id,
                    "generation_id": context.operation_id,
                    "active_version_id": target["id"],
                    "active_target_kind": target["kind"],
                    "active_target_id": target["id"],
                    "runtime_profile_id": target["profile_id"],
                    "source_sha256": target["source_sha256"],
                    "runtime_id": runtime_id,
                    "template_version": CHAT_TEMPLATE_VERSION,
                    "cancellation_token": cancellation_token,
                    "cancellation_state": "acknowledged",
                    "generation_state": "cancelled",
                }
            )
            self.database.execute(
                """
                UPDATE messages SET technical_details_json = ?
                WHERE id = ? AND conversation_id = ? AND role = 'user'
                """,
                (json_text(details), user_id, conversation_id),
            )
            raise
        except Exception:
            saved = self.database.fetch_one(
                """
                SELECT 1 FROM messages
                WHERE conversation_id = ? AND sequence = ? AND role = 'assistant'
                """,
                (conversation_id, next_sequence + 1),
            )
            if not saved:
                self.database.execute("DELETE FROM messages WHERE id = ?", (user_id,))
            raise

    def _generate_retry(
        self,
        conversation_id: str,
        user_message_id: str,
        previous_response_id: str,
        context: OperationContext,
        cancellation_token: str,
        generation_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        generation_settings = self._normalise_generation_settings(
            generation_settings
        )
        conversation = self.get_conversation(conversation_id)
        target, current_response = self._retry_target(
            conversation_id, user_message_id
        )
        if current_response["id"] != previous_response_id:
            raise ValueError("The retry target changed before generation started")
        target_index = next(
            index
            for index, message in enumerate(conversation["messages"])
            if message["id"] == target["id"]
        )
        history = [
            {"role": message["role"], "content": message["content"]}
            for message in conversation["messages"][: target_index + 1]
            if message["role"] in {"user", "assistant"}
        ]
        if generation_settings["system_prompt"]:
            history.insert(
                0,
                {"role": "system", "content": generation_settings["system_prompt"]},
            )
        target = self._selected_target()
        return self._generate_turn(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            history=history,
            assistant_sequence=len(conversation["messages"]),
            active_version_id=target["id"],
            active_target_kind=target["kind"],
            runtime_profile_id=target["profile_id"],
            source_sha256=target["source_sha256"],
            context=context,
            cancellation_token=cancellation_token,
            generation_settings=generation_settings,
            previous_response_id=previous_response_id,
        )

    def _image_generation_available(self) -> bool:
        """Whether the local image runtime could actually run right now.

        Activation evidence rather than residency: the diffusion stack is a
        cold one-shot worker, so requiring it to be loaded would wrongly report
        a validated runtime as unavailable.
        """

        runtime = self.image_generation_model
        return bool(
            runtime
            and runtime.get("activation_allowed") is True
            and runtime.get("external_service_required") is False
        )

    def conversation_task_state(self, conversation_id: str) -> dict[str, Any]:
        """Everything this conversation has actually established, as things.

        A projection, not a second store: it is read from the turns that
        produced the evidence, so it can never disagree with the transcript and
        there is no migration to get wrong. Newest evidence wins; a later
        research turn replaces an earlier one rather than merging two sets of
        prices from different days into one list.
        """

        from . import task_state

        row = self.database.fetch_one(
            """
            SELECT technical_details_json FROM messages
            WHERE conversation_id = ? AND role = 'assistant'
              AND technical_details_json LIKE '%"observations"%'
            ORDER BY sequence DESC LIMIT 1
            """,
            (conversation_id,),
        )
        orchestration: Mapping[str, Any] = {}
        if row:
            details = parse_json(row["technical_details_json"], None) or {}
            orchestration = details.get("orchestration") or {}

        images = self.database.fetch_all(
            """
            SELECT technical_details_json FROM messages
            WHERE conversation_id = ? AND role = 'assistant'
              AND technical_details_json LIKE '%"generated_image"%'
            ORDER BY sequence DESC LIMIT ?
            """,
            (conversation_id, task_state.MAX_ARTIFACTS),
        )
        artifacts = []
        for record in images or []:
            details = parse_json(record["technical_details_json"], None) or {}
            image = details.get("generated_image")
            if isinstance(image, Mapping):
                artifacts.append(image)

        return task_state.build(
            observations=orchestration.get("observations") or [],
            sources=orchestration.get("sources") or [],
            artifacts=artifacts,
        )

    def supersede_observations(
        self, conversation_id: str, *, refs: Sequence[str], because: str
    ) -> int:
        """Mark evidence the conversation has since disproven.

        A correction that only changes the next sentence leaves the wrong
        observation standing as trusted, so the next question is answered from
        it again. The record is kept rather than deleted — with why, and with
        its original source and retrieval time — because a conversation that
        corrected something should be able to see that the correction landed.
        """

        wanted = {str(ref).strip() for ref in refs if str(ref).strip()}
        if not wanted:
            return 0
        row = self.database.fetch_one(
            """
            SELECT id, technical_details_json FROM messages
            WHERE conversation_id = ? AND role = 'assistant'
              AND technical_details_json LIKE '%"observations"%'
            ORDER BY sequence DESC LIMIT 1
            """,
            (conversation_id,),
        )
        if not row:
            return 0
        details = parse_json(row["technical_details_json"], None) or {}
        orchestration = details.get("orchestration") or {}
        observations = list(orchestration.get("observations") or [])
        changed = 0
        for index, item in enumerate(observations, start=1):
            if not isinstance(item, Mapping):
                continue
            if f"e{index}" not in wanted:
                continue
            updated = dict(item)
            updated["status"] = "superseded"
            updated["superseded_because"] = str(because or "")[:400]
            updated["superseded_at"] = utc_now()
            observations[index - 1] = updated
            changed += 1
        if not changed:
            return 0
        orchestration["observations"] = observations
        details["orchestration"] = orchestration
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), row["id"]),
        )
        return changed

    @staticmethod
    def _turn_changed_something(details: Mapping[str, Any]) -> bool:
        """Whether this turn actually did anything to the computer."""

        orchestration = details.get("orchestration") or {}
        if not isinstance(orchestration, Mapping):
            return False
        if orchestration.get("capability"):
            return True
        for step in orchestration.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            if str(step.get("action") or "") in {"", "respond"}:
                continue
            if str(step.get("status") or "") in {"succeeded", "already_satisfied"}:
                return True

        return bool(details.get("generated_image") or orchestration.get("plan"))

    def _answer_claims_work_it_did_not_do(
        self, *, answer: str, request: str, context, generation_settings
    ) -> bool:
        """Whether a reply says the computer was changed when it was not.

        Asked to delete some log files, the application answered that it had
        "successfully executed a command to delete all log files" and marked
        the turn done. Nothing had run and every file was still there. Being
        told the work is finished is worse than being told it failed.

        One bounded question, asked only on a turn that changed nothing, about
        the sentence that was actually produced. Not a phrase list: the model
        judges whether its own words assert a completed change, which is about
        meaning rather than vocabulary. Any failure to decide is treated as
        honest, because silencing a good answer is its own harm.
        """

        from .dispatch import EFFECT_CLAIM_INSTRUCTION

        body = str(answer or "").strip()
        if not body:
            return False
        try:
            verdict = self._agent_generate(
                [
                    {"role": "system", "content": EFFECT_CLAIM_INSTRUCTION},
                    {
                        "role": "user",
                        "content": f"The user asked: {str(request or '')[:600]}\n\n"
                        f"The reply was:\n{body[:2_000]}",
                    },
                ],
                context=context,
                generation_settings=generation_settings,
            )
        except Exception:
            return False
        from .orchestrator import strip_reasoning

        return "claimed" in strip_reasoning(str(verdict or "")).strip().casefold()

    def _last_turn_made_an_image(self, conversation_id: str) -> bool:
        """Whether the newest answer in this conversation was a picture.

        A revision follows an image; it does not follow whatever else the
        conversation has since gone on to do. Offering "revise_image" because
        an image exists *somewhere* in the history left it permanently on the
        menu, and the model took it: told "but this is the price of 512 gb"
        after a research answer, it rendered a diagram, and told "when did i
        ask for image gen wtf?" it revised the diagram.

        Read from the newest assistant turn only, so the offer expires the
        moment the conversation moves on to something else.
        """

        row = self.database.fetch_one(
            """
            SELECT technical_details_json FROM messages
            WHERE conversation_id = ? AND role = 'assistant'
            ORDER BY sequence DESC LIMIT 1
            """,
            (conversation_id,),
        )
        if not row:
            return False
        details = parse_json(row["technical_details_json"], None) or {}
        if details.get("generated_image"):
            return True
        orchestration = details.get("orchestration") or {}
        return str(orchestration.get("kind") or "") in {"generate_image", "revise_image"}

    def _task_state_note(self, conversation_id: str, *, can_act: bool) -> str:
        """The compact projection of task state placed in front of the model.

        "Give me the exact links" used to run as an ordinary turn with nothing
        to work from, so the model reconstructed product-and-price pairings out
        of its own prose and produced addresses that had never been visited.
        Carrying the evidence fixed that for answering — and only for
        answering. Asked to *open* the cheapest one, the model had verified
        prices in front of it and no sign it was allowed to act on them, so it
        researched the whole thing again. What the projection says about the
        addresses now follows the authority the turn actually has.
        """

        from . import task_state

        try:
            state = self.conversation_task_state(conversation_id)
        except Exception:
            return ""
        return task_state.project(state, can_act=can_act)

    def _turn_instruction(
        self,
        conversation_id: str,
        *,
        agent_mode: bool = False,
        research_available: bool = False,
    ) -> str:
        """The routing line added to a turn, sized to what is really reachable."""

        from .dispatch import build_turn_instruction

        has_previous = self._last_turn_made_an_image(conversation_id)



        services: list[str] = []
        if self.connectors is not None:
            try:



                services = self.connectors.orchestration_hints()
            except Exception:
                services = []
        capabilities = (
            self.granted_automation_capabilities() if agent_mode else []
        )


        provenance = self._task_state_note(
            conversation_id, can_act=bool(agent_mode and capabilities)
        )




        return build_turn_instruction(
            image_available=self._image_generation_available(),
            has_previous_image=has_previous,



            capabilities=capabilities,
            connectors=services,
            agent_mode=agent_mode,


            research_available=research_available,
        ) + provenance

    def _live_runners(
        self,
        *,
        images,
        generation_settings,
        context,
        task,
        conversation_id=None,
        provenance=None,
    ):
        """Build the runners for a decided turn, on the one inference path."""

        from .runners import LiveRunners



        agent_mode = bool(generation_settings.get("agent_mode"))
        capabilities = (
            self.granted_automation_capabilities() if agent_mode else []
        )

        def generate(messages: list[dict[str, str]]) -> str:
            return self._agent_generate(
                messages, context=context, generation_settings=generation_settings
            )

        def search(query: str):
            try:
                return self._search_web(query, limit=6)
            except Exception:
                return []





        timeline: list[dict[str, Any]] = []

        def publish(step: Mapping[str, Any]) -> None:
            snapshot = task.snapshot()
            timeline.append(
                {
                    "step": step.get("step"),
                    "action": step.get("action"),
                    "reason": step.get("reason") or "",
                    "status": step.get("status"),
                    "arguments": dict(step.get("arguments") or {}),
                    "observation": dict(step.get("observation") or {}),
                    "route": step.get("route"),
                    "duration_ms": step.get("duration_ms"),
                }
            )
            del timeline[:-40]
            context.update(
                phase=f"{snapshot['state_label']}: {step['action']}",
                details={
                    **dict(provenance or {}),
                    "conversation_id": conversation_id,
                    "agent_events": list(timeline),
                    "agent_task": {
                        **snapshot,
                        "step": step["step"],
                        "action": step["action"],
                        "reason": step.get("reason") or "",
                        "status": step.get("status"),
                        "route": step.get("route"),
                    },
                },
            )

        def publish_research(progress: Mapping[str, Any]) -> None:
            """Research reporting itself while it is still running.

            Research took two minutes and said nothing until it was over, so
            the application looked frozen for the whole of it. The snapshot is
            whole rather than incremental, so the interface renders whatever
            it has whenever it polls and a missed update costs nothing.
            """

            waves = list(progress.get("waves") or [])
            sites = sum(len(wave.get("sites") or []) for wave in waves)
            context.update(
                phase=(
                    f"Searching {sites} websites"
                    if str(progress.get("phase")) == "searching"
                    else "Reading pages"
                ),
                details={
                    **dict(provenance or {}),
                    "conversation_id": conversation_id,
                    "research_progress": dict(progress),
                },
            )

        return LiveRunners(
            broker=self.automation,
            connectors=self.connectors,
            images=images,
            generate=generate,
            task=task,
            capabilities=capabilities,
            authority_mode=str(generation_settings["computer_authority_mode"]),
            search=search,
            read=self._read_source,



            continue_until_satisfied=bool(generation_settings.get("agent_mode")),





            established=self._task_state_note(
                str(conversation_id or ""), can_act=bool(capabilities)
            )
            if conversation_id
            else "",
            memory=self.memory,
            on_step=publish,
            on_research=publish_research,
            should_stop=context.stop_requested,




            checkpoint_path=(
                self.database.path.parent / "missions" / f"{task.task_id}.json"
            ),



            describe_screenshot=(
                (
                    lambda path: self._describe_screenshot(
                        path, conversation_id=str(conversation_id or "")
                    )
                )
                if conversation_id and requires_sight(capabilities)
                else None
            ),
        )

    def _search_web(self, query: str, *, limit: int = 6) -> list[dict[str, str]]:
        """Search, preferring the cheap request and falling back to the browser.

        The public index answers a plain HTTP request with HTTP 202 and a
        notice page whenever it decides the caller is not a browser, and it
        does so by address as well as by user agent. That is not a failure to
        route around quietly: it left every search returning nothing at all.

        So the same query is run again in the browser session the user already
        granted — a real engine with a real profile, which is what the index
        serves — and the results are read structurally from the page.
        """

        from ..automation.broker import BROWSER_CAPABILITY
        from ..tooling.web_search import SEARCH_ENDPOINT, _unwrap_result_url

        try:
            results = self.web_search.search(query, limit=limit)
        except Exception:
            results = []
        if results:
            return results
        if (
            self.automation is None
            or BROWSER_CAPABILITY not in self.granted_automation_capabilities()
        ):
            return []

        address = SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})

        def call(command: str, **arguments: Any) -> dict[str, Any]:
            return self.automation.invoke(
                {
                    "capability": BROWSER_CAPABILITY,
                    "arguments": {"command": command, **arguments},
                    "user_confirmed": True,
                    "authority_mode": "full_access",
                }
            )

        try:
            call("open_url", url=address)
            found = call("query", role="link", limit=60)
        except Exception:
            return []
        collected: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in found.get("matches") or []:
            unwrapped = _unwrap_result_url(str(item.get("href") or ""))
            parsed = urllib.parse.urlsplit(unwrapped)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            if parsed.hostname.casefold().endswith("duckduckgo.com"):
                continue
            normalised = urllib.parse.urlunsplit(parsed._replace(fragment=""))
            if normalised in seen:
                continue



            seen.add(normalised)
            collected.append(
                {
                    "title": " ".join(str(item.get("name") or "").split())[:240],
                    "url": normalised,
                    "snippet": "",
                }
            )
            if len(collected) >= limit:
                break
        return collected

    def _read_source(self, url: str) -> dict[str, Any]:
        """Fetch one source's structured text for the research ledger.

        Reading goes through the browser capability the user granted, in the
        session Salty Steak owns. That is deliberate: research reads real pages
        with the same audited boundary as every other host action, rather than
        opening a second, unaudited way out to the network.
        """

        from ..automation.broker import BROWSER_CAPABILITY

        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable in this build.")
        if BROWSER_CAPABILITY not in self.granted_automation_capabilities():
            raise RuntimeError(
                "Reading web pages needs the web-pages capability, which is not "
                "enabled."
            )
        address = str(url or "").strip()
        if not address.casefold().startswith(("http://", "https://")):
            raise ValueError("Only http and https addresses can be read.")

        def call(command: str, **arguments: Any) -> dict[str, Any]:
            return self.automation.invoke(
                {
                    "capability": BROWSER_CAPABILITY,
                    "arguments": {"command": command, **arguments},
                    "user_confirmed": True,
                    "authority_mode": "full_access",
                }
            )

        call("open_url", url=address)
        page = call("read_page", text_limit=20_000)
        if str(page.get("status")) != "succeeded":
            raise RuntimeError(f"That page could not be read: {page.get('status')}")
        return {
            "url": str(page.get("url") or address),
            "title": str(page.get("title") or ""),
            "summary": str(page.get("text") or page.get("summary") or ""),
        }

    def _goal_spec_for(
        self,
        *,
        request: str,
        permission_scope: str,
        context: OperationContext,
        generation_settings: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """What must be true when this request is finished.

        Declared before the work runs, from the request rather than from the
        plan, so a requirement the model never acts on is still checked. That
        is the whole point: a run that deleted the file it had been told to
        keep passed verification because every step it executed had worked, and
        the requirement it skipped was not represented anywhere.

        Returns ``None`` when nothing checkable could be read out of the
        request, and ``None`` means the turn cannot be verified — never that it
        succeeded.
        """

        from .dispatch import GOAL_SPEC_INSTRUCTION, GOAL_SPEC_REPAIR_INSTRUCTION
        from .goal_state import GoalSpec
        from .orchestrator import strip_reasoning

        if not str(request or "").strip():
            return None, {"status": "not_requested", "attempts": []}

        from .actions import _whole_json_object

        messages = [
            {"role": "system", "content": GOAL_SPEC_INSTRUCTION},
            {"role": "user", "content": str(request)[:2_000]},
        ]
        attempts: list[dict[str, Any]] = []
        for number in range(1, 3):
            try:
                reply = self._agent_generate(
                    messages,
                    context=context,
                    generation_settings={
                        **dict(generation_settings),

                        "temperature": 0.0,
                        "top_p": 1.0,
                        "top_k": 1,
                        "maximum_output_tokens": 400,
                    },
                )
            except Exception as error:
                attempts.append(
                    {
                        "attempt": number,
                        "status": "generation_failed",
                        "error_type": type(error).__name__,
                    }
                )
                reason = "the generation failed before producing an object"
                reply_text = ""
            else:
                reply_text = strip_reasoning(str(reply or ""))
                parsed = _whole_json_object(reply_text)
                if isinstance(parsed, Mapping):
                    spec = GoalSpec.from_compilation(
                        parsed,
                        fallback_goal=str(request)[:400],

                        permission_scope=permission_scope,
                    )
                    if spec.required:
                        attempts.append(
                            {
                                "attempt": number,
                                "status": "compiled",
                                "required_count": len(spec.required),
                            }
                        )
                        return spec, {
                            "status": "compiled",
                            "attempt_count": number,
                            "attempts": attempts,
                        }
                    reason = "it declared no observable required outcomes"
                    attempts.append(
                        {
                            "attempt": number,
                            "status": "no_required_predicates",
                            "parsed_keys": sorted(str(key) for key in parsed)[:20],
                        }
                    )
                else:
                    reason = "it was not one valid whole JSON object"
                    attempts.append(
                        {
                            "attempt": number,
                            "status": "invalid_json",
                            "reply_characters": len(reply_text),
                        }
                    )

            if number == 1:
                if reply_text:
                    messages.append({"role": "assistant", "content": reply_text[:2_000]})
                messages.append(
                    {
                        "role": "user",
                        "content": GOAL_SPEC_REPAIR_INSTRUCTION.format(reason=reason),
                    }
                )

        return None, {
            "status": "failed",
            "attempt_count": len(attempts),
            "attempts": attempts,
        }

    def _route_the_request_actually_needs(
        self,
        decision: Mapping[str, Any],
        *,
        request: str,
        context: OperationContext,
        generation_settings: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Keep a route only when the request genuinely needs it.

        A switch being on means a capability *may* be used. The turn decides
        whether it *should* be, and it decides that in the same generation that
        writes prose, at the sampling settings prose wants. Measured on the
        installed application, "hi" under Cooking with Research available
        answered normally on one trial, researched the word over six sources on
        another, and began rendering a greeting card on a third.

        So the decision is checked once, on its own, before it runs. Returning
        ``None`` sends the turn down the ordinary answering path.

        Deliberately not a keyword gate. The check is asked about *this*
        request and *this* route, and a list of words that means "no tool
        needed" would be wrong about somebody's request — which is the whole
        reason the routing is semantic in the first place.
        """

        from .dispatch import ROUTE_DESCRIPTIONS, ROUTE_NECESSITY_INSTRUCTION

        action = str(decision.get("action") or "")
        described = ROUTE_DESCRIPTIONS.get(action)
        if described is None:

            return dict(decision)
        if not str(request or "").strip():
            return dict(decision)

        try:
            verdict = self._agent_generate(
                [
                    {"role": "system", "content": ROUTE_NECESSITY_INSTRUCTION},




                    {"role": "user", "content": str(request)[:1_500]},
                ],
                context=context,



                generation_settings={
                    **dict(generation_settings),
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "top_k": 1,
                    "maximum_output_tokens": 6,
                },
            )
        except Exception:

            return dict(decision)




        if isinstance(getattr(self, "_route_trace", None), dict):
            self._route_trace["necessity_verdict"] = str(verdict or "")[:80]
            self._route_trace["necessity_route"] = action

        if not _reads_as_unnecessary(verdict):
            return dict(decision)
        self._route_vetoed = {"route": action, "verdict": str(verdict)[:80]}
        return None

    def _dispatch_turn(
        self,
        *,
        reply_text: str,
        request: str,
        history: list[dict[str, str]],
        conversation_id: str,
        message_id: str,
        generation_settings: Mapping[str, Any],
        context: OperationContext,
        provenance: Mapping[str, Any] | None = None,
    ):
        """Route one model reply. Returns None when it was simply an answer."""

        from .dispatch import (
            DECISION_REPAIR_INSTRUCTION,
            TurnDispatcher,
            looks_like_a_decision_attempt,
            read_decision,
            recover_agent_route,
        )
        from .orchestrator import (
            GENERATE_IMAGE,
            PLAN,
            RESEARCH,
            RESPOND,
            REVISE_IMAGE,
            SINGLE_ACTION,
            conversation_request,
            latest_user_message,
            strip_reasoning,
        )












        if bool(generation_settings.get("research_forced")):
            decision = {"action": "research", "question": request}
        elif bool(generation_settings.get("image_mode")):



            decision = {
                "action": "generate_image",
                "reason": "The user asked for an image directly.",
                "model_notes": strip_reasoning(reply_text)[:2_000],
            }
        else:
            decision = read_decision(reply_text)










            self._route_trace = {
                "raw_reply": strip_reasoning(str(reply_text or ""))[:1_500],
                "parsed_route": (decision or {}).get("action") or "respond",
                "looked_like_a_decision": looks_like_a_decision_attempt(reply_text),
                "reply_characters": len(str(reply_text or "")),
                "reasoning_mode": generation_settings.get("reasoning_mode"),
                "research_available": bool(
                    generation_settings.get("research_available")
                ),
                "agent_mode": bool(generation_settings.get("agent_mode")),
                "temperature": generation_settings.get("temperature"),
                "top_p": generation_settings.get("top_p"),
                "capabilities_offered": len(self.granted_automation_capabilities()),
            }




            if decision is not None:
                self._route_vetoed = None
                decision = self._route_the_request_actually_needs(
                    decision,
                    request=latest_user_message(history) or request,
                    context=context,
                    generation_settings=generation_settings,
                )
                if decision is None:




                    from .dispatch import TurnOutcome

                    vetoed = dict(self._route_vetoed or {})
                    self._route_vetoed = None
                    try:
                        answer = strip_reasoning(
                            str(
                                self._agent_generate(
                                    [
                                        {
                                            "role": "system",
                                            "content": DECISION_REPAIR_INSTRUCTION,
                                        },
                                        {"role": "user", "content": str(reply_text)[:4_000]},
                                    ],
                                    context=context,
                                    generation_settings=generation_settings,
                                )
                                or ""
                            )
                        ).strip()
                    except Exception:
                        answer = ""
                    if not answer or looks_like_a_decision_attempt(answer):



                        answer = (
                            "I could not finish that one. Ask me again and I "
                            "will answer it properly."
                        )
                    return TurnOutcome(
                        RESPOND,
                        content=answer,
                        details={"route_not_needed": vetoed.get("route") or True},
                    )
        decision_recovery: dict[str, Any] = {}
        if decision is None and looks_like_a_decision_attempt(reply_text):






            recovered = recover_agent_route(
                reply_text,
                agent_mode=bool(generation_settings.get("agent_mode")),
                capabilities=self.granted_automation_capabilities(),
            )
            if recovered is not None:
                recovered = self._route_the_request_actually_needs(
                    recovered,
                    request=latest_user_message(history) or request,
                    context=context,
                    generation_settings=generation_settings,
                )
            if recovered is not None:
                decision = recovered
                decision_recovery = {
                    "decision_recovered_to_agent": True,
                    "unreadable_decision": str(
                        strip_reasoning(str(reply_text or ""))
                    )[:1_200],
                }

        if decision is None and looks_like_a_decision_attempt(reply_text):



            try:
                corrected = self._agent_generate(
                    [
                        {"role": "system", "content": DECISION_REPAIR_INSTRUCTION},
                        {"role": "user", "content": str(reply_text)[:4_000]},
                    ],
                    context=context,
                    generation_settings=generation_settings,
                )
            except Exception:
                corrected = ""
            decision = read_decision(corrected)
            if decision is None:
                from .dispatch import TurnOutcome





                second = strip_reasoning(str(corrected or "")).strip()




                unread = str(strip_reasoning(str(reply_text or "")))[:1_200]
                if second and not looks_like_a_decision_attempt(second):
                    return TurnOutcome(
                        "respond",
                        content=second,
                        details={
                            "decision_corrected_to_prose": True,
                            "unreadable_decision": unread,
                        },
                    )






                return TurnOutcome(
                    "respond",
                    content=(
                        "I could not finish that one. Nothing was changed on "
                        "your computer. Ask me again and I will answer it "
                        "properly."
                    ),
                    details={
                        "decision_unparsable": True,
                        "unreadable_decision": unread,
                    },
                )
        if decision is None or decision.get("action") == RESPOND:
            return None




        goal_spec = None
        goal_compilation: dict[str, Any] = {
            "status": "not_requested",
            "attempts": [],
        }
        if str(decision.get("action") or "") in {SINGLE_ACTION, PLAN, RESEARCH}:
            action = str(decision.get("action") or "")
            permission_scope = (
                "research:enabled"
                if action == RESEARCH
                else "computer:"
                + str(generation_settings.get("computer_authority_mode") or "ask_every_time")
            )
            goal_spec, goal_compilation = self._goal_spec_for(
                request=latest_user_message(history) or request,
                permission_scope=permission_scope,
                context=context,
                generation_settings=generation_settings,
            )



        task = TaskContext(goal=request[:200])



        seed_world_state(task, self.host_environment.get())
        bind_operation_stop(task, context.stop_requested)

        from ..imaging import ImageOrchestrator
        from .runners import LiveRunners





        established = next(
            (
                str(message.get("content") or "")
                for message in reversed(history)
                if str(message.get("role") or "").casefold() == "assistant"
            ),
            "",
        )[:2_000]

        def author_brief(*, request: str, brief, notes: str = "") -> dict[str, Any] | None:
            """Base Steak writes the render brief; Steak Gen only draws it."""

            from ..imaging.orchestrator import BRIEF_AUTHOR_INSTRUCTION
            from .actions import _whole_json_object

            body = [f"REQUEST:\n{request}"]
            if notes:
                body.append(f"NOTES:\n{str(notes)[:2_000]}")
            if established:
                body.append(f"ESTABLISHED IN THIS CONVERSATION:\n{established}")
            body.append(
                "BRIEF SO FAR:\n" + json.dumps(brief.to_dict(), default=str)
            )
            reply = self._agent_generate(
                [
                    {"role": "system", "content": BRIEF_AUTHOR_INSTRUCTION},
                    {"role": "user", "content": "\n\n".join(body)},
                ],
                context=context,
                generation_settings=generation_settings,
            )
            parsed = _whole_json_object(strip_reasoning(str(reply or "")))
            return dict(parsed) if isinstance(parsed, Mapping) else None

        images = ImageOrchestrator(
            generate=self._render_image,
            backend="steak-gen-1-scaledfp8",
            author=author_brief,


            rebrief=lambda correction, brief: author_brief(
                request=conversation_request(history) or request,
                brief=brief,
                notes=correction,
            ),
        )
        runners = self._live_runners(
            images=images,
            generation_settings=generation_settings,
            context=context,
            task=task,
            conversation_id=conversation_id,
            provenance=provenance,
        )


        research_reachable = bool(
            generation_settings.get("research_available")
            or generation_settings.get("research_forced")
            or generation_settings.get("web_search_enabled")
        )
        computer_reachable = bool(
            generation_settings.get("agent_mode")
        ) and bool(self.granted_automation_capabilities())
        dispatcher = TurnDispatcher(
            images=images,
            image_store=self.image_store,
            run_agent=runners.run_action,
            run_workflow=runners.run_plan,




            run_research=(
                runners.run_research
                if research_reachable
                else None
            ),
            task=task,




            permitted=frozenset(
                {RESPOND}
                | ({SINGLE_ACTION, PLAN} if computer_reachable else set())
                | ({RESEARCH} if research_reachable else set())
                | (
                    {GENERATE_IMAGE, REVISE_IMAGE}
                    if self.image_generation_model
                    else set()
                )
            ),
        )
        turn = dispatcher.dispatch(
            decision,
            reply_text=reply_text,




            request=conversation_request(history) or request,

            latest_request=latest_user_message(history) or request,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        turn.details.update(decision_recovery)
        turn.goal_spec = goal_spec
        turn.details["goal_compilation"] = goal_compilation
        return turn

    @staticmethod
    def _capture_path_in(turn) -> str | None:
        """The newest screen-capture artifact a finished turn produced."""

        details = dict(getattr(turn, "details", {}) or {})
        found: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, Mapping):
                artifact = value.get("artifact")
                if isinstance(artifact, Mapping) and artifact.get("path"):
                    found.append(str(artifact["path"]))
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(details)
        return found[-1] if found else None

    def _register_pending_capture(self, connection) -> None:
        """Write the capture's artifact row beside the message that owns it."""

        pending = getattr(self, "_pending_capture", None)
        if not pending:
            return
        self._pending_capture = None
        connection.execute(
            """
            INSERT INTO chat_artifacts(
                id, conversation_id, message_id, operation_id, proposal_id,
                kind, relative_path, media_type, size_bytes, sha256,
                width, height, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'image', ?, 'image/png', ?, ?, ?, ?, ?, ?)
            """,
            (
                pending["artifact_id"],
                pending["conversation_id"],
                pending["message_id"],
                pending["operation_id"],


                f"capture-{pending['artifact_id']}",
                pending["relative"],
                pending["size_bytes"],
                pending["sha256"],
                pending["width"],
                pending["height"],
                json_text(
                    {
                        "schema": "salty-steak-capture-provenance-v1",
                        "source": "screen.capture",
                        "source_path": pending["source_path"],
                    }
                ),
                utc_now(),
            ),
        )

    def _publish_capture(
        self,
        turn,
        *,
        conversation_id: str,
        operation_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Make a screen capture viewable in the transcript.

        The broker writes an uncompressed BMP into its own artifact root, which
        the interface cannot load and the user cannot reach. Answering "show me
        the screen" with a file path is not showing anyone anything, so the
        capture is converted once and registered as a chat artifact through the
        same table and the same viewer a generated image uses.
        """

        source = self._capture_path_in(turn)
        if not source:
            return None
        image_path = Path(source)
        if not image_path.is_file():
            return None
        try:
            from PIL import Image
        except Exception:
            return None

        artifact_root = self.image_artifact_root
        if artifact_root is None:
            return None
        artifact_id = new_id()
        relative = f"{conversation_id}/{artifact_id}.png"
        destination = artifact_root / conversation_id / f"{artifact_id}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(image_path) as opened:
                converted = opened.convert("RGB")
                width, height = converted.size
                converted.save(destination, format="PNG", optimize=True)
        except Exception:
            return None

        payload = destination.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()



        self._pending_capture = {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "operation_id": operation_id,
            "relative": relative,
            "size_bytes": len(payload),
            "sha256": digest,
            "width": width,
            "height": height,
            "source_path": str(image_path),
        }
        return {
            "schema": "salty-steak-generated-image-v1",
            "id": artifact_id,
            "sha256": digest,
            "media_type": "image/png",
            "size_bytes": len(payload),
            "width": width,
            "height": height,
            "prompt": "Screen capture",
            "model_name": "Screen capture",
        }

    def _image_proposal_from_turn(self, turn) -> dict[str, Any] | None:
        """Turn a prepared image job into the reviewable proposal shape.

        The render brief becomes the prompt. Everything downstream — review,
        confirmation, generation, artifact persistence, cancellation — is the
        path that already existed and is already proven.
        """

        from .orchestrator import GENERATE_IMAGE, REVISE_IMAGE

        if turn.kind not in {GENERATE_IMAGE, REVISE_IMAGE}:
            return None
        job = turn.details.get("image_job")
        rendered = str(turn.details.get("render_brief") or "")
        if not job or not rendered:
            return None

        ready = self._image_generation_available()
        return {
            "schema": HOST_ACTION_SCHEMA,
            "id": new_id(),
            "kind": IMAGE_ACTION,
            "title": "Create an image",
            "summary": str(job.get("subject") or "")[:4_000],
            "arguments": {
                "prompt": rendered[:4_000],


                "negative_prompt": str(turn.details.get("render_negative") or "")[:4_000],
            },
            "state": "pending_review" if ready else "blocked_runtime_unavailable",
            "requires_confirmation": True,
            "execution_allowed": False,
            "runtime_reason": None if ready else _image_runtime_reason(
                self.image_generation_model
            ),

            "image_job_id": job.get("job_id"),
            "image_revision": job.get("revision"),
            "image_parent_job_id": job.get("parent_job_id"),
            "planner_output_sha256": hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest(),
            "planner_output_bytes": len(rendered.encode("utf-8")),
        }

    @staticmethod
    def _image_will_render(details: Mapping[str, Any]) -> bool:
        """Whether this turn's proposal starts drawing without being asked again.

        One predicate, read twice: once before the message is written so the
        turn can say it is creating an image rather than waiting on the user,
        and once after, to actually start it. Two conditions written twice is
        how a message ends up describing something that never happened.
        """

        from .orchestrator import GENERATE_IMAGE, REVISE_IMAGE

        proposal = details.get("host_action_proposal")
        orchestration = details.get("orchestration") or {}
        if not isinstance(proposal, Mapping) or not isinstance(orchestration, Mapping):
            return False
        if str(orchestration.get("kind")) not in {GENERATE_IMAGE, REVISE_IMAGE}:
            return False
        return (
            proposal.get("kind") == IMAGE_ACTION
            and proposal.get("state") == "pending_review"
        )

    def _seed_of_image_job(self, conversation_id: str, job_id: str) -> int | None:
        """The seed a finished image job was actually rendered with.

        Read from the artifact's own provenance rather than recomputed, so a
        revision reproduces the picture it is revising even after a restart.
        """

        rows = self.database.fetch_all(
            """
            SELECT m.technical_details_json AS details,
                   a.provenance_json AS provenance
            FROM messages m
            JOIN chat_artifacts a ON a.message_id = m.id
            WHERE m.conversation_id = ? AND m.role = 'assistant'
            ORDER BY m.sequence DESC LIMIT 24
            """,
            (conversation_id,),
        )
        for row in rows or []:
            details = parse_json(row["details"], {}) or {}
            proposal = details.get("host_action_proposal") or {}
            if str(proposal.get("image_job_id") or "") != job_id:
                continue
            provenance = parse_json(row["provenance"], {}) or {}
            seed = provenance.get("seed")
            if isinstance(seed, int):
                return seed
        return None

    @staticmethod
    def _image_underway_sentence(turn) -> str:
        """What the transcript says while the picture is being made."""

        job = dict(turn.details.get("image_job") or {})
        subject = str(job.get("subject") or "").strip()
        declared = str(job.get("image_type") or "").strip()
        kind = "image" if declared in {"", "other"} else declared.replace("_", " ")
        revision = int(job.get("revision") or 1)
        if revision > 1:
            return f"Revising the {kind}, keeping everything else the same."
        if subject:
            return f"Creating the {kind}: {subject}."
        return f"Creating the {kind}."

    def _start_requested_image(
        self,
        *,
        conversation_id: str,
        assistant_message_id: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Render the image the user asked for, instead of offering to.

        The proposal shape stays: it carries the checked render brief, the
        artifact persistence, the progress reporting and the cancellation that
        the reviewed path already had, and none of that is worth rebuilding.
        What changes is who confirms it. Asking someone to approve a picture
        they have not seen, having just asked for that picture, is a click that
        conveys no information — and it is why "Generate an image of a cow"
        produced a sentence about a cow and no cow.

        Only an orchestrated image decision starts itself. Anything else that
        writes a proposal keeps its review.
        """

        proposal = details.get("host_action_proposal")
        if not self._image_will_render(details) or not isinstance(proposal, Mapping):
            return None





        settings: dict[str, Any] | None = None
        parent_job_id = str(proposal.get("image_parent_job_id") or "")
        if parent_job_id:
            inherited = self._seed_of_image_job(conversation_id, parent_job_id)
            if inherited is not None:
                settings = {"seed": inherited}
        try:
            return self.confirm_image_generation(
                conversation_id,
                str(proposal.get("id") or ""),
                assistant_message_id,
                settings,
            )
        except Exception:



            self._mark_turn_completion(assistant_message_id, "waiting")
            return None

    def _mark_turn_completion(self, assistant_message_id: str, completion: str) -> None:
        """Correct how a committed turn says it ended."""

        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT technical_details_json FROM messages WHERE id = ?",
                    (assistant_message_id,),
                ).fetchone()
                if row is None:
                    return
                details = parse_json(row["technical_details_json"], {})
                if not isinstance(details, dict):
                    return
                details["turn_completion"] = completion
                if completion != "rendering":
                    details["image_render_started"] = False
                connection.execute(
                    "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                    (json_text(details), assistant_message_id),
                )
        except Exception:
            return

    def _render_image(self, brief, job):
        """Placeholder generator for the orchestrator's own bookkeeping.

        Chat never calls this: an image request becomes a reviewable proposal
        and the confirmed path does the rendering, so the runtime hand-off,
        artifact persistence and cancellation all stay in the one place that
        already handles them.
        """

        raise RuntimeError(
            "Image rendering runs through the confirmed image action, not here."
        )

    def _generate_turn(
        self,
        *,
        conversation_id: str,
        user_message_id: str,
        history: list[dict[str, str]],
        assistant_sequence: int,
        active_version_id: str,
        context: OperationContext,
        cancellation_token: str,
        active_target_kind: str = "saved_version",
        runtime_profile_id: str = "legacy_saved_version",
        source_sha256: str | None = None,
        generation_settings: dict[str, Any] | None = None,
        previous_response_id: str | None = None,
    ) -> dict[str, Any]:
        generation_settings = self._normalise_generation_settings(
            generation_settings
        )




        turn_started = time.monotonic()
        relationship = {
            "conversation_id": conversation_id,
            "user_message_id": user_message_id,
            "generation_id": context.operation_id,
            "active_version_id": active_version_id,
            "active_target_kind": active_target_kind,
            "active_target_id": active_version_id,
            "runtime_profile_id": runtime_profile_id,
            "source_sha256": source_sha256,
            "runtime_id": None,
            "template_version": CHAT_TEMPLATE_VERSION,
            "cancellation_token": cancellation_token,
            "cancellation_state": "active",
            **_generation_control_provenance(generation_settings),
        }
        if previous_response_id:
            relationship["retry_of_assistant_message_id"] = previous_response_id
        search_enabled = bool(generation_settings["web_search_enabled"])
        search_query = next(
            (
                str(message.get("content") or "")
                for message in reversed(history)
                if message.get("role") == "user"
            ),
            "",
        )





        agent_mode = bool(generation_settings.get("agent_mode"))
        action_intent = detect_host_action_intent(search_query)
        if action_intent == IMAGE_ACTION or (
            agent_mode
            and generation_settings.get("computer_authority_mode") == "full_access"
        ):
            action_intent = None




        if action_intent is not None and not bool(
            generation_settings.get("agent_mode")
        ):
            action_intent = None
        relationship["host_action"] = {
            "intent": action_intent,
            "state": "planning" if action_intent else "not_requested",
            "execution_requested": False,
            "execution_performed": False,
        }
        relationship["agent_mode"] = agent_mode




        research_available = bool(
            generation_settings.get("research_available")
            or generation_settings.get("research_forced")
            or generation_settings.get("web_search_enabled")
        )
        relationship["research_available"] = research_available
        orchestration = self._turn_instruction(
            conversation_id,
            agent_mode=agent_mode,
            research_available=research_available,
        )
        if orchestration:
            insertion = 0
            while insertion < len(history) and history[insertion].get("role") == "system":
                insertion += 1
            history.insert(insertion, {"role": "system", "content": orchestration})
        if action_intent:


            insertion = 0
            while insertion < len(history) and history[insertion].get("role") == "system":
                insertion += 1
            history.insert(
                insertion,
                {
                    "role": "system",
                    "content": host_action_planning_prompt(action_intent),
                },
            )
        relationship["web_search"] = {
            "enabled": search_enabled,



            "state": "available" if search_enabled else "disabled",
            "query": None,
            "result_count": 0,
        }
        context.update(phase="Preparing Chat runtime", details=relationship)
        legacy_identity = None
        generation = generation_settings
        generation_arguments = {
            "messages": history,
            "reserved_output_tokens": max(
                int(generation["maximum_output_tokens"]),
                int(self.config.section("generation")["reserved_output_tokens"]),
            ),
            "maximum_output_tokens": int(generation["maximum_output_tokens"]),
            "temperature": float(generation["temperature"]),
            "top_p": float(generation["top_p"]),
            "top_k": int(generation["top_k"]),
            "repetition_penalty": float(generation["repetition_penalty"]),
            "seed": int(generation["seed"]),
            "should_stop": context.stop_requested,
            "context_window_tokens": int(generation["context_window_tokens"]),
            "stop_sequences": list(generation["stop_sequences"]),
        }
        if active_target_kind == "model_bundle":
            if self.model_bundle_runtime is None:
                raise RuntimeError("Native model bundle runtime disappeared")



            with self._bundle_lifecycle_lock:
                bundle_identity = self._ensure_bundle_runtime(
                    {
                        "kind": active_target_kind,
                        "id": active_version_id,
                        "profile_id": runtime_profile_id,
                        "source_sha256": source_sha256,
                    },
                    context.operation_id,
                )
                runtime_instance_id = str(bundle_identity["runtime_id"])
                relationship["runtime_id"] = runtime_instance_id
                relationship["source_sha256"] = source_sha256
                context.raise_if_stop_requested()
                context.update(phase="Generating response", details=relationship)
                reasoning_mode = str(generation["reasoning_mode"])
                generation_arguments["messages"] = _apply_reasoning_mode(history, reasoning_mode)
                generation_arguments["reasoning_mode"] = reasoning_mode
                generation_arguments["maximum_output_mode"] = str(
                    generation["maximum_output_mode"]
                )
                relationship["reasoning_control"] = (
                    "model_soft_switch_plus_template_boundary"
                )
                preview_state: dict[str, Any] = {
                    "last_text": "",
                    "last_tokens": -1,
                }

                def publish_preview(value: dict[str, Any]) -> None:
                    preview = _bounded_generation_preview(value)
                    if preview is None:
                        return
                    if (
                        preview["tail_text"] == preview_state["last_text"]
                        and preview["token_count"] == preview_state["last_tokens"]
                    ):
                        return
                    preview_state["last_text"] = preview["tail_text"]
                    preview_state["last_tokens"] = preview["token_count"]
                    context.update(
                        phase="Generating response",
                        details={
                            **relationship,
                            "generation_preview": preview,
                        },
                    )

                generation_arguments["on_preview"] = publish_preview
                response = self.model_bundle_runtime.generate(**generation_arguments)
        else:
            self._ensure_runtime(active_version_id)
            legacy_identity = self.runtime.identity
            if not legacy_identity or legacy_identity.checkpoint_id != active_version_id:
                raise RuntimeError(
                    "Runtime identity does not match the active saved version"
                )
            runtime_instance_id = legacy_identity.runtime_id
            source_sha256 = getattr(legacy_identity, "weight_sha256", source_sha256)
            relationship["runtime_id"] = runtime_instance_id
            relationship["source_sha256"] = source_sha256
            context.raise_if_stop_requested()
            context.update(phase="Generating response", details=relationship)
            response = self.runtime.generate(
                active_checkpoint_id=active_version_id,
                **generation_arguments,
            )
        if response.cancelled or context.stop_requested():
            self.database.execute(
                """
                UPDATE messages
                SET technical_details_json = ?
                WHERE id = ? AND conversation_id = ? AND role = 'user'
                """,
                (
                    json_text(
                        {
                            **relationship,
                            "runtime_id": (
                                runtime_instance_id
                            ),
                            "cancellation_state": "acknowledged",
                            "generation_state": "cancelled",
                        }
                    ),
                    user_message_id,
                    conversation_id,
                ),
            )
            context.update(
                phase="Stopped",
                details={
                    **relationship,
                    "runtime_id": runtime_instance_id,
                    "cancellation_state": "acknowledged",
                    "partial_output_saved": False,
                },
            )
            raise OperationInterrupted(
                "Generation stopped; uncommitted output was discarded"
            )
        if active_target_kind == "model_bundle":
            origin = {
                "model_bundle_id": active_version_id,
                "model_bundle_sha256": source_sha256,
                "evaluation_state": "native_runtime_smoke_verified",
            }
        else:
            assert legacy_identity is not None
            origin = self.database.fetch_one(
                """
                SELECT v.id AS saved_version_id, v.additional_steps,
                       v.total_trained_steps, vl.lineage_id,
                       vl.parent_version_id, vl.role,
                       CASE WHEN EXISTS(
                           SELECT 1 FROM evaluations e
                           WHERE e.saved_version_id = v.id
                             AND e.status = 'completed'
                       ) THEN 'evaluated' ELSE 'not_evaluated' END AS evaluation_state
                FROM saved_versions v
                LEFT JOIN version_lineage vl
                  ON vl.saved_version_id = v.id AND vl.role = 'trained'
                WHERE v.id = ?
                ORDER BY vl.created_at DESC LIMIT 1
                """,
                (legacy_identity.checkpoint_id,),
            ) or {}
        details = {
            **response.technical_details,
            **origin,
            **_generation_control_provenance(generation),
            "context_omitted": response.omitted_turns > 0,
            "omitted_turns": response.omitted_turns,
            "generation_state": "stopped" if response.cancelled else "completed",
            "generation_operation_id": context.operation_id,
            "generation_id": context.operation_id,
            "active_version_id": active_version_id,
            "active_target_kind": active_target_kind,
            "active_target_id": active_version_id,
            "runtime_profile_id": runtime_profile_id,
            "runtime_instance_id": runtime_instance_id,
            "source_sha256": source_sha256,
            "template_version": CHAT_TEMPLATE_VERSION,
            "web_search": relationship["web_search"],
            "cancellation_token": cancellation_token,
            "cancellation_state": "not_requested",
            "turn_duration_ms": round((time.monotonic() - turn_started) * 1000),
        }
        if previous_response_id:
            details.update(
                {
                    "retry_user_message_id": user_message_id,
                    "retry_of_assistant_message_id": previous_response_id,
                }
            )





        assistant_content, reasoning_text = _separate_reasoning(response.text)
        if reasoning_text:
            details["reasoning_text"] = reasoning_text





        assistant_id = new_id()
        self._route_trace = None




        self._turn_generations = []
        self._record_generation(response)
        turn = self._dispatch_turn(
            reply_text=response.text,
            request=search_query,
            history=history,
            conversation_id=conversation_id,
            message_id=user_message_id,
            generation_settings=generation,
            context=context,
            provenance=relationship,
        )
        if turn is not None:
            assistant_content = turn.content or response.text
            details["orchestration"] = turn.to_dict()



            shown = self._publish_capture(
                turn,
                conversation_id=conversation_id,
                operation_id=context.operation_id,
                message_id=assistant_id,
            )
            if shown is not None:
                details["generated_image"] = shown
                assistant_content = turn.content or "Here is the screen."
            proposal = self._image_proposal_from_turn(turn)
            if proposal is not None:




                details["host_action_proposal"] = proposal



                if proposal["state"] == "pending_review":
                    assistant_content = self._image_underway_sentence(turn)
                relationship["host_action"] = {
                    "intent": IMAGE_ACTION,
                    "state": proposal["state"],
                    "proposal_id": proposal["id"],
                    "execution_requested": False,
                    "execution_performed": False,
                }
                action_intent = None
        action_result = normalise_host_action_response(
            intent=action_intent,
            user_text=search_query,
            model_text=response.text,
            proposal_id=new_id(),
            image_runtime=self.image_generation_model,
        )
        if action_result is not None:
            assistant_content = str(action_result["content"])
            proposal = dict(action_result["proposal"])
            details["host_action_proposal"] = proposal
            relationship["host_action"] = {
                "intent": action_intent,
                "state": proposal["state"],
                "proposal_id": proposal["id"],
                "execution_requested": False,
                "execution_performed": False,
            }









        if (
            bool(generation["agent_mode"])
            and self.granted_automation_capabilities()
            and not self._turn_changed_something(details)
            and self._answer_claims_work_it_did_not_do(
                answer=assistant_content,
                request=search_query,
                context=context,
                generation_settings=generation,
            )
        ):
            details["false_success_prevented"] = True
            details["unperformed_claim"] = str(assistant_content)[:1_200]
            assistant_content = (
                "I did not actually do that — nothing ran on your computer this "
                "turn, so nothing changed. I had started to describe it as done, "
                "which was wrong. Ask me again and I will carry it out, or tell "
                "me to go ahead and I will."
            )
            turn_status_override = "partial"
        else:
            turn_status_override = ""




        if getattr(self, "_route_trace", None):
            details["route_trace"] = self._route_trace
            self._route_trace = None




        visible_tokens = _visible_output_tokens(
            assistant_content, getattr(self, "_turn_generations", []) or []
        )
        if visible_tokens is not None:
            details["visible_output_tokens"] = visible_tokens
        self._turn_generations = []



        assistant_content = _without_control_token_echo(assistant_content)





        if _turn_reached_outside(details):
            from .verification import verify_goal

            spec = getattr(turn, "goal_spec", None)
            from .goal_state import runtime_observer

            verified, goal_evidence = verify_goal(
                details.get("orchestration") or {},
                spec=spec,
                observe=runtime_observer(
                    broker=self.automation,
                    orchestration=details.get("orchestration") or {},
                ),
            )
            details["goal_verified"] = verified
            details["goal_evidence"] = goal_evidence
            if spec is not None:


                details["goal_spec"] = spec.describe()

        details["turn_duration_ms"] = round((time.monotonic() - turn_started) * 1000)
        details["image_render_started"] = self._image_will_render(details)
        details["turn_completion"] = turn_status_override or _turn_completion(
            details,
            response=response,
            turn=turn,
            proposal_pending=bool(details.get("host_action_proposal")),
        )
        finished = utc_now()
        with self.database.transaction() as connection:
            if active_target_kind == "model_bundle":
                current = connection.execute(
                    """
                    SELECT runtime_id, target_kind, target_id, profile_id,
                           source_sha256
                    FROM active_chat_runtime WHERE singleton = 1
                    """
                ).fetchone()
                if (
                    current is None
                    or current["runtime_id"] != runtime_instance_id
                    or current["target_kind"] != active_target_kind
                    or current["target_id"] != active_version_id
                    or current["profile_id"] != runtime_profile_id
                    or current["source_sha256"] != source_sha256
                ):
                    raise RuntimeError(
                        "active model bundle changed before the response could be committed"
                    )
                saved_version_id = None
                legacy_runtime_id = None
            else:
                assert legacy_identity is not None
                current = connection.execute(
                    """
                    SELECT saved_version_id, runtime_id
                    FROM active_runtime WHERE singleton = 1
                    """
                ).fetchone()
                if (
                    current is None
                    or current["saved_version_id"] != legacy_identity.checkpoint_id
                    or current["runtime_id"] != legacy_identity.runtime_id
                ):
                    raise RuntimeError(
                        "active saved version changed before the response could be committed"
                    )
                saved_version_id = legacy_identity.checkpoint_id
                legacy_runtime_id = legacy_identity.runtime_id
            connection.execute(
                """
                UPDATE messages
                SET technical_details_json = ?, target_kind = ?, target_id = ?,
                    runtime_profile_id = ?, runtime_instance_id = ?,
                    source_sha256 = ?
                WHERE id = ? AND conversation_id = ? AND role = 'user'
                """,
                (
                    json_text(
                        {
                            **relationship,
                            "runtime_id": runtime_instance_id,
                            "cancellation_state": "not_requested",
                            "generation_state": "completed",
                        }
                    ),
                    active_target_kind,
                    active_version_id,
                    runtime_profile_id,
                    runtime_instance_id,
                    source_sha256,
                    user_message_id,
                    conversation_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, sequence,
                    saved_version_id, runtime_id, target_kind, target_id,
                    runtime_profile_id, runtime_instance_id, source_sha256,
                    technical_details_json, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assistant_id,
                    conversation_id,
                    assistant_content,
                    assistant_sequence,
                    saved_version_id,
                    legacy_runtime_id,
                    active_target_kind,
                    active_version_id,
                    runtime_profile_id,
                    runtime_instance_id,
                    source_sha256,
                    json_text(details),
                    finished,
                ),
            )
            self._register_pending_capture(connection)
            if previous_response_id:
                previous = connection.execute(
                    "SELECT technical_details_json FROM messages WHERE id = ?",
                    (previous_response_id,),
                ).fetchone()
                previous_details = parse_json(
                    previous["technical_details_json"] if previous else None,
                    {},
                )
                previous_details.update(
                    {
                        "superseded_by_retry_operation_id": context.operation_id,
                        "superseded_by_assistant_message_id": assistant_id,
                    }
                )
                connection.execute(
                    "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                    (json_text(previous_details), previous_response_id),
                )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (finished, conversation_id),
            )


        image_operation = self._start_requested_image(
            conversation_id=conversation_id,
            assistant_message_id=assistant_id,
            details=details,
        )
        result = {
            **relationship,
            "assistant_message_id": assistant_id,
            "image_operation_id": (
                str(image_operation["id"]) if image_operation else None
            ),
            "partial_output_saved": False,
            "finish_reason": details["finish_reason"],


            "generation_preview": {
                "state": "discarded",
                "token_count": int(
                    details.get("generated_output_tokens") or len(response.token_ids)
                ),
            },
        }
        context.update(
            phase="Stopped" if response.cancelled else "Saving response",
            details=result,
        )
        return result
