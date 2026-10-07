from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.backend.database.control import Database
from app.backend.datasets.library import DatasetLibrary
from app.backend.datasets.oasst2 import OASST2AdapterError, reconstruct_oasst2
from app.backend.datasets.preparation import make_sequences, split_records, tokenize_records
from app.backend.datasets.prepared_cache import verify_directory, write_split
from app.backend.system.files import atomic_write_json
from app.backend.training.engine import PreparedExamples


def message(
    identifier: str,
    parent: str | None,
    role: str,
    text: str,
    *,
    tree: str = "tree-1",
    rank: int | None = None,
    language: str = "en",
    deleted: bool = False,
    review: bool = True,
) -> dict[str, object]:
    return {
        "message_id": identifier,
        "parent_id": parent,
        "message_tree_id": tree,
        "role": role,
        "text": text,
        "rank": rank,
        "lang": language,
        "deleted": deleted,
        "review_result": review,
    }


def nested_tree() -> dict[str, object]:
    root = message("root", None, "prompter", "Question")
    root["replies"] = [
        {
            **message("worse", "root", "assistant", "Worse", rank=2),
            "replies": [],
        },
        {
            **message("best", "root", "assistant", "Best", rank=0),
            "replies": [
                {
                    **message("follow", "best", "prompter", "Follow up"),
                    "replies": [
                        {
                            **message(
                                "final", "follow", "assistant", "Final", rank=0
                            ),
                            "replies": [],
                        }
                    ],
                }
            ],
        },
    ]
    return {
        "message_tree_id": "tree-1",
        "tree_state": "ready_for_export",
        "prompt": root,
    }


def test_nested_ranked_branch_is_deterministic_and_preserves_multi_turn_roles() -> None:
    branches = reconstruct_oasst2([nested_tree()], allowed_languages=["en"])
    assert len(branches) == 1
    branch = branches[0]
    assert branch.tree_id == "tree-1"
    assert branch.branch_id == "final"
    assert [item.role for item in branch.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [item.text for item in branch.messages] == [
        "Question",
        "Best",
        "Follow up",
        "Final",
    ]


def test_flat_one_turn_reconstruction_and_shared_prefix_policy() -> None:
    rows = [
        message("root", None, "prompter", "Question"),
        message("a", "root", "assistant", "A", rank=1),
        message("b", "root", "assistant", "B", rank=0),
    ]
    best = reconstruct_oasst2(rows)
    assert len(best) == 1
    assert best[0].branch_id == "b"
    all_branches = reconstruct_oasst2(rows, branch_policy="all_valid_leaves")
    assert [branch.branch_id for branch in all_branches] == ["b", "a"]


def test_equal_rank_prefers_deeper_branch_then_nested_quality() -> None:
    rows = [
        message("root", None, "prompter", "Question"),
        message("shallow", "root", "assistant", "Short", rank=0),
        message("deep-a", "root", "assistant", "First", rank=0),
        message("follow", "deep-a", "prompter", "Follow up"),
        message("deep-b", "follow", "assistant", "Deep", rank=0),
    ]
    assert reconstruct_oasst2(rows)[0].branch_id == "deep-b"

    low = message("low", "root", "assistant", "Low quality", rank=0)
    high = message("high", "root", "assistant", "High quality", rank=0)
    low["labels"] = {"quality": {"value": 0.25, "count": 4}}
    high["labels"] = {"quality": {"value": 0.9, "count": 4}}
    assert reconstruct_oasst2([rows[0], low, high])[0].branch_id == "high"


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (
            [
                message("root", None, "prompter", "Question"),
                message("missing", "unknown", "assistant", "Answer"),
            ],
            "DATASET_MISSING_PARENT",
        ),
        (
            [
                message("a", "b", "prompter", "Question"),
                message("b", "a", "assistant", "Answer"),
            ],
            "DATASET_TREE_CYCLE",
        ),
        (
            [
                message("root", None, "prompter", "Question"),
                message("root", "root", "assistant", "Answer"),
            ],
            "DATASET_DUPLICATE_MESSAGE_ID",
        ),
        (
            [
                message("root", None, "prompter", "Question"),
                message("bad", "root", "prompter", "Wrong role"),
            ],
            "DATASET_ROLE_INVALID",
        ),
    ],
)
def test_malformed_trees_have_stable_failures(
    rows: list[dict[str, object]], code: str
) -> None:
    with pytest.raises(OASST2AdapterError) as captured:
        reconstruct_oasst2(rows)
    assert captured.value.code == code


@pytest.mark.parametrize(
    "assistant",
    [
        message("answer", "root", "assistant", "Answer", deleted=True),
        message("answer", "root", "assistant", "Answer", review=False),
        message("answer", "root", "assistant", "Answer", language="es"),
    ],
)
def test_deleted_negative_and_wrong_language_branches_are_rejected(
    assistant: dict[str, object],
) -> None:
    rows = [message("root", None, "prompter", "Question"), assistant]
    with pytest.raises(OASST2AdapterError) as captured:
        reconstruct_oasst2(rows, allowed_languages=["en"])
    assert captured.value.code == "NO_VALID_ASSISTANT_TARGETS"


def test_empty_assistant_is_rejected_as_schema_invalid() -> None:
    rows = [
        message("root", None, "prompter", "Question"),
        message("answer", "root", "assistant", ""),
    ]
    with pytest.raises(OASST2AdapterError) as captured:
        reconstruct_oasst2(rows)
    assert captured.value.code == "DATASET_SCHEMA_INVALID"


class TinyConversationTokenizer:
    fingerprint = "f" * 64
    bos_id = 1
    eos_id = 2
    pad_id = 0
    roles = {"system": 10, "user": 11, "assistant": 12}
    vocab_size = 512

    def encode(
        self, text: str, *, add_bos: bool = False, add_eos: bool = False
    ) -> list[int]:
        result = [20 + byte for byte in text.encode("utf-8")]
        if add_bos:
            result.insert(0, self.bos_id)
        if add_eos:
            result.append(self.eos_id)
        return result

    def decode(self, token_ids: list[int]) -> str:
        return bytes(token - 20 for token in token_ids if token >= 20).decode(
            "utf-8"
        )

    def conversation_example(
        self,
        messages: list[dict[str, str]],
        sequence_length: int,
        *,
        assistant_only_labels: bool = False,
    ) -> tuple[list[int], list[int]]:
        canonical = [dict(item) for item in messages]
        if canonical and canonical[0].get("role") == "system":
            source = str(canonical[0].get("content", "")).strip()
            canonical[0]["content"] = (
                f"You are Salty Steak.\n\n{source}"
                if source
                else "You are Salty Steak."
            )
        else:
            canonical.insert(
                0, {"role": "system", "content": "You are Salty Steak."}
            )
        tokens = [self.bos_id]
        labels = [-100] if assistant_only_labels else [self.bos_id]
        for item in canonical:
            role_id = self.roles[item["role"]]
            content = self.encode(item["content"], add_eos=True)
            tokens.extend([role_id, *content])
            if assistant_only_labels:
                labels.append(-100)
                labels.extend(
                    content
                    if item["role"] == "assistant"
                    else [-100] * len(content)
                )
            else:
                labels.extend([role_id, *content])
        return tokens[:sequence_length], labels[:sequence_length]


def test_tree_split_isolation_and_assistant_only_mask(tmp_path: Path) -> None:
    tokenizer = TinyConversationTokenizer()
    library = DatasetLibrary(
        Database(tmp_path / "db.sqlite"),
        tmp_path / "prepared",
        encoder=tokenizer.encode,
        tokenizer=tokenizer,
        tokenizer_reference="tiny",
        tokenizer_fingerprint=tokenizer.fingerprint,
        bos_token_id=tokenizer.bos_id,
        eos_token_id=tokenizer.eos_id,
        pad_token_id=tokenizer.pad_id,
    )
    records = [
        {
            "formatted_text": f"tree {tree}",
            "messages": [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": f"Answer {branch}"},
            ],
            "assistant_only_labels": True,
            "group_id": tree,
            "character_count": 10,
            "provenance": {
                "source_record_index": index,
                "message_tree_id": tree,
                "branch_id": branch,
            },
        }
        for index, (tree, branch) in enumerate(
            [("tree-a", "a1"), ("tree-a", "a2"), ("tree-b", "b1"), ("tree-c", "c1")],
            start=1,
        )
    ]
    settings = {
        "random_seed": 7,
        "validation_split": 1 / 3,
        "sequence_length": 128,
        "packing": True,
        "truncation": "right",
        "bos": True,
        "eos": True,
        "padding": True,
    }
    train, validation = split_records(records, settings)
    assert {item["group_id"] for item in train}.isdisjoint(
        {item["group_id"] for item in validation}
    )
    tokenised = tokenize_records(
        library,
        train,
        settings,
        context=None,
        progress_offset=0,
        progress_total=len(train),
    )
    sequences = make_sequences(tokenised, settings)
    assert sequences
    for sequence in sequences:
        tokens = sequence["tokens"]
        labels = sequence["labels"]
        assert len(tokens) == len(labels)
        assert any(label != -100 for label in labels[1:])
        for index, token in enumerate(tokens):
            if token in {tokenizer.bos_id, tokenizer.roles["user"], tokenizer.roles["assistant"]}:
                assert labels[index] == -100

    directory = tmp_path / "artifact"
    directory.mkdir()
    split_manifest = write_split(
        directory,
        "train",
        sequences,
        len(train),
        settings,
        pad_token_id=tokenizer.pad_id,
    )
    empty_manifest = write_split(
        directory,
        "validation",
        [],
        0,
        settings,
        pad_token_id=tokenizer.pad_id,
    )
    manifest = {
        "format_version": 2,
        "tokenizer_fingerprint": tokenizer.fingerprint,
        "settings": settings,
        "splits": {"train": split_manifest, "validation": empty_manifest},
    }
    atomic_write_json(directory / "manifest.json", manifest)
    verification = verify_directory(
        directory, tokenizer_fingerprint=tokenizer.fingerprint
    )
    assert verification["verified"], verification
    assert not verification["training_eligible"]
    assert verification["contract_problems"]
    loaded = PreparedExamples(directory, "train")
    tokens, labels = loaded.item(0)
    assert tokens == sequences[0]["tokens"]
    assert labels == sequences[0]["labels"]
    stored = np.load(directory / "train-labels.npy", allow_pickle=False)
    assert stored.dtype == np.int32


def test_overlong_conversation_reports_truncation_and_preserves_target() -> None:
    record = {
        "tokens": [1, 11, *range(20, 40), 12, 99, 2],
        "labels": [-100] * 23 + [99, 2],
        "group_id": "tree",
        "character_count": 20,
        "assistant_only_labels": True,
        "turn_spans": [
            {"role": "user", "start": 1, "end": 22},
            {"role": "assistant", "start": 22, "end": 25},
        ],
        "provenance": {
            "source_record_index": 1,
            "message_tree_id": "tree",
            "branch_id": "answer",
        },
    }
    sequences = make_sequences(
        [record],
        {
            "sequence_length": 16,
            "packing": False,
            "truncation": "right",
        },
    )
    assert sequences[0]["provenance"]["segments"][0]["truncated"] is True
    assert any(label != -100 for label in sequences[0]["labels"][1:])
    assert sequences[0]["tokens"][0] == 1
    assert sequences[0]["tokens"][-3:] == [12, 99, 2]


def test_exact_semantic_duplicates_are_cogrouped_across_source_groups() -> None:
    records = [
        {
            "group_id": group,
            "semantic_digest": digest,
            "provenance": {"source_record_index": index},
        }
        for index, (group, digest) in enumerate(
            [
                ("tree-a", "same-answer"),
                ("tree-b", "same-answer"),
                ("tree-c", "unique-c"),
                ("tree-d", "unique-d"),
            ]
        )
    ]
    train, validation = split_records(
        records,
        {"random_seed": 7, "validation_split": 0.5},
    )
    train_digests = {item["semantic_digest"] for item in train}
    validation_digests = {item["semantic_digest"] for item in validation}
    assert train_digests.isdisjoint(validation_digests)
    duplicate_groups = {
        item["group_id"]
        for item in [*train, *validation]
        if item["semantic_digest"] == "same-answer"
    }
    assert duplicate_groups == {"tree-a", "tree-b"}
    assert (
        {item["split_group_id"] for item in train}
        .intersection({item["split_group_id"] for item in validation})
        == set()
    )
