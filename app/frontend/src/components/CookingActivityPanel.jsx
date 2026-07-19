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
  const reasoning = mode === "instant"
    ? ""
    : active
      ? (previewKind === "reasoning" ? activeContent : "")
      : (parsedContent.reasoning || "");
  const cookingIncomplete = mode === "cooking" && Boolean(parsedContent.reasoningIncomplete);
  const details = message?.technical_details || message?.details || {};
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





            {reasoning || activeContent ? (
              <section className="cooking-activity__trace">
                <div className="cooking-activity__section-title">
                  <CookingGlyph />
                  <span>{reasoning ? "Reasoning" : "Preparing answer"}</span>
                </div>
                <p className="cooking-activity__content">
                  {`${(reasoning || activeContent).length.toLocaleString()} characters so far`}
                </p>
              </section>
            ) : (
              <p>Waiting for model output.</p>
            )}
          </div>
        ) : reasoning || sources.length || activity.length ? (
          <>
            <ActivityMetrics items={activity} />
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
            {reasoning ? (
              <section className="cooking-activity__trace">
                <div className="cooking-activity__section-title">
                  <CookingGlyph />
                  <span>Reasoning</span>
                </div>
                <p className="cooking-activity__content">
                  {`${reasoning.length.toLocaleString()} characters, not shown`}
                </p>
              </section>
            ) : null}
          </>
        ) : (
          <p className="cooking-activity__empty">No model-produced reasoning was saved for this response.</p>
        )}
      </div>
    </aside>
  );
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
