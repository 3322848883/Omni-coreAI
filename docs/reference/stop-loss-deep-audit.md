# 止损（SL）深度核实 — 问题、根因、失效场景

**结论先行**：当前止损**不是**「一定会出问题」，但在 **7 个真实路径上会失效或形同虚设**。其中 3 个属于 **P0 资金安全缺陷**（可复现）。  
本文只写「哪里坏、**为什么**坏、坏了会怎样」，不做空泛清单。

---

## 0. 止损在代码里的真实形态

```text
开仓 place_order
   ↓ 成功且 get_order 确认
按「计划张数」挂 SL
   ├─ sl_mode=trigger（默认）→ Gate price_order
   │     trigger: 价格触达 sl（price_type 默认 latest）
   │     initial:  reduce_only 限价，价 = sl ± 0.1%
   └─ sl_mode=limit_order → 普通 reduce_only 限价挂在 sl
```

关键事实：

| 事实 | 代码位置 | 含义 |
|------|----------|------|
| 触发后挂的是 **限价**，不是市价 | `executor._place_trigger` + `sizing.default_trigger_limit_price` | **跳空可能永远不成交** |
| SL 数量 = **计划** `contracts`，不是实际成交 | `_open` 传 `size=contracts` | 部分成交时 SL 数量错误 |
| **允许无 SL 开仓** | schema/risk 都不强制 `sl` | 裸仓合法存在 |
| `replace` 会 **cancel 全部 price_orders** | `_apply_replace` | 新单失败则旧 SL 已没了 |
| 确认失败会 **再下一次单** | `_place_entry_leg` / `_place_exit_leg` 重试 | 超时误判 → **双开/双 SL** |

---

## 1. P0-1：止损触发后是「限价单」，跳空就不成交

### 现象
`trigger_price=100` 触发后，实际挂出的平仓限价是 **99.9**（只让 0.1%）：

```text
long  SL: trigger 100 → initial limit 99.9   (Δ0.1%)
short SL: trigger 100 → initial limit 100.1  (Δ0.1%)
```

若行情从 100 **跳空到 95**：
- 卖出限价 99.9 挂在盘口 **上方**，变成 maker，**不会立刻成交**
- 价格可能继续跌，止损停在「已触发、未成交」

### 为什么会这样
1. **产品设计把「触发」和「成交」拆成两步**（Gate `price_orders`：trigger → initial order）。
2. `default_trigger_limit_price` 只让 **0.1%** 价差——按「轻微滑点」设计，**没按「跳空/插针」设计**。
3. 代码显式禁止 trigger 后下市价平仓：
   ```python
   if order_type == "market":
       raise GateApiError("close trigger market forbidden; use limit")
   ```
   于是只能靠限价，**成交刚性依赖盘口仍在触发价附近**。

### 失效场景（真钱）
- 暴跌/暴涨插针、流动性消失、周末跳空 → **止损成摆设**，亏损放大到下一笔愿意成交的价位，甚至到强平。

### 根因归类
> **止损成交路径没有「保证出场」语义**，只有「保证挂单」语义。  
> 金融上 SL 的核心是 **exit at any price**；当前实现是 **exit near trigger if book allows**。

---

## 2. P0-2：确认超时重试 → 可能双开、双止损

### 可复现证据
```text
FlakyConfirm：get_order 第一次抛 timeout
→ place_order 被调用 2 次
→ 两张入场单
```

### 为什么会这样
```python
for attempt in (1, 2):
    order = placer()          # 先下单
    check = confirm(order)    # 再回读
    if not confirmed:
        continue              # 再下一次！
```

**下单成功** 与 **回读成功** 不是原子的。  
`get_order` 网络超时时，**单已经在交易所**，但代码当作失败，再下一笔。

### 失效场景
- 入场：2 倍仓位（直接翻倍风险）
- SL：两份 reduce_only 止损；触发后可能对同一仓位挂两次，或一份被拒
- 部分 API 限流时更容易触发

### 根因归类
> **重试策略建立在「失败=没下单」假设上**；实际是「未知=可能已下单」。  
> 正确语义应是：先查本地 client order id / 列表核对，**不能盲目重下**。

---

## 3. P0-3：允许「无止损开仓」——裸仓是合法路径

### 可复现证据
```text
open_long + size + 无 sl
→ ok=True
→ sl_orders = []
```

### 为什么会这样
- `schema`：`open_*` 只强制 size，**不强制 sl**
- `risk.py`：只查 confidence / notional / allow_actions，**无「无 SL 拒单」**
- 策略 prompt 写了「必须 sl」，但那是 **LLM 自律**，不是程序约束

### 失效场景
- LLM 漏写 `sl`、外部 JSON 忘写 `sl`、条件单半成品
- 结果：**有仓无保护**；一旦跳空直奔强平

### 根因归类
> **把安全约束放在提示词而不是 pre-trade 规则。**  
> 交易系统铁律：**未设止损的开仓应在风控层直接拒绝**（或自动补默认止损）。

---

## 4. P1-1：`replace` 先撤旧 SL，新计划失败则「保护真空」

### 现象
```text
replace: symbol  → cancel_all_orders + cancel_all_price_orders
然后才下新单
```

### 为什么会这样
`_apply_replace` 为防「挂单堆积」，在**任何**新 intent 前全撤。  
时间线：

```text
t0  撤掉旧 SL（含在途止损）
t1  下新单失败 / 断网 / 被拒
t2  仓位还在，SL 已无
```

### 失效场景
- LLM 下一轮想「换手」，replace 后下单失败 → **已有仓位突然裸奔**
- 与 P0-2 叠加：replace 后重复下单更乱

### 根因归类
> **取消保护 与 建立新保护 不是事务。**  
> 应用「先立新 SL（或 reduce-only 对锁）再撤旧」或失败自动回滚补 SL。

---

## 5. P1-2：SL 数量按「计划张数」，不按「实际成交」

### 为什么会这样
```python
contracts = usd_to_contracts(...)   # 计划
order = place_order(size=contracts)
sl = place_trigger(size=contracts)  # 仍用计划
```
未读取 `finish_as=filled` / `left` 的真实成交。

### 失效场景
| 情况 | SL 数量 | 后果 |
|------|---------|------|
| 计划 5 张只成交 2 张 | SL 5 张 | reduce_only 多余部分挂不上/触发异常 |
| 加仓后 | 仍是首仓 SL | 加仓部分无保护 |
| 部分强平后 | SL 过大 | 触发后过量 reduce_only 被拒 |

### 根因归类
> **订单生命周期未与持仓生命周期绑定。**  
> 止损应对齐 **position.size**，不是 order.size。

---

## 6. P1-3：限价入场未成交就挂 reduce_only SL

### 现象
`type=limit` 远价挂单 → 仍立刻 `place_price_order` SL，且 `reduce_only=true`。

### 为什么会这样
`_open` 不区分「已成交」与「在途挂单」，一律在 place 后挂 TP/SL。  
`reduce_only` 在 **无仓位** 时可能被拒、或挂成无效单。

### 失效场景
- 入场永不成交 → 孤儿 SL 单
- 入场稍后成交，但 SL 已因 replace/过期没了
- 双向：以为有 SL，实际未覆盖真实持仓

### 根因归类
> **止损应在「持仓确认」之后建立，而不是「下单 API 返回」之后。**

---

## 7. P1-4：`trigger_price_type` 默认 `latest`，与强平用的 mark 不一致

### 为什么会这样
- 默认 `trigger_price_type: latest`（最新价）
- 交易所强平、保证金看 **mark_price**
- 插针（last 瞬时击穿）会触发 SL 后立刻反向

### 失效场景
- 止损被「毛刺」打掉，随后行情回到原方向 → **不必要的亏损成交**
- 若用 mark，则更稳；当前默认偏「灵敏但不稳」

### 根因归类
> **触发基准与风险基准不一致。**  
> 止损的哲学选择必须显式：防强平用 mark，抢单用 latest——**现在没让策略明确选，默认对长线不友好**。

---

## 8. P2：其它会削弱止损的点

| 点 | 为什么成问题 |
|----|----------------|
| `sl_mode: limit_order` | 这是**限价挂单**不是止损：可能立刻成交（变减仓）或永远不到价，**没有「触价必出」** |
| 触发后 initial 仍是 gtc 限价 | 与 P0-1 同源；在剧烈行情里排队 maker |
| 无「止损已触发未成交」监控 | 没人/没程序去追单、改市价、告警 |
| 崩溃于「入场成功、SL 失败」 | 步骤标 fail，但**仓位还在**；无启动对账自动补 SL |
| TP/SL 双腿失败仍可能留下单腿 | `exit_not_placed` 只报错，不自动撤销入场或补另一腿 |
| 多 bot | A 的 replace 可能撤掉 B 在同 symbol 的 SL（若共账户） |

---

## 9. 失效链（为什么比「单点 bug」更危险）

```text
无 SL 开仓（P0-3）
    或
replace 撤旧 SL（P1-1）+ 新 SL 确认超时双开（P0-2）
    ↓
持仓存在、保护缺失/错量
    ↓
行情跳空（P0-1 限价 SL 不成交）
    ↓
亏损扩大 → 强平
```

单独看每个点都像「边缘」；**组合起来是资金安全链路断裂**。

---

## 10. 为什么不是「测过了就没问题」

已有测试证明：
- SL **能挂上**（回读 confirmed）
- 触发单、限价单两种模式可用
- 三腿失败会报 `exit_not_placed`

这些测的是 **「正常路径的挂单成功」**，**没测**：
1. 跳空后是否成交  
2. 回读超时是否双开  
3. 无 SL 是否拒绝  
4. replace 与新 SL 的空窗  
5. 部分成交后的 SL 数量  
6. 触发后未成交是否有追单/告警  

所以「测试全绿」≠「止损在极端行情有效」。

---

## 11. 修复优先级（按「为什么会亏钱」排序）

| 序 | 改什么 | 为什么必须做 |
|----|--------|----------------|
| 1 | **pre-trade 强制 SL**（无 sl → 拒单或自动 ATR 默认止损） | 杜绝合法裸仓 |
| 2 | **SL 保证出场**：触发后市价/更激进限价 + 未成交追单 | 跳空仍能离场 |
| 3 | **幂等下单**（client order id / 先查后重试） | 防双开双 SL |
| 4 | **replace 事务化**（先立保护再撤旧，失败回滚） | 消除保护真空 |
| 5 | SL 数量对齐 **position 实时 size** | 部分成交/加仓正确 |
| 6 | 入场成交确认后再挂 SL；限价在途则等 fill 事件 | 防孤儿 SL |
| 7 | 默认 `trigger_price_type: mark` 或按策略显式配置 | 减少插针误止损 |
| 8 | 「SL 已触发未成交」监控 + 告警 + 自动市价补单 | 闭环 |

---

## 12. 一句话

**止损现在「能挂上」，但不保证「能出场」、不保证「只有一次」、不保证「每笔仓都有」。**  
这三件事在极端行情里就是亏损放大器；原因不在策略，而在 **执行层语义：挂单 ≠ 保护**。
