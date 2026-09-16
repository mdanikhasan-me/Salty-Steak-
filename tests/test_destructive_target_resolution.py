from types import SimpleNamespace
import json

import pytest

from app.backend.chat.goal_state import GoalSpec, Predicate
from app.backend.chat.service import ChatService, _turn_completion


@pytest.mark.parametrize('action', ['action','plan'])
@pytest.mark.parametrize('subject', ['$temp_file_path','${target}','relative.tmp',r'C:\Temp\%TARGET%'])
def test_unresolved_removal_target_cannot_start_execution(action, subject):
    service=object.__new__(ChatService)
    service.web_search=None
    service.connectors=None
    service.granted_automation_capabilities=lambda: []
    service._goal_spec_for=lambda **kwargs:(GoalSpec(
        goal='Delete the specified temporary file',required=(Predicate('absent',subject),),
    ),{'status':'compiled'})
    def forbidden(**kwargs):
        raise AssertionError('Unresolved target reached execution setup')
    service._live_runners=forbidden
    # No host_environment exists: even reading task execution state would fail.
    result=service._dispatch_turn(
        reply_text=json.dumps({'action':action,'capability':'files.manage','arguments':{'operation':'delete','path':r'C:\Temp\.ses'}}),
        request='delete temp file',history=[{'role':'user','content':'delete temp file'}],
        conversation_id='test',message_id='test',
        generation_settings={'learned_route':'agent','agent_mode':True,'computer_authority_mode':'full_access'},
        context=SimpleNamespace(),
    )
    assert result.details['needs_target_clarification'] is True
    assert result.details['execution_performed'] is False
    assert result.details['unresolved_targets']==[subject]
    assert _turn_completion({},response=SimpleNamespace(cancelled=False),turn=result,proposal_pending=False)=='waiting'


def test_resolved_target_reaches_execution_setup(tmp_path):
    service=object.__new__(ChatService)
    service.web_search=None
    service.connectors=None
    service.granted_automation_capabilities=lambda: []
    path=str(tmp_path/'named.tmp')
    service._goal_spec_for=lambda **kwargs:(GoalSpec(goal='Delete named file',required=(Predicate('absent',path),)),{})
    class ReachedExecution(Exception):pass
    def observed():raise ReachedExecution()
    service.host_environment=SimpleNamespace(get=observed)
    with pytest.raises(ReachedExecution):
        service._dispatch_turn(
            reply_text=json.dumps({'action':'action','capability':'files.manage','arguments':{'operation':'delete','path':path}}),
            request='delete '+path,history=[{'role':'user','content':'delete '+path}],
            conversation_id='test',message_id='test',
            generation_settings={'learned_route':'agent','agent_mode':True,'computer_authority_mode':'full_access'},
            context=SimpleNamespace(),
        )


def test_failed_goal_compilation_attaches_empty_deletion_scope(tmp_path, monkeypatch):
    from app.backend.chat.task_runtime import TaskContext
    from app.backend.automation.invocation import invoke_capability, CapabilityCallFailed
    service=object.__new__(ChatService)
    service.web_search=None
    service.connectors=None
    service.granted_automation_capabilities=lambda: []
    service._goal_spec_for=lambda **kwargs:(None,{'status':'failed'})
    captured=[]
    def task_factory(**kwargs):
        task=TaskContext(**kwargs); captured.append(task); return task
    monkeypatch.setattr('app.backend.chat.service.TaskContext', task_factory)
    class ReachedExecution(Exception):pass
    def observed():raise ReachedExecution()
    service.host_environment=SimpleNamespace(get=observed)
    with pytest.raises(ReachedExecution):
        service._dispatch_turn(
            reply_text=json.dumps({'action':'action','capability':'files.manage','arguments':{'operation':'delete','path':str(tmp_path/'unrelated.tmp')}}),
            request='delete temp file',history=[{'role':'user','content':'delete temp file'}],
            conversation_id='test',message_id='test',
            generation_settings={'learned_route':'agent','agent_mode':True,'computer_authority_mode':'full_access'},
            context=SimpleNamespace(),
        )
    assert captured[0].deletion_targets == ()
    def forbidden(request): raise AssertionError('Unbound target reached broker')
    with pytest.raises(CapabilityCallFailed, match='No deletion target'):
        invoke_capability(SimpleNamespace(invoke=forbidden),'files.manage',{'operation':'delete','path':str(tmp_path/'unrelated.tmp')},authority_mode='full_access',task=captured[0])
