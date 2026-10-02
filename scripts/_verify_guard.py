#!/usr/bin/env python3
"""实测结构性防护：基线 enabled:true 是否被忽略。"""
import shutil
import sys
from pathlib import Path

ROOT = Path("/opt/omnialpha")
sys.path.insert(0, str(ROOT))
from omnialpha.config import load_bot_config  # noqa: E402

target = ROOT / "config" / "bots" / "pa-a.yaml"
backup = target.read_text(encoding="utf-8")

print("=== 1) 基线改写为 enabled: true ===")
target.write_text(
    backup.replace("enabled: false", "enabled: true", 1), encoding="utf-8"
)
b = load_bot_config(target)
print(f"   基线 enabled:true → pa-a.enabled = {b.enabled}  (期望 False)")
ok1 = b.enabled is False

print()
print("=== 2) overlay 同时为 true 时才启用 ===")
ov = ROOT / "config" / "bots.local" / "pa-a.yaml"
ov_backup = ov.read_text(encoding="utf-8") if ov.is_file() else None
ov.write_text("enabled: true\n", encoding="utf-8")
b2 = load_bot_config(target)
print(f"   基线 true + overlay true → pa-a.enabled = {b2.enabled}  (期望 True)")
ok2 = b2.enabled is True

print()
print("=== 3) 恢复 ===")
target.write_text(backup, encoding="utf-8")
if ov_backup is None:
    ov.unlink(missing_ok=True)
else:
    ov.write_text(ov_backup, encoding="utf-8")
print("   已恢复")
b3 = load_bot_config(target)
print(f"   恢复后 pa-a.enabled = {b3.enabled}")

print()
print("结论:", "PASS 结构性防护生效" if (ok1 and ok2) else "FAIL")
