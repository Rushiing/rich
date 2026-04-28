# 早安 ☀️ — 2026-04-29

> 凌晨修了一晚上 snapshot 抓取卡住的问题。这份是简明交接。
> 验证完没问题就 `git rm` 这个文件。

## TL;DR

snapshot job 自 4/28 09:50 起 24 小时没写任何新数据。**已找到根因并修好**：连续四个 commit 渐进式收敛到一个解。开盘前你只需要做一件事 — 看 [验证清单](#验证清单) 走一遍。

## 这一晚的诊断流水

| 步骤 | 假设 | 验证 | 结论 |
|---|---|---|---|
| 1 | 列 `pe_ratio` 等没建出来导致 commit 炸 | 加 `/api/_diag/snapshot-schema` 端点 | 列全在，假设错 |
| 2 | 容器在 commit 前被 SIGTERM | 截图 `Stopping Container` | ✅ 这是症状之一 |
| 3 | per-row commit 解决 | 推 `03e7ecf` | 不够 — `collect_many` 自己就 5+ 分钟，commit 循环还没开始就被打 |
| 4 | per-worker commit | 推 `b21670e` | 仍然 0/49 — DB 连接池被占满了 |
| 5 | DB session 解耦 + 连接池扩容 | 推 `0f52326` | ✅ 应该是最终修法 |

附带修了两个：
- **akshare 30s 超时上限 → 12s**（`900cb25`）：单条死路由不再卡住一个 worker 半分钟
- **前端轮询 5min → 15min + 文案重写**（`900cb25`）：不会再误报 "已停止刷新" 而后端其实还在跑

## 验证清单

直接打开 [盯盘页](https://pure-emotion-production-6722.up.railway.app/stocks)：

1. 看右上角"全部 (49)"，记下当前 last_ts
2. 点 **手动抓取**
3. **预期**：5–15s 内开始有股票更新（一行一行涌出来），60–120s 内 49 支全部刷到当前时间，最坏 3min
4. 不要担心进度条／日志的 `[err]`：99% 是 akshare tqdm（已抑制但仍可能漏一些）和被 `_safe()` 兜住的 transient WARNING

如果上面没问题，**当前的所有问题都修好了**，可以直接进入开盘流程。

## 如果 9:30 仍然没自动刷新

最可能：Railway 容器睡眠（hobby/free tier 在闲置 30min 后会 sleep），in-process scheduler 跟着不动。两步排查：

1. `curl https://rich-production-afb6.up.railway.app/health` 唤醒它
2. 立刻点一次"手动抓取"
3. 之后 5min/30min 内应该又能自动跑

如果想根治这个：把 snapshot/quotes 改成 Railway 自带的 cron jobs 调用 HTTP 端点（`/api/stocks/snapshot`），而不是 in-process scheduler。半天工作量，**今天先不做**。

## 关于 "全部重新解析后刷新页面会不会丢请求"

**不丢**。后端是 daemon thread 守护任务，独立于浏览器；前端 mount 时会自动检查 `/api/stocks/analysis/batch/status`，发现还在跑就接上轮询（[stocks/page.tsx:79](frontend/app/stocks/page.tsx:79)）。每支股票完成立刻 `db.commit()`，已完成的都是落库的。容器重启会丢正在跑的那一支，重新点会自动 `only_missing=true` 跳过已完成。

## 工具

- `/api/_diag/snapshot-schema` — 看 Postgres 列表是否齐全
- `/api/stocks/snapshot/status` — 看是否在跑
- `/health` — 唤醒 + 健康检查

晚安效果如何，早起见分晓。我把验证脚本也跑了，结果在 git log 最新一次 push 后的对话里能看到。
