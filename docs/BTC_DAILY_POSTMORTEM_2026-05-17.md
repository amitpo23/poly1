# btc_daily Postmortem - 2026-05-17

## Current State

poly1 is back in `freeze`.

- `data/HALT` exists.
- `deploy/.env.runtime` has `EXECUTE=false`.
- `data/runtime_control.json` has `allowed_live_agents=[]`.
- `python3 scripts/trading_stability_preflight.py --mode freeze` passes.
- `python3 scripts/trading_stability_preflight.py --mode live` is blocked.

Docker Desktop was initially not reachable from this shell:

```text
Cannot connect to the Docker daemon at unix:///Users/mymac/.docker/run/docker.sock
```

After Docker Desktop was started, it auto-restored stale containers. This was a
real safety finding: `poly1-news-signal` came back with old env values including
`EXECUTE=true` and all entry flags enabled; `poly1-position-manager` came back
with the previous `live_probe` runtime hash.

Immediate remediation completed:

- stopped stale `poly1-scalper`;
- stopped stale `poly1-news-signal`;
- stopped stale `poly1-wallet-watcher`;
- recreated `position_manager`, `trading-supervisor`, and
  `settlement-reconciler` under the current freeze env.

Post-remediation `docker compose ps` shows only:

- `dashboard`;
- `grafana`;
- `position_manager`;
- `trading-supervisor`;
- `settlement-reconciler`.

The recreated safety containers now have:

```text
RUNTIME_MODE=freeze
RUNTIME_CONFIG_HASH=18e59d789c9ae259
EXECUTE=false
EXECUTE_BTC_DAILY=false
EXECUTE_SCALPER=false
EXECUTE_NEAR_RESOLUTION=false
EXECUTE_NEWS_SHOCK=false
EXECUTE_WALLET_FOLLOW=false
EXECUTE_MAINTAIN=true
```

## btc_daily Evidence

Real `btc_daily_open` entries, excluding shadow rows:

| Date | Entries | Entry USDC | Notes |
| --- | ---: | ---: | --- |
| 2026-05-07 | 2 | 6.00 | First live rows mixed with repeated dust close attempts |
| 2026-05-08 | 3 | 9.00 | 2 stop losses, 1 take profit |
| 2026-05-11 | 2 | 6.00 | Dust/stop-loss accounting noise |
| 2026-05-12 | 3 | 9.00 | Close loop generated many dust rows |
| 2026-05-13 | 2 | 6.00 | Both closed immediately as stop losses |

The 2026-05-13 live-probe rows were:

```text
2354 2026-05-13T00:25:49Z btc_daily_open SELL 0.5 size=3.0
2355 2026-05-13T00:26:03Z closed_stop_loss SELL 0.3283 size=1.9698
2388 2026-05-13T13:36:13Z btc_daily_open BUY  0.5 size=3.0
2389 2026-05-13T13:36:30Z closed_stop_loss SELL 0.3038 size=1.8228
```

Naive realized delta for those two entries is about `-2.2074 USDC` before any
broader wallet accounting.

## Failed Attempts After 2026-05-13

After the freeze/live-probe boundary, the journal has seven failed live attempts:

- 4 slippage failures: live ask was far above the intended `0.50` price
  (`0.73`, `0.99`, `0.994`, `0.94`).
- 1 invalid API key failure on 2026-05-16.
- 1 missing orderbook failure.
- 1 no-asks failure.

These are operational blockers, not proof of a profitable strategy.

## Conclusion

`btc_daily` should not be re-armed live from current evidence. It needs a shadow
period with clean accounting and explicit pass/fail rules before any new live
probe.

The strongest diagnosis is not just "bad direction"; it is a combined failure:

- immediate stop-loss exits on the latest real live entries;
- repeated execution failures from slippage and orderbook quality;
- at least one API credential failure;
- noisy close/dust rows that make realized PnL harder to trust without a
  cleaner report.

## Recommended Execution Plan

1. Keep `freeze` active.
2. Fix the stale-container auto-restore risk before any future Docker restart.
3. Fix the Polymarket API key issue before any live attempt.
4. Keep LLM-dependent agents disabled until OpenAI quota is fixed.
5. Run one agent in shadow only, preferably `btc_daily`, for 14 days.
6. Require a green preflight, clean heartbeat, no critical API failures, and a
   clean simulated PnL report before any live-probe command.
