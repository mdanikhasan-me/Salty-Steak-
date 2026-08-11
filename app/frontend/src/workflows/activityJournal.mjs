const ACTIVITY_STATES = new Set([
  "pending",
  "running",
  "completed",
  "skipped",
  "failed",
]);







export function normaliseActivityJournal(
  operation,
  details,
  { active = false } = {},
) {
  const live = [
    operation?.result?.activity_journal,
    operation?.progress?.activity_journal,
  ];
  const saved = [
    details?.orchestration?.activity_journal,
    details?.activity_journal,
  ];
  const candidates = active ? [...live, ...saved] : [...saved, ...live];
  const value = candidates.find(Array.isArray) || [];
  return value
    .filter((entry) => entry && typeof entry === "object")
    .map((entry, index) => {
      const state = String(entry.state || "completed");
      return {
        id: String(entry.id || `activity-${index + 1}`),
        kind: String(entry.kind || "thinking"),
        sequence: Number(entry.sequence) || index + 1,
        label: String(entry.label || "Working").slice(0, 120),
        detail: String(entry.detail || "").slice(0, 400),
        state: ACTIVITY_STATES.has(state) ? state : "completed",
        token_count: finiteMetric(entry.token_count),
        character_count: finiteMetric(entry.character_count),
        started_elapsed_ms: finiteMetric(entry.started_elapsed_ms),
        updated_elapsed_ms: finiteMetric(entry.updated_elapsed_ms),
      };
    })
    .sort((left, right) => left.sequence - right.sequence);
}

export function currentActivityEntry(entries) {
  return [...(entries || [])].reverse().find((entry) => entry.state === "running") || null;
}

export function activityEntryTelemetry(entry) {
  const parts = [];
  if (entry.token_count !== null) {
    parts.push(`${Math.round(entry.token_count).toLocaleString()} tokens`);
  }
  if (entry.character_count !== null) {
    parts.push(`${Math.round(entry.character_count).toLocaleString()} characters`);
  }
  if (Number.isFinite(entry.updated_elapsed_ms)) {
    const elapsed = Math.max(
      0,
      entry.updated_elapsed_ms - (entry.started_elapsed_ms ?? entry.updated_elapsed_ms),
    );
    if (elapsed >= 100) parts.push(formatElapsedMilliseconds(elapsed));
  }
  const state = {
    pending: "Planned",
    running: "In progress",
    completed: "Complete",
    skipped: "Not needed",
    failed: "Failed",
  }[entry.state] || "Complete";
  return parts.length ? `${state} · ${parts.join(" · ")}` : state;
}

function formatElapsedMilliseconds(value) {
  const seconds = value / 1_000;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function finiteMetric(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

export default normaliseActivityJournal;
