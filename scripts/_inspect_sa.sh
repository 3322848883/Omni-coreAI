#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/sa-src 2>/dev/null || { echo "目录不存在"; exit 1; }

echo "=== 1) 目录结构 ==="
ls -la | head -20

echo ""
echo "=== 2) README / 说明 ==="
for f in README.md README.md.bak readme.md README.rst; do
  [ -f "$f" ] && echo "--- $f ---" && head -30 "$f"
done

echo ""
echo "=== 3) docker-compose 服务定义 ==="
if [ -f docker-compose.yml ]; then
  grep -E '^\s{2}[a-z0-9-]+:|image:|container_name:|command:' docker-compose.yml | head -40
elif [ -f docker-compose.yaml ]; then
  grep -E '^\s{2}[a-z0-9-]+:|image:|container_name:|command:' docker-compose.yaml | head -40
fi

echo ""
echo "=== 4) .env（脱敏）==="
if [ -f .env ]; then
  sed 's/=.*/=***/' .env | head -20
fi

echo ""
echo "=== 5) 项目用途线索 ==="
grep -rhiE 'description|项目|用途|名字|app_name|APP_NAME|PROJECT' --include='*.md' --include='*.env*' --include='*.yml' --include='*.yaml' . 2>/dev/null | grep -viE 'password|secret|key|token' | head -15

echo ""
echo "=== 6) 容器健康/运行时长 ==="
docker ps --filter "name=sa-src" --format "{{.Names}}\t{{.Status}}\t{{.Image}}"

echo "=== SA_DONE ==="
