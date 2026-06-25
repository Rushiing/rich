# 卖出线 v1 — 实现 plan

> 2026-06-25。遵 `three-line-principle.md`:独立管线、**不碰买入**、解耦引擎/融合表达。
> 方向经 me×codex 三轮攻防 + Rush 红队定稿。

## Context
卖出现在 ≈0、无战绩(原 `actionable=建议卖出` 是买入轴残值,用粗糙 `return_d5<0` 量)。
重做成**独立的「当前状态风险信号」**:纯客观触发、跟用户入场点脱钩、自攒战绩、覆盖
用户全部自选(含 RICH 没推过的存量票)。

**codex 攻防修正的关键**:不是「先造秤」单走 —— 最大杠杆是 **L2 信号 + L4 覆盖 + L1 秤
并行**;信号带「验证中」诚实框当天就发(给警觉价值 + 造真实卖出事件),**L1 当 claim 闸**
(秤证明跑赢 baseline 才解锁自信表述/推送)。仓位语境(盈/平/亏)是决定**动作**的结构输入,
③ 漏斗已提供。

## Scope v1(三块并行)

### S1 — 风险信号引擎(L2 + L4 覆盖)
`sell_risk_signal(code, snapshot, klines, outcome) -> {level, triggers[]}`,纯客观、**跑在全部自选**:
- **资金**:`main_net_flow<0 且 net_flow_3d<0 且 return_d5 < 同日同板块中位 − 3pp`
- **技术**:收盘 `< MA20` 连 3 日;或 破预选池 `invalidation_rule`
- **短线背离**:`nd_trend=up 但 return_d1 < −3% 且未收复 MA20`
- level = 触发条数/严重度;triggers 带人话理由(给 S3 解释,不预测)
字段都现成(Snapshot.main_net_flow/net_flow_3d、Kline→MA20、AnalysisOutcome.nd_trend、PoolEntry.invalidation)。

### S2 — 锚点 + 记分(L1 秤 / claim 闸)
- 信号触发即 anchor 一条 `SellSignalOutcome`(code/triggers/anchor_close/fired_at)。
- 前向记分(复用 outcomes 的 kline bisect):**避免回撤口径** —— hit = 信号后 dN 内
  `hold_return < seg_baseline − 阈值`(继续持有确实跑输→该卖是对的);带去重 + clean 过滤。
- `sell_signal_stats()` 读数 = **claim 闸**:60 天滚动跑赢 baseline 才算「有效」。
- ⚠️ 初期 n 小标样本不足,**不对客**。

### S3 — 诚实呈现(动作按盈亏档,框「验证中」)
- 详情页 + 今日需行动:展示风险状态 + 触发理由(客观),**动作按 ③ 漏斗 盈/平/亏 分档**
  (大赚→护利、平→观察/轻减、大亏→护本、未持有→只提示不催卖)。
- 全程框「**客观风险提示·有效性验证中**」,**不出**「RICH 卖得准」「护利护本」自信表述。

## 不在 v1(L1 闸控,等战绩)
- L3 自信表述(护利/护本作为「有把握」的话术)—— 等 S2 亮 edge。
- L5 主动推送 —— ≈0 时推送烧信任,等 S2 最小战绩。
- 换仓建议(已判砍:不当交易指挥)。

## Critical files
| 文件 | 改动 |
|---|---|
| `backend/app/services/sell_signal.py`(新) | `sell_risk_signal()` 引擎(纯客观,读 snapshot/kline/outcome/pool) |
| `backend/app/models.py` | 新 `SellSignalOutcome`(锚点 + 前向记分列) |
| `backend/app/services/outcomes.py` 或新 `sell_outcomes.py` | 锚点写入 + 避免回撤前向记分 + `sell_signal_stats()` |
| `backend/app/services/cron.py` | 信号 tick:全自选跑 sell_risk_signal、触发即 anchor;outcomes tick 回填记分 |
| `backend/app/main.py` | `/api/_diag/sell-signal-stats`(claim 闸,样本不足标注) |
| `backend/app/services/action_items.py` | 风险状态进「今日需行动」,动作按盈亏档(融合表达) |
| `frontend` 详情页/action-items | 风险 chip + 触发理由 +「验证中」框 |
| `backend/tests/` | 信号触发逻辑 + 避免回撤 hit 单测 |

## Verification
- 单测:三类触发各自命中/不命中;避免回撤 hit(信号后跑输 baseline=对)、clean 过滤。
- 后端 py_compile + import;新表 create_all / 新列登记 db.py。
- 端到端(部署后):全自选跑出风险信号、anchor 进库;打 claim 闸端点出结构(n 小、样本不足);
  详情页见「客观提示·验证中」+ 动作按盈亏档。

## 先做哪块
**S1 信号引擎先行**(L2+L4 是地基,S2 要它产事件、S3 要它产状态)。S1 出来后 S2/S3 并行。
S1 自身**不碰买入分析**,纯新增旁路。
