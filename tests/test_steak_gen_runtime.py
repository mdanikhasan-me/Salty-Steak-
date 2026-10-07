from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.backend.runtime.steak_gen import (
    PUBLIC_MODEL_ID,
    PUBLIC_MODEL_NAME,
    PRIVATE_WORKER_PYTHON_FLAGS,
    CancellationSignal,
    PinnedFile,
    SteakGenCancelled,
    SteakGenError,
    SteakGenIntegrityError,
    SteakGenPaths,
    SteakGenRequest,
    SteakGenWorkerClient,
    verify_exact_bundle,
    verify_exact_bundle_cached,
)
from app.backend.runtime.steak_gen_engine import (
    EXPECTED_TARGET_TENSORS,
    ScaledFP8Linear,
    inspect_transformer,
    map_source_tensor,
)


def _synthetic_paths(tmp_path: Path) -> SteakGenPaths:
    workspace = tmp_path / "workspace"
    paths = SteakGenPaths.from_workspace(workspace)
    paths.support_root.mkdir(parents=True)
    paths.transformer_path.write_bytes(b"steak")
    return paths


def test_worker_prefers_packaged_virtual_environment_with_model_dependencies(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "package"
    venv_python = project_root / ".venv" / "Scripts" / "python.exe"
    private_base = project_root / ".python" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    private_base.parent.mkdir(parents=True)
    venv_python.touch()
    private_base.touch()

    client = SteakGenWorkerClient(
        project_root=project_root,
        workspace_root=tmp_path / "workspace",
    )

    assert client.python_executable == venv_python.resolve()
    assert PRIVATE_WORKER_PYTHON_FLAGS == ("-B",)


def test_request_requires_serial_text_runtime_and_scoped_png_output(
    tmp_path: Path,
) -> None:
    paths = SteakGenPaths.from_workspace(tmp_path / "workspace")
    output = paths.output_root / "result.png"
    request = SteakGenRequest(prompt="a calm forest", output_path=str(output))

    with pytest.raises(SteakGenError, match="Base Steak must be unloaded"):
        request.validate(paths)

    ready = SteakGenRequest(
        prompt=request.prompt,
        output_path=request.output_path,
        text_runtime_unloaded=True,
    )
    assert ready.validate(paths) == output.resolve()

    escaped = SteakGenRequest(
        prompt="x",
        output_path=str(tmp_path / "outside.png"),
        text_runtime_unloaded=True,
    )
    with pytest.raises(ValueError, match="workspace output root"):
        escaped.validate(paths)


@pytest.mark.parametrize(
    ("width", "height"),
    [(255, 512), (513, 512), (512, 1030)],
)
def test_request_fails_closed_on_unsafe_dimensions(
    tmp_path: Path, width: int, height: int
) -> None:
    paths = SteakGenPaths.from_workspace(tmp_path / "workspace")
    request = SteakGenRequest(
        prompt="x",
        output_path=str(paths.output_root / "x.png"),
        width=width,
        height=height,
        text_runtime_unloaded=True,
    )
    with pytest.raises(ValueError, match="divisible by 16"):
        request.validate(paths)


def test_exact_bundle_recomputes_hashes_and_exposes_no_embedded_metadata(
    tmp_path: Path,
) -> None:
    paths = _synthetic_paths(tmp_path)
    support = paths.support_root / "support.bin"
    support.write_bytes(b"pinned-support")
    transformer_bytes = paths.transformer_path.read_bytes()
    pins = {
        "support.bin": PinnedFile(
            support.stat().st_size,
            "sha256",
            hashlib.sha256(support.read_bytes()).hexdigest(),
        )
    }

    report = verify_exact_bundle(
        paths,
        support_files=pins,
        transformer_size=len(transformer_bytes),
        transformer_sha256=hashlib.sha256(transformer_bytes).hexdigest(),
    )

    assert report["model_id"] == PUBLIC_MODEL_ID
    assert report["model_name"] == PUBLIC_MODEL_NAME
    assert report["support_files_verified"] == 1
    assert report["hashes_recomputed"] is True
    assert report["embedded_metadata_visibility"] == "technical_only"
    assert "metadata" not in report


def test_exact_bundle_rejects_extra_support_and_hash_mismatch(tmp_path: Path) -> None:
    paths = _synthetic_paths(tmp_path)
    support = paths.support_root / "support.bin"
    support.write_bytes(b"changed")
    (paths.support_root / "unexpected.bin").write_bytes(b"unexpected")
    pins = {
        "support.bin": PinnedFile(
            support.stat().st_size,
            "sha256",
            hashlib.sha256(b"original").hexdigest(),
        )
    }

    with pytest.raises(SteakGenIntegrityError) as raised:
        verify_exact_bundle(
            paths,
            support_files=pins,
            transformer_size=paths.transformer_path.stat().st_size,
            transformer_sha256=hashlib.sha256(
                paths.transformer_path.read_bytes()
            ).hexdigest(),
        )

    assert "unexpected support file" in str(raised.value)
    assert "support hash mismatch" in str(raised.value)


def test_exact_bundle_cache_reuses_only_an_unchanged_full_hash_audit(
    tmp_path: Path,
) -> None:
    paths = _synthetic_paths(tmp_path)
    support = paths.support_root / "support.bin"
    support.write_bytes(b"pinned-support")
    transformer_bytes = paths.transformer_path.read_bytes()
    pins = {
        "support.bin": PinnedFile(
            support.stat().st_size,
            "sha256",
            hashlib.sha256(support.read_bytes()).hexdigest(),
        )
    }
    arguments = {
        "support_files": pins,
        "transformer_size": len(transformer_bytes),
        "transformer_sha256": hashlib.sha256(transformer_bytes).hexdigest(),
    }

    first = verify_exact_bundle_cached(paths, **arguments)
    second = verify_exact_bundle_cached(paths, **arguments)
    support.write_bytes(b"pinned-support")
    third = verify_exact_bundle_cached(paths, **arguments)

    assert first["hashes_recomputed"] is True
    assert first["verification_cache_hit"] is False
    assert second["hashes_recomputed"] is False
    assert second["verification_cache_hit"] is True
    assert third["hashes_recomputed"] is True
    assert third["verification_cache_hit"] is False


def test_cancellation_signal_is_explicit_and_recoverable(tmp_path: Path) -> None:
    signal = CancellationSignal(tmp_path / "control" / "cancel.signal")
    assert signal.cancelled is False
    signal.cancel()
    assert signal.cancelled is True
    with pytest.raises(SteakGenCancelled):
        signal.raise_if_cancelled()
    signal.clear()
    assert signal.cancelled is False


@pytest.mark.skipif(
    not SteakGenPaths.from_workspace(
        Path(r"D:\Projects\Salty Steak\workspace")
    ).transformer_path.exists(),
    reason="canonical Steak Gen transformer is not present",
)
def test_isolated_worker_honours_preflight_cancellation_without_gpu_work(
    tmp_path: Path,
) -> None:
    project_root = Path(r"D:\Projects\Salty Steak")
    paths = SteakGenPaths.from_workspace(project_root / "workspace")
    cancellation_path = tmp_path / "cancel.signal"
    cancellation_path.touch()
    output = paths.output_root / f"cancelled-{uuid.uuid4().hex}.png"
    payload = {
        "workspace_root": str(paths.workspace_root),
        "cancellation_path": str(cancellation_path),
        "request": {
            "prompt": "contract test only",
            "output_path": str(output),
            "text_runtime_unloaded": True,
            "verify_hashes": False,
        },
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.backend.runtime.steak_gen_worker",
            "--serve-once",
        ],
        cwd=project_root,
        input=json.dumps(payload) + "\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=False,
    )
    response = json.loads(completed.stdout.strip().splitlines()[-1])

    assert completed.returncode == 1
    assert response["ok"] is False
    assert response["error"]["type"] == "SteakGenCancelled"
    assert output.exists() is False


def test_fused_qkv_mapping_preserves_exact_projection_slices() -> None:
    mapped = map_source_tensor(
        "model.diffusion_model.layers.7.attention.qkv.weight",
        shape=(11520, 3840),
        dtype="F8_E4M3",
    )
    assert [item.target_key for item in mapped] == [
        "layers.7.attention.to_q.weight",
        "layers.7.attention.to_k.weight",
        "layers.7.attention.to_v.weight",
    ]
    assert [item.row_slice for item in mapped] == [
        (0, 3840),
        (3840, 7680),
        (7680, 11520),
    ]


def test_scaled_fp8_linear_applies_weight_times_scalar_semantics() -> None:
    import torch

    layer = ScaledFP8Linear(2, 1, has_bias=True)
    layer._parameters["weight"] = torch.nn.Parameter(
        torch.tensor([[1.0, -2.0]], dtype=torch.float8_e4m3fn),
        requires_grad=False,
    )
    layer._parameters["bias"] = torch.nn.Parameter(
        torch.tensor([0.25], dtype=torch.float32), requires_grad=False
    )
    layer._buffers["weight_scale"] = torch.tensor(0.5, dtype=torch.float32)

    actual = layer(torch.tensor([[2.0, 3.0]], dtype=torch.float32))

    assert torch.allclose(actual, torch.tensor([[-1.75]]))


@pytest.mark.skipif(
    not SteakGenPaths.from_workspace(
        Path(r"D:\Projects\Salty Steak\workspace")
    ).transformer_path.exists(),
    reason="canonical Steak Gen transformer is not present",
)
def test_canonical_transformer_matches_installed_z_image_config_without_payload_load(
) -> None:
    paths = SteakGenPaths.from_workspace(
        Path(r"D:\Projects\Salty Steak\workspace")
    )
    inspection = inspect_transformer(paths.transformer_path)
    report = inspection.public_report()

    assert len(inspection.mappings) == EXPECTED_TARGET_TENSORS
    assert report["scaled_fp8_tensor_count"] == 168
    assert report["metadata_visibility"] == "technical_only"
    assert report["metadata_values_exposed"] is False
    assert report["scaled_fp8_semantics"].endswith("fp8_weight*weight_scale")
