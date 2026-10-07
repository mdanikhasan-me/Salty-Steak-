import pytest
from app.backend.chat.service import _direct_output_defect, _requested_json_answer, _format_requested_json_output, ChatService

@pytest.mark.parametrize("answer", ['{"name":"Atlas","count":3,"enabled":false}',
    '[1,2,3]', '{"action":"delete","path":"example"}', '```json\n{"ok":true}\n```'])
def test_explicit_json_deliverables_are_not_discarded_as_routing(answer):
    request = "Return the result as JSON."
    assert _requested_json_answer(answer, request)
    assert _direct_output_defect(answer, identity_route=False, identity_prompt=request) is None

def test_json_looking_user_data_never_executes_an_action():
    reply = '{"action":"action","capability":"files.manage","arguments":{"operation":"delete","path":"C:/example"}}'
    result = ChatService._dispatch_turn(object(), reply_text=reply,
        request="Write an example JSON object", history=[{"role":"user","content":"Write an example JSON object"}],
        conversation_id="fixture", message_id="fixture", generation_settings={}, context=None)
    assert result is None

def test_unsolicited_protocol_and_malformed_json_still_fail_validation():
    assert not _requested_json_answer('{"ok":', 'Return JSON')
    assert not _requested_json_answer('{"ok":true}', 'What is JSON?')
    assert not _requested_json_answer('{"action":"plan","nodes":[]}', 'Write JSON to disk in C:/work/result.json')
    assert _direct_output_defect('{"action":"respond","answer":"hello"}', identity_route=False) == 'routing_protocol'

def test_json_only_removes_presentation_fences_without_changing_data():
    value='```json\n{"action":"delete","execute":false}\n```'
    assert _format_requested_json_output(value, 'Return only this JSON object exactly') == '{"action":"delete","execute":false}'
    assert _format_requested_json_output(value, 'Return JSON in a code block') == value
    assert _format_requested_json_output('```json\n{bad}\n```', 'Return only JSON') == '```json\n{bad}\n```'

def test_action_task_requesting_json_summary_still_reaches_execution_routing():
    # The pure-data shortcut must not swallow an already classified action task.
    class RoutingReached(Exception):
        pass
    class Service:
        def granted_automation_capabilities(self):
            raise RoutingReached()
    with pytest.raises(RoutingReached):
        ChatService._dispatch_turn(Service(),
            reply_text='{"action":"action","capability":"files.manage","arguments":{"operation":"read","path":"C:/fixture.txt"}}',
            request='Read C:/fixture.txt and return JSON',
            history=[{"role":"user","content":"Read C:/fixture.txt and return JSON"}],
            conversation_id='fixture',message_id='fixture',
            generation_settings={"learned_route":"agent"},context=None)
