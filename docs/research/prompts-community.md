# Community Trading Strategy Prompts — Research Notes

> Sources: GitHub prompt packs, awesome-chatgpt-prompts, Tradecraft skill collection, openfinclaw-cli, prompTrading, PromptFolio. Reddit / 知乎 / 币安广场 were blocked (403 / captcha) during this session; content below is from accessible primary sources.

---

## 1. Financial Analyst (awesome-chatgpt-prompts)

- **URL**: https://github.com/f/prompts.chat (formerly f/awesome-chatgpt-prompts) → `PROMPTS.md`
- **Excerpt**:
  > "Want assistance provided by qualified individuals enabled with experience on understanding charts using technical analysis tools while interpreting macroeconomic environment prevailing across world consequently assisting customers acquire long term advantages requires clear verdicts therefore seeking same through informed predictions written down precisely! First statement contains following content- Can you tell us what future stock market looks like based upon current conditions?"
- **Why excellent**: Canonical role-play prompt with 171k-star provenance. Forces technical-analysis + macro dual lens and demands "clear verdicts" (no wishy-washy output). Good base for a trend-following analyst agent.
- **Risk notes**: Prompt is vague on risk framing — add explicit stop-loss / position-size constraints when adapting. "Long term advantages" may bias toward holding through drawdowns.

---

## 2. Investment Manager (awesome-chatgpt-prompts)

- **URL**: https://github.com/f/prompts.chat → `PROMPTS.md`
- **Excerpt**:
  > "Seeking guidance from experienced staff with expertise on financial markets, incorporating factors such as inflation rate or return estimates along with tracking stock prices over lengthy period ultimately helping customer understand sector then suggesting safest possible options available where he/she can allocate funds depending upon their requirement & interests! Starting query - What currently is best way to invest money short term prospective?"
- **Why excellent**: Encodes multi-factor allocation logic (inflation, returns, sector, risk appetite). Useful as a risk-manager / portfolio-allocation prompt skeleton.
- **Risk notes**: "Safest possible options" is subjective — pin down concrete risk metrics (max DD, VaR, Kelly fraction). Short-term framing may encourage overtrading.

---

## 3. Accountant / Risk Manager (awesome-chatgpt-prompts)

- **URL**: https://github.com/f/prompts.chat → `PROMPTS.md`
- **Excerpt**:
  > "I want you to act as an accountant and come up with creative ways to manage finances. You'll need to consider budgeting, investment strategies and risk management when creating a financial plan for your client. In some cases, you may also need to provide advice on taxation laws and regulations in order to help them maximize their profits."
- **Why excellent**: Explicitly names "risk management" + "budgeting" as core duties. Good scaffold for a dedicated risk-manager agent that audits trade plans.
- **Risk notes**: Tax/regulatory advice is jurisdiction-specific — strip that for a pure trading-risk bot. "Creative ways" invites hallucinated strategies; constrain to validated risk formulas.

---

## 4. Tradecraft Strategy Skills Pack (GitHub)

- **URL**: https://github.com/mahmoud20138/Tradecraft
- **Excerpt** (from `COMMANDS.md` skill descriptions):
  - `breakout-strategy-engine`: "Pre-built breakout strategy templates — volatility squeeze detection, range breakout, momentum breakout with confirmation filters."
  - `mean-reversion-engine`: "Mean reversion strategy templates — Bollinger bounce, RSI extreme fade, Z-score reversion with regime guard. ONLY use in ranging regimes."
  - `grid-trading-engine`: "Systematic grid trading — place buy/sell orders at fixed intervals across a price range. Works with market-regime-classifier (best in RANGING regimes)."
  - `asian-session-scalper`: "Tokyo session low-volatility scalping setups — range-bound strategies for the quietest session."
  - `jdub-price-action-strategy`: "3-step price action framework: Direction, Location, Execution. Three-bar confirmation entry. 9:30 AM NY open M5 scalping variant."
  - `risk-and-portfolio`: "Complete trading risk management, portfolio construction, performance tracking — position sizing, stop loss placement, drawdown management, Kelly criterion, ATR-based sizing."
  - `market-regime-classifier`: "ML-powered market regime classification — trending, ranging, volatile, quiet. Automatically adapts strategy selection."
  - `capitulation-mean-reversion`: "Lance Breitstein's capitulation mean reversion — 7-variable checklist, Right Side of the V entry, slope analysis for waterfalls."
- **Why excellent**: 104 trading skills with trigger-keyword YAML frontmatter, executable code blocks, and cross-references. Covers **all six required categories** (trend via ICT/structure, mean-rev, breakout, grid, scalping, risk manager). Mode calibration table maps risk% / min R:R / setup grade to timeframes — directly reusable.
- **Risk notes**: Skills assume MT5/yfinance data pipeline. ICT/SMC concepts are discretionary and contested. Regime guard on mean-reversion/grid is critical — running them in trends is the classic blow-up mode.

---

## 5. openfinclaw-cli Example Prompts (GitHub)

- **URL**: https://github.com/mirror29/openfinclaw-cli
- **Excerpt** (README "Example Prompts"):
  > **Strategy generation**
  > - "Design a momentum strategy on US mega-cap tech. Backtest 2y. Tell me where it breaks."
  > - "Write a mean-reversion strategy on BTC and show drawdown behavior through 2022."
  > - "A-shares 沪深 300 日内轮动策略，年化目标 15%，最大回撤 < 10%。"
  >
  > **Backtest & stress-test**
  > - "Backtest a 50/200 SMA crossover on SPY from 2015. Include costs and slippage."
  > - "Stress-test my forked strategy against the 2020 and 2022 crashes."
- **Why excellent**: Copy-paste prompts with built-in validation loop ("tell me where it breaks", "show drawdown behavior through 2022"). Explicit cost/slippage and stress-test framing. Chinese A-share example shows cross-market applicability.
- **Risk notes**: Backtest performance ≠ live results. The "annualized 15%, max DD < 10%" target is a constraint, not a guarantee — models may overfit to hit it.

---

## 6. prompTrading Prompt-to-Strategy (GitHub)

- **URL**: https://github.com/xlabsg/prompTrading
- **Excerpt** (README):
  > "Build an intraday breakout strategy on BTC/USDT 15m candles with a volume surge filter, 2.5x ATR trailing stop, and dynamic profit targets based on recent swing highs."
  >
  > Defense-in-Depth Risk Engine: 9 Invasive Pre-Trade Checks (max order size, leverage caps, price sanity, balance validation), Profit-Activated Trailing Stops, Dynamic TP/SL (Support/Resistance + ATR volatility-based triggers).
- **Why excellent**: Single-sentence prompt that packs timeframe, entry filter, stop mechanism, and TP logic — ideal template shape for a gate-signal-bot. Risk engine checklist is directly transplantable as a risk-manager prompt.
- **Risk notes**: LLM-generated strategies need sandboxed backtesting before live. API keys must never have withdrawal permission. Crypto leverage caps are exchange-specific.

---

## 7. PromptFolio / ai-hedge-fund Agents (GitHub)

- **URL**: https://github.com/Magmute/PromptFolio
- **Excerpt**:
  > "Prompt-Based Trading: Configure strategies using only prompts — no coding required. Built-in AI Agents: Pre-configured trading agents migrated from ai-hedge-fund to automate investment analysis and portfolio management."
- **Why excellent**: Demonstrates pure-prompt strategy configuration pattern. ai-hedge-fund lineage means the agent prompts are battle-tested for portfolio construction.
- **Risk notes**: Single-commit repo (immature). MongoDB + FastAPI stack is heavy for a signal bot — harvest the prompt patterns, not the infra.

---

## Summary: Prompt Templates by Category

| Category | Primary Source | Template Name |
|----------|---------------|---------------|
| **Trend following** | Tradecraft | `ict-smart-money`, `market-structure-bos-choch`, `elliott-wave-engine` |
| **Mean reversion** | Tradecraft | `mean-reversion-engine` (Bollinger/RSI/Z-score + regime guard), `capitulation-mean-reversion` |
| **Breakout** | Tradecraft / prompTrading | `breakout-strategy-engine`, `dan-zanger-breakout-strategy`, prompTrading BTC 15m prompt |
| **Grid** | Tradecraft | `grid-trading-engine` (ranging-regime only) |
| **Scalping** | Tradecraft | `asian-session-scalper`, `jdub-price-action-strategy` (M5/OR break-retest) |
| **Risk manager** | awesome-gpt-prompts / Tradecraft / prompTrading | `Accountant` prompt, `risk-and-portfolio`, `real-time-risk-monitor`, prompTrading 9-point pre-trade checks |

---

## Access Limitations (this session)

| Source | Status |
|--------|--------|
| Reddit (r/ChatGPTTrading, r/algotrading) | 403 Blocked |
| 知乎 (zhihu.com) | 403 Blocked |
| 币安广场 (Binance Square) | Human verification wall |
| Baidu SERP | Captcha |
| Bing CN SERP | Returned generic results, poor for English prompt queries |

Recommend retrying Reddit/知乎 via a logged-in browser session or cached mirrors in a follow-up pass.
