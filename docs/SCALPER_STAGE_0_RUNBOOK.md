# Scalper Stage 0 — Shadow Mode Runbook

Goal: validate that the trigger logic fires on real markets. NOT a
profitability test.

## Pre-launch

- [ ] `.env` includes `EXECUTE_SCALPER="false"`.
- [ ] `.env` includes `SCALPER_RESERVE_USDC="20"`.
- [ ] `data/HALT` does NOT exist.
- [ ] Trader has been running cleanly for ≥24h (no MAY_HAVE_FIRED).
- [ ] `git tag stage0-scalper-shadow-$(date -u +%Y%m%d-%H%M)` and push.

## Launch

```bash
docker compose --profile scalper up -d scalper
docker compose logs -f scalper
```

## Daily checks (each day for 2-3 days)

```bash
docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 50
```

- Did pairs get created? (rows under "last X scalper pairs")
- How many SHADOW legs? (under "scalper legs")

```bash
docker compose ps
```
— both containers `Up`, healthcheck passing.

```bash
docker compose logs scalper --since 1h | grep -E "ERROR|exception"
```
— should be empty.

## Pass criteria for moving to Stage 1

| Criterion | Threshold |
|-----------|-----------|
| Pairs created per day | ≥ 5 |
| SHADOW legs that satisfied profit gate | ≥ 8/day |
| RECONCILE_NEEDED rows | 0 |
| Unhandled exceptions in scalper logs | 0 |
| Heartbeat staleness ever > 30s | No |

If all pass after 48h, proceed to Stage 1 (a separate runbook).

## Rollback

```bash
docker compose --profile scalper stop scalper
# Pairs in non-terminal state remain in the table. Mark them:
docker compose run --rm trader sqlite3 data/trade_log.db \
    "UPDATE scalper_pairs SET state='shadow' WHERE state IN ('tracking','leg1_filled');"
```
