"""列 TV 指标模块与已装 skill。"""
import importlib
import inspect
from pathlib import Path

print("=== TV 指标模块（tv_indicators）===")
for mod in ("linreg_trendlines", "rsi_yata", "lr_ha_candles"):
    m = importlib.import_module("gate_bot.strategist.tv_indicators." + mod)
    fns = [n for n, _ in inspect.getmembers(m, inspect.isfunction) if not n.startswith("_")]
    print(f"  {mod}:")
    for f in fns:
        print(f"      {f}")

print()
print("=== 已装 skill（skills/）===")
from gate_bot.skillkit import SkillRegistry  # noqa: E402

reg = SkillRegistry()
reg.scan([Path("skills")])
for m in reg.all_metas():
    ver = m.version or "-"
    print(f"  {m.id}  v{ver}  {m.body_tokens} tokens  model_invoke={m.model_invocation}")

print()
print("=== skill 源（skills-src/）===")
for p in sorted(Path("skills-src").iterdir()):
    if p.is_dir():
        n = sum(1 for _ in p.rglob("*") if _.is_file())
        print(f"  {p.name}  ({n} files)")
