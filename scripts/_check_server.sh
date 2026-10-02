#!/bin/bash
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha
echo "=== 心跳 ==="
.venv/bin/python -c '
import json,subprocess,sys
r=subprocess.run([sys.executable,"-m","omnialpha","status"],capture_output=True,text=True,encoding="utf-8")
d=json.loads(r.stdout)
for h in (d.get("heartbeats") or []):
    print(" ", h["bot_id"], h["component"], h["detail"], "age=%ss"%round(h["age_s"]))
en=[b for b in d.get("bots",[]) if b.get("enabled")]
print("  enabled bots:", len(en))
'
echo "=== 进程（去重 bot/mode）==="
ps -eo args | grep '[g]ate_bot' | grep -E 'plan-loop|paper-run|run --bot|supervisor' | sed 's/.*--bot //' | awk '{print $2"/"$1}' | sort | uniq | head -40
echo "=== 内存 ==="
free -m | head -2
echo "=== 服务 ==="
systemctl is-active omnialpha-watchdog
echo "=== CHECK_DONE ==="
