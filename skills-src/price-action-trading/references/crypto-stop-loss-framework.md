# Crypto Stop Loss Quick Reference

> Quick-reference card for crypto stop loss validation (Step 4.0).
> Source: `references/knowledge/instrument_crypto_specifics.md` §5.1 + `references/knowledge/strategy_workflow.md` 第7章.

## Original Rules (Must Follow)

| Rule | Description | Action |
|------|-------------|--------|
| **SA-04** | Stop distance < 1 tick | Don't trade |
| **SA-05** | Stop distance > daily range × 20% | Reduce position or skip |

## Crypto-Specific Considerations

Crypto volatility is 3-8x higher than traditional markets (ES). Structure stops (signal bar extreme + 1 tick) may be too tight and get stopped out by noise.

**Reference values** from `instrument_crypto_specifics.md §5.1` (typical stop distances on 5M chart):

| Instrument | Typical Stop | Scalp Target | Swing Target | Daily ATR Ref |
|-----------|--------------|--------------|--------------|---------------|
| BTC | $100-200 | $150-300 | $500-1000 | $150-300 |
| ETH | $5-10 | $8-15 | $20-50 | $8-15 |
| SOL | $0.50-1.00 | $0.80-1.50 | $2-5 | $0.80-1.50 |

> ⚠️ These are **reference values** for合理性检查, not hard minimums. Actual stops should be based on price structure.

## Step 4.0 Validation Checklist

Execute BEFORE calculating trader's equation:

```
1. Measure stop loss distance = |entry - stop|
2. Check SA-04: distance ≥ 1 tick?           → FAIL = reject trade
3. Check SA-05: distance ≤ daily range × 20%? → FAIL = reduce position or skip
4. Reasonableness check: compare with typical stop distances above
   - If structure stop is significantly tighter than typical → consider widening
   - If structure stop is significantly wider than typical → check if setup is valid
5. Only if ALL pass → proceed to trader's equation (Step 5)
```

## Volatility Adjustment (crypto §5.3)

```
volatility_ratio = current_ATR / daily_avg_ATR

< 0.7  → Low vol:    stop = 80% of standard, position +20%
0.7-1.5 → Normal:    stop = standard, position = standard
1.5-2.5 → High vol:  stop = 1.5× standard, position = 50%, R:R ≥ 3:1
> 2.5  → Extreme:    stop = 2× standard OR don't trade
```

## Common Pitfall

**Structure stop too tight**: On small bars, the signal bar high/low + 1 tick may produce a stop distance that's tight for crypto. In this case:
- Compare with typical stop distances for the instrument
- Consider widening stop if significantly below typical range
- Recalculate position size: `position = risk_amount / stop_distance`
- Recalculate trader's equation with new risk
- If R:R becomes negative after adjustment → reject trade

## Rules Reference

- SL-01: Signal bar stop (most common)
- SA-04: Stop too small (< 1 tick) → don't trade
- SA-05: Stop too large (> 20% daily range) → reduce or skip
- PM-03: Position = max_risk / stop_distance
- BAN-06: Tight range stop entry → don't trade
