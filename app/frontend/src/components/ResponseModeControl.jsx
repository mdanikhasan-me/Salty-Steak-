import { useEffect, useRef } from "react";
import { Check } from "lucide-react";

const MODES = [
  { id: "instant", label: "Instant", description: "Quick replies" },
  { id: "cooking", label: "Cooking", description: "More time to reason" },
];

export function ResponseModeControl({ value, onChange, autoFocus = false }) {
  const menu = useRef(null);

  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => {
      menu.current?.querySelector('[aria-checked="true"]')?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);

  function navigate(event) {
    const buttons = [...menu.current.querySelectorAll('[role="menuitemradio"]')];
    const current = buttons.indexOf(document.activeElement);
    const direction = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!direction && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    event.stopPropagation();
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (current + direction + buttons.length) % buttons.length;
    buttons[next]?.focus();
  }

  return (
    <div ref={menu} id="composer-cooking-menu" className="response-mode-control" role="menu" aria-label="Response mode" onKeyDown={navigate}>
      <p className="response-mode-control__heading">Response mode</p>
      <div className="response-mode-control__choices">
        {MODES.map(mode => (
          <button key={mode.id} type="button" role="menuitemradio" aria-checked={value === mode.id}
            tabIndex={value === mode.id ? 0 : -1} onClick={() => onChange(mode.id)}>
            <span className="response-mode-control__label">{mode.label}{value === mode.id ? <Check aria-hidden="true" /> : null}</span>
            <small>{mode.description}</small>
          </button>
        ))}
      </div>
    </div>
  );
}
