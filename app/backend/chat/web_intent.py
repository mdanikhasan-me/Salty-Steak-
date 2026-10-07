"""Per-turn public-web requests, independent of the persistent Research toggle."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def explicit_web_intent(request: str) -> str | None:
    # Structured payloads are data, not an instruction to follow quoted text.
    if str(request).lstrip().startswith(("{", "[")):
        return None
    text = re.sub(r"```[\s\S]*?```|`[^`]*`", " ", str(request)).casefold()
    if re.search(r"\b(?:do not|don't|never)\s+(?:use\s+)?(?:search|browse|research|the web|web search|the internet)\b"
                 r"|\b(?:without|no)\s+(?:web\s+search|browsing|internet\s+access|online\s+search)\b", text):
        return "off"
    if re.search(r"\b(?:deep|in[- ]depth|thorough)\s+research\b", text):
        return "deep"
    # A direct request to read a supplied public address does not require a
    # separate "search the web" incantation. Code/quoted payloads stay data.
    prose = re.sub(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d', ' ', text)
    if re.search(r'^\s*(?:please\s+)?(?:read|fetch|retrieve|summari[sz]e|check)\b.{0,120}https?://', prose):
        return 'search'
    # Asking for current offers or actual links is itself a retrieval request.
    # It must work in Blink/Cook/Lock In without the user knowing a toggle.
    # Keep explanations, coding examples, and purely hypothetical questions local.
    if not re.search(r"^\s*(?:explain|describe|translate|define|write|implement)\b", text):
        if re.search(r"\b(?:find|give|show|send|list|need|want)\b.{0,100}\b(?:links?|urls?|websites?)\b", text):
            return "search"
        shopping = re.search(r"\b(?:buy|shops?|shopping|cheapest|best price|in stock|affordable|budget|under\s+\d+)\b", text)
        offer = re.search(r"\b(?:find|recommend|where|which|what|compare|looking for|need|want)\b", text)
        definition = re.search(r"^\s*what\s+is\s+(?:a\s+|an\s+)?(?:budget|shopping|price)\s*[?.!]*$", text)
        if shopping and offer and not definition and not re.search(r"\b(?:algorithm|function|data structure|example|hypothetical|fictional)\b", text):
            return "search"
    if re.search(r"^\s*(?:what\s+(?:is|does)|explain|describe|translate|define)\b", text):
        return None
    if re.search(r"\b(?:search|browse|check|look\s*up|find|fetch|retrieve)\b.{0,85}"
                 r"\b(?:public\s+web|web|internet|online|websites?|sources?)\b"
                 r"|\b(?:web|online|internet)\s+search\b"
                 r"|^\s*(?:please\s+)?research\s+", text):
        return "search"
    return None


def effective_web_settings(settings: Mapping[str, Any], request: str) -> dict[str, Any]:
    result = dict(settings)
    intent = explicit_web_intent(request)
    if intent is None:
        return result
    result["web_intent_source"] = "explicit_user_request"
    result["web_intent"] = intent
    enabled = intent != "off"
    for key in ("research_available", "research_forced", "research_command", "web_search_enabled"):
        result[key] = enabled
    if enabled:
        result["research_profile"] = "cooking" if intent == "deep" else "instant"
    return result
