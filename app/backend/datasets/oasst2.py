"""Deterministic reconstruction of OASST2 conversation trees.

The public adapter accepts both the nested ``*.trees.jsonl`` export and flat
message rows.  It deliberately selects one ranked root-to-leaf branch per tree
by default so shared prefixes are not multiplied without an explicit policy.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any


ADAPTER_VERSION = "oasst2-tree-adapter-v1"
DEFAULT_BRANCH_POLICY = "best_ranked_leaf"


class OASST2AdapterError(ValueError):
    """A stable, record-oriented OASST2 reconstruction failure."""

    def __init__(self, code: str, message: str, *, tree_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.tree_id = tree_id


@dataclass(frozen=True, slots=True)
class OASST2Message:
    message_id: str
    parent_id: str | None
    message_tree_id: str
    role: str
    text: str
    language: str | None
    rank: int | None
    review_result: bool | None
    deleted: bool
    quality: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OASST2Branch:
    tree_id: str
    branch_id: str
    messages: tuple[OASST2Message, ...]
    branch_policy: str
    source_layout: str
    source_record_indices: tuple[int, ...]

    def chat_messages(self) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.text}
            for message in self.messages
        ]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["chat_messages"] = self.chat_messages()
        return result


def _required_text(value: Any, field: str, *, tree_id: str | None = None) -> str:
    text = str(value or "")
    if not text.strip():
        raise OASST2AdapterError(
            "DATASET_SCHEMA_INVALID",
            f"OASST2 field {field} is missing or empty",
            tree_id=tree_id,
        )
    return text


def _role(value: Any, *, tree_id: str | None = None) -> str:
    role = str(value or "").strip().casefold()
    if role == "prompter":
        return "user"
    if role == "assistant":
        return role
    raise OASST2AdapterError(
        "DATASET_ROLE_INVALID",
        f"Unsupported OASST2 role: {value!r}",
        tree_id=tree_id,
    )


def _optional_rank(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OASST2AdapterError(
            "DATASET_SCHEMA_INVALID", f"Invalid OASST2 rank: {value!r}"
        ) from exc


def _optional_review(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = str(value).strip().casefold()
    if token in {"true", "pass", "positive", "1"}:
        return True
    if token in {"false", "fail", "negative", "0"}:
        return False
    raise OASST2AdapterError(
        "DATASET_SCHEMA_INVALID", f"Invalid OASST2 review result: {value!r}"
    )


def _strict_bool(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    token = str(value).strip().casefold()
    if token in {"true", "1", "yes"}:
        return True
    if token in {"false", "0", "no", ""}:
        return False
    raise OASST2AdapterError(
        "DATASET_SCHEMA_INVALID",
        f"Invalid OASST2 boolean {field}: {value!r}",
    )


def _message(
    raw: Mapping[str, Any],
    *,
    fallback_tree_id: str | None = None,
    fallback_parent_id: str | None = None,
) -> OASST2Message:
    explicit_tree_id = raw.get("message_tree_id")
    tree_id = _required_text(
        explicit_tree_id or fallback_tree_id,
        "message_tree_id",
        tree_id=fallback_tree_id,
    )
    if (
        explicit_tree_id not in (None, "")
        and fallback_tree_id is not None
        and str(explicit_tree_id).strip() != str(fallback_tree_id).strip()
    ):
        raise OASST2AdapterError(
            "DATASET_TREE_RECONSTRUCTION_FAILED",
            "Nested OASST2 message tree ID differs from its parent tree",
            tree_id=str(fallback_tree_id),
        )
    message_id = _required_text(raw.get("message_id"), "message_id", tree_id=tree_id)
    parent_value = raw.get("parent_id", fallback_parent_id)
    parent_id = str(parent_value).strip() if parent_value not in (None, "") else None
    text = _required_text(raw.get("text"), "text", tree_id=tree_id)
    language_value = raw.get("lang", raw.get("language"))
    language = (
        str(language_value).strip().casefold()
        if language_value not in (None, "")
        else None
    )
    known = {
        "message_id",
        "parent_id",
        "message_tree_id",
        "role",
        "text",
        "lang",
        "language",
        "rank",
        "review_result",
        "deleted",
        "replies",
        "labels",
    }
    quality: dict[str, Any] = {
        str(key): value
        for key, value in raw.items()
        if key not in known
        and (
            "quality" in str(key).casefold()
            or "score" in str(key).casefold()
            or "label" in str(key).casefold()
        )
    }
    labels = raw.get("labels")
    if isinstance(labels, Mapping):
        for label_name, label_payload in labels.items():
            if (
                isinstance(label_payload, Mapping)
                and isinstance(label_payload.get("value"), (int, float))
            ):
                quality[f"label.{label_name}"] = float(
                    label_payload["value"]
                )
    return OASST2Message(
        message_id=message_id,
        parent_id=parent_id,
        message_tree_id=tree_id,
        role=_role(raw.get("role"), tree_id=tree_id),
        text=text,
        language=language,
        rank=_optional_rank(raw.get("rank")),
        review_result=_optional_review(raw.get("review_result")),
        deleted=_strict_bool(raw.get("deleted"), "deleted"),
        quality=quality,
    )


def _flatten_nested(
    raw: Mapping[str, Any],
    *,
    tree_id: str,
    parent_id: str | None,
    output: list[OASST2Message],
) -> None:
    message = _message(
        raw,
        fallback_tree_id=tree_id,
        fallback_parent_id=parent_id,
    )
    if parent_id is not None and message.parent_id != parent_id:
        raise OASST2AdapterError(
            "DATASET_TREE_RECONSTRUCTION_FAILED",
            f"Nested OASST2 message {message.message_id} declares parent "
            f"{message.parent_id!r}, expected {parent_id!r}",
            tree_id=tree_id,
        )
    output.append(message)
    replies = raw.get("replies", [])
    if replies is None:
        replies = []
    if not isinstance(replies, Sequence) or isinstance(
        replies, (str, bytes, bytearray)
    ):
        raise OASST2AdapterError(
            "DATASET_SCHEMA_INVALID",
            "OASST2 replies must be a list",
            tree_id=tree_id,
        )
    for reply in replies:
        if not isinstance(reply, Mapping):
            raise OASST2AdapterError(
                "DATASET_SCHEMA_INVALID",
                "OASST2 reply must be an object",
                tree_id=tree_id,
            )
        _flatten_nested(
            reply,
            tree_id=tree_id,
            parent_id=message.message_id,
            output=output,
        )


def _normalise_languages(languages: Iterable[str] | None) -> frozenset[str] | None:
    if languages is None:
        return None
    values = frozenset(
        str(language).strip().casefold()
        for language in languages
        if str(language).strip()
    )
    return values or None


def _branch_score(branch: Sequence[OASST2Message]) -> tuple[Any, ...]:
    """Lower scores win under the documented rank/depth/quality policy."""

    ranks = tuple(
        message.rank if message.rank is not None else 1_000_000
        for message in branch
        if message.role == "assistant"
    )
    preferred_quality = [
        float(value)
        for message in branch
        for key, value in sorted(message.quality.items())
        if key.casefold()
        in {"label.quality", "label.helpfulness", "quality_score"}
        and isinstance(value, (int, float))
    ]
    mean_quality = (
        sum(preferred_quality) / len(preferred_quality)
        if preferred_quality
        else -1.0
    )
    return (
        sum(ranks),
        max(ranks, default=1_000_000),
        -len(ranks),
        -mean_quality,
        -len(branch),
        branch[-1].message_id,
    )


def _valid_branches(
    messages: Sequence[OASST2Message],
    *,
    allowed_languages: frozenset[str] | None,
    include_negative_reviews: bool,
) -> list[tuple[OASST2Message, ...]]:
    if not messages:
        return []
    tree_id = messages[0].message_tree_id
    by_id: dict[str, OASST2Message] = {}
    children: dict[str, list[OASST2Message]] = defaultdict(list)
    roots: list[OASST2Message] = []
    for message in messages:
        if message.message_tree_id != tree_id:
            raise OASST2AdapterError(
                "DATASET_TREE_RECONSTRUCTION_FAILED",
                "Messages from different OASST2 trees were mixed",
                tree_id=tree_id,
            )
        if message.message_id in by_id:
            raise OASST2AdapterError(
                "DATASET_DUPLICATE_MESSAGE_ID",
                f"Duplicate OASST2 message ID: {message.message_id}",
                tree_id=tree_id,
            )
        by_id[message.message_id] = message
    for message in messages:
        if message.parent_id is None:
            roots.append(message)
        elif message.parent_id not in by_id:
            raise OASST2AdapterError(
                "DATASET_MISSING_PARENT",
                f"Missing parent {message.parent_id} for {message.message_id}",
                tree_id=tree_id,
            )
        else:
            children[message.parent_id].append(message)
    for message in messages:
        visited: set[str] = set()
        cursor: OASST2Message | None = message
        while cursor is not None:
            if cursor.message_id in visited:
                raise OASST2AdapterError(
                    "DATASET_TREE_CYCLE",
                    f"Cycle detected at {cursor.message_id}",
                    tree_id=tree_id,
                )
            visited.add(cursor.message_id)
            cursor = by_id.get(cursor.parent_id) if cursor.parent_id else None
    if len(roots) != 1:
        raise OASST2AdapterError(
            "DATASET_TREE_RECONSTRUCTION_FAILED",
            f"OASST2 tree requires exactly one root; found {len(roots)}",
            tree_id=tree_id,
        )

    root = roots[0]
    if root.role != "user":
        raise OASST2AdapterError(
            "DATASET_ROLE_INVALID",
            "OASST2 tree root must be a prompter/user message",
            tree_id=tree_id,
        )

    branches: list[tuple[OASST2Message, ...]] = []

    def walk(
        current: OASST2Message,
        path: tuple[OASST2Message, ...],
        active: frozenset[str],
    ) -> bool:
        if current.message_id in active:
            raise OASST2AdapterError(
                "DATASET_TREE_CYCLE",
                f"Cycle detected at {current.message_id}",
                tree_id=tree_id,
            )
        expected = "user" if len(path) % 2 == 0 else "assistant"
        if current.role != expected:
            raise OASST2AdapterError(
                "DATASET_ROLE_INVALID",
                f"Expected {expected}, found {current.role}",
                tree_id=tree_id,
            )
        next_path = (*path, current)
        next_active = frozenset((*active, current.message_id))
        visible_children = sorted(
            children.get(current.message_id, []),
            key=lambda item: (
                item.rank if item.rank is not None else 1_000_000,
                item.message_id,
            ),
        )
        descendant_assistant = False
        for child in visible_children:
            descendant_assistant = (
                walk(child, next_path, next_active) or descendant_assistant
            )



        if current.role == "assistant" and not descendant_assistant:
            branches.append(next_path)
            return True
        return descendant_assistant

    walk(root, (), frozenset())
    accepted: list[tuple[OASST2Message, ...]] = []
    for branch in branches:
        if any(message.deleted for message in branch):
            continue
        if (
            not include_negative_reviews
            and any(message.review_result is False for message in branch)
        ):
            continue
        if allowed_languages is not None and any(
            message.language not in allowed_languages for message in branch
        ):
            continue
        if not any(
            message.role == "assistant" and message.text.strip() for message in branch
        ):
            continue
        accepted.append(branch)
    return accepted


def reconstruct_oasst2(
    records: Iterable[Mapping[str, Any]],
    *,
    allowed_languages: Iterable[str] | None = None,
    include_negative_reviews: bool = False,
    branch_policy: str = DEFAULT_BRANCH_POLICY,
    skip_rejected: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> list[OASST2Branch]:
    """Reconstruct and deterministically select branches from OASST2 records."""

    if branch_policy not in {"best_ranked_leaf", "all_valid_leaves"}:
        raise OASST2AdapterError(
            "DATASET_SCHEMA_INVALID",
            f"Unsupported OASST2 branch policy: {branch_policy}",
        )
    nested_trees: list[
        tuple[str, list[OASST2Message], tuple[int, ...], str]
    ] = []
    flat: dict[str, list[OASST2Message]] = defaultdict(list)
    flat_indices: dict[str, list[int]] = defaultdict(list)
    def reject(
        exc: OASST2AdapterError,
        *,
        record_index: int | None = None,
    ) -> bool:
        if not skip_rejected:
            raise exc
        if diagnostics is not None:
            diagnostics.append(
                {
                    "code": exc.code,
                    "message": str(exc),
                    "message_tree_id": exc.tree_id,
                    "source_record_index": record_index,
                }
            )
        return True

    for record_index, raw in enumerate(records, start=1):
        if not isinstance(raw, Mapping):
            reject(
                OASST2AdapterError(
                    "DATASET_SCHEMA_INVALID", "OASST2 record must be an object"
                ),
                record_index=record_index,
            )
            continue
        prompt = raw.get("prompt")
        tree_state = raw.get("tree_state")
        if tree_state not in (None, "", "ready_for_export"):
            reject(
                OASST2AdapterError(
                    "DATASET_TREE_STATE_INVALID",
                    f"OASST2 tree is not ready for export: {tree_state!r}",
                    tree_id=str(raw.get("message_tree_id") or "") or None,
                ),
                record_index=record_index,
            )
            continue
        if isinstance(prompt, Mapping):
            try:
                tree_id = _required_text(
                    raw.get("message_tree_id") or prompt.get("message_tree_id"),
                    "message_tree_id",
                )
                flattened: list[OASST2Message] = []
                _flatten_nested(
                    prompt,
                    tree_id=tree_id,
                    parent_id=None,
                    output=flattened,
                )
            except OASST2AdapterError as exc:
                reject(exc, record_index=record_index)
                continue
            nested_trees.append((tree_id, flattened, (record_index,), "nested"))
        else:
            try:
                message = _message(raw)
            except OASST2AdapterError as exc:
                reject(exc, record_index=record_index)
                continue
            flat[message.message_tree_id].append(message)
            flat_indices[message.message_tree_id].append(record_index)
    trees = [
        *nested_trees,
        *[
            (
                tree_id,
                messages,
                tuple(flat_indices[tree_id]),
                "flat",
            )
            for tree_id, messages in sorted(flat.items())
        ],
    ]
    globally_seen_messages: dict[str, str] = {}
    unique_trees: list[
        tuple[str, list[OASST2Message], tuple[int, ...], str]
    ] = []
    for tree_id, messages, source_indices, source_layout in trees:
        duplicate = next(
            (
                message.message_id
                for message in messages
                if message.message_id in globally_seen_messages
            ),
            None,
        )
        if duplicate is not None:
            reject(
                OASST2AdapterError(
                    "DATASET_DUPLICATE_MESSAGE_ID",
                    f"OASST2 message ID {duplicate} appears in both "
                    f"{globally_seen_messages[duplicate]} and {tree_id}",
                    tree_id=tree_id,
                ),
                record_index=source_indices[0] if source_indices else None,
            )
            continue
        for message in messages:
            globally_seen_messages[message.message_id] = tree_id
        unique_trees.append(
            (tree_id, messages, source_indices, source_layout)
        )
    trees = unique_trees
    languages = _normalise_languages(allowed_languages)
    result: list[OASST2Branch] = []
    seen_tree_ids: set[str] = set()
    for tree_id, messages, source_indices, source_layout in trees:
        if tree_id in seen_tree_ids:
            reject(
                OASST2AdapterError(
                    "DATASET_DUPLICATE_TREE",
                    f"OASST2 tree appears more than once: {tree_id}",
                    tree_id=tree_id,
                ),
                record_index=source_indices[0] if source_indices else None,
            )
            continue
        seen_tree_ids.add(tree_id)
        try:
            valid = _valid_branches(
                messages,
                allowed_languages=languages,
                include_negative_reviews=include_negative_reviews,
            )
        except OASST2AdapterError as exc:
            reject(
                exc,
                record_index=source_indices[0] if source_indices else None,
            )
            continue
        if not valid:
            reject(
                OASST2AdapterError(
                    "NO_VALID_ASSISTANT_TARGETS",
                    "OASST2 tree has no valid assistant-ending branch after filters",
                    tree_id=tree_id,
                ),
                record_index=source_indices[0] if source_indices else None,
            )
            continue
        selected = (
            sorted(valid, key=_branch_score)[:1]
            if branch_policy == "best_ranked_leaf"
            else sorted(valid, key=_branch_score)
        )
        for branch in selected:
            result.append(
                OASST2Branch(
                    tree_id=tree_id,
                    branch_id=branch[-1].message_id,
                    messages=branch,
                    branch_policy=branch_policy,
                    source_layout=source_layout,
                    source_record_indices=source_indices,
                )
            )
    return result


def reconstruct_oasst2_record(
    record: Mapping[str, Any],
    *,
    allowed_languages: Iterable[str] | None = None,
    include_negative_reviews: bool = False,
    branch_policy: str = DEFAULT_BRANCH_POLICY,
) -> OASST2Branch:
    branches = reconstruct_oasst2(
        [record],
        allowed_languages=allowed_languages,
        include_negative_reviews=include_negative_reviews,
        branch_policy=branch_policy,
    )
    if len(branches) != 1:
        raise OASST2AdapterError(
            "DATASET_TREE_RECONSTRUCTION_FAILED",
            "One OASST2 tree produced an ambiguous branch set",
        )
    return branches[0]
