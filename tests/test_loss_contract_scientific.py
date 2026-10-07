from __future__ import annotations

import math

import torch
from torch.nn import functional

from app.backend.system.loss_contract import causal_cross_entropy


def test_single_batch_matches_independent_token_level_reference() -> None:
    logits = torch.tensor(
        [
            [
                [2.0, 0.0, -1.0, 0.5],
                [0.1, 1.2, -0.3, 0.4],
                [-0.5, 0.2, 2.1, 0.0],
                [0.7, -0.2, 0.1, 1.1],
            ]
        ],
        dtype=torch.float64,
    )
    labels = torch.tensor([[-100, 1, -100, 3]], dtype=torch.long)
    result = causal_cross_entropy(logits, labels)
    token_one = functional.cross_entropy(
        logits[:, 0, :].float(), torch.tensor([1]), reduction="sum"
    )
    token_three = functional.cross_entropy(
        logits[:, 2, :].float(), torch.tensor([3]), reduction="sum"
    )
    expected_numerator = token_one + token_three
    assert result.valid_target_tokens == 2
    torch.testing.assert_close(result.numerator, expected_numerator)
    torch.testing.assert_close(result.mean, expected_numerator / 2)
    assert math.isclose(
        math.exp(float(result.mean)),
        math.exp(float(expected_numerator / 2)),
    )


def test_padding_and_prompt_masks_do_not_change_denominator() -> None:
    generator = torch.Generator().manual_seed(17)
    logits = torch.randn((2, 5, 7), generator=generator)
    labels = torch.tensor(
        [
            [-100, -100, 2, 3, -100],
            [-100, -100, -100, 1, 4],
        ]
    )
    result = causal_cross_entropy(logits, labels)
    assert result.valid_target_tokens == 4


def test_gradient_accumulation_token_weighting_matches_combined_batch() -> None:
    parameter = torch.nn.Parameter(torch.tensor([[0.3, -0.1], [0.2, 0.4]]))
    inputs = [
        torch.tensor([[1.0, 0.0], [0.0, 1.0]]),
        torch.tensor([[1.0, 1.0]]),
    ]
    targets = [torch.tensor([0, 1]), torch.tensor([1])]
    counts = [2, 1]

    combined = torch.cat(inputs) @ parameter
    combined_loss = functional.cross_entropy(
        combined, torch.cat(targets), reduction="mean"
    )
    combined_loss.backward()
    expected = parameter.grad.detach().clone()

    parameter.grad = None
    for batch, target, count in zip(inputs, targets, counts, strict=True):
        loss = functional.cross_entropy(batch @ parameter, target, reduction="mean")
        (loss * count / sum(counts)).backward()
    torch.testing.assert_close(parameter.grad, expected)
