// Thin client for the backend API. All calls go through the Next.js catch-all
// proxy at /api/[...path], which forwards cookies and Set-Cookie headers.

export type WatchlistItem = {
  code: string;
  name: string;
  exchange: string;
  added_at: string;
};

export type ImportResult = {
  added: WatchlistItem[];
  skipped_existing: string[];
  invalid: string[];
};

export type StockRow = {
  code: string;
  name: string;
  exchange: string;
  last_ts: string | null;
  price: number | null;
  change_pct: number | null;
  main_net_flow: number | null;
  signals: string[];
  has_strong_signal: boolean;
  news_count: number;
  notices_count: number;
  on_lhb: boolean;
};

export type StockDetail = {
  code: string;
  name: string;
  exchange: string;
  last_ts: string | null;
  price: number | null;
  change_pct: number | null;
  main_net_flow: number | null;
  signals: string[];
  news: { title: string; url: string; ts: string }[];
  notices: { title: string; url: string; ts: string; type?: string | null }[];
  lhb: { name?: string; reason?: string; net_buy?: number | null } | null;
};

export type SnapshotResult = { codes: number; inserted: number; post_close: boolean };

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

export const api = {
  listWatchlist: () => request<WatchlistItem[]>("/api/watchlist"),
  importCodes: (raw: string) =>
    request<ImportResult>("/api/watchlist/import", {
      method: "POST",
      body: JSON.stringify({ raw }),
    }),
  deleteCode: (code: string) =>
    request<{ ok: boolean }>(`/api/watchlist/${code}`, { method: "DELETE" }),
  listStocks: () => request<StockRow[]>("/api/stocks"),
  stockDetail: (code: string) => request<StockDetail>(`/api/stocks/${code}`),
  triggerSnapshot: () =>
    request<SnapshotResult>("/api/stocks/snapshot", { method: "POST" }),
};
