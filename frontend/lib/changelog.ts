/**
 * Release notes — single source of truth.
 *
 * Append new entries to the TOP of CHANGELOG when shipping. The entry's
 * `date` doubles as its ID; the modal uses it to decide whether the user
 * has seen this release yet (compared against localStorage).
 *
 * Format philosophy:
 *   - User-facing only. No infrastructure / model names / library churn.
 *   - Sections are optional sub-headings within an entry. Older entries
 *     usually use a single untitled section; bigger releases group bullets
 *     under titles.
 */

export type ChangelogSection = {
  title?: string;
  items: string[];
};

export type ChangelogEntry = {
  date: string;          // YYYY-MM-DD — acts as version ID
  sections: ChangelogSection[];
};

export const CHANGELOG: ChangelogEntry[] = [
  {
    date: "2026-05-10",
    sections: [
      {
        title: "多账号",
        items: [
          "手机号 + 短信验证码登录（内测期固定 8888，加白名单生效）",
          "每个账号独立的自选池，互不可见",
          "登录持久化 30 天",
        ],
      },
      {
        title: "盯盘字段重做",
        items: [
          "新增 3 日涨幅 / 3 日换手 / 3 日净流入",
          "新增「行业 / 水位」列：所属行业 + 估(PE 行业分位) / 势(3 日涨幅分位) / 金(3 日资金分位) 三个 chip",
        ],
      },
      {
        title: "三档操作建议",
        items: [
          "详情页关键数据卡可在 激进 / 中立 / 保守 三档之间切换，各档独立给出仓位、买入价、持有时间和操作思路",
        ],
      },
      {
        title: "技术面 + 次日预判",
        items: [
          "详情页新增「次日走势预判」卡：方向、价格区间、置信度",
          "新信号：突破 20 日新高 / 跌破年线 / MACD 金叉 / MACD 死叉",
        ],
      },
      {
        title: "板块",
        items: [
          "/板块 页面置顶「今日 TOP5 推荐」：每个板块 3 支个股 + AI 给出推荐理由",
          "下方保留全部板块涨跌榜",
        ],
      },
      {
        title: "体验优化",
        items: [
          "深 / 浅色主题切换（默认跟随系统）",
          "各种图标 hover 出详细解释：估/势/金、🔴 红旗、买/卖/观望、已过期 等",
          "自选池改名「自选池管理」；批量删除从粘贴文本改为「勾选 + 删除选中」",
          "北交所 920xxx 新代码段支持",
          "详情页移除模型名和无意义的 8 项五星评分",
        ],
      },
    ],
  },
  {
    date: "2026-04-29",
    sections: [
      {
        items: [
          "盯盘列表自动按「建议买入 / 卖出 / 观望 / 不入手」分组，顶部显示每组数量",
          "标星「特别关注」⭐：标星的票自动置顶到分组首位",
          "交易时段后台 30 秒自动刷新，无需手动 reload",
          "强信号行红色高亮，一眼可见",
        ],
      },
    ],
  },
  {
    date: "2026-04-27",
    sections: [
      {
        items: [
          "盯盘列表：当日报价、涨跌幅、成交、主力净流入、北向持股一览",
          "信号自动识别：涨停 / 跌停 / 主力大额流入流出 / 重要公告 / 上龙虎榜",
          "详情页 AI 深度解析：合理买入价 / 卖出价、建议仓位、持有时间、置信度",
          "关键数据卡：风险红旗、分档止损线、未持仓 / 浮盈 / 浮亏 不同场景的操作建议",
          "详情页一键生成 + 每天 9:35 自动批量生成",
          "列表顶部按「建议买入 / 观望 / 卖出 / 不入手 / 待生成」快速筛选",
          "手机端适配 + 可加到桌面 / 主屏",
        ],
      },
    ],
  },
  {
    date: "2026-04-26",
    sections: [
      {
        items: [
          "自选池：粘贴 6 位代码或上传 Excel / CSV 批量导入",
          "简单密码登录",
        ],
      },
    ],
  },
];

export const LATEST_CHANGELOG = CHANGELOG[0];
