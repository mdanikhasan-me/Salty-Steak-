import { useCallback, useEffect, useMemo, useState } from "react";
import { Brain, Search, Trash2 } from "lucide-react";

import { api } from "../api/client.js";
import { errorMessage } from "../workflows/formatters.js";

function memoryDate(value) {
  const milliseconds = Number(value || 0) * 1_000;
  if (!milliseconds) return "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(milliseconds));
}

export function MemoryPanel() {
  const [payload, setPayload] = useState({ memories: [], statistics: {} });
  const [query, setQuery] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      setPayload(await api.listChatMemories());
    } catch (cause) {
      setError(errorMessage(cause));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const memories = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return payload.memories || [];
    return (payload.memories || []).filter((memory) =>
      `${memory.subject || ""} ${memory.body || ""} ${(memory.tags || []).join(" ")}`
        .toLocaleLowerCase()
        .includes(needle));
  }, [payload.memories, query]);
  const hasAnyMemory = Boolean(
    (payload.memories || []).length || Number(payload.excluded_non_explicit || 0),
  );

  async function save(event) {
    event.preventDefault();
    const exact = note.trim();
    if (!exact || busy) return;
    setBusy("save");
    setError("");
    try {
      await api.saveChatMemory(exact);
      setNote("");
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function forget(memoryId) {
    if (busy) return;
    setBusy(memoryId);
    setError("");
    try {
      await api.forgetChatMemory(memoryId);
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  async function clearAll() {
    if (busy || !hasAnyMemory) return;
    if (!window.confirm("Delete every saved memory? Conversation history will not be deleted.")) return;
    setBusy("clear");
    setError("");
    try {
      await api.clearChatMemories();
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="memory-panel" aria-label="Memory management">
      <header className="memory-panel__intro">
        <Brain aria-hidden="true" />
        <div>
          <strong>Saved only when you ask</strong>
          <p>
            Chats stay separate. Use <code>/save mem</code> in a chat to save its
            current context, or <code>/save mem your note</code> to save one note.
          </p>
        </div>
      </header>

      <form className="memory-panel__add" onSubmit={save}>
        <label htmlFor="memory-note">Add a global memory</label>
        <textarea
          id="memory-note"
          rows="3"
          maxLength="12000"
          value={note}
          placeholder="A preference or context you want available in other chats"
          onChange={(event) => setNote(event.target.value)}
        />
        <button type="submit" disabled={!note.trim() || Boolean(busy)}>
          {busy === "save" ? "Saving…" : "Save memory"}
        </button>
      </form>

      <div className="memory-panel__bar">
        <label>
          <Search aria-hidden="true" />
          <span className="sr-only">Search memories</span>
          <input
            type="search"
            value={query}
            placeholder="Search saved memory"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="memory-panel__clear"
          disabled={Boolean(busy) || !hasAnyMemory}
          onClick={clearAll}
        >
          Clear all
        </button>
      </div>

      {error ? <p className="memory-panel__error" role="alert">{error}</p> : null}
      <p className="memory-panel__count">
        {Number(payload.statistics?.active || 0).toLocaleString()} saved
        <span>Automatic saving is off</span>
      </p>
      {Number(payload.excluded_non_explicit || 0) > 0 ? (
        <p className="memory-panel__legacy" role="status">
          {Number(payload.excluded_non_explicit).toLocaleString()} older non-explicit
          memory {Number(payload.excluded_non_explicit) === 1 ? "entry is" : "entries are"}
          disabled and never added to a chat prompt. Clear all removes them too.
        </p>
      ) : null}
      <div className="memory-panel__list">
        {memories.map((memory) => (
          <article key={memory.memory_id} className="memory-row">
            <div>
              <strong>{memory.subject}</strong>
              <p>{memory.body}</p>
              <small>{memoryDate(memory.created_at)}</small>
            </div>
            <button
              type="button"
              aria-label={`Delete memory ${memory.subject}`}
              disabled={Boolean(busy)}
              onClick={() => void forget(memory.memory_id)}
            >
              <Trash2 aria-hidden="true" />
            </button>
          </article>
        ))}
        {!memories.length ? (
          <p className="memory-panel__empty">
            {(payload.memories || []).length ? "No saved memory matches." : "Nothing is saved yet."}
          </p>
        ) : null}
      </div>
    </section>
  );
}

export default MemoryPanel;
