"""Salty Steak Native Desktop AI Platform — deterministic mail connector.

A mail service with a local store behind it, implementing exactly the contract
a Gmail or Microsoft provider will implement.

This is not a mock. It is the same connector interface, the same declared
risks, the same pagination and the same batch semantics, with an in-memory
mailbox instead of a network. That is what makes it worth testing against: a
workflow proven here is proven against the shape of the real thing, and the
provider that replaces it changes nothing above this file.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .contract import (
    RISK_DESTRUCTIVE,
    RISK_READ,
    RISK_SEND_EXTERNAL,
    RISK_WRITE_LOCAL,
    SERVICE_MAIL,
    BaseConnector,
    ConnectorDescriptor,
    ConnectorError,
    OperationSpec,
)

MAX_PAGE_SIZE = 500


class LocalMailConnector(BaseConnector):
    """A mailbox that behaves like a service, including its awkward parts."""

    def __init__(
        self,
        descriptor: ConnectorDescriptor | None = None,
        *,
        vault: Any = None,
    ) -> None:
        super().__init__(
            descriptor
            or ConnectorDescriptor(
                connector_id="mail.local",
                service_type=SERVICE_MAIL,
                display_name="Mail",
                account="local@salty.test",
            ),
            vault=vault,
        )
        self._messages: dict[str, dict[str, Any]] = {}
        self._labels: dict[str, dict[str, Any]] = {}
        self._sent: list[dict[str, Any]] = []
        self._drafts: dict[str, dict[str, Any]] = {}



    def operations(self) -> Sequence[OperationSpec]:
        return (
            OperationSpec(
                name="search",
                summary="Search messages",
                risk=RISK_READ,
                arguments={
                    "query": "words to match in subject, sender or body",
                    "from": "sender address",
                    "label": "restrict to a label",
                    "unread": "true to match only unread",
                    "limit": "page size",
                    "cursor": "continue a previous page",
                },
                paginated=True,
            ),
            OperationSpec(
                name="list_labels", summary="List labels", risk=RISK_READ
            ),
            OperationSpec(
                name="get",
                summary="Read one message",
                risk=RISK_READ,
                arguments={"id": "message id"},
                required=("id",),
            ),
            OperationSpec(
                name="get_thread",
                summary="Read a whole thread",
                risk=RISK_READ,
                arguments={"thread_id": "thread id"},
                required=("thread_id",),
            ),
            OperationSpec(
                name="create_label",
                summary="Create a label",
                risk=RISK_WRITE_LOCAL,
                arguments={"name": "label name"},
                required=("name",),
                verify_with="list_labels",
            ),
            OperationSpec(
                name="apply_label",
                summary="Apply a label to messages",
                risk=RISK_WRITE_LOCAL,
                arguments={"ids": "message ids", "label": "label name"},
                required=("ids", "label"),
                batchable=True,
                verify_with="search",
            ),
            OperationSpec(
                name="remove_label",
                summary="Remove a label from messages",
                risk=RISK_WRITE_LOCAL,
                arguments={"ids": "message ids", "label": "label name"},
                required=("ids", "label"),
                batchable=True,
                verify_with="search",
            ),
            OperationSpec(
                name="archive",
                summary="Archive messages",
                risk=RISK_WRITE_LOCAL,
                arguments={"ids": "message ids"},
                required=("ids",),
                batchable=True,
                verify_with="search",
            ),
            OperationSpec(
                name="delete",
                summary="Delete messages permanently",


                risk=RISK_DESTRUCTIVE,
                arguments={"ids": "message ids"},
                required=("ids",),
                batchable=True,
                verify_with="search",
            ),
            OperationSpec(
                name="create_draft",
                summary="Save a draft",
                risk=RISK_WRITE_LOCAL,
                arguments={"to": "recipient", "subject": "subject", "body": "body"},
                required=("to", "subject"),
            ),
            OperationSpec(
                name="send",
                summary="Send a message",

                risk=RISK_SEND_EXTERNAL,
                arguments={"to": "recipient", "subject": "subject", "body": "body"},
                required=("to", "subject"),
                verify_with="list_sent",
            ),
            OperationSpec(name="list_sent", summary="List sent mail", risk=RISK_READ),
        )



    def seed(self, messages: Sequence[Mapping[str, Any]]) -> int:
        for message in messages:
            record = {
                "id": message.get("id") or f"msg-{uuid.uuid4().hex[:10]}",
                "thread_id": message.get("thread_id") or f"thr-{uuid.uuid4().hex[:8]}",
                "from": message.get("from", "unknown@example.com"),
                "to": message.get("to", self.descriptor.account),
                "subject": message.get("subject", ""),
                "body": message.get("body", ""),
                "labels": list(message.get("labels") or []),
                "unread": bool(message.get("unread", False)),
                "archived": bool(message.get("archived", False)),
                "received_at": float(message.get("received_at", time.time())),
            }
            self._messages[record["id"]] = record
            for label in record["labels"]:
                self._labels.setdefault(label, {"name": label, "system": True})
        return len(self._messages)

    @property
    def message_count(self) -> int:
        return len(self._messages)



    def op_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        matches = [
            message
            for message in self._messages.values()
            if self._matches(message, arguments)
        ]


        matches.sort(key=lambda item: (-item["received_at"], item["id"]))

        limit = max(1, min(int(arguments.get("limit") or 100), MAX_PAGE_SIZE))
        start = self._cursor_offset(arguments.get("cursor"))
        window = matches[start : start + limit]
        next_offset = start + limit
        return {
            "items": [self._summary(message) for message in window],
            "next_cursor": f"offset:{next_offset}" if next_offset < len(matches) else None,
            "total_estimate": len(matches),
        }

    def op_list_labels(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"items": sorted(self._labels.values(), key=lambda item: item["name"])}

    def op_get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"message": dict(self._require(arguments.get("id")))}

    def op_get_thread(self, arguments: dict[str, Any]) -> dict[str, Any]:
        thread_id = arguments.get("thread_id")
        messages = [
            self._summary(message)
            for message in self._messages.values()
            if message["thread_id"] == thread_id
        ]
        if not messages:
            raise ConnectorError(f"No thread {thread_id!r}", kind="not_found")
        messages.sort(key=lambda item: item["received_at"])
        return {"thread_id": thread_id, "items": messages, "count": len(messages)}

    def op_list_sent(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {"items": list(self._sent), "count": len(self._sent)}



    def op_create_label(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = str(arguments.get("name") or "").strip()
        if not name:
            raise ConnectorError("A label needs a name.", kind="invalid_request")
        created = name not in self._labels
        self._labels[name] = {"name": name, "system": False}
        return {"label": name, "created": created}

    def op_apply_label(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._label_change(arguments, add=True)

    def op_remove_label(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._label_change(arguments, add=False)

    def op_archive(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._per_item(arguments, lambda message: message.update(archived=True))

    def op_delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        succeeded, failed = [], []
        for identifier in self._ids(arguments):
            if identifier in self._messages:
                del self._messages[identifier]
                succeeded.append(identifier)
            else:
                failed.append({"id": identifier, "error": "no such message"})
        return {"succeeded": succeeded, "failed": failed}

    def op_create_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        draft_id = f"draft-{uuid.uuid4().hex[:8]}"
        self._drafts[draft_id] = {
            "id": draft_id,
            "to": arguments.get("to"),
            "subject": arguments.get("subject"),
            "body": arguments.get("body", ""),
        }
        return {"draft": draft_id, **self._drafts[draft_id]}

    def op_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        message = {
            "id": f"sent-{uuid.uuid4().hex[:8]}",
            "to": arguments.get("to"),
            "subject": arguments.get("subject"),
            "body": arguments.get("body", ""),
            "sent_at": time.time(),
        }
        self._sent.append(message)
        return dict(message)



    def _label_change(self, arguments: dict[str, Any], *, add: bool) -> dict[str, Any]:
        label = str(arguments.get("label") or "").strip()
        if not label:
            raise ConnectorError("A label is required.", kind="invalid_request")
        if add and label not in self._labels:
            raise ConnectorError(
                f"There is no label called {label!r}. Create it first.",
                kind="not_found",
            )

        def change(message: dict[str, Any]) -> None:
            labels = set(message["labels"])
            labels.add(label) if add else labels.discard(label)
            message["labels"] = sorted(labels)

        return self._per_item(arguments, change)

    def _per_item(self, arguments: dict[str, Any], change) -> dict[str, Any]:
        succeeded, failed = [], []
        for identifier in self._ids(arguments):
            message = self._messages.get(identifier)
            if message is None:
                failed.append({"id": identifier, "error": "no such message"})
                continue
            change(message)
            succeeded.append(identifier)
        return {"succeeded": succeeded, "failed": failed}

    @staticmethod
    def _ids(arguments: Mapping[str, Any]) -> list[str]:
        raw = arguments.get("ids") or arguments.get("id")
        if isinstance(raw, str):
            return [raw]
        if not raw:
            raise ConnectorError("No messages were named.", kind="invalid_request")
        return [str(item) for item in raw]

    def _require(self, identifier: Any) -> dict[str, Any]:
        message = self._messages.get(str(identifier))
        if message is None:
            raise ConnectorError(f"No message {identifier!r}", kind="not_found")
        return message

    @staticmethod
    def _cursor_offset(cursor: Any) -> int:
        if not cursor:
            return 0
        text = str(cursor)
        if not text.startswith("offset:"):
            raise ConnectorError(f"Unusable cursor {cursor!r}", kind="invalid_request")
        return max(0, int(text.split(":", 1)[1]))

    @staticmethod
    def _summary(message: Mapping[str, Any]) -> dict[str, Any]:


        return {
            "id": message["id"],
            "thread_id": message["thread_id"],
            "from": message["from"],
            "subject": message["subject"],
            "labels": list(message["labels"]),
            "unread": message["unread"],
            "archived": message["archived"],
            "received_at": message["received_at"],
        }

    def _matches(self, message: Mapping[str, Any], arguments: Mapping[str, Any]) -> bool:
        sender = str(arguments.get("from") or "").casefold()
        if sender and sender not in str(message["from"]).casefold():
            return False
        label = arguments.get("label")
        if label and label not in message["labels"]:
            return False
        if arguments.get("unread") is True and not message["unread"]:
            return False
        if arguments.get("archived") is False and message["archived"]:
            return False
        query = str(arguments.get("query") or "").strip()
        if query:
            query_sender = ""
            query_subject = ""

            def take_field(match: re.Match[str]) -> str:
                nonlocal query_sender, query_subject
                field = match.group("field").casefold()
                value = (match.group("quoted") or match.group("plain") or "").strip()
                if field == "from":
                    query_sender = value
                else:
                    query_subject = value
                return " "

            remaining = re.sub(
                r'(?P<field>from|subject):(?:"(?P<quoted>[^"]+)"|(?P<plain>\S+))',
                take_field,
                query,
                flags=re.IGNORECASE,
            )
            if query_sender and query_sender.casefold() not in str(message["from"]).casefold():
                return False
            if query_subject and query_subject.casefold() not in str(message["subject"]).casefold():
                return False
            haystack = " ".join(
                [message["subject"], message["body"], message["from"]]
            ).casefold()


            words = [
                word
                for word in re.findall(r"[\w']+", remaining.casefold())
                if word not in {"and"}
            ]
            if not all(word in haystack for word in words):
                return False
        return True


__all__ = ["LocalMailConnector"]
