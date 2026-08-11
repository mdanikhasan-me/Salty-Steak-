"""Small learned full-context classifier for identity specialist selection."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Sequence

import numpy as np

from .identity_specialists import IDENTITY_SPECIALIST_POLICIES


IDENTITY_SUBROUTE_CLASSIFIER_SCHEMA = "base-steak-identity-subroute-linear-v1"
IDENTITY_SUBROUTE_FEATURE_DIMENSION = 32_768


def _normalise(text: object) -> str:
    return " ".join(str(text or "").casefold().split())


def _hash_feature(value: str, dimension: int) -> int:
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little") % dimension


def _ngrams(text: str, low: int, high: int):
    padded = f"^{text}$"
    for width in range(low, high + 1):
        for index in range(max(0, len(padded) - width + 1)):
            yield padded[index : index + width]


def identity_subroute_features(
    messages: Sequence[tuple[str, str]],
    *,
    dimension: int = IDENTITY_SUBROUTE_FEATURE_DIMENSION,
) -> tuple[np.ndarray, np.ndarray]:
    """Return deterministic sparse hashed features from the full conversation."""

    if dimension < 1024:
        raise ValueError("Identity subroute feature dimension is too small")
    cleaned = [
        (str(role).casefold(), _normalise(content))
        for role, content in messages
        if str(role).casefold() != "system"
    ]
    latest = next((text for role, text in reversed(cleaned) if role == "user"), "")
    counts: dict[int, float] = {}

    def add(namespace: str, token: str, weight: float) -> None:
        index = _hash_feature(f"{namespace}:{token}", dimension)
        counts[index] = counts.get(index, 0.0) + weight

    for gram in _ngrams(latest, 2, 5):
        add("latest-char", gram, 2.0)
    latest_words = re.findall(r"[\w'.-]+", latest, flags=re.UNICODE)
    for width in (1, 2, 3):
        for index in range(max(0, len(latest_words) - width + 1)):
            add("latest-word", " ".join(latest_words[index : index + width]), 4.0)

    for turn_index, (role, text) in enumerate(cleaned[-8:]):
        distance = len(cleaned[-8:]) - turn_index
        turn_weight = 1.0 / max(1.0, distance / 2.0)
        words = re.findall(r"[\w'.-]+", text, flags=re.UNICODE)
        for width in (1, 2):
            for index in range(max(0, len(words) - width + 1)):
                add(f"{role}-word", " ".join(words[index : index + width]), turn_weight)
        for gram in _ngrams(text, 3, 4):
            add(f"{role}-char", gram, 0.35 * turn_weight)
    add("shape", f"turns={min(len(cleaned), 8)}", 2.0)
    add("shape", f"latest_words={min(len(latest_words), 20)}", 1.0)
    indices = np.asarray(sorted(counts), dtype=np.int32)
    values = np.asarray([counts[index] for index in indices], dtype=np.float32)
    norm = float(np.linalg.norm(values))
    if norm > 0:
        values /= norm
    return indices, values


class IdentitySubrouteLinearClassifier:
    def __init__(self, weights: np.ndarray, bias: np.ndarray):
        checked_weights = np.asarray(weights, dtype=np.float32)
        checked_bias = np.asarray(bias, dtype=np.float32)
        if checked_weights.ndim != 2 or checked_weights.shape[0] != len(
            IDENTITY_SPECIALIST_POLICIES
        ):
            raise ValueError("Identity subroute classifier weights are invalid")
        if checked_bias.shape != (len(IDENTITY_SPECIALIST_POLICIES),):
            raise ValueError("Identity subroute classifier bias is invalid")
        self.weights = checked_weights
        self.bias = checked_bias

    @property
    def dimension(self) -> int:
        return int(self.weights.shape[1])

    def scores(self, messages: Sequence[tuple[str, str]]) -> np.ndarray:
        indices, values = identity_subroute_features(
            messages,
            dimension=self.dimension,
        )
        return self.bias + self.weights[:, indices] @ values

    def predict(self, messages: Sequence[tuple[str, str]]) -> str:
        return IDENTITY_SPECIALIST_POLICIES[int(np.argmax(self.scores(messages)))]

    @classmethod
    def load(cls, path: str | Path) -> "IdentitySubrouteLinearClassifier":
        with np.load(Path(path).resolve(), allow_pickle=False) as payload:
            return cls(payload["weights"], payload["bias"])


__all__ = [
    "IDENTITY_SUBROUTE_CLASSIFIER_SCHEMA",
    "IDENTITY_SUBROUTE_FEATURE_DIMENSION",
    "IdentitySubrouteLinearClassifier",
    "identity_subroute_features",
]
