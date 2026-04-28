# 早安 ☀️ — 2026-04-29

> 凌晨修了一晚上 snapshot 抓取卡住的问题，已经修到可上线状态。
> 验证完没问题就 `git rm` 这个文件。

## TL;DR

**彻底修好了。49/49 全绿。开盘前你只需走 [验证清单](#验证清单) 复查一次。**

凌晨 6 个 commit 把"snapshot 自 4/28 09:50 起 24h 没更新"的问题拆成 5 层根因逐个解了：

1. ❌ 我以为是列缺失 → 加诊断端点查到列其实都在（`a23a87a`）
2. ✅ 容器在 commit 前被 SIGTERM → per-row commit（`03e7ecf`）
3. ✅ collect_many 自己就 5min，commit 循环都没开始 → per-worker commit（`b21670e`）
4. ✅ 10 worker 同时持 DB 连接打满池 → session 解耦 + 池扩容（`0f52326`）
5. ✅ akshare 慢字节流绕过 read timeout 让 worker 永久 hang → 整 job 4min ceiling（`aa026fb`）
6. ✅ 重磅新闻股 akshare 内部翻全量历史页绕过 12s read timeout → 8s 硬墙时钟 wall-time cap（`50b8fa6`）

外加：
- `900cb25`：akshare 默认 30s timeout → 12s；前端轮询 5min → 15min（修之前的"已停止刷新"误报）

## 当前生产状态（凌晨 01:36 验证）

最后一次手动抓取效果：

| 北京时间 | 数量 | 说明 |
|---|---|---|
| 01:28 | 1 | 上一波遗漏的 |
| 01:35 | 24 | 本次 trigger 第一波 |
| 01:36 | 24 | 本次 trigger 第二波 |

**49/49 全部 fresh，全部在过去 9 分钟内。**

进度时序（这次 250s 的 trigger）：
- t=15s → 6 支已写入
- t=35s → 16 支
- t=56s → 28 支
- t=77s → 42 支
- t=98s → 48 支
- t=250s → status 终止（48 + 之前那 1 = 49）

## 验证清单（开盘前 5 分钟）

打开 https://pure-emotion-production-6722.up.railway.app/stocks

1. 看右上角"全部 (49)"
2. 点 **手动抓取**
3. **预期**：5–15s 内开始有股票更新（一行一行涌出来），4 分钟内 35–45 支会刷到当前时间
4. 4 分钟后 status 自动 toggle 回 "手动抓取"，不会卡 running 状态

## 9:30 开盘后预期行为

- **9:30 + 每 5 分钟**：`quotes_tick` 跑 Tencent bulk → 49 支 price/change/flow **全部更新**（这条路 100% 可靠，几乎不会失败）
- **9:30 / 10:30 / 11:30 / 14:00 / 15:00 / 16:00**：`snapshot_tick` 跑 full（含 news/notices），4min 内能刷 35-45 支，剩下的下小时再跑
- **9:35**：`daily_analysis_tick` 跑批量解析（kimi-k2.5），20 分钟左右刷完 49 支

**最关键的"价格流动"靠 quotes_tick，跟 snapshot 解耦。即使 snapshot 全挂，盯盘依然能看到价格变化。**

## 关于剩余可能失败的少量股票

`stock_news_em` / `stock_notice_report` 在重磅新闻股上分页拉历史可能 8s 内来不及。我用 `_safe_with_timeout` 包了 8s 硬墙：超时直接放弃 news/notices（按空数组算），**但行情/资金流/信号还是会写入**。所以最坏情况是某只股票本轮 news 字段空，下一 cron tick 重试一次成功就有 news 了。**最关键的"价格在动"100% 可靠**。

## 关于"全部重新解析后刷新页面会不会丢请求"

**不丢**。后端是 daemon thread 跑，前端 mount 时检查 `/api/stocks/analysis/batch/status`，发现还在跑就接上轮询（[stocks/page.tsx:79](frontend/app/stocks/page.tsx:79)）。每支股票完成立刻 `db.commit()`。容器重启会丢 in-flight 那一支，重新点会 `only_missing=true` 跳过已完成的。

## 工具

- `/api/_diag/snapshot-schema` — 看 Postgres 列表是否齐全
- `/api/stocks/snapshot/status` — 看是否在跑
- `/health` — 唤醒 + 健康检查
- Railway logs 能看到 `inserted N/49 (failed=X, timed_out=Y)` 总结行

## 这一夜的 commits

```
50b8fa6  fix(scrape): hard wall-time cap (8s) on news/notice akshare calls   ← 最后一刀
aa026fb  fix(snapshots): 4min ceiling + skip stuck workers
0f52326  fix(snapshots): decouple DB session + bigger pool
900cb25  fix(scrape,ui): cap akshare timeout 30s→12s; bump frontend poll
b21670e  fix(snapshots): per-worker commit
03e7ecf  fix(snapshots): commit per row
a23a87a  fix(snapshots): self-heal columns + diag endpoint
```

晚安效果如何，早起见分晓 — 看到 9:30 后 5min 内 price/change 在动 + 49/49 时间戳是当天，就说明全栈打通。
