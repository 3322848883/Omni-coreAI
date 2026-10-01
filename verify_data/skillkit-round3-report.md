# SkillKit 第三轮多角度生产测试报告

> 2026-10-01 · 本地 testnet · 未合并服务器

## 本轮覆盖的 6 个新角度

| # | 角度 | 方法 | 结果 |
|---|------|------|------|
| 1 | **allowed-tools 收窄** | `narrow-tools` fixture 声明白名单 | ✅ 解析为 frozenset |
| 2 | **body 预算截断** | `big-body` fixture（45KB） | ✅ 超限截断 + 标记 |
| 3 | **安全矩阵** | 7 类坏包/逃逸 | ✅ 全部拦截 |
| 4 | **journal 完整性** | 验证 bot_id / truncated 字段 | ✅ 修复生效 |
| 5 | **CLI 全生命周期** | validate→install→list→show→remove | ✅ 往返通过 |
| 6 | **L3 引用读取**（新发现缺口） | `skill_ref` 工具 | ✅ **新增并打通** |

## 本轮发现并修复：L3 渐进披露不可达

**问题**：skill body 明确要求「先读 references/SOUL.md」，但 **bot 的 21 个工具里没有任何文件读取能力** → L3（按需加载捆绑资源）在架构上不可达。渐进披露只有 L1（catalog）+ L2（body）两层真正工作。

**修复**：新增第 22 个工具 `skill_ref(name, path)`
- 只读读取 skill 内 `references/` `scripts/` `assets/` 文件
- **realpath 遏制**：路径必须落在 skill 目录内（`../../../etc/passwd` 被拒）
- 白名单强制（同 skill 工具）
- 预算截断（默认 4000 token）
- 审计 `skill_ref_read`

**验证**（真实 LLM）：
```
skill_activate    references/... body 2479 tokens
skill_ref_read    references/knowledge/theme3_pullbacks.md    truncated=true
skill_ref_read    references/knowledge/theme12_abbreviations.md
skill_ref_read    references/knowledge/theme2_trends.md
skill_ref_read    references/knowledge/theme15_signal_bar_standards.md
skill_ref_read    references/knowledge/theme11_probability.md
```
LLM 产出中 **H2 引用 31 次、L2 20 次、theme 28 次**——真正读了原文。

## 自动化测试明细（17 例，16 OK + 1 skip）

### allowed-tools 收窄
- `narrow-tools` 的 `allowed-tools: "klines indicators ticker"` → frozenset ✅
- `price-action-trading` 无该字段 → None（不收窄）✅

### body 预算
- `big-body`（45KB）body_tokens > 5000 ✅
- `max_body_tokens=100` → 截断 + `[skill body truncated]` ✅
- 正常 body 不截断 ✅

### 安全矩阵
| 用例 | 结果 |
|------|:---:|
| 保留名精确匹配拒绝（`gate`） | ✅ |
| 保留名子串放行（`gatekeeper`） | ✅ |
| 路径逃逸 `../../etc/passwd` | ✅ 拒 |
| XML 注入 `<system>` | ✅ 拒 |
| frontmatter 缺失 | ✅ 拒 |
| frontmatter 未闭合 | ✅ 拒 |
| 非 kebab 目录名 | ✅ 拒 |
| 符号链接逃逸 | ⏭ skip（Windows 需管理员） |

### journal 完整性
- `bot_id` 正确记录（修复前为空）✅
- `truncated` 标志 ✅

### CLI 全生命周期
```
validate → exit 0
install  → skills/cli-test-skill/ + .installed
list     → 显示
show     → 元数据
remove（无 --yes）→ exit 1（拒）
remove --yes → exit 0 + 目录删除
```

### L3 skill_ref
- 读 references/SOUL.md ✅
- 路径逃逸拒绝 ✅
- 文件不存在报错 ✅
- 白名单强制 ✅
- 工具面 22 个 ✅
- 引用截断 ✅

## 回归

| 套件 | 结果 |
|------|------|
| skillkit 三轮合计 | **67 OK**（+1 skip） |
| 全量 | **801 OK**（+1 skip） |

## 累计修复的问题（三轮生产测试）

| 轮次 | 问题 | 严重度 |
|------|------|:---:|
| 1 | catalog 缺使用提示 → LLM 不知道调 skill | 中 |
| 2 | **白名单形同虚设** → `skills:[]` 仍能加载（权限绕过） | **高** |
| 3 | **L3 不可达** → references 无法读取 | 中 |

三个都是**单元测试覆盖不到、只有端到端生产测试才能发现**的问题。

## 当前能力矩阵

| 能力 | 状态 |
|------|:---:|
| L1 catalog 注入（含使用提示） | ✅ |
| L2 skill 工具加载 body | ✅ |
| **L3 skill_ref 读取引用** | ✅ **新增** |
| 白名单强制（runner 注入） | ✅ |
| allowed-tools 收窄 | ✅ 解析（收窄执行待接） |
| body/ref 预算截断 | ✅ |
| 安全矩阵（逃逸/注入/坏包） | ✅ |
| journal 审计 | ✅ |
| CLI 全生命周期 | ✅ |
