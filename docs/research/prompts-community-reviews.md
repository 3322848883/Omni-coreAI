# Research notes - TradingAgents SERP (Bing CN)
Date: 2026-09-25
Query: TradingAgents TauricResearch review github experience

Results:
1. github.com/hsliuping/TradingAgents-CN - 基于多智能体LLM Community Edition (Apache-2.0 core + proprietary)
2. zhihu.com zhuanlan - "用 TradingAgents 跑了一个月实盘，说句大实话" (2026-03-22) - KEY REVIEW
   Quote snippet: "TradingAgents 是一个很有意思的研究框架，代码质量不错，架构设计也清晰。但它本质上是在用 LLM 做投资决..."
3. github.com/TauricResearch/TradingAgents - official repo
4. tradings.cn - TradingAgents 多智能体 LLM 金融交易框架 (2026-07-07) - "面向研究的多智能体交易框架"
5. zhihu.com - "拆解TradingAgents，AI量化交易实践" (2026-04-13)
6. tradingagents-cn.com - Chinese edition
7. tauricresearch.github.io/TradingAgents - project page
8. tencent cloud - "TradingAgents 是GitHub上6.9万星标的多智能体金融交易框架" (2026-05-05) - STAR COUNT ~69k claim
9. aibars.net - TradingAgents-CN

TODO: open zhihu live-trading review, github repo for stars/issues

## TradingAgents evidence so far
- Zhihu article (blocked 403): "用 TradingAgents 跑了一个月实盘，说句大实话" (2026-03-22)
  URL: https://zhuanlan.zhihu.com/p/2018778915422912641
  Bing snippet quote (ZH): "TradingAgents 是一个很有意思的研究框架，代码质量不错，架构设计也清晰。 但它本质上是在用 LLM 做投资决…"
  Interpretation: praise for code quality/architecture, skepticism about LLM-based investment decisions.
- Tencent cloud (2026-05-05): "TradingAgents是GitHub上6.9万星标的多智能体金融交易框架" (~69k stars claim)
- tradings.cn: "面向研究的多智能体交易框架" (research-oriented)

### TradingAgents GitHub (TauricResearch/TradingAgents)
- URL: https://github.com/TauricResearch/TradingAgents
- Stars: 108.6k–109k | Forks: 20.8k | Watchers: 818 | Open issues: 34 | Closed: 463 | PRs: 40 | Discussions: 2
- Releases: 11 (latest v0.5.1), 22 contributors
- README warning (verbatim): "Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return."
- Also: "Even at a fixed temperature, providers do not guarantee byte-identical output across calls"
- Issue themes: technical bugs (Yahoo rate limits, parsers, US-only macro, benchmark_map missing .TW silently scored vs SPY #1392). Almost NO "doesn't make money" complaint threads — community treats it as research/engineering framework.
- Discussions: only 2 threads (API key issues). Little profitability debate on GitHub.
- tradings.cn description: "面向研究的多智能体交易框架" (research-oriented multi-agent trading framework)

## FinMem / FinGPT evidence
### FinMem (pipiku915/FinMem-LLM-StockTrading)
- Stars: 960 | Forks: 196 | Open issues: 8 | Closed: 13
- Paper claims "boosting cumulative investment returns" but README has NO user profitability testimonials
- Issues show REPRODUCIBILITY BARRIERS:
  - #28 "Has anyone figured out how to get the TSLA.pkl dataset?" (data not public)
  - #22 "Instructions on accessing Refinitiv private data" (private data dependency)
  - #26 "question about baseline" (users questioning comparison baselines)
  - #21 "about price_data.parquet"
- Sentiment: academic-interest, users struggle to reproduce

### FinGPT (AI4Finance-Foundation/FinGPT)
- Stars: 21.3k | Forks: 3k | Open issues: 35 | Closed: 274+ | PRs: 17
- README disclaimer (verbatim): "Disclaimer: We are sharing codes for academic purposes under the MIT education license. Nothing herein is financial advice, and NOT a recommendation to trade real money. Please use common sense and always first consult a professional before trading or investing."
- Issue #145 "Fingpt forecastor backtesting" (OPEN, 2024+):
  User erdult: "Backtesting using historical data and showing the performance of forecasting is key to understand the performance of predictions."
  User dayuyang1999: "Same question. Wondering if there is any evaluation method for the FinGPT forecastor."
  Maintainer amayuelas: "We're working on it" (i.e. NO official eval/backtest as of that thread)
- Issue #12: "why do you want to fine-tune a text model on numerical data?" (methodological skepticism)
- Issue #11 "Trying to replicate the work" (reproducibility struggles)
- Spam issues present (payment processing ads) — weak moderation

### FinRobot (AI4Finance-Foundation/FinRobot)
- Stars: 8.1k | Forks: 1.4k | Open issues: 60 | PRs: 16
- README disclaimer (verbatim): "Disclaimer: The codes and documents provided herein are released under the Apache-2.0 license. They should not be construed as financial counsel or recommendations for live trading. It is imperative to exercise caution and consult with qualified financial professionals prior to any trading or investment actions."
- Issues: mostly bugs; #57 "Error when running agent_trade_strategist.ipynb" (trade strategist tutorial broken); #13 "Tutorial not working"
- Sentiment: research/demo platform, not a proven money-maker

## ChatGPT / AI trading signals - Hacker News (goldmine)
Thread: "Show HN: TrendFi – I built AI trading signals that self-optimize" (35 pts, 52 comments, 2025-06-19)
URL: https://news.ycombinator.com/item?id=44291681

Quotes:
1. OP wolfman1 (builder): "Asking ChatGPT 'Should I buy Bitcoin today?' doesn't work well because the LLM doesnt have a set trading strategy to opperate from. In addition, the small context window makes it challenging to fit enough historical data into."
2. bko (skeptic): "I'm very skeptical of systems that claim to help with trading signals. Primarily because if you had a system, then you would use it yourself. Trading is pretty much a zero sum game especially when it comes to signals. If you had a profitable signal and you publicized it, it would disappear almost immediately. If your performance is correct and you're getting 200% per year, why sell the sauce for  a month?"
3. wolfman1 replying: "This is typically true for day trading signal services. I'm very skeptical of them too. I've tried many of them and none worked for me. There are a lot of scams."
4. fasthands9 (ex-finance): "It would be trivially easy to build an algo that beats the market 90% of years but 10% has huge losses... If you sold that, it would appear like its working for awhile and you'd probably have lots of trusted customers by the time it fails. (I'm a bit cynical, I worked in finance briefly, and I realized the fund we were selling to investors was essentially this)"
5. ImPostingOnHN: "This is literally one of the most common hook/marketing lines from investment advice 'businesses'/scammers." / "struggling with the concept of hypothesis/claims testing isn't a reassuring look for someone selling an investment product."

Sentiment: strongly skeptical of any paid AI trading signal service; zero-sum logic; demand for independent verification.

### More HN TrendFi thread quotes (same thread)
6. IAmGraydon: "Of course it's a scam. The product they're selling is hope, not actual trading intelligence." / "if this worked at all, you would have poured all of your resources into investing your own money or starting a fund... Instead, you decided to build a website and sell it to others for  per month... your product is false hope and is a scam, as all of these systems are."
7. helsinki: "If you're making real money trading, you're not telling people about it."
8. dataviz1000 (retail quant learner): "People had blogs and videos about using XGBoost and LSTM with other deep-learning libraries—every single one failed. There's so much BS in the industry, and I got sucked into the rabbit hole."
9. wolfman1 (even the AI-signal builder admits): "Anything that is related to HFT or day trading is almost impossible in my experience as well. The big funds can do it but I dont think retail has a chance with these approaches."
10. mdorazio: "Looking at the Performance page, this doesn't seem that impressive to me. The profits are primarily driven by going long on coins during a crypto boom... I've been burned before by services like this and am not in a hurry to do so again."
11. henning: "The usual response applies: if you have profitable trading signals, why [sell them]..."

## prompTrading (xlabsg/prompTrading) - GitHub
- URL: https://github.com/xlabsg/prompTrading
- Stars: 2 | Forks: 0 | Tags: 0 | Commits: 192 | Issues: 0 open visible
- Tagline: "Prompt in, Alpha out." / "Turn plain-language market ideas into battle-tested, live algorithmic trading strategies."
- Demo claims "+10.05% return, 4.94 Sharpe" (backtest example only)
- README Warning (verbatim): "Trading real money carries significant financial risk. PrompTrading executes real orders against live cryptocurrency exchanges. No strategy generated by an LLM is guaranteed to be profitable. Always validate strategies in paper / demo trading before deploying real capital, and never grant withdrawal permissions to your API keys."
- DISCLAIMER.md (verbatim, key parts):
  "PrompTrading is experimental software designed solely for research, educational, and testing purposes."
  "Strategies generated, modified, or executed via Large Language Models (LLMs) or AI Agents may exhibit non-deterministic behaviors, hallucinations, mathematical inaccuracies, or programming flaws. An AI-generated trading model may execute unintended orders, misinterpret market signals, fail to trigger stop-losses, or suffer severe slippage."
- Sentiment: too new/small for community reviews; authors themselves heavily disclaim profitability

## Tradecraft (mahmoud20138/Tradecraft) - GitHub
- URL: https://github.com/mahmoud20138/Tradecraft
- Stars: 15 | Forks: 4 | Commits: 14 | Tags: 0 | Issues: 0
- Description: "102 Claude Code skills across 7 categories -- trading strategies, Azure, VSCode extensions, AI prompts" / "5 entry-point commands on top of 169 curated AI skills"
- It's a Claude Code plugin (analyze/markets/recommendations/strategies), not a proven trading system
- No strong profitability disclaimer found in README; community too small for user reviews
- Sentiment: unknown / no community evidence

## AI trading losses - HN "My lobster lost \ this weekend"
Thread: https://news.ycombinator.com/item?id=47140773 (59 pts, 28 comments, ~2026-02)
Source article: pashpashpash.substack.com AI-agent memecoin trading story

Community reaction (strong skepticism / mockery):
1. gngoo: "What this whole hype cycle is teaching me is that the great majority of people trying out these tools are idiots."
2. wasmainiac: "This feels like a big PR stunt. Published by a ai tech bro, highly ambiguous, hard to verify, where's the money going? ... Just looked into timeline, it does not add up."
3. nylonstrung: "It's extremely similar to the fake 'agentic' crypto plays a year ago where Goatseus Maximus and stuff supposedly created coins and invested autonomously. Obviously it was BS but it fueled a huge amount of attention and speculation."
4. monster_truck: "More made up bullshit. Where are the transactions?"
5. wasmainiac: "It's at best a staged pr stunt, at worst a pump and dump scheme. Money is probably going circular, it's not real."
6. Jamesbeam: "as soon as any AI story is connected to any form of crypto trading, I consider it automatically a scam. Especially if it has a fabulous story and AI-generated pretty pictures, but no substantial data to show that can be analysed scientifically."
7. jweather: "That \ was actually worth \ when it was cashed out. Why are you calling it \"

## Other HN trading-bot loss quotes
- IgorPartola (crypto arb bot): "when I actually implemented a bot to trade BTC it lost money more often than not"
- cellis: "let me tell you from my experience poorly building a bitcoin trading bot over a few weekends, I lost money (play money) from silly off by one mistakes and not having a proper grasp of the statistics I was using. In this type of situation, only a real quant is likely to make money"

## ai-hedge-fund (virattt/ai-hedge-fund) - GitHub (related LLM trading system)
- Stars: 63.7k | Forks: 11.2k | Open issues: 56 | Closed: 54 | PRs: 115
- README (verbatim): "This is a proof of concept for an AI-powered hedge fund... This project is for educational purposes only and is not intended for real trading or investment. Note: the system does not actually make any trades."
- Disclaimer: "Not intended for real trading or investment / No investment advice or guarantees provided / Creator assumes no liability for financial losses / Past performance does not indicate future results"

### Issue #667 (CLOSED as "not planned" / owner said "Closing as spam")
Title: "Root README overstates paper/live/persistent trading capability versus roadmap status"
User tg12 (verbatim): "The root README foregrounds persistent/paper/live-trading capabilities in a way that overstates what is currently built. Readers coming from the main project page will reasonably infer that these modes are substantially available now, when the roadmap marks major pieces as planned."
Owner virattt response: "Closing as spam."  <-- legitimate capability-accuracy concern dismissed

### Issue #720 (OPEN) - CRITICAL LOOK-AHEAD BIAS FINDING
Title: "Model memory can still leak into backtests through the ticker and filing dates in the snapshot"
User lizhuojunx86 (verbatim, abbreviated): "If the model remembers what that company did next, part of a backtest over that period is recall."
"I measured how much current models remember... 3,708 announcements from 250 S&P 500 companies, pre-registered. On 24 September 2026, 10 of 12 models on OpenRouter showed recall: GPT-6 Astra 0.904, Gemini 3.8 Flash 0.799, Claude Opus 5.5 0.766, GPT-6 Sol 0.691 and Grok 4.7 0.603..."
Implication: LLM backtests over pre-training-cutoff periods are contaminated by memorization, not genuine predictive skill.

### Issue #688: "Fix backtest crash when a short wipes out the book" / #686 "Fix short margin accounting"
