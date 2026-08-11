"""Bounded native Discord read/navigation adapter.

The local model chooses the objective, operation, query, and next step. This
adapter executes exactly one selected operation and collapses only that
operation's brittle focus/Quick-Switcher/UIA mechanics. It never expands a
request into an inventory or content scan on its own.

It has no send, react, join, login, token, or account-creation operation.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

DISCORD_INSPECTION_SCHEMA = "salty-steak-discord-inspection-v1"
DISCORD_INVENTORY_CACHE_SCHEMA = "salty-steak-discord-inventory-cache-v2"



INVENTORY_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
DISCORD_OPERATIONS = frozenset(
    {
        "current_account",
        "find_channels",
        "inventory",
        "list_servers",
        "open_channel",
        "read_channel",
        "scan_batch",
    }
)
MAX_RESULTS = 5_000
MAX_MESSAGES = 100
MAX_SCROLLS = 100
SCAN_UIA_TIMEOUT_MS = 5_000
SCAN_UIA_TIMEOUT_MAX_MS = 30_000
DEFAULT_UIA_TIMEOUT_MS = 20_000
MAX_CHANNEL_QUERY_CANDIDATES = 3
CHANNEL_NAVIGATION_BUDGET_SECONDS = 25.0
CHANNEL_READ_BUDGET_SECONDS = 20.0

_CHANNEL_RESULT = re.compile(
    r"^(?P<channel>.+?),\s*(?P<channel_type>Announcement|Text|Voice|Forum|Stage|Media|Thread)"
    r"(?:\s+Channel)?,\s*"
    r"(?P<server>.+)$",
    re.IGNORECASE,
)
_DISCORD_TITLE = re.compile(
    r"^#?(?P<channel>.+?)\s*\|\s*(?P<server>.+?)\s*-\s*Discord$",
    re.IGNORECASE,
)
_REQUIREMENT_PATTERNS = (
    (
        "entry_limit",
        re.compile(
            r"(?:~?\d+|one|two|three)\s+entr(?:y|ies)\s+per\s+"
            r"(?:ip|person|account|user)",
            re.I,
        ),
    ),
    (
        "role",
        re.compile(
            r"(?:must|need|require(?:d|s)?|only).{0,100}\brole\b[^.!\n]{0,120}|"
            r"<@&\d+>",
            re.I,
        ),
    ),
    (
        "level",
        re.compile(
            r"(?:level|lvl)\s*(?:>=|>|at\s+least|minimum|min\.?|:)??\s*\d+|"
            r"(?:must|need).{0,60}\blevel\b[^.!\n]{0,40}",
            re.I,
        ),
    ),
    (
        "invites",
        re.compile(
            r"(?:at\s+least|minimum|min\.?|need|requires?)?\s*\d+\s+"
            r"(?:valid\s+)?(?:invites?|referrals?)|"
            r"(?:invites?|referrals?)\s*[:>=-]?\s*\d+",
            re.I,
        ),
    ),
    (
        "messages",
        re.compile(
            r"(?:at\s+least|minimum|min\.?|need|requires?)?\s*\d+\s+messages?|"
            r"messages?\s*[:>=-]?\s*\d+",
            re.I,
        ),
    ),
    (
        "follow",
        re.compile(
            r"(?:must|need|require(?:d)?\s+to\s+)?(?:follow|subscribe|join)\b"
            r"[^.!\n]{0,160}",
            re.I,
        ),
    ),
    (
        "boost",
        re.compile(
            r"(?:must|need|require(?:d|s)?|only).{0,100}\b"
            r"(?:boost(?:er|ing)?|server\s+booster)\b[^.!\n]{0,80}",
            re.I,
        ),
    ),
    (
        "verification",
        re.compile(
            r"(?:must|need|require(?:d|s)?|only).{0,100}\b"
            r"verif(?:y|ied|ication)\b[^.!\n]{0,80}",
            re.I,
        ),
    ),
    (
        "account_age",
        re.compile(
            r"(?:discord\s+)?account.{0,80}(?:at\s+least\s+)?\d+\s+"
            r"(?:days?|weeks?|months?|years?)\s+old|account\s+age[^.!\n]{0,100}",
            re.I,
        ),
    ),
    (
        "membership_age",
        re.compile(
            r"(?:member|joined|in\s+(?:this|the)\s+server).{0,80}"
            r"(?:at\s+least\s+)?\d+\s+(?:days?|weeks?|months?|years?)",
            re.I,
        ),
    ),
    (
        "region",
        re.compile(
            r"(?:region|country|residents?\s+of|(?:us|uk|eu|canada|india|"
            r"bangladesh)[ -]only)\b[^.!\n]{0,120}",
            re.I,
        ),
    ),
    (
        "linked_account",
        re.compile(
            r"(?:must|need|require(?:d|s)?).{0,100}\b(?:link|connect)\b"
            r"[^.!\n]{0,120}\baccount\b|\b(?:steam|xbox|playstation|epic|"
            r"twitter|youtube|twitch)\s+account\b[^.!\n]{0,80}",
            re.I,
        ),
    ),
    (
        "direct_messages",
        re.compile(
            r"(?:dms?|direct\s+messages?).{0,60}(?:open|enabled|on)|"
            r"(?:enable|open).{0,40}(?:dms?|direct\s+messages?)",
            re.I,
        ),
    ),
    (
        "age",
        re.compile(r"(?:must\s+be\s+|age\s+)?(?:13|16|18|21)\s*\+", re.I),
    ),
)
_RULE_CLAUSE = re.compile(
    r"\b(?:must|need|require(?:d|ment|ments|s)?|eligib(?:le|ility)|criteria|"
    r"rule|before\s+entering|to\s+enter|only|at\s+least|minimum|do\s+not|"
    r"cannot|can['’]?t|not\s+eligible)\b",
    re.I,
)
_GIVEAWAY_EVIDENCE = re.compile(
    r"\b(?:give[ -]?away|raffle|sweepstakes|prize\s+draw)\b", re.I
)
_ENDED_EVIDENCE = re.compile(
    r"(?:\[\s*(?:ended|closed|finished|results?)\s*\]|"
    r"\b(?:give[ -]?away|raffle)\s+(?:has\s+)?(?:ended|closed|finished)\b|"
    r"\b(?:ended|closed|finished|results?|winners?)\s*:|"
    r"\b(?:ends?|ended|closed)\s*:\s*[^\n]{0,120}\bago\b|"
    r"\bthe\s+winner\s+of\s+this\s+give[ -]?away\b)",
    re.I,
)
_ACTIVE_EVIDENCE = re.compile(
    r"(?:\[\s*(?:active|live|open)\s*\]|"
    r"\bends?\s*:\s*(?:in\b|next\b|tomorrow\b|today\b|on\b|at\b|<t:\d+)|"
    r"\bends?\s+\b(?:in|on|at)\b|"
    r"\btime\s+remaining\b|\bentries?\s*:)",
    re.I,
)
_END_EVIDENCE = re.compile(
    r"(?:\[\s*(?:ended|closed|finished|results?)\s*\]|"
    r"\b(?:ends?|ended|closed|finished|results?|time\s+remaining|winners?)\s*:"
    r"\s*[^\n]{0,180})",
    re.I,
)
_END_TIMESTAMP = re.compile(
    r"\b(?:ends?|ending|end\s+time|closes?|closing)\b[^\n]{0,120}"
    r"<t:(?P<timestamp>\d+)(?::[A-Za-z])?>",
    re.I,
)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3}
_DURATION_MULTIPLIERS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}


class DiscordInspectionError(RuntimeError):
    pass


class DiscordInspectionCancelled(DiscordInspectionError):
    """Raised between bounded UI calls after the parent task is stopped."""

    pass


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _semantic_text(value: Any) -> str:
    """Compare visible labels without trusting decorative glyphs or spacing."""

    small_cap_fold = str.maketrans(
        {
            "ᴀ": "a",
            "ʙ": "b",
            "ᴄ": "c",
            "ᴅ": "d",
            "ᴇ": "e",
            "ꜰ": "f",
            "ɢ": "g",
            "ʜ": "h",
            "ɪ": "i",
            "ᴊ": "j",
            "ᴋ": "k",
            "ʟ": "l",
            "ᴍ": "m",
            "ɴ": "n",
            "ᴏ": "o",
            "ᴘ": "p",
            "ʀ": "r",
            "ꜱ": "s",
            "ᴛ": "t",
            "ᴜ": "u",
            "ᴠ": "v",
            "ᴡ": "w",
            "ʏ": "y",
            "ᴢ": "z",
        }
    )
    normalised = unicodedata.normalize("NFKD", _text(value)).translate(
        small_cap_fold
    ).casefold()
    folded = []
    for character in normalised:
        if character.isascii() and character.isalnum():
            folded.append(character)
            continue
        if unicodedata.combining(character):
            continue
        category = unicodedata.category(character)
        name = unicodedata.name(character, "")
        if category.startswith("L") and "LATIN" not in name:
            folded.append(character)
        else:
            folded.append(" ")
    return " ".join(
        "".join(folded).split()
    )


def _channel_key(channel: Any, server: Any) -> str:
    identity = f"{_semantic_text(channel)}\x1f{_semantic_text(server)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _inventory_channel_key(record: Mapping[str, Any]) -> str:
    """Keep same-named channels in different servers as distinct locations."""

    recorded = _text(record.get("channel_key"))
    if recorded:
        return recorded
    return _channel_key(
        record.get("channel") or record.get("name"),
        record.get("server"),
    )


def _first_number(value: str) -> int | None:
    numeric = re.search(r"\b(\d+)\b", value)
    if numeric:
        return int(numeric.group(1))
    lowered = value.casefold()
    return next(
        (number for word, number in _NUMBER_WORDS.items() if re.search(rf"\b{word}\b", lowered)),
        None,
    )


def _discovery_priority(record: Mapping[str, Any]) -> int:
    """Prioritize likely event channels without excluding any live channel."""

    visible = _text(record.get("channel") or record.get("name"))
    semantic = _semantic_text(visible)
    score = 0
    if any(marker in semantic for marker in ("giveaway", "give away", "raffle")):
        score += 100
    if any(marker in visible for marker in ("\U0001f381", "\U0001f389", "\U0001f38a")):
        score += 80
    for marker in (
        "prize",
        "reward",
        "promo",
        "free drop",
        "weekly drop",
        "monthly",
        "daily",
        "event",
        "winner",
        "redeem",
    ):
        if marker in semantic:
            score += 20
    if str(record.get("channel_type") or "").casefold() == "announcement":
        score += 5


    if any(
        marker in semantic
        for marker in ("winner", "results", "archive", "ended", "completed", "proof")
    ):
        score -= 90
    return score


def _account_identity(record: Mapping[str, Any]) -> str:
    return _semantic_text(record.get("handle") or record.get("label"))


def _server_identity(records: list[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        sorted(
            _semantic_text(record.get("server") or record.get("name"))
            for record in records
            if _semantic_text(record.get("server") or record.get("name"))
        )
    )


class DiscordDesktopInspector:
    def __init__(
        self,
        *,
        launch: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        window: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        input_control: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        uia: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        sleep: Callable[[float], None] = time.sleep,
        inventory_cache: dict[str, Any] | None = None,
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.launch = launch
        self.window = window
        self.input_control = input_control
        self.uia = uia
        self.sleep = sleep
        self.inventory_cache = inventory_cache if inventory_cache is not None else {}
        self.on_progress = on_progress
        self.should_stop = should_stop
        self._last_progress_at = 0.0
        self._uia_timeout_ms = DEFAULT_UIA_TIMEOUT_MS
        self.substeps: list[dict[str, Any]] = []
        self.coverage: dict[str, Any] = {}

    def _ensure_active(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise DiscordInspectionCancelled("Discord inspection was stopped")

    def _progress(self, summary: str, *, force: bool = False, **detail: Any) -> None:
        if self.on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_progress_at < 0.2:
            return
        self._last_progress_at = now
        try:
            self.on_progress({"summary": _text(summary)[:240], **detail})
        except BaseException:
            return

    def run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_active()
        operation = _text(arguments.get("operation")).casefold()
        if operation not in DISCORD_OPERATIONS:
            raise ValueError(
                "Discord operation must be one of: " + ", ".join(sorted(DISCORD_OPERATIONS))
            )
        limit = self._bounded_integer(
            arguments.get("limit"),
            MAX_RESULTS if operation == "inventory" else 20,
            1,
            MAX_RESULTS,
            "limit",
        )
        max_scrolls = self._bounded_integer(
            arguments.get("max_scrolls"),
            MAX_SCROLLS if operation == "inventory" else 8,
            0,
            MAX_SCROLLS,
            "max_scrolls",
        )
        cursor = self._bounded_integer(
            arguments.get("cursor"), 0, 0, MAX_RESULTS, "cursor"
        )
        batch_limit = self._bounded_integer(
            arguments.get("batch_limit"), 10, 1, 25, "batch_limit"
        )
        query = _text(arguments.get("query"))
        channel = _text(arguments.get("channel"))
        raw_channels = arguments.get("channels", [])
        if raw_channels is None:
            raw_channels = []
        if not isinstance(raw_channels, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in raw_channels
        ):
            raise ValueError("Discord channels must be an array of non-empty strings")
        if len(raw_channels) > 25:
            raise ValueError("Discord channels may contain at most 25 selectors")
        explicit_channel_selectors = [_text(value) for value in raw_channels]
        if operation == "scan_batch" and channel:
            explicit_channel_selectors.append(channel)
        refresh = arguments.get("refresh", False)
        if not isinstance(refresh, bool):
            raise ValueError("Discord refresh must be true or false")
        self._uia_timeout_ms = (
            min(
                SCAN_UIA_TIMEOUT_MAX_MS,
                max(SCAN_UIA_TIMEOUT_MS, max_scrolls * 500),
            )
            if operation in {"open_channel", "read_channel", "scan_batch"}
            else DEFAULT_UIA_TIMEOUT_MS
        )

        if (
            operation == "find_channels"
            and self.inventory_cache.get("complete")
            and self.inventory_cache.get("validated_for_task") is True
        ):
            cached_channels = list(self.inventory_cache.get("channels") or [])
            needle = _semantic_text(query)
            matches = [
                dict(record)
                for record in cached_channels
                if not needle
                or needle in _semantic_text(record.get("channel"))
                or needle in _semantic_text(record.get("server"))
                or needle in _semantic_text(record.get("name"))
            ][:limit]
            return {
                "schema": DISCORD_INSPECTION_SCHEMA,
                "status": "succeeded",
                "operation": operation,
                "account": dict(self.inventory_cache.get("account") or {}),
                "channels": matches,
                "coverage": {
                    "channels": {
                        "source": "complete_account_bound_inventory",
                        "query": query,
                        "inventory_channel_count": len(cached_channels),
                        "unique_results": len(matches),
                        "truncated_by_limit": len(matches) >= limit,
                        "scroll_boundary_reached": True,
                    }
                },
                "substeps": [],
            }

        if operation == "current_account":
            result = self._current_account()
        elif operation == "inventory":
            account_observation = self._current_account()
            account_record = dict(account_observation.get("account") or {})
            servers = self._query_results(
                prefix="*", query="", limit=limit, max_scrolls=max_scrolls
            )
            server_collection_coverage = dict(self.coverage.get("servers") or {})
            self._close_switcher()
            now = time.time()
            cached_account = dict(self.inventory_cache.get("account") or {})
            cached_servers = [
                dict(record)
                for record in self.inventory_cache.get("servers") or []
                if isinstance(record, Mapping)
            ]
            current_server_identity = _server_identity(servers)
            cached_server_identity = _server_identity(cached_servers)
            account_matches = bool(
                account_record.get("observed")
                and _account_identity(account_record)
                and _account_identity(account_record) == _account_identity(cached_account)
            )
            try:
                cache_age = max(
                    0.0, now - float(self.inventory_cache.get("created_at") or 0)
                )
            except (TypeError, ValueError):
                cache_age = float("inf")
            cache_fresh = bool(
                self.inventory_cache.get("complete")
                and account_matches
                and cache_age <= INVENTORY_CACHE_MAX_AGE_SECONDS
                and not refresh
            )
            if cache_fresh and current_server_identity == cached_server_identity:
                cached_channels = sorted(
                    (
                        {
                            **dict(record),
                            "discovery_priority": _discovery_priority(record),
                            "inventory_index": int(
                                record.get("inventory_index")
                                if record.get("inventory_index") is not None
                                else index
                            ),
                        }
                        for index, record in enumerate(
                            self.inventory_cache.get("channels") or []
                        )
                        if isinstance(record, Mapping)
                    ),
                    key=lambda record: (
                        -int(record.get("discovery_priority") or 0),
                        int(record.get("inventory_index") or 0),
                    ),
                )
                cached_coverage = dict(self.inventory_cache.get("coverage") or {})
                channel_coverage = dict(cached_coverage.get("channels") or {})
                channel_coverage.update(
                    {
                        "source": "validated_account_inventory_cache",
                        "cache_age_seconds": round(cache_age, 3),
                        "servers_total": len(servers),
                        "servers_scanned": len(servers),
                        "servers_at_boundary": len(servers),
                        "scroll_boundary_reached": True,
                        "truncated_by_limit": False,
                        "gaps": [],
                    }
                )
                self.coverage = {
                    "servers": server_collection_coverage,
                    "channels": channel_coverage,
                    "account": {"observed": True, "identity_bound": True},
                }
                self.inventory_cache["validated_at"] = now
                self.inventory_cache["validated_for_task"] = True
                self.inventory_cache["channels"] = cached_channels
                self.inventory_cache["coverage"] = dict(self.coverage)
                self._progress(
                    f"Validated and reused {len(cached_channels)} indexed Discord channels.",
                    force=True,
                    completed=len(servers),
                    total=len(servers),
                    channels=len(cached_channels),
                )
                return {
                    "schema": DISCORD_INSPECTION_SCHEMA,
                    "status": "succeeded",
                    "operation": operation,
                    "account": account_record,
                    "servers": servers,
                    "channels": cached_channels[:limit],
                    "inventory_gaps": [],
                    "discovery_candidate_count": sum(
                        int(record.get("discovery_priority") or 0) > 0
                        for record in cached_channels
                    ),
                    "inventory_reused": True,
                    "inventory_refresh_forced": False,
                    "inventory_cache_age_seconds": round(cache_age, 3),
                    "inventory_cache_max_age_seconds": INVENTORY_CACHE_MAX_AGE_SECONDS,
                    "coverage": dict(self.coverage),
                    "substeps": list(self.substeps),
                }

            current_server_names = {
                _text(record.get("server") or record.get("name")).removesuffix(", Server")
                for record in servers
            }
            cached_server_names = {
                _text(record.get("server") or record.get("name")).removesuffix(", Server")
                for record in cached_servers
            }
            reusable_server_names = (
                current_server_names & cached_server_names if cache_fresh else set()
            )
            cached_channel_records = [
                dict(record)
                for record in self.inventory_cache.get("channels") or []
                if isinstance(record, Mapping)
                and _text(record.get("server")) in reusable_server_names
            ]
            channels_by_key: dict[str, dict[str, Any]] = {
                _inventory_channel_key(record): record
                for record in cached_channel_records
                if _text(record.get("channel") or record.get("name"))
                and _text(record.get("server"))
            }
            cached_per_server = dict(
                (
                    (self.inventory_cache.get("coverage") or {})
                    .get("channels", {})
                    .get("per_server", {})
                )
            )
            per_server: dict[str, Any] = {
                name: (
                    dict(cached_per_server.get(name) or {})
                    if isinstance(cached_per_server.get(name), Mapping)
                    else {
                        "scroll_boundary_reached": True,
                        "source": "validated_inventory_cache",
                    }
                )
                for name in reusable_server_names
            }
            servers_to_scan = [
                server
                for server in servers
                if _text(server.get("server") or server.get("name")).removesuffix(", Server")
                not in reusable_server_names
            ]
            self._progress(
                (
                    f"Reusing {len(reusable_server_names)} servers and indexing "
                    f"{len(servers_to_scan)} changed servers."
                    if reusable_server_names
                    else f"Found {len(servers)} Discord servers; indexing their channels."
                ),
                force=True,
                completed=len(reusable_server_names),
                total=len(servers),
                channels=len(channels_by_key),
            )
            gaps: list[dict[str, str]] = []
            for server_index, server in enumerate(servers_to_scan, start=1):
                server_name = _text(server.get("server") or server.get("name")).removesuffix(", Server")
                success = False
                attempt_errors: list[str] = []
                for attempt in range(1, 6):
                    if attempt > 1:
                        self._close_switcher()
                        self.sleep(min(1.0, 0.2 * attempt))
                    try:
                        self._open_server(server_name)



                        self.sleep(0.35)
                        server_channels = self._query_results(
                            prefix="#",
                            query="",
                            limit=limit,
                            max_scrolls=max_scrolls,
                        )
                        coverage = dict(self.coverage.get("channels") or {})
                        coverage["attempts"] = attempt
                        per_server[server_name] = coverage
                        for channel_record in server_channels:
                            channels_by_key.setdefault(
                                _inventory_channel_key(channel_record),
                                channel_record,
                            )
                        self._close_switcher()
                        if coverage.get("scroll_boundary_reached"):
                            success = True
                            break
                        attempt_errors.append(
                            "channel collection did not reach a verified boundary "
                            f"(results={int(coverage.get('unique_results') or 0)}, "
                            f"basis={_text(coverage.get('boundary_basis')) or 'none'})"
                        )
                    except Exception as error:
                        attempt_errors.append(f"{type(error).__name__}: {error}")
                        continue
                if not success:
                    gaps.append(
                        {
                            "server": server_name,
                            "error": "; ".join(attempt_errors[-5:])[:2_000]
                            or "channel collection did not reach a verified boundary",
                        }
                    )
                if len(channels_by_key) >= limit:
                    break
                self._progress(
                    f"Indexed {len(reusable_server_names) + server_index} of {len(servers)} Discord servers.",
                    force=True,
                    completed=len(reusable_server_names) + server_index,
                    total=len(servers),
                    channels=len(channels_by_key),
                )
            complete_servers = sum(
                bool(item.get("scroll_boundary_reached")) for item in per_server.values()
            )
            self.coverage = {
                "servers": server_collection_coverage,
                "channels": {
                    "unique_results": len(channels_by_key),
                    "servers_total": len(servers),
                    "servers_scanned": len(per_server),
                    "servers_at_boundary": complete_servers,
                    "scroll_boundary_reached": bool(servers)
                    and complete_servers == len(servers)
                    and not gaps,
                    "truncated_by_limit": len(channels_by_key) >= limit,
                    "gaps": gaps,
                    "per_server": per_server,
                },
            }
            ordered_channels = sorted(
                (
                    {
                        **record,
                        "discovery_priority": _discovery_priority(record),
                        "inventory_index": index,
                    }
                    for index, record in enumerate(channels_by_key.values())
                ),
                key=lambda record: (
                    -int(record.get("discovery_priority") or 0),
                    int(record.get("inventory_index") or 0),
                ),
            )
            result = {
                "account": account_record,
                "servers": servers,
                "channels": ordered_channels[:limit],
                "inventory_gaps": gaps,
                "discovery_candidate_count": sum(
                    int(record.get("discovery_priority") or 0) > 0
                    for record in ordered_channels
                ),
            }
            inventory_complete = bool(
                account_record.get("observed")
                and (account_record.get("handle") or account_record.get("label"))
                and self.coverage["servers"].get("scroll_boundary_reached")
                and self.coverage["channels"].get("scroll_boundary_reached")
                and not self.coverage["channels"].get("truncated_by_limit")
                and not gaps
            )
            self.coverage["account"] = {
                "observed": bool(account_record.get("observed")),
                "identity_bound": bool(
                    account_record.get("handle") or account_record.get("label")
                ),
            }
            owner_task_id = self.inventory_cache.get("owner_task_id")
            self.inventory_cache.clear()
            self.inventory_cache.update(
                {
                    "owner_task_id": owner_task_id,
                    "schema": DISCORD_INVENTORY_CACHE_SCHEMA,
                    "complete": inventory_complete,
                    "created_at": time.time(),
                    "validated_at": time.time(),
                    "validated_for_task": inventory_complete,
                    "account": account_record,
                    "channels": [
                        {
                            key: value
                            for key, value in channel_record.items()
                            if key != "element"
                        }
                        for channel_record in result["channels"]
                    ],
                    "servers": [
                        {
                            key: value
                            for key, value in server_record.items()
                            if key != "element"
                        }
                        for server_record in result["servers"]
                    ],
                    "coverage": dict(self.coverage),
                }
            )
        elif operation == "scan_batch":
            complete_index_ready = bool(
                self.inventory_cache.get("complete")
                and self.inventory_cache.get("validated_for_task") is True
            )
            candidate_index_ready = bool(
                self.inventory_cache.get("candidate_validated_for_task") is True
                and self.inventory_cache.get("candidate_channels")
            )
            explicit_channels: list[dict[str, Any]] = []
            if explicit_channel_selectors:
                available_channels = [
                    dict(record)
                    for record in self.inventory_cache.get("channels") or []
                    if isinstance(record, Mapping)
                ]



                selectors: list[str] = []
                for selector in explicit_channel_selectors:
                    whole_matches = [
                        record
                        for record in available_channels
                        if selector.casefold()
                        in {
                            _text(record.get("channel_key")).casefold(),
                            _text(record.get("name")).casefold(),
                            _text(record.get("channel")).casefold(),
                        }
                    ]
                    if whole_matches:
                        selectors.append(selector)
                    else:
                        selectors.extend(
                            value.strip()
                            for value in selector.split(",")
                            if value.strip()
                        )
                selected_keys: set[str] = set()
                for selector in selectors:
                    checked = selector.casefold()
                    direct = [
                        record
                        for record in available_channels
                        if checked
                        in {
                            _text(record.get("channel_key")).casefold(),
                            _text(record.get("name")).casefold(),
                        }
                    ]
                    semantic = [
                        record
                        for record in available_channels
                        if _semantic_text(selector)
                        == _semantic_text(record.get("channel"))
                    ]
                    matches = direct or semantic
                    unique = {
                        _inventory_channel_key(record): record for record in matches
                    }
                    if len(unique) != 1:
                        raise DiscordInspectionError(
                            "Explicit scan channel must match exactly one channel "
                            f"observed in this task: {selector!r}"
                        )
                    key, record = next(iter(unique.items()))
                    if key not in selected_keys:
                        explicit_channels.append(record)
                        selected_keys.add(key)
            explicit_index_ready = bool(
                explicit_channels
                and self.inventory_cache.get("owner_task_id")
                and self.inventory_cache.get("account")
            )
            if (
                not complete_index_ready
                and not candidate_index_ready
                and not explicit_index_ready
            ):
                if self.inventory_cache.get("complete"):
                    raise DiscordInspectionError(
                        "scan_batch needs current-account inventory validation in this task first"
                    )
                raise DiscordInspectionError(
                    "scan_batch needs a current-account-bound complete inventory or "
                    "live candidate index from this task first"
                )
            account_observation = self._current_account()
            account_record = dict(account_observation.get("account") or {})
            cached_account = dict(
                self.inventory_cache.get(
                    "account"
                    if complete_index_ready or explicit_index_ready
                    else "candidate_account"
                )
                or {}
            )
            current_identity = _text(
                account_record.get("handle") or account_record.get("label")
            ).casefold()
            cached_identity = _text(
                cached_account.get("handle") or cached_account.get("label")
            ).casefold()
            if (
                not account_record.get("observed")
                or not current_identity
                or current_identity != cached_identity
            ):
                self.inventory_cache.clear()
                raise DiscordInspectionError(
                    "The current Discord account could not be matched to the cached inventory"
                )
            cached_channels = (
                explicit_channels
                if explicit_index_ready
                else list(
                    self.inventory_cache.get(
                        "channels" if complete_index_ready else "candidate_channels"
                    )
                    or []
                )
            )
            batch = cached_channels[cursor : cursor + batch_limit]
            scans: list[dict[str, Any]] = []
            gaps: list[dict[str, Any]] = []
            for channel_record in batch:
                selector = _text(channel_record.get("name"))
                attempt_errors: list[str] = []
                completed_scan: dict[str, Any] | None = None
                for attempt in range(1, 5):
                    if attempt > 1:
                        self._close_switcher()
                        self.sleep(min(0.8, 0.2 * attempt))
                    try:
                        selected, _window = self._open_channel(
                            selector,
                            max_scrolls=max_scrolls,
                        )
                        inspection = self._read_channel(
                            limit=limit,
                            max_scrolls=max_scrolls,
                            selected_channel=selected,
                        )
                        completed_scan = {
                            "channel_key": selected.get("channel_key"),
                            "selected_channel": selected,
                            **inspection,
                            "navigation_attempts": attempt,
                            "coverage": dict(self.coverage),
                        }
                        break
                    except DiscordInspectionCancelled:
                        raise
                    except Exception as error:
                        attempt_errors.append(f"{type(error).__name__}: {error}")
                if completed_scan is not None:
                    scans.append(completed_scan)
                else:
                    gaps.append(
                        {
                            "channel_key": channel_record.get("channel_key"),
                            "channel": selector,
                            "error": "; ".join(attempt_errors[-4:])[:2_000]
                            or "Discord channel inspection did not complete",
                        }
                    )
                    self._close_switcher()
                self._progress(
                    f"Inspected {len(scans) + len(gaps)} of {len(batch)} channels in this batch.",
                    force=True,
                    completed=len(scans) + len(gaps),
                    total=len(batch),
                    cursor=cursor,
                    next_cursor=cursor + len(scans) + len(gaps),
                )
            next_cursor = min(len(cached_channels), cursor + len(batch))
            self.coverage = {
                "inventory": (
                    dict(self.inventory_cache.get("coverage") or {})
                    if complete_index_ready
                    else {
                        "source": "task_targeted_live_candidate_index",
                        "complete": False,
                        "queries": list(self.inventory_cache.get("candidate_queries") or []),
                        "candidate_channels": len(cached_channels),
                    }
                    if not explicit_index_ready
                    else {
                        "source": "explicit_current_task_inventory_channels",
                        "complete": False,
                        "selected_channels": len(cached_channels),
                    }
                ),
                "batch": {
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "requested": len(batch),
                    "succeeded": len(scans),
                    "gaps": len(gaps),
                    "total_channels": len(cached_channels),
                    "done": next_cursor >= len(cached_channels),
                    "complete_inventory": complete_index_ready,
                },
            }
            result = {
                "account": account_record,
                "scans": scans,
                "scan_gaps": gaps,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "done": next_cursor >= len(cached_channels),
                "total_channels": len(cached_channels),
                "index_scope": (
                    "complete_account_inventory"
                    if complete_index_ready
                    else "explicit_observed_channels"
                    if explicit_index_ready
                    else "task_targeted_candidates"
                ),
            }
        elif operation == "list_servers":
            result = {
                "servers": self._query_results(
                    prefix="*", query=query, limit=limit, max_scrolls=max_scrolls
                )
            }
            self._close_switcher()
        elif operation == "find_channels":



            account_observation = self._current_account()
            account_record = dict(account_observation.get("account") or {})
            channels = self._query_results(
                prefix="#", query=query, limit=limit, max_scrolls=max_scrolls
            )
            query_coverage = dict(self.coverage.get("channels") or {})
            self._close_switcher()
            if not account_record.get("observed") or not _account_identity(account_record):
                raise DiscordInspectionError(
                    "The current Discord account could not be observed for candidate binding"
                )
            cached_candidate_account = dict(
                self.inventory_cache.get("candidate_account") or {}
            )
            if (
                cached_candidate_account
                and _account_identity(cached_candidate_account)
                != _account_identity(account_record)
            ):
                self.inventory_cache.pop("candidate_channels", None)
                self.inventory_cache.pop("candidate_queries", None)
            candidate_by_key = {
                str(record.get("channel_key") or _channel_key(
                    record.get("channel"), record.get("server")
                )): dict(record)
                for record in self.inventory_cache.get("candidate_channels") or []
                if isinstance(record, Mapping)
            }
            for record in channels:
                clean = {key: value for key, value in record.items() if key != "element"}
                candidate_by_key[str(clean.get("channel_key"))] = clean
            queries = [
                _text(value)
                for value in self.inventory_cache.get("candidate_queries") or []
                if _text(value)
            ]
            if query and query not in queries:
                queries.append(query)
            ordered_candidates = sorted(
                candidate_by_key.values(),
                key=lambda record: (
                    -_discovery_priority(record),
                    _semantic_text(record.get("server")),
                    _semantic_text(record.get("channel")),
                ),
            )
            self.inventory_cache.update(
                {
                    "schema": DISCORD_INVENTORY_CACHE_SCHEMA,
                    "candidate_account": account_record,
                    "candidate_channels": ordered_candidates,
                    "candidate_queries": queries,
                    "candidate_validated_for_task": True,
                    "candidate_created_at": time.time(),
                }
            )
            self.coverage = {
                "account": {"observed": True, "identity_bound": True},
                "channels": {
                    **query_coverage,
                    "source": "task_targeted_live_query",
                    "query": query,
                    "complete_inventory": False,
                    "candidate_index_size": len(ordered_candidates),
                },
            }
            result = {
                "account": account_record,
                "channels": channels,
                "candidate_index_size": len(ordered_candidates),
                "candidate_server_count": len(
                    {
                        _semantic_text(record.get("server"))
                        for record in ordered_candidates
                        if _semantic_text(record.get("server"))
                    }
                ),
                "index_scope": "task_targeted_candidates",
            }
        elif operation == "open_channel":
            if not channel and not query:
                raise ValueError("open_channel needs an exact channel or an unambiguous query")
            selected, window = self._open_channel(channel or query, max_scrolls=max_scrolls)
            result = {"selected_channel": selected, "window": window}
        else:
            selected = None
            if channel:
                selected, _window = self._open_channel(channel, max_scrolls=max_scrolls)
            result = self._read_channel(
                limit=limit,
                max_scrolls=max_scrolls,
                selected_channel=selected,
            )
            if selected is not None:
                result["selected_channel"] = selected

        return {
            "schema": DISCORD_INSPECTION_SCHEMA,
            "status": "succeeded",
            "operation": operation,
            **result,
            "coverage": dict(self.coverage),
            "substeps": list(self.substeps),
        }

    @staticmethod
    def _bounded_integer(value: Any, default: int, minimum: int, maximum: int, name: str) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Discord {name} must be a whole number")
        if not minimum <= value <= maximum:
            raise ValueError(f"Discord {name} must be between {minimum} and {maximum}")
        return value

    def _call(
        self,
        capability: str,
        function: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        arguments: Mapping[str, Any],
        *,
        required: bool = True,
    ) -> dict[str, Any]:
        self._ensure_active()
        call_arguments = dict(arguments)
        if capability == "ui.automation":
            call_arguments.setdefault("timeout_ms", self._uia_timeout_ms)
        command = _text(
            call_arguments.get("command") or call_arguments.get("action")
        )
        subject = _text(
            call_arguments.get("name")
            or call_arguments.get("scope_name")
            or call_arguments.get("control_type")
            or call_arguments.get("target")
        )
        started = time.monotonic()
        self._progress(
            f"{capability}: {command or 'call'}"
            + (f" - {subject}" if subject else ""),
            force=True,
            phase="starting",
            capability=capability,
            command=command or None,
            subject=subject or None,
        )
        try:
            result = dict(function(call_arguments) or {})
        except Exception as error:
            result = {
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "failure_kind": getattr(error, "kind", type(error).__name__),
            }
        self._ensure_active()
        self.substeps.append(
            {
                "capability": capability,
                "arguments": call_arguments,
                "status": result.get("status"),
                "command": result.get("command") or result.get("action"),
                "count": result.get("count"),
                "failure_kind": result.get("failure_kind"),
                "error": result.get("error"),
            }
        )
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        self._progress(
            f"{capability}: {command or 'call'} "
            f"{_text(result.get('status') or 'finished')}"
            + (f" - {subject}" if subject else ""),
            force=True,
            phase="finished",
            capability=capability,
            command=command or None,
            subject=subject or None,
            status=result.get("status"),
            duration_ms=duration_ms,
            count=result.get("count"),
        )
        if required and result.get("status") != "succeeded":
            raise DiscordInspectionError(
                _text(result.get("error"))
                or f"{capability} did not succeed during Discord inspection"
            )
        return result

    def _discord_window(self) -> dict[str, Any]:
        listed = self._call("window.control", self.window, {"action": "list"})
        matches = [
            dict(item)
            for item in listed.get("windows") or []
            if self._is_discord_title(item.get("title"))
        ]
        if not matches:
            self._call(
                "application.launch",
                self.launch,
                {"target": "discord", "wait_ms": 5_000},
            )
            for _attempt in range(20):
                listed = self._call("window.control", self.window, {"action": "list"})
                matches = [
                    dict(item)
                    for item in listed.get("windows") or []
                    if self._is_discord_title(item.get("title"))
                ]
                if matches:
                    break
                self.sleep(0.25)
        if not matches:
            raise DiscordInspectionError("Discord did not expose a visible top-level window")
        focused = [item for item in matches if item.get("focused")]
        return focused[0] if focused else matches[0]

    def _focus(self) -> dict[str, Any]:
        window = self._discord_window()
        focused = self._call(
            "window.control",
            self.window,
            {"action": "focus", "handle": int(window["handle"])},
        )
        return dict(focused.get("window") or window)

    @staticmethod
    def _is_discord_title(value: Any) -> bool:
        title = _text(value).casefold()
        return title in {"discord", "- discord"} or title.endswith(" - discord")

    def _find(
        self,
        *,
        process_id: int | None = None,
        element: str | None = None,
        name: str | None = None,
        control_type: str | None = None,
        pattern: str | None = None,
        exact: bool = False,
        limit: int = 10,
        visible_only: bool = True,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "command": "find_control",
            "limit": limit,
            "visible_only": visible_only,
            "enabled_only": enabled_only,
        }
        for key, value in (
            ("process_id", process_id),
            ("element", element),
            ("name", name),
            ("control_type", control_type),
            ("pattern", pattern),
        ):
            if value is not None:
                payload[key] = value
        if name is not None:
            payload["exact"] = exact
        result = self._call("ui.automation", self.uia, payload)
        return [dict(item) for item in result.get("matches") or []]

    def _quick_switcher(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        window = self._discord_window()
        process_id = int(window["process_id"])

        def controls() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            combos = self._find(
                process_id=process_id,
                name="Quick Switcher",
                control_type="ComboBox",
                exact=True,
                limit=2,
            )
            scopes = self._find(
                process_id=process_id,
                name="Quick Switcher",
                control_type="Window",
                exact=True,
                limit=2,
            )
            return (combos[0] if len(combos) == 1 else None, scopes[0] if len(scopes) == 1 else None)

        combo, scope = controls()
        if combo is not None and scope is not None:
            return window, combo, scope
        for _attempt in range(8):
            try:
                window = self._focus()
            except DiscordInspectionError:
                continue
            dispatched = self._call(
                "input.control",
                self.input_control,
                {
                    "action": "key_combo",
                    "combo": "Ctrl+K",
                    "expected_window_handle": int(window["handle"]),
                    "expected_process_id": int(window["process_id"]),
                    "post_action_delay_ms": 350,
                },
                required=False,
            )
            if dispatched.get("status") != "succeeded":
                continue
            combo, scope = controls()
            if combo is not None and scope is not None:
                return window, combo, scope
        raise DiscordInspectionError(
            "Discord's Quick Switcher could not be opened under stable foreground focus"
        )

    def _query_results(
        self,
        *,
        prefix: str,
        query: str,
        limit: int,
        max_scrolls: int,
        _state_retry: int = 0,
    ) -> list[dict[str, Any]]:
        window, combo, scope = self._quick_switcher()
        cleaned = query.lstrip("*#! ").strip()


        value = prefix + (f" {cleaned}" if cleaned else " ")
        self._call(
            "ui.automation",
            self.uia,
            {
                "command": "set_value",
                "element": combo["element"],
                "name": "Quick Switcher",
                "value": value,
                "post_action_delay_ms": 700,
            },
        )

        found: dict[str, dict[str, Any]] = {}
        boundary_reached = False
        boundary_basis: str | None = None
        last_percent: float | None = None
        pages_read = 0

        def remember(items: list[dict[str, Any]]) -> None:
            for item in items:
                name = _text(item.get("name"))
                if not name:
                    continue
                parsed = _CHANNEL_RESULT.match(name)




                if prefix == "*" and (
                    parsed or not name.casefold().endswith(", server")
                ):
                    continue
                if prefix == "#" and parsed is None:
                    continue
                record = {
                    "name": name,
                    "kind": "channel"
                    if parsed
                    else "server"
                    if name.endswith(", Server")
                    else "result",
                    "channel": _text(parsed.group("channel")) if parsed else None,
                    "channel_type": _text(parsed.group("channel_type")) if parsed else None,
                    "server": _text(parsed.group("server"))
                    if parsed
                    else name.removesuffix(", Server"),
                    "channel_key": (
                        _channel_key(parsed.group("channel"), parsed.group("server"))
                        if parsed
                        else None
                    ),
                    "element": item.get("element"),
                }
                patterns = list(item.get("patterns") or [])
                if patterns:
                    record["patterns"] = patterns
                found.setdefault(name.casefold(), record)

        bulk = self._call(
            "ui.automation",
            self.uia,
            {
                "command": "collect_list",
                "process_id": int(window["process_id"]),
                "scope_name": "Quick Switcher",
                "scope_control_type": "Window",
                "item_control_type": "ListItem",
                "scroller_name": "Results",
                "limit": limit,
                "max_scrolls": max_scrolls,
                "post_scroll_delay_ms": 60,
            },
            required=False,
        )
        if bulk.get("status") == "succeeded":
            bulk_items = list(bulk.get("items") or [])
            remember(bulk_items)
            if bulk_items and not found:
                self._close_switcher()
                if _state_retry < 2:
                    self.sleep(0.15)
                    return self._query_results(
                        prefix=prefix,
                        query=query,
                        limit=limit,
                        max_scrolls=max_scrolls,
                        _state_retry=_state_retry + 1,
                    )
                expected = "server" if prefix == "*" else "channel"
                raise DiscordInspectionError(
                    "Discord Quick Switcher kept stale results instead of the "
                    f"requested {expected} filter"
                )
            pages_read = int(bulk.get("pages_read") or 0)
            last_percent = bulk.get("last_vertical_percent")
            if bool(bulk.get("scroll_boundary_reached")):
                self.coverage["servers" if prefix == "*" else "channels"] = {
                    "query": value,
                    "unique_results": len(found),
                    "pages_read": pages_read,
                    "scroll_boundary_reached": True,
                    "boundary_basis": bulk.get("boundary_basis"),
                    "last_vertical_percent": last_percent,
                    "truncated_by_limit": bool(bulk.get("truncated_by_limit")),
                    "max_scrolls": max_scrolls,
                    "collector": "native_bulk_uia",
                }
                return list(found.values())[:limit]




        if str(bulk.get("failure_kind") or "").casefold() == "timeout":
            raise DiscordInspectionError(
                "Discord Quick Switcher collection reached its 5-second UI "
                "Automation deadline"
            )

        def keyboard_walk() -> tuple[bool, int]:
            """Finish a virtualized result list when percent scrolling closes it."""

            try:
                key_window, key_combo, _key_scope = self._quick_switcher()
                self._call(
                    "ui.automation",
                    self.uia,
                    {
                        "command": "set_value",
                        "element": key_combo["element"],
                        "name": "Quick Switcher",
                        "value": value,
                        "post_action_delay_ms": 500,
                    },
                )
            except DiscordInspectionError:
                return False, 0
            unchanged = 0
            reads = 0
            key_budget = min(500, max(20, max_scrolls * 10))
            for _index in range(key_budget):
                scopes = self._find(
                    process_id=int(key_window["process_id"]),
                    name="Quick Switcher",
                    control_type="Window",
                    exact=True,
                    limit=2,
                )
                if len(scopes) != 1:
                    return False, reads
                visible = self._find(
                    element=scopes[0]["element"],
                    control_type="ListItem",
                    limit=50,
                    visible_only=True,
                    enabled_only=False,
                )
                reads += 1
                previous_count = len(found)
                remember(visible)
                if len(found) >= limit:
                    return False, reads
                unchanged = unchanged + 1 if len(found) == previous_count else 0



                if unchanged >= max(8, len(visible) + 2):
                    return True, reads
                dispatched = self._call(
                    "input.control",
                    self.input_control,
                    {
                        "action": "key_press",
                        "key": "down",
                        "expected_window_handle": int(key_window["handle"]),
                        "expected_process_id": int(key_window["process_id"]),
                        "post_action_delay_ms": 80,
                    },
                    required=False,
                )
                if dispatched.get("status") != "succeeded":
                    try:
                        key_window = self._focus()
                    except DiscordInspectionError:
                        continue
            return False, reads

        for scroll_index in range(max_scrolls + 1):
            pages_read += 1


            scopes = self._find(
                process_id=int(window["process_id"]),
                name="Quick Switcher",
                control_type="Window",
                exact=True,
                limit=2,
            )
            if len(scopes) != 1:
                keyboard_boundary, keyboard_reads = keyboard_walk()
                pages_read += keyboard_reads
                if keyboard_boundary:
                    boundary_reached = True
                    boundary_basis = "keyboard_result_walk_exhausted"
                break
            scope = scopes[0]
            visible = self._find(
                element=scope["element"],
                control_type="ListItem",
                limit=50,
                visible_only=True,
                enabled_only=False,
            )
            remember(visible)
            if len(found) >= limit or scroll_index >= max_scrolls:
                break
            scrollers = self._find(
                element=scope["element"],
                pattern="Scroll",
                limit=10,
                visible_only=True,
                enabled_only=False,
            )
            scroller = next(
                (
                    item
                    for item in scrollers
                    if _text(item.get("name")).casefold() == "results"
                ),
                scrollers[0] if scrollers else None,
            )
            if scroller is None:
                lists = self._find(
                    element=scope["element"],
                    control_type="List",
                    limit=5,
                    visible_only=True,
                    enabled_only=False,
                )
                scroller = lists[0] if lists else None
            if scroller is None:
                break
            properties = self._call(
                "ui.automation",
                self.uia,
                {"command": "get_properties", "element": scroller["element"]},
                required=False,
            )
            scroll = dict((properties.get("element") or {}).get("scroll") or {})
            try:
                current_percent = float(scroll.get("vertical_percent"))
                view_size = float(scroll.get("vertical_view_size"))
            except (TypeError, ValueError):
                current_percent = -1.0
                view_size = 0.0
            if scroll and scroll.get("vertical_scrollable") is False:
                boundary_reached = True
                boundary_basis = "scroll_pattern_not_scrollable"
                break
            if scroll.get("vertical_scrollable") is True and current_percent >= 0:
                last_percent = current_percent
                if current_percent >= 99.999:
                    boundary_reached = True
                    boundary_basis = "vertical_percent_100"
                    break
                next_percent = min(100.0, current_percent + max(5.0, view_size * 0.9))
                scroll_arguments = {
                    "command": "scroll",
                    "element": scroller["element"],
                    "vertical_percent": next_percent,
                    "post_action_delay_ms": 250,
                }
            else:
                all_accessible = self._find(
                    element=scope["element"],
                    control_type="ListItem",
                    limit=50,
                    visible_only=False,
                    enabled_only=False,
                )
                remember(all_accessible)
                if len(all_accessible) < 50:
                    boundary_reached = True
                    boundary_basis = "all_accessible_descendants_exhausted"
                    break
                scroll_arguments = {
                    "command": "scroll",
                    "element": scroller["element"],
                    "amount": 1,
                    "post_action_delay_ms": 250,
                }
            scrolled = self._call(
                "ui.automation", self.uia, scroll_arguments, required=False
            )
            if scrolled.get("status") != "succeeded":
                keyboard_boundary, keyboard_reads = keyboard_walk()
                pages_read += keyboard_reads
                if keyboard_boundary:
                    boundary_reached = True
                    boundary_basis = "keyboard_result_walk_exhausted"
                break
            result_scroll = dict(scrolled.get("scroll") or {})
            try:
                last_percent = float(result_scroll.get("vertical_percent"))
            except (TypeError, ValueError):
                pass
            if last_percent is not None and last_percent >= 99.999:

                boundary_reached = True
                boundary_basis = "vertical_percent_100"
        self.coverage["servers" if prefix == "*" else "channels"] = {
            "query": value,
            "unique_results": len(found),
            "pages_read": pages_read,
            "scroll_boundary_reached": boundary_reached,
            "boundary_basis": boundary_basis,
            "last_vertical_percent": last_percent,
            "truncated_by_limit": len(found) >= limit,
            "max_scrolls": max_scrolls,
        }
        return list(found.values())[:limit]

    def _open_channel(
        self, channel: str, *, max_scrolls: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        requested = _text(channel)
        parsed_requested = _CHANNEL_RESULT.match(requested)
        requested_channel = (
            _text(parsed_requested.group("channel")) if parsed_requested else requested
        )
        requested_server = (
            _text(parsed_requested.group("server")) if parsed_requested else ""
        )


        query = _semantic_text(requested_channel) or requested_channel
        identity = (
            _channel_key(requested_channel, requested_server)
            if parsed_requested
            else None
        )
        requested_type = (
            _text(parsed_requested.group("channel_type"))
            if parsed_requested
            else ""
        )

        def choose(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
            exact = [
                item
                for item in items
                if _text(item.get("name")).casefold() == requested.casefold()
            ]
            full_semantic = [
                item
                for item in items
                if _semantic_text(item.get("name")) == _semantic_text(requested)
            ]
            identity_matches = (
                [item for item in items if item.get("channel_key") == identity]
                if identity
                else []
            )
            typed_identity = [
                item
                for item in identity_matches
                if not requested_type
                or _text(item.get("channel_type")).casefold()
                == requested_type.casefold()
            ]
            semantic = [
                item
                for item in items
                if _semantic_text(item.get("channel"))
                == _semantic_text(requested_channel)
                and (
                    not requested_server
                    or _semantic_text(item.get("server"))
                    == _semantic_text(requested_server)
                )
            ]
            typed_semantic = [
                item
                for item in semantic
                if not requested_type
                or _text(item.get("channel_type")).casefold()
                == requested_type.casefold()
            ]
            identity_choice = (
                identity_matches
                if len(identity_matches) == 1
                else typed_identity
            )
            identity_basis = (
                "stable_channel_key"
                if len(identity_matches) == 1
                else "typed_stable_channel_key"
            )
            for candidates, basis in (
                (exact, "exact_visible_name"),
                (identity_choice, identity_basis),
                (full_semantic, "exact_semantic_result"),
                (typed_semantic, "typed_semantic_channel"),
                (semantic, "unique_semantic_channel"),
            ):
                if candidates:
                    return candidates, basis
            return [], "not_found"

        common_server_tokens = {
            "and",
            "community",
            "discord",
            "for",
            "free",
            "official",
            "server",
            "the",
        }
        server_tokens = [
            token
            for token in _semantic_text(requested_server).split()
            if len(token) >= 3 and token not in common_server_tokens
        ]
        server_tokens.sort(key=lambda token: (-len(token), token))
        query_candidates = [
            f"{query} {token}" for token in server_tokens
        ]
        query_candidates.extend([query, requested_channel])
        query_candidates = list(
            dict.fromkeys(_text(value) for value in query_candidates if _text(value))
        )[:MAX_CHANNEL_QUERY_CANDIDATES]
        selected: dict[str, Any] | None = None
        match_basis = "not_found"
        candidate_count = 0
        last_error: Exception | None = None
        navigation_started = time.monotonic()
        for query_index, query_value in enumerate(query_candidates):
            if (
                time.monotonic() - navigation_started
                >= CHANNEL_NAVIGATION_BUDGET_SECONDS
            ):
                last_error = DiscordInspectionError(
                    "Discord channel navigation reached its 25-second budget"
                )
                break
            channels = self._query_results(
                prefix="#",
                query=query_value,
                limit=100,
                max_scrolls=max_scrolls if query_index == 0 else min(max_scrolls, 3),
            )
            candidates, basis = choose(channels)
            candidate_count = max(candidate_count, len(candidates))
            if len(candidates) != 1 or not candidates[0].get("element"):
                continue
            candidate = candidates[0]
            try:
                invoked = self._call(
                    "ui.automation",
                    self.uia,
                    {
                        "command": "invoke",
                        "element": candidate["element"],
                        "name": candidate["name"],
                        "post_action_delay_ms": 900,
                    },
                )
            except Exception as error:
                last_error = error
                continue
            if invoked.get("status") == "succeeded":
                selected = candidate
                match_basis = basis
                break
        if (
            selected is None
            and requested_server
            and time.monotonic() - navigation_started
            < CHANNEL_NAVIGATION_BUDGET_SECONDS
        ):






            try:
                self._open_server(requested_server)
                self.sleep(0.35)
                local_channels = self._query_results(
                    prefix="#",
                    query="",
                    limit=MAX_RESULTS,
                    max_scrolls=max_scrolls,
                )
                candidates, basis = choose(local_channels)
                candidate_count = max(candidate_count, len(candidates))
                if len(candidates) == 1 and candidates[0].get("element"):
                    candidate = candidates[0]
                    invoked = self._call(
                        "ui.automation",
                        self.uia,
                        {
                            "command": "invoke",
                            "element": candidate["element"],
                            "name": candidate["name"],
                            "post_action_delay_ms": 900,
                        },
                    )
                    if invoked.get("status") == "succeeded":
                        selected = candidate
                        match_basis = f"server_scoped_{basis}"
            except Exception as error:
                last_error = error
        if selected is None:
            if last_error is not None:
                raise DiscordInspectionError(
                    f"The observed Discord channel could not be opened: {last_error}"
                ) from last_error
            raise DiscordInspectionError(
                f"Channel query was {'not found' if candidate_count == 0 else 'ambiguous'}: {requested}"
            )
        window = self._call(
            "ui.automation", self.uia, {"command": "get_active_window"}
        ).get("window") or {}
        title = _text(window.get("name") or window.get("title"))
        readback = _DISCORD_TITLE.match(title)
        selected_key = _channel_key(selected.get("channel"), selected.get("server"))
        readback_matches = bool(
            readback
            and _channel_key(readback.group("channel"), readback.group("server"))
            == selected_key
        )
        if not readback_matches:
            listed = self._call(
                "window.control", self.window, {"action": "list"}
            )
            candidates = []
            for candidate in listed.get("windows") or []:
                candidate_title = _text(candidate.get("title"))
                parsed = _DISCORD_TITLE.match(candidate_title)
                if parsed is None:
                    continue
                if _channel_key(
                    parsed.group("channel"), parsed.group("server")
                ) == selected_key:
                    candidates.append((dict(candidate), parsed, candidate_title))
            if len(candidates) == 1:
                window, readback, title = candidates[0]
                readback_matches = True
        if readback is None or not readback_matches:
            raise DiscordInspectionError(
                "Discord channel navigation did not produce a matching fresh title readback"
            )
        selected = {
            **selected,
            "readback_channel": _text(readback.group("channel")),
            "readback_server": _text(readback.group("server")),
            "window_title": title,
            "selection_match_basis": match_basis,
            "readback_matches_channel_key": True,
        }
        return selected, dict(window)

    def _open_server(self, server: str) -> dict[str, Any]:
        requested = _text(server).removesuffix(", Server")
        requested_identity = _semantic_text(requested)
        listed = self._call("window.control", self.window, {"action": "list"})
        selected_titles = []
        for item in listed.get("windows") or []:
            title = _text(item.get("title"))
            parsed_title = _DISCORD_TITLE.match(title)
            if (
                parsed_title is not None
                and _semantic_text(parsed_title.group("server"))
                == requested_identity
            ):
                selected_titles.append(title)
        if len(selected_titles) == 1:
            return {
                "name": f"{requested}, Server",
                "kind": "server",
                "server": requested,
                "element": None,
                "window_title": selected_titles[0],
                "selection_match_basis": "already_selected_window_title",
            }
        candidates: list[dict[str, Any]] = []
        queries = tuple(
            dict.fromkeys(
                value
                for value in (
                    requested,
                    requested.casefold(),
                    requested_identity,
                )
                if value
            )
        )
        for query in queries:
            servers = self._query_results(
                prefix="*", query=query, limit=20, max_scrolls=0
            )
            candidates = [
                item
                for item in servers
                if _semantic_text(
                    _text(item.get("server") or item.get("name")).removesuffix(
                        ", Server"
                    )
                )
                == requested_identity
            ]
            if candidates:
                break
            self._close_switcher()
        if not candidates:





            servers = self._query_results(
                prefix="*",
                query="",
                limit=MAX_RESULTS,
                max_scrolls=MAX_SCROLLS,
            )
            candidates = [
                item
                for item in servers
                if _semantic_text(
                    _text(item.get("server") or item.get("name")).removesuffix(
                        ", Server"
                    )
                )
                == requested_identity
            ]
        if len(candidates) != 1:
            raise DiscordInspectionError(
                f"Server query was {'not found' if not candidates else 'ambiguous'}: {requested}"
            )
        selected = candidates[0]
        invoked = self._call(
            "ui.automation",
            self.uia,
            {
                "command": "invoke",
                "element": selected["element"],
                "name": selected["name"],
                "post_action_delay_ms": 700,
            },
            required=False,
        )
        if invoked.get("status") != "succeeded":
            replacement = self._invoke_visible_quick_switcher_result(
                value="* ",
                exact_name=_text(selected.get("name")),
            )
            if replacement is None:
                raise DiscordInspectionError(
                    "The observed Discord server row became stale before it "
                    f"could be invoked: {requested}"
                )
            selected = replacement
        listed = self._call("window.control", self.window, {"action": "list"})
        readbacks = [
            _text(item.get("title"))
            for item in listed.get("windows") or []
            if self._is_discord_title(item.get("title"))
            and requested.casefold() in _text(item.get("title")).casefold()
        ]
        if not readbacks:
            raise DiscordInspectionError(
                f"Discord server navigation had no fresh title readback: {requested}"
            )
        return {**selected, "window_title": readbacks[0]}

    def _invoke_visible_quick_switcher_result(
        self,
        *,
        value: str,
        exact_name: str,
    ) -> dict[str, Any] | None:
        """Invoke one freshly visible virtual row without cached handles or Enter."""

        window, combo, _scope = self._quick_switcher()
        self._call(
            "ui.automation",
            self.uia,
            {
                "command": "set_value",
                "element": combo["element"],
                "name": "Quick Switcher",
                "value": value,
                "post_action_delay_ms": 500,
            },
        )
        for _index in range(500):
            self._ensure_active()
            scopes = self._find(
                process_id=int(window["process_id"]),
                name="Quick Switcher",
                control_type="Window",
                exact=True,
                limit=2,
            )
            if len(scopes) != 1:
                return None
            visible = self._find(
                element=scopes[0]["element"],
                control_type="ListItem",
                limit=50,
                visible_only=True,
                enabled_only=False,
            )
            matches = [
                item
                for item in visible
                if _text(item.get("name")).casefold() == exact_name.casefold()
            ]
            if len(matches) == 1 and matches[0].get("element"):
                invoked = self._call(
                    "ui.automation",
                    self.uia,
                    {
                        "command": "invoke",
                        "element": matches[0]["element"],
                        "name": matches[0]["name"],
                        "post_action_delay_ms": 700,
                    },
                    required=False,
                )
                if invoked.get("status") == "succeeded":
                    return dict(matches[0])
            moved = self._call(
                "input.control",
                self.input_control,
                {
                    "action": "key_press",
                    "key": "down",
                    "expected_window_handle": int(window["handle"]),
                    "expected_process_id": int(window["process_id"]),
                    "post_action_delay_ms": 80,
                },
                required=False,
            )
            if moved.get("status") != "succeeded":
                try:
                    window = self._focus()
                except DiscordInspectionError:
                    continue
        return None

    def _read_channel(
        self,
        *,
        limit: int,
        max_scrolls: int,
        selected_channel: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        window = self._discord_window()
        process_id = int(window["process_id"])
        lists = self._find(
            process_id=process_id,
            name="Messages in",
            control_type="List",
            exact=False,
            limit=5,
            visible_only=True,
            enabled_only=False,
        )
        lists = [item for item in lists if _text(item.get("name")).casefold().startswith("messages in")]
        if len(lists) != 1:
            raise DiscordInspectionError("The current Discord channel has no unambiguous message list")
        messages: list[str] = []
        seen: set[str] = set()
        message_list = lists[0]
        scroll_attempts = 0
        message_boundary_reached = False
        message_boundary_basis: str | None = None
        bulk = self._call(
            "ui.automation",
            self.uia,
            {
                "command": "collect_list",
                "process_id": process_id,
                "scope_name": "Messages in",
                "scope_control_type": "List",
                "scope_exact": False,
                "item_control_type": "ListItem",
                "scroller_name": "",
                "limit": limit,
                "max_scrolls": max_scrolls,
                "post_scroll_delay_ms": 60,
                "reverse": True,
            },
            required=False,
        )
        if bulk.get("status") == "succeeded":
            for item in bulk.get("items") or []:
                name = _text(item.get("name"))
                if name and name not in seen:
                    seen.add(name)
                    messages.append(name[:800])
            scroll_attempts = max(0, int(bulk.get("pages_read") or 1) - 1)
            message_boundary_reached = bool(bulk.get("scroll_boundary_reached"))
            message_boundary_basis = _text(bulk.get("boundary_basis")) or None
            if bulk.get("truncated_by_limit"):
                message_boundary_reached = False
                message_boundary_basis = "requested_limit_reached"
        elif str(bulk.get("failure_kind") or "").casefold() == "timeout":
            raise DiscordInspectionError(
                "Discord message collection reached its "
                f"{self._uia_timeout_ms / 1000:g}-second UI Automation deadline"
            )
        else:
            read_started = time.monotonic()
            read_budget_seconds = min(
                60.0,
                max(CHANNEL_READ_BUDGET_SECONDS, float(max_scrolls) * 2.0),
            )
            for scroll_index in range(max_scrolls + 1):
                if time.monotonic() - read_started >= read_budget_seconds:
                    message_boundary_basis = "channel_read_time_budget_reached"
                    break
                tree = self._call(
                    "ui.automation",
                    self.uia,
                    {
                        "command": "get_tree",
                        "element": message_list["element"],
                        "depth": 8,
                        "max_nodes": 400,
                    },
                )
                before = len(messages)
                for node in tree.get("nodes") or []:
                    if node.get("control_type") != "ListItem":
                        continue
                    name = _text(node.get("name"))
                    if name and name not in seen:
                        seen.add(name)
                        messages.append(name[:800])
                if len(messages) >= limit or scroll_index >= max_scrolls:
                    if len(messages) >= limit:
                        message_boundary_basis = "requested_limit_reached"
                    break
                scroll_attempts += 1
                scrolled = self._call(
                    "ui.automation",
                    self.uia,
                    {
                        "command": "scroll",
                        "element": message_list["element"],
                        "amount": -1,
                        "post_action_delay_ms": 250,
                    },
                    required=False,
                )
                if scrolled.get("status") != "succeeded":
                    message_boundary_basis = "message_scroll_failed"
                    break
                if len(messages) == before:
                    message_boundary_reached = True
                    message_boundary_basis = "no_new_accessible_messages"
                    break
                lists = self._find(
                    process_id=process_id,
                    name="Messages in",
                    control_type="List",
                    exact=False,
                    limit=5,
                    visible_only=True,
                    enabled_only=False,
                )
                lists = [
                    item
                    for item in lists
                    if _text(item.get("name")).casefold().startswith("messages in")
                ]
                if len(lists) != 1:
                    message_boundary_basis = "message_list_lost_after_repaint"
                    break
                message_list = lists[0]
        giveaway_records = [
            (message_index, message)
            for message_index, message in enumerate(messages)
            if _GIVEAWAY_EVIDENCE.search(message)
            or _ENDED_EVIDENCE.search(message)
            or _ACTIVE_EVIDENCE.search(message)
        ]
        giveaway_evidence = [evidence for _message_index, evidence in giveaway_records]
        requirements = self._extract_requirements(giveaway_evidence)
        giveaway_items: list[dict[str, Any]] = []
        observed_at_timestamp = int(time.time())
        for evidence_index, (message_index, evidence) in enumerate(giveaway_records):
            timestamp_match = _END_TIMESTAMP.search(evidence)
            end_timestamp = (
                int(timestamp_match.group("timestamp"))
                if timestamp_match is not None
                else None
            )
            timestamp_state = (
                "ended"
                if end_timestamp is not None
                and end_timestamp <= observed_at_timestamp
                else "active"
                if end_timestamp is not None
                and end_timestamp > observed_at_timestamp
                else None
            )
            item_state = (
                "ended"
                if _ENDED_EVIDENCE.search(evidence)
                else timestamp_state
                if timestamp_state is not None
                else "active"
                if _ACTIVE_EVIDENCE.search(evidence)
                else "unknown"
            )
            state_basis = (
                "explicit_ended_text"
                if _ENDED_EVIDENCE.search(evidence)
                else "end_timestamp_in_past"
                if timestamp_state == "ended"
                else "end_timestamp_in_future"
                if timestamp_state == "active"
                else "explicit_active_text"
                if _ACTIVE_EVIDENCE.search(evidence)
                else "insufficient_lifecycle_evidence"
            )
            item_requirements = self._extract_requirements([evidence])
            item_manual_count = sum(
                requirement.get("type") == "manual_review"
                for requirement in item_requirements
            )
            end_match = _END_EVIDENCE.search(evidence)
            event_key = hashlib.sha256(
                (
                    f"{_semantic_text((selected_channel or {}).get('server'))}|"
                    f"{_semantic_text((selected_channel or {}).get('channel'))}|"
                    f"{_semantic_text(evidence)}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            giveaway_items.append(
                {
                    "event_key": event_key,
                    "evidence_index": evidence_index,
                    "source_message_index": message_index,
                    "evidence": evidence,
                    "state": item_state,
                    "state_basis": state_basis,
                    "end_evidence": _text(end_match.group(0)) if end_match else None,
                    "end_timestamp": end_timestamp,
                    "observed_at_timestamp": observed_at_timestamp,
                    "is_newest_observed": evidence_index == len(giveaway_records) - 1,
                    "requirements": item_requirements,
                    "criteria_status": (
                        "needs_manual_review"
                        if item_manual_count
                        else "structured_requirements_observed"
                        if item_requirements
                        else "no_requirement_text_observed"
                    ),
                    "disposition": (
                        "ended"
                        if item_state == "ended"
                        else "requirements_unverified"
                        if item_state == "active" and item_requirements
                        else "active_without_observed_requirements"
                        if item_state == "active"
                        else "state_unverified"
                    ),
                }
            )
        active_giveaway_items = [
            item for item in giveaway_items if item.get("state") == "active"
        ]
        ended_giveaway_items = [
            item for item in giveaway_items if item.get("state") == "ended"
        ]
        unknown_giveaway_items = [
            item for item in giveaway_items if item.get("state") == "unknown"
        ]
        explicit_state = (
            "active"
            if active_giveaway_items
            else "ended"
            if ended_giveaway_items and not unknown_giveaway_items
            else "unknown"
        )
        unassociated_rule_evidence = []
        giveaway_set = set(giveaway_evidence)
        for message_index, message in enumerate(messages):
            if message in giveaway_set:
                continue
            extracted = self._extract_requirements([message])
            if extracted:
                unassociated_rule_evidence.append(
                    {
                        "message_index": message_index,
                        "evidence": message,
                        "requirements": extracted,
                        "association": "unverified",
                    }
                )
        discovery_signals = {
            "giveaway_text": any("giveaway" in value.casefold() for value in messages),
            "verified_app_or_bot": any(
                "verified app" in value.casefold() or "giveawaybot" in value.casefold()
                for value in messages
            ),
            "entry_control_or_count": any(
                "enter giveaway" in value.casefold() or "entries:" in value.casefold()
                for value in messages
            ),
        }
        selected_name = _text(
            (selected_channel or {}).get("channel")
            or (selected_channel or {}).get("name")
        )
        gift_markers = ("\U0001f381", "\U0001f389", "\U0001f38a", "\U0001f4e6")
        discovery_signals = {
            **discovery_signals,
            "gift_icon": any(
                marker in value
                for value in [selected_name, *messages]
                for marker in gift_markers
            ),
            "decorative_symbol_in_channel_name": any(
                unicodedata.category(character) == "So" for character in selected_name
            ),
            "channel_name_keyword": "giveaway" in selected_name.casefold(),
        }
        lifecycle_evidence = explicit_state in {"active", "ended"}
        content_signals = sum(
            bool(discovery_signals[key])
            for key in (
                "giveaway_text",
                "verified_app_or_bot",
                "entry_control_or_count",
            )
        )
        candidate_classification = (
            "confirmed_giveaway"
            if lifecycle_evidence and content_signals >= 1
            else "probable_giveaway"
            if content_signals >= 2
            else "discovery_only"
            if any(discovery_signals.values())
            else "not_observed"
        )
        manual_requirements = sum(
            requirement.get("type") == "manual_review" for requirement in requirements
        )
        criteria_status = (
            "needs_manual_review"
            if manual_requirements
            else "structured_requirements_observed"
            if requirements
            else "no_requirement_text_observed"
        )
        disposition = (
            "ended"
            if explicit_state == "ended"
            else "requirements_unverified"
            if explicit_state == "active"
            and (requirements or not message_boundary_reached)
            else "ready_for_policy_review"
            if explicit_state == "active"
            and candidate_classification == "confirmed_giveaway"
            else "state_unverified"
        )
        self.coverage["messages"] = {
            "visible_unique_messages": len(messages),
            "requested_limit": limit,
            "scroll_attempts": scroll_attempts,
            "history_boundary_reached": message_boundary_reached,
            "boundary_basis": message_boundary_basis,
            "criteria_evidence_is_partial": not message_boundary_reached,
            "read_budget_seconds": (
                min(
                    60.0,
                    max(CHANNEL_READ_BUDGET_SECONDS, float(max_scrolls) * 2.0),
                )
            ),
        }
        return {
            "window": window,
            "message_list": _text(message_list.get("name")),
            "messages": messages[-limit:],
            "giveaway_evidence": giveaway_evidence[-limit:],
            "giveaway_items": giveaway_items[-limit:],
            "active_giveaway_items": active_giveaway_items[-limit:],
            "ended_giveaway_items": ended_giveaway_items[-limit:],
            "unknown_giveaway_items": unknown_giveaway_items[-limit:],
            "newest_observed_giveaway": (
                giveaway_items[-1] if giveaway_items else None
            ),
            "giveaway_state_counts": {
                "active": len(active_giveaway_items),
                "ended": len(ended_giveaway_items),
                "unknown": len(unknown_giveaway_items),
            },
            "unassociated_rule_evidence": unassociated_rule_evidence[-limit:],
            "explicit_state": explicit_state,
            "requirements": requirements,
            "discovery_signals": discovery_signals,
            "candidate_classification": candidate_classification,
            "criteria_status": criteria_status,
            "disposition": disposition,
        }

    @staticmethod
    def _requirement_record(
        requirement_type: str,
        text: str,
        source_index: int,
    ) -> dict[str, Any]:
        lowered = text.casefold()
        number = _first_number(text)
        key = f"{requirement_type}_verified"
        operator = "equals"
        expected: Any = True
        confidence = "medium"
        details: dict[str, Any] = {}

        if requirement_type == "entry_limit":
            limit = number or 1
            key = "prior_verified_entries"
            operator = "maximum"
            expected = max(0, limit - 1)
            details = {
                "entry_limit": limit,
                "scope": next(
                    (scope for scope in ("ip", "person", "account", "user") if scope in lowered),
                    "unknown",
                ),
            }
            confidence = "high"
        elif requirement_type in {"level", "invites", "messages"} and number is not None:
            key = {
                "level": "level",
                "invites": "valid_invite_count",
                "messages": "message_count",
            }[requirement_type]
            operator = "minimum"
            expected = number
            confidence = "high"
        elif requirement_type in {"account_age", "membership_age"}:
            duration = re.search(
                r"(?P<number>\d+)\s+(?P<unit>days?|weeks?|months?|years?)",
                text,
                re.I,
            )
            if duration:
                expected = int(duration.group("number")) * _DURATION_MULTIPLIERS[
                    duration.group("unit").casefold()
                ]
                key = (
                    "account_age_days"
                    if requirement_type == "account_age"
                    else "server_membership_days"
                )
                operator = "minimum"
                confidence = "high"
        elif requirement_type == "role":
            role_id = re.search(r"<@&(?P<id>\d+)>", text)
            named_role = re.search(
                r"\brole\b\s*(?:named|called|:|-)?\s*[\"'`]?"
                r"(?P<role>[\w -]{2,80})",
                text,
                re.I,
            )
            target = (
                role_id.group("id")
                if role_id
                else _text(named_role.group("role"))
                if named_role
                else ""
            )
            if target:
                key = "role_ids" if role_id else "roles"
                operator = "contains"
                expected = target
                confidence = "high" if role_id else "medium"
        elif requirement_type == "follow":
            target = re.sub(
                r"^(?:must|need|require(?:d)?\s+to\s+)?(?:follow|subscribe|join)\s+",
                "",
                text,
                flags=re.I,
            ).strip(" :-")
            if target:
                key = "completed_external_tasks"
                operator = "contains"
                expected = target[:160]
        elif requirement_type == "boost":
            key = "server_boost_active"
        elif requirement_type == "verification":
            key = "account_verified"
        elif requirement_type == "linked_account":
            platform = next(
                (
                    value
                    for value in (
                        "steam",
                        "xbox",
                        "playstation",
                        "epic",
                        "twitter",
                        "youtube",
                        "twitch",
                    )
                    if value in lowered
                ),
                "",
            )
            if platform:
                key = "linked_accounts"
                operator = "contains"
                expected = platform
                confidence = "high"
        elif requirement_type == "direct_messages":
            key = "direct_messages_enabled"
            confidence = "high"
        elif requirement_type == "age" and number is not None:
            key = "age_years"
            operator = "minimum"
            expected = number
            confidence = "high"
        elif requirement_type == "region":
            region = next(
                (
                    value
                    for value in ("us", "uk", "eu", "canada", "india", "bangladesh")
                    if re.search(rf"\b{value}\b", lowered)
                ),
                "",
            )
            if region:
                key = "region"
                operator = "equals"
                expected = region
                confidence = "high"

        return {
            "type": requirement_type,
            "text": text,
            "status": "unknown",
            "key": key,
            "operator": operator,
            "expected": expected,
            "confidence": confidence,
            "source_message_index": source_index,
            **details,
        }

    @classmethod
    def _extract_requirements(cls, evidence: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_index, value in enumerate(evidence):
            covered_spans: list[tuple[int, int]] = []
            for requirement_type, pattern in _REQUIREMENT_PATTERNS:
                for match in pattern.finditer(value):
                    text = _text(match.group(0))[:240]
                    identity = f"{requirement_type}:{text.casefold()}"
                    if not text or identity in seen:
                        continue
                    seen.add(identity)
                    covered_spans.append(match.span())
                    found.append(
                        cls._requirement_record(
                            requirement_type,
                            text,
                            source_index,
                        )
                    )



            for clause_match in re.finditer(r"[^\n;.!?]+", value):
                clause = _text(clause_match.group(0))[:240]
                if not clause or not _RULE_CLAUSE.search(clause):
                    continue
                if any(
                    start < clause_match.end() and clause_match.start() < end
                    for start, end in covered_spans
                ):
                    continue
                identity = f"manual_review:{clause.casefold()}"
                if identity in seen:
                    continue
                seen.add(identity)
                requirement_key = hashlib.sha256(
                    clause.casefold().encode("utf-8")
                ).hexdigest()[:16]
                found.append(
                    {
                        "type": "manual_review",
                        "text": clause,
                        "status": "unknown",
                        "key": f"manual_requirement:{requirement_key}",
                        "operator": "equals",
                        "expected": True,
                        "confidence": "explicit_unparsed_rule",
                        "source_message_index": source_index,
                    }
                )
        return found

    def _current_account(self) -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        last_window: dict[str, Any] = {}

        def observe() -> tuple[dict[str, Any], dict[str, Any]]:
            window = self._focus()
            process_id = int(window["process_id"])
            groups = self._find(
                process_id=process_id,
                name="User status and settings",
                control_type="Group",
                exact=True,
                limit=2,
            )
            if not groups:


                self._quick_switcher()
                self._close_switcher()
                groups = self._find(
                    process_id=process_id,
                    name="User status and settings",
                    control_type="Group",
                    exact=True,
                    limit=2,
                )
            if len(groups) != 1:
                return (
                    {
                        "label": "Current signed-in Discord account",
                        "observed": False,
                    },
                    window,
                )
            tree = self._call(
                "ui.automation",
                self.uia,
                {
                    "command": "get_tree",
                    "element": groups[0]["element"],
                    "depth": 4,
                    "max_nodes": 80,
                },
            )
            return self._extract_account(tree.get("nodes") or []), window

        for attempt in range(3):
            if attempt:
                self._close_switcher()
                self.sleep(0.12)
            account, last_window = observe()
            identity = _account_identity(account)
            if not account.get("observed") or not identity:
                continue
            observations.append(account)
            matching = [
                value
                for value in observations
                if _account_identity(value) == identity
            ]
            if len(matching) >= 2:
                confirmed = dict(matching[-1])
                confirmed["identity_confirmed"] = True
                confirmed["observation_count"] = len(observations)
                return {"account": confirmed, "window": last_window}




        return {
            "account": {
                "label": "Current signed-in Discord account",
                "handle": None,
                "observed": False,
                "identity_confirmed": False,
                "observation_count": len(observations),
            },
            "window": last_window,
        }

    @staticmethod
    def _extract_account(nodes: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Read identity only from Discord's account-popout control bounds."""

        account_button = next(
            (
                node
                for node in nodes
                if _text(node.get("name")) == "Manage profile and status"
                and node.get("control_type") == "Button"
            ),
            None,
        )
        bounds = dict((account_button or {}).get("bounds") or {})
        try:
            left = int(bounds["x"])
            top = int(bounds["y"])
            right = left + int(bounds["width"])
            bottom = top + int(bounds["height"])
        except (KeyError, TypeError, ValueError):
            left = top = right = bottom = 0

        def inside(node: Mapping[str, Any]) -> bool:
            if not account_button:



                return True
            node_bounds = dict(node.get("bounds") or {})
            try:
                x = int(node_bounds["x"])
                y = int(node_bounds["y"])
                width = int(node_bounds["width"])
                height = int(node_bounds["height"])
            except (KeyError, TypeError, ValueError):
                return False
            center_x = x + max(0, width // 2)
            center_y = y + max(0, height // 2)
            return left <= center_x <= right and top <= center_y <= bottom + 16

        status_labels = {"online", "idle", "do not disturb", "invisible"}
        texts = [
            _text(node.get("name"))
            for node in nodes
            if node.get("control_type") == "Text" and inside(node)
        ]
        handle = next(
            (
                value
                for value in texts
                if re.fullmatch(
                    r"(?:[.@][A-Za-z0-9_.-]{2,64}|"
                    r"[A-Za-z0-9_-]{2,32}\.[A-Za-z0-9_.-]{2,32})",
                    value,
                )
            ),
            None,
        )
        if handle is None:




            plain_handles = [
                value
                for value in texts
                if value == value.casefold()
                and re.fullmatch(r"[a-z0-9_][a-z0-9_.-]{1,31}", value)
                and value.casefold() not in status_labels
            ]
            if plain_handles:
                handle = plain_handles[-1]
        display = next(
            (
                value
                for value in texts
                if value != handle
                and value.casefold() not in status_labels
                and 2 <= len(value) <= 64
                and any(character.isalnum() for character in value)
            ),
            None,
        )
        return {
            "label": display or (handle or "").lstrip(".@") or "Current signed-in Discord account",
            "handle": handle,
            "observed": bool(display or handle),
        }

    def _close_switcher(self) -> None:
        windows = self._call("window.control", self.window, {"action": "list"})
        discord = next(
            (
                item
                for item in windows.get("windows") or []
                if self._is_discord_title(item.get("title"))
            ),
            None,
        )
        if not discord:
            return
        self._call(
            "window.control",
            self.window,
            {"action": "focus", "handle": int(discord["handle"])},
            required=False,
        )
        self._call(
            "input.control",
            self.input_control,
            {
                "action": "key_press",
                "key": "escape",
                "expected_window_handle": int(discord["handle"]),
                "expected_process_id": int(discord["process_id"]),
                "post_action_delay_ms": 100,
            },
            required=False,
        )


__all__ = [
    "DISCORD_INSPECTION_SCHEMA",
    "DISCORD_OPERATIONS",
    "DiscordDesktopInspector",
    "DiscordInspectionCancelled",
    "DiscordInspectionError",
]
