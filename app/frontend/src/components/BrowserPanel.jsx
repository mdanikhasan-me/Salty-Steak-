import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Globe, Plus, RefreshCw, X, FileText, Eye } from "lucide-react";
import { api } from "../api/client.js";
import { errorMessage } from "../workflows/formatters.js";
import "../styles/browser-panel.css";

export function BrowserPanel({ onClose, onPermissions, initialUrl = "" }) {
  const ref = useRef(null);
  const viewportRef = useRef(null);
  const native = Boolean(window.chrome?.webview?.postMessage);
  const [embedded, setEmbedded] = useState(false);
  const pending = useRef(false);
  const alive = useRef(true);
  const editingAddress = useRef(false);
  const [tabs, setTabs] = useState([]);
  const [active, setActive] = useState(null);
  const [address, setAddress] = useState("");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [allowed, setAllowed] = useState(null);
  const [updated, setUpdated] = useState(null);
  const [pageText, setPageText] = useState(null);
  const currentTab = tabs.find(tab => tab.tab === active);

  async function toggleText() {
    if (pageText !== null) { setPageText(null); return; }
    try { const page = await command("read_page", { tab: active }); if (alive.current) setPageText(page.summary || ""); }
    catch (reason) { if (alive.current) setError(errorMessage(reason)); }
  }

  function sizePane(width) {
    const workspace = ref.current?.closest('.chat-page');
    if (!workspace) return;
    const bounded = Math.min(window.innerWidth * 0.62, Math.max(360, width));
    workspace.style.setProperty('--browser-pane-width', `${Math.round(bounded)}px`);
  }

  async function command(command, args = {}) {
    const result = await api.invokeAutomation({ capability: "browser.control",
      arguments: { command, ...args }, user_confirmed: true, authority_mode: "ask_every_time" });
    if (result.status !== "succeeded") throw new Error(result.error || `Browser ${result.status}`);
    return result;
  }

  async function refresh(action, args = {}) {
    if (pending.current) return;
    pending.current = true;
    if (action) editingAddress.current = false;
    if (alive.current) { setBusy(true); setError(""); }
    try {
      if (action) await command(action, args);
      if (action && alive.current) setPageText(null);
      const state = await command("list_tabs");
      if (!alive.current) return;
      const tab = state.tabs?.find(item => item.tab === state.active) || state.tabs?.[0];
      setTabs(state.tabs || []); setActive(tab?.tab || null);
      if (!editingAddress.current) {
        setAddress(tab?.url === "about:blank" ? "" : tab?.url || "");
        editingAddress.current = false;
      }
      if (!native) {
        const image = await command("capture_preview", tab ? { tab: tab.tab } : {});
        if (alive.current) setPreview({ ...image, src: api.browserPreviewUrl(image.audit_record_id) });
      }
      if (alive.current) setUpdated(new Date());
    } catch (reason) {
      if (alive.current) setError(errorMessage(reason));
    } finally { pending.current = false; if (alive.current) setBusy(false); }
  }

  useEffect(() => {
    alive.current = true;
    let timer;
    let cancelled = false;
    async function tick() {
      if (cancelled) return;
      if (document.visibilityState !== "hidden") await refresh();
      if (!cancelled) timer = setTimeout(tick, 4000);
    }
    api.getAutomationStatus().then(status => {
      if (cancelled) return;
      const enabled = status.capabilities?.some(item => item.capability === "browser.control" && item.effective_enabled);
      setAllowed(Boolean(enabled));
      if (enabled) {
        if (initialUrl) void refresh("navigate", { url: initialUrl }).then(() => { if (!cancelled) timer = setTimeout(tick, 4000); });
        else void tick();
      }
    }).catch(reason => { if (alive.current) setError(errorMessage(reason)); });
    return () => { cancelled = true; alive.current = false; clearTimeout(timer); };
  }, []);

  useEffect(() => {
    if (!native || !allowed || !viewportRef.current) return undefined;
    const bridge = window.chrome.webview;
    let timer;
    let previous = "";
    function place() {
      const rect = viewportRef.current?.getBoundingClientRect();
      if (!rect) return;
      const blocked = [...document.querySelectorAll('.dialog-layer,.chat-settings-layer,.plugin-connect-layer')]
        .some(item => item.getClientRects().length > 0);
      const message = { type: "browser_surface", visible: pageText === null && !blocked && document.visibilityState !== "hidden" && rect.width >= 80 && rect.height >= 80,
        x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) };
      const key = JSON.stringify(message);
      if (key !== previous) { previous = key; bridge.postMessage(message); }
    }
    const schedule = () => { clearTimeout(timer); timer = setTimeout(place, 70); };
    function receive(event) {
      if (event.data?.type !== "browser_surface_state") return;
      setEmbedded(Boolean(event.data.embedded));
      if (event.data.error) setError(event.data.error);
      else if (event.data.embedded) setError("");
    }
    bridge.addEventListener("message", receive);
    const observer = new ResizeObserver(schedule); observer.observe(viewportRef.current);
    const mutations = new MutationObserver(schedule); mutations.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", schedule); document.addEventListener("visibilitychange", schedule);
    place();
    return () => {
      clearTimeout(timer); observer.disconnect(); mutations.disconnect(); window.removeEventListener("resize", schedule);
      document.removeEventListener("visibilitychange", schedule); bridge.removeEventListener("message", receive);
      bridge.postMessage({ type: "browser_surface", visible: false });
    };
  }, [allowed, native, pageText]);

  function navigate(event) {
    event.preventDefault();
    try {
      const url = new URL(/^[a-z][a-z\d+.-]*:/i.test(address.trim()) ? address.trim() : `https://${address.trim()}`);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("Enter an http or https website address.");
      void refresh("navigate", { url: url.href, ...(active ? { tab: active } : {}) });
    } catch (reason) { setError(errorMessage(reason)); }
  }

  return <aside className="browser-panel-dock">
    <div className="browser-panel__divider" role="separator" aria-label="Resize browser pane" aria-orientation="vertical" tabIndex={0}
      onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) sizePane(window.innerWidth - event.clientX); }}
      onPointerUp={event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}
      onKeyDown={event => { if (['ArrowLeft','ArrowRight'].includes(event.key)) { event.preventDefault(); sizePane(ref.current.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 24 : -24)); } }} />
    <section className="browser-panel" ref={ref} role="region" aria-label="Sawlper browser" tabIndex={-1}>
      <header className="browser-panel__header"><div className="browser-panel__view-switch">
        <button aria-label="Page view" aria-pressed={pageText === null} onClick={() => setPageText(null)}><Eye size={15} /></button>
        <button aria-label="Read page text" aria-pressed={pageText !== null} disabled={busy || !active} onClick={toggleText}><FileText size={15} /></button>
      </div><strong title={currentTab?.title || 'Browser'}>{currentTab?.title || 'Browser'}</strong><div className="browser-panel__header-actions">
        <button aria-label="New browser tab" disabled={busy || !allowed} onClick={() => refresh("new_tab")}><Plus size={16} /></button>
        <button aria-label="Close browser preview" onClick={onClose}><X size={17} /></button>
      </div></header>
      {allowed === false ? <div className="browser-panel__empty"><Globe size={28} /><h3>Enable browser access</h3><p>Browse with Sawlper and see the pages it uses here.</p><button onClick={onPermissions}>Open computer access</button></div> : <>
        <div className={`browser-panel__tabs ${tabs.length < 2 ? 'browser-panel__tabs--single' : ''}`} role="tablist" aria-label="Browser tabs">
          {tabs.map(tab => <div className="browser-panel__tab" key={tab.tab}><button role="tab" aria-selected={tab.tab === active} disabled={busy} onClick={() => refresh("switch_tab", { tab: tab.tab })}>{tab.title || (tab.url === "about:blank" ? "New tab" : tab.url)}</button><button aria-label={`Close ${tab.title || "tab"}`} disabled={busy || tabs.length < 2} onClick={() => refresh("close_tab", { tab: tab.tab })}><X size={13} /></button></div>)}
        </div>
        <form className="browser-panel__address" onSubmit={navigate}>
          <button type="button" aria-label="Browser back" disabled={busy || !active} onClick={() => refresh("back", { tab: active })}><ArrowLeft size={17} /></button>
          <button type="button" aria-label="Browser forward" disabled={busy || !active} onClick={() => refresh("forward", { tab: active })}><ArrowRight size={17} /></button>
          <button type="button" aria-label="Reload browser page" disabled={busy || !active} onClick={() => refresh("reload", { tab: active })}><RefreshCw size={16} /></button>
          <input aria-label="Website address" placeholder="Enter a website address" value={address} onChange={event => { editingAddress.current = true; setAddress(event.target.value); }} spellCheck={false} />
          <button type="submit" aria-label="Go to address" disabled={busy || !allowed || !address.trim()}><ArrowRight size={14} /></button>
        </form>
        <div className="browser-panel__viewport" ref={viewportRef} aria-busy={busy} data-embedded={embedded}>
          {pageText !== null ? <pre className="browser-panel__text">{pageText || "No readable page text was found."}</pre> : native ? <div className="browser-panel__empty"><Globe size={30} /><p>{embedded ? "Live page" : "Opening browser here…"}</p></div> : preview ? <img src={preview.src} alt={`Browser snapshot: ${preview.title || preview.url || "New tab"}`} /> : <div className="browser-panel__empty"><Globe size={30} /><p>{allowed ? "Opening your browser…" : "Checking browser access…"}</p></div>}
        </div>
        {!native && <footer className="browser-panel__footer"><span>{updated ? `Updated ${updated.toLocaleTimeString()}` : 'Preview'}</span></footer>}
        {!native ? <p className="browser-panel__note">Live interaction is available inside the Salty Steak desktop app.</p> : null}
      </>}
      {error ? <p className="browser-panel__error" role="alert">{error}</p> : null}
    </section>
  </aside>;
}
