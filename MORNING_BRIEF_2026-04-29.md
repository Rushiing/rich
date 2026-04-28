# 早安 ☀️ — 2026-04-29

> 凌晨修了一晚上 snapshot 抓取卡住的问题，已经修到可上线状态。
> 验证完没问题就 `git rm` 这个文件。

## TL;DR

**修好了。开盘前你只需走 [验证清单](#验证清单)。**

凌晨 5 个 commit 把"snapshot 自 4/28 09:50 起 24h 没更新"的问题拆成 4 层根因逐个解了：

1. ❌ 我以为是列缺失 → 加诊断端点查到列其实都在（`a23a87a`）
2. ✅ 容器在 commit 前被 SIGTERM → per-row commit（`03e7ecf`）
3. ✅ collect_many 自己就 5min，commit 循环都没开始 → per-worker commit（`b21670e`）
4. ✅ 10 worker 同时持 DB 连接打满池 → session 解耦 + 池扩容（`0f52326`）
5. ✅ akshare 慢字节流绕过 read timeout 让 worker 永久 hang → 整 job 4min ceiling（`aa026fb`）

外加：
- `900cb25`：akshare 默认 30s timeout → 12s；前端轮询 5min → 15min（之前的"已停止刷新"误报）

## 当前生产状态

凌晨 01:13–01:28 跑了几次手动 snapshot 验证，结果：

| 状态 | 数量 | 说明 |
|---|---|---|
| 已刷新 (4/29 凌晨) | 40 | 包括 Tencent 行情 + akshare news/notices |
| 卡住 (4/28 09:50) | 7 | 重磅新闻股（润泽/香农/锐科/中钨/岩山/洛阳/金卡），akshare 拉全量新闻太慢 |
| null (从未抓过) | 2 | 同有科技 / 天承科技，新加入或刚 import |

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

## 剩余 7 支为什么卡住

`stock_news_em(symbol=...)` 在重磅新闻股上分页拉历史，akshare 内部一页一页 streaming，每一小段都在 12s 读取 timeout 之内但累计能跑几分钟。我加的 4min job ceiling 让他们超时被 abandoned，但下次 snapshot 又重蹈覆辙。

**根治方案**（不在今晚 scope）：
- 把 _news / _notices 改成"只拉第一页 + 最近 24h 过滤"
- 或换成 eastmoney 的 RESTful 端点（不用 akshare 包装）
- 或干脆把 news/notices 做成独立的低频 job（每天一次）

今天先这样上线，验证 quotes 5min 正常即可。

## 关于"全部重新解析后刷新页面会不会丢请求"

**不丢**。后端是 daemon thread 跑，前端 mount 时检查 `/api/stocks/analysis/batch/status`，发现还在跑就接上轮询（[stocks/page.tsx:79](frontend/app/stocks/page.tsx:79)）。每支股票完成立刻 `db.commit()`。容器重启会丢 in-flight 那一支，重新点会 `only_missing=true` 跳过已完成的。

## 工具

- `/api/_diag/snapshot-schema` — 看 Postgres 列表是否齐全
- `/api/stocks/snapshot/status` — 看是否在跑
- `/health` — 唤醒 + 健康检查
- Railway logs 能看到 `inserted N/49 (failed=X, timed_out=Y)` 总结行

## 这一夜的 commits

```
aa026fb  fix(snapshots): 4min ceiling + skip stuck workers
0f52326  fix(snapshots): decouple DB session + bigger pool
900cb25  fix(scrape,ui): cap akshare timeout 30s→12s; bump frontend poll
b21670e  fix(snapshots): per-worker commit
03e7ecf  fix(snapshots): commit per row
a23a87a  fix(snapshots): self-heal columns + diag endpoint
```

晚安效果如何，早起见分晓 — 看到 9:30 后 5min 内 price/change 在动，就说明全栈打通。
