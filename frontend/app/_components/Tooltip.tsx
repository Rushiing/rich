"use client";

/**
 * Lightweight tooltip — wraps any inline element, shows a styled popover on
 * hover/focus (desktop) or tap (mobile). Replaces the native `title=...`
 * UX, which has ~700ms delay, no wrapping, and no styling.
 *
 * Rendering strategy: createPortal → body, position: fixed, coords computed
 * from the trigger's getBoundingClientRect. This avoids being clipped by
 * any ancestor with `overflow: auto/hidden/scroll` (notably .table-scroll
 * around the stocks list), which would otherwise crop the popover.
 *
 * Auto-flips top↔bottom if the popover would overflow viewport. Recomputes
 * on scroll/resize while open so a scrolled-out trigger pulls the popover
 * with it (or hides if intersection allows; we just track position here).
 *
 * Mobile contract: a tap on the trigger toggles open; while open, a tap
 * outside (anywhere) closes it. Hover/focus is desktop-only.
 */

import {
  ReactNode, useEffect, useRef, useState, type CSSProperties,
} from "react";
import { createPortal } from "react-dom";

type Placement = "top" | "bottom";

const SHOW_DELAY_MS = 150;
const VIEWPORT_MARGIN = 8;
const POPOVER_GAP = 6;       // gap between trigger and popover

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
  // Position is computed from the trigger's rect. We store viewport coords
  // and a measured `actualPlacement` after we can see the popover's height.
  const [coords, setCoords] = useState<{
    top: number; left: number; placement: Placement;
  } | null>(null);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const showTimer = useRef<number | null>(null);
  const [mounted, setMounted] = useState(false);

  // Portal target — rendering before mount returns null, so the SSR HTML
  // doesn't include the popover.
  useEffect(() => setMounted(true), []);

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

  // Reposition: read trigger rect, decide top vs bottom based on available
  // space, write {top, left} for the fixed popover. Called on first open
  // and on scroll/resize while open.
  function reposition() {
    if (!wrapRef.current || !popRef.current) return;
    const trig = wrapRef.current.getBoundingClientRect();
    const pop = popRef.current.getBoundingClientRect();

    let p: Placement = placement;
    if (placement === "top" && trig.top - pop.height - VIEWPORT_MARGIN < 0) {
      p = "bottom";
    } else if (
      placement === "bottom" &&
      trig.bottom + pop.height + VIEWPORT_MARGIN > window.innerHeight
    ) {
      p = "top";
    }

    const top = p === "top"
      ? trig.top - pop.height - POPOVER_GAP
      : trig.bottom + POPOVER_GAP;

    // Center horizontally on the trigger, but keep within viewport.
    let left = trig.left + trig.width / 2 - pop.width / 2;
    left = Math.max(VIEWPORT_MARGIN, Math.min(left, window.innerWidth - pop.width - VIEWPORT_MARGIN));

    setCoords({ top, left, placement: p });
  }

  // Position on open and whenever the trigger or window changes.
  useEffect(() => {
    if (!open) return;
    // popRef may not yet be set on first render — defer one tick.
    const r = requestAnimationFrame(reposition);
    function onScrollOrResize() { reposition(); }
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      cancelAnimationFrame(r);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Tap-outside dismiss: only attach while open so we're not paying for it
  // on every page.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      const t = e.target as Node;
      if (wrapRef.current?.contains(t)) return;
      if (popRef.current?.contains(t)) return;
      close();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  useEffect(() => () => clearShowTimer(), []);

  const popStyle: CSSProperties = {
    position: "fixed",
    top: coords?.top ?? -9999,
    left: coords?.left ?? -9999,
    zIndex: 1000,
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
    // Hide visually until we have measured-and-positioned coords (first paint
    // happens at -9999 to let us measure, then this kicks in).
    visibility: coords ? "visible" : "hidden",
  };

  return (
    <span
      ref={wrapRef}
      onMouseEnter={scheduleOpen}
      onMouseLeave={close}
      onFocus={scheduleOpen}
      onBlur={close}
      onClick={(e) => {
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
      {mounted && open && createPortal(
        <div ref={popRef} style={popStyle} role="tooltip">
          {content}
        </div>,
        document.body,
      )}
    </span>
  );
}
