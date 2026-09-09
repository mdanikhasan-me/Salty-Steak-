import { X } from "lucide-react";
import { useId } from "react";
import { createPortal } from "react-dom";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";

export function Dialog({ open, title, description, onClose, children, wide = false, compact = false, className = "" }) {
  const titleId = useId();
  const dialogRef = useModalFocusTrap({ active: open, onClose, initialFocusSelector: compact ? "[data-dialog-cancel]" : undefined });

  if (!open) return null;

  return createPortal((
    <div
      className="dialog-layer"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={`dialog ${wide ? "dialog--wide" : ""} ${compact ? "dialog--confirmation" : ""} ${className}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex="-1"
      >
        <header className="dialog__header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        </header>
        <div className="dialog__body">{children}</div>
      </section>
    </div>
  ), document.body);
}
