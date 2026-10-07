from __future__ import annotations

import hashlib
import json
from pathlib import Path

import sentencepiece as sentencepiece


CONTROL_PIECES = [
    "<system>",
    "<user>",
    "<assistant>",
    "<tool>",
    "<tool_call>",
    "<tool_result>",
]


def build_test_tokenizer(root: Path, *, vocab_size: int = 64) -> Path:
    """Create a real, tiny SentencePiece tokenizer for isolated pipeline tests."""
    root.mkdir(parents=True, exist_ok=True)
    corpus = root / "corpus.txt"
    corpus.write_text(
        "\n".join(
            [
                "Salty Steak learns from carefully prepared local text.",
                "The quick brown potato rests near the quiet shore.",
                "User questions and assistant answers form conversations.",
                "Training evaluation runtime generation tokenizer checkpoint.",
            ]
            * 40
        )
        + "\n",
        encoding="utf-8",
    )
    prefix = root / "trained"
    sentencepiece.SentencePieceTrainer.train(
        input=str(corpus),
        model_prefix=str(prefix),
        vocab_size=vocab_size,
        model_type="bpe",
        pad_id=0,
        bos_id=1,
        eos_id=2,
        unk_id=3,
        user_defined_symbols=CONTROL_PIECES,
        character_coverage=1.0,
        hard_vocab_limit=True,
    )
    model = root / "tokenizer.model"
    (root / "trained.model").replace(model)
    (root / "trained.vocab").unlink()
    corpus.unlink()
    model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()
    metadata = {
        "format": "salty-potato-tokenizer-test-v1",
        "vocab_size": vocab_size,
        "model_sha256": model_sha256,
        "fingerprint": hashlib.sha256(
            f"test:{model_sha256}:{vocab_size}".encode("ascii")
        ).hexdigest(),
        "special_token_ids": {"pad": 0, "bos": 1, "eos": 2, "unk": 3},
        "control_token_ids": {
            piece: index + 4 for index, piece in enumerate(CONTROL_PIECES)
        },
    }
    (root / "tokenizer.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return root
