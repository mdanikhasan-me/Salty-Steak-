import json
from pathlib import Path
import shutil
import pytest
from app.backend.application import Application
from app.backend.database.control import new_id,utc_now,json_text

@pytest.fixture
def app(tmp_path,monkeypatch):
    monkeypatch.delenv('SALTY_POTATO_WORKSPACE',raising=False)
    source=Path(__file__).resolve().parents[1]/'config'
    shutil.copytree(source,tmp_path/'config')
    (tmp_path/'config/local.toml').write_text('[training]\ndevice="cpu"\nprecision="fp32"\n[server]\nhost="127.0.0.1"\nport=0\n')
    instance=Application(tmp_path,recover_operations=False)
    yield instance
    instance.close()

def seed(app):
    conversation=app.create_conversation('chat')
    app.database.execute('INSERT INTO messages(id,conversation_id,role,content,sequence,created_at) VALUES(?,?,?,?,?,?)',
        (new_id(),conversation['id'],'user','Private conversation content',0,utc_now()))
    artifact=app.paths.conversations/'sample.txt';artifact.write_text('generated chat output')
    cache=app.paths.cache/'generated.tmp';cache.write_text('temporary')
    weights=app.paths.workspace/'models/example/model.gguf';weights.parent.mkdir(parents=True,exist_ok=True);weights.write_bytes(b'preserve weights')
    app.chat.save_memory('Remember the preferred blue theme')
    unrelated=app.paths.project_root/'unrelated.txt';unrelated.write_text('user file')
    return artifact,cache,weights,unrelated

def test_chat_clear_only_removes_chats_and_chat_artifacts(app):
    artifact,cache,weights,unrelated=seed(app)
    preview=app.data_management.preview('chats')
    assert preview['conversation_count']==1 and preview['file_count']==1
    result=app.data_management.execute(preview['token'],preview['confirmation'])
    assert result['status']=='completed',result
    assert not artifact.exists() and cache.exists() and weights.exists() and unrelated.exists()
    assert app.list_conversations()==[]
    assert app.chat.memory.statistics()['active']==1
    with pytest.raises(ValueError):app.data_management.execute(preview['token'],preview['confirmation'])

def test_clear_application_data_preserves_models_application_and_unrelated_files(app):
    artifact,cache,weights,unrelated=seed(app)
    hidden_weight=app.paths.cache/'preserve.safetensors';hidden_weight.write_bytes(b'weights')
    preview=app.data_management.preview('all')
    result=app.data_management.execute(preview['token'],preview['confirmation'])
    assert result['status']=='completed',result
    assert not artifact.exists() and not cache.exists()
    assert weights.read_bytes()==b'preserve weights' and hidden_weight.read_bytes()==b'weights'
    assert unrelated.exists() and (app.paths.project_root/'config/defaults.toml').exists()
    assert app.chat.memory.statistics()['active']==0
    assert app.chat.mission_memory.statistics()['mission_events']==0
    assert app.database.fetch_one('PRAGMA integrity_check')['integrity_check']=='ok'

def test_confirmation_and_stale_plan_cannot_delete_new_or_changed_data(app):
    artifact,*_=seed(app)
    preview=app.data_management.preview('chats')
    with pytest.raises(ValueError,match='Confirmation'):app.data_management.execute(preview['token'],'wrong')
    artifact.write_text('changed after preview')
    with pytest.raises(RuntimeError,match='changed'):app.data_management.execute(preview['token'],preview['confirmation'])
    assert artifact.read_text()=='changed after preview' and app.list_conversations()

def test_active_operation_blocks_preview(app):
    app.operations.create('test_work')
    with pytest.raises(RuntimeError,match='active tasks'):app.data_management.preview('all')

def test_audited_external_created_file_removed_but_modified_file_preserved(app,tmp_path):
    seed(app)
    app.automation.grant({'capabilities':['files.manage'],'user_confirmed':True})
    generated=tmp_path/'created-by-app.txt'
    modified=tmp_path/'modified-after-generation.txt'
    for path in (generated,modified):
        app.automation.invoke({'capability':'files.manage','arguments':{'operation':'write','path':str(path),'content':'original output'},'user_confirmed':True})
    modified.write_text('user modified it')
    preview=app.data_management.preview('all')
    result=app.data_management.execute(preview['token'],preview['confirmation'])
    assert result['status']=='completed',result
    assert not generated.exists() and modified.read_text()=='user modified it'

def test_linked_directory_is_not_followed(app,tmp_path):
    outside=tmp_path/'external';outside.mkdir();(outside/'private.txt').write_text('preserve')
    link=app.paths.conversations/'link'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('Symlink unavailable')
    preview=app.data_management.preview('all')
    result=app.data_management.execute(preview['token'],preview['confirmation'])
    assert (outside/'private.txt').read_text()=='preserve'

def test_staging_failure_restores_files_and_conversations(app,monkeypatch):
    artifact,*_=seed(app)
    second=app.paths.conversations/'second.txt';second.write_text('second')
    preview=app.data_management.preview('chats')
    original=Path.replace
    def fail_one(self,target):
        if self==second:raise PermissionError('locked fixture')
        return original(self,target)
    monkeypatch.setattr(Path,'replace',fail_one)
    with pytest.raises(PermissionError,match='locked'):app.data_management.execute(preview['token'],preview['confirmation'])
    assert artifact.exists() and second.exists() and len(app.list_conversations())==1

def test_audit_append_only_boundary_is_restored_after_clear(app):
    seed(app)
    preview=app.data_management.preview('all')
    assert app.data_management.execute(preview['token'],preview['confirmation'])['status']=='completed'
    trigger=app.database.fetch_one("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_automation_audit_append_only_delete'")
    assert trigger and 'RAISE' in trigger['sql']

def test_new_message_invalidates_reviewed_chat_scope(app):
    seed(app)
    preview=app.data_management.preview('chats')
    cid=app.list_conversations()[0]['id']
    app.database.execute('INSERT INTO messages(id,conversation_id,role,content,sequence,created_at) VALUES(?,?,?,?,?,?)',
        (new_id(),cid,'user','new message',1,utc_now()))
    with pytest.raises(RuntimeError,match='changed'):app.data_management.execute(preview['token'],preview['confirmation'])


def test_chat_owned_external_output_is_deleted_without_touching_other_outputs(app, tmp_path):
    seed(app)
    app.automation.grant({'capabilities':['files.manage'],'user_confirmed':True})
    owned = tmp_path / 'conversation-output.txt'
    unrelated = tmp_path / 'other-operation.txt'
    results = [app.automation.invoke({'capability':'files.manage', 'arguments':{
        'operation':'write','path':str(path),'content':'generated'},'user_confirmed':True})
        for path in (owned, unrelated)]
    app.database.execute('UPDATE messages SET technical_details_json=?', (json_text(results[0]),))
    preview = app.data_management.preview('chats')
    assert app.data_management.execute(preview['token'], preview['confirmation'])['status'] == 'completed'
    assert not owned.exists() and unrelated.exists()


def test_chat_clear_removes_saved_drafts_but_preserves_theme(app):
    from app.backend.chat.workspace_state import save_workspace_state, load_workspace_state, ui_preferences
    seed(app)
    save_workspace_state(app.database, 'chat', {'drafts':[['new', {'content':'private unsent draft'}]]})
    ui_preferences(app.database, {'theme':'warm'})
    preview = app.data_management.preview('chats')
    app.data_management.execute(preview['token'], preview['confirmation'])
    assert load_workspace_state(app.database, 'chat') == {'epoch':1}
    assert ui_preferences(app.database)['theme'] == 'warm'


def test_old_tab_cannot_restore_deleted_drafts_after_cleanup(app):
    from app.backend.chat.workspace_state import save_workspace_state, load_workspace_state
    preview = app.data_management.preview('chats')
    app.data_management.execute(preview['token'], preview['confirmation'])
    stale = {'drafts':[['new', {'draft':'should never return'}]]}
    with pytest.raises(RuntimeError, match='reload'):
        save_workspace_state(app.database, 'chat', stale)
    fresh = load_workspace_state(app.database, 'chat')
    save_workspace_state(app.database, 'chat', {**stale, **fresh})
    assert load_workspace_state(app.database, 'chat')['drafts'] == stale['drafts']


def test_new_global_memory_invalidates_all_data_preview(app):
    seed(app)
    preview = app.data_management.preview('all')
    app.chat.save_memory('A new memory after the preview')
    with pytest.raises(RuntimeError, match='changed'):
        app.data_management.execute(preview['token'], preview['confirmation'])
    assert app.chat.memory.statistics()['active'] == 2


def test_busy_generation_rejects_cleanup_without_waiting(app):
    import threading
    seed(app)
    preview = app.data_management.preview('chats')
    held, release = threading.Event(), threading.Event()
    def hold():
        with app.chat._generation_lock:
            held.set()
            assert release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(2)
    try:
        with pytest.raises(RuntimeError, match='busy'):
            app.data_management.execute(preview['token'], preview['confirmation'])
    finally:
        release.set()
        worker.join(2)
    assert app.list_conversations()


def test_registered_model_in_cache_keeps_its_config_and_weights(app):
    seed(app)
    folder = app.paths.cache / 'custom-model'
    folder.mkdir()
    checkpoint = folder / 'weights.custom'
    checkpoint.write_bytes(b'model weights')
    metadata = folder / 'model.json'
    metadata.write_text('{"architecture":"fixture"}')
    app.database.execute('''INSERT INTO saved_versions
        (id,label,checkpoint_path,checksum,size_bytes,architecture_revision,
         context_tokens,tokenizer_checksum,integrity,created_at,verified_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (new_id(),'fixture',str(checkpoint),'0'*64,13,1,2048,'1'*64,'verified',utc_now(),utc_now()))
    preview = app.data_management.preview('all')
    result = app.data_management.execute(preview['token'], preview['confirmation'])
    assert result['status'] == 'completed', result
    assert checkpoint.read_bytes() == b'model weights' and metadata.exists()
    assert app.database.fetch_one('SELECT COUNT(*) AS n FROM saved_versions')['n'] == 1


@pytest.mark.parametrize('scope', ['chats', 'all'])
def test_live_http_preview_confirm_clear_and_restart(app, scope):
    """Exercise the real router, database and filesystem in a disposable app."""
    import urllib.request
    import urllib.error
    from app.backend.server import start_server
    artifact, cache, weights, unrelated = seed(app)
    def post(base, path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    with start_server(application=app, project_root=app.paths.project_root,
                      port=0, owns_application=False) as server:
        status, payload = post(server.url, '/api/data/preview', {'scope':scope})
        assert status == 200, payload
        preview = payload['data']
        status, _ = post(server.url, '/api/data/clear', {'token':preview['token'], 'confirmation':'wrong'})
        assert status == 400 and artifact.exists()
        status, payload = post(server.url, '/api/data/clear', {
            'token':preview['token'], 'confirmation':preview['confirmation']})
        assert status == 200 and payload['data']['status'] == 'completed', payload
        assert not artifact.exists() and weights.exists() and unrelated.exists()
        assert cache.exists() == (scope == 'chats')
        status, _ = post(server.url, '/api/data/clear', {
            'token':preview['token'], 'confirmation':preview['confirmation']})
        assert status == 400
    fresh = Application(app.paths.project_root, recover_operations=False)
    try:
        assert fresh.list_conversations() == []
        assert fresh.chat.memory.statistics()['active'] == (1 if scope == 'chats' else 0)
    finally:
        fresh.close()


@pytest.mark.parametrize('phase', ['before_move','after_move','before_commit','after_commit','during_delete'])
@pytest.mark.parametrize('scope', ['chats','all'])
def test_process_crash_recovers_without_stranding_files_or_restoring_deleted_chats(app, phase, scope):
    import os
    import subprocess
    import sys
    artifact, cache, weights, unrelated = seed(app)
    environment = {**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    environment.pop('SALTY_POTATO_WORKSPACE', None)
    crashed = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                              str(app.paths.project_root), phase, scope],
                             env=environment, capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 71, (crashed.stdout, crashed.stderr)
    assert app.data_management.recovery_pending()
    recovered = Application(app.paths.project_root, recover_operations=False)
    try:
        committed = phase in {'after_commit','during_delete'}
        assert artifact.exists() != committed
        assert cache.exists() == (scope == 'chats' or not committed)
        assert bool(recovered.list_conversations()) != committed
        assert recovered.chat.memory.statistics()['active'] == (0 if committed and scope == 'all' else 1)
        assert weights.read_bytes() == b'preserve weights' and unrelated.exists()
        assert not recovered.data_management.recovery_pending()
        assert not list(app.paths.workspace.rglob('.salty-cleanup/*/*'))
        assert recovered.database.fetch_one('PRAGMA integrity_check')['integrity_check'] == 'ok'
        assert recovered.data_management.recover()['files_removed'] == 0
    finally:
        recovered.close()


def test_recovery_never_overwrites_a_new_original_file(app):
    artifact, *_ = seed(app)
    plan = app.data_management._plan('chats')
    token = 'x' * 43
    journal = app.data_management._prepare_journal(plan, token)
    staged = Path(journal['files'][0]['staged'])
    staged.parent.mkdir(parents=True)
    artifact.replace(staged)
    artifact.write_text('new user content')
    with pytest.raises(RuntimeError, match='overwrite'):
        app.data_management.recover()
    assert artifact.read_text() == 'new user content'
    assert staged.read_text() == 'generated chat output'
    assert app.data_management.recovery_pending()


def test_changed_staged_file_is_preserved_for_manual_recovery(app):
    artifact, *_ = seed(app)
    journal = app.data_management._prepare_journal(app.data_management._plan('chats'), 'x'*43)
    staged = Path(journal['files'][0]['staged'])
    staged.parent.mkdir(parents=True)
    artifact.replace(staged)
    staged.write_text('changed')
    with pytest.raises(RuntimeError, match='Staged cleanup data changed'):
        app.data_management.recover()
    assert staged.exists() and app.data_management.recovery_pending()


def test_partial_file_deletion_retains_journal_and_recovers_after_restart(app, monkeypatch):
    artifact, cache, weights, _ = seed(app)
    original = Path.unlink
    def locked(path, *args, **kwargs):
        if '.salty-cleanup' in path.parts:
            raise PermissionError('simulated transient lock')
        return original(path, *args, **kwargs)
    preview = app.data_management.preview('all')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', locked)
        result = app.data_management.execute(preview['token'], preview['confirmation'])
    assert result['status'] == 'partial' and result['restart_recommended']
    assert app.data_management.recovery_pending()
    with pytest.raises(RuntimeError, match='restart'):
        app.data_management.preview('all')
    app.data_management.recover()
    assert not app.data_management.recovery_pending()
    assert not artifact.exists() and not cache.exists() and weights.exists()


def test_pending_recovery_blocks_http_mutations(app):
    import urllib.request
    import urllib.error
    from app.backend.server import start_server
    seed(app)
    app.data_management._prepare_journal(app.data_management._plan('chats'), 'x'*43)
    with start_server(application=app, project_root=app.paths.project_root,
                      port=0, owns_application=False) as server:
        request = urllib.request.Request(server.url+'/api/chat/conversations',
            data=b'{"workspace_mode":"chat"}', headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 409
        assert 'recovery' in json.load(error.value)['error']['message']
    app.data_management.recover()
    assert len(app.list_conversations()) == 1


def _crash_child(root, phase, scope):
    """Real abrupt exit, not an exception that the cleanup code can roll back."""
    import os
    instance = Application(root, recover_operations=False)
    management = instance.data_management
    original_replace, original_unlink = Path.replace, Path.unlink
    original_save, original_recover = management._save_journal, management.recover
    def replace(path, target):
        if '.salty-cleanup' in Path(target).parts:
            if phase == 'before_move': os._exit(71)
            result = original_replace(path, target)
            if phase == 'after_move': os._exit(71)
            return result
        return original_replace(path, target)
    def save(connection, journal):
        original_save(connection, journal)
        if phase == 'before_commit' and journal['state'] == 'committed': os._exit(71)
    def recover():
        if phase == 'after_commit': os._exit(71)
        return original_recover()
    def unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if phase == 'during_delete' and '.salty-cleanup' in path.parts: os._exit(71)
        return result
    Path.replace, Path.unlink = replace, unlink
    management._save_journal, management.recover = save, recover
    preview = management.preview(scope)
    management.execute(preview['token'], preview['confirmation'])
    raise AssertionError('Crash injection was not reached')


if __name__ == '__main__':
    import sys
    _crash_child(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
