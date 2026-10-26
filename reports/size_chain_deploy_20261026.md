# 尺寸链收尾：四口径统一入口 + 部署前验证

日期：2026-10-26 17:30（UTC+8） · 分支 `fix/size-chain`（`f6bf0ca`，基点 `9d026e8`）
适用：服务器线 `/opt/omnialpha`（**不是**本地 `feat/fractional-sizing` 那条线）

---

## 一、结论先说

| 项 | 结果 |
|---|---|
| 本地全量 | 2575 项 = 12 fail + 6 error，与改动前**同一批 18 个**（逐字相同） |
| 服务器环境（生产快照 + 10 文件） | 2510 项 = 29 fail + 24 error；对照「生产原样 2470 项 = 29 fail + 24 error」→ **仅 mine=0 / 仅 base=0** |
| 新增测试 | 本地 `test_size_resolve` 26 条 + `test_fractional_position` 14 条 = **40 条全绿**（服务器 Python 3.12 上同样 40 条全绿） |
| 实网只读核实 | 6 币种 × 四口径 **90 PASS / 0 FAIL**（本地 + 服务器两处各跑一次） |

→ **可以部署，零回归。** 未部署：等生产窗口 + 你点头。

---

## 二、修了什么（四层，逐层都能单独致命）

### 层 1 · 策略层：模型写的 0.7 张，进来就变 0

`omnialpha/strategist/schema.py` 的 `Chip.size: Optional[int]` + `_int()` 解析：

| 模型写 | 旧行为 |
|---|---|
| `size: 0.7` | `int(0.7)` → **0**（静默，等于"没给量"） |
| `size: "0.7"` | `int("0.7")` → `PlanError` → **整轮计划作废** |

改：`Chip.size → float`，新增 `_qty()`（保留小数；坏输入仍抛 `PlanError`；NaN/inf 也抛）。
`test_chip_size_accepts_fraction` 在改前实测得 **0.0** —— 这就是 8.2 小时空转的入口之一。

### 层 2 · 执行层：`int(intent.size)` 后再被"抬到 1 张"放大 41%

`_open` / `_stop_entry` 各自抄了一份同样的解析代码：

```python
if intent.size is not None:
    contracts = int(intent.size)        # 0.7 → 0
else:
    contracts = usd_to_contracts(...)   # 0 → 被兜底抬到 1 张
```

改：新增 `Executor._resolve_order_size(intent, meta, entry_hint) -> (张数, 名义, 口径)`，
两个入口全改调它。规则：

| 输入 | 行为 |
|---|---|
| `size=0.7` | 0.7 张（绝对值；方向由 action 决定，不靠符号） |
| `size=0.15`（ETH step 0.1） | 0.1 张（floor） |
| `size=0` | `SIZE_INVALID` 拒单 |
| `size=0.05`（< 一个步长） | **`SIZE_TOO_SMALL` 拒单** —— 抬到 0.1 等于放大 2 倍，不能静默 |
| `size_usd=17` | 0.7 张（`floor(17 / 23.25)`） |
| `size_usd=1`（< 一个步长 2.325U） | 抬到 `size_min` = **0.1 张**（原来抬到 1 张 = 23.25U，差 10 倍） |
| 同时给 `size` + `size_usd` | `SIZE_CONFLICT` 拒单（原来静默 size 优先，另一个口径白写） |
| 一个都不给 | `SIZE_REQUIRED` 拒单 |
| `size_pct` / `margin_pct` | 走 `get_available_usdt()`（`margin_pct` × 杠杆） |

### 层 3 · 步长：代码根本不知道 ETH 步长是 0.1

`usd_to_contracts` 原来用 `meta.order_size_round` 当步长 —— **交易所不提供这个字段**
（1061 个合约 0 命中，恒 1 是假值），于是"张数只能整数"写死在算法里。

改：`usd_to_contracts → float`，走 `safe_step(meta)`（`enable_decimal → 0.1`），
夹 `size_min` / `size_max`。**返回值类型从 int 变 float** —— 所有调用方都按数值用，
既有测试 `== 20` 对 `20.0` 仍成立。

### 层 4 · 出场腿：TP/SL 拆腿也按整数算

`_tp_legs` 原全程整数：`int(exit_size)` / `int(round(exit_size * share))` / `max(1, ...)`。
持仓 0.7 张拆两档 → 要么 0 张（挂一条 0 张 reduce-only），要么 1 张（超平被拒）。

改：**按步长做整数单位运算，最后乘回 step** —— step=1 时与旧行为逐字一致（BTC 6 张三档仍 2/2/2）。
连带 `exit_size`、`planned`、`_place_trigger(size=)`、`sl_size_override` 全改 float。

### 层 5 · `ensure_grid_tp`（服务器线独有的档位条件限价 TP）

三处 `_int_or_zero(size)`：

| 位置 | 0.7 张时的后果 |
|---|---|
| 多头持仓合计 | 读成 **0** → 误判"无多头持仓" → **撤掉在盘 TP** 并拒绝挂新 |
| 在盘 `-gtp` 已覆盖 | 恒 **0** → 每轮重挂 → 累积超配 → 交易所拒 |
| 候选档张数 `or 1` | 0.3 张档 → **0** → 整档挂不上 |

改：`_num` / `qty` + `_gtp_step` 兜底。

### 顺带挖出两个真 bug（不是本轮目标，但同族）

1. **`_rollback_unprotected_entry` 的 `int("0.7")` 抛 ValueError** —— 这是「入场单挂上了、保护单挂不上」的**紧急回滚路径**。回滚自己崩掉 = 裸仓挂单留在场上。触发条件：小数张 + 保护单挂失败。
2. **成交张数 `filled = abs(int(size)) - abs(int(left))`** —— 0.7 张成交读成 0，影响 `hang_mode: simultaneous` 的后续判断。

---

## 三、验证矩阵

| 验证 | 环境 | 结果 |
|---|---|---|
| `tests/test_size_resolve.py`（26 条，先红后绿） | 本地 Windows / 服务器 3.12 | 全绿 |
| `tests/test_fractional_position.py`（14 条） | 同上 | 全绿 |
| `scripts/_verify_size_resolve.py`（只读公开接口） | 本地直连 Gate | **90 PASS / 0 FAIL** |
| 同上 | 生产快照 staging | **90 PASS / 0 FAIL** |
| 本地全量 2575 项 | 本地 | 12F+6E，与改动前**同一批 18 个** |
| 生产快照 + 10 文件 2510 项 | `/opt/omnialpha_stg2` | 29F+24E；**仅 mine=0 / 仅 base=0** |

实网数值（`size_usd=17`，真实合约规格）：

| | ETH | BTC | SOL | XRP | DOGE | PEPE |
|---|---|---|---|---|---|---|
| 张数 | 0.7 | 2 | 0.1 | 1.3 | 22 | 0.3 |
| 名义 | 16.3U | 16.0U | 17.0U | 16.8U | 17.0U | 13.3U |

---

## 四、部署清单（10 个文件，**禁止整文件覆盖以外的粗暴同步**）

本次改动依赖 `ebc885e`（规格字段），两个提交必须一起上——单独上 `executor` 会在
`ContractMeta(size_min=...)` 上直接 TypeError（**staging 实测复现过：23 errors**）。

| # | 文件 | 内容 | 来自 |
|---|---|---|---|
| 1 | `omnialpha/gate_client.py` | `ContractMeta` 规格字段 + `size_step` + `X-Gate-Size-Decimal` + 缓存 v2 | `ebc885e` |
| 2 | `omnialpha/exchanges/gate.py` | `public_get(extra_headers=)` 包装签名 | `ebc885e` |
| 3 | `omnialpha/sizing.py` | `safe_step` / `quantize_size` / `usd_to_contracts → float` | `ebc885e` + `f6bf0ca` |
| 4 | `omnialpha/schema.py` | `Intent` 四个尺寸字段 → float + `_q()` | `f6bf0ca` |
| 5 | `omnialpha/strategist/schema.py` | `Chip.size → float` + `_qty()` | `f6bf0ca` |
| 6 | `omnialpha/executor.py` | `_resolve_order_size` / `_tp_legs` 步长 / `_fmt_qty` / 读取侧 float / `ensure_grid_tp` | 全部 |
| 7 | `omnialpha/reconcile.py` | `_protector_size` / 待成交张数 / `gap` → float | `5ac2feb` |
| 8 | `omnialpha/watcher.py` | 裸仓判定 → float | `5ac2feb` |
| 9 | `omnialpha/strategist/snapshot.py` | positions 读取 → float（0.7 张会让整段丢失） | `5ac2feb` |
| 10 | `omnialpha/strategist/tools.py` | 持仓工具 → float | `5ac2feb` |

### 部署步骤

```bash
# 1) 备份
cd /opt/omnialpha && tar czf _bak_sizechain_$(date +%Y%m%d%H%M).tar.gz \
    omnialpha/{executor.py,schema.py,sizing.py,gate_client.py,reconcile.py,watcher.py} \
    omnialpha/exchanges/gate.py omnialpha/strategist/{schema.py,snapshot.py,tools.py}

# 2) 上传 10 个文件（scp，逐文件 md5 校验）
# 3) 编译 + 单测门槛
.venv/bin/python -m py_compile <10 个文件>
cd tests && PYTHONPATH=/opt/omnialpha .venv/bin/python -m unittest \
    test_size_resolve test_fractional_position test_plan_universe_gate test_strategist

# 4) 重启（supervisor 5s 自动拉起）
kill <plan-loop PIDs>

# 5) 盯一轮：必须看到 ok: True 且 orders 正常
grep "'ok': True" data/bots/<id>/logs/plan.err | tail -1
```

### 回滚

```bash
cd /opt/omnialpha && tar xzf _bak_sizechain_*.tar.gz && kill <plan-loop PIDs>
```

---

## 五、风险与未覆盖

| 风险 | 说明 | 处置 |
|---|---|---|
| 🟠 **行为变化：名义路径抬到 `size_min` 而非 1 张** | 原来小账户会下 1 张（ETH 23.25U），现在下 0.1 张（2.3U）。**变小**是安全方向，但会让"最小仓位"策略失效 | 明确记录；若 bot 依赖"至少 1 张"，由人格显式写 `size: 1` |
| 🟠 **行为变化：多口径同给从"静默 size 优先"变拒单** | 若现有人格同时写 `size` + `size_usd`，会整条 chip 失败 | ✅ **已核**：两个实盘 bot 最近信号的量口径见下表，**全部只给一个口径**，不会误伤 |
| 🟡 `exchanges/{binance,bitget,bybit,okx,hyperliquid}.py` 的 `int(size)` 未改 | 本项目只跑 Gate，但多所适配器同一族缺陷 | 明确列为未做（非本分支范围） |
| 🟡 `paper/validate.py` 仍用 `order_size_round` 当步长 | paper 的 ETH 仍是整数张，与 live 不一致 → 回测结论偏保守 | 待做：paper 也走 `safe_step` |
| 🟡 名义额闸门只查 `size_usd` | `size`（张数）路径绕过 `max_notional_pct` | 待确认后再改（会改变线上行为的闸门，不本轮做） |

---

## 六、上线后的行为变化（用现网真实信号预演）

生产 `archive/done/` 里最近信号的量口径（**实测**）：

| bot | action | 口径 | 值 |
|---|---|---|---|
| martin-grid | `open_long` | `size_usd` | 26.0 / 25.5 |
| pa-eth-v5 | `modify_tp_sl` / `cancel_all` | 无（不需要量） | — |

→ 两个实盘 bot **从不混给口径**，`SIZE_CONFLICT` 不会误伤现网。

**张数预演**（ETH 2325，1 张 = 23.25U）：

| 输入 | 旧 | 新 | 差异 |
|---|---|---|---|
| `size_usd: 26.0` | 1 张 = 23.25U（少 11%） | **1.1 张 = 25.6U**（少 1.5%） | 更贴近模型意图 |
| `size_usd: 25.5` | 1 张 = 23.25U | 1.0 张 = 23.25U | 不变 |
| `size_usd: 1.0`（小账户） | 1 张 = 23.25U（放大 23×） | 0.1 张 = 2.3U | **风险降一个数量级** |

---

## 七、现场顺带发现（**不是本次改动引起**，已核）

| 现象 | 事实 | 判断 |
|---|---|---|
| `pa-eth-v5` 今天 `archive/failed/` 2 条 | 08:53 / 08:57 两条 `TRIGGER_PRICE_SIDE: sl=2329.6 需 < mark 2328.93（rule=2）` —— 做多止损价给在现价**上方** | 模型给的价格方向错，`_precheck_exit_triggers` 正确拦下；今天 done 36 / failed 2，**不是连续失败** |
| `cycle_id` 是未来日期 | `2026-10-30T06:00-cycle`（今天是 10-26） | kline 时间轴错位，只影响命名/去重键，不影响下单；**待单独排查** |
| `plan.err` 尾部有 `symbols` TypeError | 08:00Z 之后 **0 条**（历史行） | 15 小时事故已修复且未复发 |

---

## 八、提交

| 提交 | 内容 |
|---|---|
| `5ac2feb` | 读取侧小数张（executor 8 / reconcile 5 / watcher 2 / gate_client 4 / snapshot 1 / tools 1） |
| `ebc885e` | 合约规格字段 + `safe_step`/`quantize_size` + 缓存 v2 |
| `4ea6b77` | TP 腿按步长拆分 |
| `f6bf0ca` | **四口径统一入口 + `_tp_legs` 步长 + `ensure_grid_tp` + 回滚路径** |

分支指针：`fix/size-chain` = `f6bf0ca`（基线 `9d026e8`，与对方的告警提交 `4ad3732`/`577d681` 零文件交叠）。
