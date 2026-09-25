# LLM/AI Trading Strategy Prompts — Exchanges & Bot Products

Research date: 2026-09-25  
Method: Bing/Brave SERP + Playwright (headless Chromium) page extraction. Evidence: `prompts-exchanges-evidence*.md`, `prompts-exchanges-raw*.json`.

Focus: prompt texts/templates useful for a **perp multi-symbol signal bot**.

---

## 1. Cryptohopper.AI — Writing Effective Prompts (grid/DCA bot spec style)

**URL**: https://www.cryptohopper.ai/en/docs/prompt-engineering

**Prompt excerpt / structure** (vague vs specific pattern from official docs):

> **Vague:** "Make me a trading bot."
>
> **Specific:** "A grid bot for ETH/USDT with 10 grid levels between $2,000 and $3,000, 0.5% take-profit per level, and a dashboard showing open orders, filled orders, and total realized P&L. Start in paper mode."

**Structure they teach**: Name the pair + strategy → state rules precisely (entry/exit, grid levels/spacing, TP/SL, schedule) → describe interface → iterate one change at a time.

**Strengths**:
- Spec-as-prompt: pair, numeric levels, TP, paper-mode safety are first-class
- Explicit anti-vague guidance reduces silent LLM assumption-filling
- Paper-mode default is a production safety pattern

**Fit for perp multi-symbol bot**: High for **parameter generation** (per-symbol grid/DCA/TP/SL templates). Extend the skeleton: `symbol + timeframe + entry rule + TP/SL + max leverage + position cap`. Paper-mode maps to dry-run / signal-only mode.

---

## 2. Cryptohopper.AI — Crypto Template Pack (seed prompts)

**URL**: https://www.cryptohopper.ai/en/docs/crypto-template-pack

**Prompt excerpt / structure**:

> Start from a ready-made **grid bot, DCA bot, portfolio dashboard, signals dashboard, or backtester**. Pick one on the home page to **seed the prompt, then refine it to match your strategy**. A template is just a starting point — change the pair, adjust the rules, add panels, or wire in extra data — **one change at a time**. The trading templates **default to paper mode**.

**Included starter intents**: Grid Bot · DCA Bot · Portfolio Dashboard · News+Signals · Backtester.

**Strengths**:
- Template-as-prompt-seed workflow (select → refine) instead of free-form from scratch
- Separates "deploy scaffold" from "strategy rules"
- Paper-mode + one-change-at-a-time iteration is bot-ops discipline

**Fit for perp multi-symbol bot**: High. Reuse the 5 starter types as **intent router** labels (grid/dca/signals/backtest/portfolio) for a multi-symbol router; keep paper-first as default policy.

---

## 3. XCryptoBot — 20+ Copy-Paste ChatGPT Prompts for 3Commas bots

**URL**: https://xcryptobot.com/blog/chatgpt-prompts-crypto-trading-strategies-2026

**Prompt excerpts** (20+ concrete prompts; representative set):

**DCA strategy (Prompt 1)**:
> Create a DCA (Dollar Cost Averaging) trading strategy for Bitcoin with the following parameters:
> Capital: $5,000 · Risk tolerance: Medium · Time horizon: 6 months · Market condition: Volatile bull market
> Include: Base order size · Safety order sizes and spacing · Take profit targets · Stop loss levels · Maximum number of safety orders
> **Format the output as a table with exact settings I can use in 3Commas.**

**RSI signal (Prompt 7)**:
> Develop a crypto trading strategy based on RSI with these rules: Buy when RSI < 30 (oversold) · Sell when RSI > 70 (overbought) · Use 14-period RSI on 4-hour timeframe · Include position sizing based on RSI strength · Add stop loss and take profit rules · Format as actionable bot settings.

**Multi-indicator (Prompt 8)**:
> Create a comprehensive trading strategy combining: RSI for momentum · MACD for trend direction · Bollinger Bands for volatility · Volume for confirmation. Provide: Exact entry conditions (all indicators must align) · Exit conditions · Position sizing rules · Risk management parameters.

**Risk framework (Prompt 10)**:
> Create a risk management framework for a $20,000 crypto portfolio with: Maximum 2% risk per trade · Maximum 20% portfolio drawdown limit · Position sizing formula · Correlation-based diversification rules · Emergency exit conditions.

**Custom template (bonus)**:
> Create a [STRATEGY TYPE] trading strategy for [CRYPTO PAIR] with: Capital: [AMOUNT] · Risk Tolerance: [LOW/MEDIUM/HIGH] · Time Horizon: [TIMEFRAME] · Market Condition: [BULL/BEAR/SIDEWAYS]
> The strategy should: [GOAL 1/2/3]
> Include: Entry conditions · Exit conditions · Position sizing · Risk management · Expected performance metrics
> **Format as [3COMMAS/TRADINGVIEW/EXCEL] compatible settings.**

**Strengths**:
- Copy-paste ready; strong **output-shape constraint** ("table of exact bot settings")
- Covers DCA / Grid / RSI / multi-indicator / risk / regime / arbitrage / mean-reversion / breakout
- Chain-prompting and scenario-testing techniques (flash crash, slow bleed, pump, chop)

**Fit for perp multi-symbol bot**: Very high. The **slot template** (`[STRATEGY TYPE] [PAIR] capital/risk/horizon/regime → entry/exit/size/risk`) is ideal for batch multi-symbol generation; "format as bot settings" maps cleanly to JSON schema for a signal bot. Add leverage + funding + liquidation-price fields for perps.

---

## 4. PionexGPT — Natural-language strategy → Pine Script → Signal Bot

**URL**: https://www.pionex.com/blog/automate-your-trading-ideas-on-pionex-with-pionexgpt-and-tradingview/

**Prompt excerpt** (official tutorial sample):

> Create a strategy using the exponential moving average indicator. Buy when the exponential moving average of 9 is greater than the exponential moving average of 21. Exit a long position when the exponential moving average of 9 is less than the exponential moving average of 21.

**Structure**: NL strategy statement → PionexGPT emits Pine Script → TradingView backtest → Pionex Signal Bot webhook execution.

**Strengths**:
- Minimal, complete signal pair (entry + exit) in one sentence
- Explicit **NL → code → backtest → webhook** pipeline
- Strategy-as-natural-language is the right abstraction level for LLM

**Fit for perp multi-symbol bot**: High as a **signal-clause grammar**: `Buy when … Exit long when …` per symbol. For multi-symbol, wrap: "For {symbol} on {tf}, …" and emit side+reason+TP/SL. Drop Pine; emit structured signal JSON instead.

---

## 5. DocsBot — Futures Trading Bot system prompt

**URL**: https://docsbot.ai/prompts/technical/futures-trading-bot

**Prompt excerpt / structure**:

> Create a comprehensive and functional trading bot specifically designed for futures markets. The bot should be capable of:
> - Connecting to a futures trading platform via API.
> - Analyzing real-time market data and historical data to make informed trading decisions.
> - Implementing trading strategies such as trend following, mean reversion, or momentum strategies.
> - Managing risk through stop-loss orders, take-profit targets, and position sizing.
> - Executing trades automatically based on predefined criteria.
> - Logging all trading activity and performance metrics for analysis.
>
> Please provide detailed explanations… Begin by reasoning through the key components…
>
> **# Steps** 1…8 (basics → strategy → API → analysis → execution → risk → logging → test before live)
> **# Output Format** clear, well-commented Python + config + component explanations
> **# Notes** standard futures API · clarity · robustness/error handling · no real API keys

**Strengths**:
- Classic **role + capabilities + Steps + Output Format + Notes** system-prompt skeleton
- Futures-specific (risk, leverage-adjacent, auto-exec, logging)
- Forces reasoning before code — reduces brittle one-shot scripts

**Fit for perp multi-symbol bot**: Medium–high as the **planner/executor system prompt** for the bot *builder*, not the live trader. Steal the `# Steps / # Output Format / # Notes` sections for any strategy-spec generation task. Add multi-symbol loop, leverage cap, funding awareness.

---

## 6. Bitget Agent Skill (official) — multi-step trading workflows + write-safety

**URL**: https://github.com/Bitget-AI/agent-skill

**Prompt excerpt / structure** (skill definition teaches the agent):

> Trigger recognition in English and Chinese: "buy BTC at market" / 「帮我买 BTC」…
> Multi-step workflow: "check my positions, and if I have no BTC open a 10x long" — the AI queries first, reasons, then places the order, showing **[CAUTION] before every write operation** and waiting for confirmation.
> Paper trading: "use demo account" switches to sandbox automatically.
> Compose: `bgc order --action place --category SPOT --symbol BTCUSDT --side buy --orderType market --qty 0.1`, preview with `--dry-run`, confirm, then run.

Skill files: `SKILL.md` (triggers + grammar + write-safety) · `references/commands.md` · `trading-safety.md` · `demo-trading.md` · `error-codes.md`.

**Strengths**:
- Real exchange-grade **intent → multi-step tool chain → confirm → execute** pattern
- Write-safety (`--dry-run` / `--confirm` / `[CAUTION]`) is production-critical for a trading bot
- Bilingual trigger phrases; discover-first API workflow

**Fit for perp multi-symbol bot**: Very high for **execution layer policy**: always preview, require confirm on size/leverage, support demo mode, structured error recovery. Model "check positions → condition → place" for multi-symbol decision loops.

---

## 7. Bybit Exchange Skills — natural-language trading skill pack

**URL**: https://github.com/bybit-exchange/skills  
**Raw skill**: https://raw.githubusercontent.com/bybit-exchange/skills/main/SKILL.md

**Prompt excerpt / structure** (frontmatter + skill body):

```yaml
name: bybit-trading
description: Bybit AI Trading Skill — Trade on Bybit using natural language.
  Covers spot, derivatives, earn… Works with Claude, ChatGPT, OpenClaw…
metadata:
  version: 1.7.5
  author: Bybit
```

> # Bybit Trading Skill — Trade on Bybit  
> (spot, derivatives, earn modules; installable from a single URL)

**Strengths**:
- Official exchange **skill-pack distribution** (URL-installable, versioned)
- Natural-language → derivatives module coverage (perp-relevant)
- Comparable to Bitget: skill = judgment + tool map, not just a CLI

**Fit for perp multi-symbol bot**: High as a **skill-pack packaging model** for the bot's own "how to trade" instructions. Reuse the idea: one SKILL.md with trigger phrases, instrument map, safety rules for perps.

---

## 8. TradingAgents — Market Analyst system prompt (indicator selection)

**URL**: https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/tradingagents/agents/analysts/market_analyst.py  
**Repo**: https://github.com/TauricResearch/TradingAgents

**Prompt excerpt** (system_message):

> You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy.
>
> Moving Averages: `close_50_sma` (medium-term trend…), `close_200_sma` (long-term…), `close_10_ema` (responsive short-term…)
> MACD Related: `macd`, `macds`, `macdh` …
> Momentum: `rsi` … Usage: Apply 70/30 thresholds…
> Volatility: `boll`, `boll_ub`, `boll_lb`, `atr` … Usage: Set stop-loss levels and adjust position sizes based on current market volatility.

**Strengths**:
- **Bounded choice** (pick ≤8 complementary indicators) — anti-hallucination / anti-redundancy
- Each indicator carries Usage + Tips (when NOT to trust it)
- Multi-agent debate (bull/bear researchers + risk + portfolio manager) upstream of final decision

**Fit for perp multi-symbol bot**: High for the **analysis stage** of a multi-symbol scanner: per symbol pick a small indicator set conditioned on regime; ATR tip is directly reusable for perp SL sizing. Pair with structured output (TradingAgents `PortfolioDecision` pattern).

---

## 9. Algorier — 12-part Trading Strategy Prompt Framework

**URL**: https://algorier.com/blog/trading-strategy-prompt/

**Prompt excerpt / structure** (framework table + example):

> **Weak:** "Create a crypto trading strategy."
>
> **Strong:** "Create a long-only Bitcoin trend-following strategy for the 4-hour chart. Confirm each signal at bar close and enter at the next bar open. Define measurable entry and exit rules, risk 0.5% of equity per trade, and allow only one open position. Specify which metrics should be measured during backtesting, but do not invent performance results. State every assumption and ask clarifying questions if any rule is ambiguous."

**12 components**: Market/Instrument · Direction · Timeframe · Entry Conditions · **Signal Confirmation Timing** · Exit Conditions · Execution Method · Risk & Position Sizing · Portfolio/Operating Constraints · Backtest Scope & Costs · Evaluation Metrics · Clarification & Output Instructions.

**Strengths**:
- Best **spec completeness checklist** found in exchange/bot-adjacent research
- Explicitly separates signal confirmation from execution (anti look-ahead)
- "Do not invent performance results" guardrail

**Fit for perp multi-symbol bot**: Excellent as the **canonical strategy-spec schema**. Multi-symbol extension: loop Market=each symbol, share Risk/Portfolio constraints, per-symbol Entry/Exit. Add funding, leverage, mark-price vs last-price.

---

## 10. TradeAlgo — Risk Assessment + Strategy Evaluation prompt templates

**URL**: https://www.tradealgo.com/trading-guides/ai-trading/chatgpt-trading-prompts-guide

**Prompt excerpts**:

**Risk assessment**:
> I am considering the following trade: Position: [LONG/SHORT] [TICKER] at $[PRICE] · Position size: [X]% of portfolio · Time horizon: [DURATION] · Thesis: [YOUR THESIS]
> Provide a comprehensive risk assessment: THESIS RISKS · POSITION SIZING RISK (ATR context) · TIMING RISKS · ALTERNATIVE SCENARIOS (best/base/worst/black swan) · RISK MITIGATION (stop-loss, hedges, add/reduce/exit rules)

**Strategy evaluation**:
> Evaluate the following trading strategy for robustness… LOGICAL CONSISTENCY · POTENTIAL BIASES (survivorship, lookahead, data snooping) · REGIME DEPENDENCY · PRACTICAL CONCERNS · IMPROVEMENTS

**Strengths**:
- Structured section headers force complete risk coverage
- Black-swan + regime dependency language is useful for perp liquidation awareness
- "Do not infer or hallucinate data points" instruction in related prompts

**Fit for perp multi-symbol bot**: High for a **pre-trade risk gate prompt** (run before any signal is emitted). Adapt "position size % / ATR" to leverage + margin ratio + liquidation distance.

---

## Honorable mentions (product context, less literal prompt text)

| Source | URL | Note |
|--------|-----|------|
| **Bybit TradeGPT** | https://www.bybit.global/en/learn/bybit-guide/what-is-bybit-tradegpt | AI assistant with recommended questions, Lens/Daily Pulse modules, one-click derivatives/grid recs; shows exchange UX for prompt+data fusion |
| **Bitget GetAgent Playbook** | https://u.today/bitget-expands-ai-trading-with-getagent-playbook | "Ready strategy templates without building complex prompts from scratch"; Agent Harness orchestrates analysis → execution → risk |
| **GateAI Trading Bot guide** | https://www.gate.com/blog/gateai-trading-bot-guide-run-bots-stop-loss-settings-smart-crypto-trading-strategies-risk-management | Gate's smart bot + stop-loss/risk ops narrative |
| **DocsBot Bybit beginner prompt** | https://docsbot.ai/prompts/business/bybit-beginner-trading-advice | Same Steps/Output/Notes skeleton; conservative small-cap advice |
| **3Commas AI Assistant / QuantPilot** | https://3commas.io | Product surface: AI builds/tests/optimizes strategies end-to-end (prompt text not publicly exposed) |
| **OKX agent-trade-kit** | https://github.com/okx/agent-trade-kit | MCP server for spot/swap/futures/options/grid — tool interface rather than strategy prompt |
| **FinRobot agent_library.py** | https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/finrobot/agents/agent_library.py | Role profiles (Market_Analyst, Expert_Investor) usable as persona prompts |

---

## Cross-cutting takeaways for a perp multi-symbol bot

1. **Spec, not wish**: pair + timeframe + direction + entry/exit + TP/SL + risk + output format (Cryptohopper, Algorier, XCryptoBot).
2. **Constrain output shape** ("table of exact bot settings" / structured JSON) so LLMs emit machine-usable signals.
3. **Signal vs execution timing** must be explicit (confirm at close, enter next open) — Algorier.
4. **Safety rails**: paper/dry-run first, [CAUTION] confirm on writes, no invented performance (Bitget skill, Cryptohopper, Algorier).
5. **Bounded analysis**: pick ≤N complementary indicators per regime (TradingAgents).
6. **Risk gate as separate prompt**: thesis/size/timing/scenarios/mitigation before emit (TradeAlgo).
7. **Multi-symbol**: promote XCryptoBot's slot template into a batch schema; share portfolio risk limits across symbols.

---

## Suggested hybrid prompt skeleton (synthesized)

```text
You are a crypto PERP signal engine for multiple symbols.
For each symbol {S} on timeframe {TF}:
1. Select ≤6 complementary indicators for current regime (trend/chop/vol).
2. Define measurable entry/exit; confirm signal at bar close; execute next bar open.
3. Size with risk ≤{R}% equity per trade; cap leverage ≤{L}x; set SL/TP from ATR.
4. Output JSON only: {symbol, side, reason, entry, sl, tp, size_pct, leverage, confidence, regime}.
Constraints: one open position per symbol; max {N} symbols; no invented fills/PnL;
if ambiguous, ask or state assumptions; default to "no trade".
```

Evidence files: `prompts-exchanges-evidence.md`, `prompts-exchanges-evidence2.md`, `prompts-exchanges-evidence3.md`, `prompts-exchanges-raw.json`, `prompts-exchanges-raw2.json`, `prompts-exchanges-raw3.json`.
