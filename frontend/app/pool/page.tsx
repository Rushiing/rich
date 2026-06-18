"use client";

/**
 * 预选池 — B0/B1 (6/10),6/18 升级为批次(cohort)区域结构。
 *
 * 三段:
 *  - 可推荐 → 按"批次"(晋升周)聚合成卡片。每批是一个可考核单位:支数 +
 *    至今平均收益,阶段2 的中证500超额/买卖命中率先留占位预热文案。
 *  - 观察中 / 最近淘汰 → 保持表格(没有批次概念)。
 *
 * 区域分离(Rush 定):所有票都链到 /pool/{code}(系统推荐区域),绝不跳
 * /stocks(用户自选区域)。点系统推荐票看的是预选池专属详情,不污染自选。
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
          <BatchSection rows={data.recommendable} />
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

// 可推荐区域:按批次(晋升周 cohort_week)聚合成卡片。每批 = 一个可考核单位。
function BatchSection({ rows }: { rows: PoolEntryRow[] }) {
  const meta = STATE_META.recommendable;
  if (rows.length === 0) return null;

  // 按 cohort_week 分组;晋升前没记 cohort 的老票归"早期入选"
  const byCohort = new Map<string, PoolEntryRow[]>();
  for (const r of rows) {
    const k = r.cohort_week || "早期入选";
    (byCohort.get(k) ?? byCohort.set(k, []).get(k)!).push(r);
  }
  // 新周在前;"早期入选"垫底
  const cohorts = [...byCohort.keys()].sort((a, b) => {
    if (a === "早期入选") return 1;
    if (b === "早期入选") return -1;
    return b.localeCompare(a);
  });

  return (
    <section style={{ marginTop: 22 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 10 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: meta.color }}>
          可推荐 · 按批次（{rows.length}）
        </span>
        <span style={{ color: "var(--text-faint)", fontSize: 12 }}>{meta.hint}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 14 }}>
        {cohorts.map((c) => <BatchCard key={c} cohort={c} rows={byCohort.get(c)!} />)}
      </div>
    </section>
  );
}

function BatchCard({ cohort, rows }: { cohort: string; rows: PoolEntryRow[] }) {
  const weekNum = /-W(\d+)/.exec(cohort)?.[1];
  const title = weekNum ? `第 ${weekNum} 周批次` : cohort;
  // 批次至今收益 = 等权平均(若你跟这批建议等额建仓的收益)
  const valid = rows.filter((r) => r.return_pct != null);
  const avg = valid.length ? valid.reduce((s, r) => s + (r.return_pct ?? 0), 0) / valid.length : null;
  const avgColor = avg == null ? "var(--text-muted)" : avg >= 0 ? "#ef4444" : "#22c55e";

  return (
    <div style={{
      border: "1px solid var(--border)", borderRadius: 10, padding: 14,
      background: "var(--surface-alt)",
    }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>{title}</span>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{rows.length} 支</span>
      </div>

      <div style={{ marginTop: 8, display: "flex", alignItems: "baseline", gap: 6 }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>至今等权平均</span>
        <span style={{ fontSize: 20, fontWeight: 700, fontFamily: "monospace", color: avgColor }}>
          {avg != null ? `${avg >= 0 ? "+" : ""}${avg.toFixed(2)}%` : "—"}
        </span>
      </div>

      {/* 阶段2 指标占位预热(中证500超额 + 买卖命中率,数据攒够 d5 后揭晓) */}
      <div style={{
        marginTop: 10, padding: "8px 10px", borderRadius: 6,
        background: "var(--surface)", border: "1px dashed var(--border-mid)",
        fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6,
      }}>
        📊 相对中证500超额 + 买卖建议命中率<br />
        <span style={{ color: "var(--text-muted)" }}>正在为这一批积累 5 日数据 · 预计 6 月底首次揭晓</span>
      </div>

      {/* 批次成分股,每支链到 /pool/{code}(系统推荐区域) */}
      <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 4 }}>
        {rows.map((r) => {
          const rc = r.return_pct == null ? "var(--text-muted)" : r.return_pct >= 0 ? "#ef4444" : "#22c55e";
          return (
            <a key={r.id} href={`/pool/${r.code}`} style={{
              display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
              padding: "5px 8px", borderRadius: 5, textDecoration: "none",
              background: "var(--surface)", border: "1px solid var(--border-faint)",
            }}>
              <span style={{ display: "flex", alignItems: "baseline", gap: 6, minWidth: 0 }}>
                <span style={{ color: "var(--text)", fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.name || r.code}
                </span>
                <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 11 }}>{r.code}</span>
                <span style={{
                  fontSize: 10, padding: "0 4px", borderRadius: 3,
                  color: r.source === "rules" ? "#93c5fd" : "#d8b4fe",
                  background: r.source === "rules" ? "rgba(59,130,246,0.12)" : "rgba(168,85,247,0.12)",
                }}>
                  {SOURCE_LABEL[r.source] ?? r.source}
                </span>
              </span>
              <span style={{ fontFamily: "monospace", fontSize: 13, fontWeight: 600, color: rc }}>
                {r.return_pct != null ? `${r.return_pct >= 0 ? "+" : ""}${r.return_pct.toFixed(2)}%` : "—"}
              </span>
            </a>
          );
        })}
      </div>
    </div>
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
                  <a href={`/pool/${r.code}`} style={{ color: "var(--text)", textDecoration: "none" }}>
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
