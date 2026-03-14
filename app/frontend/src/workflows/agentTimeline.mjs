









const TOOL_PRESENTATION = {
  "terminal.execute": { label: "Terminal", icon: "terminal" },
  "browser.control": { label: "Browser", icon: "globe" },
  "screen.capture": { label: "Screenshot", icon: "camera" },
  "application.launch": { label: "Application", icon: "app" },
  "window.control": { label: "Window", icon: "window" },
  "ui.automation": { label: "Interface", icon: "pointer" },
  "input.control": { label: "Input", icon: "keyboard" },
  respond: { label: "Result", icon: "check" },
  research: { label: "Research", icon: "search" },
  plan: { label: "Plan", icon: "list" },
};

export function toolPresentation(action) {
  const key = String(action || "");
  return TOOL_PRESENTATION[key] || { label: key || "Step", icon: "dot" };
}


export function eventState(event) {
  const status = String(event?.status || "").toLowerCase();
  if (status === "succeeded" || status === "completed") return "completed";
  if (status === "already_satisfied") return "skipped";
  if (status === "failed" || status === "revoked") return "failed";
  if (status === "blocked" || status === "needs_review") return "waiting";
  if (!status) return "running";
  return "completed";
}


export function eventSummary(event) {
  const action = String(event?.action || "");
  const args = event?.arguments || {};
  const observation = event?.observation || {};

  if (action === "browser.control") {
    const command = String(args.command || "");
    if (command === "open_url" || command === "navigate") return `Open ${args.url || "page"}`;
    if (command === "show_window") return "Bring the browser onto the screen";
    if (command === "read_page") return "Read the page";
    if (command === "query" || command === "find_element") return "Find elements on the page";
    if (command === "click") return "Click an element";
    if (command === "set_value") return "Type into a field";
    return command ? `Browser ${command.replace(/_/g, " ")}` : "Browser";
  }
  if (action === "application.launch") return `Open ${args.target || observation.target || "application"}`;
  if (action === "terminal.execute") {
    const argv = Array.isArray(args.argv) ? args.argv.join(" ") : "";
    return argv ? `Run ${argv}` : "Run a command";
  }
  if (action === "screen.capture") return "Capture the current display";
  if (action === "window.control") {
    const act = String(args.action || "");
    if (act === "list") return "List open windows";
    return `${act === "close" ? "Close" : "Focus"} ${args.title || "window"}`;
  }
  if (action === "ui.automation") return `Interface ${String(args.command || "").replace(/_/g, " ")}`.trim();
  if (action === "input.control") return `Send ${String(args.action || "input").replace(/_/g, " ")}`;
  if (action === "respond") return "Report the result";


  return String(event?.reason || "").slice(0, 140) || "Step";
}


export function eventDetail(event) {
  const args = event?.arguments || {};
  const observation = { ...(event?.observation || {}) };
  delete observation.artifact;
  const input = Object.keys(args).length ? JSON.stringify(args, null, 2) : "";
  const output = Object.keys(observation).length ? JSON.stringify(observation, null, 2) : "";
  return { input, output };
}


export function eventArtifact(event) {
  const artifact = event?.observation?.artifact;
  if (!artifact) return null;
  return {
    path: String(artifact.path || ""),
    width: Number(artifact.width) || null,
    height: Number(artifact.height) || null,
  };
}

export function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value < 1000) return `${Math.round(value)}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}





export function eventsFromPlanNodes(nodes) {
  return (nodes || [])
    .filter((node) => node && (node.capability || node.connector))
    .map((node, index) => ({
      step: index + 1,
      action: node.capability || node.connector,
      reason: node.objective || "",
      arguments: node.arguments || {},
      status:
        node.state === "completed"
          ? "succeeded"
          : node.state === "failed"
            ? "failed"
            : node.state === "ready" || node.state === "pending"
              ? ""
              : node.state,
      observation: node.failure ? { error: node.failure } : {},
      duration_ms: node.duration_ms,
    }));
}





export function timelineFor({ liveDetails, messageDetails }) {
  const live = liveDetails?.agent_events;
  if (Array.isArray(live) && live.length) return live;
  const orchestration = messageDetails?.orchestration || {};
  if (Array.isArray(orchestration.steps) && orchestration.steps.length) {
    return orchestration.steps;
  }
  const nodes = orchestration.plan?.nodes;
  if (Array.isArray(nodes) && nodes.length) return eventsFromPlanNodes(nodes);
  return [];
}


export function executionEvents(events) {
  return (events || []).filter((event) => String(event?.action || "") !== "respond");
}
