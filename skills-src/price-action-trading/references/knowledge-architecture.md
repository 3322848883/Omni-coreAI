# 知识库架构 — 三系统分离

## 系统划分

```
┌─────────────────────────────────────────────────────────────┐
│                    价格行为学技能                              │
│                                                             │
│  references/knowledge/          memory/            assets/templates/          │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐       │
│  │ 分析引擎  │       │ 交易记忆  │       │ 交易日志  │       │
│  │          │       │          │       │          │       │
│  │ workflow │       │ trader_  │       │ trading_ │       │
│  │ strategy │       │ profile  │       │ journal  │       │
│  │ 16 themes│       │ error_   │       │ trade_   │       │
│  │ source/  │       │ patterns │       │ review   │       │
│  │ crypto   │       │ market_  │       │ order_   │       │
│  │          │       │ wisdom   │       │ log      │       │
│  └──────────┘       └──────────┘       └──────────┘       │
│  AI 分析时读取        AI 分析时检索       分析结果写入        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    Obsidian Vault                            │
│                                                             │
│  concepts/          entities/         queries/              │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐       │
│  │ 概念页    │       │ 作者页    │       │ 查询页    │       │
│  │ 13个     │       │ al-brooks│       │ (不用)    │       │
│  └──────────┘       └──────────┘       └──────────┘       │
│                                                             │
│  raw/papers/al-brooks/                                      │
│  ┌──────────────────────────────────────────┐              │
│  │ V1_趋势篇/ V2_区间/ V3_反转/ (27个原文)   │              │
│  └──────────────────────────────────────────┘              │
│  人工阅读、搜索、笔记、理论扩展                               │
└─────────────────────────────────────────────────────────────┘
```

## 各系统职责

### references/knowledge/ — AI 分析引擎
- **workflow.md**（260KB）：8阶段49步骤工作流，驱动分析流程
- **strategy_workflow.md**（100KB）：23章133+规则引擎，执行决策
- **theme1-16**：16个主题知识库，提供理论支撑
- **source/**：27个Al Brooks原文提取报告，提供原文证据
- **instrument_crypto_specifics.md**：加密货币专项规则
- **用途**：AI 分析行情时按 Step 读取对应文件

### memory/ — 交易记忆（动态）
- **trader_profile.md**：个人画像（优势/劣势/情绪模式）
- **pattern_effectiveness.md**：设置有效性（各类型胜率）
- **error_patterns.md**：错误模式（活跃错误/根因/改进）
- **market_wisdom.md**：市场智慧（已验证规律/品种特征）
- **strategy_hypotheses.md**：策略假设（验证进度）
- **cross_instrument.md**：跨品种对比
- **用途**：AI 分析时自动检索，提供个人化建议
- **数据来源**：assets/templates/ 中的交易记录驱动记忆更新

### assets/templates/ — 交易日志
- **price_action_analysis.md**：26步分析模板
- **trading_journal.md**：交易日志（分析结果存这里）
- **trade_review.md**：交易复盘
- **order_log.md**：订单日志
- **daily_deep_review.md**：每日深度复盘
- **用途**：记录分析结果和交易数据，驱动 memory/ 更新

### Obsidian + llm-wiki — 人工知识库
- **concepts/**：概念页（趋势/回撤/突破/反转等）
- **entities/**：人物/来源页
- **raw/papers/**：Al Brooks 原文（与 references/knowledge/source/ 重复）
- **用途**：人工阅读、学习、笔记、理论扩展、Graph View 可视化

## 数据流

```
分析行情时：
  references/knowledge/ → AI 读取 → 输出分析结果 → assets/templates/trading_journal.md
  memory/    → AI 检索 → 附加个人化建议

交易完成后：
  assets/templates/trade_review.md → 更新 memory/pattern_effectiveness.md
  assets/templates/error_log.md    → 更新 memory/error_patterns.md
  assets/templates/daily_review.md → 更新 memory/market_wisdom.md

人工学习时：
  Obsidian concepts/ → 阅读、笔记、关联
  Obsidian raw/papers/ → 原文查阅
```

## 不要做的事

- ❌ 把分析结果写入 Obsidian queries/
- ❌ 用 Obsidian 概念页作为分析依据
- ❌ 在 Obsidian 和 references/knowledge/ 之间同步文件
- ❌ 用 Obsidian 替代 memory/ 的个人交易记忆
- ❌ 在分析中引用 Obsidian 路径

## 要做的事

- ✅ 分析结果 → assets/templates/trading_journal.md
- ✅ 分析依据 → references/knowledge/ 中的实际文件
- ✅ 个人记忆 → memory/ 自动检索
- ✅ 理论学习 → Obsidian concepts/ + raw/papers/
- ✅ 两套系统各司其职，互不干扰
