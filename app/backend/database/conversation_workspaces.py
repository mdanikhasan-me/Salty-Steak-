"""One-time assignment of conversations and folders to durable workspaces."""

import json
import uuid


def migrate_conversation_workspaces(connection):
    connection.execute("SAVEPOINT conversation_workspaces")
    try:
        _migrate(connection)
    except BaseException:
        connection.execute("ROLLBACK TO conversation_workspaces")
        raise
    finally:
        connection.execute("RELEASE conversation_workspaces")


def _migrate(connection):
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversations)")}
    if "workspace_mode" in columns:
        return
    connection.execute("ALTER TABLE conversations ADD COLUMN workspace_mode TEXT NOT NULL DEFAULT 'chat' CHECK(workspace_mode IN ('agent','chat','code'))")
    connection.execute("ALTER TABLE conversations ADD COLUMN workspace_locked INTEGER NOT NULL DEFAULT 0 CHECK(workspace_locked IN (0,1))")
    # Keep mixed legacy sessions whole. Their first explicitly recorded mode owns them.
    assigned = set()
    for row in connection.execute("SELECT conversation_id, technical_details_json FROM messages WHERE role='user' ORDER BY conversation_id, sequence").fetchall():
        identifier = row["conversation_id"]
        if identifier in assigned:
            continue
        try:
            details = json.loads(row["technical_details_json"] or "{}")
            settings = details.get("generation_settings") or {}
        except (ValueError, AttributeError):
            continue
        if not isinstance(settings, dict) or not any(key in settings for key in ("agent_mode", "code_mode")):
            continue
        mode = "code" if settings.get("code_mode") is True else "agent" if settings.get("agent_mode") is True else "chat"
        connection.execute("UPDATE conversations SET workspace_mode=? WHERE id=?", (mode, identifier))
        assigned.add(identifier)
    connection.execute("ALTER TABLE conversation_labels ADD COLUMN workspace_mode TEXT NOT NULL DEFAULT 'chat' CHECK(workspace_mode IN ('agent','chat','code'))")
    connection.execute("DROP INDEX IF EXISTS ux_conversation_labels_name")
    connection.execute("CREATE UNIQUE INDEX ux_conversation_labels_name ON conversation_labels(workspace_mode,lower(trim(name)))")
    for label in connection.execute("SELECT * FROM conversation_labels").fetchall():
        for mode in ("agent", "code"):
            links = connection.execute("SELECT l.conversation_id,l.created_at FROM conversation_label_links l JOIN conversations c ON c.id=l.conversation_id WHERE l.label_id=? AND c.workspace_mode=?", (label["id"], mode)).fetchall()
            if not links:
                continue
            identifier = str(uuid.uuid4())
            connection.execute("INSERT INTO conversation_labels(id,name,tone,created_at,updated_at,workspace_mode) VALUES(?,?,?,?,?,?)", (identifier,label["name"],label["tone"],label["created_at"],label["updated_at"],mode))
            for link in links:
                connection.execute("UPDATE conversation_label_links SET label_id=? WHERE conversation_id=? AND label_id=?", (identifier,link["conversation_id"],label["id"]))
    connection.execute("UPDATE conversations SET workspace_locked=1")
    connection.execute("CREATE INDEX ix_conversations_workspace_updated ON conversations(workspace_mode,updated_at DESC)")


def checked_workspace_mode(value):
    if value not in ("agent", "chat", "code"):
        raise ValueError("workspace_mode must be agent, chat, or code")
    return value
