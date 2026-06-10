"use client";

/**
 * 预选池 — B0/B1 (6/10). Read-only view over the virtual pool: paper
 * positions the system entered via rules / sector-picks channels and is
 * observing before anything becomes a recommendation.
 *
 * Three groups: 可推荐 (survived ≥5 days with thesis intact) → 观察中 →
 * 最近淘汰 (kept visible on purpose — the elimination发生率 is exactly
 * what makes the surviving recommendations credible).
 */

import { useEffect, useState } from "react";
import { api, PoolEntryRow, PoolOverview } from "../../lib/api";

const STATE_META: Record<string, { label: string; color: string; hint: string }> = {
  recommendable: {
    label: "可推荐",
    color: "#ef4444",
    hint: "观察 ≥5 个交易日、收益为正且守住 MA20 — 论据经受住了时间",
  },
  observing: {
    label: "观察中",
    color: "#3b82f6",
    hint: "刚入池，先看它几天 — 不追刚冲高的票，正是为了躲开次日回落",
  },
  eliminated: {
    label: "最近淘汰",
    color: "#6b7280",
    hint: "触发失效线出局。淘汰记录公开展示 — 没有被淘汰的池子不可信",
  },
};

const SOURCE_LABEL: Record<string, string> = {
  rules: "规则筛选",
  sector_picks: "板块精选",
};

export default function PoolPage() {
  const [data, setData] = useState<PoolOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.poolOverview()
      .then(setData)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <main style={{ padding: 20, maxWidth: 1080, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>预选池</h1>
        <span style={{ color: "var(--text-muted)", fontSize: 13 }}>
          系统每日收盘后筛入候选、连续观察，论据经受住时间的票才会升级为可推荐
        </span>
      </header>

      {loading && <p style={{ color: "var(--text-faint)", marginTop: 24 }}>加载中…</p>}
      {err && <p style={{ color: "#ef4444", marginTop: 24, fontSize: 13 }}>{err}</p>}

      {data && (
        <>
          <div style={{ marginTop: 10, color: "var(--text-muted)", fontSize: 13 }}>
            观察中 {data.counts.observing} · 可推荐 {data.counts.recommendable} · 累计淘汰 {data.counts.eliminated_total}
          </div>
          <Group state="recommendable" rows={data.recommendable} />
          <Group state="observing" rows={data.observing} />
          <Group state="eliminated" rows={data.eliminated_recent} />
          {data.counts.observing === 0 && data.counts.recommendable === 0
            && data.eliminated_recent.length === 0 && (
            <p style={{ color: "var(--text-faint)", marginTop: 32, fontSize: 13 }}>
              池子还是空的 — 每个交易日 16:45 系统会自动筛一轮（突破 20 日新高 +
              主力大额流入 + 业绩为正的自选股，以及当日板块精选）。
            </p>
          )}
        </>
      )}
    </main>
  );
}

function Group({ state, rows }: { state: string; rows: PoolEntryRow[] }) {
  const meta = STATE_META[state];
  if (!meta || rows.length === 0) return null;
  return (
    <section style={{ marginTop: 22 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 8 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: meta.color }}>
          {meta.label}（{rows.length}）
        </span>
        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>{meta.hint}</span>
      </div>
      <div className="table-scroll">
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ color: "var(--text-muted)", fontSize: 12, textAlign: "left" }}>
              <th style={th}>代码 / 名称</th>
              <th style={th}>来源</th>
              <th style={th}>入池日</th>
              <th style={th}>入池价</th>
              <th style={th}>现价</th>
              <th style={th}>池内收益</th>
              <th style={th}>最大回撤</th>
              <th style={th}>观察天数</th>
              <th style={th}>{state === "eliminated" ? "淘汰原因" : "入池论据 / 失效线"}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} style={{ borderTop: "1px solid var(--border-faint)" }}>
                <td style={td}>
                  <a href={`/stocks/${r.code}`} style={{ color: "var(--text)", textDecoration: "none" }}>
                    {r.name || r.code}
                    <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 12, marginLeft: 4 }}>
                      {r.code}
                    </span>
                  </a>
                </td>
                <td style={td}>
                  <span style={{
                    fontSize: 11, padding: "1px 6px", borderRadius: 4,
                    background: r.source === "rules" ? "rgba(59,130,246,0.15)" : "rgba(168,85,247,0.15)",
                    color: r.source === "rules" ? "#93c5fd" : "#d8b4fe",
                  }}>
                    {SOURCE_LABEL[r.source] ?? r.source}
                  </span>
                </td>
                <td style={{ ...td, fontFamily: "monospace" }}>{r.entry_date}</td>
                <td style={{ ...td, fontFamily: "monospace" }}>{r.entry_close.toFixed(2)}</td>
                <td style={{ ...td, fontFamily: "monospace" }}>
                  {r.last_close != null ? r.last_close.toFixed(2) : "—"}
                </td>
                <td style={{ ...td, fontFamily: "monospace" }}>
                  {r.return_pct != null ? (
                    <b style={{ color: r.return_pct >= 0 ? "#ef4444" : "#22c55e" }}>
                      {r.return_pct >= 0 ? "+" : ""}{r.return_pct.toFixed(2)}%
                    </b>
                  ) : "—"}
                </td>
                <td style={{ ...td, fontFamily: "monospace", color: "var(--text-soft)" }}>
                  {r.max_drawdown_pct != null ? `${r.max_drawdown_pct.toFixed(1)}%` : "—"}
                </td>
                <td style={{ ...td, fontFamily: "monospace" }}>{r.days_observed}</td>
                <td style={{ ...td, maxWidth: 360 }}>
                  {state === "eliminated" ? (
                    <span style={{ color: "var(--text-soft)", fontSize: 12 }}>{r.eliminated_reason}</span>
                  ) : (
                    <div style={{ fontSize: 12, lineHeight: 1.5 }}>
                      <div style={{ color: "var(--text)" }}>{r.thesis.summary}</div>
                      <div style={{ color: "var(--text-faint)" }}>
                        失效线 <span style={{ fontFamily: "monospace" }}>{r.thesis.invalidation_price.toFixed(2)}</span>
                        （{r.thesis.invalidation_rule}）
                      </div>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const th: React.CSSProperties = { padding: "6px 10px", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "8px 10px", verticalAlign: "top" };
