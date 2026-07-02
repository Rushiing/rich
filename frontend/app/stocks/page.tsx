"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { api, ActionItemsOut, AnalysisBrief, FunnelStateOut, HitRateSummary, StockRow, confidenceBucket, confidenceLabel } from "../../lib/api";
import { HolderStance, holderStanceFor, setFunnelState, reportFunnelChoice } from "../../lib/holdingFunnel";
import Tooltip from "../_components/Tooltip";
import { groupByBoard } from "../../lib/market";
import { SegmentHeader } from "../_components/SegmentSection";

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
// Background auto-refresh: re-pull /api/stocks every 30s during trading
// hours so the user sees quotes_5min results without hitting reload.
// 30s = quotes_5min × 1/10, so a fresh tick lands within 30s in the worst
// case. Skipped when the tab is hidden, off-hours, or user-triggered
// pollers are already in flight (avoids double-fetch).
const AUTO_REFRESH_INTERVAL_MS = 30_000;

// Maps the LLM's structured `actionable` enum to a (color, short label) pair.
// A股语境：红=买/涨，绿=卖/跌；中性观望灰色。
const ACTIONABLE_STYLE: Record<string, { color: string; label: string }> = {
  "建议买入":   { color: "#ef4444", label: "买" },
  "观望":       { color: "#9ca3af", label: "观望" },
  "建议卖出":   { color: "#22c55e", label: "卖" },
  "不建议入手": { color: "#6b7280", label: "不入" },
};

// Tooltip blurbs for each `actionable` value. AI-generated; kept short.
const ACTIONABLE_EXPLAIN: Record<string, string> = {
  "建议买入":   "AI 综合估值、资金、技术、消息面后给出的入场建议。",
  "观望":       "信号不充分或风险未消化，建议先按兵不动。",
  "建议卖出":   "估值偏贵、动能转弱或基本面恶化，建议减仓或离场。",
  "不建议入手": "命中负面规则（如 ST、业绩塌方等），不在可选池里。",
};

// Confidence chip palette. Three buckets, mapped from confidenceBucket():
//   high → 绿 (有把握)
//   med  → 灰 (中等)
//   low  → 橙 (信号弱)
// Kept muted (low-saturation backgrounds) so they don't compete visually
// with the actionable badge — which is the primary signal.
const CONFIDENCE_BG: Record<"high" | "med" | "low", string> = {
  high: "rgba(34, 197, 94, 0.15)",
  med:  "rgba(156, 163, 175, 0.15)",
  low:  "rgba(245, 158, 11, 0.18)",
};
const CONFIDENCE_FG: Record<"high" | "med" | "low", string> = {
  high: "#22c55e",
  med:  "#9ca3af",
  low:  "#f59e0b",
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

// 7/2 持仓立场轴:标签改成双受众读法 — 前半是持仓者动作(默认),
// 后半是未持仓者动作(显式标未持的行)。
const GROUP_DEFS: {
  key: GroupKey;
  label: string;
  color: string;
  defaultCollapsed: boolean;
}[] = [
  { key: "sell",  label: "减仓/离场 · 卖出", color: "#22c55e", defaultCollapsed: false },
  { key: "buy",   label: "持有/加仓 · 买入", color: "#ef4444", defaultCollapsed: false },
  { key: "watch", label: "持有观望 · 观望",  color: "#9ca3af", defaultCollapsed: true  },
  { key: "other", label: "不入手 + 待生成",  color: "#6b7280", defaultCollapsed: true  },
];

// 7/2 持仓立场轴:每行的"派生结论"。盯盘池的票默认视为已持仓(Rush 拍板,
// 不揣测盈亏 → 取 holding_small 象限的立场),显式标了未持仓(服务端漏斗
// held=false)或 legacy 行(无 holder_direction)回落买家视角 actionable。
// chip / 分组 / 筛选三处都从这一个派生走,保证轴一致。
type RowVerdict =
  | { kind: "holder"; stance: HolderStance; advice: string | null }
  | { kind: "actionable"; value: string }
  | { kind: "pending" };

function rowVerdict(r: StockRow, serverHeld?: boolean): RowVerdict {
  const a = r.analysis;
  if (!a || !a.actionable) return { kind: "pending" };
  const held = serverHeld !== false; // undefined = 没标过 → 默认持仓
  const stance = held ? holderStanceFor(a.holder_direction) : null;
  if (stance) return { kind: "holder", stance, advice: a.holder_advice ?? null };
  return { kind: "actionable", value: a.actionable };
}

function groupOf(r: StockRow, serverHeld?: boolean): GroupKey {
  const v = rowVerdict(r, serverHeld);
  if (v.kind === "holder") {
    if (v.stance.direction === "看空") return "sell";
    if (v.stance.direction === "看多") return "buy";
    return "watch";
  }
  if (v.kind === "actionable") {
    if (v.value === "建议买入") return "buy";
    if (v.value === "建议卖出") return "sell";
    if (v.value === "观望")     return "watch";
  }
  return "other"; // 不建议入手(未持仓行) / 待生成 / 未知 都归这里
}

const DEFAULT_COLLAPSED: Record<GroupKey, boolean> = Object.fromEntries(
  GROUP_DEFS.map((g) => [g.key, g.defaultCollapsed]),
) as Record<GroupKey, boolean>;

// 市场为外层后,买卖分组退成每个 market 区内部的中性次序。把一段 rows
// 按派生结论分组(starred 置顶),供市场区内复用。
function actionableGroups(
  segRows: StockRow[],
  heldOf: (code: string) => boolean | undefined,
): Record<GroupKey, StockRow[]> {
  const g: Record<GroupKey, StockRow[]> = { buy: [], sell: [], watch: [], other: [] };
  for (const r of segRows) g[groupOf(r, heldOf(r.code))].push(r);
  for (const k of Object.keys(g) as GroupKey[]) {
    g[k].sort((a, b) => Number(b.starred) - Number(a.starred));
  }
  return g;
}

// A股 continuous-trading window check, locked to Asia/Shanghai regardless
// of where the user's browser thinks it is. Padded ±5min around each
// session edge so the post-9:30-open and post-15:00-close cron writes
// (which can land a few seconds to a minute late) actually surface in
// the UI before auto-refresh stops. So:
//   morning:   09:25 – 11:35
//   afternoon: 12:55 – 15:05
function isTradingTimeShanghai(): boolean {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai",
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  const dow = parts.weekday;
  if (dow === "Sat" || dow === "Sun") return false;
  const h = parseInt(parts.hour, 10);
  const m = parseInt(parts.minute, 10);
  const morning   = (h === 9 && m >= 25) || h === 10 || (h === 11 && m <= 35);
  const afternoon = (h === 12 && m >= 55) || h === 13 || h === 14 || (h === 15 && m <= 5);
  return morning || afternoon;
}

export default function StocksPage() {
  const [rows, setRows] = useState<StockRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  // Last-refresh error, separate from `msg`. `msg` is for action feedback
  // ("已开始抓取" etc.); `loadError` is "the most recent data refresh
  // failed". Keeping them separate so an action toast doesn't overwrite
  // the persistent "list is stale" banner, and vice versa.
  const [loadError, setLoadError] = useState<string | null>(null);

  // 6/3: 全局 hit_rate summary — passed to each ActionableCell so its
  // tooltip can show "AI 历史命中率 X% (n=Y)" for buy/sell verdicts.
  // Single mount-time fetch; backend caches 30 min so this is cheap.
  // Silent failure: hit_rate is supplementary, shouldn't drag the page
  // down if outcomes service is unhealthy.
  const [hitRate, setHitRate] = useState<HitRateSummary | null>(null);
  // ③ 跨设备:用户每只票的服务端最新漏斗选择(给 HeldToggle hydrate,跟账号走)。
  const [funnelMap, setFunnelMap] = useState<Record<string, FunnelStateOut> | null>(null);
  // S1 (6/10): 今日需行动 — holdings-aware sell triggers. Refreshed
  // alongside the row list (refresh()) so a stop-loss breach shows up
  // within one auto-refresh cycle. Silent failure: supplementary.
  const [actionItems, setActionItems] = useState<ActionItemsOut | null>(null);
  // null = show all rows; otherwise a derived-verdict GroupKey (7/2 持仓
  // 立场轴 — 跟分组/chip 同一个派生),or "__pending" for rows without a
  // cached analysis yet.
  const [filter, setFilter] = useState<string | null>(null);
  // Per-group fold state. Initialized from GROUP_DEFS' defaults; only
  // applies when filter === null (the grouped view). Filter mode flattens.
  // Key is now `${board}:${actionable}` (市场为外层后,折叠按市场×买卖独立)。
  // 空初始 → 默认折叠态由 actionable 的 defaultCollapsed 兜底(见渲染处)。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  function toggleGroup(ck: string, current: boolean) {
    setCollapsed((c) => ({ ...c, [ck]: !current }));
  }
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollDeadline = useRef<number>(0);
  // Analysis batch polling is independent from snapshot polling — user may
  // legitimately have both running at once.
  const analysisPollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const analysisPollDeadline = useRef<number>(0);

  async function refresh(opts: { silent?: boolean } = {}) {
    if (!opts.silent) setLoading(true);
    // Piggyback action items on every row refresh — same cadence, no
    // extra timer. Fire-and-forget so a failure can't block the rows.
    api.actionItems().then(setActionItems).catch(() => {});
    try {
      setRows(await api.listStocks());
      setLoadError(null);  // success clears any prior failure banner
    } catch (e) {
      // Critical: WITHOUT this catch the exception flies past the try/
      // finally, `rows` stays at its initial [], and the empty-state UI
      // says "自选池为空" — misleading users into thinking their data
      // is gone when actually the backend is broken. Keep stale rows
      // so they still see the previous good snapshot; surface the error
      // as a banner above the table.
      const msg = e instanceof Error ? e.message : String(e);
      setLoadError(msg);
      // Do NOT setRows([]) — keep the last good data on screen.
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
    // 6/3: hit_rate summary — single mount fetch, silent failure (it's
    // supplementary tooltip content, not blocking).
    api.hitRateSummary().then(setHitRate).catch(() => {});
    // ③ 跨设备:拉我的全部最新漏斗选择(静默失败)。
    api.getMyFunnel().then(setFunnelMap).catch(() => {});
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

  // Background auto-refresh during trading hours so quotes_5min results
  // surface without manual reload. Guarded so it doesn't fire when:
  //   - the tab is hidden (no point burning bandwidth in the background)
  //   - we're outside the A股 continuous-trading window
  //   - a user-triggered poller (snapshot or batch analysis) already runs
  //     — those poll on a faster cadence and would otherwise double-fetch
  useEffect(() => {
    const tick = () => {
      if (typeof document === "undefined") return;
      if (document.visibilityState !== "visible") return;
      if (!isTradingTimeShanghai()) return;
      if (pollTimer.current || analysisPollTimer.current) return;
      refresh({ silent: true }).catch(() => {});
    };
    const id = setInterval(tick, AUTO_REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
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
  const heldOf = (code: string) => funnelMap?.[code]?.held;
  // HeldToggle 点选 → 乐观更新 funnelMap,chip/分组/筛选立即重派生
  // (否则要刷新页面才生效 — funnelMap 只在挂载时拉一次)。服务端上报
  // 由 HeldToggle 内部的 reportFunnelChoice 负责,这里只管本地视图。
  function onHeldChange(code: string, held: boolean) {
    setFunnelMap((m) => ({
      ...(m ?? {}),
      [code]: { held, pnl: m?.[code]?.pnl ?? null, tier: m?.[code]?.tier ?? "aggressive" },
    }));
  }
  const visibleRows = filter === null
    ? rows
    : rows.filter((r) => {
        const a = r.analysis?.actionable ?? "";
        if (filter === "__pending") return !a;
        return !!a && groupOf(r, heldOf(r.code)) === filter;
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
  const starredCount = rows.filter((x) => x.starred).length;

  // Optimistic toggle: flip locally first so the star reacts in <16ms,
  // then call the API. If the call fails roll back. Background poller
  // will eventually rectify any drift either way.
  async function toggleStar(code: string) {
    const before = rows.find((r) => r.code === code)?.starred ?? false;
    setRows((prev) => prev.map((r) =>
      r.code === code ? { ...r, starred: !before } : r,
    ));
    try {
      const res = await api.toggleStar(code);
      // Server is the truth — re-apply in case of races.
      setRows((prev) => prev.map((r) =>
        r.code === code ? { ...r, starred: res.starred } : r,
      ));
      // Re-pull list so server-side ordering (starred first) takes effect.
      refresh({ silent: true }).catch(() => {});
    } catch (e) {
      // Revert the optimistic flip AND surface the failure so the user
      // doesn't see a "phantom UI glitch" (star changed then jumped back).
      setRows((prev) => prev.map((r) =>
        r.code === code ? { ...r, starred: before } : r,
      ));
      setMsg(`星标操作失败：${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // 默认视图的分桶移到渲染处(市场为外层 → 每个市场区内再按 actionable 分,
  // 见 actionableGroups)。这里只保留筛选态的 starred 置顶。
  // Filter mode: same starred-first treatment so the flat list also reacts.
  const visibleRowsSorted = filter === null
    ? visibleRows
    : [...visibleRows].sort((a, b) => Number(b.starred) - Number(a.starred));

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
    <main style={{ padding: 20, maxWidth: 1600, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>盯盘</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
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
        <div style={{ marginTop: 12, color: "var(--text-soft)", fontSize: 13 }}>{msg}</div>
      )}
      {loadError && (
        <div style={{
          marginTop: 12,
          padding: "8px 12px",
          border: "1px solid #b91c1c",
          background: "rgba(185, 28, 28, 0.08)",
          borderRadius: 6,
          color: "#dc2626",
          fontSize: 13,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
        }}>
          <span>列表加载失败:{loadError}{rows.length > 0 ? "(下方为上次成功的数据)" : ""}</span>
          <button
            type="button"
            onClick={() => refresh()}
            style={{
              background: "transparent",
              border: "1px solid #dc2626",
              color: "#dc2626",
              borderRadius: 4,
              padding: "2px 10px",
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            重试
          </button>
        </div>
      )}

      {/* S1 (6/10): 今日需行动 — 持仓票的卖出触发,放在所有内容之上。
          只在有 item 时渲染;没有持仓或一切正常时整个区块不出现。 */}
      <ActionItemsBanner data={actionItems} />

      {/* 6/3: AI 历史命中率 banner — 集中展示买/卖两类的全局命中率,
          胜过在每行 tooltip 里重复同一个数字。带数据口径说明和样本量
          透明度,降低用户对 AI 建议的盲信门槛。 */}
      <HitRateBanner hitRate={hitRate} />

      <ActionableFilter rows={rows} value={filter} onChange={setFilter} heldOf={heldOf} />

      <div style={{ marginTop: 8, color: "var(--text-muted)", fontSize: 13 }}>
        {filter === null ? `共 ${rows.length} 支` : `${visibleRows.length} / ${rows.length} 支`}
        {starredCount > 0 && (
          <span style={{ marginLeft: 12 }}>
            <span style={{ color: "#facc15" }}>★ </span>
            特别关注 {starredCount}
          </span>
        )}
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
          <span style={{ marginLeft: 12, color: "var(--text-faint)" }}>
            点分组标题可折叠
          </span>
        )}
      </div>

      <div className="table-scroll">
      <table style={tableStyle}>
        <thead>
          <tr style={{ color: "var(--text-muted)", fontSize: 12 }}>
            <th style={{ ...th, width: 28 }} aria-label="特别关注"></th>
            <th style={th}>代码</th>
            <th style={th}>名称</th>
            <th style={{ ...th, textAlign: "right" }}>现价</th>
            <th style={{ ...th, textAlign: "right" }}>今日</th>
            <th style={{ ...th, textAlign: "right" }}>3日涨幅</th>
            <th style={{ ...th, textAlign: "right" }}>3日换手</th>
            <th style={{ ...th, textAlign: "right" }}>3日净流入</th>
            <th style={th}>行业 / 水位</th>
            <th style={th}>操作建议</th>
            <th style={th}>更新</th>
            <th style={{ ...th, textAlign: "right" }}>详情</th>
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td colSpan={12} style={{ ...td, textAlign: "center", color: "var(--text-faint)" }}>
                加载中…
              </td>
            </tr>
          )}
          {/* Empty-state ONLY when we're confident the user has no rows
              (not when the fetch failed — that case shows the loadError
              banner above instead, so we don't mislead with "自选池为空"). */}
          {!loading && rows.length === 0 && !loadError && (
            <tr>
              <td colSpan={12} style={{ ...td, textAlign: "center", color: "var(--text-faint)" }}>
                自选池为空，先去
                <a href="/watchlist" style={{ color: "var(--link)", marginLeft: 4 }}>
                  导入股票
                </a>
                ；导入后点右上角"手动抓取"或等下个整点
              </td>
            </tr>
          )}
          {!loading && rows.length > 0 && filter !== null && visibleRows.length === 0 && (
            <tr>
              <td colSpan={12} style={{ ...td, textAlign: "center", color: "var(--text-faint)" }}>
                当前筛选下没有股票
              </td>
            </tr>
          )}
          {/* Filter mode: flat list of whichever bucket is selected. */}
          {!loading && filter !== null && visibleRowsSorted.map((r) => stockRow(r, toggleStar, funnelMap?.[r.code]?.held, onHeldChange))}
          {/* Default mode: 市场为外层(科创板 teal 摘出),买卖分组退到每个
              市场区内部的中性折叠次序。强调权给市场维度,符合「一次只强调一个
              维度」规范。行模板(stockRow)不变。 */}
          {!loading && filter === null && groupByBoard(rows, (r) => r.code).map((seg) => (
            <Fragment key={seg.board}>
              <SegmentHeader as="row" colSpan={12} board={seg.board} count={seg.items.length} />
              {(() => {
                const segGroups = actionableGroups(seg.items, heldOf);
                return GROUP_DEFS.map(({ key, label }) => {
                  const groupRows = segGroups[key];
                  if (groupRows.length === 0) return null;
                  const ck = `${seg.board}:${key}`;
                  const isCollapsed = collapsed[ck] ?? DEFAULT_COLLAPSED[key];
                  return (
                    <Fragment key={ck}>
                      <tr
                        onClick={() => toggleGroup(ck, isCollapsed)}
                        style={{ cursor: "pointer", background: "var(--bg)" }}
                      >
                        <td colSpan={12} style={{
                          padding: "6px 10px 6px 22px",
                          borderBottom: "1px solid var(--border-faint)",
                          fontSize: 12,
                          userSelect: "none",
                        }}>
                          <span style={{ display: "inline-block", width: 14, color: "var(--text-faint)" }}>
                            {isCollapsed ? "▸" : "▾"}
                          </span>
                          <span style={{ color: "var(--text-soft)", marginRight: 8 }}>
                            {label}
                          </span>
                          <span style={{ color: "var(--text-muted)" }}>({groupRows.length})</span>
                        </td>
                      </tr>
                      {!isCollapsed && groupRows.map((r) => stockRow(r, toggleStar, funnelMap?.[r.code]?.held, onHeldChange))}
                    </Fragment>
                  );
                });
              })()}
            </Fragment>
          ))}
        </tbody>
      </table>
      </div>
    </main>
  );
}

// 6/3: 集中展示 AI 历史命中率 — 列表顶部 banner. 替代了之前在每行
// tooltip 里重复同一全局数字的设计 (噪音 + 不一目了然)。详情页保留
// per-stock 上下文展示。silent 处理 null/loading — 数据没回来时
// 不渲染整个 banner,不阻塞主列表。
// S1 (6/10): 今日需行动 banner — the no-push spec's push surrogate.
// urgent = red left bar (跌破止损 / 建议卖出 / 看跌强信号), warn = amber
// (有效期失效 / 其他新信号). Each row links to the detail page where the
// user can re-generate or act.
const ACTION_TYPE_LABEL: Record<string, string> = {
  stop_loss_breach: "跌破止损",
  sell_verdict: "建议卖出",
  sell_stance: "持仓看空",
  valid_window_expired: "建议已过期",
  signal_alert: "新强信号",
};

function ActionItemsBanner({ data }: { data: ActionItemsOut | null }) {
  if (!data || data.items.length === 0) return null;
  const urgentCount = data.items.filter((i) => i.severity === "urgent").length;
  return (
    <section style={{
      marginTop: 14,
      border: "1px solid #7f1d1d",
      borderRadius: 8,
      overflow: "hidden",
      background: "rgba(127, 29, 29, 0.12)",
    }}>
      <div style={{
        padding: "10px 14px",
        fontSize: 13,
        fontWeight: 600,
        color: "#fca5a5",
        display: "flex",
        alignItems: "center",
        gap: 8,
      }}>
        <span>⚠️ 今日需行动</span>
        <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>
          按持仓检查 {data.checked_holdings} 支,{data.items.length} 条触发
          {urgentCount > 0 && `（紧急 ${urgentCount}）`}
        </span>
      </div>
      <div>
        {data.items.map((it, i) => (
          <a
            key={`${it.code}-${it.type}-${i}`}
            href={`/stocks/${it.code}`}
            style={{
              display: "flex",
              gap: 10,
              padding: "9px 14px",
              borderTop: "1px solid rgba(127, 29, 29, 0.35)",
              borderLeft: `3px solid ${it.severity === "urgent" ? "#ef4444" : "#f59e0b"}`,
              textDecoration: "none",
              alignItems: "baseline",
            }}
          >
            <span style={{ whiteSpace: "nowrap", fontSize: 13, color: "var(--text)" }}>
              {it.name}
              <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 12, marginLeft: 4 }}>
                {it.code}
              </span>
            </span>
            <span style={{
              whiteSpace: "nowrap",
              fontSize: 11,
              padding: "1px 6px",
              borderRadius: 4,
              background: it.severity === "urgent" ? "rgba(239,68,68,0.18)" : "rgba(245,158,11,0.15)",
              color: it.severity === "urgent" ? "#fca5a5" : "#fcd34d",
            }}>
              {ACTION_TYPE_LABEL[it.type] ?? it.type}
            </span>
            <span style={{ fontSize: 12.5, color: "var(--text-soft)", lineHeight: 1.45 }}>
              {it.message}
            </span>
          </a>
        ))}
      </div>
    </section>
  );
}


function HitRateBanner({ hitRate }: { hitRate: HitRateSummary | null }) {
  if (!hitRate) return null;
  const buy = hitRate.by_actionable["建议买入"];
  const sell = hitRate.by_actionable["建议卖出"];
  if (!buy && !sell) return null;

  // S2 (6/10) 口径升级:大数字用去重命中率(按 code+日取末锚,剥掉盘中
  // 重复解析的聚类膨胀),配同日基线超额(剥市场 beta)。买入超额为正/
  // 卖出超额为负 = 真选股区分度,绿色;否则琥珀色提示"主要是行情"。
  const StatCard = ({
    label, bucket, color, isBuy,
  }: {
    label: string;
    bucket: HitRateSummary["by_actionable"][string] | undefined;
    color: string;
    isBuy: boolean;
  }) => {
    if (!bucket || bucket.hit_rate == null) return null;
    const rate = bucket.hit_rate_dedup ?? bucket.hit_rate;
    const nShown = bucket.n_unique ?? bucket.n;
    const excess = bucket.excess_return_d5;
    const excessSupports =
      excess != null && (isBuy ? excess > 1 : excess < -1);
    return (
      <div style={{
        flex: 1,
        minWidth: 200,
        padding: "12px 16px",
        background: "var(--surface)",
        border: "1px solid var(--border)",
        borderLeft: `3px solid ${color}`,
        borderRadius: 6,
      }}>
        <div style={{
          color: "var(--text-muted)", fontSize: 12, marginBottom: 4,
        }}>
          {label}
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          <span style={{
            fontSize: 28, fontWeight: 700, color,
            fontFamily: "monospace", lineHeight: 1,
          }}>
            {rate.toFixed(1)}%
          </span>
          <span style={{ color: "var(--text-soft)", fontSize: 12 }}>
            去重 n={nShown}{nShown < 30 ? " · 偏小" : ""}
          </span>
          {excess != null && (
            <span style={{ fontSize: 12, color: excessSupports ? "#22c55e" : "#f59e0b" }}>
              5日超额 {excess >= 0 ? "+" : ""}{excess.toFixed(1)}%
            </span>
          )}
        </div>
      </div>
    );
  };

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{
        display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6,
      }}>
        <span style={{
          color: "var(--text-muted)", fontSize: 12, fontWeight: 600,
        }}>
          AI 历史命中率
        </span>
        <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
          基于单股解析历史 · 共 {hitRate.total_scored} 条已结算样本
        </span>
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <StatCard label="建议买入" bucket={buy} color="#ef4444" isBuy />
        <StatCard label="建议卖出" bucket={sell} color="#22c55e" isBuy={false} />
      </div>
      <div style={{
        marginTop: 6,
        color: "var(--text-faint)", fontSize: 11, lineHeight: 1.5,
      }}>
        口径:建议买入命中 = 5 个交易日后股价上涨;建议卖出命中 = 5 个交易日后股价下跌。
        样本 &lt; 30 时数字噪音较大,仅作参考。
      </div>
    </div>
  );
}

function ActionableFilter({
  rows,
  value,
  onChange,
  heldOf,
}: {
  rows: StockRow[];
  value: string | null;
  onChange: (next: string | null) => void;
  heldOf: (code: string) => boolean | undefined;
}) {
  // 7/2 持仓立场轴:筛选桶与分组/chip 同一个派生(groupOf),不再按裸
  // actionable 匹配 — 否则 chip 显"减仓/离场"的行点"卖出"筛不出来。
  // 0-count buckets stay clickable but visually muted.
  const counts: Record<string, number> = {
    buy: 0, sell: 0, watch: 0, other: 0, __pending: 0,
  };
  for (const r of rows) {
    if (!r.analysis?.actionable) counts.__pending += 1;
    else counts[groupOf(r, heldOf(r.code))] += 1;
  }

  const GROUP_COLOR: Record<string, string> = Object.fromEntries(
    GROUP_DEFS.map((g) => [g.key, g.color]),
  );
  const buttons: { key: string | null; label: string; count: number }[] = [
    { key: null,         label: "全部",        count: rows.length },
    { key: "sell",       label: "减仓·卖出",   count: counts.sell },
    { key: "buy",        label: "加仓·买入",   count: counts.buy },
    { key: "watch",      label: "观望",        count: counts.watch },
    { key: "other",      label: "不入手",      count: counts.other },
    { key: "__pending",  label: "待生成",      count: counts.__pending },
  ];

  return (
    <div style={{ marginTop: 16, display: "flex", gap: 6, flexWrap: "wrap" }}>
      {buttons.map((b) => {
        const active = value === b.key;
        const accent = (b.key && b.key !== "__pending" ? GROUP_COLOR[b.key] : null) ?? "#9ca3af";
        return (
          <button
            key={b.key ?? "all"}
            onClick={() => onChange(b.key)}
            style={{
              padding: "4px 10px",
              fontSize: 12,
              borderRadius: 14,
              border: `1px solid ${active ? accent : "var(--border)"}`,
              background: active ? hexA(accent, 0.18) : "transparent",
              color: active ? accent : b.count === 0 ? "var(--text-faint)" : "var(--text-soft)",
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

function ActionableCell({ analysis, serverHeld }: { analysis: AnalysisBrief | null; serverHeld?: boolean }) {
  if (!analysis || !analysis.actionable) {
    return <span style={{ color: "var(--text-dim)", fontSize: 12 }}>待生成</span>;
  }
  // 7/2 持仓立场轴:默认持仓 → 主 chip 显持仓者立场(减仓/离场 等),
  // tooltip 里保留未持仓视角的 actionable;显式标未持/legacy 行沿用旧 chip。
  const held = serverHeld !== false;
  const stance = held ? holderStanceFor(analysis.holder_direction) : null;
  const style = stance
    ? { color: stance.color, label: stance.short }
    : ACTIONABLE_STYLE[analysis.actionable] ?? {
        color: "#9ca3af",
        label: analysis.actionable,
      };
  const flagCount = analysis.red_flags?.length ?? 0;
  const explainBlurb = ACTIONABLE_EXPLAIN[analysis.actionable] ?? "";
  // 5/28: low-confidence visual degradation. Only applied when the verdict
  // is directional (买/卖 or 持仓立场的看多/看空) — observing/不入手 are
  // already "no action" so a confidence bucket doesn't change their
  // weight visually. Dashed border + reduced opacity is the signal:
  // "the model said this but isn't sure, slow down".
  const confBucket = confidenceBucket(analysis.confidence);
  const isActionable = stance
    ? stance.direction !== "中性"
    : analysis.actionable === "建议买入" || analysis.actionable === "建议卖出";
  const degraded = confBucket === "low" && isActionable;
  return (
    // Wider on desktop (ultrawide-friendly), still capped so a single line of
    // 操作建议 doesn't run forever on huge screens. minWidth keeps the cell
    // from collapsing on mobile.
    <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 180, maxWidth: 480 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <Tooltip
          content={
            <div>
              {stance ? (
                <>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>
                    持仓者视角:{stance.label}
                  </div>
                  {analysis.holder_advice && (
                    <div style={{ color: "var(--text-soft)" }}>{analysis.holder_advice}</div>
                  )}
                  <div style={{ marginTop: 4, color: "var(--text-faint)", fontSize: 11 }}>
                    未持仓视角:{analysis.actionable}。盯盘池默认按已持仓展示,
                    点名称旁的「持有/未持」可切换。
                  </div>
                </>
              ) : (
                <>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{analysis.actionable}</div>
                  <div style={{ color: "var(--text-soft)" }}>{explainBlurb}</div>
                </>
              )}
              {/* 6/3: 全局历史命中率 moved to top-of-list HitRateBanner
                  (per-row tooltip 重复同一数字是噪音). 这里只留结论
                  解释 + 低置信警告。 */}
              {degraded && (
                <div style={{ marginTop: 4, color: "#f59e0b", fontSize: 11 }}>
                  ⚠️ 低置信，慎跟
                </div>
              )}
            </div>
          }
        >
          <span
            style={{
              display: "inline-block",
              padding: "1px 6px",
              borderRadius: 3,
              background: hexA(style.color, degraded ? 0.08 : 0.15),
              color: style.color,
              fontSize: 11,
              fontWeight: 600,
              opacity: degraded ? 0.65 : 1,
              border: degraded ? `1px dashed ${style.color}` : "none",
            }}
          >
            {style.label}
          </span>
        </Tooltip>
        {analysis.confidence != null && (
          <Tooltip
            content={
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  模型自评置信度：{typeof analysis.confidence === "number"
                    ? `${analysis.confidence} / 100`
                    : analysis.confidence}
                </div>
                {analysis.confidence_reason && (
                  <div style={{ color: "var(--text-soft)" }}>
                    {analysis.confidence_reason}
                  </div>
                )}
              </div>
            }
          >
            <span
              style={{
                display: "inline-block",
                padding: "1px 5px",
                borderRadius: 3,
                background: CONFIDENCE_BG[confBucket],
                color: CONFIDENCE_FG[confBucket],
                fontSize: 10,
                fontWeight: 600,
              }}
            >
              {confidenceLabel(analysis.confidence)}
            </span>
          </Tooltip>
        )}
        {flagCount > 0 && (
          <Tooltip
            content={
              <div>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>检测到 {flagCount} 项风险</div>
                <ul style={{ margin: 0, paddingLeft: 16, color: "var(--text-soft)" }}>
                  {analysis.red_flags.map((f, i) => (
                    <li key={i} style={{ marginBottom: 2 }}>{f}</li>
                  ))}
                </ul>
              </div>
            }
          >
            <span
              style={{
                display: "inline-block",
                padding: "1px 5px",
                borderRadius: 3,
                background: "rgba(239, 68, 68, 0.18)",
                color: "#fca5a5",
                fontSize: 10,
              }}
            >
              🔴 {flagCount}
            </span>
          </Tooltip>
        )}
        {!analysis.is_fresh && (
          <Tooltip content="解析生成已超过 4 小时，建议在详情页点 '重新生成' 拿最新版本。">
            <span style={{ color: "var(--text-faint)", fontSize: 10 }}>已过期</span>
          </Tooltip>
        )}
      </div>
      {analysis.company_tag && (
        <span style={{ color: "var(--text)", fontSize: 13, fontWeight: 600, lineHeight: 1.5 }}>
          {analysis.company_tag}
        </span>
      )}
      {analysis.one_line_reason && (
        <span style={{ color: "var(--text-soft)", fontSize: 12, lineHeight: 1.5 }}>
          {analysis.one_line_reason}
        </span>
      )}
      {/* 6/3: valid_window 透出到列表 — 用户反馈在详情页只在
          actionable 旁不够,列表也要一眼看到决策窗口。橙色高亮跟其它
          灰字理由拉开层级,告诉用户"这个建议什么时候作废"。 */}
      {analysis.valid_window && (
        <span style={{
          marginTop: 2,
          alignSelf: "flex-start",
          padding: "2px 7px",
          borderRadius: 3,
          background: "rgba(245, 158, 11, 0.12)",
          border: "1px solid rgba(245, 158, 11, 0.35)",
          color: "#f59e0b",
          fontSize: 12,
          fontWeight: 500,
          lineHeight: 1.4,
          maxWidth: "100%",
          whiteSpace: "normal",
        }}>
          ⏱ 参考时效 {analysis.valid_window}
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

// 列表行内的轻量「持/未」点选 —— 写同一份 localStorage 的 held 位
// （详情页漏斗共用）。列表只捕获"持有吗"这一个最便宜、最高含金量的 bit，
// 盈亏/风险留到详情页。SSR 安全：初值在挂载后才从 localStorage 读，避免
// 服务端渲染与客户端不一致（hydration mismatch）。
function HeldToggle({ code, serverHeld, onChange }: {
  code: string;
  serverHeld?: boolean;
  onChange?: (code: string, held: boolean) => void;
}) {
  // null = 尚未读出（SSR / 首帧），不上色。
  // ③ 跨设备:服务端有该票最新选择(serverHeld 非 undefined)→ 以**账号**为准、
  // 同步回 localStorage;否则探 localStorage。
  // 7/2 持仓立场轴:没标过的票默认视为持有(Rush 拍板 — 盯盘池绝大比例是
  // 持仓票),与操作建议 chip 的默认持仓口径保持一致;显式标记过的用高亮
  // 区分(红=确认持有),默认态弱化显示。
  const [held, setHeld] = useState<boolean | null>(null);
  const [explicit, setExplicit] = useState(false);
  useEffect(() => {
    if (serverHeld !== undefined) {
      setHeld(serverHeld);
      setExplicit(true);
      try { setFunnelState(code, { held: serverHeld }); } catch { /* noop */ }
      return;
    }
    try {
      const raw = window.localStorage.getItem(`rich:funnel:${code}`);
      if (raw) {
        setHeld(JSON.parse(raw).held === true);
        setExplicit(true);
      } else {
        setHeld(true); // 默认持仓
        setExplicit(false);
      }
    } catch {
      setHeld(true);
      setExplicit(false);
    }
  }, [code, serverHeld]);
  const active = held === true;
  const confirmed = active && explicit;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        const next = !active;
        setHeld(next);
        setExplicit(true);
        setFunnelState(code, { held: next });
        reportFunnelChoice(code); // ③ 服务端埋点(去抖、静默)
        onChange?.(code, next);   // 乐观同步 funnelMap → chip/分组即时切轴
      }}
      title={active
        ? (explicit ? "已标记持有，点一下改为未持仓" : "默认按持有展示，点一下标记为未持仓")
        : "已标记未持仓，点一下标记为持有"}
      style={{
        padding: "1px 7px",
        borderRadius: 10,
        border: `1px solid ${confirmed ? "#ef4444" : "var(--border)"}`,
        background: confirmed ? "rgba(239, 68, 68, 0.15)" : "transparent",
        color: confirmed ? "#ef4444" : active ? "var(--text-soft)" : "var(--text-dim)",
        fontSize: 11,
        fontWeight: confirmed ? 600 : 400,
        cursor: "pointer",
        whiteSpace: "nowrap",
        lineHeight: 1.4,
      }}
    >
      {active ? "持有" : "未持"}
    </button>
  );
}

// Single-row renderer — extracted so both grouped and filter-flat modes
// share one source of truth. `onToggleStar` is required because the star
// toggle needs access to component state, but rest of the row is pure
// data → JSX.
function stockRow(
  r: StockRow,
  onToggleStar: (code: string) => void,
  serverHeld?: boolean,
  onHeldChange?: (code: string, held: boolean) => void,
) {
  return (
    <tr key={r.code} style={r.has_strong_signal ? rowStrong : undefined}>
      <td style={{ ...td, width: 28, padding: "10px 0 10px 6px" }}>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onToggleStar(r.code); }}
          aria-label={r.starred ? "取消星标" : "标为特别关注"}
          title={r.starred ? "取消星标" : "标为特别关注"}
          style={{
            background: "transparent",
            border: "none",
            padding: 2,
            fontSize: 14,
            cursor: "pointer",
            color: r.starred ? "#facc15" : "var(--text-dim)",
            lineHeight: 1,
          }}
        >
          {r.starred ? "★" : "☆"}
        </button>
      </td>
      <td style={{ ...td, fontFamily: "monospace" }}>
        <span style={{ color: "var(--text-faint)", marginRight: 4 }}>{exchangeLabel[r.exchange] || ""}</span>
        {r.code}
      </td>
      <td style={td}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {r.name}
          <HeldToggle code={r.code} serverHeld={serverHeld} onChange={onHeldChange} />
        </span>
      </td>
      <td style={{
        ...td,
        textAlign: "right",
        fontFamily: "monospace",
        color: r.change_pct == null ? "var(--text-soft)" : r.change_pct >= 0 ? "#ef4444" : "#22c55e",
      }}>
        {r.price != null ? r.price.toFixed(2) : "-"}
      </td>
      <td style={{
        ...td,
        textAlign: "right",
        fontFamily: "monospace",
        color: r.change_pct == null ? "var(--text-muted)" : r.change_pct >= 0 ? "#ef4444" : "#22c55e",
      }}>
        {r.change_pct != null ? `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(2)}%` : "-"}
      </td>
      <td style={{
        ...td,
        textAlign: "right",
        fontFamily: "monospace",
        color: r.change_pct_3d == null ? "var(--text-muted)" : r.change_pct_3d >= 0 ? "#ef4444" : "#22c55e",
      }}>
        {r.change_pct_3d != null ? `${r.change_pct_3d >= 0 ? "+" : ""}${r.change_pct_3d.toFixed(2)}%` : "-"}
      </td>
      <td style={{ ...td, textAlign: "right", fontFamily: "monospace", color: "var(--text-soft)" }}>
        {r.turnover_rate_3d != null ? `${r.turnover_rate_3d.toFixed(1)}%` : "-"}
      </td>
      <td style={{ ...td, textAlign: "right", fontFamily: "monospace", color: "var(--text-soft)" }}>
        {fmtFlow(r.net_flow_3d)}
      </td>
      <td style={td}>
        <IndustryWaterCell row={r} />
      </td>
      <td style={td}>
        <ActionableCell analysis={r.analysis} serverHeld={serverHeld} />
      </td>
      <td style={{ ...td, color: "var(--text-faint)", fontSize: 12 }}>
        {r.last_ts ? new Date(r.last_ts).toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "numeric", day: "numeric" }) : "未抓取"}
      </td>
      <td style={{ ...td, textAlign: "right" }}>
        <a href={`/stocks/${r.code}`} style={{ color: "var(--link)", fontSize: 12, textDecoration: "none" }}>
          解析 →
        </a>
      </td>
    </tr>
  );
}

function IndustryWaterCell({ row }: { row: StockRow }) {
  // Compact "industry name + 3 percentile chips" cell. Each chip uses a
  // color stop so the user reads it at a glance:
  //   estimation chip (PE percentile): higher = redder (over-valued vs peers)
  //   trend chip (3-day change pctile): higher = redder (lead the pack)
  //   capital chip (3-day flow pctile): higher = redder (money piling in)
  // None of the three having a value (cold start) → just industry name.
  if (!row.industry_name && row.industry_pe_pctile == null
      && row.industry_change_3d_pctile == null && row.industry_flow_3d_pctile == null) {
    return <span style={{ color: "var(--text-dim)" }}>–</span>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {row.industry_name && (
        <span style={{ color: "var(--text-soft)", fontSize: 11 }}>{row.industry_name}</span>
      )}
      <div style={{ display: "flex", gap: 3 }}>
        <PctChip
          label="估"
          value={row.industry_pe_pctile}
          title="估值分位 (PE)"
          desc="本股 PE 在所属行业内的分位（0=最便宜，100=最贵）。70+ 红色 = 估值偏贵；30- 绿色 = 相对便宜。"
        />
        <PctChip
          label="势"
          value={row.industry_change_3d_pctile}
          title="走势分位 (3日涨幅)"
          desc="近 3 日涨幅在行业内的排位。70+ = 领涨同业；30- = 落后，资金可能正撤离。"
        />
        <PctChip
          label="金"
          value={row.industry_flow_3d_pctile}
          title="资金分位 (3日主力净流入)"
          desc="主力近 3 日净流入在行业内的排位。70+ = 资金堆积；30- = 主力撤退。"
        />
      </div>
    </div>
  );
}

function PctChip({
  label, value, title, desc,
}: {
  label: string;
  value: number | null;
  title: string;
  desc: string;
}) {
  // Build the tooltip body once; both null + value paths share it. The "current
  // value" line only appears when we have one.
  const tooltipBody = (currentLine: string | null) => (
    <div>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <div style={{ color: "var(--text-soft)" }}>{desc}</div>
      {currentLine && (
        <div style={{ marginTop: 6, fontFamily: "monospace", color: "var(--text)" }}>
          {currentLine}
        </div>
      )}
    </div>
  );

  if (value == null) {
    return (
      <Tooltip content={tooltipBody("当前：暂无数据")}>
        <span style={{
          padding: "1px 5px", borderRadius: 3, fontSize: 10,
          background: "var(--border-faint)", color: "var(--text-dim)", letterSpacing: 0.5,
        }}>{label}–</span>
      </Tooltip>
    );
  }
  // Linear color ramp from green (low) → grey (mid) → red (high)
  const v = Math.max(0, Math.min(100, value));
  const color = v >= 70 ? "#fca5a5" : v <= 30 ? "#86efac" : "#9ca3af";
  const bg = v >= 70 ? "rgba(239,68,68,0.18)" : v <= 30 ? "rgba(34,197,94,0.15)" : "rgba(255,255,255,0.05)";
  return (
    <Tooltip content={tooltipBody(`当前分位：${v.toFixed(0)}%`)}>
      <span style={{
        padding: "1px 5px", borderRadius: 3, fontSize: 10,
        background: bg, color, letterSpacing: 0.5,
      }}>{label}{v.toFixed(0)}</span>
    </Tooltip>
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

const ghostBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "transparent",
  color: "var(--text-soft)",
  border: "1px solid var(--border-mid)",
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
  borderBottom: "1px solid var(--border-soft)",
  fontWeight: 500,
};
const td: React.CSSProperties = {
  padding: "10px",
  borderBottom: "1px solid var(--border-faint)",
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
    color: strong ? "#fca5a5" : "var(--text-soft)",
    fontSize: 11,
  };
}
