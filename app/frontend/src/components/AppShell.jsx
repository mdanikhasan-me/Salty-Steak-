import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import {
  Archive,
  Database,
  GraduationCap,
  MessageCircle,
  MonitorCog,
  FlaskConical,
  Bell,
  X,
  Sprout,
} from "lucide-react";
import { useAppState } from "../state/AppState.jsx";
import { drawerReducer } from "../workflows/drawer.mjs";
import { isTrainingPage } from "../workflows/navigation.mjs";
import { NotificationCenter } from "./NotificationCenter.jsx";
import { api } from "../api/client.js";

import { WORKSPACE_MODES, normaliseWorkspaceMode } from "../workflows/workspaceModes.mjs";
import { applyAppearance, readAppearance } from "./WorkspaceSettings.jsx";

const SIDEBAR_STORAGE_KEY = "salty-potato:sidebar-open";
const ShellContext = createContext(null);

const TRAINING_DESTINATIONS = [
  { id: "data", label: "Data", description: "Sources and preparation", icon: Database },
  { id: "train", label: "Train", description: "Runs and telemetry", icon: Sprout },
  { id: "versions", label: "Models", description: "Local runtimes and checkpoints", icon: Archive },
  { id: "evaluate", label: "Evaluate", description: "Metrics and comparisons", icon: FlaskConical },
  { id: "system", label: "System", description: "Runtime and diagnostics", icon: MonitorCog },
];

function readSidebarPreference() {
  try {
    return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) !== "closed";
  } catch {
    return true;
  }
}

export function useShell() {
  const context = useContext(ShellContext);
  if (!context) throw new Error("useShell must be used inside AppShell.");
  return context;
}

export function AppShell({ page, aboutFrom = "chat", onNavigate, children }) {
  const [sidebarOpen, dispatchSidebar] = useReducer(
    drawerReducer,
    undefined,
    readSidebarPreference,
  );
  const { connection, chatStatus, notifications } = useAppState();
  const [workspaceMode, setWorkspaceModeState] = useState(() => {
    try { return normaliseWorkspaceMode(sessionStorage.getItem("salty-steak:workspace-mode")); } catch { return "chat"; }
  });
  const [settingsRequest, setSettingsRequest] = useState(null);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notificationAnchor, setNotificationAnchor] = useState({ left: 18, bottom: 52 });
  const notificationTrigger = useRef(null);
  const notificationPanel = useRef(null);
  const closeNotifications = useCallback(() => setNotificationsOpen(false), []);
  const openNotifications = useCallback(event => {
    const trigger = event?.currentTarget;
    const rect = trigger?.getBoundingClientRect();
    notificationTrigger.current = trigger;
    if (rect) setNotificationAnchor({ left: Math.max(12, Math.min(innerWidth - 292, rect.left)), bottom: Math.max(12, innerHeight - rect.top + 8) });
    setSettingsRequest({ close: true, id: Date.now() });
    setNotificationsOpen(open => !open);
  }, []);
  useEffect(() => {
    if (!notificationsOpen) return;
    function dismiss(event) {
      if (notificationPanel.current?.contains(event.target) || notificationTrigger.current?.contains(event.target)) return;
      setNotificationsOpen(false);
    }
    function key(event) {
      if (event.key !== "Escape") return;
      setNotificationsOpen(false); notificationTrigger.current?.focus();
    }
    document.addEventListener("pointerdown", dismiss);document.addEventListener("keydown", key);
    window.addEventListener("resize", closeNotifications);
    return () => { document.removeEventListener("pointerdown", dismiss);document.removeEventListener("keydown", key);window.removeEventListener("resize", closeNotifications); };
  }, [notificationsOpen, closeNotifications]);
  const [preferencesLoaded, setPreferencesLoaded] = useState(false);
  const touchedPreferences = useRef({ mode: false, sidebar: false });
  const changeSidebar = useCallback(action => {
    touchedPreferences.current.sidebar = true;
    dispatchSidebar(action);
  }, []);
  const setWorkspaceMode = (mode) => {
    touchedPreferences.current.mode = true;
    closeNotifications();
    const next = normaliseWorkspaceMode(mode);
    if (next === workspaceMode) return;
    setSettingsRequest(null);
    setWorkspaceModeState(next);
    try { sessionStorage.setItem("salty-steak:workspace-mode", next); } catch {}
  };
  useEffect(() => {
    let active = true;
    api.getUiPreferences().then((saved) => {
      if (!active) return;
      const appearance = { ...readAppearance(), ...saved };
      applyAppearance(appearance);
      try { localStorage.setItem("salty-steak:appearance-v1", JSON.stringify(appearance)); } catch {}
      if (!touchedPreferences.current.sidebar && typeof saved.sidebarOpen === "boolean") dispatchSidebar({ type: saved.sidebarOpen ? "open" : "close" });
      if (!touchedPreferences.current.mode && saved.workspaceMode) setWorkspaceModeState(normaliseWorkspaceMode(saved.workspaceMode));
      setPreferencesLoaded(true);
    }).catch(() => { if (active) { applyAppearance(readAppearance()); setPreferencesLoaded(true); } });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (preferencesLoaded) void api.saveUiPreferences({ sidebarOpen, workspaceMode }).catch(() => {});
  }, [preferencesLoaded, sidebarOpen, workspaceMode]);
  const requestSettings = (section = "general") => {
    setSettingsRequest({ section, id: Date.now() });
    if (page !== "chat") onNavigate("chat");
  };










  const lastFocused = useRef(null);
  useEffect(() => {
    function remember(event) {
      const node = event.target;
      if (!node || node === document.body) return;
      if (node.id === "main-content") return;
      lastFocused.current = node;
    }
    document.addEventListener("focusin", remember, true);
    return () => document.removeEventListener("focusin", remember, true);
  }, []);

  const returnFocusFromMain = useCallback((event) => {

    if (event.relatedTarget) return;
    const previous = lastFocused.current;
    if (!previous || !previous.isConnected || previous === event.target) return;
    previous.focus({ preventScroll: true });
  }, []);
  const contentTrainingPage = isTrainingPage(page);
  const trainingMode =
    contentTrainingPage || (page === "about" && isTrainingPage(aboutFrom));

  useEffect(() => {
    try {
      window.localStorage.setItem(
        SIDEBAR_STORAGE_KEY,
        sidebarOpen ? "open" : "closed",
      );
    } catch {

    }
  }, [sidebarOpen]);

  const shellValue = {
    sidebarOpen, workspaceMode, setWorkspaceMode, settingsRequest, requestSettings,
    openNotifications, closeNotifications,
    toggleSidebar: () => changeSidebar({ type: "toggle" }),
    closeSidebar: () => changeSidebar({ type: "close" }),
  };

  return (
    <ShellContext.Provider value={shellValue}>
      <div
        className={`app-shell ${
          trainingMode && sidebarOpen ? "app-shell--sidebar-open" : ""
        }`}
      >
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>

        <header className={`app-toolbar ${sidebarOpen ? "app-toolbar--sidebar-open" : ""}`}>
          <div className="workspace-brand">
            <img src="/assets/salty-potato-symbol.svg" width="19" height="19" alt="" />
            <span>salty steak</span>
            <button className="icon-button app-menu-button" type="button" title={sidebarOpen ? "Close sidebar" : "Open sidebar"} aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"} aria-expanded={sidebarOpen} aria-controls="workspace-sidebar" onClick={() => changeSidebar({ type: "toggle" })}><SidebarGlyph aria-hidden="true" /></button>
          </div>
          <div className="workspace-heading">
            <span className="workspace-context">{trainingMode ? (TRAINING_DESTINATIONS.find((item) => item.id === page)?.label || "Models & training") : workspaceMode === "code" ? "Code workspace" : workspaceMode === "agent" ? "Agent workspace" : "Workspace"}</span>
            {trainingMode ? (
              <nav className="workspace-return" aria-label="Primary workspace"><button type="button" onClick={() => onNavigate("chat")}><MessageCircle aria-hidden="true" /><span>Chat</span></button><span>Models & training</span></nav>
            ) : (
              <nav className="workspace-modes" aria-label="Primary workspace" style={{ "--mode-index": WORKSPACE_MODES.findIndex((mode) => mode.id === workspaceMode) }}>
                {WORKSPACE_MODES.map((mode) => <button key={mode.id} type="button" aria-pressed={workspaceMode === mode.id} onClick={() => setWorkspaceMode(mode.id)}>{mode.label}</button>)}
              </nav>
            )}
            <RuntimeStatus connection={connection} chatStatus={chatStatus} />
          </div>
        </header>

        <div className={`app-frame ${trainingMode ? "app-frame--training" : ""}`}>
          {trainingMode ? (
            <TrainingSidebar
              page={page}
              open={sidebarOpen}
              onNavigate={onNavigate}
            />
          ) : null}
          {trainingMode && sidebarOpen ? (
            <button
              className="workspace-sidebar-backdrop"
              type="button"
              aria-label="Close Models and training sidebar"
              onClick={() => changeSidebar({ type: "close" })}
            />
          ) : null}
          <main
            id="main-content"
            className={`main-content ${
              contentTrainingPage ? "main-content--training" : ""
            } ${page === "about" ? "main-content--about" : ""}`}
            tabIndex="-1"
            onFocus={returnFocusFromMain}
          >
            {!connection.online && !connection.loading ? (
              <div className="connection-banner" role="status">
                The local service is unavailable. Your work stays on this computer while the app reconnects.
              </div>
            ) : null}
            {children}
          </main>
        </div>

        {notificationsOpen ? <aside ref={notificationPanel} style={notificationAnchor} className="workspace-notifications" aria-label="Notification history"><header><h2>Notifications</h2><button className="icon-button" aria-label="Close notifications" onClick={closeNotifications}><X /></button></header>{notifications.length ? notifications.map((item) => <p key={item.id}>{item.message}</p>) : <p>You're all caught up.</p>}</aside> : null}
        <NotificationCenter page={page} />
      </div>
    </ShellContext.Provider>
  );
}

function SidebarGlyph(props) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.45"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <rect x="2.75" y="3" width="14.5" height="14" rx="1.6" />
      <path d="M7.2 3.35v13.3" />
    </svg>
  );
}

function TrainingSidebar({ page, open, onNavigate }) {
  return (
    <aside
      id="workspace-sidebar"
      className={`workspace-sidebar ${
        open ? "workspace-sidebar--open" : "workspace-sidebar--closed"
      }`}
      aria-label="Training"
      aria-hidden={!open}
      inert={!open ? "" : undefined}
    >
      <div className="workspace-sidebar__heading">
        <span>Models & training</span>
        <small>Model workbench</small>
      </div>
      <nav className="workspace-sidebar__navigation" aria-label="Training destinations">
        {TRAINING_DESTINATIONS.map((item) => {
          const Icon = item.icon;
          const selected = page === item.id;
          return (
            <button
              key={item.id}
              type="button"
              className={selected ? "sidebar-row sidebar-row--active" : "sidebar-row"}
              aria-current={selected ? "page" : undefined}
              onClick={() => onNavigate(item.id)}
            >
              <Icon aria-hidden="true" />
              <span className="sidebar-row__copy">
                <strong>{item.label}</strong>
                <small>{item.description}</small>
              </span>
            </button>
          );
        })}
      </nav>
      <div className="workspace-sidebar__footer"><button type="button" className="sidebar-about" onClick={() => onNavigate("chat")}><MessageCircle aria-hidden="true" /><span>Back to workspace</span></button></div>
    </aside>
  );
}

function RuntimeStatus({ connection, chatStatus }) {
  const raw = String(
    chatStatus?.readiness || chatStatus?.runtime_state || chatStatus?.state || "",
  ).toLowerCase();
  const activeVersion = chatStatus?.active_saved_version_id || chatStatus?.saved_version_id;
  let state = "idle";
  let label = "Local · no model";
  if (connection.loading) {
    state = "loading";
    label = "Connecting";
  } else if (connection.online && (chatStatus?.runtime_ready === true || (activeVersion && raw === "ready"))) {
    state = "ready";
    label = "Ready";
  } else if (connection.online && activeVersion && raw === "preparing_salty_potato") {
    state = "loading";
    label = "Preparing model";
  } else if (connection.online && (raw.includes("prepar") || raw.includes("load"))) {
    state = "loading";
    label = "Loading model";
  } else if (connection.online && activeVersion) {
    state = "selected";
    label = "Model selected";
  }
  return (
    <span className={`runtime-status runtime-status--${state}`} role="status" title={label}>
      <span className="connection-dot" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
