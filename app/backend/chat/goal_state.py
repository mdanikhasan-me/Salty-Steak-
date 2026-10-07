"""Salty Steak Native Desktop AI Platform — what the user asked for, as predicates.

The goal is the specification. The steps are not.

A verifier assembled out of step evidence answers "did everything I happened to
do work?". That is a different question from "is what the user asked for now
true", and on the run that matters they disagree completely:

    delete run-a.log  -> succeeded, nothing left behind  -> verified
    delete notes.txt  -> succeeded, nothing left behind  -> verified
                                                         => Zonted

Both steps did exactly what they claimed. The user had said to keep
``notes.txt``, and nothing in that evidence represents it, because nothing ever
bound it. Summing verified steps cannot notice a requirement that was never
executed — and a requirement nobody acted on is precisely the one worth
checking.

So the required outcomes are declared from the request, before execution, and
checked afterwards by re-observing the world. Three rules hold the whole thing
up:

* a goal with no declared predicates is **never** verified;
* a predicate nobody could observe is **unknown**, and unknown is not success;
* one contradicted predicate outranks any number of satisfied ones.

Nothing here knows about log files, or about any particular product, site or
application. A predicate is a kind, a subject and an optional detail; the
observers that answer them live with the capability that can see that kind of
thing.
"""

from __future__ import annotations

import glob
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

GOAL_STATE_SCHEMA = "salty-steak-goal-state-v1"


@dataclass(frozen=True)
class Predicate:
    """One thing that must be true when the work is done.

    ``kind`` says what sort of claim it is, ``subject`` what it is about, and
    ``detail`` carries whatever that kind needs — a glob for a folder rule, a
    field name for an entity. Frozen so a spec cannot drift while it is being
    verified against.
    """

    kind: str
    subject: str
    detail: str = ""
    source: str = "requested"

    def describe(self) -> dict[str, str]:
        described = {"kind": self.kind, "subject": self.subject}
        if self.detail:
            described["detail"] = self.detail
        return described


@dataclass
class GoalSpec:
    """The user's goal, in a form something can check.

    ``protected`` is separate from ``required`` on the way in because it comes
    from a different part of the request — the half that says what must *not*
    change — and it has been the half that goes missing. It is folded into the
    required predicates immediately, so by verification time there is one list
    and no way to check the deletions while forgetting the preservations.
    """

    goal: str
    required: tuple[Predicate, ...] = ()
    current_turn_intent: str = ""
    constraints: tuple[str, ...] = ()
    protected_resources: tuple[str, ...] = ()
    permission_scope: str = ""
    unknowns: tuple[str, ...] = ()
    future_dependencies: tuple[str, ...] = ()
    stopping_conditions: tuple[str, ...] = ()
    notes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_outcomes(
        cls,
        *,
        goal: str,
        outcomes: Iterable[Mapping[str, Any]] = (),
        protected: Iterable[str] = (),
        constraints: Iterable[str] = (),
    ) -> "GoalSpec":
        required: list[Predicate] = []
        seen: set[tuple[str, str, str]] = set()

        def add(predicate: Predicate) -> None:
            key = (predicate.kind, predicate.subject, predicate.detail)
            if predicate.subject and key not in seen:
                seen.add(key)
                required.append(predicate)

        for outcome in outcomes or ():
            if not isinstance(outcome, Mapping):
                continue
            add(
                Predicate(
                    kind=str(outcome.get("kind") or "").strip().casefold(),
                    subject=str(outcome.get("target") or outcome.get("subject") or ""),
                    detail=str(outcome.get("detail") or outcome.get("pattern") or ""),
                )
            )




        for resource in protected or ():
            if str(resource).strip():
                add(Predicate("present", str(resource), source="protected"))

        return cls(
            goal=str(goal or ""),
            required=tuple(required),
            constraints=tuple(str(item) for item in (constraints or ()) if str(item)),
            protected_resources=tuple(
                str(item) for item in (protected or ()) if str(item).strip()
            ),
        )

    @classmethod
    def from_compilation(
        cls,
        payload: Mapping[str, Any],
        *,
        fallback_goal: str = "",
        permission_scope: str | None = None,
    ) -> "GoalSpec":
        """Build the operational objective emitted before execution.

        This accepts the current names and the original ``outcomes`` / ``protected``
        aliases so an older local-model reply degrades to fewer fields rather than
        losing every predicate. Protected resources become filesystem predicates
        only when they are absolute paths; other protected state remains explicit
        operational context until a suitable observer kind is supplied.
        """

        outcomes = payload.get("required_outcomes") or payload.get("outcomes") or []
        protected = payload.get("protected_resources") or payload.get("protected") or []
        protected_items = (
            tuple(str(item).strip() for item in protected if str(item).strip())
            if isinstance(protected, Sequence)
            and not isinstance(protected, (str, bytes))
            else ()
        )
        protected_paths = [item for item in protected_items if _looks_like_absolute_path(item)]
        base = cls.from_outcomes(
            goal=str(payload.get("objective") or fallback_goal),
            outcomes=(
                item for item in outcomes if isinstance(item, Mapping)
            ) if isinstance(outcomes, Sequence) and not isinstance(outcomes, (str, bytes)) else (),
            protected=protected_paths,
            constraints=_string_items(payload.get("constraints")),
        )
        return cls(
            goal=base.goal,
            required=base.required,
            current_turn_intent=str(payload.get("current_turn_intent") or "")[:400],
            constraints=base.constraints,
            protected_resources=protected_items,
            permission_scope=str(
                permission_scope
                if permission_scope is not None
                else payload.get("permission_scope") or ""
            )[:200],
            unknowns=_string_items(payload.get("unknowns")),
            future_dependencies=_string_items(payload.get("future_dependencies")),
            stopping_conditions=_string_items(payload.get("stopping_conditions")),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "schema": GOAL_STATE_SCHEMA,
            "goal": self.goal[:400],
            "current_turn_intent": self.current_turn_intent,
            "required": [predicate.describe() for predicate in self.required],
            "constraints": list(self.constraints),
            "protected_resources": list(self.protected_resources),
            "permission_scope": self.permission_scope,
            "unknowns": list(self.unknowns),
            "future_dependencies": list(self.future_dependencies),
            "stopping_conditions": list(self.stopping_conditions),
        }




Observer = Callable[[Predicate], "bool | None"]


def verify_predicates(
    spec: GoalSpec | None, observe: Observer
) -> tuple[bool | None, dict[str, Any]]:
    """Check every required predicate against the world as it is now.

    Re-observed rather than accumulated: the mechanism that made a change is
    never the thing that confirms it. A capability reporting success is a claim
    about a call, and this wants a claim about the world.
    """

    if spec is None or not spec.required:


        return None, {
            "reason": "no_required_predicates",
            "required_count": 0,
            "verified_count": 0,
        }

    verified: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    unverified: list[dict[str, str]] = []

    for predicate in spec.required:
        try:
            answer = observe(predicate)
        except Exception:

            answer = None
        described = predicate.describe()
        if answer is True:
            verified.append(described)
        elif answer is False:
            failed.append(described)
        else:
            unverified.append(described)

    evidence = {
        "required_count": len(spec.required),
        "verified_count": len(verified),
        "verified": verified,
        "failed": failed,
        "unverified": unverified,
    }




    if failed:
        return False, {**evidence, "reason": "a_required_predicate_is_false"}
    if unverified:
        return None, {**evidence, "reason": "a_required_predicate_was_not_observed"}
    return True, evidence


def is_concrete_filesystem_target(subject: object) -> bool:
    text = str(subject or "").strip()
    return bool(text) and Path(text).is_absolute() and not re.search(
        r"\$(?:\{[^}]+\}|[A-Za-z_]\w*)|%[^%]+%|\{\{[^}]+\}\}", text,
    )


def filesystem_observer() -> Observer:
    """Answers ``present``/``absent`` by looking at the disk right now.

    Deliberately not reading the capability's own report of what it did. The
    filesystem is the strongest truth available for a file goal, and asking it
    directly is what makes this a verification rather than a restatement.
    """

    def observe(predicate: Predicate) -> bool | None:
        if predicate.kind not in {"present", "absent"}:
            return None
        subject = str(predicate.subject or "").strip()
        if not subject:
            return None
        # A planner variable is not a filesystem location. Observing a literal
        # "$temp_file_path" used to certify deletion without checking the file.
        if not is_concrete_filesystem_target(subject):
            return None
        try:
            if any(character in subject for character in "*?["):






                found = next(
                    glob.iglob(subject, recursive=True, include_hidden=True),
                    None,
                )
                return found is None if predicate.kind == "absent" else found is not None
            try:
                Path(subject).stat()
                exists = True
            except FileNotFoundError:
                exists = False
        except OSError:
            return None
        return exists if predicate.kind == "present" else not exists

    return observe


def runtime_observer(
    *,
    broker: Any,
    orchestration: Mapping[str, Any],
    media_probe_delay: float = 0.75,
) -> Observer:
    """Observe generic final state through independent read-only primitives.

    Results are cached per verification pass, so several predicates about the
    same browser or window snapshot share one fresh observation. A missing
    broker, grant, helper, or field returns unknown; it never rounds up to true.
    """

    filesystem = filesystem_observer()
    cache: dict[str, Any] = {}

    def invoke_once(key: str, capability: str, arguments: Mapping[str, Any]) -> Any:
        if key in cache:
            return cache[key]
        if broker is None:
            cache[key] = None
            return None
        try:
            result = broker.invoke(
                {
                    "capability": capability,
                    "arguments": dict(arguments),
                    "user_confirmed": True,
                    "authority_mode": "full_access",
                }
            )
        except Exception:
            result = None
        cache[key] = result
        return result

    def observe(predicate: Predicate) -> bool | None:
        if predicate.kind in {"present", "absent"}:
            return filesystem(predicate)
        if predicate.kind == "discord_report_valid":
            if (
                str(orchestration.get("kind") or "") != "action"
                or str(orchestration.get("capability") or "") != "discord.inspect"
                or str(orchestration.get("status") or "") != "completed"
            ):
                return False
            steps = [
                dict(step)
                for step in orchestration.get("steps") or []
                if isinstance(step, Mapping)
            ]
            if any(
                str(step.get("action") or "")
                not in {"discord.inspect", "respond"}
                for step in steps
            ):
                return False
            inspections = [
                step
                for step in steps
                if str(step.get("action") or "") == "discord.inspect"
                and str(step.get("status") or "") == "succeeded"
            ]
            find = next(
                (
                    dict(step.get("observation") or {})
                    for step in inspections
                    if str((step.get("observation") or {}).get("operation") or "")
                    == "find_channels"
                ),
                {},
            )
            scan = next(
                (
                    dict(step.get("observation") or {})
                    for step in inspections
                    if str((step.get("observation") or {}).get("operation") or "")
                    == "scan_batch"
                ),
                {},
            )
            candidate_count = int(
                find.get("candidate_index_size") or find.get("channel_count") or 0
            )
            attempted = len(scan.get("scans") or []) + len(
                scan.get("scan_gaps") or []
            )
            answer = " ".join(
                str(orchestration.get("answer") or "").casefold().split()
            )
            honest_scope = any(
                phrase in answer
                for phrase in (
                    "not a complete inventory",
                    "inventory remains incomplete",
                    "coverage is incomplete",
                    "coverage is limited",
                )
            )
            return bool(candidate_count > 0 and attempted > 0 and answer and honest_scope)
        if predicate.kind == "artifact_valid":
            subjects = (
                _artifact_paths(orchestration)
                if predicate.subject == "$artifact"
                else [predicate.subject]
            )
            if not subjects:
                return None
            return any(_valid_artifact(Path(subject)) for subject in subjects)
        if predicate.kind in {"window_present", "window_absent"}:
            snapshot = invoke_once("windows", "window.control", {"action": "list"})
            if not isinstance(snapshot, Mapping) or snapshot.get("status") != "succeeded":
                return None
            wanted = predicate.subject.strip().casefold()
            if not wanted:
                return None
            found = any(
                wanted in str(item.get("title") or "").casefold()
                for item in (snapshot.get("windows") or [])
                if isinstance(item, Mapping)
            )
            return found if predicate.kind == "window_present" else not found
        if predicate.kind == "window_focused":
            snapshot = invoke_once(
                "active_window", "ui.automation", {"command": "get_active_window"}
            )
            if not isinstance(snapshot, Mapping) or snapshot.get("status") != "succeeded":
                return None
            wanted = predicate.subject.strip().casefold()
            active = snapshot.get("window") or snapshot.get("active_window") or snapshot
            if not isinstance(active, Mapping) or not wanted:
                return None
            return wanted in str(active.get("title") or active.get("name") or "").casefold()
        if predicate.kind in {"active_url", "browser_visible", "browser_title_reported"}:
            snapshot = invoke_once(
                "browser_session",
                "browser.control",
                {"command": "get_session_state"},
            )
            if not isinstance(snapshot, Mapping) or snapshot.get("status") != "succeeded":
                return None
            if predicate.kind == "browser_title_reported":
                title = str(snapshot.get("title") or "").strip()
                answer = str(orchestration.get("answer") or "")
                if not title:
                    return None
                if predicate.subject != "$observed_title" and title != predicate.subject:
                    return False
                return title.casefold() in answer.casefold()
            if predicate.kind == "browser_visible":
                visible = snapshot.get("visible")
                return bool(visible) if isinstance(visible, bool) else None
            wanted = (
                _selected_url(orchestration)
                if predicate.subject == "$selected_url"
                else predicate.subject
            )
            actual = str(snapshot.get("url") or "")
            if not wanted or not actual:
                return None
            return (_canonical_url(actual) == _canonical_url(wanted)
                    or _observed_www_redirect(orchestration, wanted, actual))
        if predicate.kind == "media_playing":
            first = _read_media(broker)
            if first is None:
                return None
            candidates = _matching_media(first, predicate.subject)
            if not candidates:
                return False
            playing = [
                item
                for item in candidates
                if item.get("playing") is True and item.get("visible") is not False
            ]
            if not playing:
                return False
            initial = playing[0]
            try:
                initial_time = float(initial.get("current_time"))
            except (TypeError, ValueError):
                return None
            if media_probe_delay > 0:
                time.sleep(min(float(media_probe_delay), 2.0))
            second = _read_media(broker)
            if second is None:
                return None
            identity = str(initial.get("element") or "")
            later = next(
                (
                    item
                    for item in _matching_media(second, predicate.subject)
                    if not identity or str(item.get("element") or "") == identity
                ),
                None,
            )
            if later is None or later.get("playing") is not True:
                return False
            try:
                later_time = float(later.get("current_time"))
            except (TypeError, ValueError):
                return None
            return later_time > initial_time + 0.05
        return None

    return observe


def _string_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _looks_like_absolute_path(value: str) -> bool:
    try:
        return Path(value).expanduser().is_absolute()
    except (OSError, ValueError):
        return False


def _artifact_paths(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            artifact = item.get("artifact")
            if isinstance(artifact, Mapping) and artifact.get("path"):
                found.append(str(artifact["path"]))
            elif isinstance(artifact, str) and artifact:
                found.append(artifact)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for nested in item:
                visit(nested)

    visit(value)
    return list(dict.fromkeys(found))


def _valid_artifact(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 1:
            return False
        with path.open("rb") as stream:
            header = stream.read(16)
    except OSError:
        return False
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".bmp":
        return header.startswith(b"BM")
    if suffix == ".webp":
        return header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    return True


def _selected_url(value: Any) -> str:
    selected: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key in ("selected_url", "exact_url", "exact_page"):
                if item.get(key):
                    selected.append(str(item[key]))
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for nested in item:
                visit(nested)

    visit(value)
    return selected[-1] if selected else ""


def _observed_www_redirect(orchestration: Mapping[str, Any], wanted: str, actual: str) -> bool:
    """Accept a measured www/HTTPS redirect, never infer navigation from a name."""
    try:
        before, after = urlsplit(wanted), urlsplit(actual)
        if (not before.hostname or not after.hostname
                or before.hostname.casefold().removeprefix("www.") != after.hostname.casefold().removeprefix("www.")
                or (before.path or "/", before.query, before.fragment) != (after.path or "/", after.query, after.fragment)
                or before.scheme not in {"http", "https"} or after.scheme != "https"
                or before.port != after.port):
            return False
    except ValueError:
        return False
    steps = orchestration.get("steps") or (orchestration.get("agent_task") or {}).get("steps") or []
    for step in steps:
        if not isinstance(step, Mapping) or step.get("action") != "browser.control":
            continue
        arguments, observation = step.get("arguments") or {}, step.get("observation") or {}
        if (arguments.get("command") in {"open_url", "navigate"}
                and _canonical_url(str(arguments.get("url") or "")) == _canonical_url(wanted)
                and observation.get("status") == "succeeded"
                and _canonical_url(str(observation.get("url") or "")) == _canonical_url(actual)):
            return True
    return False


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return str(value).strip()
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def _read_media(broker: Any) -> list[Mapping[str, Any]] | None:
    if broker is None:
        return None
    try:
        result = broker.invoke(
            {
                "capability": "browser.control",
                "arguments": {"command": "get_media"},
                "user_confirmed": True,
                "authority_mode": "full_access",
            }
        )
    except Exception:
        return None
    if not isinstance(result, Mapping) or result.get("status") != "succeeded":
        return None
    media = result.get("media")
    if not isinstance(media, Sequence) or isinstance(media, (str, bytes)):
        return None
    return [item for item in media if isinstance(item, Mapping)]


def _matching_media(
    media: Sequence[Mapping[str, Any]], subject: str
) -> list[Mapping[str, Any]]:
    wanted = str(subject or "").strip().casefold()
    if not wanted or wanted == "$active_media":
        return list(media)
    return [
        item
        for item in media
        if any(
            wanted in str(item.get(field) or "").casefold()
            for field in ("title", "name", "src", "current_src", "kind")
        )
    ]


__all__ = [
    "GOAL_STATE_SCHEMA",
    "GoalSpec",
    "Observer",
    "Predicate",
    "filesystem_observer",
    "runtime_observer",
    "verify_predicates",
]
