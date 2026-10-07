import { useEffect, useRef } from "react";
import { ChevronRight, RotateCcw } from "lucide-react";
import { useState } from "react";
import "../styles/response-mode-card.css";

function TurtleMark() {
  return <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M3.5 14.5c0-4.8 2.8-8 7-8s7 3.2 7 8H3.5Z"/>
    <path d="m16 12 2-2.8a2.5 2.5 0 1 1 3.7 3.3l-3.1 3H5.1l-2.7-1.2a.8.8 0 0 1 .5-1.5l1.8.4M6 14h2.5v3a1.25 1.25 0 0 1-2.5 0zm7 0h2.5v3a1.25 1.25 0 0 1-2.5 0z"/>
  </svg>;
}

export function ResponseModeControl({ value, modelLabel, onChange, turtle = false, onTurtleChange, autoFocus = false }) {
  const slider = useRef(null);
  const [pointerFocus, setPointerFocus] = useState(false);
  const modes = ["instant", "cooking", "lock_in"];
  const labels = ["Blink", "Cook", "Lock In"];
  const index = Math.max(0, modes.indexOf(value));
  const cooking = index > 0;
  const particleCount = index === 2 ? 22 : 0;
  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => slider.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);
  return <div id="composer-cooking-menu" data-mode-index={index} className={`response-mode-control ${cooking ? "response-mode-control--cooking" : ""} ${turtle ? "response-mode-control--turtle" : ""}`} role="dialog" aria-label="Response mode">
    <header>
      <span className="response-mode-control__turtle-slot" data-pointer-focus={pointerFocus}>
        <button className="response-mode-control__turtle" type="button" aria-label="Turtle mode" aria-pressed={turtle} aria-describedby="turtle-mode-tip" onPointerDown={()=>setPointerFocus(true)} onBlur={()=>setPointerFocus(false)} onKeyDown={()=>setPointerFocus(false)} onClick={()=>onTurtleChange?.(!turtle)}><TurtleMark/></button>
        <span id="turtle-mode-tip" role="tooltip" className="response-mode-control__tip"><strong>{turtle ? "Turtle is on" : "Turtle mode"}</strong><span>Slower generation · lower activity</span><small>GPU use varies. Model memory stays loaded.</small></span>
      </span>
      <span><strong className="response-mode-control__title">{labels[index]}<ChevronRight aria-hidden="true"/></strong><small>{modelLabel}</small></span>
      <button type="button" title="Reset to Blink" aria-label="Reset response mode to Blink" disabled={!cooking} onClick={()=>onChange("instant")}><RotateCcw aria-hidden="true" /></button>
    </header>
    <div className="response-mode-control__track" data-value={index}>
      <span className="response-mode-control__fill" aria-hidden="true"/>
      {particleCount ? <span className="response-mode-control__particles" aria-hidden="true">{Array.from({length:particleCount},(_,i)=><i key={i} style={{"--particle-index":i,"--particle-count":particleCount,top:`${3+((i*7)%18)}px`,width:i%6===0?'2px':'1.2px',height:i%6===0?'2px':'1.2px'}}/>)}</span> : null}
      <span className="response-mode-control__stop response-mode-control__stop--start" aria-hidden="true"/>
      <span className="response-mode-control__stop response-mode-control__stop--middle" aria-hidden="true"/>
      <span className="response-mode-control__stop response-mode-control__stop--end" aria-hidden="true"/>
      <input ref={slider} type="range" min="0" max="2" step="1" value={index} aria-label="Response mode" aria-valuetext={labels[index]} onChange={event=>onChange(modes[Number(event.target.value)])} />
    </div>
  </div>;
}
