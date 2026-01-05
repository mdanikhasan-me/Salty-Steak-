import { ChevronRight } from "lucide-react";

export function CookingGlyph() {
  return (
    <svg
      className="cooking-glyph"
      viewBox="0 0 20 20"
      aria-hidden="true"
    >
      <path d="M10 1.5v4M10 14.5v4M1.5 10h4M14.5 10h4M4 4l2.8 2.8m6.4 6.4L16 16M16 4l-2.8 2.8m-6.4 6.4L4 16" />
    </svg>
  );
}

export function CookingStatus({
  label = "Cooking",
  active = false,
  busy = false,
  controls,
  onClick,
  complete = false,
}) {
  const content = (
    <>
      <CookingGlyph />
      <span
        className={busy ? "activity-phrase activity-phrase--response" : undefined}
        aria-hidden={onClick && busy ? "true" : undefined}
      >
        {label}
      </span>
      {onClick ? <ChevronRight className="cooking-status__chevron" aria-hidden="true" /> : null}
    </>
  );

  if (!onClick) {
    return (
      <div
        className="cooking-status cooking-status--busy"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {content}
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        className={`cooking-status ${active ? "cooking-status--open" : ""} ${
          complete ? "cooking-status--complete" : ""
        }`}
        aria-expanded={active}
        aria-controls={controls}
        onClick={onClick}
      >
        {content}
      </button>
      {busy ? (
        <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {label}
        </span>
      ) : null}
    </>
  );
}
