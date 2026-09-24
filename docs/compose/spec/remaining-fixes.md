---
feature: remaining-fixes
status: designed
updated: 2026-09-24
branch: master
commits: # 填于交付
---

# 遗留问题修复方案（Remaining Fixes）

## Report

## [S1] Problem

P0/P1（未知名拒绝、MA/MACD/BOLL、新触发）已完成，单测 119 OK。仍有 **功能缺口、代码质量残留、外部依赖限制** 三类未闭环项，需要按优先级修复，避免策略误用与生产隐患。

## [S2] Design

### 2.1 问题清单与定级

| ID | 级别 | 类别 | 问题 | 现状 |
|----|------|------|------|------|
| F1 | **P2** | 功能 | 条件只能 **OR**，无 `all`/`any` 组合 | 缺 |
| F2 | **P2** | 功能 | 无 Stoch/CCI/VWAP（Gate CLI 也没有） | 按需，默认不做 |
| F3 | **P3** | 功能 | 未接 `get-indicator-history` 旁路 | 依赖 Intel 稳 |
| F4 | **P2** | 质量 | `ema/atr/rsi` 公式 API 与 `_fill_*` 双实现，易漂移 | 残留 |
| F5 | **P2** | 质量 | `_fill_rsi_from` 跨空档不算「相对前收变化」，首根 gap 后为 None | 残留 |
| F6 | **P2** | 质量 | `get_account` 成功但 `available=None` 仍继续 plan | 应 abort 或标 degraded |
| F7 | **P2** | 质量 | 未知 **condition type** 只返回 `unknown condition`，不拒绝配置 | 应在 parse 阶段报错 |
| F8 | **P2** | 运维 | `limit_order` 远价 `PRICE_TOO_DEVIATED` 无自动收价 | 仅文档限制 |
| F9 | **P3** | 运维 | trail 追踪单（资金密码/测试网） | **继续搁置** |
| F10 | **P3** | 外部 | Gate Intel TLS 超时（events/macro） | 已重试；不可强修 |
| F11 | **P2** | 杂项 | 工作区 `hist/` 脏目录；`history/` 已 ignore | 需清理 |

### 2.2 修复设计

#### F1 组合条件（`any` / `all`）

```yaml
conditions:
  - type: all                 # 全部满足才触发
    cooldown_sec: 120
    children:
      - {type: rsi, symbol: BTC_USDT, period: 14, op: gt, level: 60}
      - {type: macd_cross, symbol: BTC_USDT, dir: up}
  - type: any                 # 默认行为（兼容现有多条 OR）
    children:
      - {type: price_break, symbol: ETH_USDT, lookback: 20, side: high}
```

- 顶层多条仍是 **OR**；`children` 内按 `type: all|any` 聚合
- `cooldown_sec` 记在组节点；子条件各自求值，失败返回 reason 串
- 深度限制 2，防止配置炸弹

#### F5 RSI 跨 gap

- gap（c 为 None）后：用 **gap 前最近 close** 作 `prev_c` 算 change，而不是跳过整根
- 与 ATR `_fill_atr_from` 的 `prev_c` 策略对齐

#### F6 account 完整性

```text
if available in (None, "") or positions 获取失败 → account.error → run_once abort
```

- `available=None` 不再放行

#### F7 未知 condition 拒绝

- `parse_conditions` 调 `KNOWN_CONDITIONS` 校验；未知 **raise ValueError**
- 与指标 F 行为一致（配置期失败）

#### F8 限价偏离带（可选增强）

- `_place_limit_exit` 收到 `PRICE_TOO_DEVIATED` → 按 `order_price_round` 向盘口收一次价重试
- 仍失败则上抛（不静默改语义）

#### F4 双实现收敛

- 公开 `ema/rsi/atr/sma/macd/boll` 为唯一计算核心
- `_fill_*` 仅负责 warm-start/保库值；核心窗口计算复用公开函数
- 加「公开 API 与 fill 结果一致」对照测试

### 2.3 不做 / 搁置

| 项 | 决定 |
|----|------|
| F2 Stoch/CCI/VWAP | 默认不做，有策略需求再开 |
| F3 Intel 指标旁路 | P3，等通道稳定再评估 |
| F9 trail | 继续搁置（资金密码） |
| F10 Intel TLS | 接受 + 重试已有；不伪装成功 |

### 2.4 测试边界

| 项 | 验收 |
|----|------|
| F1 | `all` 全真才 fire；`any` 一真即 fire；深度>2 拒绝 |
| F5 | 构造 gap 序列，rsi 在 gap 后下一根有值 |
| F6 | `available=None` → `account_unavailable`，不调 LLM |
| F7 | `parse_conditions([{type:zzz}])` 抛错 |
| F8 | mock 一次 `PRICE_TOO_DEVIATED` 后重试成功 |
| F4 | 同序列 `ema()` vs `_fill_ema_from` 数值一致 |
| 回归 | 全量 unittest + `scripts/test_production.py` |

## [S3] Out of Scope

- Gate Intel 服务端稳定性
- trail 资金密码流程
- pa-data-source 采集逻辑改动
- 实盘切换与仓位管理策略

## Tasks

- [ ] T1: F7 未知 condition 配置期拒绝 — acceptance: parse 抛 ValueError；单测 (covers: S2.2)
- [ ] T2: F1 `all`/`any` 组合条件 + 深度限制 — acceptance: 组合单测 + 文档 (covers: S2.2; depends: T1)
- [ ] T3: F5 RSI 跨 gap + F6 account available 校验 — acceptance: gap 单测 + abort 单测 (covers: S2.2)
- [ ] T4: F4 指标 API 收敛 + 一致性对照测试 — acceptance: fill 与纯函数一致 (covers: S2.2)
- [ ] T5: F8 limit_order 偏离带收价重试一次 — acceptance: mock 400 后成功 (covers: S2.2)
- [ ] T6: F11 清理 hist/ 等脏目录 + gitignore — acceptance: git status 干净 (covers: S2.2)
- [ ] T7: README/matrix 更新 + 全量测试 + testnet 生产复跑 — acceptance: 119+ 全绿；prod 24/24 (covers: S2.2, S2.4; depends: T1–T6)
