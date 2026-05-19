"use client";

/**
 * Dashboard — the post-login landing page. Three sections:
 *   1. 大盘     — 上证/深证/创业板 index quotes
 *   2. 今日推荐 — top 3 LLM-curated sectors (reuses /api/sectors/picks)
 *   3. 今日要看 — filtered watchlist rows worth attention today
 *
 * Everything reuses existing endpoints; no new aggregation backend.
 */

import { useEffect, useState } from "react";
import {
  api, IndexQuote, SectorPicksResponse, StockRow,
} from "../lib/api";

const ACTIONABLE_STYLE: Record<string, { color: string; label: string }> = {
  "建议买入":   { color: "#ef4444", label: "买" },
  "观望":       { color: "#9ca3af", label: "观望" },
  "建议卖出":   { color: "#22c55e", label: "卖" },
  "不建议入手": { color: "#6b7280", label: "不入" },
};

// A watchlist row is "worth looking at today" if it has a strong signal,
// moved hard, or carries an actionable buy/sell verdict.
function worthAttention(r: StockRow): boolean {
  if (r.has_strong_signal) return true;
  if (r.change_pct != null && Math.abs(r.change_pct) >= 5) return true;
  const a = r.analysis?.actionable;
  return a === "建议买入" || a === "建议卖出";
}

export default function DashboardPage() {
  const [indices, setIndices] = useState<IndexQuote[] | null>(null);
  const [picks, setPicks] = useState<SectorPicksResponse | null>(null);
  const [rows, setRows] = useState<StockRow[] | null>(null);

  useEffect(() => {
    api.listIndices().then(setIndices).catch(() => setIndices([]));
    api.getSectorPicks().then(setPicks).catch(() => setPicks(null));
    api.listStocks().then(setRows).catch(() => setRows([]));
  }, []);

  const attention = (rows ?? [])
    .filter(worthAttention)
    .slice(0, 10);

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: 18, margin: 0 }}>首页</h1>

      {/* ---- 大盘 ---- */}
      <Section title="大盘">
        {indices == null ? (
          <Muted>加载中…</Muted>
        ) : indices.length === 0 ? (
          <Muted>指数数据暂不可用</Muted>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 10 }}>
            {indices.map((ix) => {
              const up = ix.change_pct >= 0;
              const c = up ? "#ef4444" : "#22c55e";
              return (
                <div key={ix.symbol} style={card}>
                  <div style={{ fontSize: 13, color: "var(--text-muted)" }}>{ix.name}</div>
                  <div style={{ fontFamily: "monospace", fontSize: 20, color: c, marginTop: 4 }}>
                    {ix.point.toFixed(2)}
                  </div>
                  <div style={{ fontFamily: "monospace", fontSize: 13, color: c }}>
                    {up ? "+" : ""}{ix.change_pct.toFixed(2)}%
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Section>

      {/* ---- 今日推荐板块 ---- */}
      <Section title="今日推荐板块" href="/sectors" hrefLabel="全部板块 →">
        {picks == null ? (
          <Muted>加载中…</Muted>
        ) : picks.sectors.length === 0 ? (
          <Muted>暂无推荐</Muted>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10 }}>
            {picks.sectors.slice(0, 3).map((s) => {
              const up = s.change_pct >= 0;
              return (
                <div key={s.name} style={card}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                    <span style={{ fontSize: 14, fontWeight: 600 }}>{s.name}</span>
                    <span style={{ fontFamily: "monospace", fontSize: 13, color: up ? "#ef4444" : "#22c55e", fontWeight: 600 }}>
                      {up ? "+" : ""}{s.change_pct.toFixed(2)}%
                    </span>
                  </div>
                  <p style={{ margin: "6px 0 0", fontSize: 12, color: "var(--text-soft)", lineHeight: 1.5 }}>
                    {s.reason}
                  </p>
                  <div style={{ marginTop: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {s.picks.map((p) => (
                      <a key={p.code} href={`/stocks/${p.code}`} style={{
                        fontSize: 11, padding: "2px 6px", borderRadius: 4,
                        background: "var(--surface)", border: "1px solid var(--border-faint)",
                        color: "var(--text)", textDecoration: "none",
                      }}>{p.name}</a>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Section>

      {/* ---- 今日要看 ---- */}
      <Section title="今日要看" href="/stocks" hrefLabel="全部盯盘 →">
        {rows == null ? (
          <Muted>加载中…</Muted>
        ) : attention.length === 0 ? (
          <Muted>今日自选池无强信号 / 大涨大跌 / 买卖建议</Muted>
        ) : (
          <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
            {attention.map((r, i) => {
              const verdict = r.analysis?.actionable
                ? ACTIONABLE_STYLE[r.analysis.actionable]
                : null;
              return (
                <a
                  key={r.code}
                  href={`/stocks/${r.code}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "9px 12px",
                    borderTop: i === 0 ? undefined : "1px solid var(--border-faint)",
                    background: r.has_strong_signal ? "var(--row-strong-bg)" : undefined,
                    color: "var(--text)",
                    textDecoration: "none",
                    fontSize: 13,
                  }}
                >
                  <span style={{ fontFamily: "monospace", color: "var(--text-faint)", width: 56 }}>
                    {r.code}
                  </span>
                  <span style={{ width: 90, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {r.name}
                  </span>
                  <span style={{
                    fontFamily: "monospace", width: 64, textAlign: "right",
                    color: r.change_pct == null ? "var(--text-muted)"
                      : r.change_pct >= 0 ? "#ef4444" : "#22c55e",
                  }}>
                    {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(2)}%` : "-"}
                  </span>
                  {verdict ? (
                    <span style={{
                      fontSize: 11, fontWeight: 600, padding: "1px 6px", borderRadius: 3,
                      color: verdict.color, background: "var(--surface)",
                      border: "1px solid var(--border-faint)",
                    }}>{verdict.label}</span>
                  ) : (
                    <span style={{ width: 30 }} />
                  )}
                  <span style={{
                    flex: 1, minWidth: 0, color: "var(--text-soft)", fontSize: 12,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>
                    {r.analysis?.one_line_reason || ""}
                  </span>
                </a>
              );
            })}
          </div>
        )}
      </Section>
    </main>
  );
}

function Section({
  title, href, hrefLabel, children,
}: {
  title: string;
  href?: string;
  hrefLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <section style={{ marginTop: 24 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 10 }}>
        <h2 style={{ fontSize: 14, margin: 0, fontWeight: 600, color: "var(--text-soft)" }}>{title}</h2>
        {href && (
          <a href={href} style={{ fontSize: 12, color: "var(--link)", textDecoration: "none" }}>
            {hrefLabel}
          </a>
        )}
      </div>
      {children}
    </section>
  );
}

function Muted({ children }: { children: React.ReactNode }) {
  return <p style={{ color: "var(--text-faint)", fontSize: 13, margin: 0 }}>{children}</p>;
}

const card: React.CSSProperties = {
  padding: 12,
  border: "1px solid var(--border)",
  borderRadius: 8,
  background: "var(--surface-alt)",
};
