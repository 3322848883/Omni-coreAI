# LLM Trading Agent PROMPTS — Open-Source Systems Survey

Research date: 2026-09-25  
Target use: Gate.io perpetual futures multi-chip (multi-symbol) signal bots  
Local signal contract reference: `templates/standard-signal.json` (action/symbol/tp/sl/leverage/meta)

---

## 1. TradingAgents (TauricResearch)

**URL**: https://github.com/TauricResearch/TradingAgents  
**Source files**: `tradingagents/agents/analysts/market_analyst.py`, `trader/trader.py`, `schemas.py`

### Prompt excerpt — Market Analyst (system)

> You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. […] Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). […] call `get_stock_data` first […]. Then use `get_indicators` with the specific indicator names. Before writing the final report, call `get_verified_market_snapshot` for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices. Write a very detailed and nuanced report […] Make sure to append a Markdown table at the end of the report.

Collab preamble (shared across analysts):

> You are a helpful AI assistant, collaborating with other assistants. Use the provided tools to progress towards answering the question. If you are unable to fully answer, that's OK; another assistant with different tools will help where you left off. […] Report what your tools support; another agent decides the trade.

### Prompt excerpt — Trader (system + output contract)

> You are a trading agent analyzing market data to make investment decisions. Based on your analysis, provide a specific recommendation to buy, sell, or hold. Ground concrete price levels (entry, stop-loss, position sizing) in the technical market report's price structure — current price, support/resistance, ATR, and volatility […] State entry price and stop-loss as absolute price levels in the instrument's quote currency (for example 189.5), never a percentage or a range; convert a percentage distance to the price level it implies, or omit the field if you cannot state a number.

### JSON schema (Pydantic → provider-native structured output)

```json
{
  "action": "Buy | Hold | Sell",
  "reasoning": "2-4 sentences, anchored in analyst reports + research plan",
  "entry_price": 189.5,
  "stop_loss": 172.0,
  "position_sizing": "5% of portfolio"
}
```

Portfolio Manager schema (5-tier): `rating ∈ {Buy, Overweight, Hold, Underweight, Sell}`, `executive_summary`, `investment_thesis`, `price_target: float|null`, `time_horizon: str|null`.  
Sentiment schema: `overall_band` (6-tier), `overall_score 0-10`, `confidence ∈ {low,medium,high}`, `narrative`.

### Strengths
- Hard grounding rules against hallucinated prices (verified snapshot as source of truth).
- Absolute-price fields with anti-percentage coercion (`_coerce_optional_float`) — survives LLM "15%" mistakes.
- Layered decision: analysts → research manager → trader → portfolio manager.
- Field descriptions double as model instructions (saves prompt tokens).

### Fit for Gate.io perp multi-chip bots
**High.** Absolute entry/SL maps 1:1 to `standard-signal.json` `price`/`sl`. 5-tier rating can map to chip weight (Overweight = larger `size_pct`). Multi-analyst split is natural for multi-chip (per-symbol analyst fan-out). Need to add leverage/margin_mode/tp_type fields; ground levels in mark-price ATR to match `trigger_price_type: mark`.

---

## 2. FinRobot (AI4Finance-Foundation)

**URL**: https://github.com/AI4Finance-Foundation/FinRobot  
**Source files**: `finrobot/agents/prompts.py`, `finrobot/agents/agent_library.py`

### Prompt excerpt — Leader system message

> You are the leader of the following group members: {group_desc}  
> As a group leader, you are responsible for coordinating the team's efforts to achieve the project's objectives. You must ensure that the team is working together effectively and efficiently.  
> - Summarize the status of the whole project progress each time you respond.  
> - End your response with an order to one of your team members to progress the project, if the objective has not been achieved yet.  
> - Orders should follow the format: "[<name of staff>] <order>".  
> - Orders need to be detailed, including necessary time period information, stock information or instruction from higher level leaders.  
> - Make only one order at a time.  
> - After receiving feedback from a team member, check the results of the task, and make sure it has been well completed before proceeding to the next order.  
> Reply "TERMINATE" in the end when everything is done.

### Prompt excerpt — Role / Order templates

> As a {title}, your responsibilities are as follows: {responsibilities}  
> Reply "TERMINATE" in the end when everything is done.

> Follow leader's order and complete the following task with your group members: {order}  
> For coding tasks, provide python scripts and executor will run it for you. Save your results or any intermediate data locally and let group leader know how to read them. DO NOT include "TERMINATE" in your response until you have received the results from the execution of the Python scripts. If the task cannot be done currently or need assistance from other members, report the reasons or requirements to group leader ended with TERMINATE.

### Prompt excerpt — Expert Investor role card

> Role: Expert Investor / Department: Finance / Primary Responsibility: Generation of Customized Financial Analysis Reports  
> […] Analytical Precision: Employ meticulous analytical prowess to interpret financial data, identifying underlying trends and anomalies.  
> Effective Communication: Simplify and effectively convey complex financial narratives […]  
> Adherence to Excellence: Maintain the highest standards of quality and integrity in report generation […]

Market Analyst profile (tool-bound): *"collect necessary financial information and aggregate them based on client's requirement. For coding tasks, only use the functions you have been provided with."* + toolkits (FinnHub company profile/news/basic financials, YFinance stock data).

### Strengths
- Explicit one-order-at-a-time protocol + TERMINATE handshake → deterministic orchestration.
- Role library with bound toolkits prevents tool sprawl.
- Coder-executor split (agent writes script, executor runs) good for on-box data pulls.

### Fit for Gate.io perp multi-chip bots
**Medium-high for orchestration shell, low as signal prompt.** Leader/order pattern is excellent for a multi-chip coordinator (one order per chip per cycle). TERMINATE is too loose for live trading JSON — replace with structured signal emit. Role-card style works for chip specialists (breakout / mean-revert / funding-rate).

---

## 3. FinMem (pipiku915 / Yu et al., arXiv 2311.13743)

**URL**: https://github.com/pipiku915/FinMem-LLM-StockTrading  
**Source files**: `puppy/prompts.py`, `puppy/reflection.py`

### Prompt excerpt — Test-time decision prompt (system/user)

> Given the information, can you make an investment decision? Just summarize the reason of the decision.  
> please consider only the available short-term information, the mid-term information, the long-term information, the reflection-term information.  
> please consider the momentum of the historical stock price.  
> When cumulative return is positive or zero, you are a risk-seeking investor.  
> please consider how much share of the stock the investor holds now.  
> You should provide exactly one of the following investment decisions: buy or sell.  
> When it is really hard to make a 'buy'-or-'sell' decision, you could go with 'hold' option.  
> You also need to provide the id of the information to support your decision.  
> ${investment_info}  
> ${gr.complete_json_suffix_v2}

Risk-preference flip (commented policy, active in design):

> When cumulative return is positive or zero, you are a risk-seeking investor, positive information have a greater influence on your investment decisions […] But when cumulative return is negative, you are a risk-averse investor, negative information have a greater influence […]

Momentum injection: *"The cumulative return of past 3 days for this stock is negative / zero / positive."*

### JSON schema (Guardrails + Pydantic)

```json
{
  "investment_decision": "buy | sell | hold",
  "summary_reason": "why the trader drove such a decision",
  "short_memory_index": [{"memory_index": 0}],
  "middle_memory_index": [{"memory_index": 0}],
  "long_memory_index": [{"memory_index": 0}],
  "reflection_memory_index": [{"memory_index": 0}]
}
```

Memory-index fields are validated with `ValidChoices(id_list, on_fail="reask")` — citations must exist.

### Strengths
- Layered memory (short/mid/long/reflection) with mandatory citation IDs → auditability.
- Risk-seeking ↔ risk-averse switch from cumulative PnL is a cheap, effective regime filter.
- Guardrails reask loop for schema repair.
- Sentiment/momentum explanations embedded as pedagogy so small models follow.

### Fit for Gate.io perp multi-chip bots
**High.** Citation-of-memory-ids is ideal for multi-chip signal QA ("which chip/factor drove this?"). PnL-based risk preference maps to per-chip equity curve (losing chip → shrink size / need stronger confirmation). Map decision to `action: open_long/open_short/close/hold` and push memory IDs into `meta.reasoning` / `meta.signal_id`.

---

## 4. FinGPT-Forecaster (AI4Finance-Foundation)

**URL**: https://github.com/AI4Finance-Foundation/FinGPT/tree/master/fingpt/FinGPT_Forecaster  
**Source file**: `prompt.py` (and README "Prompts used")

### Prompt excerpt — SYSTEM_PROMPT

> You are a seasoned stock market analyst. Your task is to list the positive developments and potential concerns for companies based on relevant news and basic financials from the past weeks, then provide an analysis and prediction for the companies' stock price movement for the upcoming week. Your answer format should be as follows:  
> [Positive Developments]: 1. ...  
> [Potential Concerns]: 1. ...  
> [Prediction & Analysis]: ...

### Prompt excerpt — user template (structure)

> [Company Introduction]: {name} is a leading entity in the {finnhubIndustry} sector. […] As of today, {name} has a market capitalization of {marketCapitalization} […]  
> From {startDate} to {endDate}, {name}'s stock price {increase/decrease} from {startPrice} to {endPrice}. Company news during this period are listed below:  
> [Headline]: ... / [Summary]: ...  
> Some recent basic financials of {name}, reported at {date} […] [Basic Financials]: {attr}: {value}  
> Based on all the information before {curday}, let's first analyze the positive developments and potential concerns for {symbol}. Come up with 2-4 most important factors respectively and keep them concise. Most factors should be inferred from company-related news. Then make your prediction of the {symbol} stock price movement for next week ({period}). Provide a summary analysis to support your prediction.

Wrapped in Llama-2 chat format: `B_INST + B_SYS + SYSTEM_PROMPT + E_SYS + YOUR_PROMPT + E_INST`.

Optional sentiment overlay fields injected into prompt: average sentiment score, source coverage, source alignment, per-source sentiment/activity counts (Reddit/X/News/Polymarket).

### Strengths
- Rigid section headers (`[Positive Developments]` / `[Potential Concerns]` / `[Prediction & Analysis]`) — parseable without JSON schema.
- "2-4 most important factors" limits rambling; forces prioritization.
- Concise factor requirement ("keep them concise") good for multi-chip latency.
- Disclaimers + no-lookahead framing (train on data before {curday}).

### Fit for Gate.io perp multi-chip bots
**Medium-high as the "narrative signal" layer.** Section headers can become a pre-JSON brief per chip. Replace "next week" with the bot timeframe (15m/1h/4h). Add funding rate / open interest / basis as "basic financials". Map Prediction to direction, then a second structured pass emits `standard-signal.json`. Works well as chip-level news/momentum analyst feeding the trader.

---

## 5. FinCon (Yu et al., arXiv 2407.06567)

**URL**: https://arxiv.org/html/2407.06567v1  
**Also**: conceptual sibling of FinMem; manager-analyst hierarchy + verbal reinforcement

### Prompt excerpt — Profiling Module role assignment

Manager Agent:
> You are an experienced trading manager in the investment firm […]  
> Your responsibilities are to consolidate investment insights from analysts and make trading actions on {asset symbols} […]

Analyst Agents:
> You are the investment analysts for news / market data / Form 10-K (Q) / ECC audio recording […]  
> Your responsibilities are to distill investment insights and other indicators like financial sentiment for {asset symbols} […]

### Role I/O contract (from Figure 3 modular design)

- Manager Action Module: `Conduct` (trading actions) + `Reflect` (trading reasons and analyst contribution assessment).
- Manager Perception: perceives analyst insights + risk signal + trajectory-level investment belief updates from risk-control; sends feedback to analysts about contribution to P&L.
- Analyst Memory: Working (Observation → Retrieval → Distillation) → Procedural (distilled insights, financial sentiment, recommended actions).
- Risk metrics used for risk-control: PnL, VaR, CVaR, Cumulative Return, Sharpe, Max Drawdown.
- Memory retrieval score = relevancy (cosine) + importance (forgetting-curve decay); critical memory IDs get +5 importance via Guardrails validation.

### Strengths
- Real firm hierarchy (manager consolidates, analysts distill) — proven comms pattern.
- Trajectory-level verbal reinforcement (conceptual beliefs update after episodes) — self-improving prompts.
- Quant risk-control component (VaR/CVaR) not just "stop loss".
- Feedback loop to analysts on P&L contribution reduces noisy agents.

### Fit for Gate.io perp multi-chip bots
**High for architecture, medium for verbatim prompt.** Manager = multi-chip portfolio manager (cross-chip exposure, net delta). Analysts = per-source (funding/OI, CVD, news, on-chain) rather than per-chip. Risk-control beliefs can encode "chip X keeps false-breaking on low funding — require CVD confirm". Map risk-control output to per-chip `size_pct` / `leverage` caps. Paper does not ship verbatim JSON schema — you must define one (reuse TradingAgents schemas).

---

## 6. FinAgent (effective-p port of Zhang et al. multimodal foundation agent)

**URL**: https://github.com/effective-p/FinAgent  
**Paper**: arXiv 2402.18485 (multimodal, tool-augmented trading agent)  
**Source file**: `finagent/modules/decision_making.py`

### Pipeline

DataFetcher → MarketIntelligence → LowLevelReflection → HighLevelReflection → DecisionMaking → Portfolio  
(+ MemoryStore ChromaDB 3 collections: MI / LLR / HLR; trader preference aggressive/moderate/conservative)

### Prompt excerpt — Decision-making (structure; Korean source translated)

> You are asked to make an investment decision on {target_date}.  
> [Symbol] {symbol}  
> [Trading Preference] {preference_text}   // aggressive | moderate | conservative  
> [Current Portfolio] cash / position / total_value  
> [Technical signals] {tech_signals}   // MACD, KDJ, RSI, ZMR, BB  
> [Market Intelligence] latest: {mi_latest}; past: {mi_past}  
> [Low-Level Reflection — short/medium/long term] {llr_short} {llr_medium} {llr_long}  
> [High-Level Reflection — past decision evaluation] reasoning: {hlr_reasoning}; improvement: {hlr_improvement}  
> [Fundamentals — PER/PBR/dividend guidance] {fundamental_guidance}  
> Constraints: if cash > previous cash → favor BUY; if position == 0 → no SELL.  
> action must be exactly one of BUY, SELL, HOLD.  
> Reply in this XML structure:
> ```
> <output>
>   <analysis>…synthesize MI/LLR/HLR/tech/fundamental → market view (3-5 sentences)</analysis>
>   <action>BUY 或 SELL 或 HOLD</action>
>   <reasoning>key decision rationale (2-3 sentences)</reasoning>
> </output>
> ```

Parsed via custom XML parser; invalid action defaults to HOLD.

### Strengths
- Multimodal reflection stack (chart vision + text MI + technical injection) before the final decide call.
- Trader preference knob (aggressive/moderate/conservative) as a first-class prompt slot.
- Explicit portfolio-state constraints (cash/position) inside the prompt.
- Safe default: unrecognized action → HOLD (fail-closed).
- XML output is easy to parse and tolerate.

### Fit for Gate.io perp multi-chip bots
**High.** Preference knob maps to chip risk profile (memecoins = conservative leverage, BTC = moderate). Portfolio-state block should include open perp positions + unrealized PnL. Fail-closed HOLD is exactly what live bots need. Convert XML to JSON `standard-signal.json` (add `leverage`, `margin_mode`, `tp/sl`, `trigger_price_type`). HighLevelReflection maps to post-trade journal that updates chip prompts.

---

## Cross-system patterns worth stealing (for Gate.io perp multi-chip)

| Pattern | Source | How to apply |
|---|---|---|
| Absolute price levels, never % | TradingAgents | `price`, `sl`, `tp` as quote-currency numbers; coerce "15%" → null |
| Verified-snapshot grounding | TradingAgents | Mark-price / last-Kline tool is source of truth; flag conflicts |
| Layered memory + citation IDs | FinMem | Chip factor IDs in `meta.reasoning`; reask if invalid |
| PnL-based risk-seeking/averse flip | FinMem | Losing chip → stricter confirm + smaller `size_pct` |
| Manager-analyst hierarchy + risk-control | FinCon | Portfolio-level manager caps net exposure across chips |
| Structured section headers | FinGPT | Pre-parse brief: [Positives]/[Concerns]/[Prediction] |
| One-order-at-a-time + TERMINATE | FinRobot | Multi-chip coordinator emits one signal per cycle per chip |
| Fail-closed HOLD + preference knob | FinAgent | Invalid output → hold; per-chip risk profile slot |
| 5-tier rating → size | TradingAgents | Buy/Overweight/Hold/Underweight/Sell → `size_pct` ladder |

## Suggested Gate.io multi-chip signal JSON (synthesis)

```json
{
  "action": "open_long | open_short | close | hold",
  "symbol": "BTC_USDT",
  "size_pct": 5.0,
  "price": 70000.0,
  "tp": 73000.0,
  "sl": 69000.0,
  "leverage": 5,
  "margin_mode": "cross",
  "meta": {
    "strategy": "multi-chip-v1",
    "chip": "breakout|meanrev|funding",
    "confidence": 0.85,
    "rating": "Overweight",
    "memory_ids": [12, 45],
    "risk_regime": "risk-seeking|risk-averse",
    "timeframe": "15m",
    "reasoning": "…"
  }
}
```

---

## Sources actually opened (browser)

1. https://github.com/TauricResearch/TradingAgents — market_analyst.py, trader.py, schemas.py (raw)
2. https://github.com/AI4Finance-Foundation/FinRobot — prompts.py, agent_library.py
3. https://github.com/pipiku915/FinMem-LLM-StockTrading — puppy/prompts.py, puppy/reflection.py
4. https://github.com/AI4Finance-Foundation/FinGPT/tree/master/fingpt/FinGPT_Forecaster — prompt.py + README
5. https://arxiv.org/html/2407.06567v1 — FinCon manager/analyst roles, risk-control
6. https://github.com/effective-p/FinAgent — decision_making.py, pipeline README

Local contract: `C:/Users/w6485/Desktop/测试/OmniAlpha/templates/standard-signal.json`
