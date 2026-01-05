"""One validated device contract for every model workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import torch


DeviceInput: TypeAlias = str | int | torch.device
SUPPORTED_PRECISIONS = frozenset({"bf16", "fp16", "fp32"})


class DeviceConfigurationError(RuntimeError):
    """A device selection that cannot be honoured exactly."""


@dataclass(frozen=True, slots=True)
class ResolvedDevice:
    device: torch.device
    cuda_index: int | None

    @property
    def is_cuda(self) -> bool:
        return self.cuda_index is not None


def _parse_device(value: DeviceInput) -> tuple[str, int | None]:
    if isinstance(value, bool):
        raise DeviceConfigurationError(
            "Choose auto, cpu, cuda, cuda:0, or a CUDA device index."
        )
    if isinstance(value, int):
        return "cuda", value
    if isinstance(value, torch.device):
        if value.type == "cuda":
            return "cuda", 0 if value.index is None else int(value.index)
        if value.type == "cpu":
            return "cpu", None
        raise DeviceConfigurationError(
            f"The device type {value.type!r} is not supported."
        )

    selected = str(value).strip().lower()
    if selected == "auto":
        return ("cuda", 0) if torch.cuda.is_available() else ("cpu", None)
    if selected == "cpu":
        return "cpu", None
    if selected == "cuda":
        return "cuda", 0
    if selected.isdecimal():
        return "cuda", int(selected)
    if selected.startswith("cuda:") and selected[5:].isdecimal():
        return "cuda", int(selected[5:])
    raise DeviceConfigurationError(
        "Choose auto, cpu, cuda, cuda:0, or a CUDA device index."
    )


def _validate_precision(
    device_type: str,
    cuda_index: int | None,
    precision: str | None,
) -> None:
    if precision is None:
        return
    selected = str(precision).strip().lower()
    if selected not in SUPPORTED_PRECISIONS:
        raise DeviceConfigurationError(
            "Precision must be bf16, fp16, or fp32."
        )
    if device_type == "cpu" and selected != "fp32":
        raise DeviceConfigurationError(
            f"{selected.upper()} is not supported by the CPU execution path. "
            "Choose FP32 for CPU."
        )
    if device_type == "cuda" and selected == "bf16":
        assert cuda_index is not None
        if not cuda_supports_bf16(cuda_index):
            raise DeviceConfigurationError(
                f"CUDA device {cuda_index} does not support native BF16. "
                "Choose FP16 or FP32 explicitly."
            )


def cuda_supports_bf16(cuda_index: int) -> bool:
    """Report native BF16 support for one validated CUDA device index."""

    with torch.cuda.device(int(cuda_index)):
        return bool(torch.cuda.is_bf16_supported(including_emulation=False))


def normalise_device(
    value: DeviceInput,
    *,
    precision: str | None = None,
    make_current: bool = False,
    reset_peak_memory: bool = False,
) -> ResolvedDevice:
    """Resolve and validate a device without changing requested precision."""

    device_type, cuda_index = _parse_device(value)
    if device_type == "cuda":
        assert cuda_index is not None
        if not torch.cuda.is_available():
            raise DeviceConfigurationError(
                "CUDA was selected, but no CUDA GPU is available."
            )
        device_count = int(torch.cuda.device_count())
        if cuda_index < 0 or cuda_index >= device_count:
            raise DeviceConfigurationError(
                f"CUDA device {cuda_index} does not exist. "
                f"This computer reports {device_count} CUDA device"
                f"{'s' if device_count != 1 else ''}."
            )
        _validate_precision(device_type, cuda_index, precision)
        if make_current or reset_peak_memory:
            torch.cuda.set_device(cuda_index)
        if reset_peak_memory:
            torch.cuda.reset_peak_memory_stats(cuda_index)
        return ResolvedDevice(torch.device(f"cuda:{cuda_index}"), cuda_index)

    _validate_precision(device_type, None, precision)
    return ResolvedDevice(torch.device("cpu"), None)


def precision_for_dtype(dtype: torch.dtype | None) -> str | None:
    if dtype is torch.bfloat16:
        return "bf16"
    if dtype is torch.float16:
        return "fp16"
    if dtype is torch.float32:
        return "fp32"
    return None


def release_device(device: ResolvedDevice) -> None:
    """Release allocator cache owned by a completed or failed CUDA workflow."""

    if device.is_cuda and torch.cuda.is_available():
        torch.cuda.empty_cache()
