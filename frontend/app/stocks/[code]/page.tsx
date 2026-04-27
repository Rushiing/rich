"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import {
  api, KeyTable, RiskScores, ScenarioAdvice, StockAnalysis, StopLossLevel,
} from "../../../lib/api";

const RISK_DIM_LABEL: Record<keyof Omit<RiskScores, "overall">, string> = {
  fundamentals: "基本面",
  valuation: "估值",
  earnings_momentum: "业绩兑现",
  industry: "行业景气度",
  governance: "公司治理",
  price_action: "股价表现",
  capital: "资金面",
  thematic: "题材炒作",
};

export default function StockDetailPage({
  params,
}: {
  params: Promise<{ code: string }>;
}) {
  const { code } = use(params);
  const [analysis, setAnalysis] = useState<StockAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function loadCached() {
    setLoading(true);
    try {
      const a = await api.getAnalysis(code);
      setAnalysis(a);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadCached();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  async function regenerate() {
    setGenerating(true);
    setErr(null);
    try {
      const a = await api.generateAnalysis(code);
      setAnalysis(a);
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
        <p style={{ color: "#666", marginTop: 24 }}>加载中…</p>
      ) : !analysis ? (
        <EmptyState onGenerate={regenerate} generating={generating} err={err} />
      ) : (
        <>
          <FreshnessBar
            analysis={analysis}
            generating={generating}
            onRegenerate={regenerate}
          />
          {err && <div style={{ color: "#ef4444", marginTop: 8, fontSize: 13 }}>{err}</div>}
          <KeyTableCard kt={analysis.key_table} />
          <DeepAnalysis md={analysis.deep_analysis} />
          <Footnote analysis={analysis} />
        </>
      )}
    </main>
  );
}

function EmptyState({ onGenerate, generating, err }: { onGenerate: () => void; generating: boolean; err: string | null }) {
  return (
    <div style={{ marginTop: 32, padding: 24, border: "1px solid #2a2a2a", borderRadius: 8, textAlign: "center" }}>
      <p style={{ color: "#aaa", fontSize: 14, margin: 0 }}>尚未生成深度解析</p>
      <p style={{ color: "#666", fontSize: 12, marginTop: 6 }}>
        生成会调一次 Claude API（基于该股票最新 snapshot），约 5–15 秒
      </p>
      <button
        onClick={onGenerate}
        disabled={generating}
        style={{
          marginTop: 16,
          padding: "8px 16px",
          background: generating ? "#444" : "#3b82f6",
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

function FreshnessBar({ analysis, generating, onRegenerate }: { analysis: StockAnalysis; generating: boolean; onRegenerate: () => void }) {
  return (
    <div style={{ marginTop: 16, display: "flex", alignItems: "center", justifyContent: "space-between", color: "#888", fontSize: 12 }}>
      <span>
        生成于 {new Date(analysis.created_at).toLocaleString("zh-CN")}
        {!analysis.is_fresh && <span style={{ color: "#facc15", marginLeft: 8 }}>· 缓存已过期 (&gt;4h)</span>}
        <span style={{ marginLeft: 8 }}>· 模型 {analysis.model}</span>
      </span>
      <button
        onClick={onRegenerate}
        disabled={generating}
        style={{
          padding: "4px 10px",
          background: "transparent",
          color: "#aaa",
          border: "1px solid #333",
          borderRadius: 4,
          fontSize: 12,
          cursor: generating ? "not-allowed" : "pointer",
        }}
      >
        {generating ? "生成中…" : "重新生成"}
      </button>
    </div>
  );
}

function KeyTableCard({ kt }: { kt: KeyTable }) {
  const actionableColor =
    kt.actionable === "建议买入" ? "#ef4444" :   // A股语境：红=买/涨
    kt.actionable === "建议卖出" ? "#22c55e" :   // 绿=卖/跌
    kt.actionable === "不建议入手" ? "#6b7280" :
    "#9ca3af";                                   // 观望

  return (
    <section style={{ marginTop: 16, display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Header card: actionable verdict + company portrait + red flags */}
      <div style={{ padding: 16, background: "#141414", border: "1px solid #2a2a2a", borderRadius: 8 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
          <div style={{ fontSize: 22, fontWeight: 600, color: actionableColor }}>{kt.actionable}</div>
          {kt.company_tag && (
            <div style={{ color: "#9ca3af", fontSize: 13 }}>{kt.company_tag}</div>
          )}
        </div>
        {kt.one_line_reason && (
          <div style={{ marginTop: 6, color: "#d4d4d4", fontSize: 14 }}>{kt.one_line_reason}</div>
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

      {/* Two-column: key prices/positions on left, risk scorecard on right */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div style={{ border: "1px solid #2a2a2a", borderRadius: 8, overflow: "hidden" }}>
          <div style={{ padding: "10px 14px", background: "#0f0f0f", color: "#888", fontSize: 12 }}>关键数据</div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <tbody>
              <KtRow label="合理买入价" value={`${kt.buy_price_low.toFixed(2)} – ${kt.buy_price_high.toFixed(2)}`} />
              <KtRow label="合理卖出价" value={`${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}`} />
              <KtRow label="建议仓位" value={`${kt.position_pct.toFixed(0)}%`} />
              <KtRow label="持有时间" value={kt.hold_period} />
              <KtRow label="置信度" value={kt.confidence} />
              {kt.risk_scores?.overall && (
                <KtRow label="综合评级" value={kt.risk_scores.overall} />
              )}
            </tbody>
          </table>
        </div>
        {kt.risk_scores && <RiskScoreCard scores={kt.risk_scores} />}
      </div>

      {/* Stop-loss tiers — most important for high-risk picks */}
      {kt.stop_loss_levels && kt.stop_loss_levels.length > 0 && (
        <StopLossCard levels={kt.stop_loss_levels} />
      )}

      {/* Scenario-based advice — what to do based on current holding state */}
      {kt.scenario_advice && <ScenarioAdviceCard advice={kt.scenario_advice} />}
    </section>
  );
}

function KtRow({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td style={{ padding: "8px 14px", color: "#888", fontSize: 13, width: "45%", borderBottom: "1px solid #1a1a1a" }}>{label}</td>
      <td style={{ padding: "8px 14px", fontSize: 14, fontFamily: "monospace", borderBottom: "1px solid #1a1a1a" }}>{value}</td>
    </tr>
  );
}

function RiskScoreCard({ scores }: { scores: RiskScores }) {
  const dims = Object.entries(RISK_DIM_LABEL) as [keyof typeof RISK_DIM_LABEL, string][];
  return (
    <div style={{ border: "1px solid #2a2a2a", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: "10px 14px", background: "#0f0f0f", color: "#888", fontSize: 12 }}>风险评分</div>
      <div style={{ padding: 4 }}>
        {dims.map(([key, label]) => (
          <div key={key} style={{ display: "flex", alignItems: "center", padding: "6px 10px" }}>
            <span style={{ color: "#888", fontSize: 13, flex: 1 }}>{label}</span>
            <span style={{ fontSize: 13, color: "#facc15", letterSpacing: 1 }}>
              {"⭐".repeat(scores[key])}
              <span style={{ color: "#333" }}>{"⭐".repeat(5 - scores[key])}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function StopLossCard({ levels }: { levels: StopLossLevel[] }) {
  const colorOf = (label: string) =>
    label === "紧急止损" ? "#ef4444" :
    label === "中线止损" ? "#facc15" :
    "#9ca3af";
  return (
    <div style={{ border: "1px solid #2a2a2a", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: "10px 14px", background: "#0f0f0f", color: "#888", fontSize: 12 }}>止损线</div>
      <div>
        {levels.map((lv, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "center",
              padding: "10px 14px",
              borderTop: i === 0 ? undefined : "1px solid #1a1a1a",
              gap: 12,
            }}
          >
            <span style={{ color: colorOf(lv.label), fontSize: 12, fontWeight: 600, minWidth: 60 }}>
              🛡️ {lv.label}
            </span>
            <span style={{ fontFamily: "monospace", fontSize: 15, color: colorOf(lv.label), minWidth: 60 }}>
              {lv.price.toFixed(2)}
            </span>
            <span style={{ color: "#aaa", fontSize: 13, lineHeight: 1.5 }}>{lv.reason}</span>
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
    <div style={{ border: "1px solid #2a2a2a", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: "10px 14px", background: "#0f0f0f", color: "#888", fontSize: 12 }}>按持仓情境</div>
      <div>
        {items.map((it, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              padding: "10px 14px",
              borderTop: i === 0 ? undefined : "1px solid #1a1a1a",
              gap: 12,
            }}
          >
            <span style={{ color: "#888", fontSize: 13, minWidth: 130 }}>{it.label}</span>
            <span style={{ color: "#d4d4d4", fontSize: 13, lineHeight: 1.5, flex: 1 }}>{it.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function DeepAnalysis({ md }: { md: string }) {
  return (
    <section style={{ marginTop: 16, padding: 16, border: "1px solid #2a2a2a", borderRadius: 8, lineHeight: 1.7, fontSize: 14 }}>
      {renderMarkdown(md)}
    </section>
  );
}

function Footnote({ analysis }: { analysis: StockAnalysis }) {
  return (
    <p style={{ marginTop: 16, color: "#555", fontSize: 11, textAlign: "center" }}>
      策略 {analysis.strategy}
      {analysis.snapshot_id != null && <> · 基于 snapshot #{analysis.snapshot_id}</>}
      <br />
      仅供参考，投资有风险，决策请独立判断。
    </p>
  );
}

// Minimal markdown: ## headings, paragraphs, - lists, **bold**.
function renderMarkdown(md: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const lines = md.split(/\r?\n/);
  let listBuf: string[] = [];
  let paraBuf: string[] = [];

  const flushList = () => {
    if (!listBuf.length) return;
    blocks.push(
      <ul key={`u${blocks.length}`} style={{ margin: "8px 0 8px 20px", padding: 0 }}>
        {listBuf.map((l, i) => <li key={i} style={{ marginBottom: 4 }}>{inline(l)}</li>)}
      </ul>
    );
    listBuf = [];
  };
  const flushPara = () => {
    if (!paraBuf.length) return;
    blocks.push(
      <p key={`p${blocks.length}`} style={{ margin: "8px 0", color: "#d4d4d4" }}>
        {inline(paraBuf.join(" "))}
      </p>
    );
    paraBuf = [];
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (/^#{1,6}\s/.test(line)) {
      flushPara(); flushList();
      const text = line.replace(/^#+\s+/, "");
      blocks.push(
        <h3 key={`h${blocks.length}`} style={{ fontSize: 15, margin: "16px 0 4px", color: "#e5e5e5", borderBottom: "1px solid #222", paddingBottom: 4 }}>
          {text}
        </h3>
      );
    } else if (/^-\s/.test(line)) {
      flushPara();
      listBuf.push(line.replace(/^-\s+/, ""));
    } else if (line.trim() === "") {
      flushPara(); flushList();
    } else {
      flushList();
      paraBuf.push(line);
    }
  }
  flushPara(); flushList();
  return blocks;
}

function inline(s: string): ReactNode {
  const parts = s.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((p, i) => {
    if (p.startsWith("**") && p.endsWith("**")) {
      return <strong key={i} style={{ color: "#fff" }}>{p.slice(2, -2)}</strong>;
    }
    return <span key={i}>{p}</span>;
  });
}
