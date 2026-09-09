import { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Keep the popup outside the history's scrolling clip.
export function HistoryActionMenu({ anchor, label, keyboard, onClose, children }) {
  const menu = useRef(null);
  const close = useRef(onClose);
  close.current = onClose;
  const [position, setPosition] = useState({ visibility: "hidden" });
  useLayoutEffect(() => {
    if (!anchor || !menu.current) return;
    function place() {
      const trigger = anchor.getBoundingClientRect();
      const bounds = menu.current.getBoundingClientRect();
      const below = trigger.bottom + 6;
      setPosition({
        left: Math.max(8, Math.min(innerWidth - bounds.width - 8, trigger.right - bounds.width)),
        top: Math.max(8, below + bounds.height <= innerHeight - 8 ? below : trigger.top - bounds.height - 6),
        visibility: "visible",
      });
    }
    place();
    const resize = new ResizeObserver(place);
    resize.observe(menu.current);
    function dismissOnScroll(event) {
      if (!menu.current?.contains(event.target)) close.current();
    }
    window.addEventListener("resize", place);
    document.addEventListener("scroll", dismissOnScroll, true);
    const focusFrame = keyboard ? requestAnimationFrame(() => menu.current?.querySelector('[role="menuitem"]')?.focus({ preventScroll: true })) : null;
    return () => {
      if (focusFrame !== null) cancelAnimationFrame(focusFrame);
      resize.disconnect();
      window.removeEventListener("resize", place);
      document.removeEventListener("scroll", dismissOnScroll, true);
    };
  }, [anchor, keyboard]);

  function navigate(event) {
    if (event.target.tagName === "INPUT") return;
    const items = [...menu.current.querySelectorAll('[role="menuitem"], [role="menuitemradio"]')].filter(item => !item.disabled);
    const current = items.indexOf(document.activeElement);
    const direction = { ArrowDown: 1, ArrowUp: -1 }[event.key];
    if (!direction && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : current < 0 ? (direction === 1 ? 0 : items.length - 1) : (current + direction + items.length) % items.length;
    items[next]?.focus();
  }
  return createPortal(<div ref={menu} className="chat-menu chat-menu--anchored" style={position} role="menu" aria-label={label} onKeyDown={navigate}>{children}</div>, document.body);
}
