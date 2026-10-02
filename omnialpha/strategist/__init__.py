"""Strategist package: LLM plan → risk gate → inbox signal files."""
from .bridge import chips_to_signal, write_hold_audit, write_signal_file
from .llm_client import LLMClient, LLMConfig, LLMError
from .market import MarketConfig
from .indicators import IndicatorNameError, parse_indicator_name
from .triggers import check_conditions, parse_conditions
from .prompt import build_system_prompt, build_user_prompt, load_strategy_prompt
from .risk import RiskConfig, apply_risk
from .schema import Chip, Plan, PlanError, parse_plan, parse_plan_text
from .snapshot import collect_snapshot

__all__ = [
    "Chip",
    "Plan",
    "PlanError",
    "parse_plan",
    "parse_plan_text",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "MarketConfig",
    "check_conditions",
    "parse_conditions",
    "parse_indicator_name",
    "IndicatorNameError",
    "RiskConfig",
    "apply_risk",
    "collect_snapshot",
    "build_system_prompt",
    "build_user_prompt",
    "load_strategy_prompt",
    "chips_to_signal",
    "write_signal_file",
    "write_hold_audit",
]
