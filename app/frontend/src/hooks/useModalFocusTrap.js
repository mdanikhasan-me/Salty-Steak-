import { useEffect, useRef } from "react";

const FOCUSABLE = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useModalFocusTrap({ active = true, onClose, canClose = true } = {}) {
  const containerRef = useRef(null);
  const closeRef = useRef(onClose);
  const canCloseRef = useRef(canClose);

  useEffect(() => { closeRef.current = onClose; }, [onClose]);
  useEffect(() => { canCloseRef.current = canClose; }, [canClose]);

  useEffect(() => {
    if (!active || !containerRef.current) return undefined;
    const root = containerRef.current;
    const previous = document.activeElement;
    const focusable = () => Array.from(root.querySelectorAll(FOCUSABLE)).filter(
      (node) => {
        if (!node.getClientRects().length || node.closest("[inert]") || getComputedStyle(node).visibility === "hidden") return false;
        for (let parent = node.parentElement; parent && parent !== root; parent = parent.parentElement) {
          if (parent.tagName === "DETAILS" && !parent.open && !parent.querySelector(":scope > summary")?.contains(node)) return false;
        }
        return true;
      },
    );
    // Keep the dialog focusable without drawing a selection box around its
    // first unrelated control when opened with a pointer.
    const initialFocus = window.requestAnimationFrame(() => root.focus({ preventScroll: true }));

    function handleKeyDown(event) {
      if (event.key === "Escape" && canCloseRef.current) {
        event.preventDefault();
        closeRef.current?.();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        root.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (document.activeElement === root || !root.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(initialFocus);
      document.removeEventListener("keydown", handleKeyDown);
      if (previous instanceof HTMLElement && previous.isConnected) {
        window.requestAnimationFrame(() => previous.focus());
      }
    };
  }, [active]);

  return containerRef;
}
