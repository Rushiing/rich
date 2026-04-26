"use client";

import { use, useEffect, useState, type ReactNode } from "react";
import { api, KeyTable, StockAnalysis } from "../../../lib/api";

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
    kt.actionable === "建议买入" ? "#22c55e" :
    kt.actionable === "建议卖出" ? "#ef4444" :
    kt.actionable === "不建议入手" ? "#888" : "#facc15";

  return (
    <section style={{ marginTop: 16, border: "1px solid #2a2a2a", borderRadius: 8, overflow: "hidden" }}>
      <div style={{ padding: 16, background: "#141414" }}>
        <div style={{ fontSize: 22, fontWeight: 600, color: actionableColor }}>{kt.actionable}</div>
        <div style={{ marginTop: 4, color: "#aaa", fontSize: 13 }}>{kt.one_line_reason}</div>
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <tbody>
          <KtRow label="合理买入价" value={`${kt.buy_price_low.toFixed(2)} – ${kt.buy_price_high.toFixed(2)}`} />
          <KtRow label="合理卖出价" value={`${kt.sell_price_low.toFixed(2)} – ${kt.sell_price_high.toFixed(2)}`} />
          <KtRow label="建议仓位" value={`${kt.position_pct.toFixed(0)}%`} />
          <KtRow label="持有时间" value={kt.hold_period} />
          <KtRow label="止损线" value={kt.stop_loss.toFixed(2)} />
          <KtRow label="置信度" value={kt.confidence} />
        </tbody>
      </table>
    </section>
  );
}

function KtRow({ label, value }: { label: string; value: string }) {
  return (
    <tr>
      <td style={{ padding: "8px 16px", color: "#888", fontSize: 13, width: 110, borderBottom: "1px solid #1a1a1a" }}>{label}</td>
      <td style={{ padding: "8px 16px", fontSize: 14, fontFamily: "monospace", borderBottom: "1px solid #1a1a1a" }}>{value}</td>
    </tr>
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
