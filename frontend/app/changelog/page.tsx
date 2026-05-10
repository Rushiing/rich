"use client";

/**
 * /changelog — full release-notes archive. Renders all entries reverse-
 * chronologically (CHANGELOG[0] is already the newest).
 *
 * Pattern matches other pages: themed header with UserChip + ThemeToggle
 * + back-to-/stocks link. Width capped at 880px so long bullet lines
 * stay readable.
 */

import { CHANGELOG } from "../../lib/changelog";
import UserChip from "../_components/UserChip";
import ThemeToggle from "../_components/ThemeToggle";

export default function ChangelogPage() {
  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>更新日志</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <ThemeToggle />
          <UserChip />
          <a href="/stocks" style={linkStyle}>盯盘</a>
        </div>
      </header>

      <p style={{ color: "var(--text-muted)", fontSize: 12, marginTop: 12 }}>
        共 {CHANGELOG.length} 个版本，按时间倒序。
      </p>

      <div style={{ marginTop: 24, display: "flex", flexDirection: "column", gap: 32 }}>
        {CHANGELOG.map((entry) => (
          <section
            key={entry.date}
            style={{
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 18,
              background: "var(--surface-alt)",
            }}
          >
            <h2 style={{ margin: 0, fontSize: 16 }}>📅 {entry.date}</h2>
            <div style={{ marginTop: 14, display: "flex", flexDirection: "column", gap: 16 }}>
              {entry.sections.map((sec, i) => (
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
                    paddingLeft: 20,
                    fontSize: 14,
                    lineHeight: 1.75,
                    color: "var(--text)",
                  }}>
                    {sec.items.map((item, j) => (
                      <li key={j}>{item}</li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </section>
        ))}
      </div>
    </main>
  );
}

const linkStyle: React.CSSProperties = {
  color: "var(--text-soft)",
  fontSize: 13,
  textDecoration: "none",
  padding: "6px 10px",
};
