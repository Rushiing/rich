"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { api, AnalysisBrief, StockRow } from "../../lib/api";

// While a snapshot job is running we re-pull /api/stocks at this cadence so
// rows surface as their data lands. 5s feels responsive without hammering.
const POLL_INTERVAL_MS = 5000;
// Hard cap so we eventually stop polling even if the status endpoint lies.
// Bumped from 5min → 15min: snapshot's per-stock akshare fan-out can hit
// 30s timeouts on flaky days, pushing total wall time past 5 min even on a
// 49-stock watchlist. With per-worker commit the user sees rows trickling
// in the whole time anyway, so a longer poll window doesn't waste anything.
const POLL_MAX_DURATION_MS = 15 * 60 * 1000;
// LLM batch grows ~linearly with watchlist size (~10s/code × N codes; with
// retries can be 2x). 20min covers ~100 codes worst case.
const ANALYSIS_POLL_MAX_DURATION_MS = 20 * 60 * 1000;

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

// Grouped view: split watchlist into act / wait / discard buckets so the
// user's eyes land on "what should I actually do today" first. Buy and
// sell live in their own groups (opposite intents — flattening into one
// "actionable" pile makes the user re-classify by hand). Watch and the
// 不入手+待生成 bucket together usually account for ~80% of a 50-stock
// list, so they default to collapsed.
type GroupKey = "buy" | "sell" | "watch" | "other";

const GROUP_DEFS: {
  key: GroupKey;
  label: string;
  color: string;
  defaultCollapsed: boolean;
}[] = [
  { key: "buy",   label: "建议买入",       color: "#ef4444", defaultCollapsed: false },
  { key: "sell",  label: "建议卖出",       color: "#22c55e", defaultCollapsed: false },
  { key: "watch", label: "观望",           color: "#9ca3af", defaultCollapsed: true  },
  { key: "other", label: "不入手 + 待生成", color: "#6b7280", defaultCollapsed: true  },
];

function groupOf(r: StockRow): GroupKey {
  const a = r.analysis?.actionable;
  if (a === "建议买入")    return "buy";
  if (a === "建议卖出")    return "sell";
  if (a === "观望")        return "watch";
  return "other"; // 不建议入手 / 待生成 / 未知 actionable 都归这里
}

export default function StocksPage() {
  const [rows, setRows] = useState<StockRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // null = show all rows; otherwise exact match against StockRow.analysis.actionable
  // (or "__pending" for rows that don't have a cached analysis yet).
  const [filter, setFilter] = useState<string | null>(null);
  // Per-group fold state. Initialized from GROUP_DEFS' defaults; only
  // applies when filter === null (the grouped view). Filter mode flattens.
  const [collapsed, setCollapsed] = useState<Record<GroupKey, boolean>>(
    () => Object.fromEntries(
      GROUP_DEFS.map((g) => [g.key, g.defaultCollapsed]),
    ) as Record<GroupKey, boolean>,
  );
  function toggleGroup(k: GroupKey) {
    setCollapsed((c) => ({ ...c, [k]: !c[k] }));
  }
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadline = useRef<number>(0);
  // Analysis batch polling is independent from snapshot polling — user may
  // legitimately have both running at once.
  const analysisPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const analysisPollDeadline = useRef<number>(0);

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
    api.batchAnalysisStatus().then((s) => {
      if (s.running) startAnalysisPolling();
    }).catch(() => {});
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      if (analysisPollTimer.current) clearInterval(analysisPollTimer.current);
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
        setMsg("抓取超过 15 分钟未结束，已停止前端轮询。已完成的股票仍会在 Railway 后端继续写入；刷新页面可查看最新数据。");
      }
    }, POLL_INTERVAL_MS);
  }

  // Filter only narrows the visible set; sort + strong-signal highlighting
  // were already applied server-side, so just keep that order.
  const visibleRows = filter === null
    ? rows
    : rows.filter((r) => {
        const a = r.analysis?.actionable ?? "";
        if (filter === "__pending") return !a;
        return a === filter;
      });

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

  function stopAnalysisPolling() {
    if (analysisPollTimer.current) {
      clearInterval(analysisPollTimer.current);
      analysisPollTimer.current = null;
    }
    setAnalyzing(false);
  }

  function startAnalysisPolling() {
    if (analysisPollTimer.current) return;
    setAnalyzing(true);
    analysisPollDeadline.current = Date.now() + ANALYSIS_POLL_MAX_DURATION_MS;
    analysisPollTimer.current = setInterval(async () => {
      try {
        await refresh({ silent: true });
        const status = await api.batchAnalysisStatus();
        if (!status.running) {
          stopAnalysisPolling();
          setMsg("批量解析完成");
          return;
        }
      } catch {
        // ignore transient poll errors
      }
      if (Date.now() > analysisPollDeadline.current) {
        stopAnalysisPolling();
        setMsg("批量解析超过 10 分钟未结束，已停止刷新；可在 Railway logs 查看后端");
      }
    }, POLL_INTERVAL_MS);
  }

  // 待生成 = no v2 analysis row yet (server-rendered as `analysis === null`).
  // The button's behavior switches on this count: fill them in if any exist,
  // otherwise fall through to "全部重新解析" (with a confirm to avoid
  // accidentally burning tokens on every code).
  const pendingCount = rows.filter((x) => !x.analysis).length;
  const strongCount = rows.filter((x) => x.has_strong_signal).length;

  // Bucket rows into the four groups while preserving server-side ordering
  // (strong-signal first, then |change_pct| desc).
  const groupedRows: Record<GroupKey, StockRow[]> = {
    buy: [], sell: [], watch: [], other: [],
  };
  for (const r of rows) groupedRows[groupOf(r)].push(r);

  async function batchAnalyze() {
    setMsg(null);
    const onlyMissing = pendingCount > 0;
    if (!onlyMissing) {
      const ok = window.confirm(
        `所有股票都已有解析。确认要全部重新解析 ${rows.length} 支吗？\n` +
        `这会调用 ${rows.length} 次 LLM API，约 ${Math.ceil(rows.length * 8 / 60)} 分钟、产生 token 费用。`,
      );
      if (!ok) return;
    }
    try {
      const r = await api.triggerBatchAnalysis({ onlyMissing });
      if (r.already_running) {
        setMsg("已有解析任务在进行中，正在跟随刷新");
      } else if (onlyMissing) {
        setMsg(`已开始解析 ${pendingCount} 支待生成，每支约 5–10 秒，会逐步刷新`);
      } else {
        setMsg(`已开始全部重新解析 ${rows.length} 支，每支约 5–10 秒，会逐步刷新`);
      }
      startAnalysisPolling();
    } catch (e) {
      setMsg(`触发失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <main style={{ padding: 20, maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>盯盘</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <a href="/watchlist" style={primaryLinkBtn}>自选池</a>
          <button onClick={batchAnalyze} disabled={analyzing} style={ghostBtn}>
            {analyzing
              ? "解析中…"
              : pendingCount > 0
                ? `批量解析 (${pendingCount})`
                : "全部重新解析"}
          </button>
          <button onClick={manualSnapshot} disabled={refreshing} style={ghostBtn}>
            {refreshing ? "抓取中…" : "手动抓取"}
          </button>
        </div>
      </header>

      {msg && (
        <div style={{ marginTop: 12, color: "#aaa", fontSize: 13 }}>{msg}</div>
      )}

      <ActionableFilter rows={rows} value={filter} onChange={setFilter} />

      <div style={{ marginTop: 8, color: "#888", fontSize: 13 }}>
        {filter === null ? `共 ${rows.length} 支` : `${visibleRows.length} / ${rows.length} 支`}
        {strongCount > 0 && (
          <span style={{ marginLeft: 12 }}>
            <span style={{ color: "#ef4444" }}>● </span>
            强信号 {strongCount}
          </span>
        )}
        {pendingCount > 0 && (
          <span style={{ marginLeft: 12, color: "#facc15" }}>
            待解析 {pendingCount}
          </span>
        )}
        {filter === null && (
          <span style={{ marginLeft: 12, color: "#666" }}>
            点分组标题可折叠
          </span>
        )}
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
          {!loading && rows.length > 0 && filter !== null && visibleRows.length === 0 && (
            <tr>
              <td colSpan={9} style={{ ...td, textAlign: "center", color: "#666" }}>
                当前筛选下没有股票
              </td>
            </tr>
          )}
          {/* Filter mode: flat list of whichever bucket is selected. */}
          {!loading && filter !== null && visibleRows.map(stockRow)}
          {/* Default mode: grouped, with collapsible non-act sections. */}
          {!loading && filter === null && GROUP_DEFS.map(({ key, label, color }) => {
            const groupRows = groupedRows[key];
            if (groupRows.length === 0) return null;
            const isCollapsed = collapsed[key];
            return (
              <Fragment key={key}>
                <tr
                  onClick={() => toggleGroup(key)}
                  style={{ cursor: "pointer", background: "#0a0a0a" }}
                >
                  <td colSpan={9} style={{
                    padding: "8px 10px",
                    borderTop: "1px solid #222",
                    borderBottom: "1px solid #222",
                    fontSize: 12,
                    userSelect: "none",
                  }}>
                    <span style={{ display: "inline-block", width: 14, color: "#666" }}>
                      {isCollapsed ? "▸" : "▾"}
                    </span>
                    <span style={{ color, fontWeight: 600, marginRight: 8 }}>
                      {label}
                    </span>
                    <span style={{ color: "#888" }}>({groupRows.length})</span>
                  </td>
                </tr>
                {!isCollapsed && groupRows.map(stockRow)}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      </div>
    </main>
  );
}

function ActionableFilter({
  rows,
  value,
  onChange,
}: {
  rows: StockRow[];
  value: string | null;
  onChange: (next: string | null) => void;
}) {
  // Count how many rows fall into each bucket so we can show "建议买入 (3)".
  // 0-count buckets stay clickable but visually muted — useful when waiting
  // for the next analysis pass to populate them.
  const counts: Record<string, number> = {
    "建议买入": 0, "观望": 0, "建议卖出": 0, "不建议入手": 0, __pending: 0,
  };
  for (const r of rows) {
    const a = r.analysis?.actionable;
    if (!a) counts.__pending += 1;
    else if (a in counts) counts[a] += 1;
    else counts.__pending += 1; // unknown enum value -> treat as pending
  }

  const buttons: { key: string | null; label: string; count: number }[] = [
    { key: null,           label: "全部",   count: rows.length },
    { key: "建议买入",     label: "买入",   count: counts["建议买入"] },
    { key: "观望",         label: "观望",   count: counts["观望"] },
    { key: "建议卖出",     label: "卖出",   count: counts["建议卖出"] },
    { key: "不建议入手",   label: "不入手", count: counts["不建议入手"] },
    { key: "__pending",    label: "待生成", count: counts.__pending },
  ];

  return (
    <div style={{ marginTop: 16, display: "flex", gap: 6, flexWrap: "wrap" }}>
      {buttons.map((b) => {
        const active = value === b.key;
        const themed = b.key && b.key !== "__pending" ? ACTIONABLE_STYLE[b.key] : null;
        const accent = themed?.color ?? "#9ca3af";
        return (
          <button
            key={b.key ?? "all"}
            onClick={() => onChange(b.key)}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              borderRadius: 14,
              border: `1px solid ${active ? accent : "#2a2a2a"}`,
              background: active ? hexA(accent, 0.18) : "transparent",
              color: active ? accent : b.count === 0 ? "#555" : "#aaa",
              cursor: "pointer",
              transition: "all 0.15s",
            }}
          >
            {b.label} {b.count > 0 && <span style={{ opacity: 0.7 }}>({b.count})</span>}
          </button>
        );
      })}
    </div>
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
  const flagCount = analysis.red_flags?.length ?? 0;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 180, maxWidth: 280 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
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
        {flagCount > 0 && (
          <span
            style={{
              display: "inline-block",
              padding: "1px 5px",
              borderRadius: 3,
              background: "rgba(239, 68, 68, 0.18)",
              color: "#fca5a5",
              fontSize: 10,
            }}
            title={analysis.red_flags.join(" / ")}
          >
            🔴 {flagCount}
          </span>
        )}
        {!analysis.is_fresh && (
          <span style={{ color: "#666", fontSize: 10 }} title="生成时间已超过 4 小时">已过期</span>
        )}
      </div>
      {analysis.company_tag && (
        <span style={{ color: "#9ca3af", fontSize: 11, lineHeight: 1.4 }}>
          {analysis.company_tag}
        </span>
      )}
      {analysis.one_line_reason && (
        <span style={{ color: "#777", fontSize: 11, lineHeight: 1.4 }}>
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

// Single-row renderer — extracted so both grouped and filter-flat modes
// share one source of truth.
function stockRow(r: StockRow) {
  return (
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
  );
}

function fmtFlow(yuan: number | null): string {
  if (yuan == null) return "-";
  const abs = Math.abs(yuan);
  const sign = yuan >= 0 ? "+" : "-";
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`;
  return `${sign}${abs.toFixed(0)}`;
}

// "自选池" is the primary CTA on the 盯盘 page now — paint it as the
// highlighted action; batch-analyze / manual-snapshot drop to ghost.
const primaryLinkBtn: React.CSSProperties = {
  display: "inline-block",
  padding: "6px 14px",
  background: "#3b82f6",
  color: "white",
  border: "none",
  borderRadius: 6,
  fontSize: 13,
  textDecoration: "none",
  cursor: "pointer",
  fontWeight: 500,
};
const ghostBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "transparent",
  color: "#aaa",
  border: "1px solid #333",
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
