// 板块(market board)派生 + 分段元数据 —— 镜像后端 stocks.market_board。
// market 不存任何字段,纯按 code 前缀实时派生,跟后端口径必须一致。
//
// 视觉规范(锁定):分段一次只强调一个维度、强调色锁死单色(teal)。
// 当前唯一被「摘出强调」的板块是科创板;其余为中性。详见 SegmentSection。

export type Board = "主板" | "科创板" | "创业板" | "北交所" | "未知";

export function board(code: string): Board {
  if (code.startsWith("688") || code.startsWith("689")) return "科创板";
  if (code.startsWith("30")) return "创业板";
  if (code.startsWith("60") || code.startsWith("00")) return "主板";
  if (/^[849]/.test(code)) return "北交所";
  return "未知";
}

export type SegmentMeta = {
  // emphasis 段用 teal 区头 + 左条摘出;非 emphasis 段中性素色。
  emphasis: boolean;
  // 区头图标(Tabler 名,前端用 <i class="ti ti-..">,这里只存名字)。
  icon: string;
};

// 强调色锁死单色 —— teal #6ee7b7,复用现有 designated 通道色,科创视觉语言统一。
export const SEGMENT_EMPHASIS_COLOR = "#6ee7b7";

// 阶段1:只有科创板被摘出强调。以后要拆别的维度(创业板/专题),
// 把对应段的 emphasis 设 true 即可 —— 但规范要求「一次只强调一个维度」,
// 同一视图里 emphasis 段应保持唯一。
export const SEGMENT_META: Record<Board, SegmentMeta> = {
  科创板: { emphasis: true, icon: "ti-rocket" },
  主板: { emphasis: false, icon: "" },
  创业板: { emphasis: false, icon: "" },
  北交所: { emphasis: false, icon: "" },
  未知: { emphasis: false, icon: "" },
};

// 段顺序:中性段在前,被摘出强调的段排到后面(视觉上「摘出来」放一块)。
const BOARD_ORDER: Board[] = ["主板", "创业板", "北交所", "未知", "科创板"];

export type Segment<T> = { board: Board; meta: SegmentMeta; items: T[] };

// 把一组带 code 的对象按 board 分段,返回非空段(按 BOARD_ORDER 排序)。
export function groupByBoard<T>(items: T[], getCode: (x: T) => string): Segment<T>[] {
  const buckets = new Map<Board, T[]>();
  for (const it of items) {
    const b = board(getCode(it));
    (buckets.get(b) ?? buckets.set(b, []).get(b)!).push(it);
  }
  return BOARD_ORDER
    .filter((b) => buckets.has(b))
    .map((b) => ({ board: b, meta: SEGMENT_META[b], items: buckets.get(b)! }));
}
