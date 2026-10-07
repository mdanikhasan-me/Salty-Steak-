from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import torch

from app.backend.system.device import (
    DeviceConfigurationError,
    ResolvedDevice,
    normalise_device,
)
from app.backend.training import engine as training_engine


def _mock_cuda(monkeypatch, *, available=True, count=1, bf16=True):
    selected: list[int] = []
    reset: list[int] = []
    monkeypatch.setattr(torch.cuda, "is_available", lambda: available)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: count)
    monkeypatch.setattr(torch.cuda, "set_device", selected.append)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", reset.append)
    monkeypatch.setattr(torch.cuda, "device", lambda _index: nullcontext())
    monkeypatch.setattr(
        torch.cuda,
        "is_bf16_supported",
        lambda **_kwargs: bf16,
    )
    return selected, reset


def test_cuda_resolves_to_indexed_device_and_cuda_apis_receive_integer(
    monkeypatch,
) -> None:
    selected, reset = _mock_cuda(monkeypatch)
    resolved = normalise_device(
        "cuda",
        precision="bf16",
        make_current=True,
        reset_peak_memory=True,
    )
    assert resolved == ResolvedDevice(torch.device("cuda:0"), 0)
    assert selected == [0]
    assert reset == [0]
    assert normalise_device(0).device == torch.device("cuda:0")


def test_cpu_still_works(monkeypatch) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    resolved = normalise_device("cpu", precision="fp32")
    assert resolved.device == torch.device("cpu")
    assert resolved.cuda_index is None
    assert normalise_device("auto", precision="fp32").device.type == "cpu"


def test_invalid_gpu_index_fails_clearly(monkeypatch) -> None:
    _mock_cuda(monkeypatch, count=1)
    with pytest.raises(DeviceConfigurationError, match="does not exist"):
        normalise_device("cuda:1")


def test_unsupported_bf16_fails_without_precision_fallback(monkeypatch) -> None:
    _mock_cuda(monkeypatch, bf16=False)
    with pytest.raises(DeviceConfigurationError, match="does not support native BF16"):
        normalise_device("cuda", precision="bf16")


def test_failed_training_releases_resolved_device(monkeypatch) -> None:
    resolved = ResolvedDevice(torch.device("cpu"), None)
    released = []
    monkeypatch.setattr(training_engine, "normalise_device", lambda *_a, **_k: resolved)
    monkeypatch.setattr(
        training_engine,
        "_run_training",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("failed before step 1")),
    )
    monkeypatch.setattr(training_engine, "release_device", released.append)
    settings = SimpleNamespace(device="cpu", precision="fp32")
    with pytest.raises(RuntimeError, match="before step 1"):
        training_engine.run_training(settings, object())
    assert released == [resolved]
