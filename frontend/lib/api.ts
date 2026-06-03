// Thin client for the backend API. All calls go through the Next.js catch-all
// proxy at /api/[...path], which forwards cookies and Set-Cookie headers.

export type WatchlistItem = {
  code: string;
  name: string;
  exchange: string;
  added_at: string;
  starred: boolean;
};

export type ImportResult = {
  added: WatchlistItem[];
  skipped_existing: string[];
  invalid_format: string[];   // ^\d{6}$ failed
  lookup_failed: string[];    // akshare didn't return a name — retryable
};

export type AnalysisBrief = {
  actionable: string;          // 建议买入 / 观望 / 建议卖出 / 不建议入手
  one_line_reason: string;
  company_tag: string;         // one-line portrait
  red_flags: string[];         // hard-detected risk markers
  created_at: string;
  is_fresh: boolean;           // < 4h
  // 5/28: confidence is int (new), legacy enum string ("高"/"中"/"低"), or
  // null for very old rows. Use confidenceBucket() to bucket.
  confidence?: number | string | null;
  confidence_reason?: string | null;
  // 6/3: AI-declared validity window, e.g. "3 个交易日内" / "跌破 X 元前".
  // Surfaced in list view as the last (highlighted) line of the
  // 操作建议 cell so users see decision freshness at a glance.
  valid_window?: string | null;
};

export type StopLossLevel = {
  price: number;
  label: "紧急止损" | "中线止损" | "深跌止损";
  reason: string;
};

export type ScenarioAdvice = {
  not_holding: string;
  holding_big_gain: string;
  holding_small: string;
  holding_big_loss: string;
};

export type RiskScores = {
  fundamentals: number;
  valuation: number;
  earnings_momentum: number;
  industry: number;
  governance: number;
  price_action: number;
  capital: number;
  thematic: number;
  overall: string;             // e.g. "⭐⭐⭐⭐ 较好"
};

export type StockRow = {
  code: string;
  name: string;
  exchange: string;
  last_ts: string | null;
  // 今日涨跌 — kept on the row even though the redesign hides "价格"
  change_pct: number | null;
  // Phase 7: 3-day rolling
  change_pct_3d: number | null;
  turnover_rate_3d: number | null;
  net_flow_3d: number | null;
  // Phase 7: industry context
  industry_name: string | null;
  industry_pe_pctile: number | null;
  industry_change_3d_pctile: number | null;
  industry_flow_3d_pctile: number | null;
  signals: string[];
  has_strong_signal: boolean;
  on_lhb: boolean;
  starred: boolean;
  analysis: AnalysisBrief | null;
};

export type StarToggleResult = { code: string; starred: boolean };

export type StockDetail = {
  code: string;
  name: string;
  exchange: string;
  last_ts: string | null;
  price: number | null;
  change_pct: number | null;
  main_net_flow: number | null;
  change_pct_3d: number | null;
  turnover_rate_3d: number | null;
  net_flow_3d: number | null;
  pe_ratio: number | null;
  pb_ratio: number | null;
  industry_name: string | null;
  industry_pe_pctile: number | null;
  industry_change_3d_pctile: number | null;
  industry_flow_3d_pctile: number | null;
  industry_pe_avg: number | null;
  industry_pb_avg: number | null;
  signals: string[];
  news: { title: string; url: string; ts: string }[];
  notices: { title: string; url: string; ts: string; type?: string | null }[];
  lhb: { name?: string; reason?: string; net_buy?: number | null } | null;
};

export type SnapshotTriggerResult = { started: boolean; already_running?: boolean };
export type SnapshotStatus = { running: boolean };
export type AnalysisBatchResult = { started: boolean; already_running?: boolean };
export type AnalysisBatchStatus = { running: boolean };

export type ActionableTier = {
  action: string;
  position_pct: number;
  buy_price_low: number;
  buy_price_high: number;
  hold_period: string;
  reason: string;
};

export type ActionableTiers = {
  aggressive: ActionableTier;
  neutral: ActionableTier;
  conservative: ActionableTier;
};

export type NextDayOutlook = {
  trend: string;          // 看涨 / 看平 / 看跌
  target_low: number;
  target_high: number;
  reasoning: string;
  confidence: string;
};

export type KeyTable = {
  company_tag: string;
  actionable: string;
  one_line_reason: string;
  red_flags: string[];
  buy_price_low: number;
  buy_price_high: number;
  sell_price_low: number;
  sell_price_high: number;
  position_pct: number;
  hold_period: string;
  stop_loss_levels: StopLossLevel[];
  scenario_advice: ScenarioAdvice;
  // Phase 8: optional because legacy cached rows from before this schema
  // bump won't have it. UI gracefully degrades.
  actionable_tiers?: ActionableTiers;
  // Phase 9: same — present on freshly generated rows, absent on legacy.
  next_day_outlook?: NextDayOutlook;
  risk_scores: RiskScores;
  // Top-level confidence. Pre-5/28: enum "高"/"中"/"低" (legacy rows).
  // Post-5/28: integer 0-100 (set via tool schema). Old rows are
  // backfilled via /api/_diag/migrate-confidence-to-int to 85/65/45 but
  // we keep the union type for graceful degradation if the migration
  // hasn't run yet. Use `confidenceBucket()` to normalize.
  confidence: string | number;
  // New 5/28: 1-sentence reason for the confidence score (≤30 字).
  // Optional because legacy rows pre-this-schema-bump don't carry it.
  confidence_reason?: string;
  // New 5/29: LLM-declared validity window for this verdict, e.g.
  // "3 个交易日内" / "跌破 12.50 元前" / "出 Q3 财报前". Surfaced on
  // detail page beside the confidence card so users know how long
  // the verdict is meant to apply. Optional for legacy rows.
  valid_window?: string;
};

export type StockAnalysis = {
  code: string;
  key_table: KeyTable;
  deep_analysis: string;
  model: string;
  strategy: string;
  created_at: string;
  snapshot_id: number | null;
  is_fresh: boolean;
  // "single" | "debate" — when "debate" the detail page shows a banner +
  // auto-scrolls to the 看多 vs 看空 section.
  mode?: string | null;
  // 0-100 data completeness score computed by the backend at analysis
  // time. Surfaced on the detail page as a small meta line so the user
  // knows how much input the LLM had. Optional for legacy rows.
  data_completeness?: number | null;
};

// 6/3: hit-rate summary for the buy/sell verdicts. Surfaced in list view
// tooltips + detail page so users see "AI 历史命中 60% (n=48)" alongside
// the actionable badge — building trust by showing the verdict has real
// calibration data behind it (and being honest about sample size).
export type HitRateBucket = {
  n: number;
  hit_rate: number | null;        // % (0-100); null for non-directional
  avg_return_d5: number | null;   // %; can be negative
};

export type HitRateSummary = {
  by_actionable: Record<string, HitRateBucket>;  // keyed by "建议买入" / "建议卖出"
  total_scored: number;
  cached_at: string;
};

// 5/29: one historical anchor row from AnalysisOutcome. Detail-page
// "历史解析" card shows the last N of these so users can see how the
// verdict + confidence shifted across regenerations, alongside the
// forward returns those anchors actually achieved.
export type AnalysisHistoryItem = {
  generated_at: string;            // ISO timestamp
  actionable: string;              // 建议买入 / 观望 / ...
  anchor_price: number;            // snapshot.price at generation time
  confidence?: number | null;      // 0-100, null for legacy rows
  data_completeness?: number | null;
  return_d1?: number | null;       // % return 1 trading day later (null = not yet)
  return_d3?: number | null;
  return_d5?: number | null;
  mode?: string | null;            // "single" | "debate"
  prompt_version?: string | null;
};

// ---------------------------------------------------------------------------
// confidence display utilities. Backend sometimes returns the legacy enum
// (老 analyses 直到 migrate-confidence-to-int 跑完前可能还是 "高"/"中"/"低"),
// sometimes a 0-100 integer. UI需要一个稳定的"3 档桶"做染色 + 视觉降级。
// ---------------------------------------------------------------------------

export type ConfidenceBucket = "high" | "med" | "low";

export function confidenceBucket(c: string | number | null | undefined): ConfidenceBucket {
  if (c == null) return "med"; // unknown → neutral
  if (typeof c === "number") {
    if (c >= 80) return "high";
    if (c >= 60) return "med";
    return "low";
  }
  // Legacy enum strings
  if (c === "高") return "high";
  if (c === "低") return "low";
  return "med";
}

// Short label shown in lists (1 char to keep cells compact). Detail
// pages show the number + bucket together.
export function confidenceLabel(c: string | number | null | undefined): string {
  const b = confidenceBucket(c);
  return b === "high" ? "高" : b === "low" ? "低" : "中";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

export type Me =
  | { ok: true; user_id: number; phone: string }
  | { ok: true; anonymous: true };

export type Sector = {
  name: string;
  code: string;
  company_count: number;
  avg_price: number | null;
  change_pct: number;
  total_volume: number | null;
  total_turnover: number | null;
  leader: {
    code: string;
    name: string;
    change_pct: number;
    price: number | null;
  };
};

export type SectorPick = {
  code: string;
  name: string;
  reason: string;
};

export type SectorPickGroup = {
  name: string;
  change_pct: number;
  reason: string;
  picks: SectorPick[];
};

export type SectorPicksResponse = {
  sectors: SectorPickGroup[];
  generated_at: string;
  is_fresh: boolean;
};

export type IndexQuote = {
  symbol: string;
  name: string;
  point: number;
  change_pct: number;
};

export type Holding = {
  code: string;
  cost_price: number;
  shares: number | null;
  opened_at: string | null;
  note: string | null;
  updated_at: string;
};

export type HoldingUpsert = {
  cost_price: number;
  shares?: number | null;
  opened_at?: string | null;
  note?: string | null;
};

export const api = {
  // ---- auth ----
  me: () => request<Me>("/api/auth/me"),
  // ---- market ----
  listIndices: () => request<IndexQuote[]>("/api/market/indices"),
  // ---- sectors ----
  listSectors: () => request<Sector[]>("/api/sectors"),
  getSectorPicks: () => request<SectorPicksResponse>("/api/sectors/picks"),
  refreshSectorPicks: () =>
    request<SectorPicksResponse>("/api/sectors/picks/refresh", { method: "POST" }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  // ---- watchlist + stocks ----
  listWatchlist: () => request<WatchlistItem[]>("/api/watchlist"),
  importCodes: (raw: string) =>
    request<ImportResult>("/api/watchlist/import", {
      method: "POST",
      body: JSON.stringify({ raw }),
    }),
  deleteCode: (code: string) =>
    request<{ ok: boolean }>(`/api/watchlist/${code}`, { method: "DELETE" }),
  bulkDelete: (raw: string) =>
    request<{ deleted: string[]; not_found: string[] }>(
      "/api/watchlist/bulk-delete",
      { method: "POST", body: JSON.stringify({ raw }) },
    ),
  toggleStar: (code: string) =>
    request<StarToggleResult>(`/api/watchlist/${code}/star`, { method: "POST" }),
  listStocks: () => request<StockRow[]>("/api/stocks"),
  stockDetail: (code: string) => request<StockDetail>(`/api/stocks/${code}`),
  triggerSnapshot: () =>
    request<SnapshotTriggerResult>("/api/stocks/snapshot", { method: "POST" }),
  snapshotStatus: () => request<SnapshotStatus>("/api/stocks/snapshot/status"),
  triggerBatchAnalysis: (opts: { onlyMissing: boolean }) =>
    request<AnalysisBatchResult>(
      `/api/stocks/analysis/batch?only_missing=${opts.onlyMissing}`,
      { method: "POST" },
    ),
  batchAnalysisStatus: () =>
    request<AnalysisBatchStatus>("/api/stocks/analysis/batch/status"),
  getAnalysis: (code: string) =>
    request<StockAnalysis | null>(`/api/stocks/${code}/analysis`),
  // 5/29: force=true bypasses the snapshot-id cache. Detail page sets
  // force=true for the user-pressed "重新生成" button (user explicitly
  // wants a fresh take); batch flows don't.
  generateAnalysis: (
    code: string,
    mode: "single" | "debate" = "single",
    opts: { force?: boolean } = {},
  ) =>
    request<StockAnalysis>(
      `/api/stocks/${code}/analysis?mode=${mode}${opts.force ? "&force=true" : ""}`,
      { method: "POST" },
    ),
  analysisHistory: (code: string, limit = 10) =>
    request<AnalysisHistoryItem[]>(
      `/api/stocks/${code}/analysis-history?limit=${limit}`,
    ),
  hitRateSummary: () =>
    request<HitRateSummary>(`/api/stocks/hit-rate-summary`),
  // ---- holdings (cost basis) ----
  getHolding: (code: string) =>
    request<Holding | null>(`/api/holdings/${code}`),
  upsertHolding: (code: string, body: HoldingUpsert) =>
    request<Holding>(`/api/holdings/${code}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  deleteHolding: (code: string) =>
    request<{ ok: boolean }>(`/api/holdings/${code}`, { method: "DELETE" }),
};
