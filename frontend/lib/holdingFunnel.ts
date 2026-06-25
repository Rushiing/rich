// 持仓决策漏斗状态 —— 详情页/列表页共用。轻量:只存"持有/盈亏/风险偏好"三个
// 点选,**不要成本价**(内测用户嫌录入麻烦)。per-stock localStorage 持久化。
// 默认(Rush 拍板):持有 · 盈 · 激进 —— 因为自选池绝大比例是已持仓票。
//
// ③ 埋点记分(把"已持仓建议含金量高"从肉眼变成验证数)是后端、后续单独做;
// v1 先 localStorage 把交互跑通。

import { api } from "./api";

export type TierKey = "aggressive" | "neutral" | "conservative";
export type PnlBucket = "盈" | "平" | "亏";
export type FunnelState = { held: boolean; pnl: PnlBucket; tier: TierKey };

const DEFAULT: FunnelState = { held: true, pnl: "盈", tier: "aggressive" };
const PNLS: PnlBucket[] = ["盈", "平", "亏"];
const TIERS: TierKey[] = ["aggressive", "neutral", "conservative"];
const keyOf = (code: string) => `rich:funnel:${code}`;

export function getFunnelState(code: string): FunnelState {
  if (typeof window === "undefined") return { ...DEFAULT };
  try {
    const raw = window.localStorage.getItem(keyOf(code));
    if (!raw) return { ...DEFAULT };
    const p = JSON.parse(raw) as Partial<FunnelState>;
    return {
      held: typeof p.held === "boolean" ? p.held : DEFAULT.held,
      pnl: PNLS.includes(p.pnl as PnlBucket) ? (p.pnl as PnlBucket) : DEFAULT.pnl,
      tier: TIERS.includes(p.tier as TierKey) ? (p.tier as TierKey) : DEFAULT.tier,
    };
  } catch {
    return { ...DEFAULT };
  }
}

export function setFunnelState(code: string, partial: Partial<FunnelState>): FunnelState {
  const next = { ...getFunnelState(code), ...partial };
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(keyOf(code), JSON.stringify(next));
    } catch {
      /* 隐私模式/配额满 — 静默,本就是体验增强 */
    }
  }
  return next;
}

// 已录成本价的票:用 P&L% 预填盈亏档(big_gain ≥10% / big_loss ≤-10% / 中间)。
export function pnlBucketFromPct(pct: number): PnlBucket {
  if (pct >= 10) return "盈";
  if (pct <= -10) return "亏";
  return "平";
}

// 漏斗(持仓 + 盈亏)→ key_table.scenario_advice 的某一条。
// 盈→大幅浮盈 / 平→小幅 / 亏→大幅浮亏(对应 scenario_advice 的三个持仓档)。
export type ScenarioKey =
  | "not_holding" | "holding_big_gain" | "holding_small" | "holding_big_loss";

export function scenarioKeyFor(held: boolean, pnl: PnlBucket): ScenarioKey {
  if (!held) return "not_holding";
  if (pnl === "盈") return "holding_big_gain";
  if (pnl === "亏") return "holding_big_loss";
  return "holding_small";
}

// ③ 服务端埋点:把当前 localStorage 漏斗态 fire-and-forget 上报。同一 code
// 短时去抖(连点只报最后一次),避免刷接口。失败静默(api.logFunnelChoice
// 内部已吞)。详情页/列表页两个点选点共用 —— 点完写 localStorage 后调它。
const _reportTimers: Record<string, ReturnType<typeof setTimeout>> = {};
export function reportFunnelChoice(code: string): void {
  if (typeof window === "undefined") return;
  clearTimeout(_reportTimers[code]);
  _reportTimers[code] = setTimeout(() => {
    const s = getFunnelState(code);
    api.logFunnelChoice(code, {
      held: s.held,
      pnl: s.held ? s.pnl : null,
      tier: s.tier,
    });
  }, 800);
}
