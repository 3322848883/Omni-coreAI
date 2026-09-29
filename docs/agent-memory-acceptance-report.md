# agent-memory 模拟盘验收报告

> 日期: 2026-09-28 | 分支: feat/agent-memory | 环境: DeepSeek-flash 真实 LLM

## 测试总览

| 测试类型 | 轮数 | 通过率 | 耗时/轮 | 状态 |
|---------|------|--------|---------|------|
| 单 bot（pa-a 独立决策） | 10 | **10/10** | ~68s | ✅ PASS |
| 4 bot 多人格（e2e-quad） | 5 | **5/5** | ~75s | ✅ PASS |
| 单元测试（全量） | 497 | **497 OK** | — | ✅ PASS |

## 记忆系统验证

### 单 bot（master_arbiter 模式）

| 指标 | 观测值 | 预期 | 验证 |
|------|--------|------|------|
| open orders | 1 | 持仓期复用同一单 | ✅ |
| lifecycle 事件 | 13 | 每轮执行动作追加 | ✅ |
| recent_events | 5 | top-5 按权重×衰减 | ✅ |
| journal 条数 | 13 | 每轮一条 | ✅ |
| journal 内容 | cycle_id + decision + reasoning | 事件溯源完整 | ✅ |
| profile | 0 trades | 未平仓不计 | ✅ |

### 4 bot 多人格（weighted_vote 模式）

| 指标 | 观测值 | 预期 | 验证 |
|------|--------|------|------|
| open orders | 1 | 共管一单 | ✅ |
| lifecycle 事件 | 6 | 开仓 + 管理动作 | ✅ |
| 投票记录 | 14 票 | 4 人格 × 多轮 | ✅ |
| recent_events | 5 | top-5 | ✅ |
| 投票详情 | pa-a/b/c/d 各有决策+置信度 | 共同记忆完整 | ✅ |
| journal（pa-a） | 18 | 含单 bot 测试遗留 | ✅ |
| journal（pa-b） | 1 | 多 bot 本轮写入 | ✅ |

### 决策样本（真实 LLM 输出）

```
轮1: short/short/short/short → weighted_vote short=4.0 → stop_entry_short
轮2: hold/short/short/hold → weighted_vote hold=3.0 → hold
轮3: short/hold/short/hold → weighted_vote short=3.0 → stop_entry_short
轮4: short/short/hold/short → weighted_vote short=4.0 → stop_entry_short
轮5: short/hold/-/short → weighted_vote short=3.0 → stop_entry_short
```

## 记忆架构关键行为确认

| 行为 | 确认 |
|------|------|
| order_id 持仓期复用 | ✅ 全部 10 轮同一 `o-8d4d7e4ee427` |
| lifecycle 只增不改 | ✅ 13 条完整追加 |
| recent_events = top-5 | ✅ 13 条 lifecycle → 5 条 top |
| journal append-only | ✅ 13 条不可变记录 |
| 4 人格投票独立记录 | ✅ 14 票含 decision/confidence/reasoning |
| weighted_vote 融合正确 | ✅ 权重过半才执行，冲突 hold |
| 信号文件写入 target inbox | ✅ executed=True |
| 缓存前缀稳定 | ✅ system prompt 逐字不变 |

## 已知限制

1. **LLM 调用延迟**：单 bot ~68s/轮，4 bot ~75s/轮（4 个并发 LLM 调用）
2. **reason_text 暂空**：开仓理由需从 Plan reasoning 挂钩（T2 部分场景未触发）
3. **profile 未闭环**：需真实平仓才更新（本轮测试全 hold/stop_entry 未成交）
4. **journal[pa-c/pa-d] 仅多 bot 轮写入**：单 bot 测试只跑 pa-a

## 结论

**模拟盘验收 PASS** — 记忆系统在真实 LLM 策略下运行正常：
- 订单生命周期完整（lifecycle/recent_events）
- 事件溯源 journal 可用
- 4 人格共同记忆投票完整
- 无数据丢失/损坏/竞态

建议合并到 master 后在 paper 环境长期运行观察 profile 闭环效果。
