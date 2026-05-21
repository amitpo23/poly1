"""Canonical strategy catalog and pre-live evaluation gates.

The project has many agents, providers, and research scripts.  This module is
the small, deterministic map that answers two questions before a live run:

1. Which historical algo family does a strategy belong to?
2. What evidence is required before the strategy may receive live capital?

It is intentionally data-only plus simple validators.  No live trading code
imports this as a permission bypass; the catalog is for research, QA, and
operator reports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class EvidenceGate:
    min_samples: int = 30
    min_winrate: float = 0.55
    min_pnl_per_100: float = 0.0
    require_all_windows: bool = True
    require_shadow_markout: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    family: str
    owner_agent: str
    maturity: str
    description: str
    backtest_command: Optional[str]
    shadow_agent: Optional[str]
    gate: EvidenceGate = field(default_factory=EvidenceGate)
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gate"] = self.gate.to_dict()
        payload["blockers"] = list(self.blockers)
        return payload


@dataclass(frozen=True)
class WindowResult:
    label: str
    samples: int
    winrate: Optional[float]
    pnl_per_100: Optional[float]


@dataclass(frozen=True)
class GateVerdict:
    strategy_id: str
    state: str
    blockers: list[str]
    windows_checked: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STRATEGY_CATALOG: tuple[StrategySpec, ...] = (
    StrategySpec(
        strategy_id="btc_daily_mean_reversion",
        family="mean_reversion",
        owner_agent="btc_daily",
        maturity="live_candidate_blocked",
        description="Fade BTC daily up/down moves after MarketBrain and EV gates.",
        backtest_command="python scripts/python/backtest_mean_reversion.py --days {days} --position-size 100 --json",
        shadow_agent="btc_daily",
        blockers=("recent_backtest_negative",),
    ),
    StrategySpec(
        strategy_id="btc_daily_momentum",
        family="trend_following",
        owner_agent="btc_daily",
        maturity="research_only",
        description="Follow BTC daily direction instead of fading it.",
        backtest_command="python scripts/python/backtest_btc_momentum.py --days {days} --json",
        shadow_agent="btc_daily",
        blockers=("fails_split_windows",),
    ),
    StrategySpec(
        strategy_id="crypto_5m_directional",
        family="trend_following",
        owner_agent="btc_5min",
        maturity="shadow_only",
        description="Fast crypto up/down entries from momentum, tape, funding and MetaBrain.",
        backtest_command=None,
        shadow_agent="btc_5min",
        blockers=("missing_30_60_90_backtest_harness",),
    ),
    StrategySpec(
        strategy_id="scalper_spread_edge",
        family="market_microstructure",
        owner_agent="scalper",
        maturity="shadow_only",
        description="Short-horizon spread/edge scalping on crypto prediction markets.",
        backtest_command="python scripts/python/backtest_scalper.py --hours 48 --position-size 2.5 --json",
        shadow_agent="scalper",
        blockers=("recent_backtest_negative", "history_limited_to_pair_db"),
    ),
    StrategySpec(
        strategy_id="crypto_5m_market_maker_shadow",
        family="market_making",
        owner_agent="crypto_5m_market_maker_shadow",
        maturity="shadow_only",
        description="Quote both sides when spread/depth/tape allow a tiny positive edge.",
        backtest_command=None,
        shadow_agent="crypto_5m_market_maker_shadow",
        gate=EvidenceGate(min_samples=100, min_winrate=0.55, min_pnl_per_100=1.0),
        blockers=("shadow_markout_required",),
    ),
    StrategySpec(
        strategy_id="sports_cheap_hold",
        family="event_driven_relative_value",
        owner_agent="market_sweep",
        maturity="research_candidate",
        description="Buy cheap sports outcomes and hold to resolution; surfaced by market sweep.",
        backtest_command="python scripts/python/backtest_market_sweep.py --max-age-days {days} --max-markets 300 --json",
        shadow_agent="scanner_executor",
        gate=EvidenceGate(min_samples=50, min_winrate=0.55, min_pnl_per_100=5.0),
        blockers=("needs_lookahead_bias_audit", "needs_split_stability"),
    ),
    StrategySpec(
        strategy_id="cross_venue_arb",
        family="statistical_arbitrage",
        owner_agent="meta_arbiter",
        maturity="guard_only",
        description="Cross-venue prediction-market arbitrage after rule mapping, freshness and depth checks.",
        backtest_command=None,
        shadow_agent=None,
        gate=EvidenceGate(min_samples=10, min_winrate=0.90, min_pnl_per_100=1.0),
        blockers=("requires_external_venue_quotes", "requires_rule_mapping"),
    ),
    StrategySpec(
        strategy_id="equity_options_fair_value",
        family="volatility_relative_value",
        owner_agent="equity_options_fair_value",
        maturity="shadow_only",
        description="Use equity/options implied fair value to price equity-linked prediction markets.",
        backtest_command=None,
        shadow_agent="equity_options_fair_value",
        gate=EvidenceGate(min_samples=30, min_winrate=0.55, min_pnl_per_100=3.0),
        blockers=("needs_resolved_equity_event_backtest",),
    ),
    StrategySpec(
        strategy_id="external_conviction_providers",
        family="news_sentiment_event_driven",
        owner_agent="external_conviction",
        maturity="shadow_only",
        description="Provider signals from news, debate, tape, whale, Alpaca, OpenBB and related sources.",
        backtest_command="python scripts/python/backtest_external_convictions.py --json",
        shadow_agent="external_conviction_api",
        blockers=("provider_scorecard_required",),
    ),
    StrategySpec(
        strategy_id="climax_volume_reversal",
        family="mean_reversion",
        owner_agent="openbb_market_data",
        maturity="shadow_feature",
        description="Volume exhaustion reversal: high-volume candle plus opposite confirmation.",
        backtest_command=None,
        shadow_agent="external_conviction_openbb",
        blockers=("needs_dedicated_walk_forward_backtest",),
    ),
    StrategySpec(
        strategy_id="rl_reward_policy_lab",
        family="machine_learning",
        owner_agent="rl_reward_lab",
        maturity="offline_research",
        description="Build reward rows from shadow markouts for later offline policy learning.",
        backtest_command=None,
        shadow_agent=None,
        gate=EvidenceGate(min_samples=500, min_winrate=0.0, min_pnl_per_100=0.0),
        blockers=("never_live_directly", "requires_offline_validation"),
    ),
)


def catalog_by_id() -> dict[str, StrategySpec]:
    return {spec.strategy_id: spec for spec in STRATEGY_CATALOG}


def strategy_family(strategy_id: str, *, default: str = "other") -> str:
    spec = catalog_by_id().get(str(strategy_id or ""))
    return spec.family if spec else default


def catalog_summary() -> dict[str, Any]:
    by_family: dict[str, int] = {}
    by_maturity: dict[str, int] = {}
    for spec in STRATEGY_CATALOG:
        by_family[spec.family] = by_family.get(spec.family, 0) + 1
        by_maturity[spec.maturity] = by_maturity.get(spec.maturity, 0) + 1
    return {
        "strategy_count": len(STRATEGY_CATALOG),
        "families": by_family,
        "maturity": by_maturity,
        "strategies": [spec.to_dict() for spec in STRATEGY_CATALOG],
    }


def evaluate_gate(strategy_id: str, windows: list[WindowResult]) -> GateVerdict:
    spec = catalog_by_id().get(strategy_id)
    if spec is None:
        return GateVerdict(strategy_id, "unknown_strategy", ["strategy_not_in_catalog"], len(windows))
    blockers = list(spec.blockers)
    gate = spec.gate
    if not windows:
        blockers.append("missing_backtest_windows")
    passing = 0
    for window in windows:
        if window.samples < gate.min_samples:
            blockers.append(f"{window.label}:insufficient_samples")
            continue
        if window.winrate is None or window.winrate < gate.min_winrate:
            blockers.append(f"{window.label}:winrate_below_gate")
            continue
        if window.pnl_per_100 is None or window.pnl_per_100 <= gate.min_pnl_per_100:
            blockers.append(f"{window.label}:pnl_below_gate")
            continue
        passing += 1
    if gate.require_all_windows and passing < len(windows):
        blockers.append("split_window_gate_failed")
    elif not gate.require_all_windows and passing == 0:
        blockers.append("no_passing_window")
    state = "live_eligible" if not blockers else "shadow_or_research_only"
    return GateVerdict(strategy_id, state, sorted(set(blockers)), len(windows))
