export const CONNECTOR_DEFINITIONS = Object.freeze([
  Object.freeze({
    id: "gmail",
    name: "Gmail",
    description: "Search, read, and draft mail through a compatible Gmail MCP service.",
  }),
  Object.freeze({
    id: "google_calendar",
    name: "Google Calendar",
    description: "Read schedules and prepare changes through a compatible Google Calendar MCP service.",
  }),
  Object.freeze({
    id: "icloud_calendar",
    name: "iCloud Calendar",
    description: "Use an iCloud calendar through a compatible CalDAV-to-MCP service.",
  }),
]);

const CONNECTION_STATES = new Set([
  "connected",
  "configured",
  "degraded",
  "error",
  "disabled",
  "connecting",
  "disconnected",
  "attention",
  "unavailable",
]);

export function normaliseConnector(value) {
  const source = typeof value === "string" ? { status: value } : value || {};
  const requested = String(source.status || "disconnected").trim().toLowerCase();
  return {
    ...source,
    status: CONNECTION_STATES.has(requested) ? requested : "disconnected",
  };
}

export function connectorPresentation(value, busy = false) {
  const connector = normaliseConnector(value);
  if (busy || connector.status === "connecting") {
    return {
      stateLabel: "Connecting",
      actionLabel: "Connecting…",
      action: "connect",
      disabled: true,
    };
  }
  if (connector.status === "connected" || connector.status === "configured") {
    return {
      stateLabel: connector.status === "configured" ? "Configured" : "Connected",
      actionLabel: "Manage",
      action: "manage",
      disabled: false,
    };
  }
  if (connector.status === "attention") {
    return {
      stateLabel: "Needs attention",
      actionLabel: "Review",
      action: "manage",
      disabled: false,
    };
  }
  if (connector.status === "degraded" || connector.status === "error") {
    return {
      stateLabel: connector.status === "degraded" ? "Limited" : "Connection failed",
      actionLabel: "Review",
      action: "manage",
      disabled: false,
    };
  }
  if (connector.status === "unavailable" || connector.status === "disabled") {
    return {
      stateLabel: connector.status === "disabled" ? "Disabled" : "Unavailable",
      actionLabel: connector.status === "disabled" ? "Disabled" : "Unavailable",
      action: "none",
      disabled: true,
    };
  }
  return {
    stateLabel: "Not connected",
    actionLabel: "Connect",
    action: "connect",
    disabled: false,
  };
}
