import { useEffect, useRef } from "react";
import { Check, CircleAlert, Square, ChevronRight, Globe2, X } from "lucide-react";
import { splitAssistantContent } from "../workflows/chatContent.mjs";
import {
  activityEntryTelemetry,
  currentActivityEntry,
  normaliseActivityJournal,
} from "../workflows/activityJournal.mjs";
import { formatDuration } from "../workflows/formatters.js";
import { activitySummary } from "../workflows/responseActivitySummary.mjs";
import { completionState } from "../workflows/responseProvenance.mjs";
import { CookingGlyph, CookingStatus } from "./CookingStatus.jsx";

export function CookingActivityPanel({
  message,
  active = false,
  mode = "instant",
  operation = null,
  onClose,
  onStop,
  stopBusy = false,
}) {
  const preview = active
    ? (operation?.result?.generation_preview || operation?.progress?.generation_preview || null)
    : null;
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
  const journal = normaliseActivityJournal(operation, details, { active });
  const researchActivity = Boolean(
    liveDetails?.research_progress
      || details?.research_progress
      || details?.orchestration?.kind === "research"
      || details?.orchestration?.research,
  );
  const cookingIncomplete = mode === "cooking" && Boolean(parsedContent.reasoningIncomplete);
  const webSearch = details?.web_search || {};
  const sources = Array.isArray(webSearch.sources) ? webSearch.sources : [];
  const durationSeconds = finiteDurationSeconds(details);
  const activity = activityTelemetry(operation, details);
  const stage = cookingStage(operation?.phase, mode);
  const title = active
    ? (researchActivity ? "Researching" : mode === "cooking" ? "Cooking" : "Responding")
    : (researchActivity
      ? "Research activity"
      : mode === "cooking"
        ? (cookingIncomplete ? "Cooking paused" : "Cooked")
        : "Response activity");
  const panelLabel = title.toLowerCase().endsWith("activity")
    ? title
    : `${title} activity`;

  const completion = completionState(details);
  const summary = activitySummary(journal, { active, stopped: ["stopped","interrupted","cancelled"].includes(operation?.state) || completion === "stopped" || cookingIncomplete, failed: operation?.state === "failed" || completion === "failed" });
  return (
    <aside id="cooking-activity-panel" className="cooking-activity" aria-label={panelLabel}>
      <header className="cooking-activity__header"><div><strong>Activity</strong><span>{summary.state === "running" ? "In progress" : summary.state === "failed" ? "Needs attention" : summary.state === "stopped" ? "Stopped" : durationSeconds === null ? "This response" : `Finished in ${formatDuration(durationSeconds)}`}</span></div><button type="button" aria-label="Close cooking activity" onClick={onClose}><X aria-hidden="true" /></button></header>
      <div className="cooking-activity__body">
        <section className="activity-overview">
          <div className="activity-overview__status" data-state={summary.state}>{summary.state === "failed" ? <CircleAlert aria-hidden="true" /> : summary.state === "running" ? <CookingGlyph /> : summary.state === "stopped" ? <Square aria-hidden="true" /> : <Check aria-hidden="true" />}<span>{active ? stage : summary.label}</span></div>
          {summary.failure ? <p role="alert">{summary.failure}</p> : null}
          {cookingIncomplete ? <p>The response stopped before a final answer was ready.</p> : null}
          {active && onStop ? <div className="activity-overview__actions"><button type="button" onClick={onStop} disabled={stopBusy}><Square aria-hidden="true" />{stopBusy ? "Stopping\u2026" : "Stop response"}</button></div> : null}
        </section>
        {summary.steps.length ? <ol className="activity-history" aria-label="Progress summary">{summary.steps.map((entry) => <li key={entry.id}>{entry.state === "failed" ? <CircleAlert /> : entry.state === "completed" ? <Check /> : <ChevronRight />}<div><strong>{entry.label}</strong><small>{entry.state === "completed" ? "Complete" : entry.state === "failed" ? "Failed" : entry.state === "running" ? "In progress" : entry.state === "skipped" ? "Not needed" : "Planned"}</small></div></li>)}</ol> : null}
        {sources.length ? <section className="cooking-activity__research"><h3 className="cooking-activity__section-title"><Globe2 />Sources</h3>{sources.map((source,index) => { const url = typeof source === "string" ? source : source.url; return url && /^https?:\/\//i.test(url) ? <a className="activity-source" key={url+index} href={url} target="_blank" rel="noreferrer">{source.title || new URL(url).hostname}</a> : null; })}</section> : null}
        <details className="activity-diagnostics"><summary>Technical details &middot; {journal.length} events</summary><ActivityMetrics items={activity} /><ActivityJournal entries={journal} active={false} />{reasoning ? <RawTrace text={reasoning} /> : null}</details>
      </div>
    </aside>
  );
}

function ActivityJournal({ entries, active = false }) {
  const current = currentActivityEntry(entries);
  const currentRef = useRef(null);
  const finished = entries.filter((entry) => ["completed", "skipped", "failed"].includes(entry.state)).length;
  const history = active && current
    ? entries.filter((entry) => entry.id !== current.id)
    : entries;

  useEffect(() => {
    if (!active || !current?.id || !currentRef.current) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    currentRef.current.scrollIntoView({
      block: "nearest",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [active, current?.id]);

  if (!entries.length) return null;
  return (
    <section className="cooking-activity__journal" aria-label="Activity journal">
      <div className="cooking-activity__section-title">
        <CookingGlyph />
        <span>{active ? "Live activity" : "Activity"}</span>
        <span className="cooking-activity__step-count">
          {active
            ? `${finished} finished / ${current ? "1 live" : "waiting"}`
            : `${entries.length} event${entries.length === 1 ? "" : "s"}`}
        </span>
      </div>
      {active && current ? (
        <div
          className="cooking-activity__current"
          ref={currentRef}
          data-kind={current.kind}
          aria-live="polite"
          aria-atomic="true"
        >
          <span>Now</span>
          <strong>{current.label}</strong>
          {current.detail ? <p>{current.detail}</p> : null}
          <small>{activityEntryTelemetry(current)}</small>
        </div>
      ) : null}
      {history.length ? <ol>
        {history.map((entry) => (
          <li
            key={entry.id || `${entry.label}-${entry.sequence}`}
            data-state={entry.state}
            data-kind={entry.kind}
          >
            <span className="cooking-activity__journal-marker" aria-hidden="true" />
            <div>
              <strong>{entry.label}</strong>
              {entry.detail ? <p>{entry.detail}</p> : null}
              <small>{activityEntryTelemetry(entry)}</small>
            </div>
          </li>
        ))}
      </ol> : null}
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
  if (value.includes("read")) return "Reading sources";
  if (value.includes("validat") || value.includes("reject")) return "Validating evidence";
  if (value.includes("compar")) return "Comparing evidence";
  if (value.includes("source") && value.includes("gather")) return "Sources gathered";
  if (value.includes("synth")) return "Synthesizing evidence";
  if (value.includes("writ") || value.includes("draft")) return "Writing the answer";
  if (value.includes("verif") || value.includes("citation")) return "Verifying the answer";
  if (value.includes("research complete")) return "Research complete";
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
    <dl className="cooking-activity__metrics" aria-label="Response telemetry">
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
    [
      operation?.result?.generation_preview,
      operation?.progress?.generation_preview,
    ].filter(Boolean),
    ["token_count"],
  );
  const outputTokens = previewTokens ?? firstFinite(
    sources,
    [
      "total_streamed_output_tokens",
      "visible_output_tokens",
      "generated_output_tokens",
      "output_tokens",
    ],
  );
  const outputLimit = firstFinite(sources, ["maximum_output_tokens", "max_output_tokens"]);
  const elapsedSeconds = firstFinite(sources, ["elapsed_seconds", "generation_duration_seconds"])
    ?? millisecondsToSeconds(firstFinite(sources, ["generation_duration_ms"]));
  return [
    inputTokens === null ? null : { label: "Input", value: formatTokenCount(inputTokens) },
    outputTokens === null ? null : { label: "Output", value: formatTokenCount(outputTokens) },
    outputTokens !== null || outputLimit === null
      ? null
      : { label: "Output limit", value: formatTokenCount(outputLimit) },
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

function formatTokenCount(value) {
  const count = Math.round(value);
  return `${formatCount(count)} token${count === 1 ? "" : "s"}`;
}
