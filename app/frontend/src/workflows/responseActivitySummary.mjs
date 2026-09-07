const INTERNAL = /routing|runtime|prefill|prompt|token|native|input read|model input|prepar/i;
export function activitySummary(entries, { active = false, stopped = false, failed = false } = {}) {
  const current = entries.findLast((entry) => entry.state === "running");
  const failedEntry = entries.find((entry) => entry.state === "failed");
  const state = failed || failedEntry ? "failed" : stopped ? "stopped" : active ? "running" : "completed";
  const label = state === "failed" ? "Could not finish" : state === "stopped" ? "Stopped" : active ? current?.label || "Working on your response" : "Response ready";
  const steps = entries.filter((entry) => entry.label !== label && (!INTERNAL.test(`${entry.kind} ${entry.label}`) || entry.state === "failed"));
  return { state, label, current, steps: steps.slice(-6), failure: failedEntry?.detail || "" };
}
