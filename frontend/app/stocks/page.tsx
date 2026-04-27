"use client";

import { useEffect, useRef, useState } from "react";
import { api, AnalysisBrief, StockRow } from "../../lib/api";

// While a snapshot job is running we re-pull /api/stocks at this cadence so
// rows surface as their data lands. 5s feels responsive without hammering.
const POLL_INTERVAL_MS = 5000;
// Hard cap so we eventually stop polling even if the status endpoint lies.
const POLL_MAX_DURATION_MS = 5 * 60 * 1000;

// Maps the LLM's structured `actionable` enum to a (color, short label) pair.
// A股语境：红=买/涨，绿=卖/跌；中性观望灰色。
const ACTIONABLE_STYLE: Record<string, { color: string; label: string }> = {
  "建议买入":   { color: "#ef4444", label: "买" },
  "观望":       { color: "#9ca3af", label: "观望" },
  "建议卖出":   { color: "#22c55e", label: "卖" },
  "不建议入手": { color: "#6b7280", label: "不入" },
};

const SIGNAL_LABEL: Record<string, string> = {
  limit_up: "涨停",
  limit_down: "跌停",
  big_inflow: "主力大额流入",
  big_outflow: "主力大额流出",
  important_notice: "重要公告",
  lhb: "上龙虎榜",
};

const exchangeLabel: Record<string, string> = {
  sh: "上",
  sz: "深",
  bj: "北",
  unknown: "?",
};

export default function StocksPage() {
  const [rows, setRows] = useState<StockRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadline = useRef<number>(0);

  async function refresh(opts: { silent?: boolean } = {}) {
    if (!opts.silent) setLoading(true);
    try {
      setRows(await api.listStocks());
    } finally {
      if (!opts.silent) setLoading(false);
    }
  }

  function stopPolling() {
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    setRefreshing(false);
  }

  useEffect(() => {
    refresh();
    // If a job is already running (e.g., user reloaded mid-batch), join it.
    api.snapshotStatus().then((s) => {
      if (s.running) startPolling();
    }).catch(() => {});
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startPolling() {
    if (pollTimer.current) return;
    setRefreshing(true);
    pollDeadline.current = Date.now() + POLL_MAX_DURATION_MS;
    pollTimer.current = setInterval(async () => {
      try {
        await refresh({ silent: true });
        const status = await api.snapshotStatus();
        if (!status.running) {
          stopPolling();
          setMsg("抓取完成");
          return;
        }
      } catch {
        // ignore transient poll errors; the deadline still bounds us
      }
      if (Date.now() > pollDeadline.current) {
        stopPolling();
        setMsg("抓取超过 5 分钟未结束，已停止刷新；可在 Railway logs 查看后端");
      }
    }, POLL_INTERVAL_MS);
  }

  async function manualSnapshot() {
    setMsg(null);
    try {
      const r = await api.triggerSnapshot();
      if (r.already_running) {
        setMsg("已有抓取任务在进行中，正在跟随刷新");
      } else {
        setMsg("已开始抓取，约 30–90 秒，会自动逐步刷新");
      }
      startPolling();
    } catch (e) {
      setMsg(`触发失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>盯盘</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <a href="/watchlist" style={linkStyle}>自选池</a>
          <button onClick={manualSnapshot} disabled={refreshing} style={primaryBtn}>
            {refreshing ? "抓取中…" : "手动抓取"}
          </button>
        </div>
      </header>

      {msg && (
        <div style={{ marginTop: 12, color: "#aaa", fontSize: 13 }}>{msg}</div>
      )}

      <div style={{ marginTop: 16, color: "#888", fontSize: 13 }}>
        共 {rows.length} 支
        <span style={{ marginLeft: 12 }}>红色 = 强信号</span>
      </div>

      <div className="table-scroll">
      <table style={tableStyle}>
        <thead>
          <tr style={{ color: "#888", fontSize: 12 }}>
            <th style={th}>代码</th>
            <th style={th}>名称</th>
            <th style={{ ...th, textAlign: "right" }}>价</th>
            <th style={{ ...th, textAlign: "right" }}>涨跌</th>
            <th style={{ ...th, textAlign: "right" }}>主力净流入</th>
            <th style={th}>信号</th>
            <th style={th}>操作建议</th>
            <th style={th}>更新</th>
            <th style={{ ...th, textAlign: "right" }}>详情</th>
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td colSpan={9} style={{ ...td, textAlign: "center", color: "#666" }}>
                加载中…
              </td>
            </tr>
          )}
          {!loading && rows.length === 0 && (
            <tr>
              <td colSpan={9} style={{ ...td, textAlign: "center", color: "#666" }}>
                自选池为空，先去
                <a href="/watchlist" style={{ color: "#3b82f6", marginLeft: 4 }}>
                  导入股票
                </a>
                ；导入后点右上角"手动抓取"或等下个整点
              </td>
            </tr>
          )}
          {rows.map((r) => (
            <tr key={r.code} style={r.has_strong_signal ? rowStrong : undefined}>
              <td style={{ ...td, fontFamily: "monospace" }}>
                <span style={{ color: "#666", marginRight: 4 }}>{exchangeLabel[r.exchange] || ""}</span>
                {r.code}
              </td>
              <td style={td}>{r.name}</td>
              <td style={{ ...td, textAlign: "right", fontFamily: "monospace" }}>
                {r.price != null ? r.price.toFixed(2) : "-"}
              </td>
              <td style={{
                ...td,
                textAlign: "right",
                fontFamily: "monospace",
                color: r.change_pct == null ? "#888" : r.change_pct >= 0 ? "#ef4444" : "#22c55e",
              }}>
                {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(2)}%` : "-"}
              </td>
              <td style={{ ...td, textAlign: "right", fontFamily: "monospace", color: "#aaa" }}>
                {fmtFlow(r.main_net_flow)}
              </td>
              <td style={td}>
                {r.signals.length === 0 ? (
                  <span style={{ color: "#444" }}>–</span>
                ) : (
                  r.signals.map((s) => (
                    <span key={s} style={signalChip(r.has_strong_signal && (s === "limit_up" || s === "limit_down" || s === "important_notice" || s === "lhb"))}>
                      {SIGNAL_LABEL[s] || s}
                    </span>
                  ))
                )}
              </td>
              <td style={td}>
                <ActionableCell analysis={r.analysis} />
              </td>
              <td style={{ ...td, color: "#666", fontSize: 12 }}>
                {r.last_ts ? new Date(r.last_ts).toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "numeric", day: "numeric" }) : "未抓取"}
              </td>
              <td style={{ ...td, textAlign: "right" }}>
                <a href={`/stocks/${r.code}`} style={{ color: "#3b82f6", fontSize: 12, textDecoration: "none" }}>
                  解析 →
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </main>
  );
}

function ActionableCell({ analysis }: { analysis: AnalysisBrief | null }) {
  if (!analysis || !analysis.actionable) {
    return <span style={{ color: "#444", fontSize: 12 }}>待生成</span>;
  }
  const style = ACTIONABLE_STYLE[analysis.actionable] ?? {
    color: "#9ca3af",
    label: analysis.actionable,
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 140 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            display: "inline-block",
            padding: "1px 6px",
            borderRadius: 3,
            background: hexA(style.color, 0.15),
            color: style.color,
            fontSize: 11,
            fontWeight: 600,
          }}
        >
          {style.label}
        </span>
        {!analysis.is_fresh && (
          <span style={{ color: "#666", fontSize: 10 }} title="生成时间已超过 4 小时">已过期</span>
        )}
      </div>
      {analysis.one_line_reason && (
        <span style={{ color: "#888", fontSize: 11, lineHeight: 1.4 }}>
          {analysis.one_line_reason}
        </span>
      )}
    </div>
  );
}

// "#rrggbb" → "rgba(r,g,b,a)" — for tinted chip backgrounds.
function hexA(hex: string, a: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function fmtFlow(yuan: number | null): string {
  if (yuan == null) return "-";
  const abs = Math.abs(yuan);
  const sign = yuan >= 0 ? "+" : "-";
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`;
  return `${sign}${abs.toFixed(0)}`;
}

const linkStyle: React.CSSProperties = {
  color: "#9ca3af",
  fontSize: 13,
  textDecoration: "none",
  padding: "6px 10px",
};
const primaryBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "#3b82f6",
  color: "white",
  border: "none",
  borderRadius: 6,
  fontSize: 13,
  cursor: "pointer",
};
const tableStyle: React.CSSProperties = {
  width: "100%",
  marginTop: 12,
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
const rowStrong: React.CSSProperties = {
  background: "rgba(239, 68, 68, 0.06)",
};
function signalChip(strong: boolean): React.CSSProperties {
  return {
    display: "inline-block",
    padding: "1px 6px",
    marginRight: 4,
    borderRadius: 3,
    background: strong ? "rgba(239, 68, 68, 0.2)" : "rgba(255,255,255,0.05)",
    color: strong ? "#fca5a5" : "#aaa",
    fontSize: 11,
  };
}
