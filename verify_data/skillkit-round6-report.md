# SkillKit 第六轮生产测试报告

> 2026-10-01 · 本地 testnet · 未合并服务器

## 本轮覆盖的 6 个角度

| # | 角度 | 结果 |
|---|------|------|
| 1 | **收窄状态跨 cycle 隔离** | ✅ 不泄漏（局部变量） |
| 2 | **跨 skill 越权读取** | ✅ 被拒 |
| 3 | **catalog 50+ 压测** | ✅ 预算裁剪 + 顺序稳定 |
| 4 | **目录名 vs frontmatter 名不一致** | ✅ 警告但可加载 |
| 5 | **权限/坏文件边界** | ⚠️ 发现 bug → 已修 |
| 6 | **journal 增长（200 条）** | ✅ 可读、频次统计正确 |

## 本轮发现并修复的两个 Bug

### Bug 6.1：坏 UTF-8 文件导致 scan 崩溃（健壮性）

**现象**：skills 目录里出现一个非 UTF-8 的 SKILL.md → `registry.scan()` 抛 `UnicodeDecodeError` **未捕获**，直接崩掉 bot 启动。

**根因**：`load_package` 用 `read_text(encoding="utf-8")`，编码错误抛 `UnicodeDecodeError`（ValueError 子类），而 `registry.scan` 只 `except SkillError`。

**修复**：
- `load_package` 把 `UnicodeDecodeError`/`OSError` 统一转成 `SkillError`
- `registry.scan` 捕获 `(SkillError, UnicodeDecodeError, OSError, ValueError)` → 坏包跳过不崩
- `validate_package` 同样覆盖 `UnicodeDecodeError`

**影响**：一个损坏的 skill 文件不再能让整个 bot 起不来。

### Bug 6.2：单次 `plan` 遇 LLM 网络错误抛裸 traceback

**现象**：`python -m omnialpha plan --bot X` 在 LLM 请求失败（SSL EOF / 超时）时打印完整 traceback 崩溃；而 `plan-loop` 有 `except LLMError` 兜底，两者行为不一致。

**根因**：`run_once`（单次）的 `self._chat_with_tools(...)` 调用**未包裹异常处理**。

**修复**：`run_once` 加 `except LLMError` → 返回 `{"ok": false, "error": "llm_unavailable", "detail": ...}`，与 `plan-loop` 一致。

**验证**：本轮真实运行中遇到 Gate testnet SSL 抖动，修复后优雅降级（`ok: false` + 错误详情），不再崩。

## 自动化测试明细（13 例，全过）

### 收窄状态隔离
- `active_tools` 是局部变量（源码断言无 `self.active_tools`）✅
- FakeLLM 双轮：第 1 轮全工具面、第 2 轮已收窄 ✅

### 跨 skill 越权
- 启用 test-helper，读 price-action-trading 的文件 → 拒 ✅
- 读自己启用的 skill → 允许 ✅
- `model-invocation:false` 的 skill → 拒 ✅

### catalog 50+ 压测
- 50 skill × 紧预算（100 token）→ 裁剪且 >0 ✅
- 高频 skill 在裁剪中保留 ✅
- catalog 渲染长度受限 ✅
- 大预算 → 全保留 50 ✅
- 渲染顺序稳定 + 按 id 升序 ✅

### 目录名不一致
- `dirname-x` 目录含 `name: frontmatter-y` → W01 警告 + 可加载，以 frontmatter 为准 ✅

### 权限/坏文件
- 不存在的 root → 空列表不崩 ✅
- **坏 UTF-8 SKILL.md → 跳过不崩** ✅（本轮修复）
- root 是文件而非目录 → 空列表 ✅

### journal 增长
- 200 次激活 → 200 行可读 ✅
- `read_activation_freq` 统计正确（200）✅

### 并发进程
- 4 进程同时扫描同一 skills 目录 → 全部成功 ✅

## 回归

| 套件 | 结果 |
|------|------|
| skillkit 六轮合计 | **113 OK**（+1 skip） |
| 全量 | **847 OK**（+1 skip） |

## 六轮累计修复

| 轮次 | 问题 | 严重度 | 类型 |
|------|------|:---:|------|
| 1 | catalog 缺使用提示 | 中 | 集成 |
| 2 | **白名单形同虚设** | **高** | 权限 |
| 3 | **L3 不可达** | 中 | 架构 |
| 4 | **allowed-tools 未执行** | 中 | 集成 |
| 5 | 空 frontmatter 误判 | 低 | 解析 |
| 5 | **并发 journal 丢写** | **高** | 数据完整性 |
| 6 | **坏 UTF-8 崩 scan** | 中 | 健壮性 |
| 6 | 单次 plan 裸 traceback | 中 | 健壮性 |

**八轮修复中，5 个是「单测绿但集成层断」，3 个是并发/边界/健壮性。**

## 当前完整能力矩阵

| 能力 | 状态 |
|------|:---:|
| L1 catalog（含使用提示） | ✅ |
| L2 skill 工具 | ✅ |
| L3 skill_ref | ✅ |
| 白名单强制（runner 注入） | ✅ |
| allowed-tools 运行时收窄 | ✅ |
| 收窄状态跨 cycle 隔离 | ✅ |
| 跨 skill 越权防护 | ✅ |
| 预算截断（body/ref/catalog） | ✅ |
| 安全矩阵（逃逸/注入/坏包/符号链接） | ✅ |
| 同名冲突优先级 | ✅ |
| 嵌套发现（深度限制） | ✅ |
| frontmatter 边界（空块/CRLF/emoji/中文） | ✅ |
| 坏 UTF-8 容错 | ✅ |
| 并发 journal（线程 + 跨进程） | ✅ |
| LLM 错误优雅降级 | ✅ |
| 热更新 | ✅ |
| CLI 全生命周期 | ✅ |
