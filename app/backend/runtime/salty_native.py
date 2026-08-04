"""Salty Steak Native Desktop AI Platform — Salty Native Inference Engine.

The application owns this boundary and loads its private, audited Salty Tensor
Engine execution library directly.  It does not require a command-line model
process, a localhost inference server, or a separately installed model
application.
"""

from __future__ import annotations

import codecs
import ctypes
import hashlib
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SALTY_TENSOR_TYPE_Q8_0 = 8
SALTY_FLASH_ATTN_ENABLED = 1
SALTY_DEFAULT_SEED = 0xFFFFFFFF
NATIVE_REASONING_ALIASES = {
    "instant": "instant",
    "off": "instant",
    "cooking": "cooking",
    "auto": "cooking",
    "deep": "cooking",
}


class SaltyNativeRuntimeError(RuntimeError):
    """Raised when the private native execution engine fails closed."""


def normalise_native_reasoning_mode(value: str) -> str:
    checked = str(value).strip().casefold()
    try:
        return NATIVE_REASONING_ALIASES[checked]
    except KeyError as exc:
        raise ValueError(
            "reasoning_mode must be instant or cooking "
            "(legacy aliases: off, auto, deep)"
        ) from exc


def _prompt_batch_ranges(token_count: int, batch_size: int) -> tuple[tuple[int, int], ...]:
    """Return contiguous prompt ranges that never exceed the native batch cap."""

    if token_count < 1:
        raise ValueError("token_count must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    return tuple(
        (start, min(start + batch_size, token_count))
        for start in range(0, token_count, batch_size)
    )


def _common_prefix_length(previous: Sequence[int], current: Sequence[int]) -> int:
    """Return how many leading tokens two prompts share.

    The result is the reusable KV span. A continuation of the same conversation
    shares every token up to the point where the new turn was appended, so the
    resident cache covers all of it and only the tail needs evaluating.
    """

    limit = min(len(previous), len(current))
    index = 0
    while index < limit and previous[index] == current[index]:
        index += 1
    return index


def _apply_reasoning_to_rendered_prompt(prompt: bytes, mode: str) -> tuple[bytes, str]:
    """Apply the two-mode contract after the embedded template has rendered.

    The Base Steak generation prompt ends with an open ``<think>`` block.
    Cooking preserves that exact model default. Instant closes the still-empty
    block at the rendered-prompt boundary, matching the embedded template's
    non-thinking branch without adding a user-message instruction.
    """

    canonical = normalise_native_reasoning_mode(mode)
    if canonical == "cooking":
        return prompt, "embedded_template_open_think"

    closed_suffixes = (
        b"<think>\n\n</think>\n\n",
        b"<think>\r\n\r\n</think>\r\n\r\n",
    )
    if prompt.endswith(closed_suffixes):
        return prompt, "embedded_template_empty_think_closed"
    if prompt.endswith(b"<think>\n"):
        return prompt + b"\n</think>\n\n", "embedded_template_empty_think_closed"
    if prompt.endswith(b"<think>\r\n"):
        return (
            prompt + b"\r\n</think>\r\n\r\n",
            "embedded_template_empty_think_closed",
        )





    if prompt.endswith(b"<|im_start|>assistant\n"):
        return (
            prompt + b"<think>\n\n</think>\n\n",
            "embedded_template_empty_think_closed",
        )
    if prompt.endswith(b"<|im_start|>assistant\r\n"):
        return (
            prompt + b"<think>\r\n\r\n</think>\r\n\r\n",
            "embedded_template_empty_think_closed",
        )
    raise SaltyNativeRuntimeError(
        "Instant mode requires the embedded template to end with an open think block"
    )


class _Utf8TokenDecoder:
    """Decode token byte pieces without corrupting split UTF-8 sequences."""

    def __init__(self) -> None:


        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def feed(self, piece: bytes) -> str:
        return self._decoder.decode(piece, final=False)

    def finish(self) -> str:
        return self._decoder.decode(b"", final=True)


class _StopSequenceMatcher:
    """Find stop sequences across token-piece boundaries in linear time."""

    def __init__(self, sequences: Sequence[str]) -> None:
        self._sequences = tuple(str(value) for value in sequences)
        self._tail = ""
        self._processed_characters = 0
        self._retained_characters = max(
            (len(value) - 1 for value in self._sequences),
            default=0,
        )

    def feed(self, piece: str) -> int | None:
        window = self._tail + piece
        window_start = self._processed_characters - len(self._tail)
        for value in self._sequences:
            position = window.find(value)
            if position >= 0:
                return window_start + position
        self._processed_characters += len(piece)
        self._tail = (
            window[-self._retained_characters :]
            if self._retained_characters
            else ""
        )
        return None


class _ModelTensorBufferOverride(ctypes.Structure):
    _fields_ = [("pattern", ctypes.c_char_p), ("buffer_type", ctypes.c_void_p)]


class _ModelParams(ctypes.Structure):
    _fields_ = [
        ("devices", ctypes.POINTER(ctypes.c_void_p)),
        ("tensor_buft_overrides", ctypes.POINTER(_ModelTensorBufferOverride)),
        ("n_gpu_layers", ctypes.c_int32),
        ("split_mode", ctypes.c_int),
        ("load_mode", ctypes.c_int),
        ("main_gpu", ctypes.c_int32),
        ("tensor_split", ctypes.POINTER(ctypes.c_float)),
        ("progress_callback", ctypes.c_void_p),
        ("progress_callback_user_data", ctypes.c_void_p),
        ("kv_overrides", ctypes.c_void_p),
        ("vocab_only", ctypes.c_bool),
        ("check_tensors", ctypes.c_bool),
        ("use_extra_bufts", ctypes.c_bool),
        ("no_host", ctypes.c_bool),
        ("no_alloc", ctypes.c_bool),
        ("load_mtp", ctypes.c_bool),
    ]


class _ContextParams(ctypes.Structure):
    _fields_ = [
        ("n_ctx", ctypes.c_uint32),
        ("n_batch", ctypes.c_uint32),
        ("n_ubatch", ctypes.c_uint32),
        ("n_seq_max", ctypes.c_uint32),
        ("n_rs_seq", ctypes.c_uint32),
        ("n_outputs_max", ctypes.c_uint32),
        ("n_threads", ctypes.c_int32),
        ("n_threads_batch", ctypes.c_int32),
        ("ctx_type", ctypes.c_int),
        ("rope_scaling_type", ctypes.c_int),
        ("pooling_type", ctypes.c_int),
        ("attention_type", ctypes.c_int),
        ("flash_attn_type", ctypes.c_int),
        ("rope_freq_base", ctypes.c_float),
        ("rope_freq_scale", ctypes.c_float),
        ("yarn_ext_factor", ctypes.c_float),
        ("yarn_attn_factor", ctypes.c_float),
        ("yarn_beta_fast", ctypes.c_float),
        ("yarn_beta_slow", ctypes.c_float),
        ("yarn_orig_ctx", ctypes.c_uint32),
        ("defrag_thold", ctypes.c_float),
        ("cb_eval", ctypes.c_void_p),
        ("cb_eval_user_data", ctypes.c_void_p),
        ("type_k", ctypes.c_int),
        ("type_v", ctypes.c_int),
        ("abort_callback", ctypes.c_void_p),
        ("abort_callback_data", ctypes.c_void_p),
        ("embeddings", ctypes.c_bool),
        ("offload_kqv", ctypes.c_bool),
        ("no_perf", ctypes.c_bool),
        ("op_offload", ctypes.c_bool),
        ("swa_full", ctypes.c_bool),
        ("kv_unified", ctypes.c_bool),
        ("samplers", ctypes.c_void_p),
        ("n_samplers", ctypes.c_size_t),
        ("ctx_other", ctypes.c_void_p),
    ]


class _SamplerChainParams(ctypes.Structure):
    _fields_ = [("no_perf", ctypes.c_bool)]


class _ThreadpoolParams(ctypes.Structure):
    _fields_ = [
        ("cpu_mask", ctypes.c_bool * 512),
        ("n_threads", ctypes.c_int),
        ("priority", ctypes.c_int),
        ("poll", ctypes.c_uint32),
        ("strict_cpu", ctypes.c_bool),
        ("paused", ctypes.c_bool),
    ]


class _Batch(ctypes.Structure):
    _fields_ = [
        ("n_tokens", ctypes.c_int32),
        ("token", ctypes.POINTER(ctypes.c_int32)),
        ("embd", ctypes.POINTER(ctypes.c_float)),
        ("pos", ctypes.POINTER(ctypes.c_int32)),
        ("n_seq_id", ctypes.POINTER(ctypes.c_int32)),
        ("seq_id", ctypes.POINTER(ctypes.POINTER(ctypes.c_int32))),
        ("logits", ctypes.POINTER(ctypes.c_int8)),
    ]


class _ChatMessage(ctypes.Structure):
    _fields_ = [("role", ctypes.c_char_p), ("content", ctypes.c_char_p)]


@dataclass(frozen=True)
class SaltyNativeAdapterSpec:
    adapter_id: str
    path: str
    sha256: str
    scale: float = 1.0
    activation: str = "always"

    def __post_init__(self) -> None:
        if not self.adapter_id.strip():
            raise ValueError("Native adapter_id cannot be empty")
        if not self.path.strip():
            raise ValueError("Native adapter path cannot be empty")
        digest = self.sha256.casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("Native adapter sha256 must be 64 hexadecimal characters")
        if not 0 < float(self.scale) <= 16:
            raise ValueError("Native adapter scale must be between 0 and 16")
        if self.activation not in {"always", "identity_intent", "routing_intent"}:
            raise ValueError(
                "Native adapter activation must be always, identity_intent, or routing_intent"
            )

    @classmethod
    def from_value(
        cls,
        value: "SaltyNativeAdapterSpec | Mapping[str, Any]",
    ) -> "SaltyNativeAdapterSpec":
        if isinstance(value, cls):
            return value
        return cls(
            adapter_id=str(value.get("adapter_id") or value.get("id") or ""),
            path=str(value.get("path") or ""),
            sha256=str(value.get("sha256") or "").casefold(),
            scale=float(value.get("scale", 1.0)),
            activation=str(value.get("activation") or "always").strip().casefold(),
        )


@dataclass(frozen=True)
class SaltyNativeProfile:
    profile_id: str = "base_steak_2_32k_adaptive_262k"
    gpu_layers: int = 99



    context_limit: int = 262_144




    resident_context_limit: int | None = None
    batch_size: int = 256
    micro_batch_size: int = 128
    threads: int = 12
    thread_poll: int = 100
    kv_precision: str = "q8_0"
    cuda_output_projection: bool = False




    host_kv_above_context: int | None = None

    def __post_init__(self) -> None:
        if not str(self.profile_id).strip():
            raise ValueError("Native profile_id cannot be empty")
        if self.gpu_layers < 0:
            raise ValueError("Native gpu_layers cannot be negative")
        if not 256 <= self.context_limit <= 262_144:
            raise ValueError("Native context_limit must be between 256 and 262144")
        if self.resident_context_limit is not None and not (
            256 <= self.resident_context_limit <= self.context_limit
        ):
            raise ValueError(
                "Native resident_context_limit must be between 256 and context_limit"
            )
        if not 1 <= self.batch_size <= self.context_limit:
            raise ValueError("Native batch_size must fit the configured context")
        if not 1 <= self.micro_batch_size <= self.batch_size:
            raise ValueError("Native micro_batch_size must fit the batch")
        if not 1 <= self.threads <= 128:
            raise ValueError("Native threads must be between 1 and 128")
        if not 0 <= self.thread_poll <= 100:
            raise ValueError("Native thread_poll must be between 0 and 100")
        if self.kv_precision != "q8_0":
            raise ValueError("This native build only supports the q8_0 KV profile")
        if self.host_kv_above_context is not None and not (
            self.initial_context_limit
            <= self.host_kv_above_context
            <= self.context_limit
        ):
            raise ValueError(
                "Native host_kv_above_context must be between the resident and selectable limits"
            )

    @classmethod
    def from_manifest(cls, value: Any) -> "SaltyNativeProfile":
        """Resolve an optional ``runtime.profile`` without changing legacy defaults."""

        if value is None or value == {}:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("runtime.profile must be an object")
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(
                "runtime.profile contains unsupported fields: " + ", ".join(unknown)
            )
        return cls(**value)

    @property
    def initial_context_limit(self) -> int:
        return int(self.resident_context_limit or min(32_768, self.context_limit))

    @property
    def host_kv_threshold(self) -> int:
        return int(self.host_kv_above_context or min(65_536, self.context_limit))


@dataclass
class SaltyNativeGeneration:
    text: str
    token_ids: list[int]
    omitted_turns: int
    cancelled: bool
    finish_reason: str
    technical_details: dict[str, Any]


class _NativeApi:
    def __init__(self, library_directory: Path) -> None:
        self.library_directory = library_directory.resolve()
        if not self.library_directory.is_dir():
            raise SaltyNativeRuntimeError(
                f"Native runtime directory does not exist: {self.library_directory}"
            )
        self._dll_cookie = (
            os.add_dll_directory(str(self.library_directory))
            if hasattr(os, "add_dll_directory")
            else None
        )
        try:



            self._cuda_dependencies = [
                ctypes.CDLL(str(self.library_directory / filename))
                for filename in (
                    "cudart64_12.dll",
                    "cublasLt64_12.dll",
                    "cublas64_12.dll",
                )
            ]
            self.tensor_base = ctypes.CDLL(
                str(self.library_directory / "ggml-base.dll")
            )
            self.tensor_cpu = ctypes.CDLL(
                str(self.library_directory / "ggml-cpu-haswell.dll")
            )
            self.tensor = ctypes.CDLL(str(self.library_directory / "ggml.dll"))
            self.native = ctypes.CDLL(str(self.library_directory / "llama.dll"))
        except OSError as error:
            raise SaltyNativeRuntimeError(
                f"Private native runtime could not be loaded: {error}"
            ) from error
        self._bind()

    def _bind(self) -> None:
        self.tensor.ggml_backend_load_all.argtypes = []
        self.tensor.ggml_backend_load_all.restype = None
        self.tensor.ggml_backend_load_all_from_path.argtypes = [ctypes.c_char_p]
        self.tensor.ggml_backend_load_all_from_path.restype = None
        self.tensor.ggml_backend_dev_by_name.argtypes = [ctypes.c_char_p]
        self.tensor.ggml_backend_dev_by_name.restype = ctypes.c_void_p
        self.tensor_base.ggml_backend_dev_buffer_type.argtypes = [ctypes.c_void_p]
        self.tensor_base.ggml_backend_dev_buffer_type.restype = ctypes.c_void_p
        self.tensor_base.ggml_threadpool_params_default.argtypes = [ctypes.c_int]
        self.tensor_base.ggml_threadpool_params_default.restype = _ThreadpoolParams
        self.tensor_cpu.ggml_threadpool_new.argtypes = [
            ctypes.POINTER(_ThreadpoolParams)
        ]
        self.tensor_cpu.ggml_threadpool_new.restype = ctypes.c_void_p
        self.tensor_cpu.ggml_threadpool_free.argtypes = [ctypes.c_void_p]
        self.tensor_cpu.ggml_threadpool_free.restype = None

        dll = self.native
        dll.llama_backend_init.argtypes = []
        dll.llama_backend_init.restype = None
        dll.llama_backend_free.argtypes = []
        dll.llama_backend_free.restype = None
        dll.llama_model_default_params.argtypes = []
        dll.llama_model_default_params.restype = _ModelParams
        dll.llama_context_default_params.argtypes = []
        dll.llama_context_default_params.restype = _ContextParams
        dll.llama_sampler_chain_default_params.argtypes = []
        dll.llama_sampler_chain_default_params.restype = _SamplerChainParams
        dll.llama_model_load_from_file.argtypes = [ctypes.c_char_p, _ModelParams]
        dll.llama_model_load_from_file.restype = ctypes.c_void_p
        dll.llama_model_free.argtypes = [ctypes.c_void_p]
        dll.llama_model_free.restype = None
        dll.llama_model_get_vocab.argtypes = [ctypes.c_void_p]
        dll.llama_model_get_vocab.restype = ctypes.c_void_p
        dll.llama_model_n_embd.argtypes = [ctypes.c_void_p]
        dll.llama_model_n_embd.restype = ctypes.c_int32
        dll.llama_model_chat_template.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.llama_model_chat_template.restype = ctypes.c_char_p
        dll.llama_init_from_model.argtypes = [ctypes.c_void_p, _ContextParams]
        dll.llama_init_from_model.restype = ctypes.c_void_p
        dll.llama_free.argtypes = [ctypes.c_void_p]
        dll.llama_free.restype = None
        dll.llama_attach_threadpool.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        dll.llama_attach_threadpool.restype = None
        dll.llama_detach_threadpool.argtypes = [ctypes.c_void_p]
        dll.llama_detach_threadpool.restype = None
        dll.llama_get_memory.argtypes = [ctypes.c_void_p]
        dll.llama_get_memory.restype = ctypes.c_void_p
        dll.llama_memory_clear.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        dll.llama_memory_clear.restype = None



        dll.llama_memory_seq_rm.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
        ]
        dll.llama_memory_seq_rm.restype = ctypes.c_bool
        dll.llama_tokenize.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
            ctypes.c_bool,
            ctypes.c_bool,
        ]
        dll.llama_tokenize.restype = ctypes.c_int32
        dll.llama_token_to_piece.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_char_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_bool,
        ]
        dll.llama_token_to_piece.restype = ctypes.c_int32
        dll.llama_chat_apply_template.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(_ChatMessage),
            ctypes.c_size_t,
            ctypes.c_bool,
            ctypes.c_char_p,
            ctypes.c_int32,
        ]
        dll.llama_chat_apply_template.restype = ctypes.c_int32
        dll.llama_batch_get_one.argtypes = [
            ctypes.POINTER(ctypes.c_int32),
            ctypes.c_int32,
        ]
        dll.llama_batch_get_one.restype = _Batch
        dll.llama_decode.argtypes = [ctypes.c_void_p, _Batch]
        dll.llama_decode.restype = ctypes.c_int32
        dll.llama_set_embeddings.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        dll.llama_set_embeddings.restype = None
        dll.llama_get_embeddings_ith.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        dll.llama_get_embeddings_ith.restype = ctypes.POINTER(ctypes.c_float)
        dll.llama_get_logits_ith.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        dll.llama_get_logits_ith.restype = ctypes.POINTER(ctypes.c_float)
        dll.llama_adapter_lora_init.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        dll.llama_adapter_lora_init.restype = ctypes.c_void_p
        dll.llama_adapter_lora_free.argtypes = [ctypes.c_void_p]
        dll.llama_adapter_lora_free.restype = None
        dll.llama_set_adapters_lora.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_float),
        ]
        dll.llama_set_adapters_lora.restype = ctypes.c_int32
        dll.llama_vocab_is_eog.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        dll.llama_vocab_is_eog.restype = ctypes.c_bool
        dll.llama_vocab_n_tokens.argtypes = [ctypes.c_void_p]
        dll.llama_vocab_n_tokens.restype = ctypes.c_int32
        dll.llama_sampler_chain_init.argtypes = [_SamplerChainParams]
        dll.llama_sampler_chain_init.restype = ctypes.c_void_p
        dll.llama_sampler_chain_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        dll.llama_sampler_chain_add.restype = None
        dll.llama_sampler_init_greedy.argtypes = []
        dll.llama_sampler_init_greedy.restype = ctypes.c_void_p
        dll.llama_sampler_init_dist.argtypes = [ctypes.c_uint32]
        dll.llama_sampler_init_dist.restype = ctypes.c_void_p
        dll.llama_sampler_init_top_k.argtypes = [ctypes.c_int32]
        dll.llama_sampler_init_top_k.restype = ctypes.c_void_p
        dll.llama_sampler_init_top_p.argtypes = [ctypes.c_float, ctypes.c_size_t]
        dll.llama_sampler_init_top_p.restype = ctypes.c_void_p
        dll.llama_sampler_init_temp.argtypes = [ctypes.c_float]
        dll.llama_sampler_init_temp.restype = ctypes.c_void_p
        dll.llama_sampler_init_penalties.argtypes = [
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
        ]
        dll.llama_sampler_init_penalties.restype = ctypes.c_void_p
        dll.llama_sampler_sample.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32]
        dll.llama_sampler_sample.restype = ctypes.c_int32
        dll.llama_sampler_free.argtypes = [ctypes.c_void_p]
        dll.llama_sampler_free.restype = None


class SaltyNativeRuntime:
    """Persistent, direct-library runtime for an exact Salty Native model bundle."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        library_directory: str | Path,
        profile: SaltyNativeProfile | None = None,
        source_sha256: str,
        adapters: Sequence[
            SaltyNativeAdapterSpec | Mapping[str, Any]
        ] = (),
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.library_directory = Path(library_directory).resolve()
        self.profile = profile or SaltyNativeProfile()
        self.source_sha256 = source_sha256
        self.adapters = tuple(
            SaltyNativeAdapterSpec.from_value(value) for value in adapters
        )
        self._api: _NativeApi | None = None
        self._model: int | None = None
        self._context: int | None = None
        self._vocab: int | None = None
        self._threadpool: int | None = None
        self._adapter_handles: list[int] = []
        self._verified_adapter_sha256: dict[str, str] = {}
        self._active_adapter_ids: tuple[str, ...] = ()
        self._load_seconds: float | None = None
        self._verified_source_sha256: str | None = None
        self._runtime_id: str | None = None
        self._allocated_context_limit: int | None = None
        self._lock = threading.RLock()
        self._decode_token_buffer: Any = None
        self._decode_token_batch: _Batch | None = None



        self._resident_tokens: list[int] = []

    @property
    def loaded(self) -> bool:
        return bool(self._api and self._model and self._context and self._vocab)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_adapters(self, api: _NativeApi, model: int) -> list[int]:
        handles: list[int] = []
        verified: dict[str, str] = {}
        try:
            for spec in self.adapters:
                path = Path(spec.path).resolve()
                if not path.is_file():
                    raise SaltyNativeRuntimeError(
                        f"Native adapter file is missing: {path}"
                    )
                actual = self._file_sha256(path)
                if actual != spec.sha256.casefold():
                    raise SaltyNativeRuntimeError(
                        f"Native adapter checksum differs from {spec.adapter_id} identity"
                    )
                handle = api.native.llama_adapter_lora_init(
                    model,
                    os.fsencode(path),
                )
                if not handle:
                    raise SaltyNativeRuntimeError(
                        f"Native engine could not load adapter: {spec.adapter_id}"
                    )
                handles.append(handle)
                verified[spec.adapter_id] = actual
        except BaseException:
            for handle in reversed(handles):
                api.native.llama_adapter_lora_free(handle)
            raise
        self._verified_adapter_sha256 = verified
        return handles

    def _default_adapter_ids(self) -> tuple[str, ...]:
        return tuple(
            spec.adapter_id
            for spec in getattr(self, "adapters", ())
            if spec.activation == "always"
        )

    def conditional_adapter_ids(self, activation: str) -> tuple[str, ...]:
        """Return hash-bound adapters registered for one learned controller lane."""

        checked = str(activation).strip().casefold()
        return tuple(
            spec.adapter_id
            for spec in getattr(self, "adapters", ())
            if spec.activation == checked
        )

    def _normalise_enabled_adapter_ids(
        self,
        enabled_adapter_ids: Sequence[str] | None,
    ) -> tuple[str, ...]:
        requested = set(self._default_adapter_ids())
        if enabled_adapter_ids is not None:
            requested.update(str(value) for value in enabled_adapter_ids)
        known = {spec.adapter_id for spec in getattr(self, "adapters", ())}
        unknown = sorted(requested - known)
        if unknown:
            raise ValueError(f"Unknown native adapter ids: {', '.join(unknown)}")
        return tuple(
            spec.adapter_id
            for spec in getattr(self, "adapters", ())
            if spec.adapter_id in requested
        )

    def _apply_adapters(
        self,
        api: _NativeApi,
        context: int,
        enabled_adapter_ids: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        adapter_handles = list(getattr(self, "_adapter_handles", []))
        if not adapter_handles:
            return self._normalise_enabled_adapter_ids(enabled_adapter_ids)
        adapter_specs = tuple(getattr(self, "adapters", ()))
        active_ids = self._normalise_enabled_adapter_ids(enabled_adapter_ids)
        active = [
            (handle, spec)
            for handle, spec in zip(adapter_handles, adapter_specs, strict=True)
            if spec.adapter_id in active_ids
        ]
        handles = (
            (ctypes.c_void_p * len(active))(*(handle for handle, _spec in active))
            if active
            else None
        )
        scales = (
            (ctypes.c_float * len(active))(
                *(float(spec.scale) for _handle, spec in active)
            )
            if active
            else None
        )
        status = int(
            api.native.llama_set_adapters_lora(
                context,
                handles,
                len(active),
                scales,
            )
        )
        if status != 0:
            raise SaltyNativeRuntimeError(
                f"Native engine rejected the registered adapters: {status}"
            )
        return active_ids

    def _set_adapter_activation(
        self,
        enabled_adapter_ids: Sequence[str] | None,
    ) -> bool:
        requested = self._normalise_enabled_adapter_ids(enabled_adapter_ids)
        current = tuple(getattr(self, "_active_adapter_ids", ()))
        if requested == current:
            return False
        if not self.loaded or not self._api or not self._context:
            raise SaltyNativeRuntimeError("Salty Steak is not loaded")
        applied = self._apply_adapters(
            self._api,
            self._context,
            requested,
        )
        memory = self._api.native.llama_get_memory(self._context)
        self._api.native.llama_memory_clear(memory, True)
        self._resident_tokens = []
        self._active_adapter_ids = applied
        return True

    def _create_context(
        self,
        api: _NativeApi,
        model: int,
        context_limit: int,
    ) -> int:
        context_params = api.native.llama_context_default_params()
        context_params.n_ctx = int(context_limit)
        context_params.n_batch = min(self.profile.batch_size, int(context_limit))
        context_params.n_ubatch = min(
            self.profile.micro_batch_size, context_params.n_batch
        )
        context_params.n_threads = self.profile.threads
        context_params.n_threads_batch = self.profile.threads
        context_params.flash_attn_type = SALTY_FLASH_ATTN_ENABLED
        context_params.offload_kqv = bool(
            int(context_limit) <= self.profile.host_kv_threshold
        )
        if self.profile.kv_precision == "q8_0":
            context_params.type_k = SALTY_TENSOR_TYPE_Q8_0
            context_params.type_v = SALTY_TENSOR_TYPE_Q8_0
        context = api.native.llama_init_from_model(model, context_params)
        if not context:
            raise SaltyNativeRuntimeError(
                f"Native engine could not allocate the {context_limit}-token model context"
            )
        try:
            self._apply_adapters(
                api,
                context,
                getattr(self, "_active_adapter_ids", self._default_adapter_ids()),
            )
        except BaseException:
            api.native.llama_free(context)
            raise
        return context

    def _ensure_context_allocation(self, requested_limit: int) -> bool:
        """Resize only the KV/runtime context while preserving loaded weights."""

        assert self._api and self._model and self._context and self._threadpool
        resident = self.profile.initial_context_limit
        target = resident if requested_limit <= resident else int(requested_limit)
        if target == self._allocated_context_limit:
            return False
        if not 256 <= target <= self.profile.context_limit:
            raise ValueError("Requested native context is outside the configured profile")

        api = self._api
        previous_context = self._context
        previous_limit = int(self._allocated_context_limit or resident)
        api.native.llama_detach_threadpool(previous_context)
        api.native.llama_free(previous_context)
        self._context = None
        self._decode_token_buffer = None
        self._decode_token_batch = None


        self._resident_tokens = []
        try:
            replacement = self._create_context(api, self._model, target)
        except BaseException as resize_error:
            try:
                restored = self._create_context(api, self._model, previous_limit)
                api.native.llama_attach_threadpool(restored, self._threadpool, None)
                self._context = restored
                self._allocated_context_limit = previous_limit
            except BaseException as restore_error:
                self._allocated_context_limit = None
                raise SaltyNativeRuntimeError(
                    "Native context resize failed and the previous context could not be restored"
                ) from restore_error
            raise SaltyNativeRuntimeError(
                f"Native context could not be resized to {target} tokens"
            ) from resize_error
        api.native.llama_attach_threadpool(replacement, self._threadpool, None)
        self._context = replacement
        self._allocated_context_limit = target
        return True

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self.loaded:
                return self.describe()
            if not self.model_path.is_file():
                raise SaltyNativeRuntimeError(f"Model file is missing: {self.model_path}")
            source_hash = hashlib.sha256()
            with self.model_path.open("rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    source_hash.update(chunk)
            actual_source_sha256 = source_hash.hexdigest()
            if actual_source_sha256 != self.source_sha256:
                raise SaltyNativeRuntimeError(
                    "The model checksum differs from the registered model identity"
                )
            self._verified_source_sha256 = actual_source_sha256
            api = _NativeApi(self.library_directory)
            api.tensor.ggml_backend_load_all_from_path(
                os.fsencode(self.library_directory)
            )
            api.native.llama_backend_init()

            params = api.native.llama_model_default_params()
            params.n_gpu_layers = self.profile.gpu_layers
            overrides = None
            if self.profile.cuda_output_projection:
                device = api.tensor.ggml_backend_dev_by_name(b"CUDA0")
                buffer_type = (
                    api.tensor_base.ggml_backend_dev_buffer_type(device)
                    if device
                    else None
                )
                if not buffer_type:
                    api.native.llama_backend_free()
                    raise SaltyNativeRuntimeError("CUDA0 buffer is unavailable for output placement")
                overrides = (_ModelTensorBufferOverride * 2)()
                overrides[0].pattern = b"output.weight"
                overrides[0].buffer_type = buffer_type
                overrides[1].pattern = None
                overrides[1].buffer_type = None
                params.tensor_buft_overrides = ctypes.cast(
                    overrides, ctypes.POINTER(_ModelTensorBufferOverride)
                )

            started = time.perf_counter()
            model = api.native.llama_model_load_from_file(
                os.fsencode(self.model_path), params
            )
            if not model:
                api.native.llama_backend_free()
                raise SaltyNativeRuntimeError("Native engine could not load the model")
            self._adapter_handles = self._load_adapters(api, model)
            self._active_adapter_ids = self._default_adapter_ids()
            try:
                context = self._create_context(
                    api,
                    model,
                    self.profile.initial_context_limit,
                )
            except BaseException:
                for handle in reversed(self._adapter_handles):
                    api.native.llama_adapter_lora_free(handle)
                self._adapter_handles = []
                self._verified_adapter_sha256 = {}
                self._active_adapter_ids = ()
                api.native.llama_model_free(model)
                api.native.llama_backend_free()
                raise
            vocab = api.native.llama_model_get_vocab(model)
            if not vocab:
                api.native.llama_free(context)
                for handle in reversed(self._adapter_handles):
                    api.native.llama_adapter_lora_free(handle)
                self._adapter_handles = []
                self._verified_adapter_sha256 = {}
                self._active_adapter_ids = ()
                api.native.llama_model_free(model)
                api.native.llama_backend_free()
                raise SaltyNativeRuntimeError("Native engine did not expose the model vocabulary")
            threadpool_params = api.tensor_base.ggml_threadpool_params_default(
                self.profile.threads
            )
            threadpool_params.poll = max(0, min(100, self.profile.thread_poll))
            threadpool = api.tensor_cpu.ggml_threadpool_new(
                ctypes.byref(threadpool_params)
            )
            if not threadpool:
                api.native.llama_free(context)
                for handle in reversed(self._adapter_handles):
                    api.native.llama_adapter_lora_free(handle)
                self._adapter_handles = []
                self._verified_adapter_sha256 = {}
                self._active_adapter_ids = ()
                api.native.llama_model_free(model)
                api.native.llama_backend_free()
                raise SaltyNativeRuntimeError("Native CPU threadpool allocation failed")
            api.native.llama_attach_threadpool(context, threadpool, None)

            self._api = api
            self._model = model
            self._context = context
            self._vocab = vocab
            self._threadpool = threadpool
            self._allocated_context_limit = self.profile.initial_context_limit
            self._resident_tokens = []
            self._load_seconds = time.perf_counter() - started
            self._runtime_id = str(uuid.uuid4())
            return self.describe()

    def unload(self) -> None:
        with self._lock:
            api, context, model = self._api, self._context, self._model
            threadpool = self._threadpool
            adapter_handles = list(self._adapter_handles)
            self._context = None
            self._model = None
            self._vocab = None
            self._api = None
            self._threadpool = None
            self._adapter_handles = []
            self._verified_adapter_sha256 = {}
            self._active_adapter_ids = ()
            self._runtime_id = None
            self._allocated_context_limit = None
            self._decode_token_buffer = None
            self._decode_token_batch = None
            self._resident_tokens = []
            if api and context:
                api.native.llama_detach_threadpool(context)
                api.native.llama_free(context)
            if api and threadpool:
                api.tensor_cpu.ggml_threadpool_free(threadpool)
            if api:
                for handle in reversed(adapter_handles):
                    api.native.llama_adapter_lora_free(handle)
            if api and model:
                api.native.llama_model_free(model)
            if api:
                api.native.llama_backend_free()

    def describe(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "runtime_id": self._runtime_id,
            "runtime_family": "salty_native_steak20",
            "engine": "direct_in_process_native_library",
            "profile": asdict(self.profile),
            "source_sha256": self.source_sha256,
            "verified_source_sha256": self._verified_source_sha256,
            "model_path": str(self.model_path),
            "adapters": [
                {
                    **asdict(spec),
                    "path": str(Path(spec.path).resolve()),
                    "verified_sha256": getattr(
                        self,
                        "_verified_adapter_sha256",
                        {},
                    ).get(
                        spec.adapter_id
                    ),
                }
                for spec in getattr(self, "adapters", ())
            ],
            "active_adapter_ids": list(getattr(self, "_active_adapter_ids", ())),
            "architectural_context_limit": 262_144,
            "configured_context_limit": self.profile.context_limit,
            "resident_context_limit": self.profile.initial_context_limit,
            "allocated_context_limit": self._allocated_context_limit,
            "kv_cache_placement": (
                "host"
                if int(self._allocated_context_limit or self.profile.initial_context_limit)
                > self.profile.host_kv_threshold
                else "layer_device"
            ),
            "load_seconds": self._load_seconds,
            "external_service_required": False,
            "network_listener_created": False,
        }

    def _format_chat(
        self,
        messages: Sequence[dict[str, str]],
        reasoning_mode: str = "cooking",
    ) -> tuple[bytes, str]:
        assert self._api and self._model
        encoded_roles = [str(item["role"]).encode("utf-8") for item in messages]
        encoded_content = [str(item["content"]).encode("utf-8") for item in messages]
        native_messages = (_ChatMessage * len(messages))(
            *(
                _ChatMessage(role, content)
                for role, content in zip(encoded_roles, encoded_content, strict=True)
            )
        )
        template = self._api.native.llama_model_chat_template(self._model, None)
        required = self._api.native.llama_chat_apply_template(
            template, native_messages, len(messages), True, None, 0
        )
        if required < 0:
            raise SaltyNativeRuntimeError("The model chat template rejected the conversation")
        buffer = ctypes.create_string_buffer(required + 1)
        written = self._api.native.llama_chat_apply_template(
            template, native_messages, len(messages), True, buffer, len(buffer)
        )
        if written < 0 or written > required:
            raise SaltyNativeRuntimeError("The model chat template could not be rendered")
        return _apply_reasoning_to_rendered_prompt(
            bytes(buffer.raw[:written]),
            reasoning_mode,
        )

    def _tokenize_bytes(
        self,
        prompt: bytes,
        *,
        add_special: bool,
        parse_special: bool,
    ) -> list[int]:
        assert self._api and self._vocab
        count = self._api.native.llama_tokenize(
            self._vocab,
            prompt,
            len(prompt),
            None,
            0,
            add_special,
            parse_special,
        )
        needed = -count if count < 0 else count
        if needed < 1:
            raise SaltyNativeRuntimeError("The rendered prompt contains no tokens")
        buffer = (ctypes.c_int32 * needed)()
        written = self._api.native.llama_tokenize(
            self._vocab,
            prompt,
            len(prompt),
            buffer,
            needed,
            add_special,
            parse_special,
        )
        if written < 0:
            raise SaltyNativeRuntimeError("The rendered prompt could not be tokenized")
        return list(buffer[:written])

    def _tokenize(self, prompt: bytes) -> list[int]:
        return self._tokenize_bytes(
            prompt,
            add_special=True,
            parse_special=True,
        )

    def _prompt_with_budget(
        self,
        messages: Sequence[dict[str, str]],
        reserved_output_tokens: int,
        effective_context_limit: int,
        reasoning_mode: str,
    ) -> tuple[list[int], int, str]:
        working = [dict(message) for message in messages]
        omitted = 0
        while True:
            rendered, reasoning_contract = self._format_chat(
                working,
                reasoning_mode,
            )
            tokens = self._tokenize(rendered)
            if len(tokens) + reserved_output_tokens <= effective_context_limit:
                return tokens, omitted, reasoning_contract
            removable = next(
                (
                    index
                    for index, message in enumerate(working[:-1])
                    if message.get("role") == "user"
                ),
                None,
            )
            if removable is None:
                raise ValueError("The latest message exceeds the selected context window")
            del working[removable]
            if removable < len(working) and working[removable].get("role") == "assistant":
                del working[removable]
            omitted += 1

    def _sampler(
        self,
        *,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        seed: int | None,
    ) -> int:
        assert self._api and self._vocab
        dll = self._api.native
        sampler = dll.llama_sampler_chain_init(dll.llama_sampler_chain_default_params())
        if not sampler:
            raise SaltyNativeRuntimeError("Native sampler allocation failed")
        if repetition_penalty != 1.0:
            dll.llama_sampler_chain_add(
                sampler,
                dll.llama_sampler_init_penalties(
                    dll.llama_vocab_n_tokens(self._vocab),
                    64,
                    float(repetition_penalty),
                    0.0,
                    0.0,
                ),
            )
        if temperature <= 0:
            dll.llama_sampler_chain_add(sampler, dll.llama_sampler_init_greedy())
        else:
            dll.llama_sampler_chain_add(sampler, dll.llama_sampler_init_top_k(top_k))
            dll.llama_sampler_chain_add(
                sampler, dll.llama_sampler_init_top_p(float(top_p), 1)
            )
            dll.llama_sampler_chain_add(
                sampler, dll.llama_sampler_init_temp(float(temperature))
            )
            resolved_seed = (
                int(seed) & 0xFFFFFFFF
                if seed is not None and seed >= 0
                else SALTY_DEFAULT_SEED
            )
            dll.llama_sampler_chain_add(
                sampler, dll.llama_sampler_init_dist(resolved_seed)
            )
        return sampler

    def _piece_bytes(self, token: int) -> bytes:
        assert self._api and self._vocab
        buffer = ctypes.create_string_buffer(256)
        size = self._api.native.llama_token_to_piece(
            self._vocab, token, buffer, len(buffer), 0, True
        )
        if size < 0:
            buffer = ctypes.create_string_buffer(-size)
            size = self._api.native.llama_token_to_piece(
                self._vocab, token, buffer, len(buffer), 0, True
            )
        if size < 0:
            raise SaltyNativeRuntimeError("A generated token could not be decoded")
        return bytes(buffer.raw[:size])

    def generate(
        self,
        *,
        messages: Sequence[dict[str, str]],
        maximum_output_tokens: int,
        temperature: float,
        top_p: float,
        top_k: int,
        repetition_penalty: float,
        seed: int | None,
        stop_sequences: Sequence[str] = (),
        should_stop: Callable[[], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
        context_window_tokens: int | None = None,
        reserved_output_tokens: int | None = None,
        reasoning_mode: str = "cooking",
        maximum_output_mode: str = "manual",
        enabled_adapter_ids: Sequence[str] | None = None,
    ) -> SaltyNativeGeneration:
        with self._lock:
            if not self.loaded or not self._api or not self._context or not self._vocab:
                raise SaltyNativeRuntimeError("Salty Steak is not loaded")
            effective_context_limit = min(
                self.profile.context_limit,
                int(context_window_tokens or self.profile.context_limit),
            )
            reserved = max(
                maximum_output_tokens,
                int(reserved_output_tokens or maximum_output_tokens),
            )
            canonical_reasoning_mode = normalise_native_reasoning_mode(reasoning_mode)
            checked_output_mode = str(maximum_output_mode).strip().casefold()
            if checked_output_mode not in {"automatic", "manual"}:
                raise ValueError("maximum_output_mode must be automatic or manual")
            prompt_tokens, omitted_turns, reasoning_contract = self._prompt_with_budget(
                messages,
                reserved,
                effective_context_limit,
                canonical_reasoning_mode,
            )
            available_output = min(
                maximum_output_tokens,
                effective_context_limit - len(prompt_tokens),
            )
            if available_output < 1:
                raise ValueError("No output tokens remain in the selected context window")

            context_reallocated = self._ensure_context_allocation(
                effective_context_limit
            )
            adapter_activation_changed = self._set_adapter_activation(
                enabled_adapter_ids
            )
            memory = self._api.native.llama_get_memory(self._context)




            reusable_prefix = _common_prefix_length(
                self._resident_tokens,
                prompt_tokens,
            )


            reusable_prefix = max(0, min(reusable_prefix, len(prompt_tokens) - 1))
            if reusable_prefix and self._api.native.llama_memory_seq_rm(
                memory, 0, reusable_prefix, -1
            ):
                del self._resident_tokens[reusable_prefix:]
            else:
                self._api.native.llama_memory_clear(memory, True)
                self._resident_tokens = []
                reusable_prefix = 0
            pending_tokens = prompt_tokens[reusable_prefix:]
            sampler = self._sampler(
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                repetition_penalty=repetition_penalty,
                seed=seed,
            )




            if self._decode_token_buffer is None or self._decode_token_batch is None:
                self._decode_token_buffer = (ctypes.c_int32 * 1)()
                self._decode_token_batch = self._api.native.llama_batch_get_one(
                    self._decode_token_buffer, 1
                )
            generated: list[int] = []
            pieces: list[str] = []
            text_decoder = _Utf8TokenDecoder()
            stop_matcher = _StopSequenceMatcher(stop_sequences)
            cancelled = False
            finish_reason = "maximum_output"
            started = time.perf_counter()
            first_token_at: float | None = None
            prefill_finished_at: float | None = None
            prompt_ranges = _prompt_batch_ranges(
                len(pending_tokens),
                self.profile.batch_size,
            )



            prompt_token_buffer: Any = None
            batch: Any = None
            try:
                for chunk_index, (start, end) in enumerate(prompt_ranges):
                    if should_stop and should_stop():
                        cancelled = True
                        finish_reason = "cancelled"
                        break
                    chunk = pending_tokens[start:end]
                    prompt_token_buffer = (ctypes.c_int32 * len(chunk))(*chunk)
                    batch = self._api.native.llama_batch_get_one(
                        prompt_token_buffer,
                        len(chunk),
                    )
                    decode_status = self._api.native.llama_decode(self._context, batch)
                    if decode_status != 0:
                        raise SaltyNativeRuntimeError(
                            f"Native decode failed with status {decode_status}"
                        )
                    self._resident_tokens.extend(chunk)
                    if chunk_index + 1 == len(prompt_ranges):
                        prefill_finished_at = time.perf_counter()

                for output_index in range(available_output if not cancelled else 0):
                    if should_stop and should_stop():
                        cancelled = True
                        finish_reason = "cancelled"
                        break
                    if output_index:
                        decode_status = self._api.native.llama_decode(
                            self._context,
                            batch,
                        )
                        if decode_status != 0:
                            raise SaltyNativeRuntimeError(
                                f"Native decode failed with status {decode_status}"
                            )



                        self._resident_tokens.append(generated[-1])
                    token = self._api.native.llama_sampler_sample(
                        sampler, self._context, -1
                    )
                    if self._api.native.llama_vocab_is_eog(self._vocab, token):
                        finish_reason = "end_of_generation"
                        break
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    piece = text_decoder.feed(self._piece_bytes(token))
                    generated.append(token)
                    pieces.append(piece)
                    stop_index = stop_matcher.feed(piece)
                    if stop_index is not None:




                        pieces = ["".join(pieces)[:stop_index]]
                        finish_reason = "stop_sequence"
                        break
                    if on_text and piece:
                        on_text(piece)
                    self._decode_token_buffer[0] = token
                    batch = self._decode_token_batch
                trailing_text = text_decoder.finish()
                if trailing_text:
                    pieces.append(trailing_text)
                    if on_text:
                        on_text(trailing_text)
            except BaseException:



                self._resident_tokens = []
                raise
            finally:
                self._api.native.llama_sampler_free(sampler)

            finished = time.perf_counter()
            duration = finished - started
            decode_duration = finished - first_token_at if first_token_at else None
            prefill_duration = (
                prefill_finished_at - started if prefill_finished_at else None
            )
            text = "".join(pieces).strip()
            return SaltyNativeGeneration(
                text=text,
                token_ids=generated,
                omitted_turns=omitted_turns,
                cancelled=cancelled,
                finish_reason=finish_reason,
                technical_details={
                    **self.describe(),
                    "input_context_tokens": len(prompt_tokens),
                    "effective_context_limit": effective_context_limit,
                    "context_reallocated": context_reallocated,
                    "adapter_activation_changed": adapter_activation_changed,
                    "reused_prefix_tokens": reusable_prefix,
                    "prefilled_tokens": len(pending_tokens),
                    "kv_cache_reuse": (
                        "reused_resident_prefix" if reusable_prefix else "full_prefill"
                    ),
                    "reasoning_mode_effective": canonical_reasoning_mode,
                    "reasoning_prompt_contract": reasoning_contract,
                    "maximum_output_mode_effective": checked_output_mode,
                    "maximum_output_token_ceiling": available_output,
                    "generated_output_tokens": len(generated),
                    "prefill_batch_count": len(prompt_ranges),
                    "prefill_duration_seconds": (
                        round(prefill_duration, 4) if prefill_duration is not None else None
                    ),
                    "prefill_tokens_per_second": (
                        round(len(pending_tokens) / prefill_duration, 6)
                        if prefill_duration and prefill_duration > 0
                        else None
                    ),
                    "generation_duration_seconds": round(duration, 4),
                    "time_to_first_token_seconds": (
                        round(first_token_at - started, 4) if first_token_at else None
                    ),
                    "decode_tokens_per_second": (
                        round(max(0, len(generated) - 1) / decode_duration, 6)
                        if decode_duration and decode_duration > 0
                        else None
                    ),
                    "finish_reason": finish_reason,
                    "omitted_input_turns": omitted_turns,
                },
            )
