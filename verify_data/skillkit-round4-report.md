# SkillKit 第四轮生产测试报告

> 2026-10-01 · 本地 testnet · 未合并服务器

## 本轮覆盖的 5 个角度

| # | 角度 | 方法 | 结果 |
|---|------|------|------|
| 1 | **allowed-tools 运行时收窄**（新发现缺口） | 真实 LLM + `narrow-tools` | ✅ **已修并验证** |
| 2 | **并发多 bot 隔离** | 两个 bot 不同白名单 | ✅ 互不影响 |
| 3 | **热更新** | 动态增删 skill 目录后重扫描 | ✅ 即时可见/消失 |
| 4 | **长跑 journal** | 连续 5 次激活 | ✅ 只追加、可解析 |
| 5 | **catalog 隔离** | 不同 bot 的 catalog 渲染 | ✅ 各自独立 |

## 本轮发现并修复：allowed-tools 只解析未执行

**问题**：`allowed-tools` 字段能正确解析为 `frozenset`，但 **运行时从未收窄工具面**——声明形同虚设（grep 全代码库无任何执行点）。

**修复**（`gate_bot/strategist/loop.py`）：
- `_skill_allowed_tools(skill_id)` 读取 skill 声明的白名单
- `_chat_native_tools` 在 skill 成功加载后，把后续轮次的 `tools=` 收窄为 `allowed ∪ {skill, skill_ref}`
- 元工具（skill / skill_ref）始终保留，否则 L3 会断

**真实 LLM 验证**（`narrow-tools` 声明 `allowed-tools: klines indicators ticker`）：

LLM thinking 原文：
> "**Now the tool surface is narrowed to klines, indicators, ticker.** Let me try to fetch market data."

随后它只调用了 `klines` / `indicators` / `ticker`——收窄真实生效。

## 自动化测试明细（9 例，全过）

### allowed-tools 收窄
- `narrow-tools` meta 解析为 frozenset ✅
- 收窄数学：`allowed ∪ {skill, skill_ref}`，不含 smc_map/orderbook ✅
- `_skill_allowed_tools` 对声明 skill 返回集合 ✅
- 对未声明 skill 返回 None ✅

### 并发多 bot 隔离
| bot | 白名单 | 加载 test-helper | 加载 price-action-trading |
|-----|--------|:---:|:---:|
| bot-a | [test-helper] | ✅ 允许 | ❌ 拒 |
| bot-b | [price-action-trading] | ❌ 拒 | ✅ 允许 |

- catalog 隔离：bot-a 的 catalog 不含 price-action-trading ✅

### 热更新
- 空目录 → 装 hot-skill → 重扫描可见 ✅
- 删除 gone-skill → 重扫描消失 ✅

### 长跑 journal
- 连续 5 次激活 → 行数 +5 ✅
- 每行 JSON 可解析，含 ts / skill_id ✅

## 回归

| 套件 | 结果 |
|------|------|
| skillkit 四轮合计 | **76 OK**（+1 skip） |
| 全量 | **810 OK**（+1 skip） |

## 四轮累计修复的问题

| 轮次 | 问题 | 严重度 | 影响 |
|------|------|:---:|------|
| 1 | catalog 缺使用提示 | 中 | LLM 不知道调 skill |
| 2 | **白名单形同虚设** | **高** | `skills:[]` 仍能加载（权限绕过） |
| 3 | **L3 不可达** | 中 | references 读不到，渐进披露缺一层 |
| 4 | **allowed-tools 未执行** | 中 | 声明的工具收窄无效果 |

**共同点**：全是「声明/文档说支持，但运行时没接线」的集成层缺陷——单元测试各自通过，只有端到端生产测试才暴露。

## 当前完整能力矩阵

| 能力 | 状态 |
|------|:---:|
| L1 catalog 注入（含使用提示） | ✅ |
| L2 skill 工具加载 body | ✅ |
| L3 skill_ref 读取引用 | ✅ |
| 白名单强制（runner 注入，不信 LLM） | ✅ |
| **allowed-tools 运行时收窄** | ✅ **本轮新增** |
| body/ref 预算截断 | ✅ |
| 安全矩阵（逃逸/注入/坏包/符号链接） | ✅ |
| 并发多 bot 隔离 | ✅ |
| 热更新（增删即时生效） | ✅ |
| journal 审计（bot_id/path/truncated） | ✅ |
| CLI 全生命周期 | ✅ |

## 待清理（合并服务器前）

测试夹具 skill 需决定去留：
- `test-helper`、`sentinel-risk`、`narrow-tools`、`big-body`（45KB）— 纯测试用
- 测试 bot 配置：`skill-e2e.yaml`、`skill-ab.yaml`
- 测试 prompt：`skill_e2e_*.md`（7 个）
