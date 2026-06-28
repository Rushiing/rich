"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import {
  api, ActionableTier, ActionableTiers, HitRateSummary, Holding, KeyTable, NextDayOutlook,
  PeerRow, SellRisk, StockAnalysis, StockDetail, StopLossLevel,
  confidenceBucket, confidenceLabel,
} from "../../../lib/api";
// 持仓决策漏斗状态 —— per-stock localStorage（held/盈亏/风险偏好三个点选），
// 详情页漏斗与列表页轻量持仓位共用同一份持久化。
import {
  FunnelState, PnlBucket, TierKey,
  getFunnelState, setFunnelState, pnlBucketFromPct, scenarioKeyFor,
  reportFunnelChoice,
} from "../../../lib/holdingFunnel";
import Tooltip from "../../_components/Tooltip";

// 6/28: 详情页生成/重新生成走异步——POST 只启动任务,这里轮询状态直到完成。
// 慢网关单次解析 ~50s,超过 Railway ~30s HTTP 代理上限,同步请求会 Failed to
// fetch。每 3s 轮一次,最多 ~3.5 分钟;debate 模式 3 次调用更慢,留足余量。
async function pollAnalysisDone(code: string): Promise<void> {
  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
  for (let i = 0; i < 70; i++) {
    await sleep(3000);
    const st = await api.singleAnalysisStatus(code);
    if (!st.running) {
      if (st.error) throw new Error(humanizeAnalysisError(st.error));
      return;
    }
  }
  throw new Error("生成超时(>3 分钟),请稍后重试");
}

// 把后端原始异常压成一句对客文案。绝大多数是网关侧问题(超时/限流/模型名),
// 用户能做的只有重试,所以不暴露堆栈细节。
function humanizeAnalysisError(raw: string): string {
  const s = raw.toLowerCase();
  if (s.includes("timed out") || s.includes("timeout")) return "模型响应超时,请重试";
  if (s.includes("429") || s.includes("rate")) return "模型限流,请稍后重试";
  if (s.includes("401") || s.includes("403") || s.includes("auth")) return "模型服务鉴权失败(请检查后台配置)";
  if (s.includes("unusable") || s.includes("incomplete")) return "模型输出不完整,请重试";
  return "生成失败,请重试";
}

// 漏斗三行的点选定义：盈亏档 + 风险偏好。颜色沿用 A股语境（红=进取/涨、绿=保守/跌）。
const TIER_DEFS: { key: TierKey; label: string; color: string }[] = [
  { key: "aggressive",   label: "激进", color: "#ef4444" },
  { key: "neutral",      label: "中立", color: "#9ca3af" },
  { key: "conservative", label: "保守", color: "#22c55e" },
];
// 盈亏档点选：盈=红、平=灰、亏=绿（与个股涨跌色一致）。label 写明幅度——
// 这三档映射到 LLM 按幅度写的情境(big_gain≥10%/small/big_loss≤-10%),按钮
// 自带幅度才能让用户选对档:浮盈 3% 该点「小幅波动」而非「大幅浮盈」,否则会
// 拿到不匹配的止盈式建议。
const PNL_DEFS: { key: PnlBucket; label: string; color: string }[] = [
  { key: "盈", label: "大幅浮盈", color: "#ef4444" },
  { key: "平", label: "小幅波动", color: "#9ca3af" },
  { key: "亏", label: "大幅浮亏", color: "#22c55e" },
];

export default function StockDetailPage({
  params,
}: {
  params: Promise<{ code: string }>;
}) {
  const { code } = use(params);
  const [analysis, setAnalysis] = useState<StockAnalysis | null>(null);
  // Detail (latest snapshot) carries industry context + 3-day metrics, fetched
  // independently from the cached LLM analysis so a code with no analysis
  // yet still shows industry chips.
  const [detail, setDetail] = useState<StockDetail | null>(null);
  // 6/3: global hit_rate summary — fed to KeyTableCard so the actionable
  // verdict shows "AI 历史命中 X% (n=Y)" right below it. Silent failure.
  const [hitRate, setHitRate] = useState<HitRateSummary | null>(null);
  // 卖出线 S3:该票当前客观风险信号(live)。null = 无风险/未登录(静默)。
  const [sellRisk, setSellRisk] = useState<SellRisk | null>(null);
  // S1 (6/10): holding mirrored up from HoldingCard (which owns the
  // fetch + edit flow) so ScenarioAdviceCard can highlight the quadrant
  // matching the user's REAL P&L instead of showing four equal rows.
  const [holding, setHolding] = useState<Holding | null>(null);
  // S2 (6/10): deep_analysis collapsed by default — the 1500-2500 字 wall
  // was burying the conclusion. Auto-expands after a debate regenerate
  // (the payoff lives in the 看多 vs 看空 section) and via the banner jump.
  const [deepOpen, setDeepOpen] = useState(false);

  function jumpToDebateSection() {
    setDeepOpen(true);
    requestAnimationFrame(() => {
      const t = document.getElementById("md-h-看多-vs-看空")
        ?? document.getElementById("md-deep-analysis");
      t?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function loadCached() {
    setLoading(true);
    try {
      const [a, d] = await Promise.all([
        api.getAnalysis(code),
        api.stockDetail(code).catch(() => null),
      ]);
      setAnalysis(a);
      setDetail(d);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadCached();
    // 6/3: pull hit_rate summary once per code mount. Backend caches
    // 30 min so this is cheap and stable.
    api.hitRateSummary().then(setHitRate).catch(() => {});
    // 卖出线 S3:拉当前风险信号(供应性、静默失败)。
    api.getSellRisk(code).then(setSellRisk).catch(() => setSellRisk(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function regenerate(mode: "single" | "debate" = "single") {
    setGenerating(true);
    setErr(null);
    try {
      // 5/29: force=true bypasses snapshot-id cache. When a user
      // explicitly clicks "重新生成", they want a fresh LLM call even
      // if the snapshot hasn't changed. Background batch flows leave
      // force off so they dedupe correctly.
      // 6/28: the POST is now async (a single analysis can take ~50s on a
      // slow gateway, past Railway's ~30s HTTP proxy cutoff). Start the
      // job, poll status until done, then re-fetch the freshly cached row.
      await api.generateAnalysis(code, mode, { force: true });
      await pollAnalysisDone(code);
      const a = await api.getAnalysis(code);
      if (a) setAnalysis(a);
      // Deep mode: scroll users right to the "看多 vs 看空" section so
      // they see the cross-validation payoff immediately. Defer one frame
      // so the new markdown renders before we query the DOM.
      if (mode === "debate") {
        // S2: deep analysis is collapsed by default — expand before the
        // scroll so the target section exists in the DOM.
        jumpToDebateSection();
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setGenerating(false);
    }
  }

  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 18, margin: 0, display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          {detail?.name && <span>{detail.name}</span>}
          <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 15 }}>
            {code}
          </span>
          {detail?.price != null && (
            <span style={{
              fontFamily: "monospace",
              fontSize: 16,
              color: detail.change_pct == null
                ? "var(--text)"
                : detail.change_pct >= 0 ? "#ef4444" : "#22c55e",
            }}>
              {detail.price.toFixed(2)}
              {detail.change_pct != null && (
                <span style={{ fontSize: 13, marginLeft: 4 }}>
                  {detail.change_pct >= 0 ? "+" : ""}{detail.change_pct.toFixed(2)}%
                </span>
              )}
            </span>
          )}
        </h1>
        <a href="/stocks" style={{ color: "var(--text-soft)", fontSize: 13, textDecoration: "none" }}>
          ← 返回盯盘
        </a>
      </header>

      {loading ? (
        <p style={{ color: "var(--text-faint)", marginTop: 24 }}>加载中…</p>
      ) : (
        <>
          {!analysis ? (
            <>
              {detail && <IndustryContextCard detail={detail} />}
              <PeerComparableCard code={code} />
              <AnalysisHistoryCard code={code} />
              <HoldingCard
                code={code}
                price={detail?.price ?? null}
                keyTable={null}
                onHoldingChange={setHolding}
              />
              <EmptyState onGenerate={regenerate} generating={generating} err={err} />
            </>
          ) : (
            <>
              {/* S2 (6/10): 结论先行。第一屏 = 鲜度条 + 结论卡(actionable
                  / 理由 / 触发价 / 有效期 / 命中率背书),持仓对照紧随其
                  后;行业水位 + 历史解析是"对照信息",下沉到结论之下;
                  deep_analysis 默认折叠。阅读顺序 = 决策顺序。 */}
              <FreshnessBar
                analysis={analysis}
                generating={generating}
                onRegenerate={() => regenerate("single")}
                onDebate={() => regenerate("debate")}
              />
              {err && <div style={{ color: "#ef4444", marginTop: 8, fontSize: 13 }}>{err}</div>}
              {analysis.mode === "debate" && (
                <DebateBanner code={code} onJump={() => jumpToDebateSection()} />
              )}
              <KeyTableCard
                code={code}
                kt={analysis.key_table}
                currentPrice={detail?.price ?? null}
                hitRate={hitRate}
                holdingPnlPct={
                  holding && detail?.price != null && holding.cost_price > 0
                    ? (detail.price - holding.cost_price) / holding.cost_price * 100
                    : null
                }
                sellRisk={sellRisk}
              />
              <HoldingCard
                code={code}
                price={detail?.price ?? null}
                keyTable={analysis?.key_table ?? null}
                onHoldingChange={setHolding}
              />
              {detail && <IndustryContextCard detail={detail} />}
              <PeerComparableCard code={code} />
              <AnalysisHistoryCard code={code} />
              <DeepAnalysis
                md={analysis.deep_analysis}
                open={deepOpen}
                onToggle={() => setDeepOpen((v) => !v)}
              />
              <Footnote analysis={analysis} />
            </>
          )}
        </>
      )}
    </main>
  );
}

function IndustryContextCard({ detail }: { detail: StockDetail }) {
  // Skip rendering if there's literally nothing to show — happens for codes
  // whose snapshot row hasn't been written yet (cold start) or that fall
  // outside any industry mapping in our DB.
  const hasAny =
    detail.industry_name ||
    detail.industry_pe_pctile != null ||
    detail.industry_change_3d_pctile != null ||
    detail.industry_flow_3d_pctile != null ||
    detail.change_pct_3d != null ||
    detail.industry_pe_avg != null;
  if (!hasAny) return null;

  function fmtPct(v: number | null, suffix = "%") {
    return v == null ? "—" : `${v.toFixed(1)}${suffix}`;
  }
  function fmtFlow(yuan: number | null) {
    if (yuan == null) return "—";
    const abs = Math.abs(yuan);
    const sign = yuan >= 0 ? "+" : "-";
    if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`;
    if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(0)}万`;
    return `${sign}${abs.toFixed(0)}`;
  }
  function pctChip(label: string, v: number | null, title: string, desc: string) {
    const body = (currentLine: string) => (
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
        <div style={{ color: "var(--text-soft)" }}>{desc}</div>
        <div style={{ marginTop: 6, fontFamily: "monospace", color: "var(--text)" }}>
          {currentLine}
        </div>
      </div>
    );
    if (v == null) {
      return (
        <Tooltip content={body("当前：暂无数据")}>
          <span style={{
            padding: "2px 8px", borderRadius: 4, fontSize: 11,
            background: "var(--border-faint)", color: "var(--text-dim)",
          }}>{label}: —</span>
        </Tooltip>
      );
    }
    const x = Math.max(0, Math.min(100, v));
    const color = x >= 70 ? "#fca5a5" : x <= 30 ? "#86efac" : "#9ca3af";
    const bg = x >= 70 ? "rgba(239,68,68,0.18)" : x <= 30 ? "rgba(34,197,94,0.15)" : "rgba(255,255,255,0.05)";
    return (
      <Tooltip content={body(`当前分位：${x.toFixed(0)}%`)}>
        <span style={{
          padding: "2px 8px", borderRadius: 4, fontSize: 11,
          background: bg, color,
        }}>{label}: {x.toFixed(0)}%</span>
      </Tooltip>
    );
  }

  return (
    <section style={{
      marginTop: 16, padding: 14, border: "1px solid var(--border)",
      borderRadius: 8, background: "var(--surface-alt)",
    }}>
      <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "var(--text-muted)" }}>所属行业</span>
        <span style={{ fontSize: 14, color: "var(--text)" }}>
          {detail.industry_name ?? "未知"}
        </span>
      </div>
      <div style={{ marginTop: 10, display: "flex", gap: 6, flexWrap: "wrap" }}>
        {pctChip(
          "PE 分位",
          detail.industry_pe_pctile,
          "估值分位 (PE)",
          "本股 PE 在所属行业内的分位（0=最便宜，100=最贵）。70+ 红色 = 估值偏贵；30- 绿色 = 相对便宜。",
        )}
        {pctChip(
          "3 日涨幅 分位",
          detail.industry_change_3d_pctile,
          "走势分位 (3日涨幅)",
          "近 3 日涨幅在行业内的排位。70+ = 领涨同业；30- = 落后，资金可能正撤离。",
        )}
        {pctChip(
          "3 日资金 分位",
          detail.industry_flow_3d_pctile,
          "资金分位 (3日主力净流入)",
          "主力近 3 日净流入在行业内的排位。70+ = 资金堆积；30- = 主力撤退。",
        )}
      </div>
      <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "4px 16px", fontSize: 12, color: "var(--text-soft)" }}>
        <div>3 日涨幅: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{fmtPct(detail.change_pct_3d)}</b></div>
        <div>3 日换手: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{fmtPct(detail.turnover_rate_3d)}</b></div>
        <div>3 日主力净流入: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{fmtFlow(detail.net_flow_3d)}</b></div>
        <div>本股 PE / PB: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{detail.pe_ratio?.toFixed(1) ?? "—"} / {detail.pb_ratio?.toFixed(2) ?? "—"}</b></div>
        <div>行业平均 PE: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{detail.industry_pe_avg?.toFixed(1) ?? "—"}</b></div>
        <div>行业平均 PB: <b style={{ color: "var(--text)", fontFamily: "monospace" }}>{detail.industry_pb_avg?.toFixed(2) ?? "—"}</b></div>
      </div>
    </section>
  );
}


/**
 * 我的持仓 card. Lets the user record cost basis for this stock, then
 * shows a 持仓对照 overlay: float P&L + distance to the AI's sell range +
 * stop-loss cushion. All computed client-side from the holding + the
 * (globally cached) analysis numbers — no extra LLM call.
 */
function HoldingCard({
  code, price, keyTable, onHoldingChange,
}: {
  code: string;
  price: number | null;
  keyTable: KeyTable | null;
  // S1: mirrors holding state up to the page so sibling cards (scenario
  // quadrant highlight) can react without a duplicate fetch.
  onHoldingChange?: (h: Holding | null) => void;
}) {
  const [holding, setHoldingRaw] = useState<Holding | null>(null);
  const setHolding = (h: Holding | null) => {
    setHoldingRaw(h);
    onHoldingChange?.(h);
  };
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // form fields
  const [costPrice, setCostPrice] = useState("");
  const [shares, setShares] = useState("");
  const [openedAt, setOpenedAt] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    api.getHolding(code)
      .then((h) => setHolding(h))
      .catch(() => setHolding(null))
      .finally(() => setLoading(false));
  }, [code]);

  function openEdit() {
    setCostPrice(holding ? String(holding.cost_price) : "");
    setShares(holding?.shares != null ? String(holding.shares) : "");
    setOpenedAt(holding?.opened_at ?? "");
    setNote(holding?.note ?? "");
    setErr(null);
    setEditing(true);
  }

  async function save() {
    const cp = parseFloat(costPrice);
    if (!(cp > 0)) { setErr("成本价必须大于 0"); return; }
    setSaving(true);
    setErr(null);
    try {
      const h = await api.upsertHolding(code, {
        cost_price: cp,
        shares: shares.trim() ? parseFloat(shares) : null,
        opened_at: openedAt.trim() || null,
        note: note.trim() || null,
      });
      setHolding(h);
      setEditing(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!confirm("确认移除该股的持仓记录？")) return;
    setSaving(true);
    try {
      await api.deleteHolding(code);
      setHolding(null);
      setEditing(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  if (loading) return null;

  const cardStyle: React.CSSProperties = {
    marginTop: 16, padding: 14, border: "1px solid var(--border)",
    borderRadius: 8, background: "var(--surface-alt)",
  };

  // ---- edit form ----
  if (editing) {
    const inp: React.CSSProperties = {
      padding: "6px 8px", background: "var(--bg)",
      border: "1px solid var(--border-mid)", borderRadius: 4,
      color: "var(--text)", fontSize: 13, width: "100%",
    };
    return (
      <section style={cardStyle}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
          {holding ? "编辑持仓" : "记录我的持仓"}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
            成本价 *
            <input type="number" inputMode="decimal" value={costPrice}
              onChange={(e) => setCostPrice(e.target.value)}
              placeholder="22.80" style={{ ...inp, marginTop: 3 }} />
          </label>
          <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
            持仓数量（股，可选）
            <input type="number" inputMode="numeric" value={shares}
              onChange={(e) => setShares(e.target.value)}
              placeholder="1000" style={{ ...inp, marginTop: 3 }} />
          </label>
          <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
            建仓日期（可选）
            <input type="date" value={openedAt}
              onChange={(e) => setOpenedAt(e.target.value)}
              style={{ ...inp, marginTop: 3 }} />
          </label>
          <label style={{ fontSize: 12, color: "var(--text-muted)" }}>
            备注（可选）
            <input type="text" value={note} maxLength={100}
              onChange={(e) => setNote(e.target.value)}
              placeholder="加仓计划 / 心理价位…" style={{ ...inp, marginTop: 3 }} />
          </label>
        </div>
        {err && <div style={{ color: "#ef4444", fontSize: 12, marginTop: 8 }}>{err}</div>}
        <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "flex-end" }}>
          {holding && (
            <button onClick={remove} disabled={saving} style={{
              padding: "5px 10px", background: "transparent", color: "#ef4444",
              border: "1px solid var(--border-mid)", borderRadius: 4,
              fontSize: 12, cursor: "pointer", marginRight: "auto",
            }}>移除</button>
          )}
          <button onClick={() => setEditing(false)} disabled={saving} style={{
            padding: "5px 10px", background: "transparent", color: "var(--text-soft)",
            border: "1px solid var(--border-mid)", borderRadius: 4,
            fontSize: 12, cursor: "pointer",
          }}>取消</button>
          <button onClick={save} disabled={saving} style={{
            padding: "5px 12px", background: "var(--link)", color: "white",
            border: "none", borderRadius: 4, fontSize: 12, cursor: "pointer",
          }}>{saving ? "保存中…" : "保存"}</button>
        </div>
      </section>
    );
  }

  // ---- empty state ----
  if (!holding) {
    return (
      <section style={cardStyle}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
            记录持仓后，可对照 AI 的买卖价 / 止损线看你的盈亏与安全垫
          </span>
          <button onClick={openEdit} style={{
            padding: "5px 12px", background: "var(--link)", color: "white",
            border: "none", borderRadius: 4, fontSize: 12, cursor: "pointer",
            whiteSpace: "nowrap",
          }}>记录我的持仓</button>
        </div>
      </section>
    );
  }

  // ---- holding + 持仓对照 ----
  const floatPct = price != null
    ? (price - holding.cost_price) / holding.cost_price * 100
    : null;
  const floatColor = floatPct == null ? "var(--text)"
    : floatPct >= 0 ? "#ef4444" : "#22c55e";
  const marketValue = (price != null && holding.shares != null)
    ? price * holding.shares : null;
  const floatAmount = (price != null && holding.shares != null)
    ? (price - holding.cost_price) * holding.shares : null;

  // Stop-loss cushion vs the *most aggressive* (highest-priced) stop.
  let stopNote: { text: string; color: string } | null = null;
  if (keyTable?.stop_loss_levels?.length && price != null) {
    const stop = Math.max(...keyTable.stop_loss_levels.map((l) => l.price));
    if (price <= stop) {
      stopNote = { text: `已跌破 AI 止损线 ${stop.toFixed(2)}`, color: "#ef4444" };
    } else {
      const cushion = (price - stop) / price * 100;
      stopNote = {
        text: `距 AI 止损线 ${stop.toFixed(2)} 还有 ${cushion.toFixed(1)}% 安全垫`,
        color: cushion < 5 ? "#facc15" : "var(--text-soft)",
      };
    }
  }
  // Distance to the sell range.
  let sellNote: { text: string; color: string } | null = null;
  if (keyTable && price != null) {
    if (price >= keyTable.sell_price_low) {
      sellNote = { text: `已进入 AI 卖出区间（${keyTable.sell_price_low.toFixed(2)}–${keyTable.sell_price_high.toFixed(2)}），可考虑减仓`, color: "#facc15" };
    } else {
      const up = (keyTable.sell_price_low - price) / price * 100;
      sellNote = { text: `距 AI 卖出区间下沿 ${keyTable.sell_price_low.toFixed(2)} 还需 +${up.toFixed(1)}%`, color: "var(--text-soft)" };
    }
  }

  const fact: React.CSSProperties = { fontSize: 13 };
  const factVal: React.CSSProperties = { fontFamily: "monospace", color: "var(--text)" };

  return (
    <section style={cardStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 600 }}>我的持仓</span>
        <button onClick={openEdit} style={{
          padding: "2px 8px", background: "transparent", color: "var(--text-soft)",
          border: "1px solid var(--border-mid)", borderRadius: 4,
          fontSize: 11, cursor: "pointer",
        }}>编辑</button>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: "6px 16px" }}>
        <div style={fact}>
          <span style={{ color: "var(--text-muted)" }}>成本价 </span>
          <b style={factVal}>{holding.cost_price.toFixed(2)}</b>
        </div>
        <div style={fact}>
          <span style={{ color: "var(--text-muted)" }}>现价 </span>
          <b style={factVal}>{price != null ? price.toFixed(2) : "—"}</b>
        </div>
        <div style={fact}>
          <span style={{ color: "var(--text-muted)" }}>浮动盈亏 </span>
          <b style={{ fontFamily: "monospace", color: floatColor }}>
            {floatPct != null ? `${floatPct >= 0 ? "+" : ""}${floatPct.toFixed(2)}%` : "—"}
          </b>
        </div>
        {holding.shares != null && (
          <div style={fact}>
            <span style={{ color: "var(--text-muted)" }}>持仓数量 </span>
            <b style={factVal}>{holding.shares.toLocaleString("zh-CN")}</b>
          </div>
        )}
        {marketValue != null && (
          <div style={fact}>
            <span style={{ color: "var(--text-muted)" }}>市值 </span>
            <b style={factVal}>{marketValue.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</b>
          </div>
        )}
        {floatAmount != null && (
          <div style={fact}>
            <span style={{ color: "var(--text-muted)" }}>浮动金额 </span>
            <b style={{ fontFamily: "monospace", color: floatColor }}>
              {floatAmount >= 0 ? "+" : ""}{floatAmount.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}
            </b>
          </div>
        )}
      </div>
      {(sellNote || stopNote) && (
        <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border-faint)", display: "flex", flexDirection: "column", gap: 4 }}>
          {sellNote && <div style={{ fontSize: 12, color: sellNote.color }}>· {sellNote.text}</div>}
          {stopNote && <div style={{ fontSize: 12, color: stopNote.color }}>· {stopNote.text}</div>}
        </div>
      )}
      {holding.note && (
        <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-faint)" }}>
          备注：{holding.note}
        </div>
      )}
    </section>
  );
}


function EmptyState({ onGenerate, generating, err }: { onGenerate: () => void; generating: boolean; err: string | null }) {
  return (
    <div style={{ marginTop: 32, padding: 24, border: "1px solid var(--border)", borderRadius: 8, textAlign: "center" }}>
      <p style={{ color: "var(--text-soft)", fontSize: 14, margin: 0 }}>尚未生成深度解析</p>
      <p style={{ color: "var(--text-faint)", fontSize: 12, marginTop: 6 }}>
        生成会调一次大模型（基于该股票最新 snapshot），约 30–60 秒，请稍候
      </p>
      <button
        onClick={onGenerate}
        disabled={generating}
        style={{
          marginTop: 16,
          padding: "8px 16px",
          background: generating ? "var(--text-dim)" : "var(--link)",
          color: "white",
          border: "none",
          borderRadius: 6,
          fontSize: 13,
          cursor: generating ? "not-allowed" : "pointer",
        }}
      >
        {generating ? "生成中…" : "生成深度解析"}
      </button>
      {err && <div style={{ color: "#ef4444", marginTop: 12, fontSize: 13 }}>{err}</div>}
    </div>
  );
}

// 6/26:缓存"不是最新"的原因不只是时效 —— should_reanalyze 还会因行情大动
// (price_move)/盘面信号变(signal_change)提前标记。按原因显准确文案,别把
// 一只今天大跌的票误标成"(>4h)"。
function staleLabel(reason?: string | null): string {
  if (reason === "price_move") return "行情已变动,建议重新生成";
  if (reason === "signal_change") return "盘面信号已变,建议重新生成";
  if (reason === "stale") return "缓存已过期 (>4h)";
  return "建议重新生成"; // no_anchor / 其他
}

function FreshnessBar({
  analysis, generating, onRegenerate, onDebate,
}: {
  analysis: StockAnalysis;
  generating: boolean;
  onRegenerate: () => void;
  onDebate: () => void;
}) {
  return (
    <div style={{ marginTop: 16, display: "flex", alignItems: "center", justifyContent: "space-between", color: "var(--text-muted)", fontSize: 12, gap: 8, flexWrap: "wrap" }}>
      <span>
        生成于 {new Date(analysis.created_at).toLocaleString("zh-CN")}
        {!analysis.is_fresh && <span style={{ color: "#facc15", marginLeft: 8 }}>· {staleLabel(analysis.stale_reason)}</span>}
        {/* Model name intentionally hidden from end users. */}
      </span>
      <div style={{ display: "flex", gap: 6 }}>
        <Tooltip content="更深入的解析模式：从看多和看空两个角度交叉验证，风险点检测更准。约 30 秒，比常规解析慢一些。">
          <button
            onClick={onDebate}
            disabled={generating}
            style={{
              padding: "4px 10px",
              background: "transparent",
              color: "var(--text-soft)",
              border: "1px solid var(--border-mid)",
              borderRadius: 4,
              fontSize: 12,
              cursor: generating ? "not-allowed" : "pointer",
            }}
          >
            🔬 深度解析
          </button>
        </Tooltip>
        <button
          onClick={onRegenerate}
          disabled={generating}
          style={{
            padding: "4px 10px",
            background: "transparent",
            color: "var(--text-soft)",
            border: "1px solid var(--border-mid)",
            borderRadius: 4,
            fontSize: 12,
            cursor: generating ? "not-allowed" : "pointer",
          }}
        >
          {generating ? "生成中…" : "重新生成"}
        </button>
      </div>
    </div>
  );
}

function KeyTableCard({
  code, kt, currentPrice, hitRate, holdingPnlPct = null, sellRisk = null,
}: {
  // 漏斗状态用 code 做 per-stock localStorage key。
  code: string;
  kt: KeyTable;
  currentPrice: number | null;
  hitRate: HitRateSummary | null;
  // S1: user's actual P&L % (null = no holding recorded). 仅在没存过漏斗状态时
  // 用来预填盈亏档（已录成本价 → 反映其浮盈/浮亏档），不写回 localStorage。
  holdingPnlPct?: number | null;
  // 卖出线 S3:该票当前客观风险信号(live),按漏斗盈亏档融合成动作。
  sellRisk?: SellRisk | null;
}) {
  // 决策漏斗状态：持仓 / 盈亏 / 风险偏好。默认（Rush 拍板）持有·盈·激进。
  // 初值优先级：localStorage 存过 → 用存的；否则若已录成本价（holdingPnlPct
  // 非空）→ 预填 held=true + 盈亏档（仅作初值，不落库）；否则默认。
  const [funnel, setFunnel] = useState<FunnelState>(() => {
    const s = getFunnelState(code);
    const stored = typeof window !== "undefined" && window.localStorage.getItem(`rich:funnel:${code}`);
    if (!stored && holdingPnlPct != null) {
      return { ...s, held: true, pnl: pnlBucketFromPct(holdingPnlPct) };
    }
    return s;
  });
  // 点选写回 localStorage 并刷新本地状态,同时 fire-and-forget 上报服务端(③ 埋点,去抖)。
  const updateFunnel = (partial: Partial<FunnelState>) => {
    setFunnel(setFunnelState(code, partial));
    reportFunnelChoice(code);
  };

  // ③ 跨设备 hydrate:挂载后从服务端拉该票最新选择,有就覆盖 localStorage —— 让
  // 点选跟**账号**走、换电脑也在(服务端没记录则退回 localStorage/默认)。
  useEffect(() => {
    let alive = true;
    api.getFunnelLatest(code)
      .then((srv) => {
        if (!alive || !srv) return;
        const next = setFunnelState(code, {
          held: srv.held,
          pnl: (srv.pnl as PnlBucket) ?? getFunnelState(code).pnl,
          tier: srv.tier as TierKey,
        });
        setFunnel(next);
      })
      .catch(() => {});
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  const tier = funnel.tier;
  const tiers = kt.actionable_tiers;

  // Pick the active tier's view-model. When actionable_tiers is missing
  // (legacy cached row), synthesize from the top-level fields so the rest
  // of the card renders normally.
  const view: ActionableTier = tiers
    ? tiers[tier]
    : {
        action: kt.actionable,
        position_pct: kt.position_pct,
        buy_price_low: kt.buy_price_low,
        buy_price_high: kt.buy_price_high,
        hold_period: kt.hold_period,
        reason: "",
      };

  const actionableColor =
    view.action === "建议买入" ? "#ef4444" :   // A股语境：红=买/涨
    view.action === "建议卖出" ? "#22c55e" :   // 绿=卖/跌
    view.action === "不建议入手" ? "#6b7280" :
    "#9ca3af";                                  // 观望

  // 漏斗出口：持仓 + 盈亏 → 选中 scenario_advice 的一条。
  const scenarioText = kt.scenario_advice?.[scenarioKeyFor(funnel.held, funnel.pnl)];

  return (
    <section style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Header card: actionable verdict + company portrait + red flags */}
      <div style={{ padding: 16, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
          <div style={{ fontSize: 22, fontWeight: 600, color: actionableColor }}>{view.action}</div>
          {/* 6/3: valid_window 从 ConfidenceCard 底部提到 actionable 同
              行 — 它跟 actionable 一样是"决策信息",不应该被置信度数字压
              住。视觉权重:小一号字 + 灰底 chip,physically 同行但不抢
              actionable 主位。 */}
          {kt.valid_window && (
            <span style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 8px",
              borderRadius: 4,
              background: "var(--surface-alt)",
              border: "1px solid var(--border-faint)",
              color: "var(--text-soft)",
              fontSize: 12,
              fontWeight: 500,
              whiteSpace: "nowrap",
            }}>
              <span style={{ color: "var(--text-muted)" }}>⏱ 参考时效</span>
              {kt.valid_window}
            </span>
          )}
          {kt.company_tag && (
            <div style={{ color: "var(--text-soft)", fontSize: 13 }}>{kt.company_tag}</div>
          )}
        </div>
        {/* 6/3: AI 历史命中率 — 在 actionable 下方、reason 之上,作为
            "你为什么信这个建议" 的硬数据支撑。只对买/卖 显示 (其它
            没 hit_rate)。
            S2 (6/10) 口径升级:优先展示去重命中率(按 code+日取末锚,
            剥掉盘中重复解析的聚类水分)+ 相对同日全体票中位数的超额
            (剥掉市场 beta — 大跌日卖出"命中"不算本事)。超额是否支撑
            verdict 决定颜色:买入要正、卖出要负才算真区分度。 */}
        {(() => {
          const hb = hitRate?.by_actionable[view.action];
          if (!hb || hb.hit_rate == null) return null;
          const rate = hb.hit_rate_dedup ?? hb.hit_rate;
          const nShown = hb.n_unique ?? hb.n;
          const rateColor =
            rate >= 60 ? "#22c55e" :
            rate >= 50 ? "var(--text)" :
            "#f59e0b";
          const excess = hb.excess_return_d5;
          const excessSupports =
            excess != null &&
            (view.action === "建议买入" ? excess > 1 : excess < -1);
          return (
            <div style={{
              marginTop: 8,
              color: "var(--text-soft)", fontSize: 12, lineHeight: 1.5,
            }}>
              <span>AI 此类建议历史命中 </span>
              <b style={{ color: rateColor, fontSize: 13 }}>{rate.toFixed(1)}%</b>
              <span>（去重 n={nShown}{nShown < 30 ? "，偏小" : ""}）</span>
              {excess != null && (
                <span>
                  {" "}· 5 日超额{" "}
                  <b style={{ color: excessSupports ? "#22c55e" : "#f59e0b", fontSize: 13 }}>
                    {excess >= 0 ? "+" : ""}{excess.toFixed(1)}%
                  </b>
                  <Tooltip content="相对同日全体自选股中位数的超额收益。买入为正/卖出为负才说明 AI 在选股,而不是搭了大盘的顺风车。">
                    <span style={{ color: "var(--text-faint)", cursor: "help" }}> ⓘ</span>
                  </Tooltip>
                </span>
              )}
            </div>
          );
        })()}
        {/* Show the per-tier reason when the tier toggle exists; otherwise
            fall back to the global one_line_reason. */}
        {(tiers ? view.reason : kt.one_line_reason) && (
          <div style={{ marginTop: 6, color: "var(--text)", fontSize: 14 }}>
            {tiers ? view.reason : kt.one_line_reason}
          </div>
        )}
        {/* S2 (6/10): 触发价一行 — 结论卡上直接回答"什么价位动手/离场",
            不用下翻到关键数据表和止损卡。卖出/不入手给卖出区间+止损触发,
            买入给买入区间+止损,观望给"若持有"的离场线。止损取最高一档
            (第一道防线,validators 保证有序)。带现价距离百分比。 */}
        {(() => {
          const firstStop = kt.stop_loss_levels && kt.stop_loss_levels.length > 0
            ? kt.stop_loss_levels.reduce((a, b) => (b.price > a.price ? b : a))
            : null;
          const sellish = view.action === "建议卖出" || view.action === "不建议入手";
          const stopDist = firstStop && currentPrice
            ? (currentPrice - firstStop.price) / currentPrice * 100
            : null;
          const seg: { k: string; v: string }[] = [];
          if (sellish) {
            if (kt.sell_price_low != null && kt.sell_price_high != null) {
              seg.push({ k: "卖出区间", v: `${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}` });
            }
            if (firstStop) {
              seg.push({
                k: "跌破即离场",
                v: `${firstStop.price.toFixed(2)}${stopDist != null ? `（距现价 ${stopDist >= 0 ? "-" : "+"}${Math.abs(stopDist).toFixed(1)}%）` : ""}`,
              });
            }
          } else if (view.action === "建议买入") {
            seg.push({ k: "买入区间", v: `${view.buy_price_low.toFixed(2)} – ${view.buy_price_high.toFixed(2)}` });
            if (firstStop) {
              seg.push({ k: "止损", v: firstStop.price.toFixed(2) });
            }
          } else if (firstStop) {
            seg.push({
              k: "若持有，跌破离场",
              v: `${firstStop.price.toFixed(2)}${stopDist != null ? `（距现价 ${stopDist >= 0 ? "-" : "+"}${Math.abs(stopDist).toFixed(1)}%）` : ""}`,
            });
          }
          if (seg.length === 0) return null;
          return (
            <div style={{
              marginTop: 10, display: "flex", flexWrap: "wrap", gap: 14,
              fontSize: 13,
            }}>
              {seg.map((s, i) => (
                <span key={i}>
                  <span style={{ color: "var(--text-muted)" }}>{s.k} </span>
                  <b style={{ fontFamily: "monospace", color: "var(--text)" }}>{s.v}</b>
                </span>
              ))}
            </div>
          );
        })()}
        {kt.red_flags && kt.red_flags.length > 0 && (
          <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: 6 }}>
            {kt.red_flags.map((f, i) => (
              <span
                key={i}
                style={{
                  padding: "3px 8px",
                  borderRadius: 4,
                  background: "rgba(239, 68, 68, 0.15)",
                  color: "#fca5a5",
                  fontSize: 12,
                  border: "1px solid rgba(239, 68, 68, 0.3)",
                }}
              >
                🔴 {f}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 5/29: 价格已偏离 AI 推荐区间的提示 — 用户反馈"操作建议有效
          期"问题最直接的解法。买/卖类才提示;阈值 5% (5% 内属于正常
          波动)。观望/不入手不提示因为本来没建议入场。 */}
      <PriceAlertBanner
        current={currentPrice}
        kt={kt}
        actionable={view.action}
      />

      {/* 卖出线 S3:当前客观风险信号(live)。按漏斗盈亏档融合成护利/护本/观察的
          动作,框「客观提示·验证中」—— 不承诺卖得准,只对当下客观状态负责。 */}
      {sellRisk && sellRisk.triggers && sellRisk.triggers.length > 0 && (
        <SellRiskCard risk={sellRisk} held={funnel.held} pnl={funnel.pnl} />
      )}

      {/* 甜区漏斗（决策第一屏）：持仓 → 盈亏 → 风险，三行都"可点可不点"。
          一个有边框的容器框住，轻量 chip 行。默认 持有·盈·激进，已给一版建议。 */}
      <div style={{
        padding: 14, border: "1px solid var(--border)", borderRadius: 8,
        background: "var(--surface)", display: "flex", flexDirection: "column", gap: 10,
      }}>
        {/* ① 持仓 */}
        <FunnelRow label="持仓">
          <FunnelChip
            label="持有" active={funnel.held} color="#ef4444"
            onClick={() => updateFunnel({ held: true })}
          />
          <FunnelChip
            label="未持仓" active={!funnel.held} color="#9ca3af"
            onClick={() => updateFunnel({ held: false })}
          />
        </FunnelRow>
        {/* ② 盈亏（仅持有时显示） */}
        {funnel.held && (
          <FunnelRow label="盈亏">
            {PNL_DEFS.map(({ key, label, color }) => (
              <FunnelChip
                key={key} label={label} active={funnel.pnl === key} color={color}
                onClick={() => updateFunnel({ pnl: key })}
              />
            ))}
          </FunnelRow>
        )}
        {/* ③ 风险偏好（复用 TIER_DEFS）。仅在模型给了三档时可点；
            legacy 行没有 actionable_tiers，整行不渲染，跟原逻辑一致。 */}
        {tiers && (
          <FunnelRow label="风险">
            {TIER_DEFS.map(({ key, label, color }) => (
              <FunnelChip
                key={key} label={label} active={funnel.tier === key} color={color}
                onClick={() => updateFunnel({ tier: key })}
              />
            ))}
            <span style={{ color: "var(--text-faint)", fontSize: 11, marginLeft: 4 }}>
              （建议仓位 / 买卖价 / 持有时间会跟着切）
            </span>
          </FunnelRow>
        )}
      </div>

      {/* 漏斗出口：选中情境那句话 —— 替代原独立 ScenarioAdviceCard，
          由漏斗点选（持仓+盈亏）驱动，作为一句醒目建议。 */}
      {scenarioText && (
        <div style={{
          padding: "14px 16px", border: "1px solid var(--border)", borderRadius: 8,
          background: "rgba(59, 130, 246, 0.08)", borderLeft: "3px solid #3b82f6",
        }}>
          <div style={{ color: "var(--text-muted)", fontSize: 12, marginBottom: 6 }}>
            {funnel.held ? `若你${funnel.pnl === "盈" ? "大幅浮盈" : funnel.pnl === "亏" ? "大幅浮亏" : "小幅波动"}持有` : "若你尚未持仓"}
          </div>
          <div style={{ color: "var(--text)", fontSize: 15, lineHeight: 1.6 }}>
            {scenarioText}
          </div>
        </div>
      )}

      {/* 关键数据 table（driven by view=tiers[tier]）。按持仓裁剪：
          持有 → 突出 合理卖出价 / 建议仓位 / 持有时间；
          未持仓 → 突出 合理买入价 / 建议仓位。
          模型综合判断已移到下方降级区。 */}
      <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
        <div style={{ padding: "10px 14px", background: "var(--surface-alt)", color: "var(--text-muted)", fontSize: 12 }}>关键数据</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <tbody>
            {funnel.held ? (
              <>
                <KtRow label="合理卖出价" value={`${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}`} />
                <KtRow label="建议仓位" value={`${view.position_pct.toFixed(0)}%`} />
                <KtRow label="持有时间" value={view.hold_period} />
                <KtRow label="合理买入价（加仓参考）" value={`${view.buy_price_low.toFixed(2)} – ${view.buy_price_high.toFixed(2)}`} />
              </>
            ) : (
              <>
                <KtRow label="合理买入价" value={`${view.buy_price_low.toFixed(2)} – ${view.buy_price_high.toFixed(2)}`} />
                <KtRow label="建议仓位" value={`${view.position_pct.toFixed(0)}%`} />
                <KtRow label="合理卖出价（参考）" value={`${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}`} />
              </>
            )}
          </tbody>
        </table>
      </div>

      {/* Stop-loss tiers — most important for high-risk picks */}
      {kt.stop_loss_levels && kt.stop_loss_levels.length > 0 && (
        <StopLossCard levels={kt.stop_loss_levels} />
      )}

      {/* 降级区（漏斗下方）：模型自评置信度 / 次日走势 / 模型综合判断。
          这三块未校准，用更弱的视觉（整体降透明度 + 更小字 + 标"仅供参考"）
          包一下，提示用户"参考，别当准"。 */}
      <div style={{ opacity: 0.7, display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ color: "var(--text-faint)", fontSize: 11 }}>
          以下为模型自评，未经校准，仅供参考
        </div>
        {/* 5/28: 置信度独立块。低置信 + 买/卖 ⇒ dashed 黄边 警示。 */}
        <ConfidenceCard
          confidence={kt.confidence}
          reason={kt.confidence_reason}
          actionable={view.action}
        />
        {/* Phase 9: next-day price outlook (technical + capital + news driven) */}
        {kt.next_day_outlook && <NextDayOutlookCard outlook={kt.next_day_outlook} />}
        {/* 模型综合判断 —— 从关键数据移来，一行小字。 */}
        {kt.risk_scores?.overall && (
          <div style={{ fontSize: 12, color: "var(--text-soft)" }}>
            模型综合判断：<b style={{ color: "var(--text)" }}>{kt.risk_scores.overall}</b>
          </div>
        )}
      </div>
    </section>
  );
}

// 漏斗一行：左侧标签 + 右侧 chip 组。轻量行布局，复用现有间距风格。
// 卖出线 S3:当前状态风险卡。动作按持仓盈亏档**融合**(三线原则:解耦引擎、
// 融合表达)。仅给动作措辞,**不承诺有效性** —— 框「客观提示·验证中」,延续
// 诚实纪律(卖出还没战绩,不借买入信用、不说"卖得准")。
function SellRiskCard({ risk, held, pnl }: { risk: SellRisk; held: boolean; pnl: PnlBucket }) {
  const action = !held
    ? "你尚未持仓:这票当前状态转弱,暂不是买点(不催卖)。"
    : pnl === "盈"
    ? "护利:浮盈状态下,可优先考虑保护已有利润、锁定一部分。"
    : pnl === "亏"
    ? "护本:当初持有的理由在松动,控制回撤、守住本金。"
    : "观察:持有理由在转弱,留意,可考虑轻减。";
  return (
    <div style={{
      padding: "12px 14px", border: "1px solid var(--border)", borderRadius: 8,
      background: "rgba(245, 158, 11, 0.08)", borderLeft: "3px solid #f59e0b",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text)" }}>⚠️ 当前状态风险提示</span>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>客观信号 · 有效性验证中</span>
      </div>
      <ul style={{ margin: "0 0 8px", paddingLeft: 18, color: "var(--text-soft)", fontSize: 13, lineHeight: 1.6 }}>
        {risk.triggers.map((t) => <li key={t.key}>{t.reason}</li>)}
      </ul>
      <div style={{ fontSize: 13, color: "var(--text)", lineHeight: 1.6 }}>{action}</div>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 6, lineHeight: 1.5 }}>
        这是基于当前客观状态的提示,不是预测下跌;卖出信号的历史有效性我们还在攒数验证中。
      </div>
    </div>
  );
}

function FunnelRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span style={{ color: "var(--text-muted)", fontSize: 12, minWidth: 32 }}>{label}</span>
      {children}
    </div>
  );
}

// 漏斗点选 chip：选中 = 实色填充 + 深色字；未选 = 透明 + 边框。
// 复用现有 chip/button 内联样式语汇（圆角 + 细边 + 小字）。
function FunnelChip({
  label, active, color, onClick,
}: {
  label: string;
  active: boolean;
  color: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "4px 14px",
        borderRadius: 14,
        border: `1px solid ${active ? color : "var(--border)"}`,
        background: active ? color : "transparent",
        color: active ? "var(--bg)" : "var(--text-soft)",
        fontSize: 12,
        fontWeight: active ? 600 : 400,
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}


function NextDayOutlookCard({ outlook }: { outlook: NextDayOutlook }) {
  // 看涨/看平/看跌 → red/grey/green per A股 convention.
  const trendColor =
    outlook.trend === "看涨" ? "#ef4444" :
    outlook.trend === "看跌" ? "#22c55e" :
    "#9ca3af";
  const confidenceColor =
    outlook.confidence === "高" ? "#22c55e" :
    outlook.confidence === "低" ? "#facc15" :
    "#9ca3af";

  return (
    <section style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
      <div style={{
        padding: "10px 14px", background: "var(--surface-alt)",
        color: "var(--text-muted)", fontSize: 12,
        display: "flex", alignItems: "center", gap: 8,
      }}>
        <span>次日走势预判</span>
        <span style={{ color: "var(--text-dim)" }}>·</span>
        <span style={{ color: confidenceColor }}>模型自评 {outlook.confidence}</span>
        <span style={{ color: "var(--text-faint)", fontSize: 11 }}>· 仅供参考</span>
      </div>
      <div style={{ padding: "14px 16px" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 16, flexWrap: "wrap" }}>
          <span style={{ fontSize: 20, fontWeight: 600, color: trendColor }}>
            {outlook.trend}
          </span>
          <span style={{ color: "var(--text-soft)", fontSize: 13, fontFamily: "monospace" }}>
            模型预估区间 {outlook.target_low.toFixed(2)} – {outlook.target_high.toFixed(2)}
          </span>
        </div>
        {outlook.reasoning && (
          <p style={{ color: "var(--text)", fontSize: 13, marginTop: 8, marginBottom: 0, lineHeight: 1.5 }}>
            {outlook.reasoning}
          </p>
        )}
      </div>
    </section>
  );
}

function KtRow({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td style={{ padding: "8px 14px", color: "var(--text-muted)", fontSize: 13, width: "45%", borderBottom: "1px solid var(--border-faint)" }}>{label}</td>
      <td style={{ padding: "8px 14px", fontSize: 14, fontFamily: "monospace", borderBottom: "1px solid var(--border-faint)" }}>{value}</td>
    </tr>
  );
}

// 置信度卡片. 三种视觉状态:
//   high → 绿底浅边
//   med  → 中性灰
//   low (+ actionable=买/卖) → dashed 黄边 + "慎跟" 提示 (视觉降级,但
//     不改 actionable —— 让 LLM 自己的判断保留,用户看到 + 警示足矣)
//   confidence 为 null → 完全不渲染 (legacy row before this schema bump)
function ConfidenceCard({
  confidence, reason, actionable,
}: {
  confidence: string | number | null | undefined;
  reason?: string;
  actionable: string;
}) {
  if (confidence == null) return null;
  const bucket = confidenceBucket(confidence);
  const isActionable = actionable === "建议买入" || actionable === "建议卖出";
  const degraded = bucket === "low" && isActionable;
  const numericValue = typeof confidence === "number" ? confidence : null;
  const bg =
    bucket === "high" ? "rgba(34, 197, 94, 0.08)" :
    bucket === "low"  ? "rgba(245, 158, 11, 0.10)" :
                        "var(--surface)";
  const borderColor =
    bucket === "high" ? "rgba(34, 197, 94, 0.4)" :
    bucket === "low"  ? "#f59e0b" :
                        "var(--border)";
  const labelColor =
    bucket === "high" ? "#22c55e" :
    bucket === "low"  ? "#f59e0b" :
                        "#9ca3af";
  return (
    <div style={{
      padding: "12px 16px",
      background: bg,
      border: degraded ? `1px dashed ${borderColor}` : `1px solid ${borderColor}`,
      borderRadius: 8,
    }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <span style={{ color: "var(--text-muted)", fontSize: 13 }}>模型自评置信度</span>
        <span style={{ fontSize: 22, fontWeight: 600, color: labelColor, fontFamily: "monospace" }}>
          {numericValue != null ? `${numericValue}` : ""}
          {numericValue != null && <span style={{ fontSize: 13, color: "var(--text-faint)" }}> / 100</span>}
        </span>
        <span style={{
          padding: "2px 8px",
          borderRadius: 4,
          background: `${labelColor}22`,
          color: labelColor,
          fontSize: 11,
          fontWeight: 600,
        }}>
          {confidenceLabel(confidence)}
        </span>
        {degraded && (
          <span style={{ color: "#f59e0b", fontSize: 12 }}>⚠️ 低置信，慎跟</span>
        )}
      </div>
      {reason && (
        <div style={{ marginTop: 6, color: "var(--text-soft)", fontSize: 13, lineHeight: 1.5 }}>
          {reason}
        </div>
      )}
    </div>
  );
}

// 5/29: 价格已偏离 AI 推荐区间的提示。
// 用户反馈:"操作建议有效期"问题最直接的体现就是 — 建议买入 [10-12],
// 现在已经 13.5 了,我还跟吗?这个组件就是把这种状态显式标红。
// 阈值 5%:实际买卖区间通常本来就有一定波动允许度,严格按 high/low 卡
// 会噪声太多;5% 之外属于"明显偏离"。只对方向性的 actionable(买/卖)
// 提示;观望/不入手本来就没建议入场,不需要价格保护。
function PriceAlertBanner({
  current, kt, actionable,
}: {
  current: number | null;
  kt: KeyTable;
  actionable: string;
}) {
  if (current == null) return null;
  if (actionable !== "建议买入" && actionable !== "建议卖出") return null;

  const DELTA = 0.05;
  let msg: string | null = null;

  if (actionable === "建议买入") {
    if (current > kt.buy_price_high * (1 + DELTA)) {
      const pct = ((current - kt.buy_price_high) / kt.buy_price_high * 100).toFixed(1);
      msg = `当前价 ${current.toFixed(2)} 已超出 AI 推荐买入上限 ${kt.buy_price_high.toFixed(2)} 约 ${pct}%，可能已错过推荐区间,建议重新评估或重新生成解析`;
    } else if (current < kt.buy_price_low * (1 - DELTA)) {
      const pct = ((kt.buy_price_low - current) / kt.buy_price_low * 100).toFixed(1);
      msg = `当前价 ${current.toFixed(2)} 已低于 AI 推荐买入下限 ${kt.buy_price_low.toFixed(2)} 约 ${pct}%，警惕意外利空,建议重新评估`;
    }
  } else if (actionable === "建议卖出") {
    if (current < kt.sell_price_low * (1 - DELTA)) {
      const pct = ((kt.sell_price_low - current) / kt.sell_price_low * 100).toFixed(1);
      msg = `当前价 ${current.toFixed(2)} 已低于 AI 推荐卖出下限 ${kt.sell_price_low.toFixed(2)} 约 ${pct}%，可能已错过卖点`;
    } else if (current > kt.sell_price_high * (1 + DELTA)) {
      const pct = ((current - kt.sell_price_high) / kt.sell_price_high * 100).toFixed(1);
      msg = `当前价 ${current.toFixed(2)} 已超出 AI 推荐卖出上限 ${kt.sell_price_high.toFixed(2)} 约 ${pct}%，建议重新评估`;
    }
  }

  if (!msg) return null;
  return (
    <div style={{
      padding: "10px 14px",
      background: "rgba(239, 68, 68, 0.10)",
      border: "1px solid #dc2626",
      borderRadius: 8,
      color: "#dc2626",
      fontSize: 13,
      lineHeight: 1.5,
    }}>
      ⚠️ {msg}
    </div>
  );
}

// 6/18: 同业可比确定性卡。后台 compute_peers 算好(同行业 PE 最接近本股
// 5 支 + 本股),前端纯渲染——数字锁死、零幻觉、完整 5 支、每次一样。跟
// LLM 在 deep_analysis 里挑着说的叙述形成对照(那个会变会幻觉)。
// lazy load + silent fail + 不足 2 行(只本股)不渲染。
function PeerComparableCard({ code }: { code: string }) {
  const [peers, setPeers] = useState<PeerRow[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getPeers(code)
      .then((d) => { if (!cancelled) setPeers(d); })
      .catch(() => { if (!cancelled) setPeers([]); });  // silent: 补充信息,挂了不阻塞
    return () => { cancelled = true; };
  }, [code]);

  // 只有本股 1 行(无可比) → 不渲染整卡
  if (!peers || peers.filter((p) => !p.is_self).length < 1) return null;

  const fmt = (v: number | null, digits: number, suffix = "") =>
    v == null ? "—" : `${v.toFixed(digits)}${suffix}`;
  const fmtSigned = (v: number | null) =>
    v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

  const hasCross = peers.some((p) => p.is_cross_industry);

  const th: React.CSSProperties = {
    padding: "6px 8px", textAlign: "right", fontWeight: 500,
    color: "var(--text-muted)", whiteSpace: "nowrap",
  };
  const td: React.CSSProperties = {
    padding: "7px 8px", textAlign: "right", fontFamily: "monospace",
    whiteSpace: "nowrap",
  };

  return (
    <section style={{
      marginTop: 16, padding: 14,
      border: "1px solid var(--border)", borderRadius: 8,
      background: "var(--surface-alt)",
    }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
        <span style={{ fontSize: 13, color: "var(--text-muted)", fontWeight: 600 }}>同业可比</span>
        <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
          PE 最接近本股的可比股 · 数据直出不经 AI
        </span>
      </div>
      <div style={{ overflowX: "auto", margin: "0 -4px" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, minWidth: 460 }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border-faint)" }}>
              <th style={{ ...th, textAlign: "left" }}>股票</th>
              <th style={th}>PE</th>
              <th style={th}>PB</th>
              <th style={th}>营收增速</th>
              <th style={th}>ROE</th>
              <th style={th}>毛利率</th>
              <th style={th}>今日</th>
            </tr>
          </thead>
          <tbody>
            {peers.map((p) => {
              const cpColor =
                p.change_pct == null ? "var(--text-dim)"
                  : p.change_pct >= 0 ? "#ef4444" : "#22c55e";
              return (
                <tr key={p.code} style={{
                  borderBottom: "1px solid var(--border-faint)",
                  background: p.is_self ? "var(--surface)" : undefined,
                  borderLeft: p.is_self ? "2px solid var(--link)" : "2px solid transparent",
                }}>
                  <td style={{ padding: "7px 8px", textAlign: "left" }}>
                    <span style={{ fontWeight: p.is_self ? 700 : 400 }}>
                      {p.name || p.code}
                    </span>
                    {/* name 存在才补 code 小标;无 name 时上面已显 code,
                        不重复(避免 "002636 002636")。 */}
                    {p.name && (
                      <span style={{ color: "var(--text-dim)", fontFamily: "monospace", fontSize: 11, marginLeft: 5 }}>
                        {p.code}
                      </span>
                    )}
                    {p.is_self && (
                      <span style={{ color: "var(--link)", fontSize: 10, marginLeft: 5 }}>本股</span>
                    )}
                    {p.is_cross_industry && (
                      <span style={{ color: "var(--text-dim)", fontSize: 10, marginLeft: 5 }}>跨行业</span>
                    )}
                  </td>
                  <td style={td}>{fmt(p.pe_ratio, 1)}</td>
                  <td style={td}>{fmt(p.pb_ratio, 2)}</td>
                  <td style={td}>{fmtSigned(p.revenue_yoy)}</td>
                  <td style={td}>{fmt(p.roe, 1, "%")}</td>
                  <td style={td}>{fmt(p.gross_margin, 1, "%")}</td>
                  <td style={{ ...td, color: cpColor }}>{fmtSigned(p.change_pct)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {hasCross && (
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-dim)" }}>
          注:同行业可比股不足,部分为跨行业市值同量级 PE 接近股;「—」表示该股暂无财报数据。
        </div>
      )}
    </section>
  );
}

// 5/29: 历史解析折叠区。默认折叠,点击展开后是 table:
// 时间 / 建议(chip) / 置信度(label) / 当时价 / d1 / d3 / d5
// 第一行 = 当前最新分析,后面是历次 regenerate 的 anchor。让用户看到
// "AI 的判断是怎么演化的"——回应"刷新后建议变了"的焦虑。
// 没数据(code 无 anchor 历史)不渲染整个 card。
function AnalysisHistoryCard({ code }: { code: string }) {
  const [items, setItems] = useState<import("../../../lib/api").AnalysisHistoryItem[] | null>(null);
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.analysisHistory(code, 10)
      .then((d) => { if (!cancelled) setItems(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [code]);

  // No data, no card. Errors are quiet: this is supplementary info,
  // shouldn't drag down the page if outcomes endpoint is unhealthy.
  if (err || !items || items.length === 0) return null;

  return (
    <section style={{
      marginTop: 16,
      border: "1px solid var(--border)",
      borderRadius: 8,
      overflow: "hidden",
    }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          width: "100%",
          padding: "10px 14px",
          background: "var(--surface-alt)",
          color: "var(--text-muted)",
          fontSize: 13,
          textAlign: "left",
          border: "none",
          cursor: "pointer",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span>📊 历史解析 ({items.length} 次)</span>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          {open ? "▲ 收起" : "▼ 展开"}
        </span>
      </button>
      {open && (
        <div style={{ padding: "10px 14px" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                <th style={{ padding: "4px 6px", fontWeight: 500 }}>时间</th>
                <th style={{ padding: "4px 6px", fontWeight: 500 }}>建议</th>
                <th style={{ padding: "4px 6px", fontWeight: 500 }}>自评置信</th>
                <th style={{ padding: "4px 6px", fontWeight: 500, textAlign: "right" }}>当时价</th>
                <th style={{ padding: "4px 6px", fontWeight: 500, textAlign: "right" }}>d1</th>
                <th style={{ padding: "4px 6px", fontWeight: 500, textAlign: "right" }}>d3</th>
                <th style={{ padding: "4px 6px", fontWeight: 500, textAlign: "right" }}>d5</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it, i) => {
                const ACT_STYLE: Record<string, { color: string; label: string }> = {
                  "建议买入":   { color: "#ef4444", label: "买" },
                  "观望":       { color: "#9ca3af", label: "观望" },
                  "建议卖出":   { color: "#22c55e", label: "卖" },
                  "不建议入手": { color: "#6b7280", label: "不入" },
                };
                const style = ACT_STYLE[it.actionable] ?? { color: "#9ca3af", label: it.actionable };
                const confBucket = confidenceBucket(it.confidence);
                const confColor =
                  confBucket === "high" ? "#22c55e" :
                  confBucket === "low"  ? "#f59e0b" :
                                          "#9ca3af";
                const fmtPct = (v: number | null | undefined) => v == null ? "—" :
                  <span style={{ color: v >= 0 ? "#ef4444" : "#22c55e" }}>
                    {v >= 0 ? "+" : ""}{v.toFixed(2)}%
                  </span>;
                const ts = new Date(it.generated_at);
                const tsLabel = `${(ts.getMonth() + 1)}/${ts.getDate()} ${String(ts.getHours()).padStart(2, "0")}:${String(ts.getMinutes()).padStart(2, "0")}`;
                return (
                  <tr key={i} style={{ borderTop: i === 0 ? undefined : "1px solid var(--border-faint)" }}>
                    <td style={{ padding: "6px", fontFamily: "monospace", color: "var(--text-soft)" }}>{tsLabel}</td>
                    <td style={{ padding: "6px" }}>
                      <span style={{
                        padding: "1px 6px", borderRadius: 3, fontSize: 11, fontWeight: 600,
                        background: `${style.color}26`, color: style.color,
                      }}>
                        {style.label}
                      </span>
                    </td>
                    <td style={{ padding: "6px", color: confColor, fontWeight: 500 }}>
                      {it.confidence != null
                        ? `${confidenceLabel(it.confidence)}${typeof it.confidence === "number" ? ` (${it.confidence})` : ""}`
                        : "—"}
                    </td>
                    <td style={{ padding: "6px", fontFamily: "monospace", textAlign: "right" }}>
                      {it.anchor_price.toFixed(2)}
                    </td>
                    <td style={{ padding: "6px", fontFamily: "monospace", textAlign: "right" }}>{fmtPct(it.return_d1)}</td>
                    <td style={{ padding: "6px", fontFamily: "monospace", textAlign: "right" }}>{fmtPct(it.return_d3)}</td>
                    <td style={{ padding: "6px", fontFamily: "monospace", textAlign: "right" }}>{fmtPct(it.return_d5)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p style={{ marginTop: 8, color: "var(--text-faint)", fontSize: 11, lineHeight: 1.5 }}>
            d1 / d3 / d5 = 当时建议后 1 / 3 / 5 个交易日累计涨跌幅。「—」表示尚未到期或刚生成不久。
          </p>
        </div>
      )}
    </section>
  );
}

function StopLossCard({ levels }: { levels: StopLossLevel[] }) {
  const colorOf = (label: string) =>
    label === "紧急止损" ? "#ef4444" :
    label === "中线止损" ? "#facc15" :
    "#9ca3af";
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: "10px 14px", background: "var(--surface-alt)", color: "var(--text-muted)", fontSize: 12 }}>止损线</div>
      <div>
        {levels.map((lv, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "center",
              padding: "10px 14px",
              borderTop: i === 0 ? undefined : "1px solid var(--border-faint)",
              gap: 12,
            }}
          >
            <span style={{ color: colorOf(lv.label), fontSize: 12, fontWeight: 600, minWidth: 60 }}>
              🛡️ {lv.label}
            </span>
            <span style={{ fontFamily: "monospace", fontSize: 15, color: colorOf(lv.label), minWidth: 60 }}>
              {lv.price.toFixed(2)}
            </span>
            <span style={{ color: "var(--text-soft)", fontSize: 13, lineHeight: 1.5 }}>{lv.reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}


function DeepAnalysis({
  md, open, onToggle,
}: {
  md: string;
  open: boolean;
  onToggle: () => void;
}) {
  // S2: collapsed by default. The fold button advertises length so the
  // user knows what they're opening — chars ≈ reading commitment.
  const sectionCount = (md.match(/^##\s/gm) || []).length;
  if (!open) {
    return (
      <button
        onClick={onToggle}
        style={{
          marginTop: 16,
          width: "100%",
          padding: "12px 16px",
          background: "var(--surface)",
          border: "1px dashed var(--border-mid)",
          borderRadius: 8,
          color: "var(--text-soft)",
          fontSize: 13,
          cursor: "pointer",
          textAlign: "left",
        }}
      >
        📖 展开完整分析
        <span style={{ color: "var(--text-faint)", marginLeft: 8 }}>
          约 {Math.round(md.length / 100) * 100} 字{sectionCount > 0 ? ` · ${sectionCount} 节` : ""}
          （公司画像 / 股价剧情 / 看多 vs 看空 / 风险与止损 …）
        </span>
      </button>
    );
  }
  return (
    <section
      id="md-deep-analysis"
      style={{ marginTop: 16, padding: 16, border: "1px solid var(--border)", borderRadius: 8, lineHeight: 1.7, fontSize: 14 }}
    >
      <div style={{ textAlign: "right", marginBottom: 4 }}>
        <button
          onClick={onToggle}
          style={{
            background: "none", border: "none", color: "var(--text-muted)",
            fontSize: 12, cursor: "pointer", padding: 0,
          }}
        >
          收起 ▲
        </button>
      </div>
      {renderMarkdown(md)}
    </section>
  );
}

/**
 * Banner shown above the key-table when the analysis was generated in
 * "debate" mode. Tells the user what's different about this result and
 * points them to the 看多 vs 看空 section that holds the differentiating
 * content. Clickable: jumps to that section.
 */
function DebateBanner({ code, onJump }: { code: string; onJump: () => void }) {
  void code; // referenced for potential future per-code variation
  return (
    <a
      href="#md-h-看多-vs-看空"
      onClick={(e) => {
        e.preventDefault();
        // S2: deep analysis may be collapsed — the page-level handler
        // expands it first, then scrolls.
        onJump();
      }}
      style={{
        display: "block",
        marginTop: 16,
        padding: "10px 14px",
        border: "1px solid var(--border)",
        background: "rgba(59, 130, 246, 0.08)",
        borderRadius: 8,
        color: "var(--text)",
        textDecoration: "none",
        fontSize: 13,
      }}
    >
      <span style={{ fontWeight: 600 }}>🔬 这是深度解析结果</span>
      <span style={{ color: "var(--text-soft)" }}>
        {" "}· 已从看多和看空两个角度交叉验证。重点查看下方「看多 vs 看空」段 →
      </span>
    </a>
  );
}

function Footnote({ analysis }: { analysis: StockAnalysis }) {
  return (
    <p style={{ marginTop: 16, color: "var(--text-faint)", fontSize: 11, textAlign: "center" }}>
      策略 {analysis.strategy}
      {analysis.snapshot_id != null && <> · 基于 snapshot #{analysis.snapshot_id}</>}
      {/* 5/28: data_completeness 元信息. Surface the input quality the
          LLM had so users can mentally weight the verdict. Optional —
          missing on legacy rows. */}
      {analysis.data_completeness != null && (
        <> · 输入材料完整度 {analysis.data_completeness}/100</>
      )}
      <br />
      仅供参考，投资有风险，决策请独立判断。
    </p>
  );
}

// Hand-rolled markdown renderer. Block grammar:
//   ## heading
//   - / * unordered list item
//   1. / 2. ordered list item (number arbitrary; we keep model's numbering)
//   | a | b |  table row (with separator | --- | --- | on second row)
//   blank line   block break
//   anything else is a paragraph
//
// Inline grammar: **bold**. Lists support 2-space (or tab) indent for one
// nesting level — enough for the conversational style without pulling in
// a real markdown lib.
type ListItem = { text: string; depth: number };
type Block =
  | { kind: "h"; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul"; items: ListItem[] }
  | { kind: "ol"; items: ListItem[] }
  | { kind: "table"; header: string[]; rows: string[][] };

function parseBlocks(md: string): Block[] {
  const blocks: Block[] = [];
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, "");

    if (line.trim() === "") { i++; continue; }

    if (/^#{1,6}\s/.test(line)) {
      blocks.push({ kind: "h", text: line.replace(/^#+\s+/, "") });
      i++; continue;
    }

    // Markdown table: starts with `|` and the next line is the separator.
    if (line.startsWith("|") && i + 1 < lines.length && /^\|[\s|:-]+\|\s*$/.test(lines[i + 1])) {
      const header = splitTableRow(line);
      i += 2; // skip separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        rows.push(splitTableRow(lines[i]));
        i++;
      }
      blocks.push({ kind: "table", header, rows });
      continue;
    }

    // List run (unordered or ordered). The first line decides which kind;
    // subsequent indented bullets join in (one-level nesting).
    const u = matchUL(line);
    const o = matchOL(line);
    if (u || o) {
      const ordered = !!o;
      const items: ListItem[] = [];
      while (i < lines.length) {
        const cur = lines[i].replace(/\s+$/, "");
        if (cur.trim() === "") break;
        const um = matchUL(cur);
        const om = matchOL(cur);
        if (ordered && om) items.push({ text: om.text, depth: om.depth });
        else if (!ordered && um) items.push({ text: um.text, depth: um.depth });
        else if (ordered && um) items.push({ text: um.text, depth: um.depth });   // sub bullets under ordered
        else if (!ordered && om) items.push({ text: om.text, depth: om.depth });
        else break;
        i++;
      }
      blocks.push({ kind: ordered ? "ol" : "ul", items });
      continue;
    }

    // Paragraph: collect consecutive non-special lines.
    const paraStart = i;
    const buf: string[] = [];
    while (i < lines.length) {
      const cur = lines[i].replace(/\s+$/, "");
      if (cur.trim() === "") break;
      if (/^#{1,6}\s/.test(cur)) break;
      if (matchUL(cur) || matchOL(cur)) break;
      if (cur.startsWith("|")) break;
      buf.push(cur);
      i++;
    }
    if (buf.length) blocks.push({ kind: "p", text: buf.join(" ") });
    if (i === paraStart) i++; // safety
  }
  return blocks;
}

function matchUL(line: string): { text: string; depth: number } | null {
  const m = /^(\s*)[-*]\s+(.+)$/.exec(line);
  if (!m) return null;
  const depth = Math.min(2, Math.floor(m[1].replace(/\t/g, "  ").length / 2));
  return { text: m[2], depth };
}

function matchOL(line: string): { text: string; depth: number } | null {
  const m = /^(\s*)\d+[.)]\s+(.+)$/.exec(line);
  if (!m) return null;
  const depth = Math.min(2, Math.floor(m[1].replace(/\t/g, "  ").length / 2));
  return { text: m[2], depth };
}

function splitTableRow(line: string): string[] {
  return line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

// Derive a DOM-id from heading text so the debate banner / regen flow can
// scroll to a specific section (e.g. "看多 vs 看空"). Replaces whitespace
// with dashes, leaves CJK characters alone (they're valid in HTML ids).
function headingId(text: string): string {
  return "md-h-" + text.trim().replace(/\s+/g, "-");
}

function renderMarkdown(md: string): ReactNode[] {
  return parseBlocks(md).map((b, i) => {
    if (b.kind === "h") {
      return (
        <h3
          key={i}
          id={headingId(b.text)}
          style={{ fontSize: 15, margin: "20px 0 6px", color: "var(--text)",
                   borderBottom: "1px solid var(--border-soft)", paddingBottom: 4,
                   scrollMarginTop: 16 }}
        >
          {inline(b.text)}
        </h3>
      );
    }
    if (b.kind === "p") {
      return (
        <p key={i} style={{ margin: "8px 0", color: "var(--text)" }}>
          {inline(b.text)}
        </p>
      );
    }
    if (b.kind === "ul" || b.kind === "ol") {
      return renderList(b, i);
    }
    // table
    return (
      <div key={i} style={{ overflowX: "auto", margin: "12px 0" }}>
        <table style={{ borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr>
              {b.header.map((h, j) => (
                <th key={j} style={{ padding: "6px 12px", textAlign: "left",
                                     borderBottom: "1px solid var(--border-mid)", color: "var(--text-soft)", fontWeight: 500 }}>
                  {inline(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {b.rows.map((row, ri) => (
              <tr key={ri}>
                {row.map((cell, ci) => (
                  <td key={ci} style={{ padding: "6px 12px", borderBottom: "1px solid var(--border-faint)", color: "var(--text)" }}>
                    {inline(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  });
}

function renderList(
  b: { kind: "ul" | "ol"; items: ListItem[] },
  key: number,
): ReactNode {
  // Group items into a tree so depth-1 items become a nested list under
  // the previous depth-0 item. Two levels are enough for our use.
  type Node = { text: string; children: string[] };
  const top: Node[] = [];
  for (const it of b.items) {
    if (it.depth === 0 || top.length === 0) {
      top.push({ text: it.text, children: [] });
    } else {
      top[top.length - 1].children.push(it.text);
    }
  }
  const Tag = b.kind === "ol" ? "ol" : "ul";
  return (
    <Tag key={key} style={{ margin: "8px 0 8px 24px", padding: 0, color: "var(--text)" }}>
      {top.map((n, i) => (
        <li key={i} style={{ marginBottom: 4, lineHeight: 1.65 }}>
          {inline(n.text)}
          {n.children.length > 0 && (
            <ul style={{ margin: "4px 0 4px 20px", padding: 0 }}>
              {n.children.map((c, j) => (
                <li key={j} style={{ marginBottom: 2 }}>{inline(c)}</li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </Tag>
  );
}

function inline(s: string): ReactNode {
  // Bold first, then preserve the rest as plain text. Emojis and Chinese
  // pass through untouched.
  const parts = s.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) {
      return <strong key={i} style={{ color: "var(--text)" }}>{p.slice(2, -2)}</strong>;
    }
    return <span key={i}>{p}</span>;
  });
}
