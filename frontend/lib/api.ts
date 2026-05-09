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
  risk_scores: RiskScores;
  confidence: string;
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
};

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

export const api = {
  // ---- auth ----
  me: () => request<Me>("/api/auth/me"),
  // ---- sectors ----
  listSectors: () => request<Sector[]>("/api/sectors"),
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
  generateAnalysis: (code: string) =>
    request<StockAnalysis>(`/api/stocks/${code}/analysis`, { method: "POST" }),
};
