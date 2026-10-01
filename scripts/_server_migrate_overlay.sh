#!/bin/bash
# 服务器迁移到 overlay 模型：把本地配置差异转成 config/bots.local/
set -e
cd /opt/gate-signal-bot

echo "=== 1) 备份 ==="
mkdir -p /opt/backups/overlay-migrate
cp -r config /opt/backups/overlay-migrate/
echo "  -> /opt/backups/overlay-migrate/config"

echo
echo "=== 2) 记录当前启用的 bot ==="
ENABLED=$(grep -l '^enabled: true' config/bots/*.yaml 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.yaml$//' || true)
echo "  当前启用: ${ENABLED:-（无）}"

echo
echo "=== 3) 生成本机 overlay ==="
mkdir -p config/bots.local
for b in $ENABLED; do
  case "$b" in _*) continue ;; esac
  printf '# 服务器 overlay：启用 %s\nenabled: true\n' "$b" > "config/bots.local/$b.yaml"
  echo "  config/bots.local/$b.yaml"
done

echo
echo "=== 4) 丢弃本地对基线的改动（已转成 overlay） ==="
git checkout -- config/bots/ 2>/dev/null || true
echo "  baseline 已回到 git 状态"

echo
echo "=== 5) 拉取新代码 ==="
git pull --ff-only origin master 2>&1 | tail -5

echo
echo "=== 6) 校验 overlay 生效 ==="
.venv/bin/python -m gate_bot deploy-check 2>&1 | head -12
