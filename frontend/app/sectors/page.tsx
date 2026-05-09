"use client";

import { useEffect, useState } from "react";
import { api, Sector } from "../../lib/api";
import UserChip from "../_components/UserChip";

/**
 * Phase 8: industry-sector ranking. Backend pulls Sina's "新浪行业" (49
 * sectors) sorted by today's change_pct. We add a small zone separator
 * so 涨幅 vs 跌幅 sectors are visually distinct.
 */
export default function SectorsPage() {
  const [sectors, setSectors] = useState<Sector[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listSectors()
      .then(setSectors)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>板块</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <UserChip />
          <a href="/stocks" style={linkStyle}>盯盘</a>
          <a href="/watchlist" style={linkStyle}>自选池</a>
        </div>
      </header>

      <p style={{ color: "#888", fontSize: 12, marginTop: 12 }}>
        新浪行业划分，{sectors?.length ?? "—"} 个板块，按今日涨跌幅降序排列。
      </p>

      {err && <div style={{ color: "#ef4444", fontSize: 13, marginTop: 12 }}>{err}</div>}

      {!sectors ? (
        <p style={{ color: "#666", marginTop: 24 }}>加载中…</p>
      ) : (
        <div className="table-scroll" style={{ marginTop: 12 }}>
          <table style={tableStyle}>
            <thead>
              <tr style={{ color: "#888", fontSize: 12 }}>
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
                  <td style={{ ...td, textAlign: "right", color: "#666", fontSize: 12 }}>
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
                  <td style={{ ...td, textAlign: "right", color: "#aaa", fontFamily: "monospace" }}>
                    {s.company_count}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: "#aaa", fontFamily: "monospace" }}>
                    {s.avg_price != null ? s.avg_price.toFixed(2) : "—"}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: "#aaa", fontFamily: "monospace" }}>
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
                          color: "#666",
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
                      <span style={{ color: "#444" }}>—</span>
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
  borderBottom: "1px solid #222",
  fontWeight: 500,
};
const td: React.CSSProperties = {
  padding: "10px",
  borderBottom: "1px solid #1a1a1a",
  fontSize: 13,
};
