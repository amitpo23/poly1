# AGENTS_MAP.md — `agents/application/` directory guide

71 modules at one level. This map groups them by responsibility so a reader can find what they need without opening every file. **Nothing is moved**: paths stay as `agents/application/<name>.py`. This is reference only.

Last verified: 2026-05-28.

---

## 1. Entry-signal agents

Modules that decide whether to open a position. Most run as their own container (see `docker-compose.yml`).

| Module | One-line purpose |
|---|---|
| `trade.py` | Main `Trader` class, sweep loop, dry/live execution path. |
| `cron.py` | `TraderDaemon` — drives `trade.py`'s sweep on a poll interval. |
| `scanner_executor.py` | The one bridge from `market_scanner` discovery → live entry. Re-checks metadata, EV, order-book before firing. Writes `decision_journal` rows. **The only entry agent currently feeding the probability calibrator.** |
| `scalper.py` | Strategy C — fast in/out market-making, isolated from LLM. |
| `btc_5min.py` | 5-min BTC up/down market signals (multi-signal: momentum + funding + RSI). |
| `btc_daily.py` | Daily BTC trend-following entry. |
| `btc5min_timed.py` | **See sub-section below** — v1 of the timed BTC family. |
| `btc5min_timed_v2.py` | **See sub-section below** — v2. |
| `btc5min_timed_v3.py` | **See sub-section below** — v3. |
| `daily_3h_fade.py` | Daily momentum fade — 3h before market close. |
| `news_shock.py` | High-impact news event reactions (one-shot, short window). |
| `news_signal.py` | Sustained news sentiment polling (15-min cycles). |
| `climax_volume_reversal.py` | Volume-climax reversal entry. |
| `opportunity_factory.py` | Synthesizes candidate trades from upstream wallet/whale/scorecard signals into a structured `trade_recommendation`. |

### The three `btc5min_timed` files are distinct strategies, not versions of one

These files share a name prefix but they implement **different entry mechanics**. Operator decision (2026-05-28): all three keep running in parallel. Future reader: do not assume v3 is "the new v2" — they are independent.

- **`btc5min_timed.py`** (v1, 2026-05-25). Time-based contrarian: at `t=0:01` into each 5-minute period, BUY DOWN, regardless of any signal. No LLM, no microstructure read. The bet is that price spikes early in the period mean-revert by the close. Position size and SL/TP are operator-set. Tier A (Amit's custom).

- **`btc5min_timed_v2.py`** (v2, 2026-05-27). Same `t=0:01` BUY DOWN entry as v1, but adds **data-driven gates** computed from a 7-day rolling analyzer (Layer 1 = directional bias, Layer 2 = liquidity check). Trades are blocked if the gates say the current regime doesn't fit. First live test was blocked by the liquidity check, which is the expected behaviour, not a bug.

- **`btc5min_timed_v3.py`** (v3, 2026-05-27). Different mechanic entirely. Continuous spike detection over the full period: from `t=1` to `t=260` of each 5-min period, monitor mid-price; if a sufficient spike fires, bet the opposite direction. This is the "fade-the-spike" strategy. It uses the websocket book feed (`agents/utils/ws_book_feed.py`) for low-latency reads.

---

## 2. External-conviction agents

| Module | One-line purpose |
|---|---|
| `external_conviction.py` | The single binary behind every `external-conviction-*` service in `docker-compose.yml`. Provider is selected by `EXTERNAL_CONVICTION_PROVIDER` env var. |

The 15 EC services in compose (`external-conviction`, `-polifly`, `-api`, `-whale`, `-divergence`, `-debate`, `-aggregator`, `-tradingview`, `-alpaca`, `-openbb`, `-crypto-tape`, `-crypto-tape-long`, `-technical`, `-gdelt`, `-crypto-deriv`) all run this same module — they share the `x-ec-base` YAML anchor in `docker-compose.yml` and differ only by env vars (provider, poll interval, output path, healthcheck threshold, occasional mem/cpu/profile/source-mount overrides).

Provider implementations live alongside, not under EC:

| Module | Used by |
|---|---|
| `alpaca_market_data.py` | `external_conviction --provider alpaca_market_data` (1-min equity bars, momentum) |
| `openbb_market_data.py` | `external_conviction --provider openbb_market_data` (yfinance) |
| `crypto_exchange_tape.py` | `external_conviction --provider crypto_exchange_tape` (fast crypto spot tape, 60s poll) |
| `alphainsider_strategy_rankings.py` | The aggregator pulls ranked alphainsider signals from here. |
| `tavily.py` | Tavily search backend; gated by `TAVILY_*` budget vars. |

---

## 3. Brain, forecast, calibration

| Module | One-line purpose |
|---|---|
| `market_brain.py` | Per-market scoring of multiple signals into a single edge estimate. |
| `meta_brain.py` | Combines `market_brain` outputs across markets + provider weighting. |
| `meta_arbiter.py` | Final say between competing meta_brain decisions. |
| `decision_council.py` | Voting layer — collects votes from multiple experts and produces a council decision. |
| `hermes_forecast.py` | LLM-based forecast service (HTTP on :8097). Called by other agents via `HERMES_FORECAST_URL`. **Internal microservice — runs in poly1's compose, not standalone.** |
| `internal_pricer.py` | Computes our own fair value for a market and compares to the CLOB price; gate for `scanner_executor`. |
| `bayesian_aggregator.py` | Combines calibrated win-rates from multiple agents to compute net edge. |
| `probability_calibrator.py` | Per-segment Wilson-based win-rate stats (the calibrator that today only sees `scanner_executor` decisions). |
| `multi_pipeline_calibrator.py` | Aims to measure edge across all three execution pipelines (scanner_executor, direct agents, EC shadow). Currently coverage is biased — see open P0s. |

---

## 4. Routers — three distinct purposes

All three end in `_router.py`. They are **not** variants. Each routes a different thing:

- **`consensus_router.py`** — runtime gate. When 2+ entry agents approve the same market within a short window, this relaxes the gates used by `scanner_executor` because cross-source agreement adds confidence. Currently disabled by default (`SCANNER_EXECUTOR_CONSENSUS_ENABLED=false`); the infrastructure is wired but agent coverage is too uneven to enable.

- **`opportunity_router.py`** — discipline layer between discovery and trading. Takes scout/research/news/database evidence and routes it to one of four destinations: `reject`, `paper`, `backtest`, or `live_probe`. Does **not** place orders.

- **`regime_router.py`** — strategy selector. Given current market state (volatility, liquidity, time-of-day), it answers "which strategy families should lead, which should be sized down, which should wait for a better setup." Does **not** predict markets itself; it picks who plays.

---

## 5. Position management & exits

| Module | One-line purpose |
|---|---|
| `position_manager.py` | TP/SL exit, position reconciliation, partial take-profit logic. |
| `exit_executor.py` | Turns a single exit decision into a FAK SELL attempt. Never marks a position closed unless the CLOB response says fill. |
| `executor.py` | Lower-level execute helper used by the entry path. |
| `trading_supervisor.py` | Order-placement safety + capital allocation guard. |
| `settlement_reconciler.py` | Post-market settlement, drawdown reconciliation. |
| `near_resolution.py` | Market-expiry handler; trades the last hours of a soon-to-resolve market. |
| `resolution_sync.py` | Annotates `brain_decisions` with eventual market outcomes (currently has the 198k+ backlog noted in `AGENT_AUDIT_2026_05_26.md`). |

---

## 6. Risk, safety, sizing

| Module | One-line purpose |
|---|---|
| `risk_gate.py` | Central gate: HALT file check, runtime-control mode, cash/reserve floor, drawdown. Called twice per cycle (pre-sweep and per-market). |
| `execution_lock.py` | Cross-cycle lock so two cycles don't race on the same market. |
| `execution_safety.py` | Pre-order sanity (price collar, side/token mapping, idempotency check). |
| `execution_quality.py` | Post-order slippage/spread gates feeding the meta-brain weighting. |
| `sizing.py` | Position-size math: Kelly fraction, max-position-fraction, scaling. |
| `trading_policy.py` | High-level policy: which agents can execute live under which conditions. |

---

## 7. Wallet watchers

| Module | One-line purpose |
|---|---|
| `wallet_watcher.py` | Monitor whale/VIP wallet activity on-chain. |
| `wallet_follow.py` | Copy-trade detected whale positions (gated by `EXECUTE_WALLET_FOLLOW`). |

---

## 8. Market discovery

| Module | One-line purpose |
|---|---|
| `market_scanner.py` | Discover new markets, opportunity identification. Deliberately read-only — writes `brain_decisions`, doesn't trade. |
| `market_universe.py` | Market-universe metadata + segmentation (top-N by liquidity, by asset, by horizon). |
| `orderbook_monitor.py` | Token liquidity tracking + shadow brain orderbook lookback. |
| `market_microstructure.py` | Lower-level spread/depth metrics used by multiple agents. |

---

## 9. Scorecards, research, shadow

| Module | One-line purpose |
|---|---|
| `strategy_scorecard.py` | Per-strategy win-rate + profit accumulation. |
| `strategy_catalog.py` | Index of known strategies + their metadata (lives here as the library; `scripts/strategy_catalog.py` is the thin CLI wrapper). |
| `research_committee.py` | Cross-agent review/voting on backtest decisions. |
| `research_harness.py` | Backtest scaffolding for new strategies (library form; `scripts/research_harness.py` is the CLI). |
| `rl_reward_lab.py` | RL reward shaping experiments. |
| `equity_options_fair_value.py` | Black-Scholes fair value for ES1 options, fed into the brain. |
| `quant_price_fair_value.py` | Alternative fair-value model. |
| `crypto_5m_market_maker_shadow.py` | Shadow market-making — no live orders, only markout estimation. |
| `vibe_analysis.py` | Sentiment "vibe" features. |

---

## 10. Trade recommendation & shared types

| Module | One-line purpose |
|---|---|
| `trade_recommendation.py` | Dataclass + validation for the recommendation shape produced by every entry agent. |
| `signal_contract.py` | Schema for signal records consumed by the brain. |
| `trade_log.py` | SQLite ledger: trades, brain_decisions, decision_journal, pnl_events. Imports `ACTIVE_STATUSES` invariant. |
| `prompts.py` | LLM prompt templates (`one_best_trade` etc — paired with side/token mapping per CLAUDE.md). |
| `llm_config.py` | Model selection + timeouts + retry budget. |
| `anthropic_compat.py` | Shim across Anthropic SDK versions. |
| `capital_allocator.py` | Computes per-agent capital allocation from reserves + runtime config. |
| `scalper_pairs.py` | Pair-state machine for the scalper (separate dedupe contract from the trader). |
| `agent_registry.py` | Single source of truth for "what agent names exist + their metadata." |
| `arb_quality.py` | Quality metrics for arbitrage opportunities. |
| `creator.py` | Helper to create new markets on Polymarket (rare/manual). |

---

## What lives outside `agents/application/`

This map covers only `agents/application/`. The wider tree:

- `agents/polymarket/` — Polymarket client adapter (`polymarket.py`, side/token mapping). **Do not touch this without re-reading the CLAUDE.md invariants on side semantics.**
- `agents/connectors/` — external data sources (Chroma, news, search).
- `agents/utils/` — logging, notifications, the websocket book feed.
- `scripts/` — operator CLIs, runbooks, cron-driven jobs (`runtime_control.py`, `dashboard_server.py`, etc.).
- `scripts/python/` — backtests, simulations, one-shot analysis scripts.
- `deploy/` — entry points (`run.py`), runtime config (`.env.runtime`).

---

## How to use this map

- **New reader:** start with §1 (entry agents) and §3 (brain) — those decide what trades.
- **Looking at a compose service:** the `RUNTIME_AGENT` env var on each service maps 1:1 to a file here (e.g. `RUNTIME_AGENT: external_conviction_alpaca` → `external_conviction.py` with the alpaca provider).
- **Adding a new strategy:** §1 is the right home, plus a sibling test under `tests/`, plus a compose service entry. Update this file too.
- **Naming convention reminder:** `<thing>_v2.py` / `<thing>_v3.py` files in §1 are **independent strategies**, not version-bumps of one. Don't delete one assuming it's superseded.
