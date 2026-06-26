# RICH 会话交接 — 2026-06-25 ~ 06-26

> 新会话「先读这页」。这是一段高密度会话的状态快照:做了什么、产品现状、架构原则、
> 接下来做什么、以及继续时要知道的坑。所有改动**已 push 已部署**(除非另注)。

## 一、这两天落地了什么(全部已部署)

1. **③ 持仓情境埋点记分**(`feat(③)` 等 commit)
   - 用户级:漏斗选择(持有/盈亏档/风险)上报 `funnel_choices`(append-only),`POST
     /api/funnel/{code}`;**跨设备同步**(`/api/funnel/{code}/latest` + `/mine`,详情页/列表页
     hydrate,服务端为真相源、localStorage 为快缓存)。
   - 分析级:LLM schema 加 `scenario_direction`(看多/看空/中性)→ `AnalysisOutcome.
     scenario_directions` → `scenario_hit_stats()`(平移买卖记分口径)。
   - 读数:`/api/_diag/{funnel-stats,scenario-stats}`,**标样本不足、不对客**,养数中。

2. **卖出线 v1**(`docs/sell-line-v1-plan.md`)
   - S1 `services/sell_signal.py`:当前状态风险信号(资金转流出/破MA20连3日/破失效线/
     短线背离),纯客观 per-stock,新鲜度+active池护栏,跑全自选。
   - S2 `services/sell_outcomes.py`:`SellSignalOutcome` 表 + 避免回撤秤
     (`avg_excess_d5<0` = 触发后跑输同板块 = 有 edge,对称买入 +5pp);市场基线用
     AnalysisOutcome 同日同板块中位(缺基线行排除)。
   - cron `_sell_signal_tick`(16:40 anchor、每日每股去重)+ `_outcomes_tick` 回填。
   - claim 闸 `/api/_diag/sell-signal-stats`;S3 详情页风险卡(动作按盈亏档:护利/护本/
     观察)+ 全程「客观信号·验证中」,**不借买入 +5pp 信用**。
   - **战绩时钟 6/26 周五盘后起步,~3 个月才有结论。**

3. **三线解耦原则**(`docs/three-line-principle.md`,北极星)
   - 买入/卖出/选股是三个独立轴,幼苗期分开各长各的数据/记分/战绩,不跨线借信用。
   - **解耦引擎,融合表达**:后端独立,但用户「我这持仓下一步怎么办」那一刻前台要融合。
   - 卖出 = 纯当前状态风险信号(不引用任何过去时间切片;连续流没有「当时买入逻辑」)。

4. **事故/修复**(同会话内):
   - 火山 ARK 早高峰超时雪崩 → cron 日更改回 **5 并发** + timeout 120→180(曾退化成串行)。
   - diag `_diag_token_guard` 非 ASCII token → `hmac.compare_digest` 改 bytes 比较。
   - **登录 500**:`auth.py` 漏 `import settings`(NameError on set_cookie)。补了
     `tests/test_auth_smoke.py`(register/login 成功路径 + Set-Cookie)。
   - **auth 瘦身**:删 SMS(/sms/*、services/sms.py、ALIYUN_SMS_*)+ 远古单密码
     (legacy-login、check_password、APP_PASSWORD)。**只剩 邀请码 + 手机号 + 密码**。
   - 「缓存已过期 (>4h)」文案诚实化:`AnalysisOutcome→AnalysisOut.stale_reason`,前端
     按 price_move/signal_change/stale 显真实原因(行情大动不再假报 >4h)。
   - changelog 6/25 条目 + 渲染器支持 `**粗体**`。

## 二、产品现状(诚实口径)
- **买入**:历史命中跑赢同期同板块 **+5pp**(复权安全口径,已验证)。这是护城河,独立成长。
- **卖出**:**≈0、无战绩**。v1 刚上,信号在攒数,对客全程「验证中」,不说「卖得准」。
- **③ / 价位记分 / A-B / 科创**:都在养数,样本不足、不对客。

## 三、接下来(backlog)
- **到点 checkpoint**(已挂定时任务,会自动提醒):**6/30** A/B 模型读数(minimax-m3 vs
  kimi-k2.6;注:kimi 吞吐只有 minimax 一半,慢=超时多,选 live 要算进去);**7/3** 科创板
  stage 3。
- **卖出线后续(L1 闸控)**:战绩亮了再开 L3 自信表述(护利/护本作「有把握」)+ L5 主动推送。
- **③ 读数**:等数据攒够出结论。
- **下一条线选择**:按三线原则单线推进(卖出是当前主线;选股待定)。
- **技术债**:`AGENTS.md`/`backend/tools/`(含 `ark_probe.py` 火山探活)两个 untracked,
  你定留删;funnel/sell 每日去重无 DB 唯一约束(单实例够用);security P2(限流)。

## 四、继续时要知道的坑
- **生产后端 URL = `https://rich-production-afb6.up.railway.app`**(前端 `NEXT_PUBLIC_API_BASE`
  指它)。`pure-emotion-production-6722` 是**旧/过时**的(`docs/diag-endpoints.md` 里那个 URL
  错了,值得改),别拿它当真后端测。
- `/api/_diag/*` 全被 `DIAG_TOKEN` 守卫(fail-closed);Railway 已设 AUTH_DISABLED=false /
  DIAG_TOKEN / COOKIE_SECURE=true。验证 diag 端点要带 `X-Diag-Token` 头。
- **codex 协同**:`cx exec --sandbox read-only`(gpt-5.5)做二审/博弈,默认自主多轮 + 简明
  留痕。坑:最终生成的流式偶发挂在代理上(静默就 kill 重试);长 prompt 走文件 + `< /dev/null`。
- **教训**:验证要覆盖 happy path(成功路径),不能只测 reject path —— 登录 500 就是漏了
  成功路径(发 cookie 那步)。已用 test_auth_smoke 焊死。
- 内测用户密码状态 OK(早期清一码通时已全部清理,无 NULL password_hash 锁死问题)。

## 五、关键文件指针
- 原则/计划:`docs/three-line-principle.md`、`docs/sell-line-v1-plan.md`、
  `docs/customer-claim-audit.md`(对客断言诚实性审计)。
- 卖出线:`services/sell_signal.py`(S1)、`services/sell_outcomes.py`(S2)、
  `routes/stocks.py:get_sell_risk`(S3 端点)、`[code]/page.tsx:SellRiskCard`。
- ③:`models.py:FunnelChoice`、`routes/funnel.py`、`services/outcomes.py:scenario_hit_stats/
  funnel_situation_stats`、`lib/holdingFunnel.ts`。
- 记分口径:`services/outcomes.py`(买入 hit_rate_stats / recompute_returns_from_close,
  复权安全 + returns_recomputed_at clean 戳)。
