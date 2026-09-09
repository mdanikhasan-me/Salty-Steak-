import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [clearRequested, setClearRequested] = useState(false);
  const clearButton = useRef(null);
  const cancelClearButton = useRef(null);
  useEffect(() => {
    if (!clearRequested) return;
    const frame = requestAnimationFrame(() => cancelClearButton.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, [clearRequested]);

  const refresh = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      setPayload(await api.listChatMemories());
      setLoaded(true);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLoading(false);
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
    setBusy("clear");
    setError("");
    try {
      await api.clearChatMemories();
      setClearRequested(false);
      await refresh();
      clearButton.current?.focus();
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
          ref={clearButton}
          disabled={Boolean(busy) || !hasAnyMemory}
          onClick={() => setClearRequested(true)}
        >
          Clear all
        </button>
      </div>

      {clearRequested ? <div className="memory-clear-confirmation" role="group" aria-label="Confirm clearing saved memories" onKeyDown={event => {
        if (event.key === "Escape" && !busy) {
          event.preventDefault(); event.stopPropagation(); setClearRequested(false); clearButton.current?.focus();
        }
      }}>
        <strong>Delete all saved memories?</strong>
        <p>These notes will be removed permanently. Conversation history stays in place.</p>
        <div><button ref={cancelClearButton} type="button" disabled={Boolean(busy)} onClick={() => { setClearRequested(false); clearButton.current?.focus(); }}>Cancel</button><button type="button" className="memory-clear-confirmation__delete" disabled={Boolean(busy)} onClick={() => void clearAll()}>{busy === "clear" ? "Deleting…" : "Delete memories"}</button></div>
      </div> : null}

      {error ? <div className="memory-panel__error" role="alert"><p>{error}</p>{!loaded ? <button type="button" disabled={loading} onClick={() => void refresh()}>Try again</button> : null}</div> : null}
      {loading && !loaded ? <p role="status">Loading saved memories…</p> : null}
      {loaded ? <p className="memory-panel__count">
        {Number(payload.statistics?.active || 0).toLocaleString()} saved
        <span>Automatic saving is off</span>
      </p> : null}
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
        {loaded && !memories.length ? (
          <p className="memory-panel__empty">
            {(payload.memories || []).length ? "No saved memory matches." : "Nothing is saved yet."}
          </p>
        ) : null}
      </div>
    </section>
  );
}

export default MemoryPanel;
