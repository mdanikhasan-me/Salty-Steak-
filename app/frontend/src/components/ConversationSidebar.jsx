import { useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronRight,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Tag,
  Trash2,
  X,
} from "lucide-react";

import {
  activeLabel,
  conversationTitle,
  labelChoicesFor,
  organiseConversations,
} from "../workflows/conversationOrganisation.mjs";












const SECTION_STORAGE_KEY = "salty-steak:sidebar-sections";

function storedSections() {
  try {
    return JSON.parse(window.localStorage.getItem(SECTION_STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

export function ConversationSidebar({
  open,
  conversations,
  labels,
  selectedId,
  creating,
  onNewChat,
  onSelect,
  onRename,
  onDelete,
  onTogglePin,
  onToggleLabel,
  onCreateLabel,
  onRenameLabel,
  onDeleteLabel,
  onAfterSelect,
}) {
  const [query, setQuery] = useState("");
  const [labelFilter, setLabelFilter] = useState(null);
  const [menuFor, setMenuFor] = useState(null);
  const [submenuFor, setSubmenuFor] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [newLabelFor, setNewLabelFor] = useState(null);
  const [newLabelName, setNewLabelName] = useState("");
  const [renamingLabelId, setRenamingLabelId] = useState(null);
  const [labelDraft, setLabelDraft] = useState("");
  const [collapsed, setCollapsed] = useState(storedSections);
  const triggerRefs = useRef(new Map());
  const renameRef = useRef(null);

  const organised = useMemo(
    () =>
      organiseConversations({
        conversations,
        labels,
        query,
        activeLabelId: labelFilter,
      }),
    [conversations, labels, query, labelFilter],
  );
  const filteringLabel = activeLabel(organised);

  useEffect(() => {
    try {
      window.localStorage.setItem(SECTION_STORAGE_KEY, JSON.stringify(collapsed));
    } catch {

    }
  }, [collapsed]);


  useEffect(() => {
    if (labelFilter && !organised.labels.some((item) => item.id === labelFilter)) {
      setLabelFilter(null);
    }
  }, [labelFilter, organised.labels]);

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
      if (event.key === "Escape") {
        event.stopPropagation();
        closeMenu();
      }
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
    setNewLabelFor(null);
    setNewLabelName("");
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

  async function createLabelAndApply(item) {
    const name = newLabelName.trim();
    if (!name) return;
    setNewLabelName("");
    setNewLabelFor(null);
    await onCreateLabel?.(name, item);
  }

  const rowProps = (item) => ({
    key: item.id,
    className: [
      "chat-row",
      String(item.id) === String(selectedId) ? "chat-row--active" : "",
      String(menuFor) === String(item.id) ? "chat-row--menu" : "",
    ]
      .filter(Boolean)
      .join(" "),
  });

  function renderRow(item) {
    const title = conversationTitle(item);
    const pinned = Boolean(item.pinned ?? item.pinned_at);
    const menuOpen = String(menuFor) === String(item.id);
    if (String(renamingId) === String(item.id)) {
      return (
        <div {...rowProps(item)}>
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
      <div {...rowProps(item)}>
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
          {(item.labels || []).length ? (
            <span className="chat-row__labels" aria-hidden="true">
              {(item.labels || []).slice(0, 3).map((label) => (
                <span
                  key={label.id}
                  className={`chat-dot chat-dot--${label.tone || "neutral"}`}
                />
              ))}
            </span>
          ) : null}
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
    const choices = labelChoicesFor(item, labels);
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
          aria-expanded={String(submenuFor) === String(item.id)}
          className="chat-menu__submenu-trigger"
          onClick={() =>
            setSubmenuFor(
              String(submenuFor) === String(item.id) ? null : item.id,
            )
          }
        >
          <Tag aria-hidden="true" /> Add label
          <ChevronRight aria-hidden="true" className="chat-menu__chevron" />
        </button>
        {String(submenuFor) === String(item.id) ? (
          <div className="chat-menu__submenu" role="menu" aria-label="Labels">
            {choices.map((choice) => (
              <button
                key={choice.id}
                type="button"
                role="menuitemcheckbox"
                aria-checked={choice.applied}
                onClick={() => onToggleLabel?.(item, choice.id, !choice.applied)}
              >
                <span className={`chat-dot chat-dot--${choice.tone}`} aria-hidden="true" />
                <span className="chat-menu__label-name">{choice.name}</span>
                {choice.applied ? <Check aria-hidden="true" /> : null}
              </button>
            ))}
            {newLabelFor === item.id ? (
              <form
                className="chat-menu__new-label"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createLabelAndApply(item);
                }}
              >
                <input
                  autoFocus
                  value={newLabelName}
                  maxLength={60}
                  placeholder="Label name"
                  aria-label="New label name"
                  onChange={(event) => setNewLabelName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return;
                    event.preventDefault();
                    event.stopPropagation();
                    setNewLabelFor(null);
                  }}
                />
              </form>
            ) : (
              <button
                type="button"
                role="menuitem"
                className="chat-menu__create"
                onClick={() => setNewLabelFor(item.id)}
              >
                <Plus aria-hidden="true" /> New label
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
      aria-label="Chats"
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
          <span>{creating ? "Creating" : "New chat"}</span>
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
        {organised.filtered ? (
          <div className="chat-sidebar__filter" role="status">
            <span>
              {filteringLabel ? (
                <>
                  <span
                    className={`chat-dot chat-dot--${filteringLabel.tone}`}
                    aria-hidden="true"
                  />
                  {filteringLabel.name}
                </>
              ) : (
                `${organised.visibleCount} of ${organised.total}`
              )}
            </span>
            <button
              type="button"
              onClick={() => {
                setLabelFilter(null);
                setQuery("");
              }}
            >
              <X aria-hidden="true" /> Show all
            </button>
          </div>
        ) : null}
      </div>

      <nav className="chat-sidebar__body" aria-label="Conversations">
        {organised.pinned.length
          ? renderSection(
              "pinned",
              "Pinned",
              <div className="chat-rows">{organised.pinned.map(renderRow)}</div>,
              organised.pinned.length,
            )
          : null}

        {organised.labels.length
          ? renderSection(
              "labels",
              "Labels",
              <div className="chat-labels">
                {organised.labels.map((label) =>
                  String(renamingLabelId) === String(label.id) ? (
                    <form
                      className="chat-label chat-label--renaming"
                      key={label.id}
                      onSubmit={(event) => {
                        event.preventDefault();
                        const name = labelDraft.trim();
                        setRenamingLabelId(null);
                        if (name && name !== label.name) {
                          void onRenameLabel?.(label, name);
                        }
                      }}
                    >
                      <input
                        autoFocus
                        value={labelDraft}
                        maxLength={60}
                        aria-label={`Rename label ${label.name}`}
                        onChange={(event) => setLabelDraft(event.target.value)}
                        onBlur={() => setRenamingLabelId(null)}
                        onKeyDown={(event) => {
                          if (event.key !== "Escape") return;
                          event.preventDefault();
                          event.stopPropagation();
                          setRenamingLabelId(null);
                        }}
                      />
                    </form>
                  ) : (
                    <div className="chat-label" key={label.id}>
                      <button
                        type="button"
                        className="chat-label__open"
                        aria-pressed={labelFilter === label.id}
                        onClick={() =>
                          setLabelFilter(labelFilter === label.id ? null : label.id)
                        }
                      >
                        <span
                          className={`chat-dot chat-dot--${label.tone}`}
                          aria-hidden="true"
                        />
                        <span className="chat-label__name">{label.name}</span>
                        <small>{label.count}</small>
                      </button>
                      <button
                        type="button"
                        className="chat-label__edit"
                        aria-label={`Rename label ${label.name}`}
                        onClick={() => {
                          setRenamingLabelId(label.id);
                          setLabelDraft(label.name);
                        }}
                      >
                        <Pencil aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="chat-label__remove"
                        aria-label={`Delete label ${label.name}`}
                        onClick={() => onDeleteLabel?.(label)}
                      >
                        <Trash2 aria-hidden="true" />
                      </button>
                    </div>
                  ),
                )}
              </div>,
            )
          : null}

        {organised.groups.length
          ? organised.groups.map((group) =>
              renderSection(
                `recents:${group.label}`,
                group.label,
                <div className="chat-rows">{group.items.map(renderRow)}</div>,
              ),
            )
          : null}

        {!organised.visibleCount ? (
          <p className="chat-sidebar__empty">
            {organised.total
              ? "No chats match that."
              : "No conversations yet."}
          </p>
        ) : null}
      </nav>
    </aside>
  );
}
