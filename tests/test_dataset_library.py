from __future__ import annotations

import json
from pathlib import Path

import numpy

from app.backend.database.control import Database
from app.backend.datasets.library import DatasetLibrary


def _encoder(text: str) -> list[int]:
    return [byte + 3 for byte in text.encode("utf-8")]


class _ConversationTokenizer:
    fingerprint = "f" * 64
    bos_id = 1
    eos_id = 2
    pad_id = 0
    roles = {"system": 10, "user": 11, "assistant": 12}
    vocab_size = 65536

    def encode(
        self, text: str, *, add_bos: bool = False, add_eos: bool = False
    ) -> list[int]:
        result = [20 + index for index, _word in enumerate(text.split())]
        if add_bos:
            result.insert(0, self.bos_id)
        if add_eos:
            result.append(self.eos_id)
        return result

    def decode(self, token_ids: list[int]) -> str:
        return " ".join(str(token) for token in token_ids)

    def conversation_example(
        self,
        messages: list[dict[str, str]],
        sequence_length: int,
        *,
        assistant_only_labels: bool = False,
    ) -> tuple[list[int], list[int]]:
        canonical = [dict(message) for message in messages]
        if canonical and canonical[0].get("role") == "system":
            content = str(canonical[0].get("content", "")).strip()
            canonical[0]["content"] = (
                f"You are Salty Steak.\n\n{content}"
                if content
                else "You are Salty Steak."
            )
        else:
            canonical.insert(
                0, {"role": "system", "content": "You are Salty Steak."}
            )
        tokens = [self.bos_id]
        labels = [-100] if assistant_only_labels else [self.bos_id]
        for message in canonical:
            role = str(message["role"])
            content_tokens = self.encode(
                str(message["content"]), add_eos=True
            )
            tokens.extend([self.roles[role], *content_tokens])
            labels.append(-100 if assistant_only_labels else self.roles[role])
            labels.extend(
                content_tokens
                if not assistant_only_labels or role == "assistant"
                else [-100] * len(content_tokens)
            )
        return tokens[:sequence_length], labels[:sequence_length]


def _library(tmp_path: Path) -> DatasetLibrary:
    tokenizer = _ConversationTokenizer()
    tokenizer_model = tmp_path / "tokenizer.model"
    tokenizer_model.write_bytes(b"test-tokenizer-contract-v1")
    return DatasetLibrary(
        Database(tmp_path / "control" / "salty-potato.db"),
        tmp_path / "prepared",
        encoder=_encoder,
        tokenizer=tokenizer,
        tokenizer_reference=str(tokenizer_model),
        tokenizer_fingerprint=tokenizer.fingerprint,
        bos_token_id=1,
        eos_token_id=2,
        pad_token_id=0,
        default_sequence_length=32,
        architectural_context_tokens=8192,
    )


def test_inspection_uses_real_samples_and_exact_mapping_preview(tmp_path: Path) -> None:
    source = tmp_path / "instructions.json"
    source.write_text(
        json.dumps(
            [
                {
                    "task": "Name the crop.",
                    "input": "A field of rice",
                    "answer": "Rice",
                },
                {
                    "task": "Name the crop.",
                    "input": "A field of wheat",
                    "answer": "Wheat",
                },
            ]
        ),
        encoding="utf-8",
    )
    library = _library(tmp_path)

    inspection = library.inspect_source(source)
    assert inspection["format"] == "json"
    assert inspection["encoding"] == "utf-8"
    assert inspection["record_estimate"] == 2
    assert inspection["samples"][0]["answer"] == "Rice"
    assert inspection["detected_fields"] == ["answer", "input", "task"]

    preview = library.preview_mapping(
        source,
        {
            "type": "instruction",
            "instruction": "task",
            "user_input": "input",
            "assistant_response": "answer",
        },
    )
    assert preview["formatted_samples"][0]["formatted_example"] == (
        "Instruction:\nName the crop.\n\n"
        "User input:\nA field of rice\n\n"
        "Assistant:\nRice"
    )


def test_validation_reports_blocking_and_warning_affected_counts(tmp_path: Path) -> None:
    source = tmp_path / "records.jsonl"
    rows = [
        {"prompt": "Hello", "response": "Hello back"},
        {"prompt": "Hello", "response": "Hello back"},
        {"prompt": "No answer", "response": ""},
    ]
    source.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    library = _library(tmp_path)
    dataset = library.add_dataset(
        source,
        name="Dialogue",
        language="English",
        purpose="Chat",
        mapping={
            "type": "instruction",
            "user_input": "prompt",
            "assistant_response": "response",
        },
    )
    assert dataset["record_count"] == 3

    summary = library.validate_dataset(dataset["id"])

    assert summary["status"] == "invalid"
    assert summary["record_count"] == 3
    assert summary["affected_counts"]["missing_assistant_response"] == 1
    assert summary["affected_counts"]["duplicate_record"] == 1
    assert summary["blocking_error_count"] == 1
    stored = library.get_dataset(dataset["id"])
    assert stored is not None
    assert stored["validation_status"] == "invalid"
    assert stored["prepared_status"] == "not_prepared"
    assert not stored["training_ready"]


def test_verified_preparation_and_checksum_staleness(tmp_path: Path) -> None:
    source = tmp_path / "training.csv"
    source.write_text(
        "prompt,response\n"
        "one,first answer\n"
        "two,second answer\n"
        "three,third answer\n"
        "four,fourth answer\n",
        encoding="utf-8",
    )
    library = _library(tmp_path)
    dataset = library.add_dataset(
        source,
        name="Number words",
        language="English",
        purpose="Instruction response",
        mapping={
            "type": "instruction",
            "user_input": "prompt",
            "assistant_response": "response",
        },
    )
    validation = library.validate_dataset(dataset["id"])
    assert validation["status"] == "valid"

    settings = {
        "sequence_length": 32,
        "training_split": 0.75,
        "validation_split": 0.25,
        "packing": False,
        "truncation": "right",
        "random_seed": 19,
        "bos": True,
        "eos": True,
        "padding": True,
        "cache_location": str(tmp_path / "prepared"),
    }
    preflight = library.preparation_preflight(dataset["id"], settings)
    assert preflight["read_only"] is True
    assert preflight["generated_record_count"] == 4
    assert preflight["accepted_record_count"] == 4
    assert preflight["rejected_record_count"] == 0
    assert preflight["target_token_coverage"] == 1.0

    prepared = library.prepare_dataset(dataset["id"], settings)

    assert prepared["verified"] is True
    assert prepared["train_record_count"] == 3
    assert prepared["validation_record_count"] == 1
    directory = Path(prepared["path"])
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_checksum"] == dataset["source_checksum"]
    assert manifest["splits"]["train"]["source_record_count"] == 3
    inputs = numpy.load(directory / "train-input-ids.npy", allow_pickle=False)
    lengths = numpy.load(directory / "train-lengths.npy", allow_pickle=False)
    assert inputs.dtype == numpy.int32
    assert inputs.shape == (3, 32)
    assert lengths.shape == (3,)
    assert all(1 < value <= 32 for value in lengths.tolist())
    assert library.verify_prepared(prepared["prepared_dataset_id"])["verified"]

    current = library.get_dataset(dataset["id"])
    assert current is not None
    assert current["training_ready"]
    assert current["token_count"] == prepared["token_count"]

    source.write_text(
        "prompt,response\n"
        "one,changed answer\n"
        "two,second answer\n"
        "three,third answer\n"
        "four,fourth answer\n",
        encoding="utf-8",
    )
    freshness = library.check_source_changed(dataset["id"])
    assert freshness["changed"]
    stale = library.get_dataset(dataset["id"])
    assert stale is not None
    assert stale["source_changed"]
    assert stale["validation_status"] == "stale"
    assert stale["prepared_status"] == "stale"
    assert not stale["training_ready"]
    assert library.list_prepared(dataset["id"])[0]["status"] == "stale"


def test_conversation_role_validation_uses_real_messages(tmp_path: Path) -> None:
    source = tmp_path / "conversation.jsonl"
    source.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "Question"},
                    {"role": "user", "content": "Second question"},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    library = _library(tmp_path)
    dataset = library.add_dataset(
        source,
        name="Broken conversation",
        language="English",
        purpose="Conversation",
        mapping={
            "type": "conversation",
            "conversation_messages": "messages",
        },
    )
    summary = library.validate_dataset(dataset["id"])
    assert summary["status"] == "invalid"
    assert summary["affected_counts"]["missing_assistant_response"] == 1
