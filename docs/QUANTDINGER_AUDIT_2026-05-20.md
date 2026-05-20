# QuantDinger Audit - 2026-05-20

## Summary

QuantDinger is a self-hosted quant operating system: Flask backend, Vue UI,
Postgres/Redis style architecture, data providers, strategy lifecycle,
backtesting, live execution adapters, and an MCP/agent gateway.

It is interesting as architecture, not something to plug into poly1 live.

## Useful Ideas For poly1

1. Agent gateway with scoped tokens:
   - Their agent calls are audit logged.
   - Trading tokens are paper-only by default.
   - Live requires both token and server flags.
   - This matches our need for explicit runtime control.

2. Strategy lifecycle:
   - They separate strategy creation, backtest, experiment, execution, and
     monitoring.
   - This reinforces our new `agent_registry` + `strategy_scorecard` path.

3. Data-source circuit breakers:
   - They have data source abstractions, cache manager, rate limiter, and
     circuit breaker modules.
   - We should add a similar provider-health layer before promoting new data
     feeds.

4. Paper-first agent interface:
   - Good model for future Codex/Claude interactions with poly1.
   - Our current equivalent is `runtime_control.py freeze/paper/live`.

## What Not To Copy

- Do not connect its live trading modules to our wallet.
- Do not import its execution stack into poly1.
- Do not rely on its default admin/demo setup.
- Do not treat its AI-generated strategy flow as proof of profitability.

## Recommendation

Use QuantDinger as a reference for:

- agent gateway design,
- strategy lifecycle UX,
- provider health/circuit breakers,
- audit logs,
- paper-only defaults.

Do not install it into the production server or give it secrets.
