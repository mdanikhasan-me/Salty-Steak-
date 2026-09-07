"""Per-turn coding guidance; never changes a conversation's saved instructions."""

from collections.abc import Mapping, Sequence

CODE_INSTRUCTION = (
    "This turn is in Code mode. Work from the supplied code and requirements. "
    "Keep changes focused and preserve existing behavior unless asked to change it. "
    "When providing a complete source file, use a fenced block with a language and "
    "file=filename in its opening line so the workspace can show the file for review. "
    "Explain relevant checks and distinguish checks actually run from suggested checks. "
    "Do not claim you inspected a file, edited a project, or ran a test without tool evidence. "
    "Code mode does not grant computer, terminal, or filesystem permissions."
)


def code_workspace_history(
    history: Sequence[Mapping[str, str]], *, enabled: bool
) -> list[dict[str, str]]:
    result = [dict(message) for message in history]
    if enabled:
        result.insert(0, {"role": "system", "content": CODE_INSTRUCTION})
    return result
