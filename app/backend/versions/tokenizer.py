"""SentencePiece tokenizer and token-budgeted conversation formatting."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import sentencepiece as sentencepiece

from ..system.files import sha256_file


DEFAULT_SYSTEM_PREFIX = "You are Salty Steak."
CHAT_TEMPLATE_VERSION = "salty-role-control-v1"


def canonical_chat_messages(
    messages: list[dict[str, str]],
    *,
    system_prefix: str = DEFAULT_SYSTEM_PREFIX,
) -> list[dict[str, str]]:
    """Apply the one training/evaluation/inference system-message policy."""

    normalised = [dict(message) for message in messages]
    if normalised and normalised[0].get("role") == "system":
        source_system = str(normalised[0].get("content", ""))
        normalised[0]["content"] = (
            f"{system_prefix}\n\n{source_system}"
            if source_system.strip()
            else system_prefix
        )
    else:
        normalised.insert(
            0, {"role": "system", "content": str(system_prefix)}
        )
    return normalised


@dataclass(frozen=True)
class PromptBudget:
    token_ids: list[int]
    context_tokens: int
    context_limit: int
    omitted_turns: int
    omitted_tokens: int


class SaltyTokenizer:
    def __init__(self, directory: str | Path) -> None:
        root = Path(directory)
        self.root = root
        self.model_path = root / "tokenizer.model"
        self.metadata_path = root / "tokenizer.json"
        if not self.model_path.is_file() or not self.metadata_path.is_file():
            raise FileNotFoundError(f"incomplete tokenizer at {root}")
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        self.processor = sentencepiece.SentencePieceProcessor(
            model_file=str(self.model_path)
        )
        self._verify()

    def _verify(self) -> None:
        expected_hash = self.metadata.get("model_sha256")
        if expected_hash and sha256_file(self.model_path) != expected_hash:
            raise ValueError("tokenizer model checksum does not match its metadata")
        if self.processor.get_piece_size() != int(self.metadata["vocab_size"]):
            raise ValueError("tokenizer vocabulary size is incompatible")
        for piece, expected_id in self.metadata["control_token_ids"].items():
            if self.processor.piece_to_id(piece) != int(expected_id):
                raise ValueError(f"tokenizer control token is incompatible: {piece}")
        special = self.metadata["special_token_ids"]
        actual = {
            "pad": self.processor.pad_id(),
            "bos": self.processor.bos_id(),
            "eos": self.processor.eos_id(),
            "unk": self.processor.unk_id(),
        }
        if actual != {key: int(value) for key, value in special.items()}:
            raise ValueError("tokenizer special-token mapping is incompatible")

    @property
    def vocab_size(self) -> int:
        return self.processor.get_piece_size()

    @property
    def bos_id(self) -> int:
        return self.processor.bos_id()

    @property
    def eos_id(self) -> int:
        return self.processor.eos_id()

    @property
    def pad_id(self) -> int:
        return self.processor.pad_id()

    @property
    def fingerprint(self) -> str:
        return str(self.metadata["fingerprint"])

    def role_id(self, role: str) -> int:
        piece = {
            "system": "<system>",
            "user": "<user>",
            "assistant": "<assistant>",
            "tool": "<tool>",
            "tool_call": "<tool_call>",
            "tool_result": "<tool_result>",
        }.get(role)
        if piece is None:
            raise ValueError(f"unsupported conversation role: {role}")
        return int(self.metadata["control_token_ids"][piece])

    def encode(
        self, text: str, *, add_bos: bool = False, add_eos: bool = False
    ) -> list[int]:
        token_ids = list(self.processor.encode(str(text), out_type=int))
        if add_bos:
            token_ids.insert(0, self.bos_id)
        if add_eos:
            token_ids.append(self.eos_id)
        return token_ids

    def decode(self, token_ids: Iterable[int]) -> str:
        return self.processor.decode([int(token) for token in token_ids])

    def conversation_example(
        self,
        messages: list[dict[str, str]],
        sequence_length: int,
        *,
        assistant_only_labels: bool = False,
        system_prefix: str = DEFAULT_SYSTEM_PREFIX,
    ) -> tuple[list[int], list[int]]:
        normalised = canonical_chat_messages(
            messages, system_prefix=system_prefix
        )
        tokens = [self.bos_id]
        labels = [-100] if assistant_only_labels else [self.bos_id]
        for message in normalised:
            role = str(message["role"])
            content = self.encode(str(message["content"]), add_eos=True)
            tokens.append(self.role_id(role))
            tokens.extend(content)
            if assistant_only_labels:
                labels.append(-100)
                if role == "assistant":
                    labels.extend(content)
                else:
                    labels.extend([-100] * len(content))
            else:
                labels.append(self.role_id(role))
                labels.extend(content)
        return tokens[:sequence_length], labels[:sequence_length]

    def build_generation_prompt(
        self,
        messages: list[dict[str, str]],
        *,
        context_limit: int,
        reserved_output_tokens: int,
        system_prefix: str = DEFAULT_SYSTEM_PREFIX,
    ) -> PromptBudget:
        if not messages or messages[-1].get("role") != "user":
            raise ValueError("a generation prompt must end with the current user message")
        available = int(context_limit) - int(reserved_output_tokens)
        if available < 32:
            raise ValueError("reserved output leaves no usable conversation context")

        def render(message: dict[str, str]) -> list[int]:
            return [
                self.role_id(str(message["role"])),
                *self.encode(str(message["content"]), add_eos=True),
            ]

        normalised = canonical_chat_messages(
            messages, system_prefix=system_prefix
        )
        prefix = [self.bos_id, *render(normalised[0])]
        marker = [self.role_id("assistant")]
        current = render(normalised[-1])
        if len(prefix) + len(current) + len(marker) > available:
            raise ValueError(
                "the current message is too long for this saved version's context"
            )
        selected: list[tuple[dict[str, str], list[int]]] = [
            (messages[-1], current)
        ]
        used = len(prefix) + len(current) + len(marker)
        omitted = 0
        omitted_tokens = 0
        prior = [
            (message, render(message)) for message in normalised[1:-1]
        ]
        for reverse_index, (message, encoded) in enumerate(reversed(prior)):
            if used + len(encoded) <= available:
                selected.append((message, encoded))
                used += len(encoded)
            else:
                omitted_items = prior[: len(prior) - reverse_index]
                omitted += len(omitted_items)
                omitted_tokens += sum(
                    len(item_tokens) for _item, item_tokens in omitted_items
                )
                break
        selected.reverse()
        while len(selected) > 1 and selected[0][0].get("role") == "assistant":
            _message, removed_tokens = selected.pop(0)
            omitted += 1
            omitted_tokens += len(removed_tokens)
        prompt = prefix
        for _message, item in selected:
            prompt.extend(item)
        prompt.extend(marker)
        return PromptBudget(
            token_ids=prompt,
            context_tokens=len(prompt),
            context_limit=int(context_limit),
            omitted_turns=omitted,
            omitted_tokens=omitted_tokens,
        )
