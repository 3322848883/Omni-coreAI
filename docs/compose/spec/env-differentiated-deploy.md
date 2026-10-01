---
feature: env-differentiated-deploy
status: delivered
updated: 2026-10-01
branch: master
related: docs/compose/spec/skillkit.md
---

# 环境区分部署（本地 vs 服务器）

## Report

**What was built** — 三层配置模型 + 部署流水线 + 编码归一化：

| 组件 | 文件 | 作用 |
|------|------|------|
| overlay 合并 | `gate_bot/config.py`（`deep_merge` / `overlay_dir_for`） | `config/bots.local/` 覆盖基线，gitignore 不冲突 |
| 基线归一 | `config/bots/*.yaml` 全部 `enabled: false` | 安全默认；启用集进 overlay |
| 部署流水线 | `scripts/deploy.sh` | 七步：检查→pull→依赖→skill→编码自检→测试→重启验证 |
| skill 源 | `skills-src/` + `scripts/pack_skill.py` | git 跟踪源 + UTF-8 安全打包 |
| 乱码自检 | `gate_bot skill doctor [--fix]` | 检出并修复 Linux unzip 乱码目录名 |
| 部署验证 | `gate_bot deploy-check` | 版本/overlay/bot/skill/健康 一览 |
| 文档 | `docs/DEPLOY.md` + `AGENTS.md` §8 | 新机器上线 + 常见问题 |

**Verification** — `tests/test_deploy_env.py` 17 PASS（deep_merge 6 / overlay 5 / demojibake 4 / 命令 2）；全量 **884 PASS**。

**Journey log** — 起因是 SkillKit 上线时暴露的 4 个部署缺口：
1. 服务器 23 个 bot 配置未提交（`git pull` 前需手工 stash/pop）
2. skill 不在 git，需手工上传安装
3. Linux `unzip` 把 UTF-8 目录名按 CP866 解读 → 乱码（W05）
4. 部署脚本不重启 systemd

修复：overlay 结构性消除 ①；`skills-src/` + 部署步骤消除 ②；`pack_skill.py` + `skill doctor` 消除 ③；流水线第 7 步消除 ④。

## [S1] Problem

本地开发机与生产服务器**本来就需要不同的运行状态**，但当前部署只同步代码，不同步环境状态。

### 证据（2026-10-01 SkillKit 上线实测）

| # | 现象 | 根因 |
|---|------|------|
| 1 | 服务器有 **23 个未提交的 bot 配置改动**（本地禁用了 paper bot），`git pull` 前必须手工 `stash`/`pop` | `config/bots/*.yaml` 在 git 里**共享**，无环境 overlay |
| 2 | skill 不在 git（`skills/*` 被 gitignore），部署脚本不装 skill，需手工上传 zip + `skill install` | skill 分发无约定 |
| 3 | 服务器 `skill validate` 报 W05：`references/knowledge/source/V1_趋势篇` 不存在 —— 实为 **目录名乱码**（UTF-8 被 Linux `unzip` 按 CP866 解读） | 打包/解压无编码约定 |
| 4 | 部署脚本不重启 systemd，需手工 `systemctl restart gate-watchdog` | 部署流水线不完整 |

### 现状：`_update_server.sh` 只做三件事

```bash
git pull -q origin master
uv pip install -q -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

**默认「代码 = 环境」**。唯一的版本区分只有 `.env`（密钥）。

### 目标

建立**显式的环境分层**：同一份代码，通过 overlay 声明差异，部署全流程可重复、无手工步骤。

### 非目标

- 多环境（staging/prod）编排、蓝绿部署、容器化
- 配置热更新（仍是重启生效）
- 密钥管理改造（继续用 `.env`）

## [S2] Design

### [S2.1] 三层配置模型

```
config/
├── bots/                    # ① 基线（git 跟踪，全环境共享）
│   ├── brooks-btc.yaml      #    enabled: false（基线默认关）
│   ├── pa-a.yaml
│   └── ...
├── bots.local/              # ② 环境覆盖（gitignore，每机一份）
│   ├── brooks-btc.yaml      #    服务器：enabled: true
│   └── pa-a.yaml            #    服务器：enabled: false
└── alerts.yaml              # ③ 密钥（gitignore，已存在）
```

**合并规则**：`load_bot_config` 先读基线，再用 `bots.local/<id>.yaml` **深度合并**覆盖。缺失 overlay 时行为不变（向后兼容）。

**为什么这样能根治 ①**：`config/bots.local/` 被 gitignore → `git pull` **永远不会与它冲突**，服务器本地状态天然保留。

### [S2.2] 加载逻辑改动

```python
# gate_bot/config.py
def load_bot_config(path: Path, overlay_dir: Path | None = None) -> BotConfig:
    data = _yaml(path)
    if overlay_dir:
        ov = overlay_dir / path.name
        if ov.is_file():
            data = deep_merge(data, _yaml(ov))   # overlay 覆盖基线
    return _build(data)

def load_all_bots(config_dir: Path, overlay_dir: Path | None = None) -> dict[str, BotConfig]:
    ...
```

`ProjectPaths` 新增：
```python
@property
def bots_overlay_dir(self) -> Path:
    return self.root / "config" / "bots.local"
```

调用点（`__main__.py` / `watcher.py`）传 `overlay_dir=paths.bots_overlay_dir`。

**深度合并语义**：dict 递归合并；list/scalar 整体替换；`null` 显式删除键。

### [S2.3] 部署流水线（`scripts/deploy.sh`）

替代 `_update_server.sh`，7 步：

```
1) 前置检查      git 干净（仅允许 bots.local/ 与 data/ 未提交）
2) 拉取代码      git pull --ff-only origin master
3) 依赖         uv pip install -r requirements.txt -e .
4) 同步 skill   从 skills-src/ 安装（见 S2.4）
5) 编码自检     检测 mojibake 目录名并修复（见 S2.5）
6) 测试         unittest discover（失败即中止，不重启）
7) 重启 + 验证  systemctl restart gate-watchdog → 等 15s → 健康检查
```

**关键**：第 6 步失败**不重启**（保留旧进程继续跑），避免部署事故。

### [S2.4] Skill 分发

```
skills-src/                    # git 跟踪：skill 包源（可含子目录）
└── price-action-trading/
    ├── SKILL.md
    └── references/ ...

skills/                        # 运行时安装目录（gitignore）
```

部署步骤 4：
```bash
for d in skills-src/*/; do
  id=$(basename "$d")
  .venv/bin/python -m gate_bot skill validate "$d" || exit 1
  .venv/bin/python -m gate_bot skill install "$d" --yes
done
```

**为什么用 skills-src 而不是直接跟踪 skills/**：
- `skills/` 是运行时目录（含 `.installed` 标记、日志），不该进 git
- `skills-src/` 是源，可 review、可版本化
- 安装步骤走 validate（fail-closed），保证坏包进不去

**每环境启用哪些 skill**：仍由 `config/bots.local/<bot>.yaml` 的 `skills:` 键控制（overlay 覆盖基线）。

### [S2.5] 编码归一化

**问题**：Windows 打包的 zip，Linux `unzip` 把 UTF-8 文件名按 CP866 解读。

**方案**（三层防御）：

1. **打包侧**：统一用 Python 打包（`scripts/pack_skill.py`），确保 zip 条目带 UTF-8 标志位（`zipfile` 默认行为）
2. **部署侧**：用 **Python 解压**而非 `unzip`（`zipfile` 正确处理 UTF-8）
3. **自检侧**：部署脚本第 5 步跑 mojibake 检测（`gate_bot skill doctor`），发现乱码目录名即修复

```python
# 检测：非 ASCII 目录名尝试 cp866→utf-8 还原，成功即视为 mojibake
def demojibake(name): return name.encode('cp866').decode('utf-8')
```

**新增 CLI**：`python -m gate_bot skill doctor [--fix]`（扫描 skills/ 下乱码名，报告或修复）

### [S2.6] 部署后验证

```bash
# 7a) 进程
systemctl is-active gate-watchdog
ps aux | grep -c 'gate_bot --root'

# 7b) 健康
cat data/bots/brooks-btc/state/health.json   # error_streak 应为 0

# 7c) 部署报告
python -m gate_bot deploy-check
```

新增 `gate_bot deploy-check` 子命令，输出：
```
代码版本:    5c27985
配置 overlay: 12 个（bots.local/）
启用 bot:    brooks-btc
已装 skill:  price-action-trading v34.2 (100 files, 0 warnings)
健康:        error_streak=0, last_cycle=<ts>
```

### [S2.7] 环境差异一览（目标状态）

| 项 | 本地（开发） | 服务器（生产） | 载体 |
|----|-------------|---------------|------|
| 启用 bot | 测试 bot | 仅 brooks-btc | `config/bots.local/*.yaml` |
| skill 启用 | 多个测试 skill | price-action-trading | 同上 `skills:` 键 |
| 密钥 | testnet | live | `.env`（已有） |
| 数据目录 | 本地 | `/opt/.../data` | `.env` GATE_BOT_ROOT（已有） |
| skill 包 | 同源 | 同源 | `skills-src/` → install |

## [S3] Tasks

| # | 任务 | 验收 | 覆盖 |
|---|------|------|------|
| T1 | `config.py`：`load_bot_config/load_all_bots` 支持 overlay 深度合并 | 有 overlay 时覆盖、无 overlay 时行为不变；deep merge 单测 | S2.1 S2.2 |
| T2 | `ProjectPaths.bots_overlay_dir` + 调用点接线 | `gate_bot status` 读到 overlay | S2.2 |
| T3 | `.gitignore` 加 `config/bots.local/`；基线 `enabled: false` 归一 | git 里无 enabled:true 的基线 bot | S2.1 |
| T4 | `skills-src/` 建立 + `scripts/pack_skill.py`（UTF-8 打包） | 打包→解压→validate PASS 0 warning | S2.4 S2.5 |
| T5 | `gate_bot skill doctor [--fix]` 乱码检测修复 | 构造乱码名 → 检出并修复 | S2.5 |
| T6 | `scripts/deploy.sh` 七步流水线 | 幂等、失败不重启、可 dry-run | S2.3 |
| T7 | `gate_bot deploy-check` 部署后验证 | 输出代码版本/overlay/bot/skill/健康 | S2.6 |
| T8 | 文档：`docs/DEPLOY.md` + AGENTS.md 部署章节 | 照文档可完成一次部署 | 全部 |
| T9 | 端到端演练：本地造 overlay → 部署到服务器 → 验证差异生效 | 服务器状态与本地隔离、pull 不冲突 | 全部 |

## [S4] Rollout

| 阶段 | 内容 | 退出门槛 |
|------|------|----------|
| **P1（本 spec）** | 配置 overlay + 部署脚本 + skill doctor + deploy-check | T1–T9 全绿；服务器 pull 不再冲突 |
| **P2** | 多环境 profile（`bots.local` 按 `GATE_BOT_ENV` 分目录） | 需要 staging 时再做 |
| **P3** | 配置漂移检测（部署后对比 overlay 与运行时） | 按需 |

## [S5] 风险与回滚

| 风险 | 缓解 |
|------|------|
| overlay 合并语义引入配置错误 | 深度合并单测 + `deploy-check` 显示最终生效值 |
| 部署中断导致 bot 停摆 | 第 6 步测试失败即中止（不重启）；备份 config |
| 基线 `enabled: false` 改动影响本地 | 本地用 overlay 显式启用；一次性迁移 |

**回滚**：`git revert` + `systemctl restart`；config 备份在 `/opt/backups/`。

## [S6] Open questions

1. overlay 目录名用 `bots.local` 还是按环境分 `envs/<env>/bots`？（P1 先做单层，够用）
2. 基线 bot 的 `enabled` 默认值：`false`（安全）vs 保持现状？（倾向 false）
3. `skills-src/` 是否也走 overlay（不同环境装不同 skill 版本）？（P1 同源，P2 再说）
