import { useEffect, useRef } from "react";
import { RotateCcw } from "lucide-react";
import { useState } from "react";
import "../styles/response-mode-card.css";

function TurtleMark() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 14.5a6.5 6.5 0 0 1 13 0Z" fill="currentColor" fillOpacity=".12"/>
    <path d="m17 12.8 1.5-2a2 2 0 1 1 2.8 2.8L19 15H5l-2-1.5M6.5 15v2m7.5-2v2"/>
  </svg>;
}

export function ResponseModeControl({ value, modelLabel, onChange, turtle = false, onTurtleChange, autoFocus = false }) {
  const slider = useRef(null);
  const [pointerFocus, setPointerFocus] = useState(false);
  const cooking = value === "cooking";
  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => slider.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);
  return <div id="composer-cooking-menu" className={`response-mode-control ${cooking ? "response-mode-control--cooking" : ""} ${turtle ? "response-mode-control--turtle" : ""}`} role="dialog" aria-label="Response mode">
    <header>
      <span className="response-mode-control__turtle-slot" data-pointer-focus={pointerFocus}>
        <button className="response-mode-control__turtle" type="button" aria-label="Turtle mode" aria-pressed={turtle} aria-describedby="turtle-mode-tip" onPointerDown={()=>setPointerFocus(true)} onBlur={()=>setPointerFocus(false)} onKeyDown={()=>setPointerFocus(false)} onClick={()=>onTurtleChange?.(!turtle)}><TurtleMark/></button>
        <span id="turtle-mode-tip" role="tooltip" className="response-mode-control__tip"><strong>{turtle ? "Turtle is on" : "Turtle mode"}</strong><span>Slower generation · lower activity</span><small>GPU use varies. Model memory stays loaded.</small></span>
      </span>
      <span><strong>{cooking ? "Cooking" : "Instant"}</strong><small>{modelLabel}</small></span>
      <button type="button" title="Reset to Instant" aria-label="Reset response mode to Instant" disabled={!cooking} onClick={()=>onChange("instant")}><RotateCcw aria-hidden="true" /></button>
    </header>
    <div className="response-mode-control__track" data-value={cooking ? 1 : 0}>
      <span className="response-mode-control__fill" aria-hidden="true"/>
      <span className="response-mode-control__particles" aria-hidden="true">{Array.from({length: 7},(_,index)=><i key={index} style={{"--particle-index":index, top: `${5 + (index % 3) * 6}px`}}/>)}</span>
      <span className="response-mode-control__stop response-mode-control__stop--start" aria-hidden="true"/>
      <span className="response-mode-control__stop response-mode-control__stop--end" aria-hidden="true"/>
      <input ref={slider} type="range" min="0" max="1" step="1" value={cooking ? 1 : 0} aria-label="Response mode" aria-valuetext={cooking ? "Cooking: more time to reason" : "Instant: quick replies"} onChange={event=>onChange(Number(event.target.value) ? "cooking" : "instant")} />
    </div>
  </div>;
}
