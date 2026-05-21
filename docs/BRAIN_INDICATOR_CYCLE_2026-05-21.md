# Brain Indicator Cycle

This is the safe control-room loop for MetaBrain.

## Purpose

Every cycle refreshes the evidence layer before any trading agent is allowed to
act:

1. Refresh the focused Polymarket universe.
2. Pull AlphaInsider strategy rankings into `data/alphainsider_strategy_rankings_latest.json`.
3. Update shadow markouts for 1/3/5/15 minute horizons.
4. Rebuild provider and strategy scorecards.
5. Run `opportunity_factory` once so strong indicators become either executable
   calibrated candidates or attention decisions.
6. Run `market_scanner` once so MetaBrain can write approved candidates to
   `brain_decisions`.
7. Run `scanner_executor` once in shadow mode so approved candidates become
   auditable `decision_journal` rows.

## Safety

Default mode is no-trade:

- `BRAIN_INDICATOR_NO_TRADE_GUARD=true`
- `BRAIN_INDICATOR_ALLOW_LIVE_DISPATCH=false`
- `EXECUTE=false`
- `EXECUTE_SCANNER_EXECUTOR=false`

That means the cycle can collect signals and dispatch shadow entries, but it
cannot submit live orders. Live dispatch requires deliberately changing both
brain indicator guards and the normal execution flags.

## Outputs

- `data/brain_indicator_cycle_latest.json` — latest cycle report.
- `data/brain_indicator_cycle_state.json` — per-step cadence state.
- `data/brain_indicator_cycle_heartbeat` — healthcheck heartbeat.
- `data/alphainsider_strategy_rankings_latest.json` — ranked external strategy feed.
- `data/provider_scorecard.json` — resolved provider reliability feed.
- `data/strategy_scorecard.json` — shadow/live decision quality feed.
- `data/opportunity_factory_latest.json` — strong-indicator candidate report.

## Docker

Run in shadow/research mode:

```bash
docker compose --profile scanner --profile research up -d brain-indicator-cycle
```

This service should run beside `market_scanner`, `scanner-executor`,
`market_universe`, and `orderbook-monitor`. It is intentionally a coordinator:
the money-moving gates remain in `scanner_executor`, `RiskGate`, `DecisionCouncil`,
and `TradeLog`.

## Cadence

The daemon wakes every minute, but individual steps have their own cadence so
we do not waste API calls or re-score slow-changing data unnecessarily:

| Step | Default cadence | Reason |
| --- | ---: | --- |
| `market_universe` | 5m | Polymarket trends/liquidity move, but not every second. |
| `alphainsider_rankings` | 15m | Strategy leaderboards are slow-moving external data. |
| `shadow_markouts` | 1m | 1/3/5/15m markouts mature continuously. |
| `provider_scorecard` | 5m | Reliability changes after markouts/outcomes, not every loop. |
| `strategy_scorecard` | 5m | Same as provider scorecard. |
| `opportunity_factory` | 1m | Converts proven fresh signals into candidates/attention. |
| `market_scanner` | 1m | Cheap scanner pass for current candidates. |
| `scanner_executor_dispatch` | 1m | Shadow proof/audit pass; live remains disabled by guard. |

Override with `BRAIN_INDICATOR_*_INTERVAL_SEC` only when intentionally running
an experiment. A skipped step is recorded in the latest report with
`skip_reason="cadence"`.

## Indicator Authority

The factory separates two cases:

- **Executable candidate:** the source has a concrete market, side, token and
  calibrated probability. Example: a proven wallet signal with external
  win-rate.
- **Attention decision:** the source is strong but does not yet specify a side
  for a market. Example: a proven AlphaInsider strategy family without a current
  TradingView long/short/flat event.

Only executable candidates are written in the scanner opportunity shape that
`scanner_executor` consumes. Attention decisions are visible to audits and the
brain, but cannot place orders by themselves.

## Tavily / OpenAI Budget

The cycle sets a conservative default:

- `BRAIN_INDICATOR_ENABLE_TAVILY=false`
- `BRAIN_INDICATOR_TAVILY_DAILY_LIMIT=1`
- `BRAIN_INDICATOR_ENABLE_LLM=false`

That means the control-room cycle uses cheap/local feeds by default:
AlphaInsider, market universe, markouts, scorecards, OpportunityFactory, and
scanner/executor shadow rows. Tavily/OpenAI are opt-in for a controlled run only.
When Tavily is enabled, the shared cache, daily limit, max-results cap,
minimum-query interval, and critical-only filters still apply.
