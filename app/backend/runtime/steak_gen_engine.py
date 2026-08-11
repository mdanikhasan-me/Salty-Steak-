"""Isolated Steak Gen 1 ScaledFP8 execution engine.

The module is imported only by ``steak_gen_worker``.  It combines the user's
canonical transformer with the exact pinned Steak Gen support components.  It
does not connect to any external tool, localhost model service, or the network.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .steak_gen import (
    CancellationSignal,
    PUBLIC_MODEL_ID,
    PUBLIC_MODEL_NAME,
    SUPPORT_REVISION,
    TRANSFORMER_SHA256,
    SteakGenCancelled,
    SteakGenError,
    SteakGenIntegrityError,
    SteakGenPaths,
    SteakGenRequest,
    SteakGenResult,
    verify_exact_bundle,
)


SOURCE_PREFIX = "model.diffusion_model."
EXPECTED_SOURCE_TENSORS = 789
EXPECTED_PRIMARY_TENSORS = 453
EXPECTED_FP8_TENSORS = 168
EXPECTED_TARGET_TENSORS = 521
EXPECTED_QUANT_DESCRIPTOR = {
    "format": "float8_e4m3fn",
    "full_precision_matrix_mult": True,
}


@dataclass(frozen=True)
class TensorMapping:
    source_key: str
    target_key: str
    shape: tuple[int, ...]
    dtype: str
    row_slice: tuple[int, int] | None = None


@dataclass(frozen=True)
class TransformerInspection:
    mappings: tuple[TensorMapping, ...]
    fp8_source_keys: tuple[str, ...]
    metadata_sha256: str

    def public_report(self) -> dict[str, Any]:
        return {
            "model_id": PUBLIC_MODEL_ID,
            "model_name": PUBLIC_MODEL_NAME,
            "source_tensor_count": EXPECTED_SOURCE_TENSORS,
            "primary_tensor_count": EXPECTED_PRIMARY_TENSORS,
            "scaled_fp8_tensor_count": EXPECTED_FP8_TENSORS,
            "target_tensor_count": EXPECTED_TARGET_TENSORS,
            "metadata_sha256": self.metadata_sha256,
            "metadata_visibility": "technical_only",
            "metadata_values_exposed": False,
            "scaled_fp8_semantics": "dequantized_weight=fp8_weight*weight_scale",
        }


def _target_name(source_key: str) -> str:
    if not source_key.startswith(SOURCE_PREFIX):
        raise SteakGenIntegrityError(
            f"Transformer tensor is outside the exact source prefix: {source_key}"
        )
    target = source_key.removeprefix(SOURCE_PREFIX)
    if target.startswith("final_layer."):
        target = "all_final_layer.2-1." + target.removeprefix("final_layer.")
    elif target.startswith("x_embedder."):
        target = "all_x_embedder.2-1." + target.removeprefix("x_embedder.")
    target = target.replace(".attention.out.weight", ".attention.to_out.0.weight")
    target = target.replace(".attention.k_norm.weight", ".attention.norm_k.weight")
    target = target.replace(".attention.q_norm.weight", ".attention.norm_q.weight")
    return target


def map_source_tensor(
    source_key: str,
    *,
    shape: Iterable[int],
    dtype: str,
) -> tuple[TensorMapping, ...]:
    """Map a canonical source tensor to exact Steak Gen state keys."""

    checked_shape = tuple(int(axis) for axis in shape)
    target = _target_name(source_key)
    if not target.endswith(".attention.qkv.weight"):
        return (TensorMapping(source_key, target, checked_shape, dtype),)
    if len(checked_shape) != 2 or checked_shape[0] % 3:
        raise SteakGenIntegrityError(
            f"Fused QKV tensor has an invalid shape: {source_key}: {checked_shape}"
        )
    rows = checked_shape[0] // 3
    return tuple(
        TensorMapping(
            source_key=source_key,
            target_key=target.replace("qkv", projection),
            shape=(rows, checked_shape[1]),
            dtype=dtype,
            row_slice=(index * rows, (index + 1) * rows),
        )
        for index, projection in enumerate(("to_q", "to_k", "to_v"))
    )


def _metadata_digest(metadata: dict[str, str] | None) -> str:
    canonical = json.dumps(
        metadata or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _new_meta_transformer() -> Any:
    from accelerate import init_empty_weights
    from diffusers import ZImageTransformer2DModel

    with init_empty_weights(include_buffers=True):
        transformer = ZImageTransformer2DModel(
            all_patch_size=(2,),
            all_f_patch_size=(1,),
            in_channels=16,
            dim=3840,
            n_layers=30,
            n_refiner_layers=2,
            n_heads=30,
            n_kv_heads=30,
            norm_eps=1e-5,
            qk_norm=True,
            cap_feat_dim=2560,
            siglip_feat_dim=None,
            rope_theta=256.0,
            t_scale=1000.0,
            axes_dims=[32, 48, 48],
            axes_lens=[1024, 512, 512],
        )
    return transformer


def inspect_transformer(
    transformer_path: str | Path,
    *,
    transformer: Any | None = None,
) -> TransformerInspection:
    """Validate the complete header/mapping without decoding weight payloads."""

    from safetensors import safe_open

    path = Path(transformer_path).resolve()
    model = transformer or _new_meta_transformer()
    expected_state = {
        key: tuple(value.shape) for key, value in model.state_dict().items()
    }
    if len(expected_state) != EXPECTED_TARGET_TENSORS:
        raise SteakGenIntegrityError(
            "Installed Steak Gen state contract changed: "
            f"{len(expected_state)} != {EXPECTED_TARGET_TENSORS}"
        )

    with safe_open(path, framework="pt", device="cpu") as source:
        keys = tuple(source.keys())
        if len(keys) != EXPECTED_SOURCE_TENSORS:
            raise SteakGenIntegrityError(
                f"Transformer tensor count changed: {len(keys)} != {EXPECTED_SOURCE_TENSORS}"
            )
        primary = tuple(
            key
            for key in keys
            if not key.endswith(".weight_scale")
            and not key.endswith(".comfy_quant")
        )
        if len(primary) != EXPECTED_PRIMARY_TENSORS:
            raise SteakGenIntegrityError(
                "Transformer primary tensor count changed: "
                f"{len(primary)} != {EXPECTED_PRIMARY_TENSORS}"
            )
        mappings: list[TensorMapping] = []
        fp8_keys: list[str] = []
        for key in primary:
            view = source.get_slice(key)
            dtype = str(view.get_dtype())
            shape = tuple(int(axis) for axis in view.get_shape())
            if dtype not in {"BF16", "F8_E4M3"}:
                raise SteakGenIntegrityError(
                    f"Unsupported transformer dtype: {key}: {dtype}"
                )
            mapped = map_source_tensor(key, shape=shape, dtype=dtype)
            mappings.extend(mapped)
            if dtype == "F8_E4M3":
                if not key.endswith(".weight"):
                    raise SteakGenIntegrityError(
                        f"Scaled FP8 tensor is not a linear weight: {key}"
                    )
                fp8_keys.append(key)
                stem = key.removesuffix(".weight")
                scale_key = stem + ".weight_scale"
                descriptor_key = stem + ".comfy_quant"
                if scale_key not in keys or descriptor_key not in keys:
                    raise SteakGenIntegrityError(
                        f"Scaled FP8 sidecars are incomplete: {key}"
                    )
                scale_view = source.get_slice(scale_key)
                descriptor_view = source.get_slice(descriptor_key)
                if scale_view.get_dtype() != "F32" or tuple(scale_view.get_shape()):
                    raise SteakGenIntegrityError(f"Invalid FP8 scale tensor: {scale_key}")
                if descriptor_view.get_dtype() != "U8" or tuple(
                    descriptor_view.get_shape()
                ) != (63,):
                    raise SteakGenIntegrityError(
                        f"Invalid FP8 descriptor tensor: {descriptor_key}"
                    )

        if len(fp8_keys) != EXPECTED_FP8_TENSORS:
            raise SteakGenIntegrityError(
                f"Scaled FP8 tensor count changed: {len(fp8_keys)} != {EXPECTED_FP8_TENSORS}"
            )
        if len(mappings) != EXPECTED_TARGET_TENSORS:
            raise SteakGenIntegrityError(
                f"Mapped tensor count changed: {len(mappings)} != {EXPECTED_TARGET_TENSORS}"
            )
        mapped_by_target: dict[str, TensorMapping] = {}
        for mapping in mappings:
            if mapping.target_key in mapped_by_target:
                raise SteakGenIntegrityError(
                    f"Duplicate mapped target tensor: {mapping.target_key}"
                )
            mapped_by_target[mapping.target_key] = mapping
        missing = sorted(set(expected_state) - set(mapped_by_target))
        unexpected = sorted(set(mapped_by_target) - set(expected_state))
        if missing or unexpected:
            raise SteakGenIntegrityError(
                f"Steak Gen state mapping mismatch; missing={missing[:3]}; "
                f"unexpected={unexpected[:3]}"
            )
        for target, expected_shape in expected_state.items():
            actual = mapped_by_target[target].shape
            if actual != expected_shape:
                raise SteakGenIntegrityError(
                    f"Mapped tensor shape mismatch: {target}: {actual} != {expected_shape}"
                )
        metadata_sha256 = _metadata_digest(source.metadata())

    return TransformerInspection(
        mappings=tuple(mappings),
        fp8_source_keys=tuple(fp8_keys),
        metadata_sha256=metadata_sha256,
    )


def _scaled_linear_type() -> type:
    import torch
    from torch import nn
    from torch.nn import functional as functional

    class ScaledFP8Linear(nn.Module):
        """Linear layer preserving scalar scaled-FP8 checkpoint semantics."""

        def __init__(
            self,
            in_features: int,
            out_features: int,
            *,
            has_bias: bool,
        ) -> None:
            super().__init__()
            self.in_features = int(in_features)
            self.out_features = int(out_features)
            self.weight = nn.Parameter(
                torch.empty(
                    (self.out_features, self.in_features),
                    device="meta",
                    dtype=torch.float8_e4m3fn,
                ),
                requires_grad=False,
            )
            if has_bias:
                self.bias = nn.Parameter(
                    torch.empty(self.out_features, device="meta"),
                    requires_grad=False,
                )
            else:
                self.register_parameter("bias", None)
            self.register_buffer(
                "weight_scale", torch.empty((), device="meta", dtype=torch.float32)
            )






            self._dequantized_cache: Any = None

        def _dequantized_weight(self, dtype: Any) -> Any:
            cached = self._dequantized_cache
            if cached is not None and cached.dtype == dtype:
                return cached
            if self.weight.dtype != torch.float8_e4m3fn:
                raise SteakGenIntegrityError(
                    "Scaled FP8 linear received a non-F8_E4M3 weight"
                )
            if self.weight_scale.numel() != 1 or not bool(
                torch.isfinite(self.weight_scale).all()
            ):
                raise SteakGenIntegrityError("Scaled FP8 linear has an invalid scale")
            dequantized = self.weight.to(dtype=dtype)
            dequantized.mul_(self.weight_scale.to(dtype=dtype))
            self._dequantized_cache = dequantized
            return dequantized

        def _apply(self, *arguments: Any, **keywords: Any) -> Any:


            self._dequantized_cache = None
            return super()._apply(*arguments, **keywords)

        def forward(self, inputs: Any) -> Any:
            dequantized = self._dequantized_weight(inputs.dtype)
            bias = self.bias
            if bias is not None and bias.dtype != inputs.dtype:
                bias = bias.to(dtype=inputs.dtype)
            return functional.linear(inputs, dequantized, bias)

        def extra_repr(self) -> str:
            return (
                f"in_features={self.in_features}, out_features={self.out_features}, "
                f"bias={self.bias is not None}, scaled_fp8=True"
            )

    return ScaledFP8Linear





ScaledFP8Linear = _scaled_linear_type()


def _module_and_name(model: Any, state_key: str) -> tuple[Any, str]:
    if "." not in state_key:
        return model, state_key
    parent_name, name = state_key.rsplit(".", 1)
    return model.get_submodule(parent_name), name


def _assign_tensor(model: Any, state_key: str, value: Any) -> None:
    import torch

    module, name = _module_and_name(model, state_key)



    if getattr(module, "_dequantized_cache", None) is not None:
        module._dequantized_cache = None
    if name in module._parameters:
        module._parameters[name] = torch.nn.Parameter(value, requires_grad=False)
        return
    if name in module._buffers:
        module._buffers[name] = value
        return
    raise SteakGenIntegrityError(f"Mapped target is not a state tensor: {state_key}")


def _replace_scaled_linears(
    transformer: Any, inspection: TransformerInspection
) -> None:
    import torch

    scaled_targets = {
        mapping.target_key.removesuffix(".weight")
        for mapping in inspection.mappings
        if mapping.source_key in inspection.fp8_source_keys
    }
    for module_name in sorted(scaled_targets):
        original = transformer.get_submodule(module_name)
        if not isinstance(original, torch.nn.Linear):
            raise SteakGenIntegrityError(
                f"Scaled FP8 target is not a linear module: {module_name}"
            )
        replacement = ScaledFP8Linear(
            original.in_features,
            original.out_features,
            has_bias=original.bias is not None,
        )
        parent_name, child_name = module_name.rsplit(".", 1)
        parent = transformer.get_submodule(parent_name)
        parent._modules[child_name] = replacement


def _validate_descriptor(value: Any, key: str) -> None:
    try:
        document = json.loads(bytes(value.tolist()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise SteakGenIntegrityError(f"Invalid FP8 descriptor payload: {key}") from error
    if document != EXPECTED_QUANT_DESCRIPTOR:
        raise SteakGenIntegrityError(f"Unexpected FP8 descriptor semantics: {key}")


def load_scaled_fp8_transformer(
    transformer_path: str | Path,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    cancellation: CancellationSignal | None = None,
) -> tuple[Any, TransformerInspection]:
    """Load the exact mixed BF16/scaled-FP8 transformer on CPU.

    No global BF16 expansion is performed.  Each scaled FP8 matrix is expanded
    only inside its linear call after its block is moved to the accelerator.
    """

    import torch
    from safetensors import safe_open

    transformer = _new_meta_transformer()
    inspection = inspect_transformer(transformer_path, transformer=transformer)
    _replace_scaled_linears(transformer, inspection)
    mappings_by_source: dict[str, list[TensorMapping]] = {}
    for mapping in inspection.mappings:
        mappings_by_source.setdefault(mapping.source_key, []).append(mapping)

    with safe_open(
        Path(transformer_path).resolve(), framework="pt", device="cpu"
    ) as source:
        for index, source_key in enumerate(sorted(mappings_by_source), start=1):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            tensor = source.get_tensor(source_key)
            mapped_records = mappings_by_source[source_key]
            for mapping in mapped_records:
                value = tensor
                if mapping.row_slice is not None:
                    value = tensor[mapping.row_slice[0] : mapping.row_slice[1]]
                _assign_tensor(transformer, mapping.target_key, value)
            if source_key in inspection.fp8_source_keys:
                stem = source_key.removesuffix(".weight")
                scale_key = stem + ".weight_scale"
                descriptor_key = stem + ".comfy_quant"
                scale = source.get_tensor(scale_key)
                if scale.dtype != torch.float32 or scale.shape != torch.Size([]):
                    raise SteakGenIntegrityError(f"Invalid FP8 scale payload: {scale_key}")
                if not math.isfinite(float(scale.item())) or float(scale.item()) <= 0:
                    raise SteakGenIntegrityError(
                        f"FP8 scale must be finite and positive: {scale_key}"
                    )
                _validate_descriptor(source.get_tensor(descriptor_key), descriptor_key)
                for mapping in mapped_records:
                    module_name = mapping.target_key.removesuffix(".weight")
                    module = transformer.get_submodule(module_name)
                    _assign_tensor(module, "weight_scale", scale.clone())
            if on_progress is not None and (
                index == 1 or index == len(mappings_by_source) or index % 30 == 0
            ):
                on_progress(
                    {
                        "phase": "loading_transformer",
                        "completed": index,
                        "total": len(mappings_by_source),
                    }
                )

    meta_parameters = [name for name, value in transformer.named_parameters() if value.is_meta]
    meta_buffers = [name for name, value in transformer.named_buffers() if value.is_meta]
    if meta_parameters or meta_buffers:
        raise SteakGenIntegrityError(
            "Transformer load left meta tensors: "
            + ", ".join((meta_parameters + meta_buffers)[:5])
        )
    transformer.requires_grad_(False)
    transformer.eval()
    return transformer, inspection


def _cuda_cleanup(torch: Any) -> None:
    gc.collect()
    if torch.cuda.is_available():
        try:
            torch.cuda.synchronize()
        except RuntimeError:
            pass
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


def _encode_prompt_serially(
    *,
    support_root: Path,
    cache_root: Path,
    prompt: str,
    negative_prompt: str,
    guidance_scale: float,
    max_sequence_length: int,
    cancellation: CancellationSignal,
    emit: Callable[[dict[str, Any]], None],
) -> tuple[list[Any], list[Any]]:
    import torch


    from transformers import AutoTokenizer
    from transformers import Qwen3Model as BaseSteakNativeModel

    cancellation.raise_if_cancelled()
    emit({"phase": "loading_text_encoder"})
    tokenizer = AutoTokenizer.from_pretrained(
        support_root / "tokenizer", local_files_only=True
    )
    if not torch.cuda.is_available():
        raise SteakGenError("A CUDA accelerator is required for Steak Gen")
    free_bytes, _total_bytes = torch.cuda.mem_get_info()
    reserved = 1024**3
    gpu_budget = min(6 * 1024**3, max(0, free_bytes - reserved))
    if gpu_budget < 2 * 1024**3:
        raise SteakGenError(
            "Steak Gen needs at least 2 GiB of currently free VRAM after its safety reserve"
        )
    text_encoder = BaseSteakNativeModel.from_pretrained(
        support_root / "text_encoder",
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        device_map="auto",
        max_memory={0: int(gpu_budget), "cpu": 10 * 1024**3},
        offload_folder=str(cache_root / "text-offload"),
        offload_state_dict=True,
        attn_implementation="sdpa",
    )
    text_encoder.requires_grad_(False)
    text_encoder.eval()

    def encode(value: str) -> list[Any]:
        cancellation.raise_if_cancelled()
        rendered = tokenizer.apply_chat_template(
            [{"role": "user", "content": value}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=True,
        )
        inputs = tokenizer(
            [rendered],



            padding="longest",
            max_length=max_sequence_length,
            truncation=True,
            return_tensors="pt",
        )
        first_device = text_encoder.get_input_embeddings().weight.device
        ids = inputs.input_ids.to(first_device)
        mask = inputs.attention_mask.to(first_device).bool()
        with torch.inference_mode():
            hidden = text_encoder(
                input_ids=ids,
                attention_mask=mask,
                output_hidden_states=True,
                use_cache=False,
            ).hidden_states[-2]
        mask = mask.to(hidden.device)
        return [hidden[0][mask[0]].detach().to("cpu", dtype=torch.bfloat16).contiguous()]

    try:
        emit({"phase": "encoding_prompt"})
        prompt_embeds = encode(prompt)
        negative_embeds = encode(negative_prompt) if guidance_scale > 0 else []
        cancellation.raise_if_cancelled()
        return prompt_embeds, negative_embeds
    finally:
        del text_encoder
        del tokenizer
        _cuda_cleanup(torch)
        emit({"phase": "text_encoder_unloaded"})


def generate_one_image(
    *,
    paths: SteakGenPaths,
    request: SteakGenRequest,
    cancellation: CancellationSignal,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> SteakGenResult:
    """Generate one PNG with serial text/transformer residency."""

    import torch
    from diffusers import AutoencoderKL, FlowMatchEulerDiscreteScheduler, ZImagePipeline

    raw_emit = on_event or (lambda _event: None)
    generation_started = time.perf_counter()

    def emit(event: dict[str, Any]) -> None:
        raw_emit(
            {
                **event,
                "elapsed_seconds": round(
                    time.perf_counter() - generation_started,
                    6,
                ),
            }
        )
    destination = request.validate(paths)
    if destination.exists():
        raise SteakGenError(f"Refusing to overwrite an existing image: {destination}")
    cancellation.raise_if_cancelled()
    emit({"phase": "verifying_bundle"})
    if request.verify_hashes:
        from .steak_gen import verify_exact_bundle_cached

        verification = verify_exact_bundle_cached(paths)
    else:
        verification = verify_exact_bundle(paths, verify_hashes=False)
    emit({"phase": "bundle_verified", **verification})

    prompt_embeds: list[Any] = []
    negative_embeds: list[Any] = []
    transformer: Any = None
    vae: Any = None
    pipeline: Any = None
    try:
        prompt_embeds, negative_embeds = _encode_prompt_serially(
            support_root=paths.support_root,
            cache_root=paths.cache_root,
            prompt=request.prompt,
            negative_prompt=request.negative_prompt,
            guidance_scale=request.guidance_scale,
            max_sequence_length=request.max_sequence_length,
            cancellation=cancellation,
            emit=emit,
        )
        cancellation.raise_if_cancelled()
        transformer, inspection = load_scaled_fp8_transformer(
            paths.transformer_path,
            on_progress=emit,
            cancellation=cancellation,
        )
        device = torch.device("cuda", 0)
        transformer.enable_group_offload(
            onload_device=device,
            offload_device=torch.device("cpu"),
            offload_type="block_level",
            num_blocks_per_group=1,
            non_blocking=False,
            use_stream=False,
            low_cpu_mem_usage=True,
        )
        emit({"phase": "transformer_ready", **inspection.public_report()})
        cancellation.raise_if_cancelled()

        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(
            paths.support_root,
            subfolder="scheduler",
            local_files_only=True,
        )
        vae = AutoencoderKL.from_pretrained(
            paths.support_root,
            subfolder="vae",
            local_files_only=True,
            torch_dtype=torch.bfloat16,
            low_cpu_mem_usage=True,
        )
        vae.requires_grad_(False)
        vae.eval()
        vae.enable_slicing()
        vae.enable_tiling()
        vae.to(device)
        pipeline = ZImagePipeline(
            scheduler=scheduler,
            vae=vae,
            text_encoder=None,
            tokenizer=None,
            transformer=transformer,
        )
        pipeline.set_progress_bar_config(disable=True)
        prompt_embeds = [value.to(device) for value in prompt_embeds]
        negative_embeds = [value.to(device) for value in negative_embeds]
        generator = torch.Generator(device=device).manual_seed(request.seed)

        def on_step_end(
            _pipeline: Any,
            step: int,
            _timestep: Any,
            callback_kwargs: dict[str, Any],
        ) -> dict[str, Any]:
            if cancellation.cancelled:
                raise SteakGenCancelled("Steak Gen image generation was cancelled")
            emit(
                {
                    "phase": "denoising",
                    "completed": int(step) + 1,
                    "total": request.steps,
                }
            )
            return callback_kwargs

        emit(
            {
                "phase": "denoising",
                "completed": 0,
                "total": request.steps,
            }
        )
        with torch.inference_mode():
            generated = pipeline(
                prompt=None,
                height=request.height,
                width=request.width,
                num_inference_steps=request.steps,
                guidance_scale=request.guidance_scale,
                negative_prompt=None,
                prompt_embeds=prompt_embeds,
                negative_prompt_embeds=(negative_embeds or None),
                num_images_per_prompt=1,
                generator=generator,
                output_type="pil",
                callback_on_step_end=on_step_end,
                callback_on_step_end_tensor_inputs=["latents"],
                max_sequence_length=request.max_sequence_length,
            )
        cancellation.raise_if_cancelled()
        image = generated.images[0]
        if image.size != (request.width, request.height):
            raise SteakGenError(
                f"Generated image dimensions changed: {image.size} != "
                f"{(request.width, request.height)}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            destination.name + f".{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            image.save(temporary, format="PNG", optimize=False)
            cancellation.raise_if_cancelled()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        emit({"phase": "complete", "output_sha256": digest})
        return SteakGenResult(
            output_path=str(destination),
            output_sha256=digest,
            width=request.width,
            height=request.height,
            seed=request.seed,
            steps=request.steps,
        )
    finally:
        del pipeline
        del vae
        del transformer
        prompt_embeds.clear()
        negative_embeds.clear()
        _cuda_cleanup(torch)
