import { useState } from "react";
import { Plus, ServerCog } from "lucide-react";
import {
  CONNECTOR_DEFINITIONS,
  connectorPresentation,
  normaliseConnector,
} from "../workflows/pluginConnections.mjs";
import { ComputerControlPermissions } from "./ComputerControlPermissions.jsx";
import { GmailBrand, CalendarBrand, CloudBrand } from "./ConnectorBrand.jsx";
import "../styles/plugins-panel.css";

const CONNECTOR_ICONS = {
  gmail: GmailBrand,
  google_calendar: CalendarBrand,
  icloud_calendar: CloudBrand,
};

export function PluginsPanel({
  connections = {},
  automationAdapter = null,
  proposedInvocation = null,
  onAnalyzeCapture = null,
  loading = false,
  error = "",
  busyId = "",
  onRetry,
  onConnect,
  onManage,
  onAddMcpServer,
  onOpenBrowser,
}) {
  const [section, setSection] = useState("services");
  const interactive = !loading && !error;
  const mcpConnector = normaliseConnector(connections.mcp);
  const mcpPresentation = connectorPresentation(mcpConnector, busyId === "mcp");
  const mcpHandler = mcpPresentation.action === "manage" ? onManage : onAddMcpServer;

  return (
    <section className="plugins-panel" aria-label="Plugins">
      {loading ? <p className="plugins-panel__notice" role="status">Loading connections...</p> : null}
      {error ? (
        <div className="plugins-panel__notice plugins-panel__notice--error" role="alert">
          <span>Connections could not be loaded.</span>
          {onRetry ? <button type="button" onClick={onRetry}>Try again</button> : null}
        </div>
      ) : null}

      <div className="connections-sections" role="group" aria-label="Connection categories">
        <button type="button" aria-pressed={section === "services"} onClick={() => setSection("services")}>Services</button>
        <button type="button" aria-pressed={section === "computer"} onClick={() => setSection("computer")}>Computer access</button>
      </div>
      {section === "services" ? <div className="plugins-list" role="list">
        <div className="service-browser-access">
          <strong>Use your email in the browser</strong>
          <p>Open your mailbox in Sawlper’s browser and sign in yourself. These shortcuts do not connect an API or send mail.</p>
          <div><button type="button" className="plugin-action" onClick={() => onOpenBrowser?.("https://mail.google.com/")} disabled={!onOpenBrowser}>Open Gmail</button>
          <button type="button" className="plugin-action" onClick={() => onOpenBrowser?.("https://outlook.live.com/mail/")} disabled={!onOpenBrowser}>Open Outlook</button></div>
        </div>
        {CONNECTOR_DEFINITIONS.map((definition) => {
          const connector = normaliseConnector(connections[definition.id]);
          const presentation = connectorPresentation(connector, busyId === definition.id);
          const Icon = CONNECTOR_ICONS[definition.id];
          const handler = presentation.action === "manage" ? onManage : onConnect;
          const handlerAvailable = interactive && typeof handler === "function";
          return (
            <PluginRow
              key={definition.id}
              icon={Icon}
              iconTone={definition.id}
              name={definition.name}
              description={({ gmail: "Search and draft email.", google_calendar: "Find events and manage your calendar.", icloud_calendar: "Access your iCloud calendar." })[definition.id] || definition.description}
              state={presentation.stateLabel}
              stateTone={connector.status}
              detail={connector.detail || connector.unavailable_reason || ""}
              action={presentation.action !== "none" ? (
                <button
                  type="button"
                  className="plugin-action"
                  disabled={presentation.disabled || !handlerAvailable}
                  title={!handlerAvailable ? `${definition.name} setup is not available in this view.` : undefined}
                  onClick={() => handler?.(definition.id, connector)}
                >
                  {presentation.actionLabel}
                </button>
              ) : null}
            />
          );
        })}

        <PluginRow
          icon={ServerCog}
          iconTone="mcp"
          name="MCP server"
          description="Connect a tool server and choose what it can do."
          state={mcpPresentation.stateLabel}
          stateTone={mcpConnector.status}
          detail={mcpConnector.detail || mcpConnector.unavailable_reason || ""}
          action={mcpPresentation.action !== "none" ? (
            <button
              type="button"
              className="plugin-action plugin-action--add"
              disabled={!interactive || mcpPresentation.disabled || !mcpHandler}
              title={!mcpHandler ? "MCP server setup is not available in this view." : undefined}
              onClick={() => mcpHandler?.("mcp", mcpConnector)}
            >
              {mcpPresentation.action === "connect" ? <Plus aria-hidden="true" /> : null}
              {mcpPresentation.action === "connect" ? "Add server" : mcpPresentation.actionLabel}
            </button>
          ) : null}
        />

      </div> : <ComputerControlPermissions
        adapter={automationAdapter}
        proposedInvocation={proposedInvocation}
        onAnalyzeCapture={onAnalyzeCapture}
        onOpenBrowser={onOpenBrowser}
      />}

      <p className="plugins-panel__footnote">
        Connected services and computer access remain limited to the permissions shown during setup.
      </p>
    </section>
  );
}

function PluginRow({
  icon: Icon,
  iconTone,
  name,
  description,
  state,
  stateTone = "quiet",
  detail = "",
  action,
}) {
  return (
    <div className="plugin-row" role="listitem">
      <span className={`plugin-row__icon plugin-row__icon--${iconTone}`} aria-hidden="true">
        <Icon />
      </span>
      <div className="plugin-row__copy">
        <div className="plugin-row__title-line">
          <strong>{name}</strong>
          <span className={`plugin-state plugin-state--${stateTone}`}>
            <i aria-hidden="true" />
            {state}
          </span>
        </div>
        <p>{description}</p>
        {detail ? <small>{detail}</small> : null}
      </div>
      {action ? <div className="plugin-row__action">{action}</div> : null}
    </div>
  );
}
