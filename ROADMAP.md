# Roadmap — 下一波迭代

存档时间：2026-05-10
状态：Phase 1–9 + UX 第二波（主题/Tooltip/自选池/板块 TOP5/更新日志）已上线

---

## 一、AI 分析效果提升

### 1. 持仓上下文（最优先 · 价值密度最高）

**问题**：现在解析对所有用户都一样，是「通用建议」。
**方案**：让用户录入 cost basis（买入日期 / 买入价 / 持仓数量 / 成本），AI 调用前注入这些上下文。

输出会从「合理买入价 23–25」升级到：
- 你的成本 22.8 元，当前 +5.2%
- 已突破前高，可锁定 50% 浮盈
- 你的紧急止损线对应 -8%（绝对价 20.96）

**改动**：
- 新表 `holdings`：(user_id, code, cost_basis, shares, opened_at, notes)
- 自选池新增 tab「持仓」(or 行内一键转持仓)
- analysis._user_prompt 注入持仓段落
- 详情页关键数据卡顶部加「你的持仓」摘要

---

### 2. 时段感知 prompt（改动小，效果直接）

**问题**：盘前 / 盘中 / 盘后内容侧重应该不同，现在都用同一套 prompt。

**方案**：`_user_prompt` 按 Asia/Shanghai 时间分支：
- 盘前（< 9:00）：昨日复盘 + 今日开盘要点 + 当日关键事件
- 盘中（9:30–15:00）：实时调仓信号、强信号高亮
- 盘后（≥ 15:00）：当日复盘 + 明日预判（next_day_outlook 已有，重点强化）

**改动**：仅 `services/analysis.py` 一处。

---

### 3. AI 建议命中率追踪

**问题**：信任问题 + 自我迭代缺乏 ground truth。

**方案**：
- 新表 `analysis_outcomes`：(code, generated_at, actionable, target_low/high, snapshot_at_T+1/3/5/20)
- 每天 cron 跑一次：把 N 天前的建议跟现在的实盘价对比，回填表现
- 详情页 + dashboard 展示「过去 30 天买入建议平均收益 +X%、踩雷率 Y%」
- 自己也用这个数据回看哪些 prompt / 风格表现差

---

### 4. 板块 / 大盘相对位置

**问题**：现在解析孤立看一支股，没参考系。

**方案**：prompt 里加「今日大盘 +X%、行业 +Y%、本股 +Z%」段；让 AI 知道是绝对涨跌还是相对超额。

**改动**：snapshot 拉行情时多记一个 SH/SZ/CSI300 当日点位 + 行业平均。

---

### 5. 关键事件日历

**问题**：财报、解禁、ST 风险这些硬信号 AI 想不出来。

**方案**：
- 拉数据源：akshare 有 `stock_em_yjbb`（业绩报表）、`stock_em_lhb_yyjzcx`（解禁）等
- 详情页加「未来 2 周事件」卡：一季报披露 5/15、定增解禁 5/22…
- 关键事件 ≤ 3 天时盯盘列表加角标提示

---

### 6. 多策略风格选择

**问题**：用户风险偏好/风格不同，单一 prompt 服务不了所有人。

**方案**：内置 2-3 套策略
- 趋势：技术面权重高，看突破/均线
- 价值：估值 / 业绩权重高，看 PE 行业分位、ROE
- 短线博弈：题材 / 资金 / 龙虎榜权重高

用户在自选池/详情页选风格；prompt 系统提示词不同。

---

## 二、站点信息架构

### A. 全局 TopNav（地基，先做）

**问题**：每页自己 render header，nav 重复，扩充难。

**方案**：抽 `<TopNav />` 组件——左 logo + 主链接（盯盘/板块/自选池），右用户区（chip + 主题 + 日志）。一处改全站统一。

---

### B. /stocks 与 /watchlist 合并

**问题**：盯盘是看，自选池是改，但用户经常在看的时候想改。

**方案**：/watchlist 改成 /stocks 的「管理」tab——切到该模式后表格行变成可勾选 + 显示导入/删除按钮。日常 90% 时间在「盯盘」模式。

---

### C. Dashboard 首页 `/`

**问题**：现在 `/` 是空的或 redirect，缺产品门面。

**方案**：登录后落地页，三块：
- 上：今日大盘 + 板块 TOP3 推荐
- 中：自选池「今日要看的」（强信号 / 大涨大跌 / 命中规则的 5–10 支）
- 下：待关注事件（本周财报、解禁等）

完整盯盘表通过「查看全部」展开。

---

### D. 板块下钻 `/sectors/[name]`

**问题**：板块 hero 只能看推荐，点不进去；板块和自选池断层。

**方案**：
- 板块详情页：成份股全表 + 板块走势图
- **自选池里属于该板块的票高亮**——把"板块"和"自选"打通
- 板块层 AI 解析：今日异动原因、领涨股、资金流向

---

### E. 个股详情增强 `/stocks/[code]`

- 顶部加 K 线图（前端 echarts 或 lightweight-charts，喂 klines 表数据）
- 同行业 3–5 支对比 mini chart
- 历史 AI 建议时间轴：5/3 买、5/8 持有、5/10 卖出 → 实际涨跌如何

---

### F. 大屏多列（带鱼屏专属）

`>1920px` 启用 left sidebar nav + main + right rail（事件提醒 / 推荐 / 大盘）。中小屏退化为单列 + top nav。

---

## 三、优先级 / 时间线

### 第一波（2 周内）— 撬动效果大、改动聚焦

- [ ] **持仓 cost basis 录入 + AI 吃**（~3 天）
- [ ] **时段感知 prompt 分支**（~1 天）
- [ ] **全局 TopNav 组件**（~1 天）

### 第二波（4–6 周）— 结构性升级

- [ ] Dashboard 首页 `/`
- [ ] 板块下钻页 `/sectors/[name]`
- [ ] AI 命中率追踪（先收集数据，UI 第三波再做）

### 第三波（1–2 月）— 增量、需要数据源接入

- [ ] 关键事件日历（财报/解禁/分红）
- [ ] 个股详情 K 线图 + 同业对比
- [ ] 策略风格切换
- [ ] 大屏 sidebar 布局
- [ ] AI 命中率展示（卡片/榜单）

---

## ⚠️ 已知 TODO：真实手机短信验证

**当前状态**：dev 模式
- `backend/app/services/sms.py` 走「白名单 + 固定 8888」
- `SMS_DEV_WHITELIST` env var 列出允许登录的手机号
- 未列入白名单的手机号点「发送验证码」会被拒

**为什么这样做**：内测前期不想等阿里云审批，先用白名单跑通流程；不在白名单的人进不来 = 天然访问控制。

### 切到生产真实短信的步骤

#### 1. 阿里云短信服务开通（耗时主要在审批）

1. 登录阿里云控制台 → 短信服务（dysmsapi）
2. **申请签名**：填一个公司或产品名（比如「rich」「股票盯盘」）→ 提交资质 → 1–2 工作日审批
3. **申请模板**：内容形如 `您的验证码是${code}，5分钟内有效。` → 同样 1–2 工作日
4. **创建 AccessKey**：阿里云 → AccessKey 管理 → 创建子用户 + 分配 `AliyunDysmsFullAccess` 权限 → 拿到 AccessKeyId + AccessKeySecret

> 国内短信签名 + 模板都强制审批。建议留 3 个工作日缓冲。

#### 2. 后端配置

在 Railway → backend → Variables 加 4 个环境变量：

```
SMS_PROVIDER=aliyun
ALIYUN_SMS_ACCESS_KEY_ID=<你的 AccessKeyId>
ALIYUN_SMS_ACCESS_KEY_SECRET=<你的 AccessKeySecret>
ALIYUN_SMS_SIGN_NAME=<审批通过的签名，比如 "rich"
ALIYUN_SMS_TEMPLATE_CODE=<审批通过的模板 ID，形如 SMS_xxxxxxx>
```

把 `SMS_DEV_WHITELIST` 留空 / 删掉。

#### 3. `services/sms.py` 实现真实发送（半天工作）

现在的 stub 大概长这样：
```python
def send_code(phone: str, code: str) -> None:
    if settings.SMS_PROVIDER == "dev":
        ...
    elif settings.SMS_PROVIDER == "aliyun":
        # TODO: 这里要补
```

要补的是阿里云 SDK 调用。两条路：
- **走 SDK**：`pip install alibabacloud-dysmsapi20170525`，文档完善但依赖重
- **走 HTTP API**：直接 POST 到 `https://dysmsapi.aliyuncs.com`，自己拼签名（HMAC-SHA1）。轻量但要写 ~30 行签名代码

我推荐 SDK，省心。代码骨架：
```python
from alibabacloud_dysmsapi20170525.client import Client
from alibabacloud_dysmsapi20170525 import models
from alibabacloud_tea_openapi import models as open_api_models

def _aliyun_send(phone: str, code: str) -> None:
    config = open_api_models.Config(
        access_key_id=settings.ALIYUN_SMS_ACCESS_KEY_ID,
        access_key_secret=settings.ALIYUN_SMS_ACCESS_KEY_SECRET,
    )
    config.endpoint = "dysmsapi.aliyuncs.com"
    client = Client(config)
    req = models.SendSmsRequest(
        phone_numbers=phone,
        sign_name=settings.ALIYUN_SMS_SIGN_NAME,
        template_code=settings.ALIYUN_SMS_TEMPLATE_CODE,
        template_param=json.dumps({"code": code}),
    )
    resp = client.send_sms(req)
    if resp.body.code != "OK":
        raise RuntimeError(f"sms send failed: {resp.body.code} {resp.body.message}")
```

#### 4. 风控（重要）

阿里云短信按条计费（~3 分/条），开公网就要防刷：
- 同一手机号 60 秒内只能发 1 次（前端 cooldown 已有，后端也加一道）
- 同一手机号每日上限（5 条）
- 同一 IP 每日上限（20 条）
- 失败/未注册的手机号不能告知「该号未注册」（防遍历）

后端加个 Redis-less 简单方案：用 DB 表 `sms_send_log(phone, ip, sent_at)` 写记录，查询时 count 当前窗口。

#### 5. 灰度切换

我建议：
1. 阿里云审批通过 + SDK 接好 + 风控加上后，**保留 dev whitelist 双轨**——白名单内还是 8888，白名单外走真实短信
2. 跑 1-2 周稳定后，再删 dev 路径

这样新用户能进来，老内测不受影响。

---

## 不在 roadmap 的事（明确不做）

- 多模型 ensemble（成本不划算，先单模型把 prompt 调好）
- 概念/题材绑定（akshare 数据源不稳定，需要自维护）
- 公开回测「跟单收益」展示（合规风险，绝对不暴露）
