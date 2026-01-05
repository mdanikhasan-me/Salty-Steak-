"""One causal language-model loss contract shared by training and evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from torch import Tensor
from torch.nn import functional


IGNORE_INDEX = -100
LOSS_CONTRACT_VERSION = "causal-ce-shift-v1"


@dataclass(frozen=True)
class CausalLoss:
    mean: Tensor
    numerator: Tensor
    valid_target_tokens: int


def causal_cross_entropy(logits: Tensor, labels: Tensor) -> CausalLoss:
    """Apply exactly one causal shift and a valid-target token mean."""

    if logits.ndim != 3 or labels.ndim != 2:
        raise ValueError("causal loss requires [batch,time,vocab] logits and labels")
    if logits.shape[:2] != labels.shape or logits.shape[1] < 2:
        raise ValueError("labels must match at least two logit time positions")
    shifted_logits = logits[:, :-1].float().reshape(-1, logits.shape[-1])
    shifted_labels = labels[:, 1:].reshape(-1)
    valid = int((shifted_labels != IGNORE_INDEX).sum().item())
    if valid < 1:
        raise ValueError("causal loss has no valid target tokens")
    numerator = functional.cross_entropy(
        shifted_logits,
        shifted_labels,
        ignore_index=IGNORE_INDEX,
        reduction="sum",
    )
    return CausalLoss(
        mean=numerator / valid,
        numerator=numerator,
        valid_target_tokens=valid,
    )
