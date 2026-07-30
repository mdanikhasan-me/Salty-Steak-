import { Globe2, X } from "lucide-react";
import { splitAssistantContent } from "../workflows/chatContent.mjs";
import { formatDuration } from "../workflows/formatters.js";
import { CookingGlyph, CookingStatus } from "./CookingStatus.jsx";

export function CookingActivityPanel({
  message,
  active = false,
  mode = "instant",
  operation = null,
  onClose,
}) {
  const preview = active ? (operation?.result?.generation_preview || null) : null;
  const activeContent = active
    ? (preview?.tail_text || "")
    : (message?.content || "");
  const parsedContent = splitAssistantContent(activeContent);
  const previewKind = String(preview?.kind || "").toLowerCase();
  const details = message?.technical_details || message?.details || {};
  const liveDetails = operation?.result || operation?.progress || {};
  const reasoningVisibility = String(
    liveDetails?.reasoning_visibility_effective
      || details?.reasoning_visibility_effective
      || "summaries",
  ).toLowerCase();
  const rawTraceEnabled = reasoningVisibility === "raw_local";
  const reasoning = rawTraceEnabled && mode === "cooking"
    ? active
      ? (previewKind === "reasoning" ? activeContent : "")
      : String(details?.reasoning_text || parsedContent.reasoning || "")
    : "";
  const answerDraft = active && previewKind === "output" ? activeContent : "";
  const journal = activityJournal(operation, details);
  const cookingIncomplete = mode === "cooking" && Boolean(parsedContent.reasoningIncomplete);
  const webSearch = details?.web_search || {};
  const sources = Array.isArray(webSearch.sources) ? webSearch.sources : [];
  const durationSeconds = finiteDurationSeconds(details);
  const activity = activityTelemetry(operation, details);
  const stage = cookingStage(operation?.phase, mode);
  const title = active
    ? (mode === "cooking" ? "Cooking" : "Responding")
    : (mode === "cooking" ? (cookingIncomplete ? "Cooking paused" : "Cooked") : "Response activity");

  return (
    <aside id="cooking-activity-panel" className="cooking-activity" aria-label={`${title} activity`}>
      <header className="cooking-activity__header">
        <div>
          <strong>{title}</strong>
          <span>
            {active
              ? stage
              : cookingIncomplete
                ? "The final answer was not reached before generation stopped."
                : durationSeconds === null
                ? "Response activity"
                : `Completed in ${formatDuration(durationSeconds)}`}
          </span>
        </div>
        <button type="button" aria-label="Close cooking activity" onClick={onClose}>
          <X aria-hidden="true" />
        </button>
      </header>
      <div className="cooking-activity__body">
        {active ? (
          <div className="cooking-activity__live">
            <CookingStatus label={stage} busy />
            <ActivityMetrics items={activity} />
            <ActivityJournal entries={journal} />
            {
                                                                   }
            {answerDraft ? (
              <section className="cooking-activity__trace">
                <div className="cooking-activity__section-title">
                  <CookingGlyph />
                  <span>Answer draft</span>
                </div>
                <p className="cooking-activity__content cooking-activity__draft">{answerDraft}</p>
              </section>
            ) : null}
            {reasoning ? <RawTrace text={reasoning} active /> : null}
            {!journal.length && !preview ? <p>Waiting for model output.</p> : null}
          </div>
        ) : reasoning || sources.length || activity.length || journal.length ? (
          <>
            <ActivityMetrics items={activity} />
            <ActivityJournal entries={journal} />
            {sources.length ? (
              <section className="cooking-activity__research">
                <div className="cooking-activity__section-title">
                  <Globe2 aria-hidden="true" />
                  <span>Searched {sources.length} source{sources.length === 1 ? "" : "s"}</span>
                </div>
                <ol>
                  {sources.map((source) => (
                    <li key={source}><a href={source} target="_blank" rel="noreferrer">{source}</a></li>
                  ))}
                </ol>
              </section>
            ) : null}
            {reasoning ? <RawTrace text={reasoning} /> : null}
          </>
        ) : (
          <p className="cooking-activity__empty">No model-produced reasoning was saved for this response.</p>
        )}
      </div>
    </aside>
  );
}

function ActivityJournal({ entries }) {
  if (!entries.length) return null;
  return (
    <section className="cooking-activity__journal" aria-label="Activity journal">
      <div className="cooking-activity__section-title">
        <CookingGlyph />
        <span>Activity</span>
      </div>
      <ol>
        {entries.map((entry) => (
          <li key={entry.id || `${entry.label}-${entry.sequence}`} data-state={entry.state}>
            <span className="cooking-activity__journal-marker" aria-hidden="true" />
            <div>
              <strong>{entry.label}</strong>
              {entry.detail ? <p>{entry.detail}</p> : null}
              <small>{activityEntryTelemetry(entry)}</small>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function RawTrace({ text, active = false }) {
  return (
    <section className="cooking-activity__trace cooking-activity__trace--raw">
      <div className="cooking-activity__section-title">
        <CookingGlyph />
        <span>Raw local trace (developer)</span>
      </div>
      <p className="cooking-activity__content cooking-activity__raw">{text}</p>
      <small>{`${text.length.toLocaleString()} characters${active ? " so far" : ""}`}</small>
    </section>
  );
}

function activityJournal(operation, details) {
  const candidates = [
    operation?.result?.activity_journal,
    operation?.progress?.activity_journal,
    details?.activity_journal,
    details?.orchestration?.activity_journal,
  ];
  const value = candidates.find(Array.isArray) || [];
  return value
    .filter((entry) => entry && typeof entry === "object")
    .map((entry, index) => ({
      id: String(entry.id || `activity-${index + 1}`),
      sequence: Number(entry.sequence) || index + 1,
      label: String(entry.label || "Working").slice(0, 120),
      detail: String(entry.detail || "").slice(0, 400),
      state: ["running", "completed", "failed"].includes(String(entry.state))
        ? String(entry.state)
        : "completed",
      token_count: Number(entry.token_count),
      character_count: Number(entry.character_count),
    }))
    .sort((left, right) => left.sequence - right.sequence);
}

function activityEntryTelemetry(entry) {
  const parts = [];
  if (Number.isFinite(entry.token_count) && entry.token_count >= 0) {
    parts.push(`${Math.round(entry.token_count).toLocaleString()} tokens`);
  }
  if (Number.isFinite(entry.character_count) && entry.character_count >= 0) {
    parts.push(`${Math.round(entry.character_count).toLocaleString()} characters`);
  }
  if (!parts.length) return entry.state === "running" ? "In progress" : "Complete";
  return `${entry.state === "running" ? "In progress" : "Complete"} · ${parts.join(" · ")}`;
}

function finiteDurationSeconds(details) {
  const secondsValue = details?.generation_duration_seconds;
  if (secondsValue !== null && secondsValue !== undefined && secondsValue !== "") {
    const seconds = Number(secondsValue);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  }
  const millisecondsValue = details?.generation_duration_ms;
  if (millisecondsValue === null || millisecondsValue === undefined || millisecondsValue === "") {
    return null;
  }
  const milliseconds = Number(millisecondsValue);
  return Number.isFinite(milliseconds) && milliseconds >= 0 ? milliseconds / 1000 : null;
}

function cookingStage(phase, mode) {
  const value = String(phase || "").toLowerCase();
  if (value.includes("queue") || value.includes("wait")) return "Waiting to respond";
  if (value.includes("search")) return "Searching sources";
  if (value.includes("load") || value.includes("runtime")) return "Preparing runtime";
  if (value.includes("prepar") || value.includes("prefill") || value.includes("prompt")) return "Preparing response";
  if (value.includes("sav") || value.includes("final")) return "Finishing response";
  if (value.includes("generat") || value.includes("cook")) return mode === "cooking" ? "Cooking" : "Responding";
  if (value.includes("stop") || value.includes("cancel")) return "Stopping response";
  return mode === "cooking" ? "Cooking" : "Responding";
}

function ActivityMetrics({ items }) {
  if (!items.length) return null;
  return (
    <dl className="cooking-activity__metrics">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function activityTelemetry(operation, details) {
  const sources = [operation, operation?.progress, operation?.result, details].filter(Boolean);
  const inputTokens = firstFinite(sources, ["input_context_tokens", "prompt_tokens"]);
  const previewTokens = firstFinite(
    [operation?.result?.generation_preview].filter(Boolean),
    ["token_count"],
  );
  const outputTokens = previewTokens ?? firstFinite(
    sources,
    ["generated_output_tokens", "output_tokens"],
  );
  const outputLimit = firstFinite(sources, ["maximum_output_tokens", "max_output_tokens"]);
  const elapsedSeconds = firstFinite(sources, ["elapsed_seconds", "generation_duration_seconds"])
    ?? millisecondsToSeconds(firstFinite(sources, ["generation_duration_ms"]));
  return [
    inputTokens === null ? null : { label: "Input", value: `${formatCount(inputTokens)} tokens` },
    outputTokens === null ? null : { label: "Output", value: `${formatCount(outputTokens)} tokens` },
    outputTokens !== null || outputLimit === null
      ? null
      : { label: "Output limit", value: `${formatCount(outputLimit)} tokens` },
    elapsedSeconds === null ? null : { label: "Elapsed", value: formatDuration(elapsedSeconds) },
  ].filter(Boolean);
}

function firstFinite(sources, keys) {
  for (const source of sources) {
    for (const key of keys) {
      const rawValue = source?.[key];
      if (rawValue === null || rawValue === undefined || rawValue === "") continue;
      const value = Number(rawValue);
      if (Number.isFinite(value) && value >= 0) return value;
    }
  }
  return null;
}

function millisecondsToSeconds(value) {
  return value === null ? null : value / 1_000;
}

function formatCount(value) {
  return Math.round(value).toLocaleString();
}
