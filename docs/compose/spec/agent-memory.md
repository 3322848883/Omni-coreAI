---
feature: agent-memory
status: in-progress
updated: 2026-09-28
branch: feat/agent-memory
commits: 
---

# 策略机器人记忆架构（Agent Memory for Trading Bots�?
## Report

## [S1] Problem

每个策略机器�?7×24 运行�?5 分钟一轮。当前每�?Plan 都是全新对话，AI 不记得刚开的单为何开、不记得近期管理决策，无法做完整的持仓管理�?
约束（从调研 113 条发现提炼）�?1. **成本可控**：上下文恒定 ~4k/轮，DeepSeek 缓存命中 1/50 价，月成�?<$1/bot
2. **质量不退�?*：长上下文致准确率下�?~30%（LongMemEval [F4-4]�?3. **近期记忆必须跟到订单管理�?*：开仓理�?�?管理事件 �?平仓
4. **长期记忆/教训累积无必�?*：规则型策略教训�?prompt，过拟合风险
5. **决策溯源是合规要�?*：MiFID II RTS 24 + Art. 17 要求完整生命周期 + 算法 ID
6. **事件粒度优于 reasoning 粒度**：FinMem 消融证明 top-5 事件最�?[F10]
7. **共享记忆预留**：multi-persona 共同记忆已有，需扩展兼容

## [S2] Design

### [S2.1] 调研结论与方案选型

| 来源 | 结论 | 应用 |
|---|---|---|
| MemGPT (arXiv:2310.08560) | OS 式虚拟上下文，fast/slow tier | 恒定短上下文，不滚全�?|
| FinMem (F9/F10) | 3 �?× top-5 事件，K=5 最�?| **recent_events** �?top-5 关键决策 |
| TradingGPT (F10) | short=3d/mid=90d/long=365d | 衰减半衰期参�?|
| FinPos (F10) | 决策须引用记忆索�?ID | **memory_refs** 机器可校�?|
| TradingAgents (F5) | PortfolioContext + decision log | 仓位是显式输�?|
| Event Sourcing (F5) | 不可变事�?+ 投影重建 | **journal** append-only |
| MiFID II RTS 24 (F5) | 完整生命周期 + 算法 ID | lifecycle + by 字段 |
| Memora FAMA (F4) | 遗忘是一等公�?| **invalidation** 失效标记 |
| Manus (F6) | 前缀稳定 = 缓存命中关键 | 固定顺序 + 变化值殿�?|
| Anthropic (F6) | context editing +39%, -84% token | 快照殿后 + compaction |
| 1.2% 毒→0.85�?.30 (F7) | 写时准入门控 | 记忆写入一致性校�?|
| 共享记忆=攻击放大�?(F7/F8) | 结构化授�?| 投票记录不可�?|

**关键判定**：场景是「定时独立决�?+ 持久身份」，不是聊天�?*不需�?dreaming/向量 RAG**；需�?*订单生命周期级近期记�?+ 事件溯源审计日志**�?
### [S2.2] 四层记忆模型

```
┌──────────── 每轮主上下文（恒�?~4k）──────────────────�?�?1. system: 策略人格+契约+工具     ~1500t  [缓存全命中] �?�?2. 订单上下�? �?S2.3           0~800t  [部分命中]   �?�?3. recent_events: top-5 关键决策  ~300t   [结构稳定]   �?�?4. 本轮快照: 行情/账户/持仓       ~1500t  [殿后变化]   �?�?5. 请输�?Plan JSON（含 memory_refs�?                  �?└────────────────────────────────────────────────────────�?```

| �?| 认知类型 | 载体 | 生命周期 | �?prompt�?|
|----|---------|------|----------|------------|
| Working | working | 当前快照 | 每轮重建 | �?|
| Order | episodic(�? | `data/shared/orders/<id>.json` | 开仓→平仓 | �?持仓�?|
| Journal | episodic(�? | `data/bots/<id>/state/memory_journal.jsonl` | 永久 append-only | �?只读摘要 |
| Profile | semantic+procedural | `data/bots/<id>/state/memory_profile.json` | 跨订单持�?| �?精简 |

### [S2.3] 订单上下文（Order Context）�?核心

复用 multi-persona �?`data/shared/orders/<order_id>.json`，扩展字段：

```json
{
  "order_id": "o-abc123",
  "symbol": "BTC_USDT", "side": "long",
  "opened_at": "2026-09-28T10:00:00Z",
  "entry_price": 84000, "size_usd": 300,
  "tp": 85700, "sl": 83500,
  "reason": "突破24h高点追多，止损放近期结构下方",
  "memory_refs": ["journal:c-042", "journal:c-038"],
  "votes": { "...": "multi-persona 已有" },
  "lifecycle": [
    {"t": "...", "act": "open", "detail": "entry=84000", "by": "fusion:weighted_vote"},
    {"t": "...", "act": "modify_tp", "detail": "tp�?6000", "by": "brooks"}
  ],
  "recent_events": [
    {"t": "...", "act": "modify_sl", "detail": "sl�?3200", "weight": 8}
  ],
  "invalidation": [],
  "status": "open",
  "created_at": ..., "updated_at": ...
}
```

**生命周期**�?- 开仓：�?reason + entry 信息 + lifecycle 首条
- 持仓期：每轮�?prompt；管理动作追�?lifecycle + 更新 recent_events
- 平仓：标 closed，移�?prompt（文件保留可审计�?
**recent_events 选取**（FinMem top-K + 衰减）：
- �?top-5 条关键决策事件，�?`weight × 时间衰减` 排序
- 权重：开�?10, 改SL=8, 大幅减仓>30%=7, 改TP=5, 小幅减仓=4, hold=1
- 时间衰减半衰�?~10 轮（2.5h），越新越高
- 全量 lifecycle 保留（可审计），只有 top-5 �?prompt

**budget**�? �?× ~30 �?�?200t

### [S2.4] 决策日志（Journal）�?事件溯源

每轮追加�?`data/bots/<bot>/state/memory_journal.jsonl`（append-only 不可变）�?
```json
{
  "ts": "2026-09-28T10:15:00Z",
  "cycle_id": "c-042",
  "decision": "long",
  "reasoning": "突破24h高点...",
  "memory_refs": ["journal:c-038"],
  "snapshot_digest": "sha256:...",
  "llm_model": "deepseek-flash",
  "prompt_cache_hit_tokens": 1850,
  "executed": true,
  "exec_result": {"order_id": "o-abc123", "filled": 84000}
}
```

**用�?*：合规审计（MiFID II 时间序列+算法ID）、崩溃恢复、近况摘要来源、缓存命中率监控

### [S2.5] 策略画像（Profile）�?确定性聚�?
`data/bots/<bot>/state/memory_profile.json`，平仓后更新（纯 Python 统计，不�?LLM）：

```json
{
  "total_trades": 42,
  "win_rate": 0.57,
  "avg_hold_rounds": 12,
  "avg_pnl_usd": 45.2,
  "max_drawdown_usd": -180,
  "best_act": "breakout_follow",
  "worst_act": "counter_trend",
  "updated_at": "..."
}
```

精简后注�?system prompt（~50t）：`"你的历史表现: 胜率57%, 均持�?2�? 均盈�?45u"`

### [S2.6] 上下文组装（缓存优化�?
**固定前缀**（缓存命中区 ~1850t）：
1. system prompt（策略人�?契约）�?逐字稳定
2. 工具定义 �?顺序固定
3. 订单上下文模板框�?�?结构稳定
4. recent_events 框架 �?结构稳定

**动态后缀**（缓存未命中�?~1850t）：
5. 订单上下文具体�?6. recent_events 内容
7. 本轮快照

**规则**（Manus/Azure/DeepSeek）：
- 禁止秒级时间戳进 system prompt
- JSON 序列化确定性（固定 key 顺序�?- 不中途增删工�?- 记录 `prompt_cache_hit_tokens`

### [S2.7] 遗忘与失�?
| 机制 | 触发 | 行为 |
|------|------|------|
| 订单生命周期 | 平仓 | 移出 prompt，保留文�?|
| recent_events 滑窗 | 每轮 | top-5 重�?|
| TTL | 90 �?| journal 归档 `.gz` |
| invalidation | 市场结构变化 | 标记旧假设失�?|
| profile 聚合 | 平仓�?| 确定性统计更�?|

### [S2.8] 多人格兼容（复用 multi-persona�?
- 投票记录�?`votes`（已有）；lifecycle 条目 `by` 字段标记操作�?- `memory_refs` 跨人格共享（同一 order_id�?- Journal �?bot_id 分文件，各自�?reasoning 独立�?
### [S2.9] 执行挂钩

| 事件 | 动作 |
|------|------|
| 开仓成�?| 创建/更新 order_context: reason + lifecycle[open] |
| �?TP/SL | 追加 lifecycle[modify_*] + 更新 recent_events |
| 减仓 | 追加 lifecycle[reduce_*] |
| 平仓 | 追加 lifecycle[close] + status=closed + 更新 profile |
| 每轮 Plan | 追加 journal 记录（含 memory_refs + cache_hit�?|

### [S2.10] 明确不做

- 长期记忆�?/ dreaming / LLM 后台固化
- 全量历史滚动 messages
- 向量 RAG 记忆�?- 跨策�?embedding 迁移
- 记忆编辑 UI

## [S3] Out of Scope

- 组合级记忆（Phase 3，data/shared/portfolio.json�?- 合规审计导出（Phase 5，journal→RTS 24 格式�?- 记忆评估基准自建
- �?bot 共享 Journal 合并

## Tasks

- [ ] T1: order_context 扩展 �?reason/memory_refs/lifecycle/recent_events/invalidation 字段 + 读写�?(covers: S2.3)
- [ ] T2: 执行挂钩 �?开/�?平仓后自动更新订单上下文 + 更新 recent_events (covers: S2.9; depends: T1)
- [ ] T3: Journal 写入 �?每轮追加 memory_journal.jsonl（含 memory_refs/cache_hit�?covers: S2.4)
- [ ] T4: 上下文拼�?�?system→订单上下文→recent_events→快照，固定顺序殿后变化�?(covers: S2.2; S2.6; depends: T1)
- [ ] T5: recent_events 选取 �?top-5 �?weight×衰减排序 (covers: S2.3; depends: T1)
- [ ] T6: Profile 聚合 �?平仓后确定性统计更�?+ 精简注入 system (covers: S2.5; depends: T2)
- [ ] T7: 缓存护栏 �?前缀稳定校验 + prompt_cache_hit_tokens 日志 (covers: S2.6)
- [ ] T8: 遗忘机制 �?TTL 归档 + invalidation 标记 (covers: S2.7; depends: T1)
- [ ] T9: 测试 �?订单生命周期进出 prompt、recent_events 选取、journal 追加、缓存前缀稳定 (covers: S2.2–S2.7)
- [ ] T10: 文档 �?README/AGENTS 记忆机制、成本、操作指�?(covers: S2.2)
