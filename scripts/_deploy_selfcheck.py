#!/usr/bin/env python3
"""部署后自检：新模块可导入 / 引擎默认 legacy / watchlist 配置解析 / 迁移工具可运行（dry-run 逻辑不碰库）。"""
import sys

sys.path.insert(0, "/opt/omnialpha")
sys.path.insert(0, "/opt/omnialpha/pa-data-source")

import yaml

import indicators_core
import ingest
import migrate_v2
import writer_v2
from indicator_config import indicator_columns, parse_indicators, spec_hash, warmup_bars
from retention_policy import parse_limits

print("imports OK:", ingest.resolve_engine())

cfg = yaml.safe_load(open("/opt/omnialpha/pa-data-source/watchlist.yaml", encoding="utf-8"))
specs = parse_indicators(cfg.get("indicators"))
cols = indicator_columns(specs)
print(f"watchlist indicators -> {len(cols)} 列 | warmup={warmup_bars(specs)} | hash={spec_hash(specs)}")
lim = parse_limits((cfg.get("retention") or {}).get("per_interval"), warmup=warmup_bars(specs))
print("retention:", {k: (v.max_candles, v.days) for k, v in lim.items()})

# 引擎默认必须是 legacy（否则重启即改变采集行为）
eng = ingest.resolve_engine()
assert eng == "legacy", f"引擎默认值不是 legacy，是 {eng}"
print("engine default = legacy ✓")

# 库现状（只读）
from contracts.kline_schema import schema_info

info = schema_info("/opt/omnialpha/pa-data-source/data/kline.db")
print("kline.db:", {k: info[k] for k in ("ok", "version", "has_closed", "indicators")})
print("SELFCHECK OK")
