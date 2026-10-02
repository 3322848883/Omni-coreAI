# SkillKit 合并 + 服务器同步 + 生产测试报告

> 2026-10-01 · 本地 master → GitHub → 服务器 `/opt/omnialpha`

## 一、合并（本地 → GitHub）

**4 个提交**（本地 master）：

| commit | 内容 |
|--------|------|
| `5d6f6a9` | **feat(skillkit)**: 可安装 skill 引擎（L1/L2/L3）+ 8 个生产测试 bug 修复 |
| `b984760` | chore(discussion): discussion_log 落盘 + disc-trio 组 + 多 bot 工具启用 |
| `f1c2b2d` | chore(scripts): dev/运维脚本归档 |
| `5c27985` | docs(skillkit): 七轮生产测试与验证报告归档 + skills/.gitkeep |

**推送**：`git push origin master` → `f1c2b2d..5c27985` ✅

**清理**：
- 测试夹具移出生产 `skills/` → `tests/fixtures/skills/`（4 个 fixture）
- `.gitignore` 增加 `skills/*`（运行时安装目录）、`scripts/_*.txt|json`（脚本产物）
- 排除密钥处理脚本 `_export_gate_keys.py`

## 二、文档更新

| 文档 | 更新 |
|------|------|
| `AGENTS.md` | 工具 20→**22**；SkillKit 三级披露表 + CLI + 红线 |
| `README.md` | 工具 22 / 指标 25 族；SkillKit 章节 |
| `docs/HOWTO-add-skill.md` | 新建：目录契约 / frontmatter / 安装启用 / 运行时 / 红线 |
| `docs/compose/spec/skillkit.md` | status: planned → **delivered**；Report + Bug 修复记录（8 项） |
| `research/agent-skill-systems/` | 调研报告（50 来源 + MiMo 实地解剖） |

## 三、服务器同步

**服务器**：`/opt/omnialpha`（生产部署，origin 同 GitHub）

| 步骤 | 结果 |
|------|------|
| 备份 | `/opt/backups/omnialpha-skillkit/{config,omnialpha}` |
| stash 服务端本地改动 | 17 个 paper bot 的 `enabled: false` |
| `git pull origin master` | `d0ba667` → **`5c27985`** ✅ |
| stash pop | 恢复 paper bot 禁用设置 ✅ |
| 测试/paper bot 禁用 | pa-a/b/c + skill-ab/e2e/real → `enabled: false` |
| **仅 brooks-btc 启用** | ✅ |
| skill 上传 + 安装 | `/opt/skill-pkg/` → `skill install` → **PASS**（1 W05 warning） |
| 服务器测试套件 | **862 OK** ✅ |

**skill 安装校验输出**：
```
PASS  errors=0 warnings=1
  WARN  W05 SKILL.md references 'references/knowledge/source/V1_趋势篇' but it does not exist
installed: /opt/omnialpha/skills/price-action-trading
ID                           VER    TOK   MODEL  DESC
price-action-trading         34.2   2480  yes    Al Brooks 价格行为交易辅助…
```

> W05 是 warning 非 error：zip 包未含 `references/knowledge/source/` 子目录（原技能的原文分卷），不影响核心方法论使用。

## 四、重启实盘

`omnialpha-watchdog.service` 管理 brooks-btc（watchdog 拉起 plan-loop + run）。

```
重启前: PID 1739456(watchdog) 1739458(plan) 1739459(run)
重启后: PID 2352991(watchdog) 2352995(plan) 2352996(run)
日志: 看门狗已启动，接管 1 个 bot / 2 个组件
```

## 五、服务器生产测试（实盘 bot 真实使用 skill）

**关键证据**：重启后实盘 `brooks-btc` 的首个 cycle 就加载了 skill。

`logs/skill_journal.jsonl`：
```json
{"kind": "skill_activate", "bot_id": "brooks-btc", "skill_id": "price-action-trading",
 "body_tokens": 2479, "truncated": false, "ts": "2026-10-01T13:07:51Z"}
```

**LLM 使用质量**（thinking 36,094 字符，3 轮）：
```
thinking 开头: "Let me analyze the market. Let me load the price action skill and gather data."
术语命中: climax ×6   range ×133   breakout ×8   pullback ×7   Brooks ×7   barbwire ×1   BAN ×1
```

**产出**：
```json
{"cycle_id": "btc-15m-20251001-pa",
 "reasoning": "区域=区间震荡，价处中部无信号，观望等边界",
 "chips": [{"action": "hold", "symbol": "BTC_USDT", "confidence": 0.5}]}
```

**实盘健康**：
```
进程: watchdog 2352991 / plan 2352995 / run 2352996  全部运行
last_cycle: btc-pa-15m-001 @ 13:08:51
health: error_streak=0
```

## 六、验证结论

| 项 | 结果 |
|----|:---:|
| 本地 4 提交 + 推送 GitHub | ✅ |
| 文档更新（4 处 + 新建 1） | ✅ |
| 服务器 pull 到 5c27985 | ✅ |
| 服务器测试 862 OK | ✅ |
| skill 服务器安装 | ✅ |
| 实盘 bot 真实加载 skill | ✅ |
| 实盘 bot 采用 Brooks 方法论 | ✅ |
| 实盘健康（error_streak=0） | ✅ |
| 仅 brooks-btc 启用（无多余 bot） | ✅ |

## 七、同步后修复的两个问题

### 7.1 原文分卷目录名乱码（已修）

**现象**：服务器 `skill validate` 报 `W05 references/knowledge/source/V1_趋势篇 but it does not exist`。

**排查**：zip 里**有** source 目录（27 条），但服务器上的目录名是乱码：
```
期望  V1_趋势篇  → utf-8 字节 b'V1_\xe8\xb6\x8b\xe5\x8a\xbf\xe7\xaf\x87'
实际  V1_ш╢ЛхК┐чпЗ → b'V1_\xd1\x88\xe2\x95\xa2...'
```
**根因**：服务器 `unzip` 把 UTF-8 文件名按 CP866 解读（zip 未带 UTF-8 标志位），产生二次编码乱码。

**影响**：`references/knowledge/source/` 下 27 篇原文分卷（V1 趋势篇 / V2 区间 / V3 反转）**无法通过 `skill_ref` 读取**；17 个 theme*.md（ASCII 名）不受影响，核心方法论仍可用。

**修复**：`name.encode('cp866').decode('utf-8')` 逆向还原 → 重命名 3 个目录（各 9 文件）。

**验证**：
```
validate: PASS  errors=0 warnings=0
skill_ref 读 V1/V2/V3 首卷 → 全部 OK（各 ~12,196 字符）
bundled 文件数: 100
```

### 7.2 实盘 bot 的 skill 可见性（待你决策）

`config/bots/brooks-btc.yaml` **没有 `skills:` 键**。按引擎语义「缺省 = 全部可见」，实盘 bot 自动看到并使用了 skill（journal 已证）。

**影响**：
- 好处：无需配置即可用
- 风险：**今后新装的任何 skill 都会立即对实盘 bot 可见**，没有 opt-in 闸门

**建议**（二选一）：
```yaml
skills: [price-action-trading]   # 显式白名单，只允许这一个
# 或
skills: []                       # 关闭 skill，实盘不加载任何 skill
```

## 八、备份

`/opt/backups/omnialpha-skillkit/{config,omnialpha}`
