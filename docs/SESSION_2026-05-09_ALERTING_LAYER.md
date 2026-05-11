# Session log — 2026-05-09 morning: Tier 1 alerting layer

## Why this session existed

User: *"אני לא רוצה למצוא את עצמי כל שעה מבצע את הפעולות הללו"*. Then
proposed 3 new agents (strategy director, QA bot, whale follower).

Real complaint: 12+ hours of identical `0 fills, $54.26 stable` cron
reports were toil with no signal. Real solution: replace verbose
periodic reports with state-change-driven alerts. **Smaller answer
than 3 agents.** Per advisor: "stop making me do this every hour."

The advisor warned against Tier 2/3 (whale-as-controller, LLM
auto-fixer) — same trap as yesterday's scalper backtest, where
automation amplifies an unproven assumption. We deferred those
explicitly until there's a winning strategy to direct.

## What got built

### `scripts/python/state_watcher.py` (~225 lines, stdlib only)

Reads three sources every run:
- `data/trade_log.db` — `trades` (max id, status counts, MAY_HAVE_FIRED)
  and `scalper_pairs` (RECONCILE_NEEDED count)
- `~/Desktop/poly/bot/data/swarm.db` — `fills` (max id) and
  `pending_orders` (max id, submitted-open count)
- `docker ps` — health of 5 expected containers

Snapshot persisted to `data/.state_watcher_snapshot.json` (gitignored).
Diff vs. last snapshot triggers alerts; otherwise silent.

**Alert vocabulary:**
- 🚨 container DOWN / unhealthy transition
- 🚨 NEW RECONCILE_NEEDED row
- 🚨 NEW MAY_HAVE_FIRED trade
- ⚡ poly1 +N {btc_daily_open|scalper_leg|scalper_exit|closed_take_profit|closed_stop_loss|filled}
- ⚡ swarm +N fill(s)
- ℹ️ swarm submitted-open count rose

**Ignored (noise):**
- `failed`, `skipped_dry_run`, `skipped_gate`, `close_failed` — high-frequency
  retry/dry-run rows that flood the table without indicating new state.
- Coinbase feed 404 warnings — already-known issue; alerting on each
  occurrence drowns out real signals.

### Cron replacement

- Old (deleted): `13,43 * * * *` firing the verbose 4-agent status check.
- New (`1590c7bb`): `17,47 * * * *` firing `state_watcher.py` and a
  conditional prompt — investigate only if the script outputs.

## SQLite gotcha

First implementation used `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`
matching `capital_allocator.py:31-37`. Failed with `unable to open
database file` when WAL files exist (which they do — bot keeps writers
open). `mode=ro` triggers SQLite's journal-mode handshake which needs
write access on `-shm`/`-wal`.

Fix: plain `sqlite3.connect(str(path))`. We never `commit()`, so it's
read-only in practice. `mode=ro&immutable=1` would silence the error
but cache stale reads — wrong for a fresh-data watcher.

## Verification

Test 1 — silent on no-change:
```
$ python3 scripts/python/state_watcher.py  # seeds snapshot
[exit=0, no output]
$ python3 scripts/python/state_watcher.py
[exit=0, no output]
```

Test 2 — alerts on simulated diff:
```
$ python3 -c "rewind snapshot's max_trade_id by 2"
$ python3 scripts/python/state_watcher.py
=== state_watcher 2026-05-09T06:29:33+00:00 ===
⚡ poly1 +1 btc_daily_open (total 22)
⚡ swarm +1 fill(s) (max id 2)
[exit=0]
$ python3 scripts/python/state_watcher.py
[exit=0, no output]  # snapshot caught up
```

## What's deliberately NOT in scope

- **On-chain pUSD balance check.** Requires `docker exec poly1-position-manager`
  + web3 imports; adds ~3s + brittleness. Cash-stable status is also
  inferable from "no fills" — current alerts cover this implicitly. Add
  later if drawdown alerting becomes needed.
- **Drawdown thresholds.** Need historical balance series; no
  framework for it yet.
- **LLM-driven anomaly detection.** Tier 3 risk per advisor; same trap
  as yesterday's scalper auto-tune.
- **Auto-fix for config drift.** Detected `MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE`
  drift between `.env` (0.60) and container (0.35) earlier today — but
  auto-fixing would silently undo human decisions. Surfacing-only is
  the right behavior; we didn't even add an alert for it.
- **Whale tracking.** Tier 2 — research-grade signal, not built today.

## Files

Created:
- `scripts/python/state_watcher.py`
- `docs/SESSION_2026-05-09_ALERTING_LAYER.md` (this file)

Modified:
- `.gitignore` — added `data/.state_watcher_snapshot.json`

State files (untracked):
- `data/.state_watcher_snapshot.json` — last-seen state, recreated each run

## Open follow-ups (none urgent)

| # | Item | Notes |
|---|---|---|
| – | btc_daily Coinbase feed 404 | Container has 0 successful Coinbase responses lifetime; bot mostly blind. Replace feed (Binance/CoinGecko) — ~1-2h |
| – | strait-of-hormuz blocked by stale fills | 2 `filled` rows from 5/6 (id=240, 241) blocking market_maker via `BLOCKING_STATUSES`. Right fix: resolution code transitions filled→cleared. Local hack: `UPDATE pending_orders SET status='cleared' WHERE id IN (240,241)` |
| – | alphabet skip mystery | market_maker logs "submitted order exists" but DB has 0 active rows. In-memory state issue or different code path. ~30min code read in `state_store.py` / `market_maker_agent.py:189` |
