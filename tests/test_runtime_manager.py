from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import torch

from app.backend.runtime import manager as runtime_module
from app.backend.runtime.manager import RuntimeManager
from app.backend.versions.tokenizer import PromptBudget


class _Progress:
    def __init__(self) -> None:
        self.phases: list[str] = []

    def update(self, phase, *, current=None, total=None, result=None) -> None:
        self.phases.append(phase)


@dataclass
class _Config:
    max_position_embeddings: int


class _FakeModel:
    def __init__(self, marker: int, context_limit: int) -> None:
        self.marker = marker
        self.config = _Config(context_limit)
        self.anchor = torch.nn.Parameter(torch.zeros(1))

    def eval(self):
        return self

    def parameters(self):
        yield self.anchor

    def generate(self, input_ids, **settings):
        count = int(settings.get("max_new_tokens", 1))
        token = torch.tensor(
            [[self.marker] * count], dtype=torch.long, device=input_ids.device
        )
        return torch.cat((input_ids, token), dim=1)


class _FakeTokenizer:
    eos_id = 2

    def build_generation_prompt(
        self, messages, *, context_limit, reserved_output_tokens
    ) -> PromptBudget:
        assert messages[-1]["role"] == "user"
        return PromptBudget(
            token_ids=[1, 5, 6],
            context_tokens=3,
            context_limit=context_limit,
            omitted_turns=max(0, len(messages) - 1),
            omitted_tokens=0,
        )

    def decode(self, token_ids) -> str:
        return f"token-{token_ids[-1]}" if token_ids else ""


@dataclass
class _Report:
    valid: bool
    weight_sha256: str
    errors: list[str]


def test_activation_rollback_identity_and_stale_runtime_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints = {
        str((tmp_path / "one").resolve()): ("a" * 64, 21),
        str((tmp_path / "two").resolve()): ("b" * 64, 37),
    }
    for path in checkpoints:
        Path(path).mkdir()

    def inspect(checkpoint, *, calculate_checksum):
        checksum, _marker = checkpoints[str(Path(checkpoint).resolve())]
        return _Report(True, checksum, [])

    def load(checkpoint, *, device, dtype, context_limit):
        _checksum, marker = checkpoints[str(Path(checkpoint).resolve())]
        return _FakeModel(marker, context_limit), _FakeTokenizer()

    monkeypatch.setattr(runtime_module, "inspect_checkpoint", inspect)
    monkeypatch.setattr(runtime_module, "load_checkpoint", load)
    runtime = RuntimeManager(tmp_path / "runtime", device="cpu", precision="fp32")
    active: dict[str, str] = {}

    def commit(identity) -> None:
        active["checkpoint_id"] = identity.checkpoint_id
        active["runtime_id"] = identity.runtime_id

    first = runtime.activate(
        checkpoint_id="version-one",
        checkpoint_path=tmp_path / "one",
        label="Salty Steak imported version 1",
        total_trained_steps=None,
        context_limit=8192,
        expected_weight_sha256="a" * 64,
        progress=_Progress(),
        commit_active=commit,
    )
    first_artifact = Path(first.artifact_path)
    assert first_artifact.is_dir()
    generated = runtime.generate(
        active_checkpoint_id="version-one",
        messages=[{"role": "user", "content": "hello"}],
        reserved_output_tokens=1,
        maximum_output_tokens=1,
        temperature=0,
        top_p=1,
        top_k=0,
        repetition_penalty=1,
        seed=1,
    )
    assert generated.text == "token-21"
    assert generated.technical_details["checkpoint_id"] == "version-one"
    assert generated.technical_details["runtime_id"] == first.runtime_id
    assert generated.technical_details["total_trained_steps"] is None
    details = generated.technical_details
    assert details["input_context_tokens"] == 3
    assert details["generated_output_tokens"] == 1
    assert details["total_processed_tokens"] == (
        details["input_context_tokens"] + details["generated_output_tokens"]
    )
    assert details["architectural_context_limit"] == 8192
    assert details["reserved_output_tokens"] == 1
    assert details["omitted_input_tokens"] == 0
    with pytest.raises(RuntimeError, match="differ"):
        runtime.generate(
            active_checkpoint_id="version-two",
            messages=[{"role": "user", "content": "hello"}],
            reserved_output_tokens=1,
            maximum_output_tokens=1,
            temperature=0,
            top_p=1,
            top_k=0,
            repetition_penalty=1,
            seed=1,
        )

    before_children = {path.name for path in (tmp_path / "runtime").iterdir()}

    def reject(_identity) -> None:
        raise RuntimeError("database commit rejected")

    with pytest.raises(RuntimeError, match="database commit rejected"):
        runtime.activate(
            checkpoint_id="version-two",
            checkpoint_path=tmp_path / "two",
            label="Salty Steak imported version 2",
            total_trained_steps=None,
            context_limit=8192,
            expected_weight_sha256="b" * 64,
            progress=_Progress(),
            commit_active=reject,
        )
    assert runtime.identity == first
    assert {path.name for path in (tmp_path / "runtime").iterdir()} == before_children
    assert first_artifact.is_dir()

    second = runtime.activate(
        checkpoint_id="version-two",
        checkpoint_path=tmp_path / "two",
        label="Salty Steak imported version 2",
        total_trained_steps=None,
        context_limit=8192,
        expected_weight_sha256="b" * 64,
        progress=_Progress(),
        commit_active=commit,
    )
    assert runtime.identity == second
    assert active == {
        "checkpoint_id": "version-two",
        "runtime_id": second.runtime_id,
    }
    assert not first_artifact.exists()
    generated_after_switch = runtime.generate(
        active_checkpoint_id="version-two",
        messages=[{"role": "user", "content": "hello"}],
        reserved_output_tokens=1,
        maximum_output_tokens=1,
        temperature=0,
        top_p=1,
        top_k=0,
        repetition_penalty=1,
        seed=1,
    )
    assert generated_after_switch.text == "token-37"
    assert generated_after_switch.technical_details["checkpoint_id"] == "version-two"
