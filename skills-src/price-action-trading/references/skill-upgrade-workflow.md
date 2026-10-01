# Price Action Trading Skill — ZIP 升级工作流

> 从 v29→v30→v32 的实际升级经验总结。每次 ZIP 升级时参考。

## 升级流程（6 步）

```
1. 备份当前目录 → Desktop\price-action-trading-backup-vXX\
2. 保存自定义文件内容（Python dict: name → content）
3. 解压 ZIP 覆盖
4. 恢复自定义文件（references/SOUL.md + references/*.md）
5. 重新插入 SKILL.md 自定义段落
6. 验证：功能一出现1次、文件配合使用说明1次、Pitfall 1次、Step 0 次数不变
```

## 自定义文件清单（不在 ZIP 中）

| 文件 | 用途 | 升级后必须恢复 |
|------|------|:-:|
| `references/SOUL.md` | AI 角色定义 + 步骤映射 + 配合流程图 | ✓ |
| `references/account-watcher-data-source.md` | 数据源参考 | ✓ |
| `references/account-watcher-db.md` | 数据库参考 | ✓ |
| `references/market-analysis-workflow.md` | 行情分析流程参考 | ✓ |
| `references/multi-timeframe-analysis.md` | 多时间框架分析参考 | ✓ |

## SKILL.md 自定义段落（需重新插入）

### 1. 文件配合使用说明（必读）

- **插入位置**：`> 策略层参考：[references/knowledge/strategy_workflow.md]` 之前
- **标记**：以 `#### 文件配合使用说明（必读）` 开头

### 2. Pitfall 配合说明

- **插入位置**：`#### Step 0：记忆检索（核心步骤）` 之前
- **标记**：以 `### ⚠️ 关键 Pitfall` 开头

## 关键 Pitfall

### Pitfall 1：段落提取边界错误

**问题**：从旧 SKILL.md 提取 Pitfall 段落时，如果用 `\n## ` 作为结束边界，会匹配到 `## 模板文件索引` 或下一个 `## 功能一`，导致提取 ~29K 字符的重复内容。

**症状**：升级后 `功能一` 出现 2 次，`Step 0` 出现次数异常。

**正确做法**：用 `\n#### Step 0：记忆检索` 作为精确结束标记，只提取 Pitfall 标题到 Step 0 之间的内容（~4K 字符）。

### Pitfall 2：版本号双引号

**问题**：手动替换版本号时容易产生 `version: "32.0""`（多余引号），导致 YAML 解析失败。

**正确做法**：替换时精确匹配 `version: "29.0"` → `version: "32.0"`，注意引号配对。

### Pitfall 3：文件对比方法

**问题**：Windows 上 `read_file` 工具对 CRLF 文件返回 0 行，无法正常读取。

**正确做法**：用 `execute_code` + Python `open(path, 'r', encoding='utf-8')` 读取。文件对比用 `hashlib.md5` 逐文件比对。

## 自定义内容同步（新版本新增章节时）

当 ZIP 升级了 workflow.md 或 strategy_workflow.md（新增章节），需要同步更新：

1. **references/SOUL.md**：更新以下 3 处
   - 决策引擎索引（知识库文件索引 section）
   - 配合流程图（workflow×strategy_workflow section）
   - 步骤→主题文件映射表

2. **SKILL.md**：更新以下 1 处
   - 文件配合使用说明中的映射表

3. **references/SOUL.md 同步规则**：新章节在流程图和映射表中都要出现（通常 2-3 次引用）
