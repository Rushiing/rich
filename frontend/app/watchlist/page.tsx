"use client";

import { useEffect, useRef, useState } from "react";
import readXlsxFile from "read-excel-file";
import { api, WatchlistItem, ImportResult } from "../../lib/api";
import UserChip from "../_components/UserChip";

const exchangeLabel: Record<string, string> = {
  sh: "上交",
  sz: "深交",
  bj: "北交",
  unknown: "?",
};

export default function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [showImport, setShowImport] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      setItems(await api.listWatchlist());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function onDelete(code: string) {
    if (!confirm(`确认删除 ${code}？`)) return;
    await api.deleteCode(code);
    setItems((prev) => prev.filter((i) => i.code !== code));
  }

  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>自选池</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <UserChip />
          <a href="/stocks" style={linkStyle}>盯盘</a>
          <button onClick={() => setShowImport(true)} style={primaryBtn}>导入</button>
        </div>
      </header>

      <div style={{ marginTop: 16, color: "#888", fontSize: 13 }}>
        共 {items.length} 支
      </div>

      <div className="table-scroll">
      <table style={tableStyle}>
        <thead>
          <tr style={{ color: "#888", fontSize: 12 }}>
            <th style={th}>代码</th>
            <th style={th}>名称</th>
            <th style={th}>市场</th>
            <th style={th}>添加时间</th>
            <th style={{ ...th, textAlign: "right" }}>操作</th>
          </tr>
        </thead>
        <tbody>
          {loading && (
            <tr>
              <td colSpan={5} style={{ ...td, textAlign: "center", color: "#666" }}>加载中...</td>
            </tr>
          )}
          {!loading && items.length === 0 && (
            <tr>
              <td colSpan={5} style={{ ...td, textAlign: "center", color: "#666" }}>
                自选池为空，点击右上角"导入"添加股票
              </td>
            </tr>
          )}
          {items.map((item) => (
            <tr key={item.code}>
              <td style={{ ...td, fontFamily: "monospace" }}>{item.code}</td>
              <td style={td}>{item.name}</td>
              <td style={td}>{exchangeLabel[item.exchange] || item.exchange}</td>
              <td style={{ ...td, color: "#888", fontSize: 12 }}>
                {item.added_at ? new Date(item.added_at).toLocaleString("zh-CN") : "-"}
              </td>
              <td style={{ ...td, textAlign: "right" }}>
                <button onClick={() => onDelete(item.code)} style={dangerBtn}>删除</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>

      {showImport && (
        <ImportDialog
          onClose={() => setShowImport(false)}
          onDone={async () => {
            setShowImport(false);
            await refresh();
          }}
        />
      )}
    </main>
  );
}

function ImportDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    try {
      if (file.name.toLowerCase().endsWith(".csv")) {
        const csv = await file.text();
        const codes = csv
          .split(/\r?\n/)
          .map((line) => line.split(/[,;\t]/)[0]?.trim() || "")
          .filter(Boolean)
          .join("\n");
        setText((prev) => (prev ? prev + "\n" + codes : codes));
      } else {
        const rows = await readXlsxFile(file);
        const codes = rows
          .map((r) => (r[0] == null ? "" : String(r[0]).trim()))
          .filter(Boolean)
          .join("\n");
        setText((prev) => (prev ? prev + "\n" + codes : codes));
      }
    } catch (err) {
      setError(`文件解析失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onSubmit(retryText?: string) {
    setBusy(true);
    setError(null);
    try {
      const payload = retryText ?? text;
      const r = await api.importCodes(payload);
      // When retrying, merge into the existing result so the user keeps
      // seeing what already succeeded.
      setResult((prev) =>
        prev
          ? {
              added: [...prev.added, ...r.added],
              skipped_existing: prev.skipped_existing,
              invalid_format: r.invalid_format,
              lookup_failed: r.lookup_failed,
            }
          : r,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={modalBackdrop} onClick={onClose}>
      <div style={modalBox} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 16, margin: 0 }}>导入股票代码</h2>
        <p style={{ color: "#888", fontSize: 12, margin: 0 }}>
          粘贴一列 6 位代码（一行一个或用空格/逗号分隔），或上传 Excel/CSV（识别第一列）。
          系统会通过 akshare 校验代码并查询股票名称。
        </p>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"600519\n000001\n300750"}
          rows={8}
          style={textareaStyle}
        />

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            ref={fileRef}
            type="file"
            accept=".xlsx,.csv"
            onChange={onPickFile}
            style={{ fontSize: 12, color: "#aaa" }}
          />
        </div>

        {error && <div style={{ color: "#ff6b6b", fontSize: 13 }}>{error}</div>}

        {result && (
          <div style={resultBox}>
            <div>新增 <b style={{ color: "#4ade80" }}>{result.added.length}</b></div>
            <div>已存在 <b style={{ color: "#facc15" }}>{result.skipped_existing.length}</b>{result.skipped_existing.length > 0 ? `: ${result.skipped_existing.join(", ")}` : ""}</div>
            {result.invalid_format.length > 0 && (
              <div>格式无效 <b style={{ color: "#ff6b6b" }}>{result.invalid_format.length}</b>: {result.invalid_format.join(", ")}</div>
            )}
            {result.lookup_failed.length > 0 && (
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <span>查询失败 <b style={{ color: "#facc15" }}>{result.lookup_failed.length}</b>: {result.lookup_failed.join(", ")}</span>
                <button
                  onClick={() => onSubmit(result.lookup_failed.join("\n"))}
                  disabled={busy}
                  style={ghostBtn}
                  title="akshare 偶发失败，再试一次通常就好"
                >
                  {busy ? "重试中..." : "重试这几支"}
                </button>
              </div>
            )}
          </div>
        )}

        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button onClick={result ? onDone : onClose} style={ghostBtn}>
            {result ? "完成" : "取消"}
          </button>
          {!result && (
            <button onClick={() => onSubmit()} disabled={busy || !text.trim()} style={primaryBtn}>
              {busy ? "校验中..." : "导入"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

const linkStyle: React.CSSProperties = {
  color: "#9ca3af",
  fontSize: 13,
  textDecoration: "none",
  padding: "6px 10px",
};
const primaryBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "#3b82f6",
  color: "white",
  border: "none",
  borderRadius: 6,
  fontSize: 13,
  cursor: "pointer",
};
const ghostBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "transparent",
  color: "#aaa",
  border: "1px solid #333",
  borderRadius: 6,
  fontSize: 13,
  cursor: "pointer",
};
const dangerBtn: React.CSSProperties = {
  padding: "4px 8px",
  background: "transparent",
  color: "#ff6b6b",
  border: "1px solid #4b1d1d",
  borderRadius: 4,
  fontSize: 12,
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
  borderBottom: "1px solid #222",
  fontWeight: 500,
};
const td: React.CSSProperties = {
  padding: "10px",
  borderBottom: "1px solid #1a1a1a",
  fontSize: 14,
};
const modalBackdrop: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.6)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 100,
  padding: 16,
};
const modalBox: React.CSSProperties = {
  background: "#141414",
  border: "1px solid #2a2a2a",
  borderRadius: 8,
  padding: 20,
  width: "100%",
  maxWidth: 480,
  display: "flex",
  flexDirection: "column",
  gap: 12,
};
const textareaStyle: React.CSSProperties = {
  background: "#0a0a0a",
  border: "1px solid #2a2a2a",
  borderRadius: 6,
  color: "#e5e5e5",
  fontFamily: "monospace",
  fontSize: 13,
  padding: 10,
  resize: "vertical",
};
const resultBox: React.CSSProperties = {
  background: "#0a0a0a",
  border: "1px solid #2a2a2a",
  borderRadius: 6,
  padding: 10,
  fontSize: 13,
  display: "flex",
  flexDirection: "column",
  gap: 4,
};
