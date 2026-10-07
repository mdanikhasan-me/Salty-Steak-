from app.backend.chat.code_workspace import CODE_INSTRUCTION, code_workspace_history


def test_code_mode_preserves_the_callers_history_and_conversation_instruction():
    history = [{"role": "system", "content": "Use British spelling."}, {"role": "user", "content": "Write a parser."}]
    result = code_workspace_history(history, enabled=True)
    assert result[0] == {"role": "system", "content": CODE_INSTRUCTION}
    assert result[1:] == history
    result[1]["content"] = "Changed"
    assert history[0]["content"] == "Use British spelling."


def test_chat_mode_does_not_keep_a_previous_coding_instruction():
    history = [{"role": "user", "content": "Hello"}]
    code_workspace_history(history, enabled=True)
    assert code_workspace_history(history, enabled=False) == history
