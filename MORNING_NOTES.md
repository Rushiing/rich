# 早安 ☀️

> 2026-04-26 夜里跑完了 Phase 2/3/4。这份笔记列：哪些代码上线了、你早上要先做什么、哪些没法夜里验所以等你。
> **验完之后这个文件可以删掉**（`git rm MORNING_NOTES.md`）。

---

## TL;DR

- 4 个 commit 已推 `main`，Railway 应该已经全部部署到位（最后一个 commit 是 Phase 4）。
- **唯一你必须做的事**：在 Railway → backend → Variables 加两个变量并重部署：
  ```
  ANTHROPIC_API_KEY  = <你的 zenmux key>
  ANTHROPIC_BASE_URL = https://zenmux.ai/api/anthropic
  ```
  加完点一下 backend 的 "Redeploy"（或它会自动），等它重启完后才能用 Phase 3。
- 其他全部应该已经能用，按下面的清单走一遍 30 秒能确认。

## 推上去的 commits（最旧 → 最新）

| commit | 内容 |
|---|---|
| `e5a76da` | Phase 0 骨架 |
| `d8b07b9` | Phase 1 自选池 |
| `cd1c3cb` | Bump next 修 CVE |
| `5b32b71` | Fix manifest middleware 漏 |
| `4dfb88c` | **Phase 2** 抓取 + 信号 + 盯盘 |
| `9f41eeb` | **Phase 3** Claude 深度解析 |
| `a9ed0e4` | Phase 3 base URL 配置（你的 zenmux） |
| `<最新>`   | **Phase 4** 移动端 + PWA |

## 你早上 5 分钟内可以走完的验证清单

打开 https://pure-emotion-production-6722.up.railway.app

### Phase 2（不需要 ANTHROPIC_API_KEY）
1. 登录后到 `/stocks`，应该看到自选池里那 3 支股票（贵州茅台等），但 last_ts 是空的
2. 右上角点 **"手动抓取"** → 等 5–15 秒 → 应该看到价、涨跌、信号都有了
3. 强信号（涨停/跌停/重要公告/上龙虎榜）的行会有淡红底色 + 红色信号 chip
4. 表头按 信号强度 + 涨跌幅排序

> ⚠️ 抓取可能慢（akshare 同时打多个 endpoint）。如果 30 秒内没返回，刷新看看，或在 Railway backend logs 找 `snapshot job` 关键字。

### Phase 3（需要先在 Railway 设好 API key）
1. 在 `/stocks` 任意一行点 **"解析 →"** 进入 `/stocks/<code>`
2. 第一次点应该是空状态，按 **"生成深度解析"** → 5–15 秒后出来：
   - 顶部一张关键表（建议买入 / 区间 / 仓位 / 持有时间 / 止损 / 置信度）
   - 下面 4 段 markdown（基本面 / 技术面 / 消息面 / 风险点）
3. 关闭重新打开同一支 → 应该走缓存，秒出（freshness bar 显示生成时间，4 小时内是 fresh）
4. 点 **"重新生成"** → 强制刷一次

### Phase 4（手机或浏览器 mobile mode）
1. 浏览器 DevTools 切到 mobile 视图（375×667）
2. `/stocks` 表格应该可以横向滑动，不会撑破布局
3. 在手机 Safari 打开 → 分享 → "添加到主屏幕" → 应该用 SVG 图标
4. Android Chrome 同理（"Install app" 提示）

## 没法在我这边验的事（需要你看一下）

| 项 | 为什么没验 | 你怎么验 |
|---|---|---|
| akshare 真实抓取 | 你 Mac 上 Clash 代理屏蔽 eastmoney 域名，本机调不通 | Railway 上跑就行，看 logs 或 UI |
| Claude API 真实调用 | 你说 key 自己设 | 设完 key 后按上面 Phase 3 步骤 |
| zenmux base URL 是否兼容 Anthropic SDK | 没 key 没法测 | 同上。如果 SDK 抱怨格式，告诉我具体报错 |
| APScheduler 真实在 9:30/10:30/… 触发 | 测不动时间 | 等下一个交易日早上看 logs，应该有 6 次 snapshot job 记录 |

## 一个潜在的坑：Railway Postgres 现在多了 2 张新表

Phase 2 加了 `snapshots` 表，Phase 3 加了 `analyses` 表。FastAPI lifespan 里 `Base.metadata.create_all` 会自动建。**理论上 Railway 重部署后表就有了**。如果某个接口报 "no such table" / "relation does not exist"，去 backend logs 看启动时 `create_all` 有没有报错；最差情况手动跑一次：

```bash
# 在 Railway shell 里 (或本地接 prod DB)
python -c "from app.db import Base, engine; from app.models import *; Base.metadata.create_all(engine)"
```

## 想做什么后续 / 改什么

`CLAUDE.md` 末尾列了几个 future-work 候选（更多信号、snapshot 时间轴、第三层解析、北向数据、retention 等）。这些都不是 MVP 必须的。

如果 5 分钟验证下来都 OK，**这份文件可以删了**：

```bash
git rm MORNING_NOTES.md && git commit -m "chore: remove morning notes after verification" && git push
```

晚安效果如何，早起见分晓。
