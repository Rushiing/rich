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
  api, FunnelStateOut, IndexQuote, SectorPicksResponse, StockRow,
} from "../lib/api";
import { holderStanceFor } from "../lib/holdingFunnel";

const ACTIONABLE_STYLE: Record<string, { color: string; label: string }> = {
  "建议买入":   { color: "#ef4444", label: "买" },
  "观望":       { color: "#9ca3af", label: "观望" },
  "建议卖出":   { color: "#22c55e", label: "卖" },
  "不建议入手": { color: "#6b7280", label: "不入" },
};

// 7/2 持仓立场轴:与盯盘列表同口径 — 默认按已持仓看(显式标未持的行走
// actionable)。verdict chip 和"今日要看"判定都从这一个派生走。
function verdictFor(
  r: StockRow, serverHeld?: boolean,
): { color: string; label: string; direction: string | null } | null {
  const a = r.analysis;
  if (!a || !a.actionable) return null;
  const held = serverHeld !== false; // undefined = 没标过 → 默认持仓
  const stance = held ? holderStanceFor(a.holder_direction) : null;
  if (stance) return { color: stance.color, label: stance.short, direction: stance.direction };
  const s = ACTIONABLE_STYLE[a.actionable] ?? { color: "#9ca3af", label: a.actionable };
  return { ...s, direction: null };
}

// A watchlist row is "worth looking at today" if it has a strong signal,
// moved hard, or carries a directional verdict (买/卖,或持仓立场的
// 看多/看空 — 不建议入手+持仓看空 的票以前会漏掉,603986 案例)。
function worthAttention(r: StockRow, serverHeld?: boolean): boolean {
  if (r.has_strong_signal) return true;
  if (r.change_pct != null && Math.abs(r.change_pct) >= 5) return true;
  const v = verdictFor(r, serverHeld);
  if (v?.direction === "看空" || v?.direction === "看多") return true;
  const a = r.analysis?.actionable;
  return v?.direction == null && (a === "建议买入" || a === "建议卖出");
}

export default function DashboardPage() {
  const [indices, setIndices] = useState<IndexQuote[] | null>(null);
  const [picks, setPicks] = useState<SectorPicksResponse | null>(null);
  const [rows, setRows] = useState<StockRow[] | null>(null);
  // Per-section error state. Each section fails independently — when only
  // the sectors endpoint is down we still want indices + watchlist to
  // render. The previous `.catch(() => setX([]))` setup masked all
  // failures as "no data", misleading users (e.g. seeing "暂无推荐" when
  // backend was actually broken). null = no error.
  const [indicesError, setIndicesError] = useState<string | null>(null);
  const [picksError, setPicksError] = useState<string | null>(null);
  const [rowsError, setRowsError] = useState<string | null>(null);

  // Each loader extracted so the "重试" button can re-fire just that one
  // section's request without re-pulling the other two.
  function loadIndices() {
    setIndicesError(null);
    api.listIndices()
      .then((d) => { setIndices(d); setIndicesError(null); })
      .catch((e) => setIndicesError(e instanceof Error ? e.message : String(e)));
  }
  function loadPicks() {
    setPicksError(null);
    api.getSectorPicks()
      .then((d) => { setPicks(d); setPicksError(null); })
      .catch((e) => setPicksError(e instanceof Error ? e.message : String(e)));
  }
  function loadRows() {
    setRowsError(null);
    api.listStocks()
      .then((d) => { setRows(d); setRowsError(null); })
      .catch((e) => setRowsError(e instanceof Error ? e.message : String(e)));
  }

  // 持仓立场轴:用户显式标过未持的票走买家视角,与盯盘列表同源。静默失败
  // (拉不到就全按默认持仓,只影响 chip axis 不阻塞页面)。
  const [funnelMap, setFunnelMap] = useState<Record<string, FunnelStateOut> | null>(null);

  useEffect(() => {
    loadIndices();
    loadPicks();
    loadRows();
    api.getMyFunnel().then(setFunnelMap).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const heldOf = (code: string) => funnelMap?.[code]?.held;
  const attention = (rows ?? [])
    .filter((r) => worthAttention(r, heldOf(r.code)))
    .slice(0, 10);

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: 18, margin: 0 }}>首页</h1>

      {/* ---- 大盘 ---- */}
      <Section title="大盘">
        {indicesError ? (
          <ErrorLine error={indicesError} onRetry={loadIndices} />
        ) : indices == null ? (
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
        {picksError ? (
          <ErrorLine error={picksError} onRetry={loadPicks} />
        ) : picks == null ? (
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
        {rowsError ? (
          <ErrorLine error={rowsError} onRetry={loadRows} />
        ) : rows == null ? (
          <Muted>加载中…</Muted>
        ) : attention.length === 0 ? (
          <Muted>今日自选池无强信号 / 大涨大跌 / 买卖建议</Muted>
        ) : (
          <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
            {attention.map((r, i) => {
              const verdict = verdictFor(r, heldOf(r.code));
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

// Inline error for a dashboard section. Kept on one line + retry link so
// it doesn't bloat the page when all three sections fail at once (common
// — they all share the same backend). Honest about what went wrong
// instead of silently rendering "暂无数据".
function ErrorLine({ error, onRetry }: { error: string; onRetry: () => void }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 10,
      padding: "8px 12px",
      border: "1px solid #b91c1c",
      background: "rgba(185, 28, 28, 0.08)",
      borderRadius: 6,
      color: "#dc2626",
      fontSize: 13,
    }}>
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        加载失败:{error}
      </span>
      <button
        type="button"
        onClick={onRetry}
        style={{
          background: "transparent",
          border: "1px solid #dc2626",
          color: "#dc2626",
          borderRadius: 4,
          padding: "2px 10px",
          fontSize: 12,
          cursor: "pointer",
          flexShrink: 0,
        }}
      >
        重试
      </button>
    </div>
  );
}

const card: React.CSSProperties = {
  padding: 12,
  border: "1px solid var(--border)",
  borderRadius: 8,
  background: "var(--surface-alt)",
};
