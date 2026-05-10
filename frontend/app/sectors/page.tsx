"use client";

import { useEffect, useState } from "react";
import { api, Sector, SectorPicksResponse } from "../../lib/api";
import UserChip from "../_components/UserChip";
import ThemeToggle from "../_components/ThemeToggle";
import Tooltip from "../_components/Tooltip";

/**
 * Phase 8: industry-sector ranking page.
 *
 * Layout:
 *   1. Hero: LLM-curated TOP-N sector picks with per-sector and per-stock
 *      reasons. 2-hour TTL on the backend, regenerable on demand.
 *   2. Below: full 49-sector ranking table (Sina's 新浪行业 spot).
 */
export default function SectorsPage() {
  const [sectors, setSectors] = useState<Sector[] | null>(null);
  const [picks, setPicks] = useState<SectorPicksResponse | null>(null);
  const [picksLoading, setPicksLoading] = useState(true);
  const [picksErr, setPicksErr] = useState<string | null>(null);
  const [picksRefreshing, setPicksRefreshing] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listSectors()
      .then(setSectors)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
    api.getSectorPicks()
      .then((p) => { setPicks(p); setPicksErr(null); })
      .catch((e) => setPicksErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setPicksLoading(false));
  }, []);

  async function regeneratePicks() {
    setPicksRefreshing(true);
    setPicksErr(null);
    try {
      setPicks(await api.refreshSectorPicks());
    } catch (e) {
      setPicksErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPicksRefreshing(false);
    }
  }

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>板块</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <ThemeToggle />
          <UserChip />
          <a href="/stocks" style={linkStyle}>盯盘</a>
          <a href="/watchlist" style={linkStyle}>自选池</a>
        </div>
      </header>

      <PicksHero
        picks={picks}
        loading={picksLoading}
        err={picksErr}
        refreshing={picksRefreshing}
        onRegenerate={regeneratePicks}
      />

      <h2 style={{ fontSize: 14, color: "var(--text-soft)", margin: "32px 0 0", fontWeight: 500 }}>
        全部板块涨跌榜
      </h2>
      <p style={{ color: "var(--text-muted)", fontSize: 12, marginTop: 6 }}>
        新浪行业划分，{sectors?.length ?? "—"} 个板块，按今日涨跌幅降序排列。
      </p>

      {err && <div style={{ color: "#ef4444", fontSize: 13, marginTop: 12 }}>{err}</div>}

      {!sectors ? (
        <p style={{ color: "var(--text-faint)", marginTop: 24 }}>加载中…</p>
      ) : (
        <div className="table-scroll" style={{ marginTop: 12 }}>
          <table style={tableStyle}>
            <thead>
              <tr style={{ color: "var(--text-muted)", fontSize: 12 }}>
                <th style={{ ...th, textAlign: "right", width: 36 }}>#</th>
                <th style={th}>板块</th>
                <th style={{ ...th, textAlign: "right" }}>涨跌幅</th>
                <th style={{ ...th, textAlign: "right" }}>家数</th>
                <th style={{ ...th, textAlign: "right" }}>平均价</th>
                <th style={{ ...th, textAlign: "right" }}>总成交额</th>
                <th style={th}>领涨股</th>
              </tr>
            </thead>
            <tbody>
              {sectors.map((s, i) => (
                <tr key={s.code || s.name}>
                  <td style={{ ...td, textAlign: "right", color: "var(--text-faint)", fontSize: 12 }}>
                    {i + 1}
                  </td>
                  <td style={td}>
                    <span style={{ fontSize: 14 }}>{s.name}</span>
                  </td>
                  <td style={{
                    ...td,
                    textAlign: "right",
                    fontFamily: "monospace",
                    fontWeight: 600,
                    color: s.change_pct >= 0 ? "#ef4444" : "#22c55e",
                  }}>
                    {s.change_pct >= 0 ? "+" : ""}{s.change_pct.toFixed(2)}%
                  </td>
                  <td style={{ ...td, textAlign: "right", color: "var(--text-soft)", fontFamily: "monospace" }}>
                    {s.company_count}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: "var(--text-soft)", fontFamily: "monospace" }}>
                    {s.avg_price != null ? s.avg_price.toFixed(2) : "—"}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: "var(--text-soft)", fontFamily: "monospace" }}>
                    {fmtTurnover(s.total_turnover)}
                  </td>
                  <td style={td}>
                    {s.leader && s.leader.code ? (
                      <a
                        href={`/stocks/${s.leader.code}`}
                        style={{
                          color: "#9ca3af",
                          fontSize: 13,
                          textDecoration: "none",
                        }}
                      >
                        <span style={{
                          fontFamily: "monospace",
                          color: "var(--text-faint)",
                          marginRight: 6,
                        }}>
                          {s.leader.code}
                        </span>
                        {s.leader.name}
                        <span style={{
                          marginLeft: 6,
                          color: s.leader.change_pct >= 0 ? "#ef4444" : "#22c55e",
                          fontFamily: "monospace",
                          fontSize: 12,
                        }}>
                          {s.leader.change_pct >= 0 ? "+" : ""}
                          {s.leader.change_pct.toFixed(2)}%
                        </span>
                      </a>
                    ) : (
                      <span style={{ color: "var(--text-dim)" }}>—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

function PicksHero({
  picks, loading, err, refreshing, onRegenerate,
}: {
  picks: SectorPicksResponse | null;
  loading: boolean;
  err: string | null;
  refreshing: boolean;
  onRegenerate: () => void;
}) {
  return (
    <section style={{ marginTop: 16 }}>
      <div style={{
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        gap: 12,
        flexWrap: "wrap",
      }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          <h2 style={{ fontSize: 16, margin: 0 }}>今日推荐 · TOP {picks?.sectors.length ?? 5} 板块</h2>
          <Tooltip
            content="基于今日板块涨跌 + 成份股表现的 LLM 综合判断。每 2 小时缓存一次；点'重新生成'强制重算。仅供参考，不构成投资建议。"
          >
            <span style={{
              fontSize: 11,
              color: "var(--text-faint)",
              cursor: "help",
              padding: "1px 6px",
              border: "1px solid var(--border)",
              borderRadius: 10,
            }}>?</span>
          </Tooltip>
          {picks?.generated_at && (
            <span style={{ fontSize: 12, color: "var(--text-faint)" }}>
              生成于 {new Date(picks.generated_at).toLocaleString("zh-CN", {
                month: "numeric", day: "numeric",
                hour: "2-digit", minute: "2-digit",
              })}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={onRegenerate}
          disabled={refreshing || loading}
          style={{
            padding: "4px 10px",
            background: "transparent",
            color: "var(--text-soft)",
            border: "1px solid var(--border-mid)",
            borderRadius: 6,
            fontSize: 12,
            cursor: refreshing || loading ? "not-allowed" : "pointer",
          }}
        >
          {refreshing ? "生成中…" : "重新生成"}
        </button>
      </div>

      {err && <div style={{ color: "#ef4444", fontSize: 13, marginTop: 8 }}>{err}</div>}

      {loading && !picks ? (
        <p style={{ color: "var(--text-faint)", marginTop: 16 }}>加载中…</p>
      ) : !picks || picks.sectors.length === 0 ? (
        <p style={{ color: "var(--text-faint)", marginTop: 16 }}>暂无推荐</p>
      ) : (
        <div style={{
          marginTop: 12,
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
          gap: 12,
        }}>
          {picks.sectors.map((sec) => (
            <SectorPickCard key={sec.name} sec={sec} />
          ))}
        </div>
      )}
    </section>
  );
}

function SectorPickCard({ sec }: { sec: SectorPicksResponse["sectors"][number] }) {
  const upColor = "#ef4444", downColor = "#22c55e";
  const trendColor = sec.change_pct >= 0 ? upColor : downColor;
  return (
    <div style={{
      padding: 12,
      border: "1px solid var(--border)",
      borderRadius: 8,
      background: "var(--surface-alt)",
      display: "flex",
      flexDirection: "column",
      gap: 8,
    }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: 14, fontWeight: 600 }}>{sec.name}</span>
        <span style={{ fontFamily: "monospace", fontSize: 13, color: trendColor, fontWeight: 600 }}>
          {sec.change_pct >= 0 ? "+" : ""}{sec.change_pct.toFixed(2)}%
        </span>
      </div>
      <p style={{ margin: 0, fontSize: 12, color: "var(--text-soft)", lineHeight: 1.55 }}>
        {sec.reason}
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 2 }}>
        {sec.picks.map((p) => (
          <Tooltip
            key={p.code}
            content={
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{p.code} {p.name}</div>
                <div style={{ color: "var(--text-soft)" }}>{p.reason}</div>
              </div>
            }
          >
            <a
              href={`/stocks/${p.code}`}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 8,
                padding: "6px 8px",
                background: "var(--surface)",
                border: "1px solid var(--border-faint)",
                borderRadius: 6,
                color: "var(--text)",
                fontSize: 12,
                textDecoration: "none",
              }}
            >
              <span style={{ display: "flex", gap: 6, alignItems: "baseline", minWidth: 0 }}>
                <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 11 }}>{p.code}</span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {p.name}
                </span>
              </span>
              <span style={{ color: "var(--text-faint)", fontSize: 11, flexShrink: 0 }}>详情 →</span>
            </a>
          </Tooltip>
        ))}
      </div>
    </div>
  );
}

function fmtTurnover(yuan: number | null): string {
  if (yuan == null) return "—";
  if (yuan >= 1e12) return `${(yuan / 1e12).toFixed(1)}万亿`;
  if (yuan >= 1e8) return `${(yuan / 1e8).toFixed(0)}亿`;
  if (yuan >= 1e4) return `${(yuan / 1e4).toFixed(0)}万`;
  return yuan.toFixed(0);
}

const linkStyle: React.CSSProperties = {
  color: "#9ca3af",
  fontSize: 13,
  textDecoration: "none",
  padding: "6px 10px",
};
const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
};
const th: React.CSSProperties = {
  textAlign: "left",
  padding: "8px 10px",
  borderBottom: "1px solid var(--border-soft)",
  fontWeight: 500,
};
const td: React.CSSProperties = {
  padding: "10px",
  borderBottom: "1px solid var(--border-faint)",
  fontSize: 13,
};
