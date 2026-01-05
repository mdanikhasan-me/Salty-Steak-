import {
  createContext,
  useContext,
  useEffect,
  useReducer,
} from "react";
import {
  Archive,
  Database,
  GraduationCap,
  MessageCircle,
  MonitorCog,
  Sprout,
} from "lucide-react";
import { useAppState } from "../state/AppState.jsx";
import { drawerReducer } from "../workflows/drawer.mjs";
import { isTrainingPage } from "../workflows/navigation.mjs";
import { NotificationCenter } from "./NotificationCenter.jsx";

const SIDEBAR_STORAGE_KEY = "salty-potato:sidebar-open";
const ShellContext = createContext(null);

const TRAINING_DESTINATIONS = [
  { id: "data", label: "Data", description: "Sources and preparation", icon: Database },
  { id: "train", label: "Train", description: "Runs and telemetry", icon: Sprout },
  { id: "versions", label: "Models", description: "Local runtimes and checkpoints", icon: Archive },
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
  const { connection, chatStatus } = useAppState();
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
    sidebarOpen,
    toggleSidebar: () => dispatchSidebar({ type: "toggle" }),
    closeSidebar: () => dispatchSidebar({ type: "close" }),
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

        <header className="app-toolbar">
          <button
            className="icon-button app-menu-button"
            type="button"
            aria-label={sidebarOpen ? "Close sidebar" : "Open sidebar"}
            aria-expanded={sidebarOpen}
            aria-controls="workspace-sidebar"
            onClick={() => dispatchSidebar({ type: "toggle" })}
          >
            <SidebarGlyph aria-hidden="true" />
          </button>

          <nav className="workspace-switch" aria-label="Primary workspace">
            <button
              className={!trainingMode ? "workspace-switch__button is-active" : "workspace-switch__button"}
              type="button"
              aria-current={!trainingMode ? "page" : undefined}
              onClick={() => onNavigate("chat")}
            >
              <MessageCircle aria-hidden="true" />
              <span>Chat</span>
            </button>
            <button
              className={trainingMode ? "workspace-switch__button is-active" : "workspace-switch__button"}
              type="button"
              aria-current={trainingMode ? "page" : undefined}
              onClick={() => onNavigate("training-center")}
            >
              <GraduationCap aria-hidden="true" />
              <span>Models & training</span>
            </button>
          </nav>

          <RuntimeStatus connection={connection} chatStatus={chatStatus} />
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
              onClick={() => dispatchSidebar({ type: "close" })}
            />
          ) : null}
          <main
            id="main-content"
            className={`main-content ${
              contentTrainingPage ? "main-content--training" : ""
            } ${page === "about" ? "main-content--about" : ""}`}
            tabIndex="-1"
          >
            {!connection.online && !connection.loading ? (
              <div className="connection-banner" role="status">
                The local service is unavailable. Your work stays on this computer while the app reconnects.
              </div>
            ) : null}
            {children}
          </main>
        </div>

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
        <small>Local workspace</small>
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
      <div className="workspace-sidebar__local-state">
        <span className="connection-dot" aria-hidden="true" />
        <span>
          <strong>Local workspace</strong>
          <small>Data remains on this computer</small>
        </span>
      </div>
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
