"""Salty Steak Native Desktop AI Platform — credential reference boundary.

The model may know that an account exists and that Salty Steak can sign in with
it. It may never know the password.

That distinction is enforced here rather than requested politely. A
``CredentialReference`` is what travels through plans, prompts, task events and
the activity timeline: a name and a state, and nothing that could authenticate
anybody. The secret itself is resolved once, at the narrow point where a
capability actually needs it, and is never returned to the caller that asked
for the action.

The store underneath is the existing Windows DPAPI one, so secrets are already
protected for the current user before they touch the workspace.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..tooling.secrets import SecretStore, SecretStoreUnavailable

CREDENTIAL_SCHEMA = "salty-steak-credential-reference-v1"
CREDENTIAL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


SECRET_FIELDS = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "client_secret",
        "cookie",
        "cookies",
        "session",
        "session_id",
        "authorization",
        "private_key",
        "totp",
        "otp",
        "pin",
        "credential_value",
    }
)

REDACTED = "[redacted]"


class CredentialError(RuntimeError):
    """Raised when a credential cannot be referenced or resolved."""


@dataclass(frozen=True)
class CredentialReference:
    """A name for a secret, safe to put anywhere the secret must not go."""

    name: str
    service: str
    account: str
    kind: str = "password"

    def __post_init__(self) -> None:
        if not CREDENTIAL_NAME.fullmatch(self.name):
            raise CredentialError(
                "A credential name must be lowercase letters, digits, dot, dash "
                "or underscore"
            )
        if not str(self.service).strip() or not str(self.account).strip():
            raise CredentialError("A credential needs a service and an account")

    @property
    def storage_reference(self) -> str:
        return f"credential:{self.name}"

    def public(self, *, available: bool) -> dict[str, Any]:
        """What the model is allowed to see.

        Enough to reason with — this account exists, it can be used — and
        nothing that could be used without Salty Steak.
        """

        return {
            "schema": CREDENTIAL_SCHEMA,
            "credential": self.name,
            "service": self.service,


            "account": self.account,
            "kind": self.kind,
            "authentication": "available" if available else "not_configured",
            "secret_visible_to_model": False,
        }


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Remove secret-shaped values from anything on its way out of the runtime.

    Applied to observations, task events and world state before they can reach
    a prompt, a log or the activity timeline. A field named like a secret loses
    its value even when nobody remembered to think about it.
    """

    if _depth > 12:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if str(key).strip().casefold().replace("-", "_") in SECRET_FIELDS
                else redact(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    return value


class CredentialVault:
    """Own credential references and resolve secrets only at the point of use."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store
        self._references: dict[str, CredentialReference] = {}

    def register(self, reference: CredentialReference) -> dict[str, Any]:
        """Record that an account exists, without requiring its secret yet."""

        self._references[reference.name] = reference
        return reference.public(available=self.available(reference.name))

    def store_secret(self, name: str, secret: str) -> dict[str, Any]:
        """Persist a secret against a registered reference.

        The value is written straight to the operating system's protected
        store; it is not retained, echoed, or returned.
        """

        reference = self._require(name)
        if not isinstance(secret, str) or not secret:
            raise CredentialError("A credential secret must be non-empty text")
        try:
            self._store.set(reference.storage_reference, secret)
        except SecretStoreUnavailable as error:
            raise CredentialError(
                f"The operating system credential store is unavailable: {error}"
            ) from error
        return reference.public(available=True)

    def available(self, name: str) -> bool:
        reference = self._references.get(name)
        if reference is None:
            return False
        try:
            return self._store.get(reference.storage_reference) is not None
        except SecretStoreUnavailable:
            return False

    def forget(self, name: str) -> dict[str, Any]:
        reference = self._require(name)
        self._store.delete(reference.storage_reference)
        return reference.public(available=False)

    def describe(self, name: str) -> dict[str, Any]:
        return self._require(name).public(available=self.available(name))

    def catalogue(self) -> list[dict[str, Any]]:
        """Every known account, as the model is allowed to see them."""

        return [
            reference.public(available=self.available(reference.name))
            for reference in sorted(self._references.values(), key=lambda item: item.name)
        ]

    def resolve_for_use(self, name: str) -> str:
        """Return the secret itself.

        The only function in the runtime that does. Callers must hand the value
        straight to the mechanism that needs it and never place it in a result,
        an observation, an event, or a log line.
        """

        reference = self._require(name)
        try:
            secret = self._store.get(reference.storage_reference)
        except SecretStoreUnavailable as error:
            raise CredentialError(
                f"The operating system credential store is unavailable: {error}"
            ) from error
        if secret is None:
            raise CredentialError(
                f"No secret is stored for {name!r}. The user needs to sign in once."
            )
        return secret

    def _require(self, name: str) -> CredentialReference:
        reference = self._references.get(str(name))
        if reference is None:
            raise CredentialError(f"No credential is registered as {name!r}")
        return reference
