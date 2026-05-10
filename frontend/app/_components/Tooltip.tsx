"use client";

/**
 * Lightweight tooltip — wraps any inline element, shows a styled popover on
 * hover/focus (desktop) or tap (mobile). Replaces the native `title=...` UX,
 * which has ~700ms delay, no wrapping, and no styling.
 *
 * Anchors to children with absolute positioning inside a relatively-positioned
 * wrapper span. Auto-flips top↔bottom if the popover would overflow viewport.
 *
 * Mobile contract: a tap on the trigger toggles open; while open, a tap
 * outside (anywhere) closes it. Hover/focus is desktop-only.
 *
 * Note we don't portal: the chips/icons we wrap aren't inside overflow:hidden
 * containers in this app, so positioned-relative siblings render fine. If
 * that changes, switch to portal + position: fixed.
 */

import {
  ReactNode, useEffect, useRef, useState, type CSSProperties,
} from "react";

type Placement = "top" | "bottom";

const SHOW_DELAY_MS = 150;
const VIEWPORT_MARGIN = 8;

export default function Tooltip({
  content,
  children,
  placement = "top",
  maxWidth = 240,
}: {
  content: ReactNode;
  children: ReactNode;
  placement?: Placement;
  maxWidth?: number;
}) {
  const [open, setOpen] = useState(false);
  const [actualPlacement, setActualPlacement] = useState<Placement>(placement);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const showTimer = useRef<number | null>(null);

  function clearShowTimer() {
    if (showTimer.current != null) {
      window.clearTimeout(showTimer.current);
      showTimer.current = null;
    }
  }

  function scheduleOpen() {
    clearShowTimer();
    showTimer.current = window.setTimeout(() => setOpen(true), SHOW_DELAY_MS);
  }

  function close() {
    clearShowTimer();
    setOpen(false);
  }

  function toggle() {
    clearShowTimer();
    setOpen((v) => !v);
  }

  // Auto-flip logic. After open, measure the popover's effective rect; if the
  // top placement would clip above the viewport, switch to bottom (or v.v.).
  useEffect(() => {
    if (!open || !wrapRef.current || !popRef.current) return;
    const trig = wrapRef.current.getBoundingClientRect();
    const pop = popRef.current.getBoundingClientRect();
    if (placement === "top" && trig.top - pop.height - VIEWPORT_MARGIN < 0) {
      setActualPlacement("bottom");
    } else if (
      placement === "bottom" &&
      trig.bottom + pop.height + VIEWPORT_MARGIN > window.innerHeight
    ) {
      setActualPlacement("top");
    } else {
      setActualPlacement(placement);
    }
  }, [open, placement]);

  // Mobile-style tap-outside dismiss: only attach the listener while open
  // so we're not paying for it on every page.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (!wrapRef.current) return;
      if (!wrapRef.current.contains(e.target as Node)) close();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Cleanup on unmount.
  useEffect(() => () => clearShowTimer(), []);

  const popStyle: CSSProperties = {
    position: "absolute",
    left: "50%",
    transform: "translateX(-50%)",
    [actualPlacement === "top" ? "bottom" : "top"]: "calc(100% + 6px)" as unknown as number,
    zIndex: 50,
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: 6,
    color: "var(--text)",
    fontSize: 12,
    lineHeight: 1.5,
    padding: "8px 10px",
    minWidth: 140,
    maxWidth,
    whiteSpace: "normal",
    boxShadow: "0 4px 12px rgba(0,0,0,0.25)",
    pointerEvents: "auto",
  };

  return (
    <span
      ref={wrapRef}
      onMouseEnter={scheduleOpen}
      onMouseLeave={close}
      onFocus={scheduleOpen}
      onBlur={close}
      onClick={(e) => {
        // Only treat as a tap-toggle on touch-capable input. On desktop the
        // hover handlers already control state, so a click would close
        // immediately on the same event.
        if ((e.nativeEvent as PointerEvent).pointerType === "touch") {
          e.stopPropagation();
          toggle();
        }
      }}
      style={{
        position: "relative",
        display: "inline-block",
        cursor: "help",
      }}
    >
      {children}
      {open && (
        <div ref={popRef} style={popStyle} role="tooltip">
          {content}
        </div>
      )}
    </span>
  );
}
