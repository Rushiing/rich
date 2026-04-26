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
};
