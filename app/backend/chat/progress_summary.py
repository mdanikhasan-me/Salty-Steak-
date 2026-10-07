"""Coarse progress descriptions; never quote the model's private trace."""
from __future__ import annotations

import re

_TOPICS = (
    (r"\b(?:argparse|command.line|cli|arguments?|flags?)\b", "command_line", "Considering the command-line interface"),
    (r"\b(?:parse|parser|parsing|csv|encoding|utf-?8|headers?)\b", "input", "Working through input handling"),
    (r"\b(?:validat\w*|constraints?|schema|rules?|required)\b", "validation", "Working through validation rules"),
    (r"\b(?:errors?|exceptions?|edge.cases?|malformed|invalid)\b", "edge_cases", "Considering errors and edge cases"),
    (r"\b(?:unittest|assert\w*|test.cases?|self.test|examples?)\b", "checks", "Planning checks and examples"),
    (r"\b(?:class|functions?|methods?|implementation|modules?|structure)\b", "implementation", "Organizing the implementation"),
    (r"\b(?:sources?|citations?|evidence|references?)\b", "evidence", "Considering the available evidence"),
    (r"\b(?:calculate|calculation|equation|probability|formula)\b", "calculation", "Working through the calculation"),
)

def reasoning_progress_summary(text: str) -> tuple[str, str]:
    """Name the latest broad topic mentioned, without claiming a tool ran.

    These are indicative topic labels, not a transcript or a claim that a task
    is done. All displayed wording comes from this fixed vocabulary.
    """
    candidates = [(match.end(), key, label)
                  for pattern, key, label in _TOPICS
                  for match in re.finditer(pattern, str(text)[-1200:], re.I)]
    if not candidates:
        return "reasoning", "Working through the request"
    _, key, label = max(candidates)
    return key, label
