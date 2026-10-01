# Pitfall: Subagent Analysis Quality

## Problem
Using `delegate_task` to run parallel instrument analyses produces LOW QUALITY output:
- Subagents cannot read knowledge files (workflow.md, strategy_workflow.md)
- Even with rules passed as context (18KB), subagents invent their own rule names
- SB/CT/BAN rules get wrong definitions (e.g., SB-01="大阴线" instead of "强多头棒+AIL")
- BTC/ETH analyses are abbreviated, SOL gets complete but wrong rules

## Root Cause
- Subagent sandbox cannot access the skill folder filesystem (`references/knowledge/`, `memory/`, `assets/templates/`)
- Rules are too complex (133+ rules, 23 chapters) to pass accurately in context
- Subagents lack the SKILL.md + references/SOUL.md execution rules

## Correct Approach
- **One main agent analysis at a time** — sequential, not parallel
- **Or 5 dedicated Hermes profiles** — each profile has direct file access
- Never use delegate_task for price action analysis

## User Preference
User explicitly chose "5 instances, one per instrument" over parallel subagents.
Each instance should focus on one instrument only for quality and memory isolation.
