# 部署（本地 vs 服务器）

> 同一份代码，通过 **overlay** 声明环境差异。部署全流程可重复、无手工步骤。

## 1. 三层配置模型

```
config/
├── bots/            # ① 基线（git 跟踪，全环境共享）— 全部 enabled: false
│   ├── brooks-btc.yaml
│   └── ...
├── bots.local/      # ② 环境覆盖（gitignore，每机一份）
│   ├── brooks-btc.yaml     #   本机启用哪些 bot
│   └── ...
└── alerts.yaml      # ③ 密钥（gitignore）
```

**合并规则**：先读基线，再用 `bots.local/<同名>` **深度合并**覆盖。
- dict 递归合并
- list / scalar 整体替换
- overlay 里写 `null` → 显式删除该键
- **没有 overlay 时行为不变**（向后兼容）

### ⛔ 关键不变量：`enabled` 只认 overlay

**基线里的 `enabled` 一律被忽略**（即使写了 `true`）。这是结构性防护：

- 想启用 bot → 必须在 `config/bots.local/<同名>.yaml` 写 `enabled: true`
- 误改基线 `enabled: true` → 引擎忽略并打 WARNING，`deploy-check` 报「基线违规」
- 因此：**在 A 机启用某个 bot，永远不会意外带到 B 机**

```bash
# 正确：启用 pa-d
echo "enabled: true" > config/bots.local/pa-d.yaml

# 错误（无效，会被忽略并告警）
sed -i 's/enabled: false/enabled: true/' config/bots/pa-d.yaml
```

**为什么这样设计**：`config/bots.local/` 被 gitignore → `git pull` **永远不会与它冲突**，
每台机器的启用集天然保留；基线不可启用 → 并发编辑也不会污染生产。

### 环境差异对照

| 项 | 本地（开发） | 服务器（生产） | 载体 |
|----|-------------|---------------|------|
| 启用 bot | 测试 bot（24 个） | 仅 brooks-btc | `config/bots.local/*.yaml` |
| skill 启用 | 多个测试 skill | price-action-trading | bot yaml `skills:` 键（可 overlay） |
| 密钥 | testnet | live | `.env` |
| 数据目录 | 本地 | `/opt/.../data` | `.env` `GATE_BOT_ROOT` |

## 2. 新增一台机器

```bash
# 1) 克隆 + 虚拟环境
git clone <repo> /opt/gate-signal-bot && cd /opt/gate-signal-bot
uv venv --python 3.12 .venv
uv pip install -r requirements.txt -e .

# 2) 密钥
cp .env.example .env   # 若无模板则手写
chmod 600 .env         # GATE_BOT_ROOT / OPENAI_* / GATE_*

# 3) 环境 overlay（关键：决定本机启用哪些 bot）
mkdir -p config/bots.local
cat > config/bots.local/brooks-btc.yaml <<'EOF'
enabled: true
EOF

# 4) 部署
./scripts/deploy.sh
```

## 3. 部署流水线（`scripts/deploy.sh`）

```
[1/7] 前置检查    git 干净（仅允许 bots.local/ 与 data/ 未提交）
[2/7] 拉取代码    git pull --ff-only
[3/7] 依赖        uv pip install -r requirements.txt -e .
[4/7] 同步 skill  skills-src/ → validate → install
[5/7] 编码自检    skill doctor --fix（修 Linux unzip 乱码目录名）
[6/7] 全量测试    失败即中止，不重启
[7/7] 重启+验证   systemctl restart gate-watchdog → deploy-check
```

```bash
./scripts/deploy.sh              # 完整部署
./scripts/deploy.sh --no-restart # 只同步不重启（先验证）
./scripts/deploy.sh --dry-run    # 只打印将执行的动作
```

**安全设计**：第 6 步测试失败 → **中止且不重启**，旧进程继续运行，避免部署事故。

## 4. Skill 分发

```
skills-src/          # git 跟踪：skill 包源
└── price-action-trading/
skills/              # 运行时安装目录（gitignore）
```

```bash
python scripts/pack_skill.py price-action-trading -o /tmp/pa.zip   # UTF-8 安全打包
python -m gate_bot skill doctor [--fix]                            # 体检（乱码名/校验）
python -m gate_bot skill install skills-src/<id> --yes             # 安装
```

**为什么不用系统 zip/unzip**：Windows 打包 + Linux `unzip` 会把 UTF-8 文件名按 CP866
解读 → 乱码（曾导致 `references/knowledge/source/V1_趋势篇` 无法读取）。
统一用 Python `zipfile`（强制 UTF-8 标志位）+ `skill doctor` 自检。

### 4.1 每个 bot 的 skill 启用开关（`strategist.skills`）

`skills:` 键就是**该 bot 的 skill 白名单**：

| 取值 | 含义 |
|------|------|
| 缺省（无该键） | **全部可见** —— 新装 skill 下一个 cycle 自动对该 bot 可见 |
| `skills: []` | **全关** —— 该 bot 不加载任何 skill |
| `skills: [a, b]` | **白名单** —— 只有 a/b 可见，新装 skill 进不来 |

> ⚠️ `_skill_catalog()` **每个 cycle 重新扫描**，所以「缺省」= 新装即生效，无需重启。

**实盘 bot 建议显式锁定**（发布 ≠ 可用）：

```yaml
# config/bots.local/brooks-btc.yaml
enabled: true
strategist:
  skills: [price-action-trading]   # 白名单：只有这个进实盘决策
```

改动后需 `systemctl restart gate-watchdog`（runner 启动时读配置）。

## 5. 部署后验证

```bash
python -m gate_bot deploy-check
```

输出：
```
代码版本:   master @ 5c27985 ...
配置 overlay: 24 个 (config/bots.local)
启用 bot:   24 个 [...]
已装 skill: 1 个 ['price-action-trading']
              price-action-trading v34.2 (100 files)
健康 brooks-btc: OK  cycle=...
结论: PASS
```

## 6. 回滚

```bash
git revert <bad-commit> && ./scripts/deploy.sh
# 或恢复配置
cp -r /opt/backups/<ts>/config/* config/
systemctl restart gate-watchdog
```

## 7. 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `git pull` 冲突 | 改到了 `config/bots/` 基线 | 把差异移到 `config/bots.local/` |
| skill validate 报 W05 | 目录名乱码（Linux unzip） | `python -m gate_bot skill doctor --fix` |
| 部署后 bot 没起来 | 测试失败被中止 / overlay 未启用 | 看 `deploy-check` 的「启用 bot」行 |
| 新装的 skill 对所有 bot 可见 | bot 未显式配 `skills:` | 在该 bot 加 `skills: [...]` 或 `skills: []` |
