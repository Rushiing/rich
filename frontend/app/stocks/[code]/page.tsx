"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import {
  api, ActionableTier, ActionableTiers, KeyTable, NextDayOutlook,
  ScenarioAdvice, StockAnalysis, StockDetail, StopLossLevel,
} from "../../../lib/api";
import Tooltip from "../../_components/Tooltip";

type TierKey = "aggressive" | "neutral" | "conservative";
const TIER_DEFS: { key: TierKey; label: string; color: string }[] = [
  { key: "aggressive",   label: "激进", color: "#ef4444" },
  { key: "neutral",      label: "中立", color: "#9ca3af" },
  { key: "conservative", label: "保守", color: "#22c55e" },
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function regenerate(mode: "single" | "debate" = "single") {
    setGenerating(true);
    setErr(null);
    try {
      const a = await api.generateAnalysis(code, mode);
      setAnalysis(a);
      // Deep mode: scroll users right to the "看多 vs 看空" section so
      // they see the cross-validation payoff immediately. Defer one frame
      // so the new markdown renders before we query the DOM.
      if (mode === "debate") {
        requestAnimationFrame(() => {
          // Anchor id derived from heading text in renderMarkdown (see
          // h3.id assignment below). Fallback to deep-analysis container.
          const heading = document.getElementById("md-h-看多-vs-看空")
            ?? document.getElementById("md-deep-analysis");
          heading?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setGenerating(false);
    }
  }

  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0, fontFamily: "monospace" }}>{code}</h1>
        <a href="/stocks" style={{ color: "#9ca3af", fontSize: 13, textDecoration: "none" }}>
          ← 返回盯盘
        </a>
      </header>

      {loading ? (
        <p style={{ color: "var(--text-faint)", marginTop: 24 }}>加载中…</p>
      ) : (
        <>
          {detail && <IndustryContextCard detail={detail} />}
          {!analysis ? (
            <EmptyState onGenerate={regenerate} generating={generating} err={err} />
          ) : (
            <>
              <FreshnessBar
                analysis={analysis}
                generating={generating}
                onRegenerate={() => regenerate("single")}
                onDebate={() => regenerate("debate")}
              />
              {err && <div style={{ color: "#ef4444", marginTop: 8, fontSize: 13 }}>{err}</div>}
              {analysis.mode === "debate" && <DebateBanner code={code} />}
              <KeyTableCard kt={analysis.key_table} />
              <DeepAnalysis md={analysis.deep_analysis} />
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


function EmptyState({ onGenerate, generating, err }: { onGenerate: () => void; generating: boolean; err: string | null }) {
  return (
    <div style={{ marginTop: 32, padding: 24, border: "1px solid var(--border)", borderRadius: 8, textAlign: "center" }}>
      <p style={{ color: "var(--text-soft)", fontSize: 14, margin: 0 }}>尚未生成深度解析</p>
      <p style={{ color: "var(--text-faint)", fontSize: 12, marginTop: 6 }}>
        生成会调一次 Claude API（基于该股票最新 snapshot），约 5–15 秒
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
        {!analysis.is_fresh && <span style={{ color: "#facc15", marginLeft: 8 }}>· 缓存已过期 (&gt;4h)</span>}
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

function KeyTableCard({ kt }: { kt: KeyTable }) {
  // Three-tier toggle state. Default to "neutral" — that's the LLM's
  // top-level actionable + position_pct, so the card initially shows what
  // it always showed pre-Phase-8.
  const [tier, setTier] = useState<TierKey>("neutral");
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

  return (
    <section style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Header card: actionable verdict + company portrait + red flags */}
      <div style={{ padding: 16, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
          <div style={{ fontSize: 22, fontWeight: 600, color: actionableColor }}>{view.action}</div>
          {kt.company_tag && (
            <div style={{ color: "#9ca3af", fontSize: 13 }}>{kt.company_tag}</div>
          )}
        </div>
        {/* Show the per-tier reason when the tier toggle exists; otherwise
            fall back to the global one_line_reason. */}
        {(tiers ? view.reason : kt.one_line_reason) && (
          <div style={{ marginTop: 6, color: "#d4d4d4", fontSize: 14 }}>
            {tiers ? view.reason : kt.one_line_reason}
          </div>
        )}
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

      {/* Tier toggle — only renders when the model actually emitted three
          tiers. Legacy rows (no actionable_tiers) skip the segmented
          control entirely so the layout stays the same. */}
      {tiers && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ color: "var(--text-muted)", fontSize: 12 }}>风险偏好：</span>
          <div style={{
            display: "inline-flex",
            border: "1px solid var(--border)",
            borderRadius: 6,
            overflow: "hidden",
          }}>
            {TIER_DEFS.map(({ key, label, color }) => {
              const active = tier === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setTier(key)}
                  style={{
                    padding: "5px 14px",
                    background: active ? color : "transparent",
                    color: active ? "var(--bg)" : "var(--text-soft)",
                    border: "none",
                    fontSize: 12,
                    fontWeight: active ? 600 : 400,
                    cursor: "pointer",
                  }}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <span style={{ color: "var(--text-faint)", fontSize: 11 }}>
            （表里"建议仓位 / 合理买入价 / 持有时间"会跟着切）
          </span>
        </div>
      )}

      {/* Key prices / positions table. The eight-dimension star scorecard
          was removed — every dimension was coming back at 5⭐ from the LLM,
          which is noise. We keep the synthesized "综合评级" inline below
          since that's the one risk signal that actually varies. */}
      <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
        <div style={{ padding: "10px 14px", background: "var(--surface-alt)", color: "var(--text-muted)", fontSize: 12 }}>关键数据</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <tbody>
            <KtRow label="合理买入价" value={`${view.buy_price_low.toFixed(2)} – ${view.buy_price_high.toFixed(2)}`} />
            <KtRow label="合理卖出价" value={`${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}`} />
            <KtRow label="建议仓位" value={`${view.position_pct.toFixed(0)}%`} />
            <KtRow label="持有时间" value={view.hold_period} />
            <KtRow label="置信度" value={kt.confidence} />
            {kt.risk_scores?.overall && (
              <KtRow label="综合评级" value={kt.risk_scores.overall} />
            )}
          </tbody>
        </table>
      </div>

      {/* Phase 9: next-day price outlook (technical + capital + news driven) */}
      {kt.next_day_outlook && <NextDayOutlookCard outlook={kt.next_day_outlook} />}

      {/* Stop-loss tiers — most important for high-risk picks */}
      {kt.stop_loss_levels && kt.stop_loss_levels.length > 0 && (
        <StopLossCard levels={kt.stop_loss_levels} />
      )}

      {/* Scenario-based advice — what to do based on current holding state */}
      {kt.scenario_advice && <ScenarioAdviceCard advice={kt.scenario_advice} />}
    </section>
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
        <span style={{ color: confidenceColor }}>置信度 {outlook.confidence}</span>
      </div>
      <div style={{ padding: "14px 16px" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 16, flexWrap: "wrap" }}>
          <span style={{ fontSize: 20, fontWeight: 600, color: trendColor }}>
            {outlook.trend}
          </span>
          <span style={{ color: "var(--text-soft)", fontSize: 13, fontFamily: "monospace" }}>
            目标区间 {outlook.target_low.toFixed(2)} – {outlook.target_high.toFixed(2)}
          </span>
        </div>
        {outlook.reasoning && (
          <p style={{ color: "#d4d4d4", fontSize: 13, marginTop: 8, marginBottom: 0, lineHeight: 1.5 }}>
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

function ScenarioAdviceCard({ advice }: { advice: ScenarioAdvice }) {
  const items: { label: string; text: string }[] = [
    { label: "未持仓",       text: advice.not_holding },
    { label: "已持仓 · 大幅浮盈", text: advice.holding_big_gain },
    { label: "已持仓 · 小幅",     text: advice.holding_small },
    { label: "已持仓 · 大幅浮亏", text: advice.holding_big_loss },
  ];
  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: "10px 14px", background: "var(--surface-alt)", color: "var(--text-muted)", fontSize: 12 }}>按持仓情境</div>
      <div>
        {items.map((it, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              padding: "10px 14px",
              borderTop: i === 0 ? undefined : "1px solid var(--border-faint)",
              gap: 12,
            }}
          >
            <span style={{ color: "var(--text-muted)", fontSize: 13, minWidth: 130 }}>{it.label}</span>
            <span style={{ color: "#d4d4d4", fontSize: 13, lineHeight: 1.5, flex: 1 }}>{it.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function DeepAnalysis({ md }: { md: string }) {
  return (
    <section
      id="md-deep-analysis"
      style={{ marginTop: 16, padding: 16, border: "1px solid var(--border)", borderRadius: 8, lineHeight: 1.7, fontSize: 14 }}
    >
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
function DebateBanner({ code }: { code: string }) {
  void code; // referenced for potential future per-code variation
  return (
    <a
      href="#md-h-看多-vs-看空"
      onClick={(e) => {
        e.preventDefault();
        const t = document.getElementById("md-h-看多-vs-看空")
          ?? document.getElementById("md-deep-analysis");
        t?.scrollIntoView({ behavior: "smooth", block: "start" });
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
        <p key={i} style={{ margin: "8px 0", color: "#d4d4d4" }}>
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
                  <td key={ci} style={{ padding: "6px 12px", borderBottom: "1px solid var(--border-faint)", color: "#d4d4d4" }}>
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
    <Tag key={key} style={{ margin: "8px 0 8px 24px", padding: 0, color: "#d4d4d4" }}>
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
      return <strong key={i} style={{ color: "#fff" }}>{p.slice(2, -2)}</strong>;
    }
    return <span key={i}>{p}</span>;
  });
}
