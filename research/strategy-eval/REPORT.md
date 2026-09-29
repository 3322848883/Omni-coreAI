# 策略评估与模拟盘体系 — 深度调研报告

> 2026-09-28 · gate-signal-bot · 中文  
> 范围：多策略人格模拟盘赛马的**可信评估、风控、排行、运维、上实盘交接**  
> 证据：`findings/F1–F4.md`（行业/学术）+ 代码盘点（本仓库）  
> 模式：standard（4 角度 × ≤6 检索）+ 1 轮代码侧核查

---

## 0. 执行摘要

1. **当前最大的风险不是“不会算 Sharpe”，而是多重检验与样本不足**：15–20 个策略并行试错时，不校正 N（试错次数）的排行榜会系统性奖励噪声 [1][2][7]。
2. **模拟盘引擎主体可信**（撮合/强平/资金费/精度已齐，且已修双记账），但**缺少指标层、告警层、赛马层**——原始 equity/fills 都在，没有算成决策可用的分数。
3. **上实盘应采用 prop-firm 式硬门**：日亏 2–3%、总回撤 5–10%、单笔风险 1%、热管理、回撤阶梯降杠杆 [8][9][10]。
4. **LLM 策略还要加对抗/稳健性轴**：2026 FARSIGHT 综述显示学术 LLM 交易方案 80% 至少一项稳健性不过、100% 有安全面缺口 [14]。
5. **建议把 P2 升级为「策略评估中台」**：指标引擎 + 评分板 + 告警 + 晋级阶梯 + 试错台账（trial ledger），而不只是一个 audit 命令。

---

## 1. 代码侧核查（已有 vs 缺失）

### 1.1 模拟盘引擎能力

| 能力 | 状态 | 备注 |
|------|------|------|
| 撮合（限价/市价/GTC/IOC/FOK/PO） | ✅ | 盘口一价成交；**无深度吃单/滑点** |
| 触发单 TP/SL、强平、资金费、quanto | ✅ | 强平用 last 非 mark；无 ADL |
| 精度 tick/lot/最小名义/价格带/杠杆 | ✅ | |
| 双记账防护 | ✅ 刚修 | claim + 幂等 fill + 文件锁 |
| 孤儿 SL/TP 回收 | ✅ 刚修 | 平仓自动撤 reduce-only |
| 仓位三层护栏 | ✅ 刚修 | 公式钳制 → 权益 50% → max_notional |
| `order_margin` 挂单预留保证金 | ❌ | 恒 0，挂单不占额度 |
| `pnl_snapshot.drawdown` | ❌ 硬编码 0 | 回撤需自 equity 序列算 |
| 持仓时长 / per-trade 归因 | ❌ | fills 可配对但无现成 join |

### 1.2 统计与可观测

| 数据 | 可用性 |
|------|--------|
| equity 时序、fills（价/量/费/realised） | ✅ 充足 |
| thinking / hold / result / ledger | ✅ 全链路有落盘 |
| 指标 CLI（Sharpe/回撤/胜率） | ❌ 无 |
| 告警（alert/webhook） | ❌ 全库为零 |
| plan_cycle ↔ fills 关联键 | ⚠️ 弱 |
| 跨 bot 排行 / 试错台账 N | ❌ 无 |

### 1.3 风险配置全集（可编程）
`max_notional_usd`、`account_risk.{halt, max_total_notional_usd, daily_loss_limit_usd, max_leverage, max_notional_pct}`、`risk_pct/risk_per_trade_pct`、`require_sl`、`position_policy`、`order_scope`、paper 费率/杠杆/mmr 等（见盘点原文）。

---

## 2. 行业与学术结论（按主题）

### 2.1 绩效指标：必须带样本与试错次数
- Sharpe 与显著性的经典关系：t ≈ SR × √T，**样本长度才是显著性杠杆**；短周期上的“高 SR”不可信 [1]。
- **MinBTL / DSR（Bailey–López de Prado）**：N 个独立配置并行搜索时，即使真实 OOS Sharpe=0，样本内也会“冒出”好看的 SR；N=10 时期望 IS Sharpe 可达约 1.57 [2]。**排行榜必须展示 N（试错次数）与 DSR/PSR**，而非裸 Sharpe。
- 偏度/峰度必须进入显著性：负偏+肥尾会虚增 SR，并拉长所需跟踪期（例：观察 SR=2 相对门槛 1，95% 置信约需 2.7–5 年日/月度数据）[2][3]。
- 2026 肥尾研究：当权益分布尾指数约 2.7 时，常规 3–5 年样本对 SR=0.5 几乎无检验力 [4] → **不要用“跑满 3 个月”假装显著**。
- 机构常用补充：**Calmar/MAR = 收益/最大回撤**、Expectancy、Profit Factor、样本量标注；Calmar 细则本轮公开源偏弱 [single source / gap]。

### 2.2 前向 vs 回测：选择偏差是主敌
- WFA 窗口敏感，需 **double-OOS**；BTC 上窗口选择本身可制造假优势 [5]。
- **仅 10bps 成本**即可让朴素小时级信号从盈转亏——成本过滤优先级高于形态优化 [6]。
- **试错台账（trial ledger）+ DSR/PBO** 是必要的；但统计校正抓不到 look-ahead（泄漏型 Sharpe-35 仍能过 DSR）→ 还要**结构防泄漏** [7]。
- **选平台内最优 SR 会失败 OOS**；应选“平台期”（SR ≥ 0.9×SR_opt）并预注册 WFA 门（多数通过 + 灾难否决）[8]。
- 稳健性评分与实盘前向相关性可能为零（ρs≈0.013）→ **paper/OOS 高分 ≠ 实盘必赚** [9]。

### 2.3 风控与资金管理
- Prop-firm 事实标准：**日亏 2–5%、总回撤 5–10%**（FTMO CFD 2-Step 5%/10%；1-Step 3% 日亏 + 10% EOD 追踪）[10]。
- 单笔风险约 **1%**；仓位用**半凯利**（f=k(p−q/b)，k≈0.5）或固定分数 [10][11]。
- **热管理**：组合 Heat = Σ 止损风险敞口；相关组只持一仓 [12]。
- **回撤阶梯**：回撤 3/5/7% 降风险，10% 停新仓；**减仓/降风险订单永不禁止** [13]。
- 仓位应对**回撤底线/cushion** 估算，而非账户余额（追踪回撤会锁在起点）[10]。

### 2.4 产品：评分板与晋级
- QuantConnect 用 **OOS 3 个月 Sharpe** 排序，流程正式化为 research→backtest→paper→production [15]。
- FTMO/Topstep：多阶段硬门（Profit Target、Daily Loss、Max Loss、**Best Day ≤50–55%**、最少交易日），资金**分期释放**（Topstep Live 先放 20%）[16]。
- 权益下降时**自动收紧** DLL 与合约上限 [16]。
- MiFID II Art.17：时间序订单审计（含撤单）、预交易阈值、误操作防护、BCP、持续监控、按需披露策略与控制 [17]。
- FARSIGHT (2026)：LLM 交易需对抗/稳健性审查（信息源投毒、agent 攻击）[14]。
- Topstep Live 当前**限制纯自动化** —— 自动化晋级需自建或选支持 API 的通道 [16]。

---

## 3. 差距矩阵（Gap）

| 能力 | 行业/学术要求 | 本仓库 | 差距等级 |
|------|---------------|--------|----------|
| 指标引擎 | Sharpe/Sortino/Calmar/PF/期望值/MaxDD/持仓时长 | 仅原始数据 | **P0 缺** |
| 试错校正 | DSR/PSR + N 台账 | 无 | **P0 缺**（赛马期最致命） |
| 排行板 | OOS 窗口 Sharpe、样本量、成本后收益 | 无 | **P0 缺** |
| 告警 | 回撤/异常成交/进程死 | 无 | **P1 缺** |
| 热/相关 | Heat≤10%、相关组限一仓 | 仅单 bot 1–2% | **P1 缺** |
| 回撤阶梯 | 3/5/7% 降档、10% 熔断、状态持久 | daily_loss 仅入场检查 | **P1 缺** |
| 成本敏感 | 10bps 级成本过滤 | 费率模型有，无成本后评分 | **P1 缺** |
| 晋级门 | 两阶段、Best Day、最小交易日 | 无 | **P2 产品** |
| 滑点/深度 | 吃单模拟 | TOB 一价 | P2 真实度 |
| LLM 稳健性 | 对抗轴、泄漏防护 | thinking 有、无红队 | P2 |

---

## 4. 完善设计：策略评估中台（升级 P2）

### 4.1 指标引擎（每个 bot / 全局）
```
每日滚动计算并写入 data/bots/<id>/metrics.json + data/metrics/scoreboard.json
```
| 指标 | 口径 |
|------|------|
| Return / CAGR 代理 | equity 起末、按日折算 |
| MaxDD / Calmar | equity 曲线峰谷 |
| Sharpe / Sortino | 日收益；年化 √365 |
| Skew / Kurt / PSR | 四阶矩 |
| DSR | 以 **trial N** 与多策略族校正 [1][2] |
| Win rate / PF / Expectancy | round-trip fills 配对 |
| Avg hold | 开平 fill_time 差 |
| Cost drag | fees / gross PnL |
| Trades N | 样本量一票否决线（如 N&lt;20 不评星级） |

### 4.2 评分板（Scoreboard）
- 列：策略名、Days、Trades N、Return、MaxDD、Sharpe、**DSR**、Calmar、PF、BestDay%、Heat、状态*
- **排序键：DSR 或 OOS 30d Sharpe（成本后）**，禁止裸收益排序 [15][2]
- 样本量徽章：`n&lt;30` 标“探索期”；`30≤n&lt;100` 标“观察期”；`n≥100` 才可“候选上实盘”
- **试错台账** `trials.jsonl`：每次改提示词/参数记一次 N，进 DSR

### 4.3 风控升级
| 项 | 规则（可配） |
|----|----------------|
| 单笔 | risk_pct=1%（公式钳制已上线） |
| 日亏 | 2% 熔断当日停新仓（**持续 tick 检查**，不只入场） |
| 总回撤 | 5% 警告 / 7% 降半仓 / 10% 全停新仓 |
| Heat | Σ|止损风险| ≤ 10% 权益 |
| 相关组 | BTC/ETH 同向只留一腿（可配） |
| Best Day | 日盈利 &gt;50% 权益涨幅日，禁止加杠杆 |

### 4.4 晋级阶梯（Paper → Live）
```
L0 探索期  n<30,  只展示不排名
L1 观察期  n≥30,  DSR>0, MaxDD<10%
L2 候选    n≥100, 成本后 Sharpe>0.5, PF>1.2, 无引擎告警
L3 小实盘  权益≤10U, 同风控参数, 与 paper 并行对账
L4 扩权    实盘 30 日对账偏差<阈值且 MaxDD 达标 → 加额
```
每级人工确认（符合你“审核制”偏好）。

### 4.5 观测与审计（LLM 特有）
- **闭环键**：plan_cycle_id → signal file → fills（补 join）
- **告警**：equity 异常、dup fill、orphan、进程死、LLM 5xx → `alerts.json` + 可选 webhook
- **审计留痕**：thinking 摘要、action 序列、风控拒绝原因（对齐 MiFID 式时间序记录）[17]
- **稳健性抽查**：定期“压力剧本”（跳空、插针、无趋势）跑 plan-only，看人格是否违约

---

## 5. 实施路线图（建议）

| 阶段 | 内容 | 产出 |
|------|------|------|
| **R1 本周** | 指标引擎 + scoreboard JSON/CLI；trials 台账 | `gate_bot paper-score` |
| **R2** | 持续风控（日亏/回撤阶梯/Heat）+ 告警 | tick 级熔断 |
| **R3** | 成本后收益与 DSR 列；样本量徽章 | 可信排行 |
| **R4** | 晋级门 L0–L4 + 与实盘对账脚本 | 上实盘 checklist |
| **R5** | 滑点/深度、相关组 Heat、稳健性红队 | 真实度与 LLM 安全 |

**与 P2 原四项的映射**：2.1 audit → 并入 `paper-score`；2.2 评分板 → §4.2；2.3 配置模板 → 随 R2 统一 `risk_pct` 等；2.4 可信标记 → trials 台账 + reset_at。

---

## 6. Open questions
1. 排行主键用 **DSR** 还是 **OOS 30d Sharpe**？建议 DSR 为主、OOS 为辅。
2. 策略间是否允许“资金联动”（总账户热管理）？现为每 bot 独立 10k。
3. 上实盘门槛的**最少自然日**还是**最少交易数**优先？（LLM 低频人格倾向交易数）
4. 是否引入第三方 prop 规则原文做“对照认证”？（FTMO 规则会变）

---

## 7. Sources（摘录，完整见 findings/）
1. Sharpe, *The Sharpe Ratio*, 1994 — Stanford PDF  
2. Bailey, López de Prado, *Deflated Sharpe / MinBTL* 系列 — papers.ssrn.com  
3. Bailey, López de Prado, *The Probability of Backtest Overfitting* / PSR — SSRN  
4. Fat tails & MinTRL preprint, 2026 — arXiv（见 F1.md）  
5. WFA window sensitivity on Bitcoin — arXiv:2602.10785  
6. Cost-aware hourly BTC evaluation — arXiv:2606.00060  
7. Trial ledger / leaky-oracle DSR — arXiv:2608.27734  
8. IS→purged WFA→OOS 门与排名反转 — arXiv:2603.09219  
9. Robustness ≠ live profit（ρs≈0.013）— arXiv:2608.23808  
10. FTMO Trading Objectives（官方规则页）  
11. Fractional Kelly 实践 — 开源风控库 / 文献（F3.md）  
12. Heat / CorrelationGuard — 开源 risk 库（F3.md）  
13. 回撤阶梯与熔断状态机 — F3.md  
14. FARSIGHT: LLM Trading SoK, arXiv:2609.19705  
15. QuantConnect Algorithm Lab / Strategy Explorer（官方）  
16. Topstep Combine / XFA / Live 规则（官方帮助文档）  
17. MiFID II Article 17（ESMA）  
18. 本仓库代码盘点（paper/executor/strategist，2026-09-28）

> 引用细节、原文摘录见 `research/strategy-eval/findings/F1.md` … `F4.md`。

---

## 8. 对下一步的直接建议
**先做 R1（指标引擎 + 评分板 + 试错台账）**，因为它决定“谁值得继续投入”；风控 R2 紧随。原 P2 四项可直接并入 R1–R3，不必单列。
