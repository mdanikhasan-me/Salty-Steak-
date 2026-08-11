














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
  sections.push({
    id: "workspace",
    title: "Workspace",
    description: "Controls for your local assistant",
    rows: [
      {
        id: "memory",
        kind: "memory",
        name: "Memory",
        detail: "Review or delete the context you explicitly saved",
        state: "Manage",
        action: "manage",
        disabled: false,
        category: "built_in_control",
      },
    ],
  });
  return sections;
}

export default composerPickerSections;
