# L7 好 / 坏 Plan 对比（LLM 输出）

## 好 Plan（合法 + 合理）

```json
{
  "cycle_id": "good-001",
  "reasoning": "BTC 放量站上 EMA20，带止损试多",
  "chips": [
    {
      "symbol": "BTC_USDT",
      "action": "open_long",
      "confidence": 0.8,
      "size_usd": 25,
      "tp": 88000,
      "sl": 83800,
      "type": "market",
      "reasoning": "突破确认"
    }
  ]
}
```

## 好 Plan（该 hold）

```json
{
  "cycle_id": "good-002",
  "reasoning": "震荡无方向，保本优先",
  "chips": [{"symbol": "BTC_USDT", "action": "hold", "confidence": 0.4}]
}
```

## 坏 Plan（会被拒 / 错误）

| 问题 | 示例 | 结果 |
|------|------|------|
| 非法 action | `action: "exec_shell"` | PlanError |
| 路径 symbol | `symbol: "../../etc"` | PlanError |
| 无止损开仓 | 只有 size 无 sl | `SL_REQUIRED` |
| 超限仓位 | `size_usd: 9999` | risk reject |
| 用 sl 表示突破 | `action: open_long, sl: 87000` 当突破 | 语义错，应用 stop_entry_* |
| 非法 type | `type: "evil"` | PlanError |
| 缺 size | open 无 size | reject |

**给提示词的启示**：
1. 不确定 → `hold`
2. 开仓必须 `sl`
3. 突破用 `stop_entry_*`，不是 `sl`
4. `size_usd` 不超过 risk 上限
5. reasoning ≤30 字
