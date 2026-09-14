import { useEffect, useRef } from "react";
import { RotateCcw } from "lucide-react";
import "../styles/response-mode-card.css";

export function ResponseModeControl({ value, modelLabel, onChange, autoFocus = false }) {
  const slider = useRef(null);
  const cooking = value === "cooking";
  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => slider.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);
  return <div id="composer-cooking-menu" className={`response-mode-control ${cooking ? "response-mode-control--cooking" : ""}`} role="dialog" aria-label="Response mode">
    <header>
      <span><strong>{cooking ? "Cooking" : "Instant"}</strong><small>{modelLabel}</small></span>
      <button type="button" title="Reset to Instant" aria-label="Reset response mode to Instant" disabled={!cooking} onClick={()=>onChange("instant")}><RotateCcw aria-hidden="true" /></button>
    </header>
    <div className="response-mode-control__track" data-value={cooking ? 1 : 0}>
      <span className="response-mode-control__fill" aria-hidden="true"/>
      <span className="response-mode-control__stop response-mode-control__stop--start" aria-hidden="true"/>
      <span className="response-mode-control__stop response-mode-control__stop--end" aria-hidden="true"/>
      <input ref={slider} type="range" min="0" max="1" step="1" value={cooking ? 1 : 0} aria-label="Response mode" aria-valuetext={cooking ? "Cooking: more time to reason" : "Instant: quick replies"} onChange={event=>onChange(Number(event.target.value) ? "cooking" : "instant")} />
    </div>
  </div>;
}
