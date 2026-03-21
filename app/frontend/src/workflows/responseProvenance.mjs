












const COMPLETION_WORDS = {
  done: "Done in",
  cooked: "Cooked in",
  stopped: "Stopped after",
  failed: "Failed after",
  partial: "Partial ·",
  waiting: "Waiting",
};


export function formatElapsed(milliseconds) {

  if (milliseconds === null || milliseconds === undefined) return "";
  const value = Number(milliseconds);
  if (!Number.isFinite(value) || value < 0) return "";
  const seconds = value / 1000;
  if (seconds < 1) return `${Math.max(0.1, seconds).toFixed(1)}s`;
  if (seconds < 60) {
    return seconds < 10
      ? `${seconds.toFixed(1).replace(/\.0$/, "")}s`
      : `${Math.round(seconds)}s`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest === 60
    ? `${minutes + 1}m 00s`
    : `${minutes}m ${String(rest).padStart(2, "0")}s`;
}


export function turnDuration(details) {
  const total = Number(details?.turn_duration_ms);
  if (Number.isFinite(total) && total >= 0) return total;
  const decode = Number(details?.generation_duration_seconds);
  if (Number.isFinite(decode) && decode >= 0) return decode * 1000;
  const legacy = Number(details?.generation_duration_ms);
  return Number.isFinite(legacy) && legacy >= 0 ? legacy : null;
}






export function completionState(details) {
  const stored = String(details?.turn_completion || "").toLowerCase();
  if (stored) return stored;
  if (String(details?.generation_state || "") === "stopped") return "stopped";
  if (String(details?.finish_reason || "") === "maximum_output") return "partial";
  const mode = String(details?.reasoning_mode_effective || "").toLowerCase();
  return mode === "cooking" ? "cooked" : "done";
}


export function completionLabel(details) {
  const state = completionState(details);
  const word = COMPLETION_WORDS[state];
  if (!word) return "";
  if (state === "waiting") return word;
  const elapsed = formatElapsed(turnDuration(details));
  return elapsed ? `${word} ${elapsed}` : word;
}


export function workingLabel(mode, milliseconds) {
  const cooking = String(mode || "").toLowerCase() === "cooking";
  const word = cooking ? "Cooking…" : "Working…";
  const elapsed = formatElapsed(milliseconds);
  return elapsed ? `${word} · ${elapsed}` : word;
}

function orchestrationOf(details) {
  return details?.orchestration || {};
}


export function sourcesOf(details) {
  const seen = new Set();
  const sources = [];
  for (const entry of orchestrationOf(details).sources || []) {
    const url = String(entry?.url || "").trim();
    if (!url || seen.has(url)) continue;
    seen.add(url);
    let host = "";
    try {
      host = new URL(url).hostname.replace(/^www\./, "");
    } catch {
      host = "";
    }
    sources.push({
      id: String(entry?.source || url),
      url,
      host,
      title: String(entry?.title || "").trim() || host || url,
      retrievedAt: Number(entry?.retrieved_at) || null,
    });
  }
  return sources;
}





export function supportFor(details, sourceId) {
  const claims = orchestrationOf(details).claims || [];
  return claims
    .filter((claim) => (claim?.sources || []).some((id) => String(id) === String(sourceId)))
    .map((claim) => String(claim?.text || "").trim())
    .filter(Boolean);
}


export function memoriesOf(details) {
  const used = orchestrationOf(details).memory || details?.memory_used || [];
  return (Array.isArray(used) ? used : [])
    .map((entry) => String(entry?.text || entry || "").trim())
    .filter(Boolean);
}









export function activityOf(details) {
  const orchestration = orchestrationOf(details);
  const research = orchestration.research || {};
  const events = [];

  for (const query of research.queries || []) {
    const text = String(query || "").trim();
    if (text) events.push({ kind: "search", text: `Searched for “${text}”` });
  }

  const sources = sourcesOf(details);
  if (sources.length) {
    events.push({
      kind: "read",
      text: `Read ${sources.length} ${sources.length === 1 ? "source" : "sources"}`,
      sources,
    });
  }

  const disputed = Number(research.disputed) || 0;
  const corroborated = Number(research.corroborated) || 0;
  if (corroborated) {
    events.push({
      kind: "compare",
      text: `Compared evidence · ${corroborated} ${
        corroborated === 1 ? "finding agreed" : "findings agreed"
      } across sources`,
    });
  }
  if (disputed) {
    events.push({
      kind: "conflict",
      text: `${disputed} ${disputed === 1 ? "finding" : "findings"} disagreed between sources`,
    });
  }
  if (String(research.stop_reason || "") === "source_budget") {
    events.push({
      kind: "note",
      text: "Retrieval stopped at the configured source budget",
    });
  }

  for (const step of orchestration.steps || []) {
    const action = String(step?.action || "");
    if (!action || action === "respond") continue;
    events.push({
      kind: "tool",
      text: String(step?.reason || action).slice(0, 160),
      tool: action,
      durationMs: Number(step?.duration_ms) || null,
    });
  }
  for (const node of orchestration.plan?.nodes || []) {
    const target = node?.capability || node?.connector;
    if (!target) continue;
    events.push({
      kind: "tool",
      text: String(node?.objective || target).slice(0, 160),
      tool: String(target),
      durationMs: Number(node?.duration_ms) || null,
    });
  }

  if (research.source_count || orchestration.steps?.length || orchestration.plan) {
    const label = completionLabel(details);
    if (label) events.push({ kind: "finished", text: label });
  }
  return events;
}





export function responseDetails(details) {
  const sources = sourcesOf(details);
  const memories = memoriesOf(details);
  const activity = activityOf(details);
  return {
    completion: completionLabel(details),
    state: completionState(details),
    durationMs: turnDuration(details),
    sources,
    memories,
    activity,
    hasDetails: Boolean(activity.length || sources.length || memories.length),
  };
}
