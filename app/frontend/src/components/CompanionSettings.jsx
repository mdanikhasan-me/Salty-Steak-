import { useEffect, useRef, useState } from "react";

const KEY = "salty-steak:companion-v1";
let loading;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src; script.onload = resolve; script.onerror = reject;
    document.head.appendChild(script);
  });
}
export function loadCompanion() {
  if (!loading) loading = loadScript("/assets/companion/otter-data.js")
    .then(() => loadScript("/assets/companion/otter-viewer.js"))
    .catch((error) => { loading = undefined; throw error; });
  return loading;
}
function initialSettings() {
  try { return { enabled: false, position: "right", size: "small", ...JSON.parse(localStorage.getItem(KEY) || "{}") }; }
  catch { return { enabled: false, position: "right", size: "small" }; }
}

export function CompanionSettings() {
  const host = useRef(null);
  const [settings, setSettings] = useState(initialSettings);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [workPreview, setWorkPreview] = useState(false);
  const native = Boolean(window.chrome?.webview);
  useEffect(() => {
    let cancelled = false;
    let instance;
    loadCompanion().then(async () => {
      if (cancelled || !host.current) return;
      instance = await window.mountOtter(host.current);
      if (cancelled) instance?.dispose?.(); else setReady(true);
    }).catch(() => { if (!cancelled) setError("The companion could not load. Reopen Settings to retry."); });
    return () => { cancelled = true; instance?.dispose?.(); };
  }, []);
  useEffect(() => {
    const bridge = window.chrome?.webview;
    const receive = (event) => {
      if (event.data?.type === "companion_settings") setSettings(event.data.settings);
      if (event.data?.type === "companion_error") setError(event.data.message);
    };
    bridge?.addEventListener("message", receive);
    bridge?.postMessage({ type: "companion_get_settings" });
    return () => bridge?.removeEventListener("message", receive);
  }, []);
  const update = (patch) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch {}
    window.chrome?.webview?.postMessage({ type: "companion_settings", settings: next });
  };
  return <div className="preferences-page">
    <h2>Companion</h2><p className="preferences-intro">Rests when idle. Uses the tablet while your AI works.</p>
    <div className="companion-preview-row"><div ref={host} className="companion-preview" aria-label="Otter preview" /><div className="companion-preview-actions"><button type="button" disabled={!ready} aria-pressed={workPreview} onClick={() => { const next=!workPreview;setWorkPreview(next);host.current?.otter?.setWorking(next); }}>{workPreview ? "Stop preview" : "Preview working"}</button>{["Curious", "Greeting"].map((name) => <button type="button" key={name} disabled={!ready || workPreview} onClick={() => host.current?.otter?.play(name)}>{name}</button>)}</div></div>
    {error ? <p role="alert">{error}</p> : null}
    <label className="preference-row"><span>Desktop companion<small>{native ? "Sit above the Windows taskbar." : "Available in the Windows application."}</small></span><input type="checkbox" role="switch" aria-label="Desktop companion" checked={settings.enabled} disabled={!native} onChange={(event) => update({ enabled: event.target.checked })} /></label>
    <label className="preference-row"><span>Position<small>Adjust placement here.</small></span><select aria-label="Companion position" value={settings.position} onChange={(event) => update({ position: event.target.value })}><option value="right">Taskbar · right</option><option value="left">Taskbar · left</option></select></label>
    <label className="preference-row"><span>Size</span><select aria-label="Companion size" value={settings.size} onChange={(event) => update({ size: event.target.value })}><option value="small">Small</option><option value="medium">Medium</option></select></label>
    <label className="preference-row"><span>Keep still<small>Disable working motion and click reactions.</small></span><input type="checkbox" role="switch" aria-label="Keep companion still" checked={Boolean(settings.reducedMotion)} onChange={(event) => update({ reducedMotion: event.target.checked })} /></label>
  </div>;
}
