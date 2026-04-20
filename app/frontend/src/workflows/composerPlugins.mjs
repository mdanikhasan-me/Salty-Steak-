














const CONNECTED = "connected";
const NOT_CONNECTED = "not_connected";


export function connectionLabel(app) {
  const state = String(app?.state || NOT_CONNECTED);
  if (app?.last_error) return "Attention";
  if (state === CONNECTED) return "Connected";
  if (state === "expired" || state === "reauthorize") return "Reauthorize";
  return "Connect";
}


export function connectionAction(app) {
  return String(app?.state || NOT_CONNECTED) === CONNECTED && !app?.last_error
    ? "manage"
    : "connect";
}









export function composerPickerSections(pluginState) {
  const tools = (pluginState?.tools || pluginState?.plugins || []).filter(
    (tool) => tool && tool.id,
  );
  const apps = (pluginState?.connected_apps || []).filter(
    (app) => app && app.id && app.name,
  );

  const sections = [];
  if (apps.length) {
    sections.push({
      id: "connected_apps",
      title: "Connected apps",
      description: "Services with an account you authorise",
      rows: apps.map((app) => ({
        id: String(app.id),
        kind: "connected_app",
        name: String(app.name),


        detail: String(app.account || app.description || ""),
        state: connectionLabel(app),
        action: connectionAction(app),
        attention: Boolean(app.last_error),
        provider: String(app.provider || app.id),
      })),
    });
  }
  if (tools.length) {
    sections.push({
      id: "tools",
      title: "Tools",
      description: "Built into this build of Salty Steak",
      rows: tools.map((tool) => {
        const ready = tool.availability === "available";
        return {
          id: String(tool.id),
          kind: "tool",
          name: String(tool.name || tool.id),
          detail: String(
            (ready ? tool.description : tool.unavailable_reason || tool.description) || "",
          ),
          state: ready ? "Available" : "Unavailable",
          action: "manage",
          disabled: !ready,
          category: String(tool.category || ""),
        };
      }),
    });
  }
  return sections;
}

export default composerPickerSections;
