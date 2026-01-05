"""The single configuration loader for the local Salty Steak application."""

from __future__ import annotations

import copy
import math
import os
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any


LEGACY_PROJECT_ROOTS = (Path(r"P:\Projects\Salty Potatoo Ai"),)


class ConfigError(ValueError):
    """Raised when the application configuration is missing or inconsistent."""


def _merge(base: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _merge(current, value)
        elif isinstance(current, dict) != isinstance(value, Mapping) and key in merged:
            raise ConfigError(f"Configuration type mismatch at {key!r}")
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Required configuration file does not exist: {path}")
        return {}
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read configuration file {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"Configuration root must be a TOML table: {path}")
    return document


@dataclass(frozen=True, slots=True)
class StoragePaths:
    """Resolved, absolute application storage paths."""

    project_root: Path
    workspace: Path
    database: Path
    datasets: Path
    prepared: Path
    tokenizer: Path
    versions: Path
    training: Path
    evaluations: Path
    runtime: Path
    conversations: Path
    cache: Path
    logs: Path

    def directories(self) -> tuple[Path, ...]:
        return (
            self.workspace,
            self.database.parent,
            self.datasets,
            self.prepared,
            self.tokenizer,
            self.versions,
            self.training,
            self.evaluations,
            self.runtime,
            self.conversations,
            self.cache,
            self.logs,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            field: str(getattr(self, field))
            for field in (
                "workspace",
                "database",
                "datasets",
                "prepared",
                "tokenizer",
                "versions",
                "training",
                "evaluations",
                "runtime",
                "conversations",
                "cache",
                "logs",
            )
        }


class AppConfig(Mapping[str, Any]):
    """Read-only merged configuration plus resolved storage paths."""

    def __init__(
        self,
        project_root: Path,
        values: dict[str, Any],
        defaults_path: Path,
        local_path: Path,
    ) -> None:
        self.project_root = project_root.resolve()
        self.defaults_path = defaults_path.resolve()
        self.local_path = local_path.resolve()
        self._plain_values = copy.deepcopy(values)
        self._values = _freeze(values)
        self.paths = self._resolve_storage_paths()
        self._validate()

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def section(self, name: str) -> Mapping[str, Any]:
        section = self._values.get(name)
        if not isinstance(section, Mapping):
            raise ConfigError(f"Missing configuration section [{name}]")
        return section

    def value(self, dotted_key: str, default: Any = None) -> Any:
        value: Any = self._values
        for part in dotted_key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value

    def resolve(self, configured_path: str | Path) -> Path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return resolve_relocated_project_path(path, self.project_root)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._plain_values)

    def _resolve_storage_paths(self) -> StoragePaths:
        raw = self.section("paths")
        required = (
            "workspace",
            "database",
            "datasets",
            "prepared",
            "tokenizer",
            "versions",
            "training",
            "evaluations",
            "runtime",
            "conversations",
            "cache",
            "logs",
        )
        missing = [name for name in required if not raw.get(name)]
        if missing:
            raise ConfigError(f"Missing storage paths: {', '.join(missing)}")
        resolved = {name: self.resolve(str(raw[name])) for name in required}
        return StoragePaths(project_root=self.project_root, **resolved)

    def _validate(self) -> None:
        dataset = self.section("datasets")
        train_split = float(dataset.get("training_split", 0))
        validation_split = float(dataset.get("validation_split", 0))
        if not math.isclose(train_split + validation_split, 1.0, abs_tol=1e-9):
            raise ConfigError("datasets.training_split and validation_split must total 1")
        if train_split <= 0 or validation_split < 0:
            raise ConfigError("Dataset splits must be non-negative with a positive training split")

        training = self.section("training")
        sequence_length = int(training.get("sequence_length", 0))
        context = int(self.section("model").get("architectural_context_tokens", 0))
        if sequence_length <= 0 or context <= 0 or sequence_length > context:
            raise ConfigError(
                "training.sequence_length must be positive and no greater than "
                "model.architectural_context_tokens"
            )

        generation = self.section("generation")
        budget = int(generation.get("conversation_token_budget", 0))
        reserved = int(generation.get("reserved_output_tokens", 0))
        maximum = int(generation.get("maximum_output_tokens", 0))
        if min(budget, reserved, maximum) < 0 or budget + reserved > context:
            raise ConfigError("Generation token budgets exceed the architectural context")
        if maximum > reserved:
            raise ConfigError("maximum_output_tokens cannot exceed reserved_output_tokens")

        if int(self.section("versions").get("completed_retention", 0)) != 2:
            raise ConfigError("versions.completed_retention must be exactly 2")
        if int(self.section("versions").get("recovery_retention", 0)) != 1:
            raise ConfigError("versions.recovery_retention must be exactly 1")


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_relocated_project_path(
    configured_path: str | Path,
    project_root: str | Path | None = None,
) -> Path:
    """Resolve immutable legacy paths against the relocated project when possible.

    Historical evidence remains byte-for-byte unchanged, so some documents still
    contain the original P-drive root. Prefer the same relative file under the
    active project root when that target exists. Unknown absolute paths and
    already-missing historical artifacts remain unchanged instead of being
    silently redirected to a non-existent file.
    """

    path = Path(configured_path).expanduser()
    root = Path(project_root).resolve() if project_root else default_project_root()
    if path.is_absolute():
        for legacy_root in LEGACY_PROJECT_ROOTS:
            try:
                relative = path.relative_to(legacy_root)
            except ValueError:
                continue
            relocated = (root / relative).resolve()
            if relocated.exists():
                return relocated
    return path.resolve()


def load_config(
    project_root: str | Path | None = None,
    *,
    defaults_path: str | Path | None = None,
    local_path: str | Path | None = None,
) -> AppConfig:
    """Load defaults and then overlay the machine-local TOML configuration."""

    root = Path(project_root).resolve() if project_root else default_project_root()
    defaults = (
        Path(defaults_path).resolve()
        if defaults_path
        else (root / "config" / "defaults.toml").resolve()
    )
    local = (
        Path(local_path).resolve()
        if local_path
        else (root / "config" / "local.toml").resolve()
    )
    values = _merge(_read_toml(defaults, required=True), _read_toml(local, required=False))
    workspace_override = os.environ.get("SALTY_POTATO_WORKSPACE")
    if workspace_override:
        workspace = Path(workspace_override).expanduser().resolve()
        values["paths"] = {
            "workspace": str(workspace),
            "database": str(workspace / "control" / "salty-potato.db"),
            "datasets": str(workspace / "datasets"),
            "prepared": str(workspace / "datasets" / "prepared"),
            "tokenizer": str(workspace / "tokenizer"),
            "versions": str(workspace / "versions"),
            "training": str(workspace / "training"),
            "evaluations": str(workspace / "evaluations"),
            "runtime": str(workspace / "runtime"),
            "conversations": str(workspace / "conversations"),
            "cache": str(workspace / "cache"),
            "logs": str(workspace / "logs"),
        }
    return AppConfig(root, values, defaults, local)


@lru_cache(maxsize=4)
def _cached_config(root: str) -> AppConfig:
    return load_config(root)


def get_config(project_root: str | Path | None = None) -> AppConfig:
    root = Path(project_root).resolve() if project_root else default_project_root()
    return _cached_config(str(root))


def clear_config_cache() -> None:
    _cached_config.cache_clear()
