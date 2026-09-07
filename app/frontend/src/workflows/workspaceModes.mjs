export const WORKSPACE_MODES = Object.freeze([
  { id: "agent", label: "Agent", heading: "What would you like to get done?", placeholder: "Give Salty Steak a task…" },
  { id: "chat", label: "Chat", heading: "What would you like to work on?", placeholder: "Message Salty Steak…" },
  { id: "code", label: "Code", heading: "What are we building?", placeholder: "Describe a change, or attach your code…" },
]);

export function normaliseWorkspaceMode(value) {
  return WORKSPACE_MODES.some((mode) => mode.id === value) ? value : "chat";
}

export function workspaceModeDetails(value) {
  return WORKSPACE_MODES.find((mode) => mode.id === normaliseWorkspaceMode(value));
}
