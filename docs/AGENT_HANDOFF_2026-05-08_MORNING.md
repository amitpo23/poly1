# Handoff for the agent arriving morning of 2026-05-08

> The user paused work the evening of 2026-05-07 with a 24h experiment
> running. This file is the orientation a fresh agent needs to pick up
> tomorrow morning. Read this first, then `deploy/CURRENT_STATUS.md`,
> then the linked session logs only if you need depth.

## What's running

- **8 containers, all healthy** (poly1, poly1-scalper, poly1-btc-daily,
  polymarket-swarm, poly1-position-manager, poly1-strategy-reporter,
  poly1-dashboard-1, poly1-grafana).
- **Host daemon**: `scripts/python/allocator_sync.py` (PID logged in
  `data/logs/allocator_sync.log`). 5-min loop. Reads
  `CapitalAllocator`, writes the allocation to both `.env` files,
  restarts affected containers. **The allocator is the decision-maker;
  the operator is not in the loop within the $20 budget.**
- **`.env` posture (poly1)**: `EXECUTE_BTC_DAILY=true` ($20 reserve),
  `EXECUTE_SCALPER=false` ($0), `EXECUTE=false` (trader shadow),
  `EXECUTE_MAINTAIN=true` (position_manager live exit-only).
- **`.env` posture (swarm)**: `BOT_MODE=dryrun`, `TOTAL_CAPITAL=0`.
  Synced from allocator.

## Hard rules (do not violate)

1. **$20 cap.** The experiment touches at most $20. The remaining
   wallet (~$38 unreserved at session end) is off-limits.
2. **Don't bypass the allocator.** If it gates an agent at $0, that's
   the answer. Do not pre-tune `.env` to override. The way to lift a
   gate is to fix the underlying signal/state (e.g., resolve errors,
   wait for fresh decisions), not to change the score.
3. **The auto-sync daemon already enforces (1) + (2).** Just let it
   run. Don't manually edit reserves; the daemon will overwrite them
   on the next cycle.
4. **Do not write changes to `swarm/` from poly1 unless mediated by
   the allocator-sync.** The two repos share a wallet and a wallet
   ledger; they don't share code.

## What was happening when the user logged off

- Cash on-chain: **$55.32**
- Real MTM: **$0.01** (all stuck "open" journal positions are dust on-chain)
- $20 budget allocated 100% to btc_daily
- 5 btc_daily `btc_daily_open` BUYs today on token `847709108036407671`
  @ $0.50, $3 each, totaling $15 deployed. Zero closes yet — those
  positions are accumulating, awaiting resolution or exit.
- 3 scalper trades earlier in the day all lost ~10% (-$0.76 total).
  Allocator scored scalper to $0 after that.
- Swarm in dryrun. None of its 5 sub-agents has fresh decisions in
  the 24h window — that's why they're at $0.
- All RECONCILE_NEEDED rows from prior days have been cleaned up.

## Drawdown vs experiment cap

- Realized loss so far: **-$0.76** out of $20 budget = **3.8%**.
- Cap is `MAX_DAILY_LOSS_PCT=10%` ($2.00).
- Well within tolerance. Don't intervene unless drawdown crosses 8%.

## Critical bugs that were fixed this session (don't reintroduce)

1. **`position_manager._already_closed` dust override.** A tiny
   timeout fill ($0.0034) was marking positions closed even though
   most shares were still on-chain. The fix queries on-chain CTF
   balance and treats > 1 share as still open. See
   `agents/application/position_manager.py:311-334`.
2. **Stale dryrun pending order in `swarm.db`** (`id=243`,
   `order_id="dry_1"`, was blocking allocator's market_maker score).
   Cleared via SQL update.
3. **Journal phantom MTM**: I had been reporting MTM ≈ $8.59 from
   `journal_shares × mid`, but the journal positions were resolved
   long ago — on-chain shares are dust. Future-self: cross-check with
   `get_balance_allowance(asset_type=CONDITIONAL, token_id=...)`.

## Useful one-liners for the morning

```bash
# Cash + container health snapshot
export PATH=$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin
docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'poly1|swarm|btc'
docker exec poly1-position-manager python -c \
  "from agents.polymarket.polymarket import Polymarket; \
   print(f'cash=\${Polymarket(live=True).get_usdc_balance():.4f}')"

# Real fills today (filter shadow)
sqlite3 data/trade_log.db "SELECT ts, status, side, price, size_usdc \
 FROM trades WHERE ts >= datetime('now','-12 hours') \
 AND status IN ('filled','matched','closed_take_profit', \
 'closed_stop_loss','closed_timeout','scalper_leg','scalper_exit', \
 'btc_daily_open','btc_daily_close') \
 AND (error IS NULL OR error NOT LIKE 'SHADOW%') \
 ORDER BY ts DESC LIMIT 20"

# Allocator state + recent decisions
tail -30 data/logs/allocator_sync.log
sqlite3 data/trade_log.db "SELECT reason, COUNT(*) FROM brain_decisions \
 WHERE ts >= datetime('now','-1 hour') GROUP BY reason ORDER BY 2 DESC"

# On-chain CTF inventory (which positions actually still held)
docker exec poly1-position-manager python -c "
from agents.polymarket.polymarket import Polymarket
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
import sqlite3
p = Polymarket(live=True)
con = sqlite3.connect('/app/data/trade_log.db')
for (tid,) in con.execute(\"SELECT DISTINCT token_id FROM trades \
WHERE status='filled' AND token_id IS NOT NULL\").fetchall():
  resp = p.client.get_balance_allowance(params=BalanceAllowanceParams(
    asset_type=AssetType.CONDITIONAL, token_id=tid))
  sh = float(resp.get('balance', 0)) / 1_000_000
  if sh > 0.5: print(f'{tid[:18]}... {sh:.4f}')
"
```

## Where to look first if something looks off

- `data/logs/allocator_sync.log` — has the daemon kept cycling? Last
  line should be < 6 minutes old.
- `docker logs --since 30m poly1-position-manager` — exit decisions,
  retries, dust skips.
- `docker logs --since 30m poly1-btc-daily` — entry signals, fills.
- `~/Desktop/poly/bot/.env` — should still be `BOT_MODE=dryrun`,
  `TOTAL_CAPITAL=0` (unless allocator promoted swarm overnight).

## What can change overnight without operator action

The allocator-sync daemon will redistribute the $20 if any agent's
score crosses its eligibility threshold:

- If swarm sub-agents fire decisions (BTC moves enough, news arrives,
  AI key starts working): they may earn a positive score, and the
  daemon will move some of the $20 to them.
- If btc_daily's positions resolve and PnL becomes negative: its
  score drops, and the daemon may cut its allocation.
- If scalper goes 24h+ since its last losing trade and the
  `exit_path_observed` mark expires from the rolling window: it may
  earn back some budget.

All of these are autonomous. **You'll see them in the
`allocator_sync.log` as `swarm.env: set TOTAL_CAPITAL=X` /
`poly1.env: set ...` lines and corresponding container restarts.**

## What's intentionally still pending

- **Allocator-sync as a Docker service.** Currently a host process;
  doesn't survive a Mac reboot. Wrap as a docker service with
  bind-mounted .env files and `/var/run/docker.sock`.
- **Resolution-sync mechanism.** When a Polymarket market resolves,
  nothing currently writes a `resolved_*` row to the journal. Result:
  phantom-open positions and bad MTM reports. Build a job that
  detects resolutions and updates the journal accordingly.
- **Agent → allocator request channel.** Per the user's directive,
  agents should be able to ask for more capital when they see strong
  profit signals. Not built yet.
- **MarketBrain calibration.** 0/3 wins on its first approvals
  (BTC/ETH DOWN reversal). The signal at score 0.35-0.50 has no
  demonstrated edge yet. Wait for more data; do not retune.
