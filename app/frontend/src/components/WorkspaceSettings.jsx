import { useEffect, useState } from "react";
import { ChevronRight, X } from "lucide-react";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import { CompanionSettings } from "./CompanionSettings.jsx";
import { api } from "../api/client.js";

const APPEARANCE_KEY = "salty-steak:appearance-v1";
export function readAppearance() {
  try { return { theme: "dark", reducedMotion: false, ...JSON.parse(localStorage.getItem(APPEARANCE_KEY) || "{}") }; }
  catch { return { theme: "dark", reducedMotion: false }; }
}

export function applyAppearance(value) {
  document.documentElement.dataset.theme = value.theme === "warm" ? "warm" : "dark";
  document.documentElement.classList.toggle("reduce-motion", Boolean(value.reducedMotion));
  if (value.reducedMotion) window.stopOtters?.();
  window.chrome?.webview?.postMessage({ type: "application_motion", reducedMotion: Boolean(value.reducedMotion) });
}

const SECTIONS = [
  ["general", "General"], ["response", "Response"], ["advanced", "Advanced"],
  ["image", "Image generation"], ["memory", "Memory"], ["plugins", "Connections"],
  ["companion", "Companion"], ["about", "About"],
];

export function WorkspaceSettings({ section, onSectionChange, onClose, active = true, children }) {
  const ref = useModalFocusTrap({ active, onClose });
  return <section className="workspace-settings" ref={ref} role="dialog" aria-modal="true" aria-label="Settings" tabIndex={-1}>
    <nav className="workspace-settings__navigation" aria-label="Settings sections">
      <h2>Settings</h2>
      {SECTIONS.map(([id, label]) => <button key={id} type="button" aria-current={section === id ? "page" : undefined} onClick={() => onSectionChange(id)}>{label}</button>)}
    </nav>
    <button type="button" className="workspace-settings__close" aria-label="Close settings" onClick={onClose}><X aria-hidden="true" /></button>
    <div className="workspace-settings__content" key={section}>{children}</div>
  </section>;
}

export function GeneralSettings({ onSectionChange, onNavigate }) {
  const [appearance, setAppearance] = useState(readAppearance);
  const [saveError, setSaveError] = useState("");
  useEffect(() => {
    let active = true;
    applyAppearance(appearance);
    try { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance)); } catch {}
    void api.saveUiPreferences({ theme: appearance.theme, reducedMotion: appearance.reducedMotion })
      .then(() => { if (active) setSaveError(""); })
      .catch(() => { if (active) setSaveError("These preferences could not be saved. Check the local service and try again."); });
    return () => { active = false; };
  }, [appearance]);
  return <div className="preferences-page">
    <h2>General</h2><p className="preferences-intro">Make the workspace feel like yours.</p>
    {saveError ? <p role="alert">{saveError}</p> : null}
    <label className="preference-row"><span>Appearance<small>A calm background for your work.</small></span><select aria-label="Appearance" value={appearance.theme} onChange={(event) => setAppearance({ ...appearance, theme: event.target.value })}><option value="dark">Dark</option><option value="warm">Warm</option></select></label>
    <label className="preference-row"><span>Reduced motion<small>Keep state changes clear with less movement.</small></span><input type="checkbox" role="switch" aria-label="Reduced motion" checked={appearance.reducedMotion} onChange={(event) => setAppearance({ ...appearance, reducedMotion: event.target.checked })} /></label>
    <button type="button" className="preference-row preference-link" onClick={() => onSectionChange("companion")}><span>Desktop companion<small>Size, placement and short reactions.</small></span><ChevronRight aria-hidden="true" /></button>
    <button type="button" className="preference-row preference-link" onClick={() => onSectionChange("plugins")}><span>Connections<small>Connected tools and computer permissions.</small></span><ChevronRight aria-hidden="true" /></button>
    <button type="button" className="preference-row preference-link" onClick={() => onNavigate("system")}><span>System<small>Runtime, hardware and diagnostics.</small></span><ChevronRight aria-hidden="true" /></button>
  </div>;
}

export { CompanionSettings };
