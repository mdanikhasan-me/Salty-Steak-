from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
import torch
from safetensors.torch import load_file, save_file

from app.backend.versions.checkpoint import (
    inspect_checkpoint,
    load_checkpoint,
    save_completed_checkpoint,
)
from app.backend.versions.model import ModelConfig, SaltyPotato, create_initial_model
from app.backend.versions.tokenizer import SaltyTokenizer
from tests.model_fixtures import build_test_tokenizer


def _tiny_config(*, context: int = 128) -> ModelConfig:
    return ModelConfig(
        architecture_version=2,
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=context,
        use_gradient_checkpointing=False,
    )


def test_decoder_pipeline_has_shifted_loss_causal_cache_and_context_limit() -> None:
    config = _tiny_config()
    model = create_initial_model(config, seed=17)
    model.eval()
    input_ids = torch.tensor([[1, 11, 12, 13, 14]], dtype=torch.long)

    complete = model(input_ids)
    cached = None
    pieces = []
    for index in range(input_ids.shape[1]):
        output = model(
            input_ids[:, index : index + 1],
            cache=cached,
            use_cache=True,
        )
        cached = output["cache"]
        pieces.append(output["logits"])
    cached_logits = torch.cat(pieces, dim=1)
    assert torch.allclose(complete["logits"], cached_logits, atol=1e-6)

    train_model = create_initial_model(config, seed=17)
    labels = input_ids.clone()
    shifted = train_model(input_ids, labels=labels)["loss"]
    expected = torch.nn.functional.cross_entropy(
        train_model(input_ids)["logits"][:, :-1].float().reshape(-1, 64),
        labels[:, 1:].reshape(-1),
    )
    assert torch.allclose(shifted, expected)

    with pytest.raises(ValueError, match="context limit"):
        model(torch.ones((1, config.max_position_embeddings + 1), dtype=torch.long))

    timing: dict[str, object] = {}
    generated = model.generate(
        input_ids,
        max_new_tokens=3,
        eos_token_id=None,
        temperature=0,
        timing=timing,
    )
    assert generated.shape[1] == input_ids.shape[1] + 3
    assert timing["kv_cache_enabled"] is True
    assert timing["kv_cache_layers"] == config.num_hidden_layers
    assert timing["attention_backend"] == "torch.sdpa.enable_gqa"
    assert timing["decode_tokens_per_second"] is not None


def test_real_sentencepiece_special_tokens_and_token_budget(tmp_path: Path) -> None:
    tokenizer = SaltyTokenizer(build_test_tokenizer(tmp_path / "tokenizer"))
    encoded = tokenizer.encode("Salty Steak", add_bos=True, add_eos=True)
    assert encoded[0] == tokenizer.bos_id
    assert encoded[-1] == tokenizer.eos_id
    assert "Salty" in tokenizer.decode(encoded[1:-1])

    messages = [
        {"role": "user", "content": "old " * 40},
        {"role": "assistant", "content": "older answer " * 20},
        {"role": "user", "content": "newest question"},
    ]
    prompt = tokenizer.build_generation_prompt(
        messages,
        context_limit=96,
        reserved_output_tokens=24,
    )
    assert prompt.context_tokens <= 72
    assert prompt.omitted_turns > 0
    assert tokenizer.role_id("user") in prompt.token_ids
    assert prompt.token_ids[-1] == tokenizer.role_id("assistant")


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "Question"}],
        [
            {"role": "system", "content": "Use terse answers."},
            {"role": "user", "content": "Question"},
        ],
        [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Answer"},
            {"role": "user", "content": "Second"},
        ],
    ],
)
def test_training_and_generation_templates_have_exact_prefix_parity(
    tmp_path: Path,
    messages: list[dict[str, str]],
) -> None:
    tokenizer = SaltyTokenizer(build_test_tokenizer(tmp_path / "tokenizer"))
    prompt = tokenizer.build_generation_prompt(
        messages,
        context_limit=4096,
        reserved_output_tokens=64,
    )
    training_tokens, _labels = tokenizer.conversation_example(
        messages,
        4096,
        assistant_only_labels=True,
    )
    expected = [
        *training_tokens,
        tokenizer.role_id("assistant"),
    ]
    assert prompt.token_ids == expected


def test_atomic_checkpoint_save_reload_preserves_greedy_output(tmp_path: Path) -> None:
    tokenizer_path = build_test_tokenizer(tmp_path / "tokenizer")
    model = create_initial_model(_tiny_config(), seed=29)
    model.eval()
    prompt = torch.tensor([[1, 4, 15, 16]], dtype=torch.long)
    expected = model.generate(
        prompt,
        max_new_tokens=4,
        eos_token_id=None,
        temperature=0,
    )
    checkpoint = tmp_path / "saved-version"
    manifest = save_completed_checkpoint(
        checkpoint,
        model,
        tokenizer_path,
        total_steps=None,
        additional_steps=3,
        dataset_id="dataset-test",
    )
    assert manifest["total_steps"] is None

    report = inspect_checkpoint(
        checkpoint,
        require_production_architecture=False,
    )
    assert report.valid, report.errors
    assert report.step is None
    assert "training step is not independently trustworthy" in report.warnings

    restored, restored_tokenizer = load_checkpoint(
        checkpoint,
        device="cpu",
        require_production_architecture=False,
    )
    actual = restored.generate(
        prompt,
        max_new_tokens=4,
        eos_token_id=None,
        temperature=0,
    )
    assert torch.equal(expected, actual)
    assert restored_tokenizer.fingerprint == SaltyTokenizer(tokenizer_path).fingerprint
    assert restored.lm_head.weight.data_ptr() == restored.embed_tokens.weight.data_ptr()


def test_checkpoint_inspection_rejects_nonfinite_tensor(tmp_path: Path) -> None:
    tokenizer_path = build_test_tokenizer(tmp_path / "tokenizer")
    checkpoint = tmp_path / "bad-version"
    save_completed_checkpoint(
        checkpoint,
        create_initial_model(_tiny_config(), seed=31),
        tokenizer_path,
        total_steps=12,
        additional_steps=2,
        dataset_id=None,
    )
    tensors = load_file(str(checkpoint / "model.safetensors"))
    key = next(iter(tensors))
    tensors[key] = tensors[key].clone()
    tensors[key].view(-1)[0] = float("nan")
    save_file(tensors, str(checkpoint / "model.safetensors"))
    manifest_path = checkpoint / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    manifest["weights_sha256"] = hashlib.sha256(
        (checkpoint / "model.safetensors").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = inspect_checkpoint(
        checkpoint,
        require_production_architecture=False,
    )
    assert not report.valid
    assert any("non-finite" in error for error in report.errors)


def test_generation_stops_at_eos_before_maximum_even_when_prompt_contains_eos() -> None:
    model = create_initial_model(_tiny_config(), seed=41)
    calls = 0

    def scripted_forward(self, input_ids, **_kwargs):
        nonlocal calls
        logits = torch.full(
            (1, input_ids.shape[1], self.config.vocab_size),
            -100.0,
        )
        if calls == 0:
            logits[:, -1, 7] = 10.0
        else:
            logits[:, -1, 2] = 10.0
            logits[:, -1, 8] = 9.5
        calls += 1
        return {"logits": logits, "loss": None, "cache": []}

    model.forward = types.MethodType(scripted_forward, model)
    prompt = torch.tensor([[1, 2, 5]], dtype=torch.long)
    output = model.generate(
        prompt,
        max_new_tokens=8,
        eos_token_id=2,
        temperature=0,
        repetition_penalty=1.1,
    )
    assert output.tolist() == [[1, 2, 5, 7, 2]]
