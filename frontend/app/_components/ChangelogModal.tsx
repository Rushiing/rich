"use client";

/**
 * Auto-popup release-notes modal.
 *
 * Shows the LATEST_CHANGELOG entry once per release: on mount we compare
 * localStorage.changelog_seen against LATEST_CHANGELOG.date. If unset or
 * older, the modal opens; clicking "我知道了" or "查看全部更新 →" writes
 * the latest date back to localStorage so we don't pop again.
 *
 * Skipped on /login — pre-auth users don't need to see it. Other unmounted
 * routes (/changelog itself) still get the listener but won't pop because
 * by then the user typically already saw it elsewhere.
 */

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { CHANGELOG, LATEST_CHANGELOG } from "../../lib/changelog";

// Bumped to v2 (2026-05-10 evening) — the 5/10 entry was rewritten that
// same day to reflect the password + invite-code auth (replacing the
// earlier SMS-only description). Bumping the key forces a re-pop so users
// who had dismissed v1 see the corrected/expanded notes on next visit.
// Future content-only edits to existing entries: bump again.
const STORAGE_KEY = "changelog_seen_v2";

export default function ChangelogModal() {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (pathname === "/login") return;
    try {
      const seen = window.localStorage.getItem(STORAGE_KEY);
      if (!seen || seen < LATEST_CHANGELOG.date) {
        setOpen(true);
      }
    } catch {
      // localStorage may be unavailable (private mode etc.) — silently skip
    }
  }, [pathname]);

  function markSeen() {
    try {
      window.localStorage.setItem(STORAGE_KEY, LATEST_CHANGELOG.date);
    } catch {
      // ignore
    }
    setOpen(false);
  }

  function viewAll() {
    markSeen();
    router.push("/changelog");
  }

  // Render nothing on /login regardless of state — avoids edge case where
  // user just registered and the modal flashes during redirect.
  if (pathname === "/login") return null;
  if (!open) return null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
        padding: 16,
      }}
      onClick={markSeen}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 8,
          padding: 20,
          width: "100%",
          maxWidth: 520,
          maxHeight: "80vh",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
          <h2 style={{ fontSize: 16, margin: 0 }}>📅 {LATEST_CHANGELOG.date} 更新</h2>
          <span style={{ fontSize: 11, color: "var(--text-faint)" }}>共 {CHANGELOG.length} 个版本</span>
        </div>

        <div style={{
          overflowY: "auto",
          flex: 1,
          display: "flex",
          flexDirection: "column",
          gap: 14,
          paddingRight: 4,
        }}>
          {LATEST_CHANGELOG.sections.map((sec, i) => (
            <div key={i}>
              {sec.title && (
                <div style={{
                  fontSize: 13,
                  color: "var(--text-soft)",
                  fontWeight: 600,
                  marginBottom: 6,
                }}>
                  {sec.title}
                </div>
              )}
              <ul style={{
                margin: 0,
                paddingLeft: 18,
                fontSize: 13,
                lineHeight: 1.7,
                color: "var(--text)",
              }}>
                {sec.items.map((item, j) => (
                  <li key={j}>{item}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: 8,
          paddingTop: 4,
          borderTop: "1px solid var(--border-faint)",
        }}>
          <button
            type="button"
            onClick={viewAll}
            style={{
              background: "transparent",
              border: "none",
              color: "var(--link)",
              fontSize: 13,
              cursor: "pointer",
              padding: 0,
            }}
          >
            查看全部更新 →
          </button>
          <button
            type="button"
            onClick={markSeen}
            style={{
              padding: "6px 14px",
              background: "var(--link)",
              color: "white",
              border: "none",
              borderRadius: 6,
              fontSize: 13,
              cursor: "pointer",
            }}
          >
            我知道了
          </button>
        </div>
      </div>
    </div>
  );
}
