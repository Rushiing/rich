"use client";

/**
 * /pool/[code] — 预选池专属详情页(区域分离:这是"系统推荐预选",不是
 * 用户自选。绝不跟 /stocks/[code] 混)。
 *
 * 展示:
 *  - 入池卡:状态/通道/入池价/现价/收益/回撤/观察天数/批次/thesis 证据/失效线
 *  - AI 解析:晋升时挂的深度解析(同一套语言),结论行 + deep_analysis 正文
 *
 * deep_analysis 用自包含的轻量 markdown 渲染(标题/段落/列表/加粗),不复用
 * /stocks 那套重渲染(避免跨页耦合 + 回归)。
 */

import { use, useEffect, useState, type ReactNode } from "react";
import { api, PoolDetail, confidenceBucket } from "../../../lib/api";

const ACTIONABLE_COLOR: Record<string, string> = {
  "建议买入": "#ef4444",
  "建议卖出": "#22c55e",
  "观望": "#9ca3af",
  "不建议入手": "#6b7280",
};

const STATE_LABEL: Record<string, { label: string; color: string }> = {
  recommendable: { label: "可推荐", color: "#22c55e" },
  observing: { label: "观察中", color: "#9ca3af" },
  recommended: { label: "已推荐", color: "#3b82f6" },
  eliminated: { label: "已淘汰", color: "#6b7280" },
};

const SOURCE_LABEL: Record<string, string> = {
  rules: "规则信号",
  sector_picks: "板块精选",
};

export default function PoolDetailPage({
  params,
}: {
  params: Promise<{ code: string }>;
}) {
  const { code } = use(params);
  const [data, setData] = useState<PoolDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.poolDetail(code)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [code]);

  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
          {/* 区域标识:明确"系统推荐预选",跟自选详情区分 */}
          <span style={{
            fontSize: 11, color: "#3b82f6", border: "1px solid #3b82f6",
            borderRadius: 4, padding: "1px 6px",
          }}>
            系统推荐预选
          </span>
          {data?.entry?.name && <h1 style={{ fontSize: 18, margin: 0 }}>{data.entry.name}</h1>}
          <span style={{ fontFamily: "monospace", color: "var(--text-faint)", fontSize: 15 }}>{code}</span>
        </div>
        <a href="/pool" style={{ color: "var(--text-soft)", fontSize: 13, textDecoration: "none" }}>
          ← 返回预选池
        </a>
      </header>

      {err ? (
        <p style={{ color: "#ef4444", marginTop: 24 }}>加载失败:{err}</p>
      ) : !data ? (
        <p style={{ color: "var(--text-faint)", marginTop: 24 }}>加载中…</p>
      ) : (
        <>
          <EntryCard entry={data.entry} />
          {data.analysis
            ? <AnalysisView analysis={data.analysis} />
            : (
              <p style={{ marginTop: 16, color: "var(--text-faint)", fontSize: 13 }}>
                该票尚无 AI 解析(晋升成"可推荐"时自动生成)。
              </p>
            )}
        </>
      )}
    </main>
  );
}

function EntryCard({ entry: e }: { entry: PoolDetail["entry"] }) {
  const st = STATE_LABEL[e.state] ?? { label: e.state, color: "#9ca3af" };
  const retColor = e.return_pct == null ? "var(--text-muted)"
    : e.return_pct >= 0 ? "#ef4444" : "#22c55e";
  const cohortLabel = e.cohort_week
    ? `第 ${e.cohort_week.split("-W")[1] ?? e.cohort_week} 周批次`
    : null;
  return (
    <section style={{
      marginTop: 16, padding: 16,
      border: "1px solid var(--border)", borderRadius: 8, background: "var(--surface-alt)",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <span style={{
          fontSize: 12, fontWeight: 600, color: st.color,
          border: `1px solid ${st.color}`, borderRadius: 4, padding: "1px 8px",
        }}>{st.label}</span>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          来源:{SOURCE_LABEL[e.source] ?? e.source}
        </span>
        {cohortLabel && (
          <span style={{ fontSize: 12, color: "#3b82f6" }}>· {cohortLabel}</span>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: "8px 16px", fontSize: 13 }}>
        <Stat label="入池价" value={`${e.entry_close?.toFixed(2)} (${e.entry_date})`} />
        <Stat label="现价" value={e.last_close != null ? `${e.last_close.toFixed(2)}${e.last_date ? ` (${e.last_date})` : ""}` : "—"} />
        <Stat label="累计收益" value={e.return_pct != null ? `${e.return_pct >= 0 ? "+" : ""}${e.return_pct.toFixed(2)}%` : "—"} color={retColor} bold />
        <Stat label="最大回撤" value={e.max_drawdown_pct != null ? `${e.max_drawdown_pct.toFixed(2)}%` : "—"} />
        <Stat label="观察天数" value={`${e.days_observed} 日`} />
      </div>

      {e.thesis?.summary && (
        <div style={{ marginTop: 12, color: "var(--text)", fontSize: 13, lineHeight: 1.6 }}>
          <span style={{ color: "var(--text-muted)" }}>入池逻辑:</span> {e.thesis.summary}
        </div>
      )}
      {e.thesis?.evidence?.length > 0 && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18, color: "var(--text-soft)", fontSize: 12, lineHeight: 1.6 }}>
          {e.thesis.evidence.map((ev, i) => <li key={i}>{ev}</li>)}
        </ul>
      )}
      {e.thesis?.invalidation_rule && (
        <div style={{ marginTop: 8, color: "#f59e0b", fontSize: 12 }}>
          ⏱ 失效线:{e.thesis.invalidation_rule}
        </div>
      )}
      {e.eliminated_reason && (
        <div style={{ marginTop: 8, color: "#ef4444", fontSize: 12 }}>
          已淘汰:{e.eliminated_reason}
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, color, bold }: { label: string; value: string; color?: string; bold?: boolean }) {
  return (
    <div>
      <div style={{ color: "var(--text-muted)", fontSize: 11 }}>{label}</div>
      <div style={{ color: color ?? "var(--text)", fontFamily: "monospace", fontWeight: bold ? 700 : 400, marginTop: 2 }}>
        {value}
      </div>
    </div>
  );
}

function AnalysisView({ analysis }: { analysis: NonNullable<PoolDetail["analysis"]> }) {
  const kt = analysis.key_table;
  const color = ACTIONABLE_COLOR[kt.actionable] ?? "#9ca3af";
  const confNum = typeof kt.confidence === "number" ? kt.confidence : null;
  const confBucket = confidenceBucket(kt.confidence);
  const confColor = confBucket === "high" ? "#22c55e" : confBucket === "low" ? "#f59e0b" : "#9ca3af";
  return (
    <>
      {/* 结论行 */}
      <section style={{
        marginTop: 16, padding: 16,
        border: "1px solid var(--border)", borderRadius: 8, background: "var(--surface)",
      }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
          <span style={{ fontSize: 20, fontWeight: 600, color }}>{kt.actionable}</span>
          {kt.valid_window && (
            <span style={{
              fontSize: 12, color: "var(--text-soft)", background: "var(--surface-alt)",
              border: "1px solid var(--border-faint)", borderRadius: 4, padding: "2px 8px",
            }}>⏱ {kt.valid_window}</span>
          )}
          <span style={{ fontSize: 12, color: confColor }}>
            置信 {confNum != null ? `${confNum}/100` : confBucket}
          </span>
        </div>
        {kt.one_line_reason && (
          <div style={{ marginTop: 8, color: "var(--text)", fontSize: 14 }}>{kt.one_line_reason}</div>
        )}
        {kt.confidence_reason && (
          <div style={{ marginTop: 4, color: "var(--text-soft)", fontSize: 12 }}>{kt.confidence_reason}</div>
        )}
      </section>

      {/* deep_analysis 正文(轻量 markdown) */}
      {analysis.deep_analysis && (
        <section style={{
          marginTop: 16, padding: 16,
          border: "1px solid var(--border)", borderRadius: 8, lineHeight: 1.7, fontSize: 14,
        }}>
          {renderLite(analysis.deep_analysis)}
        </section>
      )}

      <p style={{ marginTop: 12, color: "var(--text-faint)", fontSize: 11, textAlign: "center" }}>
        AI 解析仅供参考,投资有风险,决策请独立判断。
      </p>
    </>
  );
}

// 轻量 markdown:## 标题 / 段落 / 列表(- 或 1.) / **加粗**。够呈现
// deep_analysis,不做 table(很少用)。自包含,不耦合 /stocks 渲染。
function renderLite(md: string): ReactNode[] {
  const out: ReactNode[] = [];
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let listBuf: string[] = [];
  const flushList = (key: string) => {
    if (listBuf.length) {
      out.push(
        <ul key={key} style={{ margin: "8px 0", paddingLeft: 20, color: "var(--text)" }}>
          {listBuf.map((it, i) => <li key={i} style={{ margin: "3px 0" }}>{bold(it)}</li>)}
        </ul>
      );
      listBuf = [];
    }
  };
  lines.forEach((raw, idx) => {
    const t = raw.trim();
    if (!t) { flushList(`l${idx}`); return; }
    if (/^#{1,6}\s/.test(t)) {
      flushList(`l${idx}`);
      out.push(
        <h3 key={idx} style={{ fontSize: 15, margin: "18px 0 6px", color: "var(--text)", borderBottom: "1px solid var(--border-soft)", paddingBottom: 4 }}>
          {bold(t.replace(/^#+\s+/, ""))}
        </h3>
      );
      return;
    }
    const li = /^([-*]|\d+[.)])\s+(.+)$/.exec(t);
    if (li) { listBuf.push(li[2]); return; }
    flushList(`l${idx}`);
    out.push(<p key={idx} style={{ margin: "8px 0", color: "var(--text)" }}>{bold(t)}</p>);
  });
  flushList("last");
  return out;
}

// **加粗** → <strong>
function bold(s: string): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*)/g).map((p, i) =>
    p.startsWith("**") && p.endsWith("**")
      ? <strong key={i}>{p.slice(2, -2)}</strong>
      : <span key={i}>{p}</span>
  );
}
