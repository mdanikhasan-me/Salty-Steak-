"""Conversation persistence and runtime backed chat orchestration."""

from __future__ import annotations

import hashlib
import os
import threading
import re
from collections.abc import Callable, Mapping
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
from ..memory import SemanticMemory
from ..system.config import AppConfig
from ..system.environment import HostEnvironmentRegistry, seed_world_state
from ..tooling.web_search import WebSearchClient, should_search_web, web_results_prompt
from ..versions.tokenizer import CHAT_TEMPLATE_VERSION
from .actions import (
    FILE_TRASH_ACTION,
    TEMP_CLEANUP_ACTION,
    TEMP_CLEANUP_CONFIRMATION,
    build_file_trash_proposal,
    build_temp_cleanup_proposal,
    detect_host_action_intent,
    extract_file_trash_target,
    host_action_planning_prompt,
    normalise_host_action_response,
)
from .agent_loop import VISION_OUTPUT_TOKENS, VISION_PROMPT, AgentLoop
from .task_runtime import TaskContext, bind_operation_stop
from .vision_inputs import VisionInputClaim, VisionInputStore


ActivationStarter = Callable[[str, str | None], dict[str, Any]]
MAX_CONVERSATION_TITLE_LENGTH = 80
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
        self.automation = automation



        self.host_environment = HostEnvironmentRegistry()



        self.memory = SemanticMemory(self.database.path.parent / "salty-memory.db")
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
        }

    def list_conversations(self) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT c.*, COUNT(m.id) AS message_count
            FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id ORDER BY c.updated_at DESC
            """
        )

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
            message["technical_details"] = details
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
            if extract_file_trash_target(exact):
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
            if detect_host_action_intent(exact) == TEMP_CLEANUP_ACTION:
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
        """Run a multi-step computer-control task for one conversation."""

        exact = str(instruction)
        if not exact.strip():
            raise ValueError("An automation task cannot be empty")
        if self.automation is None:
            raise RuntimeError("Local computer automation is unavailable in this build")
        checked_settings = self._normalise_generation_settings(generation_settings)
        capabilities = self.granted_automation_capabilities()
        if not capabilities:
            raise PermissionError(
                "Grant at least one computer-control capability before starting an "
                "automation task"
            )
        with self._generation_lock:
            self.get_conversation(conversation_id)
            self._cancel_active_generation(reason="superseded_by_new_request")
            return self._submit_generation(
                conversation_id,
                lambda context, cancellation_token: self._run_agent_task(
                    conversation_id=conversation_id,
                    instruction=exact,
                    capabilities=capabilities,
                    context=context,
                    cancellation_token=cancellation_token,
                    generation_settings=checked_settings,
                ),
                generation_settings=checked_settings,
            )

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
        return str(response.text)

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

    def _run_agent_task(
        self,
        *,
        conversation_id: str,
        instruction: str,
        capabilities: list[str],
        context: OperationContext,
        cancellation_token: str,
        generation_settings: dict[str, Any],
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        target = self._selected_target()
        next_sequence = len(conversation["messages"])
        user_id = new_id()
        now = utc_now()
        relationship = {
            "conversation_id": conversation_id,
            "user_message_id": user_id,
            "generation_id": context.operation_id,
            "active_version_id": target["id"],
            "active_target_kind": target["kind"],
            "runtime_profile_id": target["profile_id"],
            "source_sha256": target["source_sha256"],
            "template_version": CHAT_TEMPLATE_VERSION,
            "cancellation_token": cancellation_token,
            "cancellation_state": "active",
            "agent_task": {
                "state": "running",
                "step": 0,
                "capabilities": list(capabilities),
                "authority_mode": generation_settings["computer_authority_mode"],
            },
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
                    instruction,
                    next_sequence,
                    target["kind"],
                    target["id"],
                    target["profile_id"],
                    target["source_sha256"],
                    json_text(relationship),
                    now,
                ),
            )
            if conversation["title"] == "New chat":
                title = instruction.strip().replace("\n", " ")[:60] or "New chat"
                connection.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (title, now, conversation_id),
                )



        task_context = TaskContext(goal=instruction)



        seed_world_state(task_context, self.host_environment.get())
        bind_operation_stop(task_context, context.stop_requested)

        def publish(step: Mapping[str, Any]) -> None:
            snapshot = task_context.snapshot()
            context.update(
                phase=f"{snapshot['state_label']}: {step['action']}",
                details={
                    **relationship,
                    "agent_task": {
                        **relationship["agent_task"],
                        **snapshot,
                        "step": step["step"],
                        "action": step["action"],
                        "reason": step.get("reason") or "",
                        "status": step.get("status"),
                        "route": step.get("route"),
                    },
                },
            )

        context.update(
            phase="Queued",
            details={**relationship, "agent_task": {**relationship["agent_task"], **task_context.snapshot()}},
        )
        loop = AgentLoop(
            broker=self.automation,
            generate=lambda messages: self._agent_generate(
                messages,
                context=context,
                generation_settings=generation_settings,
            ),
            capabilities=capabilities,
            authority_mode=str(generation_settings["computer_authority_mode"]),
            memory=self.memory,
            on_step=publish,
            should_stop=context.stop_requested,



            describe_screenshot=(
                (
                    lambda path: self._describe_screenshot(
                        path, conversation_id=conversation_id
                    )
                )
                if requires_sight(capabilities)
                else None
            ),
            task=task_context,
        )
        outcome = loop.run(instruction)

        assistant_id = new_id()
        finished = utc_now()
        details = {
            **relationship,
            "cancellation_state": "not_requested",
            "generation_state": (
                "cancelled" if outcome["state"] == "cancelled" else "completed"
            ),
            "finish_reason": f"agent_{outcome['state']}",
            "agent_task": {
                **outcome["task"],
                "step_count": outcome["step_count"],
                "steps": outcome["steps"],
                "capabilities": outcome["capabilities"],
                "authority_mode": outcome["authority_mode"],
                "model_turns": outcome["model_turns"],
                "tiers_used": outcome["tiers_used"],
                "metrics": outcome["metrics"],
            },
        }
        with self.database.transaction() as connection:
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
                    str(outcome["answer"]),
                    next_sequence + 1,
                    target["kind"],
                    target["id"],
                    target["profile_id"],
                    target["source_sha256"],
                    json_text(details),
                    finished,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (finished, conversation_id),
            )
        result = {
            **relationship,
            "assistant_message_id": assistant_id,
            "finish_reason": f"agent_{outcome['state']}",
            "agent_task": details["agent_task"],
        }
        context.update(phase="Saving response", details=result)
        return result

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
        seed = int(settings.get("seed", 42))

        if (width, height, steps) != (512, 512, 8):
            raise ValueError("The validated Steak Gen profile is 512x512 with 8 steps")
        if not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("Image seed must fit an unsigned 64-bit integer")
        operation = self.operations.submit(
            "chat_image_generation",
            lambda context: self._generate_confirmed_image(
                conversation_id=conversation_id,
                assistant_message_id=checked_message_id,
                proposal_id=checked_proposal_id,
                prompt=prompt,
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
        proposal["state"] = "running"
        proposal["operation_id"] = operation["id"]
        proposal["execution_allowed"] = True
        details["host_action_proposal"] = proposal
        self.database.execute(
            "UPDATE messages SET technical_details_json = ? WHERE id = ?",
            (json_text(details), checked_message_id),
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
            proposal["state"] = "running"
            proposal["operation_id"] = operation["id"]
            proposal["execution_allowed"] = True
            details["host_action_proposal"] = proposal
            self.database.execute(
                "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                (json_text(details), assistant_message_id),
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
            proposal["state"] = "running"
            proposal["operation_id"] = operation["id"]
            proposal["execution_allowed"] = True
            details["host_action_proposal"] = proposal
            self.database.execute(
                "UPDATE messages SET technical_details_json = ? WHERE id = ?",
                (json_text(details), assistant_message_id),
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
        action_intent = detect_host_action_intent(search_query)
        relationship["host_action"] = {
            "intent": action_intent,
            "state": "planning" if action_intent else "not_requested",
            "execution_requested": False,
            "execution_performed": False,
        }
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
            "state": "not_needed",
            "query": None,
            "result_count": 0,
        }
        if search_enabled and action_intent is None and should_search_web(search_query):
            context.update(phase="Searching the web", details=relationship)
            relationship["web_search"] = {
                "enabled": True,
                "state": "searching",
                "query": search_query[:500],
                "result_count": 0,
            }
            try:
                results = self.web_search.search(search_query, limit=6)
            except Exception as error:
                relationship["web_search"] = {
                    "enabled": True,
                    "state": "failed",
                    "query": search_query[:500],
                    "result_count": 0,
                    "error": type(error).__name__,
                }
            else:
                relationship["web_search"] = {
                    "enabled": True,
                    "state": "completed",
                    "query": search_query[:500],
                    "result_count": len(results),
                    "sources": [item["url"] for item in results],
                }
                if results:
                    insertion = 0
                    while insertion < len(history) and history[insertion].get("role") == "system":
                        insertion += 1
                    history.insert(
                        insertion,
                        {
                            "role": "system",
                            "content": web_results_prompt(search_query, results),
                        },
                    )
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
        }
        if previous_response_id:
            details.update(
                {
                    "retry_user_message_id": user_message_id,
                    "retry_of_assistant_message_id": previous_response_id,
                }
            )
        assistant_content = response.text
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
        assistant_id = new_id()
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
        result = {
            **relationship,
            "assistant_message_id": assistant_id,
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
