"""精确统计 LLM 请求载荷与工具使用。"""
import json
import os
from pathlib import Path

from omnialpha.config import load_bot_config
from omnialpha.gate_client import GateClient
from omnialpha.strategist.loop import StrategistConfig
from omnialpha.strategist.market import MarketConfig
from omnialpha.strategist.snapshot import collect_snapshot
from omnialpha.strategist.prompt import build_user_prompt, build_system_prompt, load_strategy_prompt
from omnialpha.strategist.tools import NATIVE_TOOLS, TOOL_GUIDE
from omnialpha.strategist.vision import generate_and_encode
from omnialpha.skillkit import SkillRegistry, render_catalog
from omnialpha.watcher import ProjectPaths

root = Path(".").resolve()
paths = ProjectPaths(root)
bot = load_bot_config(paths.config_dir / "skill-e2e.yaml")
s = dict(bot.strategist or {})
client = GateClient(
    api_key=os.environ.get("GATE_TESTNET_API_KEY", ""),
    api_secret=os.environ.get("GATE_TESTNET_API_SECRET", ""),
    env="testnet",
)
mk = dict(s.get("market") or {})
cfg = StrategistConfig(
    timeframe=str(s.get("timeframe") or "1h"),
    symbols=list(s.get("symbols") or bot.symbols),
    candles=int(s.get("candles") or 60),
    market=MarketConfig(
        mode=str(mk.get("mode") or "rest_only"),
        refresh=[str(x) for x in (mk.get("refresh") or ["ticker", "stats"])],
        indicators=[str(x) for x in (mk.get("indicators") or ["ema20", "ema50", "atr14", "rsi14"])],
        extra_timeframes=list(mk.get("extra_timeframes") or []),
        extra_candles=int(mk.get("extra_candles") or 20),
    ),
    bot_root=root, bot_id=bot.bot_id, skills=s.get("skills"),
    tools=dict(s.get("tools") or {}),
    vision=bool(s.get("vision", True)),
    vision_timeframes=[str(x) for x in (s.get("vision_timeframes") or [])] or None,
)
snap = collect_snapshot(client, cfg.symbols, candles=cfg.candles,
                        interval=cfg.timeframe, market_cfg=cfg.market)

print("=" * 60)
print("A. 文本载荷（user prompt）")
print("=" * 60)
text = build_user_prompt(snap, {"min_confidence": 0.7}, cfg.symbols)
print(f"user prompt 总长: {len(text)} 字符")
print(f"snapshot JSON:    {len(json.dumps(snap, ensure_ascii=False))} 字符")
print("顶层字段:", list(snap.keys()))
m = (snap.get("market") or {}).get("ETH_USDT") or {}
candles = m.get("candles") or []
print(f"\nmarket.ETH_USDT 字段: {list(m.keys())}")
print(f"  原始K线 candles: {len(candles)} 根 (1h)")
if candles:
    print(f"  K线字段: {list(candles[0].keys())}")
    print(f"  样例末根: {candles[-1]}")
print(f"  indicators 字段: {list((m.get('indicators') or {}).keys())[:15]}")
acc = snap.get("account") or {}
print(f"\naccount 字段: {list(acc.keys())}")
print(f"  positions={acc.get('positions')}  open_orders={acc.get('open_orders')}  protections={acc.get('protections')}")

print()
print("=" * 60)
print("B. K 线图（vision）")
print("=" * 60)
sym = "ETH_USDT"
tfs = list(cfg.vision_timeframes or []) or [cfg.timeframe]
print(f"vision={cfg.vision}, vision_timeframes={tfs}")
print(f"snapshot extra_timeframes={cfg.market.extra_timeframes}, extra_candles={cfg.market.extra_candles}")
m_tf = (m.get("tf") or {})
print(f"snapshot tf 子块: {list(m_tf.keys())}")
charts = []
for tf in tfs:
    if tf == cfg.timeframe:
        klines = candles
        ind = m.get("indicators") or {}
    else:
        block = m_tf.get(tf) or {}
        klines = block.get("candles") or []
        ind = block.get("indicators") or {}
    merged = []
    for i, row in enumerate(klines):
        r = dict(row)
        for name, series in ind.items():
            if isinstance(series, (list, tuple)) and i < len(series):
                r.setdefault(name, series[i])
        merged.append(r)
    b64 = generate_and_encode(merged, symbol=sym, timeframe=tf) if merged else None
    print(f"  {tf}: candles={len(merged)} -> {'图 ' + format(len(b64), ',') + ' 字符' if b64 else 'FAIL'}")
    if b64:
        charts.append(b64)
print(f"生成图片数: {len(charts)}")
total_b64 = sum(len(c) for c in charts)
print(f"图片 base64 合计: {total_b64:,} 字符 ≈ {total_b64//1024} KB")

print()
print("=" * 60)
print("C. system prompt")
print("=" * 60)
persona = load_strategy_prompt(s.get("prompt_file"), prompts_root=root / "prompts", bot_root=root)
reg = SkillRegistry(); reg.scan([root / "skills"])
cat = render_catalog(reg.visible_for(bot.bot_id, s.get("skills")))
sysp = build_system_prompt(persona, tools_guide=TOOL_GUIDE, skill_catalog=cat)
print(f"system 总长: {len(sysp):,} 字符")
print(f"  策略人格: {len(persona):,}")
print(f"  TOOL_GUIDE: {len(TOOL_GUIDE):,}")
print(f"  skill_catalog: {len(cat):,}")
print(f"  固定契约+schema: {len(sysp)-len(persona)-len(TOOL_GUIDE)-len(cat):,}")

print()
print("=" * 60)
print("D. 工具面（function calling）")
print("=" * 60)
print(f"共 {len(NATIVE_TOOLS)} 个工具:")
print(" ", ", ".join(t["function"]["name"] for t in NATIVE_TOOLS))

print()
print("=" * 60)
print("E. LLM 实际调用的工具（据 thinking 各轮）")
print("=" * 60)
docs = sorted((root / "data" / "bots" / "skill-e2e" / "state").glob("*.thinking.json"),
              key=lambda p: p.stat().st_mtime)
doc = json.loads(docs[-1].read_text(encoding="utf-8"))
for i, step in enumerate(doc.get("reasoning_chain") or [], 1):
    hits = [w for w in ("skill", "klines", "indicators", "smc_map", "smc_events",
                        "sqzmom", "ticker", "orderbook", "account", "stats", "contract")
            if w in step]
    print(f"轮{i}: 命中关键词 {hits}")
    # 首句
    first = step.split("\n")[0][:110]
    print(f"      {first}")
