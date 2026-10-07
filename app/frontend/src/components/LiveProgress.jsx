import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { liveProgress } from "../workflows/liveProgress.mjs";
import { formatDuration } from "../workflows/formatters.js";

export function LiveProgress({ operation, compact = false, onOpenActivity, activityOpen = false }) {
  const previous = useRef(null);
  const [now, setNow] = useState(() => Date.now());
  const running = ["queued", "running", "stop_requested"].includes(operation?.state);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running, operation?.id]);
  const progress = useMemo(() => {
    previous.current = liveProgress(operation, previous.current, now);
    return previous.current;
  }, [operation, now]);
  if (!progress) return null;
  const phaseLabel = progress.current.title;
  const focus = progress.focusNote;
  return <section className={`response-work${compact ? " response-work--compact" : ""}${progress.writing ? " response-work--writing" : ""}`} aria-label="Live progress">
    <div className="response-work__heading">
      <div className="response-work__state">
        <span className="response-work__phase">{phaseLabel}</span>
        <span className="response-work__time" aria-label={`Elapsed ${formatDuration(progress.elapsed)}`}>{formatDuration(progress.elapsed)}</span>
      </div>
      {onOpenActivity ? <button type="button" className="response-work__details" onClick={onOpenActivity}
        aria-expanded={activityOpen} aria-controls="cooking-activity-panel" aria-label="Open response activity">
        <SlidersHorizontal aria-hidden="true"/><span>Details</span>
      </button> : null}
    </div>
    {!compact && progress.thinking && focus ? <div className="response-work__note" key={focus.title}>
      <p>{focus.description}</p>
      {progress.reasoningOnly ? <span className="response-work__wait">No answer text yet</span> : null}
    </div> : null}
    {!compact && progress.writing && focus?.specific ? <details className="response-work__approach">
      <summary><ChevronDown aria-hidden="true"/>Earlier focus</summary>
      <p>{focus.description}</p>
      {progress.history.length > 1 ? <ul>{progress.history.filter(item => item.key !== "answer").map(item => <li key={item.key}>{item.title}</li>)}</ul> : null}
    </details> : null}
    {!compact && !progress.thinking && !progress.writing && progress.body ? <p className="response-work__preparing">{progress.body}</p> : null}
    {!compact ? <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{progress.current.title}</span> : null}
  </section>;
}
