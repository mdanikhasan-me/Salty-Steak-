import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight,
  Brain,
  Layers,
  SlidersHorizontal,
  Plug,
  Bell,
  Folder,
  FolderOpen,
  FolderInput,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";

import {
  conversationTitle,
  folderOf,
  moveChoicesFor,
  openFolder,
  organiseConversations,
} from "../workflows/conversationOrganisation.mjs";












const SECTION_STORAGE_KEY = "salty-steak:sidebar-sections";

function storedSections(mode) {
  try {
    return JSON.parse(window.localStorage.getItem(`${SECTION_STORAGE_KEY}:${mode}`) || "{}");
  } catch {
    return {};
  }
}

export function ConversationSidebar({
  open,
  conversations,
  folders,
  selectedId,
  creating,
  onNewChat,
  onSelect,
  onRename,
  onDelete,
  onTogglePin,
  onMoveToFolder,
  onCreateFolder,
  onRenameFolder,
  onDeleteFolder,
  onAfterSelect,
  onSettings,
  onTraining,
  onNotifications,
  mode = "chat",
}) {
  const [query, setQuery] = useState("");
  const [browsing, setBrowsing] = useState(null);
  const [menuFor, setMenuFor] = useState(null);
  const [submenuFor, setSubmenuFor] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [newFolderFor, setNewFolderFor] = useState(null);
  const [newFolderName, setNewFolderName] = useState("");
  const [renamingFolderId, setRenamingFolderId] = useState(null);
  const [folderDraft, setFolderDraft] = useState("");
  const [collapsed, setCollapsed] = useState(() => storedSections(mode));
  const triggerRefs = useRef(new Map());
  const renameRef = useRef(null);

  const [today, setToday] = useState(() => new Date());
  useEffect(() => {
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    const timer = window.setTimeout(() => setToday(new Date()), midnight-now+100);
    return () => clearTimeout(timer);
  }, [today]);
  const organised = useMemo(
    () =>
      organiseConversations({
        conversations,
        folders,
        query,
        openFolderId: browsing,
        now: today,
      }),
    [conversations, folders, query, browsing, today],
  );
  const current = openFolder(organised);

  useEffect(() => {
    try {
      window.localStorage.setItem(`${SECTION_STORAGE_KEY}:${mode}`, JSON.stringify(collapsed));
    } catch {

    }
  }, [collapsed]);


  useEffect(() => {
    if (browsing && !organised.folders.some((item) => item.id === browsing)) {
      setBrowsing(null);
    }
  }, [browsing, organised.folders]);

  useEffect(() => {
    if (renamingId) renameRef.current?.select();
  }, [renamingId]);

  useEffect(() => {
    if (!menuFor) return undefined;
    function dismiss(event) {
      if (event.target.closest?.(".chat-menu")) return;
      if (event.target.closest?.(".chat-row__more")) return;
      closeMenu();
    }
    function onKey(event) {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      closeMenu();
    }
    document.addEventListener("mousedown", dismiss);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", dismiss);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [menuFor]);

  function closeMenu() {
    const previous = menuFor;
    setMenuFor(null);
    setSubmenuFor(null);
    setNewFolderFor(null);
    setNewFolderName("");
    if (previous) {
      window.requestAnimationFrame(() =>
        triggerRefs.current.get(previous)?.focus(),
      );
    }
  }

  function toggleSection(name) {
    setCollapsed((previous) => ({ ...previous, [name]: !previous[name] }));
  }

  function beginRename(item) {
    closeMenu();
    setRenamingId(item.id);
    setRenameDraft(conversationTitle(item));
  }

  async function commitRename(item) {
    const title = renameDraft.trim();
    setRenamingId(null);
    if (!title || title === conversationTitle(item)) return;
    await onRename?.(item, title);
  }

  async function createFolderAndMove(item) {
    const name = newFolderName.trim();
    if (!name) return;
    setNewFolderName("");
    setNewFolderFor(null);
    closeMenu();
    await onCreateFolder?.(name, item);
  }

  function renderRow(item) {
    const title = conversationTitle(item);
    const pinned = Boolean(item.pinned ?? item.pinned_at);
    const menuOpen = String(menuFor) === String(item.id);
    const className = [
      "chat-row",
      String(item.id) === String(selectedId) ? "chat-row--active" : "",
      menuOpen ? "chat-row--menu" : "",
    ]
      .filter(Boolean)
      .join(" ");

    if (String(renamingId) === String(item.id)) {
      return (
        <div className={className} key={item.id}>
          <form
            className="chat-row__rename"
            onSubmit={(event) => {
              event.preventDefault();
              void commitRename(item);
            }}
          >
            <input
              ref={renameRef}
              autoFocus
              value={renameDraft}
              maxLength={80}
              aria-label={`Rename ${title}`}
              onChange={(event) => setRenameDraft(event.target.value)}
              onBlur={() => void commitRename(item)}
              onKeyDown={(event) => {
                if (event.key !== "Escape") return;
                event.preventDefault();
                event.stopPropagation();
                setRenamingId(null);
              }}
            />
          </form>
        </div>
      );
    }

    return (
      <div className={className} key={item.id}>
        <button
          type="button"
          className="chat-row__open"
          title={title}
          aria-current={String(item.id) === String(selectedId) ? "page" : undefined}
          onClick={() => {
            onSelect?.(item.id);
            onAfterSelect?.();
          }}
        >
          <span className="chat-row__title">{title}</span>
        </button>
        <span className="chat-row__actions">
          <button
            type="button"
            className="chat-row__pin"
            aria-label={pinned ? `Unpin ${title}` : `Pin ${title}`}
            aria-pressed={pinned}
            onClick={() => onTogglePin?.(item, !pinned)}
          >
            {pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
          </button>
          <button
            type="button"
            className="chat-row__more"
            aria-label={`Actions for ${title}`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            ref={(element) => {
              if (element) triggerRefs.current.set(item.id, element);
              else triggerRefs.current.delete(item.id);
            }}
            onClick={() => {
              setSubmenuFor(null);
              setMenuFor(menuOpen ? null : item.id);
            }}
          >
            <MoreHorizontal aria-hidden="true" />
          </button>
        </span>
        {menuOpen ? renderMenu(item, title) : null}
      </div>
    );
  }

  function renderMenu(item, title) {
    const pinned = Boolean(item.pinned ?? item.pinned_at);
    const home = folderOf(item);
    const choices = moveChoicesFor(item, folders);
    const submenuOpen = String(submenuFor) === String(item.id);
    return (
      <div className="chat-menu" role="menu" aria-label={`Actions for ${title}`}>
        <button type="button" role="menuitem" onClick={() => beginRename(item)}>
          <Pencil aria-hidden="true" /> Rename
        </button>
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            closeMenu();
            onTogglePin?.(item, !pinned);
          }}
        >
          {pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}
          {pinned ? "Unpin" : "Pin"}
        </button>
        <button
          type="button"
          role="menuitem"
          aria-haspopup="menu"
          aria-expanded={submenuOpen}
          onClick={() => setSubmenuFor(submenuOpen ? null : item.id)}
        >
          <FolderInput aria-hidden="true" /> Move to
          <ChevronRight aria-hidden="true" className="chat-menu__chevron" />
        </button>
        {submenuOpen ? (
          <div className="chat-menu__submenu" role="menu" aria-label="Folders">
            {choices.map((choice) => (
              <button
                key={choice.id}
                type="button"
                role="menuitemradio"
                aria-checked={choice.current}
                disabled={choice.current}
                onClick={() => {
                  closeMenu();
                  onMoveToFolder?.(item, choice.id);
                }}
              >
                <Folder aria-hidden="true" />
                <span className="chat-menu__folder-name">{choice.name}</span>
              </button>
            ))}
            {home ? (
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  closeMenu();
                  onMoveToFolder?.(item, null);
                }}
              >
                <X aria-hidden="true" /> Take out of {home.name}
              </button>
            ) : null}
            {newFolderFor === item.id ? (
              <form
                className="chat-menu__new-folder"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createFolderAndMove(item);
                }}
              >
                <input
                  autoFocus
                  value={newFolderName}
                  maxLength={60}
                  placeholder="Folder name"
                  aria-label="New folder name"
                  onChange={(event) => setNewFolderName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return;
                    event.preventDefault();
                    event.stopPropagation();
                    setNewFolderFor(null);
                  }}
                />
              </form>
            ) : (
              <button
                type="button"
                role="menuitem"
                className="chat-menu__create"
                onClick={() => setNewFolderFor(item.id)}
              >
                <Plus aria-hidden="true" /> New folder
              </button>
            )}
          </div>
        ) : null}
        <button
          type="button"
          role="menuitem"
          className="chat-menu__delete"
          onClick={() => {
            closeMenu();
            onDelete?.(item);
          }}
        >
          <Trash2 aria-hidden="true" /> Delete
        </button>
      </div>
    );
  }

  function renderSection(name, heading, children, count) {
    const isCollapsed = Boolean(collapsed[name]);
    return (
      <section className="chat-section" key={name}>
        <button
          type="button"
          className="chat-section__heading"
          aria-expanded={!isCollapsed}
          onClick={() => toggleSection(name)}
        >
          <ChevronRight
            aria-hidden="true"
            className="chat-section__chevron"
            data-open={!isCollapsed || undefined}
          />
          <span>{heading}</span>
          {count === undefined ? null : <small>{count}</small>}
        </button>
        {isCollapsed ? null : children}
      </section>
    );
  }

  return (
    <aside
      id="workspace-sidebar"
      className={`workspace-sidebar chat-sidebar ${
        open ? "workspace-sidebar--open" : "workspace-sidebar--closed"
      }`}
      aria-label={`${mode[0].toUpperCase() + mode.slice(1)} conversations`}
      aria-hidden={!open}
      inert={!open ? "" : undefined}
    >
      <div className="chat-sidebar__head">
        <button
          type="button"
          className="chat-sidebar__new"
          disabled={creating}
          onClick={onNewChat}
        >
          <Plus aria-hidden="true" />
          <span>{creating ? "Creating" : mode === "code" ? "New coding task" : mode === "agent" ? "New task" : "New chat"}</span>
        </button>
        <label className="chat-sidebar__search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search chats</span>
          <input
            type="search"
            value={query}
            placeholder="Search chats"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Escape" || !query) return;
              event.preventDefault();
              event.stopPropagation();
              setQuery("");
            }}
          />
        </label>
        <button type="button" className="chat-sidebar__new" onClick={() => onSettings?.("memory")}><Brain aria-hidden="true" /><span>Memory</span></button>
      </div>

      {organised.filtered ? (
        <div className="chat-sidebar__scope" role="status">
          <span className="chat-sidebar__scope-name">
            {current ? (
              <>
                <FolderOpen aria-hidden="true" />
                {current.name}
              </>
            ) : (
              `${organised.visibleCount} of ${organised.total}`
            )}
          </span>
          <button
            type="button"
            onClick={() => {
              setBrowsing(null);
              setQuery("");
            }}
          >
            Show all
          </button>
        </div>
      ) : null}

      <nav className="chat-sidebar__body" aria-label="Conversations">
        {organised.pinned.length
          ? renderSection(
              "pinned",
              "Pinned",
              <div className="chat-rows">{organised.pinned.map(renderRow)}</div>,
              organised.pinned.length,
            )
          : null}

        {organised.folders.length
          ? renderSection(
              "folders",
              "Folders",
              <div className="chat-folders">
                {organised.folders.map((folder) =>
                  String(renamingFolderId) === String(folder.id) ? (
                    <form
                      className="chat-folder chat-folder--renaming"
                      key={folder.id}
                      onSubmit={(event) => {
                        event.preventDefault();
                        const name = folderDraft.trim();
                        setRenamingFolderId(null);
                        if (name && name !== folder.name) {
                          void onRenameFolder?.(folder, name);
                        }
                      }}
                    >
                      <input
                        autoFocus
                        value={folderDraft}
                        maxLength={60}
                        aria-label={`Rename folder ${folder.name}`}
                        onChange={(event) => setFolderDraft(event.target.value)}
                        onBlur={() => setRenamingFolderId(null)}
                        onKeyDown={(event) => {
                          if (event.key !== "Escape") return;
                          event.preventDefault();
                          event.stopPropagation();
                          setRenamingFolderId(null);
                        }}
                      />
                    </form>
                  ) : (
                    <div className="chat-folder" key={folder.id}>
                      <button
                        type="button"
                        className="chat-folder__open"
                        aria-pressed={browsing === folder.id}
                        onClick={() =>
                          setBrowsing(browsing === folder.id ? null : folder.id)
                        }
                      >
                        {browsing === folder.id ? (
                          <FolderOpen aria-hidden="true" />
                        ) : (
                          <Folder aria-hidden="true" />
                        )}
                        <span className="chat-folder__name">{folder.name}</span>
                        <small>{folder.count}</small>
                      </button>
                      <span className="chat-folder__actions">
                        <button
                          type="button"
                          aria-label={`Rename folder ${folder.name}`}
                          onClick={() => {
                            setRenamingFolderId(folder.id);
                            setFolderDraft(folder.name);
                          }}
                        >
                          <Pencil aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          aria-label={`Delete folder ${folder.name}`}
                          onClick={() => onDeleteFolder?.(folder)}
                        >
                          <Trash2 aria-hidden="true" />
                        </button>
                      </span>
                    </div>
                  ),
                )}
              </div>,
            )
          : null}

        {organised.groups.map((group) =>
          renderSection(
            `recents:${group.label}`,
            group.label,
            <div className="chat-rows">{group.items.map(renderRow)}</div>,
          ),
        )}

        {!organised.visibleCount ? (
          <p className="chat-sidebar__empty">
            {organised.total ? "No chats match that." : "No conversations yet."}
          </p>
        ) : null}
      </nav>
      <footer className="chat-sidebar__footer">
        <button type="button" className="training-destination" onClick={onTraining}><Layers aria-hidden="true" /><span>Models & training</span><ChevronRight aria-hidden="true" /></button>
        <div className="sidebar-utilities">
          <button type="button" aria-label="Settings" title="Settings" onClick={() => onSettings?.("general")}><SlidersHorizontal aria-hidden="true" /></button>
          <button type="button" aria-label="Connections" title="Connections" onClick={() => onSettings?.("plugins")}><Plug aria-hidden="true" /></button>
          <button type="button" aria-label="Notifications" title="Notifications" onClick={onNotifications}><Bell aria-hidden="true" /></button>
        </div>
      </footer>
    </aside>
  );
}
