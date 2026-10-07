from __future__ import annotations

import json

import pytest

from app.backend.automation.credentials import (
    CredentialError,
    CredentialReference,
    CredentialVault,
    redact,
)
from app.backend.chat.task_runtime import TaskContext, merge_world_state

SECRET = "correct-horse-battery-staple"


class _MemoryStore:
    """Stand-in for the DPAPI store; the boundary being tested is above it."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def set(self, reference: str, value: str) -> None:
        self._values[reference] = value

    def get(self, reference: str) -> str | None:
        return self._values.get(reference)

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)


@pytest.fixture()
def vault() -> CredentialVault:
    vault = CredentialVault(_MemoryStore())
    vault.register(
        CredentialReference(name="portal", service="Student Portal", account="a.hasan")
    )
    return vault


def test_the_model_learns_an_account_exists_and_nothing_more(vault) -> None:
    vault.store_secret("portal", SECRET)

    public = vault.describe("portal")

    assert public["account"] == "a.hasan"
    assert public["authentication"] == "available"
    assert public["secret_visible_to_model"] is False
    # The decisive check: nothing in what the model sees can authenticate.
    assert SECRET not in json.dumps(public)


def test_an_account_without_a_stored_secret_reports_itself_unconfigured(vault) -> None:
    assert vault.describe("portal")["authentication"] == "not_configured"
    assert vault.available("portal") is False

    with pytest.raises(CredentialError, match="needs to sign in once"):
        vault.resolve_for_use("portal")


def test_the_secret_is_only_reachable_through_the_one_named_call(vault) -> None:
    vault.store_secret("portal", SECRET)

    assert vault.resolve_for_use("portal") == SECRET
    # Every other way of asking about the credential must refuse to say it.
    for view in (vault.describe("portal"), *vault.catalogue()):
        assert SECRET not in json.dumps(view)


def test_forgetting_a_credential_removes_the_secret_but_keeps_the_account(vault) -> None:
    vault.store_secret("portal", SECRET)
    public = vault.forget("portal")

    assert public["authentication"] == "not_configured"
    with pytest.raises(CredentialError):
        vault.resolve_for_use("portal")


def test_an_unregistered_credential_is_refused(vault) -> None:
    with pytest.raises(CredentialError, match="No credential is registered"):
        vault.resolve_for_use("bank")


@pytest.mark.parametrize("name", ["Portal", "has space", "", "-leading", "x" * 65])
def test_credential_names_are_constrained(name: str) -> None:
    with pytest.raises(CredentialError):
        CredentialReference(name=name, service="s", account="a")


def test_secret_shaped_fields_are_stripped_wherever_they_appear() -> None:
    payload = {
        "url": "https://portal.example/login",
        "password": SECRET,
        "Refresh-Token": "rt_live_123",
        "headers": {"Authorization": "Bearer abc"},
        "cookies": [{"name": "sid", "value": "xyz"}],
        "steps": [{"api_key": "sk-1"}, {"note": "safe"}],
    }

    cleaned = redact(payload)

    assert SECRET not in json.dumps(cleaned)
    assert "rt_live_123" not in json.dumps(cleaned)
    assert "Bearer abc" not in json.dumps(cleaned)
    assert "sk-1" not in json.dumps(cleaned)
    # Everything that was not a secret survives untouched.
    assert cleaned["url"] == "https://portal.example/login"
    assert cleaned["steps"][1]["note"] == "safe"


def test_a_secret_cannot_survive_into_the_activity_timeline() -> None:
    context = TaskContext(goal="sign in to the portal")

    context.record_event("browser", command="set_value", password=SECRET)
    merge_world_state(context, {"records": {"session_id": "s-1", "user": "a.hasan"}})

    timeline = json.dumps(context.snapshot(include_events=True))
    assert SECRET not in timeline
    assert "s-1" not in timeline
    assert "a.hasan" in timeline
