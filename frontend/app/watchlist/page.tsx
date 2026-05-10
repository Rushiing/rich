"use client";

import { useEffect, useRef, useState } from "react";
import readXlsxFile from "read-excel-file";
import { api, WatchlistItem, ImportResult } from "../../lib/api";
import UserChip from "../_components/UserChip";
import ThemeToggle from "../_components/ThemeToggle";

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
  // Multi-select state for bulk delete. Set, not array, so the
  // header-checkbox "select all" toggle is O(1) per row check.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      setItems(await api.listWatchlist());
      setSelected(new Set());  // any refresh resets selection — codes may be stale
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
    setSelected((prev) => {
      if (!prev.has(code)) return prev;
      const next = new Set(prev);
      next.delete(code);
      return next;
    });
  }

  function toggleOne(code: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  function toggleAll() {
    setSelected((prev) =>
      prev.size === items.length ? new Set() : new Set(items.map((i) => i.code)),
    );
  }

  async function onBulkDelete() {
    if (selected.size === 0) return;
    if (!confirm(`确认删除选中的 ${selected.size} 支？此操作无法撤销`)) return;
    setBulkBusy(true);
    try {
      const codes = Array.from(selected);
      await api.bulkDelete(codes.join("\n"));
      setItems((prev) => prev.filter((i) => !selected.has(i.code)));
      setSelected(new Set());
    } catch (err) {
      alert(`批量删除失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBulkBusy(false);
    }
  }

  const allChecked = items.length > 0 && selected.size === items.length;
  const someChecked = selected.size > 0 && selected.size < items.length;

  return (
    <main style={{ padding: 20, maxWidth: 880, margin: "0 auto" }}>
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 18, margin: 0 }}>自选池管理</h1>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <ThemeToggle />
          <UserChip />
          <a href="/stocks" style={linkStyle}>盯盘</a>
          <a href="/changelog" style={linkStyle}>更新日志</a>
          <button onClick={() => setShowImport(true)} style={primaryBtn}>导入</button>
        </div>
      </header>

      {/* Toolbar row: count on left, bulk-delete affordance on right when
          rows are selected. Hidden when nothing's checked so the empty
          state doesn't carry a phantom button. */}
      <div style={{
        marginTop: 16,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        color: "var(--text-muted)",
        fontSize: 13,
      }}>
        <span>共 {items.length} 支{selected.size > 0 && ` · 已选 ${selected.size} 支`}</span>
        {selected.size > 0 && (
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              style={{
                background: "transparent",
                border: "none",
                color: "var(--text-soft)",
                fontSize: 13,
                cursor: "pointer",
                padding: 0,
              }}
            >
              取消选择
            </button>
            <button
              type="button"
              onClick={onBulkDelete}
              disabled={bulkBusy}
              style={{
                ...primaryBtn,
                background: bulkBusy ? "var(--text-dim)" : "#dc2626",
              }}
            >
              {bulkBusy ? "删除中…" : `删除选中 (${selected.size})`}
            </button>
          </div>
        )}
      </div>

      <div className="table-scroll">
      <table style={tableStyle}>
        <thead>
          <tr style={{ color: "var(--text-muted)", fontSize: 12 }}>
            <th style={{ ...th, width: 32, padding: "8px 6px" }}>
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => {
                  // indeterminate is a JS-only flag — set it manually to mirror
                  // the partial-selection state.
                  if (el) el.indeterminate = someChecked;
                }}
                onChange={toggleAll}
                aria-label={allChecked ? "取消全选" : "全选"}
                disabled={items.length === 0}
              />
            </th>
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
              <td colSpan={6} style={{ ...td, textAlign: "center", color: "var(--text-faint)" }}>加载中...</td>
            </tr>
          )}
          {!loading && items.length === 0 && (
            <tr>
              <td colSpan={6} style={{ ...td, textAlign: "center", color: "var(--text-faint)" }}>
                自选池为空，点击右上角"导入"添加股票
              </td>
            </tr>
          )}
          {items.map((item) => (
            <tr key={item.code}>
              <td style={{ ...td, padding: "10px 6px" }}>
                <input
                  type="checkbox"
                  checked={selected.has(item.code)}
                  onChange={() => toggleOne(item.code)}
                  aria-label={`选择 ${item.code}`}
                />
              </td>
              <td style={{ ...td, fontFamily: "monospace" }}>{item.code}</td>
              <td style={td}>{item.name}</td>
              <td style={td}>{exchangeLabel[item.exchange] || item.exchange}</td>
              <td style={{ ...td, color: "var(--text-muted)", fontSize: 12 }}>
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
        <p style={{ color: "var(--text-muted)", fontSize: 12, margin: 0 }}>
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
            style={{ fontSize: 12, color: "var(--text-soft)" }}
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
  background: "var(--link)",
  color: "white",
  border: "none",
  borderRadius: 6,
  fontSize: 13,
  cursor: "pointer",
};
const ghostBtn: React.CSSProperties = {
  padding: "6px 12px",
  background: "transparent",
  color: "var(--text-soft)",
  border: "1px solid var(--border-mid)",
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
  borderBottom: "1px solid var(--border-soft)",
  fontWeight: 500,
};
const td: React.CSSProperties = {
  padding: "10px",
  borderBottom: "1px solid var(--border-faint)",
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
  background: "var(--surface)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: 20,
  width: "100%",
  maxWidth: 480,
  display: "flex",
  flexDirection: "column",
  gap: 12,
};
const textareaStyle: React.CSSProperties = {
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  color: "var(--text)",
  fontFamily: "monospace",
  fontSize: 13,
  padding: 10,
  resize: "vertical",
};
const resultBox: React.CSSProperties = {
  background: "var(--bg)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  padding: 10,
  fontSize: 13,
  display: "flex",
  flexDirection: "column",
  gap: 4,
};
