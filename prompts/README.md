# 策略提示词（策略人格）

这里放的是 **可更换的策略人格**。交易契约、动作枚举、安全规则写死在  
`gate_bot/strategist/prompt.py`，不要在策略文件里改语义。

## 可换 / 固定

| 可换（本目录 md） | 固定（代码 / 配置） |
|------------------|---------------------|
| 角色（你是…策略引擎） | Plan JSON 格式、action 枚举 |
| 决策顺序、进出场偏好 | 开仓必须 sl、不确定 hold |
| 突破 / 网格 / 稳健等风格 | 每轮行情快照 |
| 禁止清单、频率偏好 | `risk.*` 风控（yaml，程序强制） |

系统提示词只含**输出契约与安全规则**；角色与风格全部在本目录 `prompt_file`。

## 文件怎么写

只写**角色与判断风格**，参考 `vergex_default.md`：

1. **角色** — 你是谁（如：多品种永续策略引擎）
2. **核心原则** — 一两句（如：保本优先 / 只做区间）
3. **决策顺序** — 有仓先管理 → 再谈开仓
4. **入场** — 用哪种 action、要不要 sl/tp、每轮 chip 数
5. **离场** — reduce / close 条件
6. **禁止** — 明确不做什么（减少乱动）
7. **输出** — 只输出 JSON Plan；reasoning 简短

示例骨架：

```markdown
# 我的策略人格

你是…。核心原则：…

## 决策顺序
1. 有仓：优先减仓/止损/持有。
2. 无仓：…才 open_* / stop_entry_*。
3. 不确定 → hold。

## 入场
- 每轮最多 N 个 chip。
- 必须 sl；tp …
- 突破用 stop_entry_*，不要用 sl 表示突破。

## 禁止
- 不追单边 / 不扛仓 / 不…

## 输出
- 只输出 Plan JSON。
```

## 怎么挂到 bot

```yaml
# config/bots/<bot_id>.yaml
strategist:
  prompt_file: prompts/my_strategy.md
  risk:
    min_confidence: 0.75
    max_notional_usd: 50
```

多策略：每个策略一份 md + 一份 bot 配置 + 自己的 `inbox/<bot_id>/`。

## 验收

```powershell
.venv\Scripts\python.exe -m gate_bot plan --bot <bot_id>
```

看 Plan `reasoning` / chips 是否符合你的风格；再跑  
`scripts\test_strategy_prompt.py` 做提示词级联调。
