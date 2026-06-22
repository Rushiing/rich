"use client";

/**
 * SegmentSection —— 分区视觉规范的唯一载体(锁定版,Rush 6/22)。
 *
 * 这是「把规范锁进代码」的地方:任何页面要按某个维度(市场 / 板块 / 专题…)
 * 把列表摘出分区,都用这里的 SegmentHeader,不要各页自己画。后人加维度只能
 * 往 lib/market.ts 的 SEGMENT_META 里填,画不歪。
 *
 * 规范三件套(改动前先读):
 *  1. 区头 = 图标 + 名称 + 计数,样式固定。
 *  2. 颜色锁死单色:被摘出强调的段用 teal(SEGMENT_EMPHASIS_COLOR)区头 +
 *     左条;中性段素色无强调。
 *  3. 一次只强调一个维度:同一视图里 emphasis 段应唯一。市场和买卖不能同时
 *     都用彩色强调 —— 谁是强调维度,另一维度就退成中性次序(见盯盘:市场为
 *     外层强调,买卖退内层 neutral)。
 *
 * 复杂留在逻辑里,交互克制到单维度 —— 这是产品准则,不是临时取舍。
 */

import type { CSSProperties, ReactNode } from "react";
import { SEGMENT_META, SEGMENT_EMPHASIS_COLOR, type Board } from "../../lib/market";

const EMPHASIS_BG = "rgba(16,185,129,0.14)"; // teal 区头底,匹配 designated 通道色

// 被摘出强调的段:内容容器套左条(table 行则把这个 borderLeft 加到首个 td)。
export function segmentAccentStyle(board: Board): CSSProperties {
  return SEGMENT_META[board].emphasis
    ? { borderLeft: `3px solid ${SEGMENT_EMPHASIS_COLOR}`, borderRadius: 0, paddingLeft: 8 }
    : {};
}

export function SegmentHeader({
  board,
  count,
  as = "div",
  colSpan,
  hint,
}: {
  board: Board;
  count: number;
  as?: "div" | "row";
  colSpan?: number; // as="row" 时必填
  hint?: ReactNode; // 可选右侧说明(如占位预热文案)
}) {
  const meta = SEGMENT_META[board];
  const inner = meta.emphasis ? (
    <span style={{
      fontSize: 12, color: SEGMENT_EMPHASIS_COLOR, background: EMPHASIS_BG,
      padding: "3px 10px", borderRadius: 4, display: "inline-flex", alignItems: "center", gap: 5,
    }}>
      {meta.icon && <i className={`ti ${meta.icon}`} style={{ fontSize: 13 }} aria-hidden="true" />}
      {board} · {count}
    </span>
  ) : (
    <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{board} · {count}</span>
  );

  const body = (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 10 }}>
      {inner}
      {hint && <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{hint}</span>}
    </span>
  );

  if (as === "row") {
    return (
      <tr>
        <td colSpan={colSpan} style={{
          padding: "8px 4px 4px",
          borderBottom: "1px solid var(--border-soft)",
        }}>
          {body}
        </td>
      </tr>
    );
  }
  return <div style={{ padding: "4px 2px", marginBottom: 6, borderBottom: meta.emphasis ? undefined : "1px solid var(--border-soft)" }}>{body}</div>;
}
