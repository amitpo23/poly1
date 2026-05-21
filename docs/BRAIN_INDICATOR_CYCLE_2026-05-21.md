# Brain Indicator Cycle

This is the safe control-room loop for MetaBrain.

## Purpose

Every cycle refreshes the evidence layer before any trading agent is allowed to
act:

1. Refresh the focused Polymarket universe.
2. Pull AlphaInsider strategy rankings into `data/alphainsider_strategy_rankings_latest.json`.
3. Update shadow markouts for 1/3/5/15 minute horizons.
4. Rebuild provider and strategy scorecards.
5. Run `market_scanner` once so MetaBrain can write approved candidates to
   `brain_decisions`.
6. Run `scanner_executor` once in shadow mode so approved candidates become
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
- `data/brain_indicator_cycle_heartbeat` — healthcheck heartbeat.
- `data/alphainsider_strategy_rankings_latest.json` — ranked external strategy feed.
- `data/provider_scorecard.json` — resolved provider reliability feed.
- `data/strategy_scorecard.json` — shadow/live decision quality feed.

## Docker

Run in shadow/research mode:

```bash
docker compose --profile scanner --profile research up -d brain-indicator-cycle
```

This service should run beside `market_scanner`, `scanner-executor`,
`market_universe`, and `orderbook-monitor`. It is intentionally a coordinator:
the money-moving gates remain in `scanner_executor`, `RiskGate`, `DecisionCouncil`,
and `TradeLog`.

## Tavily Budget

The cycle sets a conservative default:

- `BRAIN_INDICATOR_TAVILY_DAILY_LIMIT=3`
- existing Tavily cache and critical-only filters still apply.

If Tavily quota is closed or expensive, leave `TAVILY_ENABLED=false`; the cycle
will still refresh AlphaInsider, market universe, scorecards, markouts, and
scanner/executor shadow rows.
