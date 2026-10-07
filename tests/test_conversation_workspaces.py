import json
import sqlite3
from pathlib import Path
import pytest
from app.backend.chat.service import ChatService
from app.backend.database.control import Database
from app.backend.database.conversation_workspaces import migrate_conversation_workspaces

def store(path):
    service=ChatService.__new__(ChatService)
    service.database=Database(path)
    return service

def test_workspaces_and_same_named_folders_survive_restart(tmp_path):
    path=tmp_path/'workspace.db'
    service=store(path)
    items={mode:service.create_conversation(mode) for mode in ('chat','agent','code')}
    folders={mode:service.create_label('Project',workspace_mode=mode) for mode in items}
    for mode in items: service.set_conversation_label(items[mode]['id'],folders[mode]['id'],True)
    reopened=store(path)
    for mode in items:
        assert [row['id'] for row in reopened.list_conversations(mode)]==[items[mode]['id']]
        assert reopened.list_labels(mode)[0]['conversation_count']==1
    with pytest.raises(ValueError): reopened.set_conversation_label(items['chat']['id'],folders['agent']['id'],True)

def test_mode_is_owned_by_conversation_not_changed_by_turn_flags(tmp_path):
    service=store(tmp_path/'workspace.db')
    chat=service.create_conversation('chat')
    agent=service.create_conversation('agent')
    code=service.create_conversation('code')
    assert service._settings_in_workspace(chat['id'],{'agent_mode':True,'code_mode':True})=={'agent_mode':False,'code_mode':False}
    assert service._settings_in_workspace(agent['id'],{})['agent_mode'] is True
    assert service._settings_in_workspace(code['id'],{})['code_mode'] is True
    with pytest.raises(ValueError,match='different workspace'): service._settings_in_workspace(chat['id'],{'workspace_mode':'agent'})
    with pytest.raises(ValueError):service.create_conversation('invalid')

def test_migration_preserves_mixed_sessions_messages_and_folder_links():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    c.executescript('''CREATE TABLE conversations(id TEXT PRIMARY KEY,title TEXT,updated_at TEXT);
    CREATE TABLE messages(conversation_id TEXT,role TEXT,sequence INTEGER,technical_details_json TEXT);
    CREATE TABLE conversation_labels(id TEXT PRIMARY KEY,name TEXT,tone TEXT,created_at TEXT,updated_at TEXT);
    CREATE UNIQUE INDEX ux_conversation_labels_name ON conversation_labels(lower(trim(name)));
    CREATE TABLE conversation_label_links(conversation_id TEXT,label_id TEXT,created_at TEXT,PRIMARY KEY(conversation_id,label_id));''')
    for mode in ['chat','agent','code']:
        c.execute('INSERT INTO conversations VALUES(?,?,?)',(mode,mode,'2026-09-06'))
        c.execute('INSERT INTO messages VALUES(?,?,?,?)',(mode,'user',0,json.dumps({'generation_settings':{'agent_mode':mode=='agent','code_mode':mode=='code'}})))
        c.execute('INSERT INTO conversation_label_links VALUES(?,?,?)',(mode,'folder','2026-09-06'))
    c.execute("INSERT INTO conversation_labels VALUES('folder','Project','neutral','2026-09-06','2026-09-06')")
    before=[tuple(row) for row in c.execute('SELECT * FROM messages')]
    migrate_conversation_workspaces(c);migrate_conversation_workspaces(c)
    assert [tuple(row) for row in c.execute('SELECT * FROM messages')]==before
    assert {row['id']:row['workspace_mode'] for row in c.execute('SELECT * FROM conversations')}=={'chat':'chat','agent':'agent','code':'code'}
    assert c.execute('SELECT count(*) FROM conversation_labels').fetchone()[0]==3
    assert c.execute('SELECT count(*) FROM conversation_label_links').fetchone()[0]==3
