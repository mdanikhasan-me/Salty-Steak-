"""Durable, bounded UI state owned by one conversation workspace."""

import json
from ..database.conversation_workspaces import checked_workspace_mode


def load_workspace_state(database, mode):
    checked_workspace_mode(mode)
    row = database.fetch_one("SELECT value FROM application_metadata WHERE key=?", (f"ui.workspace.{mode}",))
    if row is None:
        return {}
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return {}


def save_workspace_state(database, mode, payload):
    checked_workspace_mode(mode)
    if not isinstance(payload, dict):
        raise ValueError("Workspace state must be an object")
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > 512_000:
        raise ValueError("Workspace draft storage is full")
    owned = {str(row["id"]) for row in database.fetch_all("SELECT id FROM conversations WHERE workspace_mode=?", (mode,))}
    selected = payload.get("selectedId")
    if selected is not None and str(selected) not in owned:
        raise ValueError("Selected conversation belongs to a different workspace or was deleted")
    drafts = payload.get("drafts", [])
    instructions = payload.get("instructions", [])
    if not isinstance(drafts, list) or not isinstance(instructions, list):
        raise ValueError("Workspace drafts must be lists")
    value = {
        "selectedId": selected,
        "settings": payload.get("settings") if isinstance(payload.get("settings"), dict) else None,
        "drafts": [item for item in drafts[-32:] if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict) and (item[0] == "new" or item[0] in owned)],
        "instructions": [item for item in instructions[-64:] if isinstance(item, list) and len(item) == 2 and isinstance(item[1], str) and item[0] in owned],
    }
    database.execute("INSERT INTO application_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"ui.workspace.{mode}", json.dumps(value, ensure_ascii=False)))
    return {"saved": True}


def ui_preferences(database, patch=None):
    if patch is not None and not isinstance(patch, dict):
        raise ValueError("Preferences must be an object")
    with database.transaction() as connection:
        row = connection.execute("SELECT value FROM application_metadata WHERE key='ui.preferences'").fetchone()
        value = json.loads(row["value"]) if row else {}
        if patch is not None:
            if patch.get("theme") in ("dark", "warm"):
                value["theme"] = patch["theme"]
            for key in ("reducedMotion", "sidebarOpen"):
                if isinstance(patch.get(key), bool):
                    value[key] = patch[key]
            if patch.get("workspaceMode") in ("agent", "chat", "code"):
                value["workspaceMode"] = patch["workspaceMode"]
            connection.execute("INSERT INTO application_metadata(key,value) VALUES('ui.preferences',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))
    return value
