import pytest
from app.backend.database.control import Database
from app.backend.chat.service import ChatService
from app.backend.chat.workspace_state import load_workspace_state, save_workspace_state, ui_preferences

def test_workspace_drafts_and_preferences_survive_a_new_app_instance(tmp_path):
    path=tmp_path/'state.db';db=Database(path);service=ChatService.__new__(ChatService);service.database=db
    chat=service.create_conversation('chat');code=service.create_conversation('code')
    save_workspace_state(db,'code',{'selectedId':code['id'],'drafts':[[code['id'],{'draft':'Unsent code'}],[chat['id'],{'draft':'Wrong mode'}]],'instructions':[[code['id'],'Use Python']]})
    reopened=Database(path);saved=load_workspace_state(reopened,'code')
    assert saved['drafts']==[[code['id'],{'draft':'Unsent code'}]]
    assert saved['instructions']==[[code['id'],'Use Python']]
    assert load_workspace_state(reopened,'chat')=={}
    with pytest.raises(ValueError):save_workspace_state(db,'code',{'selectedId':chat['id']})
    ui_preferences(db,{'theme':'warm','sidebarOpen':False,'workspaceMode':'code'})
    ui_preferences(db,{'reducedMotion':True})
    assert ui_preferences(reopened)=={'theme':'warm','sidebarOpen':False,'workspaceMode':'code','reducedMotion':True}
