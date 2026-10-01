#!/usr/bin/env python3
"""诊断服务器上 skill 的 source 目录名编码。"""
import sys
from pathlib import Path

d = Path("/opt/gate-signal-bot/skills/price-action-trading/references/knowledge/source")
print("filesystemencoding:", sys.getfilesystemencoding())
print("locale prefer:", __import__("locale").getpreferredencoding(False))
print()
print("实际条目（repr）:")
for p in sorted(d.iterdir()):
    print("  ", repr(p.name))
print()
want = d / "V1_趋势篇"
print("期望名 exists:", want.exists())
print("期望名 utf-8 字节:", "V1_趋势篇".encode("utf-8"))
print()
print("实际名 utf-8 字节:")
for p in sorted(d.iterdir()):
    print("  ", p.name.encode("utf-8", "surrogateescape"))
