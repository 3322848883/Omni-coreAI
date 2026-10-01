# SkillKit 生产测试 — LLM 真实使用 skill 做行情分析

日期：2026-09-30 · 全程本地，未合并服务器

## 测试目标

验证 **bot 的 LLM 策略层通过 `skill` 工具真实加载并使用** `price-action-trading` 技能完成一次行情分析（不是人工代劳）。

## 测试环境

| 项 | 值 |
|----|-----|
| bot | `config/bots/skill-e2e.yaml`（testnet，max_notional 10，allow: hold/open_long/open_short） |
| prompt | `prompts/skill_e2e_test.md`（本轮任务=行情分析，强制先调 skill 工具） |
| 模型 | `global:deepseek-v4.1-flash`（OPENAI_BASE_URL 网关） |
| skill | `skills/price-action-trading`（v34.2，从 zip 安装） |
| 命令 | `python -m gate_bot plan --bot skill-e2e` |

## 执行过程与修复

| 轮次 | 现象 | 结论 |
|------|------|------|
| R1 | LLM 直接 hold，无工具调用 | 配置缺 `tools.enabled` |
| R2 | 开工具后 LLM 调了 smc/sqzmom，仍不调 skill | catalog 有清单但**缺使用提示**，模型不知道要先 load |
| R3 | 修复 `render_catalog` 注入使用提示 + persona 明确分析任务 | **LLM 调用 `skill(price-action-trading)`** ✅ |

修复内容：`gate_bot/skillkit/catalog.py` 的 catalog 段加入
「任务匹配某个 skill 时，**先调用 skill(name) 加载其完整指令再作答**」。

## 关键证据

### 1. skill 激活审计（唯一由 `run_skill_tool` 写入）

`logs/skill_journal.jsonl`：
```json
{"kind": "skill_activate", "bot_id": "", "skill_id": "price-action-trading", "body_tokens": 2479, "truncated": false, "ts": "2026-09-30T15:01:09Z"}
```
时间落在本轮 plan 周期（15:00:46 启动）内 → LLM 通过 `skill` 工具真实加载了 body。

### 2. LLM 输出使用了 skill 方法论（thinking.json 14.9KB）

推理链出现 Brooks 体系概念（这些来自 skill body/knowledge）：
- **Always In** 状态判断
- **BAN** 禁止清单（区间中部禁交易）
- **trading range** 82878–85615 判定
- 假突破 / bull trap / 失败突破
- 交易者方程式概率评估（下破 ~40% / 上破情景 / 震荡 ~10%）
- 「区间中部无优势，观望」

### 3. 最终 Plan（风险闸门正确工作）

```json
{
  "cycle_id": "BTC_USDT-1h-1790780400",
  "reasoning": "价格行为：突破85615失败回落，区间中部无优势，观望",
  "chips": [{
    "symbol": "BTC_USDT", "action": "hold", "confidence": 0.58,
    "meta": {"reasoning": "区间82878-85615中部，EMA20附近，等边界信号"}
  }]
}
```

- confidence 0.58 < min_confidence 0.7 → 正确 hold
- `write_hold` 落盘 `state/*.hold.json`
- 风控：skill 只影响观点，未越权动单

## 多角度生产测试（第二轮）

### LLM 真实场景 × 4（全部 PASS）

| 场景 | 任务 | skill 激活 | 方法论命中 | thinking |
|------|------|:---:|------|------|
| **A 自然触发** | 分析 ETH 1h（不点名 skill） | ✅ | trading range | 6980 字 |
| **B 规则检查** | 检查交易计划合规 | ✅ | BAN、SL | 9952 字 |
| **C 知识查询** | H2 是什么 / Always In 判断 | ✅ | H2、Always In | 4567 字 |
| **D 复盘** | 追突破 -8% 错误模式 | ✅ | 失败分析 | 4563 字 |

### 负面场景（PASS）

| 场景 | 期望 | 实测 |
|------|------|------|
| **E 无关任务**（算术估算） | 不加载 skill | ✅ LLM 明确判断 "not matching any skill"，改用 `klines` 工具算数；journal 未增长 |

### 自动化边缘闸门 × 14（PASS）

| 组 | 用例 | 结果 |
|----|------|:---:|
| 多 skill 可见性 | 3 skill 安装；`model-invocation:false` 不进 catalog | ✅ |
| | 白名单只放行指定 skill | ✅ |
| | `skills: []` 全不可见 | ✅ |
| | catalog 同列多 skill（不含用户专属） | ✅ |
| 工具闸门 | 用户专属 skill 模型调用被拒 | ✅ |
| | 未启用/空名单被拒 | ✅ |
| | 未知 skill 报错并列出可用 id | ✅ |
| 预算降级 | 40 skill 超预算自动裁剪、高频保留 | ✅ |
| | 极小预算至少保留 1 | ✅ |
| 校验 | 3 个已装 skill 全 PASS | ✅ |
| | `model-invocation` 字段解析 | ✅ |

### 多 skill catalog 实测

```
<skill_catalog>
技能目录：…先调用 skill(name) 加载其完整指令再作答…
- price-action-trading: Al Brooks 价格行为交易辅助…
- test-helper: Test helper skill for visibility whitelist tests…
</skill_catalog>
```
`sentinel-risk`（model-invocation:false）正确隐藏。

### 回归

- skillkit 单测：25 + 14 = **39 OK**
- 全量：**759 OK**（连续确认；先前 2 个 discussion mock 失败为抖动，单跑 13 OK）

## 结论

**SkillKit 端到端验证通过**：
1. catalog 注入 ✅（含使用提示）
2. LLM 自主调用 `skill(name)` 工具 ✅（journal 证据）
3. skill body 注入后 LLM 采用其方法论产出分析 ✅（Always In/BAN/交易者方程）
4. advisory 红线保持 ✅（Plan 仅观点，hold 落盘，无越权下单）
5. 21 工具面、预算（body 2479 < 5000）、审计链完整 ✅

## 已知改进项

1. run_tool 的 `bot_id` 由 LLM 传入为空——应由 runner 上下文注入（journal 可追溯到 bot）
2. thinking.json 只存 reasoning_chain，未存原始 tool_calls 列表——审计可补 tool 调用明细
