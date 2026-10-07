import hashlib
from types import SimpleNamespace
import pytest
from app.backend.application import Application
from app.backend.chat.agent_loop import summarise_observation
from tests.test_automation_broker import _broker, _grant, _invoke

def preview_app(tmp_path, *, capability='screen.capture', enabled=True):
    root=tmp_path/'automation'
    folder=root/('screenshots' if capability=='screen.capture' else 'browser-previews')
    folder.mkdir(parents=True)
    path=folder/'capture.png';path.write_bytes(b'recorded image')
    record={'event':'invoke','outcome':'succeeded','capability':capability,
            'result':{'command':'capture_preview','artifact':{'path':str(path),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}}
    broker=SimpleNamespace(artifact_root=root,audit_record=lambda key:record if key=='capture-id' else None,
        status=lambda:{'capabilities':[{'capability':capability,'effective_enabled':enabled}]})
    return SimpleNamespace(automation=broker),record,path

@pytest.mark.parametrize('capability',['screen.capture','browser.control'])
def test_only_recorded_unchanged_capture_is_served(tmp_path,capability):
    app,record,path=preview_app(tmp_path,capability=capability)
    assert Application.action_preview_content(app,'capture-id').path==path
    with pytest.raises(PermissionError):Application.action_preview_content(app,'invented')
    path.write_bytes(b'changed')
    with pytest.raises(PermissionError,match='changed'):Application.action_preview_content(app,'capture-id')

def test_preview_respects_revocation_and_path_containment(tmp_path):
    app,record,path=preview_app(tmp_path,enabled=False)
    with pytest.raises(PermissionError,match='not enabled'):Application.action_preview_content(app,'capture-id')
    app.automation.status=lambda:{'capabilities':[{'capability':'screen.capture','effective_enabled':True}]}
    record['result']['artifact']['path']=str(tmp_path/'private.png')
    with pytest.raises(PermissionError,match='outside'):Application.action_preview_content(app,'capture-id')

def test_failed_capture_is_not_an_image_and_missing_is_explicit(tmp_path):
    app,record,path=preview_app(tmp_path)
    record['outcome']='failed'
    with pytest.raises(PermissionError):Application.action_preview_content(app,'capture-id')
    record['outcome']='succeeded';path.unlink()
    with pytest.raises(FileNotFoundError):Application.action_preview_content(app,'capture-id')

def test_file_diff_is_derived_from_verified_write_and_survives_observation(tmp_path):
    broker,db,project,_=_broker(tmp_path)
    _grant(broker,'files.manage')
    path=project/'example.js'
    def write(content,**kwargs):
        return _invoke(broker,{'capability':'files.manage','arguments':{
            'operation':'write','path':str(path),'content':content,**kwargs}})
    try:
        created=write('const answer = 1;\n')
        result=write('const answer = 2;\nconsole.log(answer);\n',overwrite=True,expected_sha256=created['sha256'])
        observation=summarise_observation('files.manage',result)
        assert observation['change']['added']==2 and observation['change']['removed']==1
        assert '-const answer = 1;' in observation['change']['diff']
        assert '+const answer = 2;' in observation['change']['diff']
        assert broker.audit_record(result['audit_record_id'])['result']['readback_verified']
    finally:broker.close()

def test_analysis_preview_is_bound_to_claimed_conversation(tmp_path):
    from tests.test_vision_inputs import _stage
    from app.backend.chat.vision_inputs import VisionInputStore
    store=VisionInputStore(tmp_path/'inputs')
    _,staged=_stage(store,tmp_path)
    with pytest.raises(PermissionError):store.preview(staged['input_id'],'chat-1')
    claim=store.claim(staged['vision_input_token'],operation_id='op-1',conversation_id='chat-1',
                      target_model_id='model',prompt='Describe image')
    assert store.preview(staged['input_id'],'chat-1')[0]==claim.image_path
    with pytest.raises(PermissionError):store.preview(staged['input_id'],'other-chat')
    with pytest.raises(PermissionError):store.preview('../outside','chat-1')
    claim.image_path.write_bytes(b'changed')
    with pytest.raises(RuntimeError):store.preview(staged['input_id'],'chat-1')
