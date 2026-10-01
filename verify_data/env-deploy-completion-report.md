# 环境区分部署 — 实施与服务器验证报告

> 2026-10-01 · 本地 master → GitHub → 服务器 `/opt/gate-signal-bot`

## 一、交付内容

| 组件 | 文件 | 作用 |
|------|------|------|
| **overlay 合并** | `gate_bot/config.py`（`deep_merge` / `overlay_dir_for`） | `config/bots.local/` 覆盖基线 |
| **基线归一** | `config/bots/*.yaml` 全 `enabled: false` | 安全默认 |
| **部署流水线** | `scripts/deploy.sh` | 七步：检查→pull→依赖→skill→编码自检→测试→重启验证 |
| **skill 源** | `skills-src/` + `scripts/pack_skill.py` | git 跟踪 + UTF-8 安全打包 |
| **乱码自检** | `gate_bot skill doctor [--fix]` | 检出并修复 CP866 乱码目录名 |
| **部署验证** | `gate_bot deploy-check` | 版本/overlay/bot/skill/健康 一览 |
| **文档** | `docs/DEPLOY.md` + `AGENTS.md §8` | 新机上线 + 常见问题 |

## 二、提交

| commit | 内容 |
|--------|------|
| `edc1431` | feat(deploy): 环境区分部署（配置 overlay + 七步流水线 + 编码归一化） |
| `32dadbd` | fix(deploy): 记录可执行位 + 前置检查忽略 mode-only 变更 |
| `2e7ae4b` | fix(gitignore): skills-src 内 references/history 不该被全局 history/ 忽略 |

## 三、服务器迁移（旧 → 新模型）

```
1) 备份 config → /opt/backups/overlay-migrate/
2) 记录当前启用集 → 仅 brooks-btc
3) 生成 config/bots.local/brooks-btc.yaml
4) git checkout -- config/bots/（基线回到 git 状态）
5) git pull --ff-only → 32dadbd
```

## 四、部署流水线实测（服务器）

```
[1/7] 前置检查     OK（overlay 1 个）
[2/7] 拉取代码     2e7ae4b
[3/7] 依赖         OK
[4/7] 同步 skill   validate PASS → installed
[5/7] 编码自检     doctor: 0 问题
[6/7] 全量测试     884 OK
[7/7] 重启+验证    service active → deploy-check PASS
```

## 五、四个缺口的验证结果

| # | 原缺口 | 现状 | 证据 |
|---|--------|------|------|
| ① | 服务器 23 个配置未提交，pull 前需手工 stash | **已消除** | 服务器 `git status` **完全干净** |
| ② | skill 不在 git，需手工上传 | **已消除** | `skills-src/` 入库，部署第 4 步自动安装 |
| ③ | Linux unzip 中文目录名乱码 | **已消除** | `skill doctor --fix`；validate **0 warnings** |
| ④ | 部署不重启服务 | **已消除** | 第 7 步自动 restart + 健康检查 |

## 六、过程中又发现并修复的问题

| 问题 | 根因 | 修复 |
|------|------|------|
| 前置检查误报（chmod +x） | `git status` 把 mode-only 变更算作 dirty | 记录可执行位 + 前置检查过滤 `0\t0` |
| skill 缺 `references/history`（W05） | `.gitignore` 全局 `history/` 规则误伤 `skills-src/**/history/` | 加否定规则 `!skills-src/**/history/**` |

> 第二个问题是**部署机制自身暴露的**：如果没有「部署后 validate」这一步，这个缺失会静默带到生产。

## 七、最终状态

**服务器**：
```
HEAD:        2e7ae4b
git status:  干净 ✅（此前 23 个未提交）
overlay:     1 个（brooks-btc）
启用 bot:    1 个（brooks-btc）
skill:       price-action-trading v34.2, 100 files, validate PASS 0 warnings
进程:        watchdog + plan-loop + run 全部运行
健康:        error_streak=0
skill 使用:  实盘 bot 已 3 次激活（13:52 / 14:07 / 14:17）
```

**本地**：全量 **884 PASS**

## 八、日常操作

```bash
./scripts/deploy.sh              # 完整部署（含重启）
./scripts/deploy.sh --no-restart # 只同步不重启
./scripts/deploy.sh --dry-run    # 预览
python -m gate_bot deploy-check  # 部署后验证
```

**改环境差异**（不改代码）：
```bash
# 本机启用/禁用 bot
echo "enabled: true" > config/bots.local/<bot>.yaml
# 或改 bot 的 skill 白名单
cat > config/bots.local/brooks-btc.yaml <<'EOF'
enabled: true
strategist:
  skills: [price-action-trading]
EOF
```

## 九、遗留建议

1. **实盘 brooks-btc 仍未显式配 `skills:`** —— 缺省 = 全部可见。建议在 `config/bots.local/brooks-btc.yaml` 显式声明白名单，避免今后新装 skill 自动对实盘可见。
2. 本地 `config/bots.local/` 有 24 个 overlay（开发用），服务器 1 个 —— 这正是设计意图（环境差异），但需注意本地测试 bot 不要误部署。
