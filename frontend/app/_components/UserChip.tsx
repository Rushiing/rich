"use client";

import { useEffect, useState } from "react";
import { api, Me } from "../../lib/api";

/**
 * Small identity chip + logout dropdown for the page header. Fetches /me
 * on mount; renders nothing while loading or anonymous (legacy v1 cookies
 * with no uid). Self-contained — drop into any page header without
 * threading state through.
 */
export default function UserChip() {
  const [me, setMe] = useState<Me | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null));
  }, []);

  if (!me || !("user_id" in me)) return null;

  // Mask middle digits — 138****0001
  const masked = me.phone.length === 11
    ? `${me.phone.slice(0, 3)}****${me.phone.slice(7)}`
    : me.phone;

  async function logout() {
    try {
      await api.logout();
    } finally {
      window.location.href = "/login";
    }
  }

  return (
    <div style={{ position: "relative" }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          padding: "4px 10px",
          background: "transparent",
          color: "#9ca3af",
          border: "1px solid #2a2a2a",
          borderRadius: 14,
          fontSize: 12,
          cursor: "pointer",
        }}
      >
        {masked} ▾
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 4px)",
            right: 0,
            minWidth: 120,
            background: "#141414",
            border: "1px solid #2a2a2a",
            borderRadius: 6,
            padding: 4,
            zIndex: 50,
          }}
        >
          <button
            onClick={logout}
            style={{
              display: "block",
              width: "100%",
              padding: "6px 10px",
              background: "transparent",
              border: "none",
              color: "#e5e5e5",
              fontSize: 13,
              textAlign: "left",
              cursor: "pointer",
            }}
          >
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}
