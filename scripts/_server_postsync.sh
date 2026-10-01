#!/bin/bash
# 服务器同步后处理：禁用测试 bot 与 paper bot（保留 brooks-btc 实盘）
set -e
cd /opt/gate-signal-bot

echo "=== 禁用测试/paper bot ==="
for b in pa-a pa-b pa-c skill-ab skill-e2e skill-real; do
  f="config/bots/$b.yaml"
  if [ -f "$f" ]; then
    sed -i 's/^enabled: true/enabled: false/' "$f"
    echo "  $b -> $(grep '^enabled:' $f)"
  fi
done

echo
echo "=== 当前 enabled: true 的 bot ==="
grep -l '^enabled: true' config/bots/*.yaml | grep -v '/_' || true

echo
echo "=== 安装 skill ==="
if [ -d /opt/skill-pkg/price-action-trading ]; then
  .venv/bin/python -m gate_bot skill validate /opt/skill-pkg/price-action-trading
  .venv/bin/python -m gate_bot skill install /opt/skill-pkg/price-action-trading --yes
  .venv/bin/python -m gate_bot skill list
else
  echo "  (skill 包未上传到 /opt/skill-pkg，跳过)"
fi
