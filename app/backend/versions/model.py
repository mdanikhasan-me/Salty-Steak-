"""The Salty Steak decoder-only Transformer.

The retained checkpoints contain ordinary dense weights. Positional information
is rotary and has no learned checkpoint tensor, so the same weights can be
loaded with a larger rotary table after an explicit compatibility check.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, TypeAlias

import torch
import torch.nn.functional as functional
from torch import Tensor, nn
from torch.utils.checkpoint import checkpoint as activation_checkpoint

from ..system.device import normalise_device
from ..system.loss_contract import causal_cross_entropy

KVCache: TypeAlias = tuple[Tensor, Tensor]


class GrowableKVCache:
    """Fixed-capacity key/value store written in place during decoding.

    Rebuilding the cache with ``torch.cat`` on every step reallocates and
    copies the whole history once per layer per token, which makes generation
    quadratic in output length.  The capacity is known before decoding starts,
    so the buffers are allocated once and only a length cursor advances.

    Training keeps the original tuple representation; this is used only by the
    cached inference path.
    """

    __slots__ = ("keys", "values", "length")

    def __init__(self, keys: Tensor, values: Tensor) -> None:
        self.keys = keys
        self.values = values
        self.length = 0

    @classmethod
    def allocate(
        cls,
        *,
        batch: int,
        heads: int,
        capacity: int,
        head_size: int,
        dtype: torch.dtype,
        device: torch.device,
    ) -> "GrowableKVCache":
        shape = (batch, heads, capacity, head_size)
        return cls(
            torch.empty(shape, dtype=dtype, device=device),
            torch.empty(shape, dtype=dtype, device=device),
        )

    def append(self, key: Tensor, value: Tensor) -> KVCache:
        """Store new tokens and return a view over everything cached so far."""

        count = int(key.shape[2])
        end = self.length + count
        if end > self.keys.shape[2]:
            raise ValueError("generation exceeded the preallocated cache capacity")
        self.keys[:, :, self.length : end] = key
        self.values[:, :, self.length : end] = value
        self.length = end
        return self.keys[:, :, :end], self.values[:, :, :end]


def _cached_token_count(entry: object) -> int:
    """Return how many tokens one layer's cache already holds."""

    if entry is None:
        return 0
    if isinstance(entry, GrowableKVCache):
        return entry.length
    return int(entry[0].shape[2])


@dataclass(frozen=True)
class ModelConfig:
    model_type: str = "salty_potato"
    display_name: str = "Salty Steak"
    architecture_version: int = 2
    vocab_size: int = 16_384
    hidden_size: int = 768
    intermediate_size: int = 2_304
    num_hidden_layers: int = 12
    num_attention_heads: int = 12
    num_key_value_heads: int = 4
    max_position_embeddings: int = 8_192
    rope_theta: float = 10_000.0
    rms_norm_epsilon: float = 1e-5
    dropout: float = 0.0
    tie_word_embeddings: bool = True
    use_gradient_checkpointing: bool = True
    initializer_range: float = 0.02

    def __post_init__(self) -> None:
        if self.model_type != "salty_potato":
            raise ValueError("checkpoint is not a Salty Steak model")
        if self.hidden_size % self.num_attention_heads:
            raise ValueError("hidden size must divide evenly across attention heads")
        if self.num_attention_heads % self.num_key_value_heads:
            raise ValueError("query heads must divide evenly across key/value heads")
        if self.head_size % 2:
            raise ValueError("rotary attention requires an even head size")
        if self.max_position_embeddings < 128:
            raise ValueError("architectural context must be at least 128 tokens")

    @property
    def head_size(self) -> int:
        return self.hidden_size // self.num_attention_heads

    @classmethod
    def from_dict(cls, value: dict) -> "ModelConfig":
        accepted = {field.name for field in fields(cls)}
        payload = {key: item for key, item in value.items() if key in accepted}
        return cls(**payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "ModelConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return asdict(self)

    def to_file(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

    def with_context(self, tokens: int, revision: int | None = None) -> "ModelConfig":
        return replace(
            self,
            max_position_embeddings=int(tokens),
            architecture_version=revision or self.architecture_version,
        )

    def expected_parameter_count(self) -> int:
        embedding = self.vocab_size * self.hidden_size
        attention = (
            2 * self.hidden_size * self.hidden_size
            + 2 * self.hidden_size * self.num_key_value_heads * self.head_size
        )
        feed_forward = 3 * self.hidden_size * self.intermediate_size
        norms = 2 * self.hidden_size
        output = 0 if self.tie_word_embeddings else embedding
        return (
            embedding
            + self.num_hidden_layers * (attention + feed_forward + norms)
            + self.hidden_size
            + output
        )


class RMSNorm(nn.Module):
    def __init__(self, width: int, epsilon: float) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.ones(width))
        self.epsilon = float(epsilon)

    def forward(self, values: Tensor) -> Tensor:
        scale = torch.rsqrt(
            values.float().square().mean(dim=-1, keepdim=True) + self.epsilon
        )
        return values * scale.to(values.dtype) * self.weight


class RotaryPositions(nn.Module):
    def __init__(self, head_size: int, limit: int, theta: float) -> None:
        super().__init__()
        frequencies = 1.0 / (
            theta
            ** (
                torch.arange(0, head_size, 2, dtype=torch.float32)
                / float(head_size)
            )
        )
        angles = torch.outer(torch.arange(limit, dtype=torch.float32), frequencies)
        self.register_buffer("cosines", angles.cos(), persistent=False)
        self.register_buffer("sines", angles.sin(), persistent=False)

    def forward(self, values: Tensor, positions: Tensor) -> Tensor:
        cosines = self.cosines.index_select(0, positions).to(values.dtype)
        sines = self.sines.index_select(0, positions).to(values.dtype)
        cosines = cosines[None, None, :, :]
        sines = sines[None, None, :, :]
        pairs = values.reshape(*values.shape[:-1], -1, 2)
        even, odd = pairs.unbind(dim=-1)
        rotated = torch.stack(
            (even * cosines - odd * sines, even * sines + odd * cosines),
            dim=-1,
        )
        return rotated.flatten(-2)


class GroupedSelfAttention(nn.Module):
    def __init__(self, config: ModelConfig) -> None:
        super().__init__()
        self.query_heads = config.num_attention_heads
        self.kv_heads = config.num_key_value_heads
        self.head_size = config.head_size
        self.q_proj = nn.Linear(config.hidden_size, config.hidden_size, bias=False)
        self.k_proj = nn.Linear(
            config.hidden_size, self.kv_heads * self.head_size, bias=False
        )
        self.v_proj = nn.Linear(
            config.hidden_size, self.kv_heads * self.head_size, bias=False
        )
        self.o_proj = nn.Linear(config.hidden_size, config.hidden_size, bias=False)
        self.positions = RotaryPositions(
            config.head_size,
            config.max_position_embeddings,
            config.rope_theta,
        )
        self.dropout = config.dropout

    @staticmethod
    def _mask(
        batch: int,
        query_tokens: int,
        key_tokens: int,
        past_tokens: int,
        device: torch.device,
        padding: Tensor | None,
    ) -> Tensor:
        queries = past_tokens + torch.arange(query_tokens, device=device)
        keys = torch.arange(key_tokens, device=device)
        allowed = keys[None, :] <= queries[:, None]
        allowed = allowed[None, None, :, :].expand(batch, 1, -1, -1)
        if padding is not None:
            if padding.shape != (batch, key_tokens):
                raise ValueError(
                    "attention mask must cover the batch and all cached key tokens"
                )
            allowed = allowed & padding[:, None, None, :].bool()
        return allowed

    def forward(
        self,
        hidden: Tensor,
        positions: Tensor,
        *,
        attention_mask: Tensor | None = None,
        cache: KVCache | GrowableKVCache | None = None,
        use_cache: bool = False,
    ) -> tuple[Tensor, KVCache | GrowableKVCache | None]:
        batch, token_count, width = hidden.shape
        query = self.q_proj(hidden).view(
            batch, token_count, self.query_heads, self.head_size
        )
        key = self.k_proj(hidden).view(
            batch, token_count, self.kv_heads, self.head_size
        )
        value = self.v_proj(hidden).view(
            batch, token_count, self.kv_heads, self.head_size
        )
        query = self.positions(query.transpose(1, 2), positions)
        key = self.positions(key.transpose(1, 2), positions)
        value = value.transpose(1, 2)

        past_tokens = 0
        next_cache: KVCache | GrowableKVCache | None = None
        if isinstance(cache, GrowableKVCache):
            past_tokens = cache.length
            key, value = cache.append(key, value)
            if use_cache:
                next_cache = cache
        else:
            if cache is not None:
                past_tokens = int(cache[0].shape[2])
                key = torch.cat((cache[0], key), dim=2)
                value = torch.cat((cache[1], value), dim=2)
            if use_cache:
                next_cache = (key, value)

        builtin_causal = cache is None and attention_mask is None
        mask = None
        if not builtin_causal:
            mask = self._mask(
                batch,
                token_count,
                int(key.shape[2]),
                past_tokens,
                hidden.device,
                attention_mask,
            )
        dropout = self.dropout if self.training else 0.0
        try:
            attended = functional.scaled_dot_product_attention(
                query,
                key,
                value,
                attn_mask=mask,
                dropout_p=dropout,
                is_causal=builtin_causal,
                enable_gqa=self.query_heads != self.kv_heads,
            )
        except TypeError:
            repeat = self.query_heads // self.kv_heads
            attended = functional.scaled_dot_product_attention(
                query,
                key.repeat_interleave(repeat, dim=1),
                value.repeat_interleave(repeat, dim=1),
                attn_mask=mask,
                dropout_p=dropout,
                is_causal=builtin_causal,
            )
        except RuntimeError as error:
            if "gqa" not in str(error).lower():
                raise
            repeat = self.query_heads // self.kv_heads
            attended = functional.scaled_dot_product_attention(
                query,
                key.repeat_interleave(repeat, dim=1),
                value.repeat_interleave(repeat, dim=1),
                attn_mask=mask,
                dropout_p=dropout,
                is_causal=builtin_causal,
            )
        attended = attended.transpose(1, 2).contiguous().view(batch, token_count, width)
        return self.o_proj(attended), next_cache


class SwiGLU(nn.Module):
    def __init__(self, config: ModelConfig) -> None:
        super().__init__()
        self.gate_proj = nn.Linear(
            config.hidden_size, config.intermediate_size, bias=False
        )
        self.up_proj = nn.Linear(
            config.hidden_size, config.intermediate_size, bias=False
        )
        self.down_proj = nn.Linear(
            config.intermediate_size, config.hidden_size, bias=False
        )

    def forward(self, hidden: Tensor) -> Tensor:
        return self.down_proj(
            functional.silu(self.gate_proj(hidden)) * self.up_proj(hidden)
        )


class DecoderLayer(nn.Module):
    def __init__(self, config: ModelConfig) -> None:
        super().__init__()
        self.attention_norm = RMSNorm(
            config.hidden_size, config.rms_norm_epsilon
        )
        self.attention = GroupedSelfAttention(config)
        self.ffn_norm = RMSNorm(config.hidden_size, config.rms_norm_epsilon)
        self.feed_forward = SwiGLU(config)

    def forward(
        self,
        hidden: Tensor,
        positions: Tensor,
        *,
        attention_mask: Tensor | None = None,
        cache: KVCache | GrowableKVCache | None = None,
        use_cache: bool = False,
    ) -> tuple[Tensor, KVCache | GrowableKVCache | None]:
        attention, next_cache = self.attention(
            self.attention_norm(hidden),
            positions,
            attention_mask=attention_mask,
            cache=cache,
            use_cache=use_cache,
        )
        hidden = hidden + attention
        hidden = hidden + self.feed_forward(self.ffn_norm(hidden))
        return hidden, next_cache


class SaltyPotato(nn.Module):
    def __init__(self, config: ModelConfig, *, initialize: bool = True) -> None:
        super().__init__()
        self.config = config
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList(
            DecoderLayer(config) for _ in range(config.num_hidden_layers)
        )
        self.norm = RMSNorm(config.hidden_size, config.rms_norm_epsilon)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        if config.tie_word_embeddings:
            self.lm_head.weight = self.embed_tokens.weight
        if initialize:
            self.reset_parameters()

    def reset_parameters(self) -> None:
        standard_deviation = self.config.initializer_range
        for module in self.modules():
            if isinstance(module, (nn.Linear, nn.Embedding)):
                nn.init.normal_(module.weight, mean=0.0, std=standard_deviation)
        residual_deviation = standard_deviation / math.sqrt(
            2 * self.config.num_hidden_layers
        )
        for layer in self.layers:
            nn.init.normal_(
                layer.attention.o_proj.weight, mean=0.0, std=residual_deviation
            )
            nn.init.normal_(
                layer.feed_forward.down_proj.weight,
                mean=0.0,
                std=residual_deviation,
            )

    def parameter_count(self) -> int:
        return sum(parameter.numel() for parameter in self.parameters())

    def checkpoint_state(self) -> dict[str, Tensor]:
        state = self.state_dict()
        if self.config.tie_word_embeddings:
            state.pop("lm_head.weight", None)
        return state

    def forward(
        self,
        input_ids: Tensor,
        *,
        labels: Tensor | None = None,
        attention_mask: Tensor | None = None,
        cache: list[KVCache | GrowableKVCache | None] | None = None,
        use_cache: bool = False,
    ) -> dict[str, Tensor | list[KVCache | GrowableKVCache | None] | None]:
        if input_ids.ndim != 2 or input_ids.dtype != torch.long:
            raise ValueError("input IDs must be a rank-two torch.long tensor")
        if use_cache and self.training:
            raise ValueError("KV caching is available only in evaluation mode")
        _, token_count = input_ids.shape
        past_tokens = _cached_token_count(cache[0]) if cache else 0
        if past_tokens + token_count > self.config.max_position_embeddings:
            raise ValueError("request exceeds this saved version's context limit")
        positions = torch.arange(
            past_tokens,
            past_tokens + token_count,
            device=input_ids.device,
        )
        hidden = self.embed_tokens(input_ids)
        produced_caches: list[KVCache | GrowableKVCache | None] = []
        for index, layer in enumerate(self.layers):
            layer_cache = cache[index] if cache else None
            if (
                self.config.use_gradient_checkpointing
                and self.training
                and not use_cache
                and layer_cache is None
            ):
                def layer_only(values: Tensor, active_layer=layer) -> Tensor:
                    return active_layer(
                        values,
                        positions,
                        attention_mask=attention_mask,
                    )[0]

                hidden = activation_checkpoint(
                    layer_only, hidden, use_reentrant=False
                )
                next_cache = None
            else:
                hidden, next_cache = layer(
                    hidden,
                    positions,
                    attention_mask=attention_mask,
                    cache=layer_cache,
                    use_cache=use_cache,
                )
            if use_cache:
                produced_caches.append(next_cache)
        logits = self.lm_head(self.norm(hidden))
        loss = None
        if labels is not None:
            if labels.shape != input_ids.shape or token_count < 2:
                raise ValueError("labels must match at least two input tokens")
            loss = causal_cross_entropy(logits, labels).mean
        return {
            "logits": logits,
            "loss": loss,
            "cache": produced_caches if use_cache else None,
        }

    @torch.inference_mode()
    def generate(
        self,
        input_ids: Tensor,
        *,
        max_new_tokens: int,
        eos_token_id: int | None,
        temperature: float = 0.8,
        top_p: float = 0.95,
        top_k: int = 40,
        repetition_penalty: float = 1.1,
        seed: int | None = None,
        prefill_chunk_size: int = 256,
        should_stop: Callable[[], bool] | None = None,
        timing: dict[str, Any] | None = None,
    ) -> Tensor:
        if input_ids.ndim != 2 or input_ids.shape[0] != 1:
            raise ValueError("generation currently accepts exactly one prompt")
        if input_ids.shape[1] + max_new_tokens > self.config.max_position_embeddings:
            raise ValueError("prompt and requested output exceed the context limit")
        if input_ids.shape[1] == 0:
            raise ValueError("generation prompt is empty")
        self.eval()


        capacity = int(input_ids.shape[1]) + int(max_new_tokens)
        parameter = next(self.parameters())
        cache: list[KVCache | GrowableKVCache | None] | None = [
            GrowableKVCache.allocate(
                batch=int(input_ids.shape[0]),
                heads=self.config.num_key_value_heads,
                capacity=capacity,
                head_size=self.config.head_size,
                dtype=parameter.dtype,
                device=input_ids.device,
            )
            for _ in range(self.config.num_hidden_layers)
        ]
        generation_started = time.perf_counter() if timing is not None else 0.0

        def sync_for_timing() -> None:
            if timing is not None and input_ids.device.type == "cuda":
                torch.cuda.synchronize(input_ids.device)

        output = None
        prefill_started = time.perf_counter() if timing is not None else 0.0
        for offset in range(0, input_ids.shape[1], prefill_chunk_size):
            chunk = input_ids[:, offset : offset + prefill_chunk_size]
            output = self(chunk, cache=cache, use_cache=True)
            cache = output["cache"]
        assert output is not None
        sync_for_timing()
        prefill_finished = time.perf_counter() if timing is not None else 0.0
        prompt_length = input_ids.shape[1]


        sequence = torch.empty(
            (int(input_ids.shape[0]), capacity),
            dtype=torch.long,
            device=input_ids.device,
        )
        sequence[:, :prompt_length] = input_ids
        produced = 0
        generated = sequence[:, :prompt_length]
        logits = output["logits"][:, -1].float()
        generator = None
        if seed is not None and seed >= 0:
            generator = torch.Generator(device=input_ids.device)
            generator.manual_seed(int(seed))

        first_token_finished = 0.0
        decode_started = 0.0
        for index in range(int(max_new_tokens)):
            if should_stop is not None and should_stop():
                break
            if not bool(torch.isfinite(logits).all()):
                raise RuntimeError("model produced non-finite generation logits")
            adjusted = logits.clone()
            if repetition_penalty != 1.0:
                continuation = generated[0, prompt_length:]
                if continuation.numel():
                    seen = torch.unique(continuation)
                    if eos_token_id is not None:
                        seen = seen[seen != int(eos_token_id)]
                    if seen.numel():
                        values = adjusted[0, seen]
                        adjusted[0, seen] = torch.where(
                            values < 0,
                            values * repetition_penalty,
                            values / repetition_penalty,
                        )
            if temperature <= 0:
                next_token = adjusted.argmax(dim=-1, keepdim=True)
            else:
                adjusted /= temperature
                if top_k > 0 and top_k < adjusted.shape[-1]:
                    threshold = adjusted.topk(top_k, dim=-1).values[:, -1:]
                    adjusted.masked_fill_(adjusted < threshold, float("-inf"))
                probabilities = functional.softmax(adjusted, dim=-1)
                sorted_probabilities, sorted_ids = probabilities.sort(
                    dim=-1, descending=True
                )
                cumulative = sorted_probabilities.cumsum(dim=-1)
                sorted_probabilities.masked_fill_(
                    cumulative - sorted_probabilities > top_p, 0
                )
                sorted_probabilities /= sorted_probabilities.sum(
                    dim=-1, keepdim=True
                ).clamp_min(1e-12)
                selected = torch.multinomial(
                    sorted_probabilities, 1, generator=generator
                )
                next_token = sorted_ids.gather(-1, selected)
            sequence[:, prompt_length + produced] = next_token[:, 0]
            produced += 1
            generated = sequence[:, : prompt_length + produced]
            if index == 0 and timing is not None:
                sync_for_timing()
                first_token_finished = time.perf_counter()
                decode_started = first_token_finished
            if eos_token_id is not None and int(next_token.item()) == eos_token_id:
                break
            if should_stop is not None and should_stop():
                break
            if index + 1 < max_new_tokens:
                output = self(next_token, cache=cache, use_cache=True)
                cache = output["cache"]
                logits = output["logits"][:, -1].float()
        if timing is not None:
            sync_for_timing()
            finished = time.perf_counter()
            generated_count = max(0, int(generated.shape[1]) - prompt_length)
            decode_duration = (
                max(0.0, finished - decode_started)
                if generated_count > 1 and decode_started
                else 0.0
            )
            timing.update(
                {
                    "prefill_duration_seconds": max(
                        0.0, prefill_finished - prefill_started
                    ),
                    "first_token_latency_seconds": max(
                        0.0,
                        (
                            first_token_finished - generation_started
                            if first_token_finished
                            else finished - generation_started
                        ),
                    ),
                    "decode_duration_seconds": decode_duration,
                    "decode_tokens_per_second": (
                        (generated_count - 1) / decode_duration
                        if decode_duration > 0 and generated_count > 1
                        else None
                    ),
                    "total_duration_seconds": max(0.0, finished - generation_started),
                    "kv_cache_enabled": True,
                    "kv_cache_layers": len(cache or []),
                    "kv_cache_device": (
                        str(cache[0].keys.device)
                        if cache and isinstance(cache[0], GrowableKVCache)
                        else None
                    ),
                    "kv_cache_tokens": (
                        _cached_token_count(cache[0]) if cache else 0
                    ),
                    "cuda_synchronisation": input_ids.device.type == "cuda",
                    "attention_backend": "torch.sdpa.enable_gqa",
                    "compile_mode": "eager",
                    "device": str(input_ids.device),
                    "dtype": str(next(self.parameters()).dtype),
                }
            )
        return generated


def create_initial_model(
    config: ModelConfig, device: str | torch.device = "cpu", seed: int = 1337
) -> SaltyPotato:
    resolved = normalise_device(device, make_current=True)
    target = resolved.device
    devices = [resolved.cuda_index] if resolved.cuda_index is not None else []
    with torch.random.fork_rng(devices=devices):
        torch.manual_seed(seed)
        if devices:
            torch.cuda.manual_seed_all(seed)
        model = SaltyPotato(config)
    if model.parameter_count() != config.expected_parameter_count():
        raise RuntimeError("constructed model does not match the architecture count")
    return model.to(target)
