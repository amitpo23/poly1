# Session log — 2026-05-07 evening: Allocator auto-sync + dust-bug fix

> Sister docs:
> - `docs/SESSION_2026-05-07_EXIT_LOGIC_ACTIVATION.md` — morning session
>   (built `position_manager`, first SL closes).
> - `docs/MARKET_BRAIN_STRATEGY_2026-05-07.md` — MarketBrain design and
>   operating rules. Set up by an earlier afternoon session.
> - `docs/POSTMORTEM_2026-05-06.md` — pre-experiment forensic.
>
> This file picks up after MarketBrain was wired and the entry agents had
> been restarted in safety posture. It covers two patches and one new
> mechanism.

## TL;DR for the next agent

- The wallet has $58.53 cash + ~$8.59 MTM ≈ $67 total.
- The user authorized a $20 live experiment over 24h to see all agents
  trading. The remaining $41.74 is **off-limits** and untouched.
- **`CapitalAllocator` is the single decision-maker for the $20 split.**
  The user explicitly does not want to be consulted on internal
  redistributions inside the $20 budget.
- A new daemon **`allocator_sync`** runs every 5 min, applies the
  allocator's recommendation to both `.env` files (poly1 + swarm), and
  restarts affected containers. No human in the loop.
- Two specific bugs were fixed this session: a position_manager
  "dust-close" idempotency bug and a stale dryrun order in `swarm.db`.

If you arrive cold, read `deploy/CURRENT_STATUS.md` first, then this
file, then run:

```bash
tail -50 data/logs/allocator_sync.log
sqlite3 data/trade_log.db "SELECT reason, COUNT(*) FROM brain_decisions WHERE ts >= datetime('now','-30 minutes') GROUP BY reason"
```

## Bug fix #1: dust-close idempotency (`position_manager._already_closed`)

### Symptom

Two pre-experiment positions (`169521...`, `115755...`) were stuck open
for 3 days even though the position_manager was running with TP=5%/
SL=7%/timeout=24h. The journal had a `closed_timeout` row for `115755`
from `10:58 UTC` with `size_usdc=0.0034` — a tiny dust fill, not a real
close. After that row, every subsequent cycle hit
`has_close_attempt_for_token` → True → skipped the position forever.

The same `115755` still held ~33 shares on-chain.

### Fix

`position_manager._already_closed` now overrides the journal terminal
status when on-chain CTF balance is still substantial:

```python
def _already_closed(self, token_id: str) -> bool:
    if not self.trade_log.has_close_attempt_for_token(token_id):
        return False
    on_chain = self._on_chain_shares(token_id)
    if on_chain is None:
        return True  # RPC failed — trust the journal
    if on_chain > 1.0:
        # Dust-close detected: journal says closed, but >1 share still
        # held. Retry the close.
        return False
    return True
```

Threshold of 1 share is conservative (the dust amount was 0.033
shares; meaningful positions are 5+ shares). `_on_chain_shares` queries
the deposit-wallet's CTF balance via
`get_balance_allowance(BalanceAllowanceParams(asset_type=CONDITIONAL,
token_id=...))`.

### Verification

After the fix + container rebuild + restart, the next cycle logged:

```
position_manager: token=847709108036407671 journal=closed but on-chain=3000.0000 shares — dust close detected, retrying close
position_manager dust skip: token=847709108036407671 notional=$0.0600
position_manager cycle: {evaluated: 5, closed_sl: 1, ...}
```

The retry detection works. The retry itself only fires when notional ≥
`MAINTAIN_MIN_EXIT_NOTIONAL_USDC=1.0` — token 847709 has 3000 shares
but mid is so low ($0.00002) that notional is $0.06, so it's
correctly skipped.

## Bug fix #2: stale dryrun pending order in `swarm.db`

### Symptom

`pending_orders.id=243` had `status='submitted'`, `agent='market_maker'`,
`order_id='dry_1'`. This was a dryrun residue from before the morning
restart. The CapitalAllocator counted it as `stale_state=1` and kept
flagging swarm_market_maker as `no_live_until_clean`.

### Fix

Direct SQL update on the swarm DB:

```sql
UPDATE pending_orders
SET status='cleared',
    note='cleared_dryrun_2026-05-07_per_user_request',
    updated_ms=strftime('%s','now')*1000
WHERE id=243;
```

After this, `pending_orders.status='submitted'` count is 0, and the
allocator's swarm_market_maker score moved 0.549 → 0.693.

The order_id was `"dry_1"` — there was nothing to cancel on-chain.
Pure DB cleanup.

## New mechanism: `allocator_sync` daemon

### Why

Before this session, the CapitalAllocator was read-only — it produced
recommendations, but the operator had to manually translate them into
`.env` changes and `docker compose restart` invocations. The user was
explicit: the allocator should be autonomous within the $20 budget,
without consulting the operator on every redistribution.

### What it does

Every 5 minutes, the daemon:

1. Builds a fresh `CapitalAllocator` report
   (capped at `ALLOC_SYNC_BUDGET_USDC`, default $20, window 24h).
2. Maps per-agent `recommended_usdc` to env vars:
   - `btc_daily` → `BTC_DAILY_RESERVE_USDC` and `EXECUTE_BTC_DAILY`
   - `scalper` → `SCALPER_RESERVE_USDC` and `EXECUTE_SCALPER`
   - `sum(swarm_*)` → `SWARM_RESERVE_USDC` (poly1) AND `TOTAL_CAPITAL`
     + `BOT_MODE` (swarm)
3. Writes only the env vars that changed.
4. Restarts the affected containers:
   - Any poly1 reserve change → `poly1-scalper`, `poly1-btc-daily`
   - Any swarm change → `polymarket-swarm`

### Files

| Path | What |
|---|---|
| `scripts/python/allocator_sync.py` | The daemon |
| `data/logs/allocator_sync.log` | Stdout/stderr |
| `data/trade_log.db` | Read by allocator (poly1 source) |
| `~/Desktop/poly/bot/data/swarm.db` | Read by allocator (swarm source) |

### Env vars (read by the daemon)

| Var | Default | Purpose |
|---|---|---|
| `POLY1_ENV_PATH` | `/app/host/poly1.env` | poly1 .env to write |
| `SWARM_ENV_PATH` | `/app/host/swarm.env` | swarm .env to write |
| `POLY1_DB_PATH` | `/app/data/trade_log.db` | poly1 DB (read) |
| `SWARM_DB_PATH` | `/app/swarm/data/swarm.db` | swarm DB (read) |
| `ALLOC_SYNC_BUDGET_USDC` | `20.0` | Total cap |
| `ALLOC_SYNC_WINDOW_HOURS` | `24.0` | Allocator look-back |
| `ALLOC_SYNC_CYCLE_SEC` | `300` | Loop interval (5 min) |
| `ALLOC_SYNC_ENFORCE` | `true` | If false, log only |

### How it's currently running

For now, as a host-side `nohup` background process (not yet a Docker
service):

```bash
POLY1_ENV_PATH=/Users/mymac/coding/poly1/.env \
SWARM_ENV_PATH=/Users/mymac/Desktop/poly/bot/.env \
POLY1_DB_PATH=/Users/mymac/coding/poly1/data/trade_log.db \
SWARM_DB_PATH=/Users/mymac/Desktop/poly/bot/data/swarm.db \
ALLOC_SYNC_ENFORCE=true \
ALLOC_SYNC_CYCLE_SEC=300 \
ALLOC_SYNC_BUDGET_USDC=20.0 \
PATH=$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin \
nohup python3 scripts/python/allocator_sync.py \
  > data/logs/allocator_sync.log 2>&1 &
```

PID is recorded in the start-up log line. To stop:
`pkill -f allocator_sync.py` or by PID.

**This does not survive a reboot.** A future task is to wrap it in a
Docker service with bind-mounted `.env` files and the docker socket.

### How to know it's working

```bash
tail -f data/logs/allocator_sync.log
```

Every 5 minutes you should see one log line per cycle, e.g.:

```
2026-05-07 20:02:05 INFO allocator_sync: allocator: btc_daily=$20.00 scalper=$0.00 swarm=$0.00 (budget $20.00)
```

If a value changes, additional lines like:

```
2026-05-07 20:02:05 INFO allocator_sync: swarm.env: set TOTAL_CAPITAL=0.0
2026-05-07 20:02:07 INFO allocator_sync: restarted container polymarket-swarm
```

### Operating rules (from the user)

1. The allocator decides allocation within the $20 cap. Period. Do not
   consult the operator on internal redistributions.
2. Agents may eventually request more from the allocator if they see
   strong profit signals. (Mechanism not yet built — a future task.)
3. Never touch the $41.74 outside the $20 budget. The cap is hard.
4. Don't bypass the allocator's safety gates by pre-tuning the .env. If
   it says `swarm_market_maker = $0` because of `no_live_until_clean`,
   that's the answer. The way to clear those gates is to fix the
   underlying signal/state, not to override the score.

## State of `.env` after this session

### `~/coding/poly1/.env` (entry-relevant vars)

```
EXECUTE="false"                    # poly1 main trader: shadow
EXECUTE_SCALPER="false"            # scalper: shadow (per allocator)
EXECUTE_BTC_DAILY="true"           # btc_daily: live with $20
EXECUTE_MAINTAIN="true"            # position_manager: live exit-only
SCALPER_RESERVE_USDC="0.0"
BTC_DAILY_RESERVE_USDC="20.0"
SWARM_RESERVE_USDC="0.0"
```

### `~/Desktop/poly/bot/.env`

```
BOT_MODE="dryrun"                  # synced from allocator
TOTAL_CAPITAL="0.0"                # synced from allocator
```

The swarm is in dryrun until the allocator decides otherwise. It still
runs and generates decisions; if those decisions raise its score (via
the allocator's `_read_swarm` path), the loop will lift its budget on
the next cycle.

## Allocator's verdict at session end

```
btc_daily:           recommend=$20.00  live_allowed=yes  market_context=+0.63
trader:              recommend=$0.00   no_live_until_clean (errors=1, veto_only=3)
scalper:             recommend=$0.00   no_live_until_clean (exit_path_observed)
swarm_market_maker:  recommend=$0.00   no_live_until_clean (now stale_state=0)
swarm_mean_reversion: recommend=$0.00  no_recent_signal
swarm_nothing_happens: recommend=$0.00 no_recent_signal
swarm_ai_decision:   recommend=$0.00   no_recent_signal
swarm_arbitrage:     recommend=$0.00   no_recent_signal
position_manager:    recommend=$0.00   exit_only_no_entry_budget (correct)
```

So in practice: only `btc_daily` is live for entries. All others are
gated. The allocator-sync daemon will lift the gates on its own as
agents earn back trust (clean cycles, no errors, fresh decisions).

## Empirical results from earlier today (for context)

3 scalper trades approved by MarketBrain before scalper was set to $0:

| time | market | side | entry | exit | result |
|---|---|---|---|---|---|
| 14:43 | btc-updown-15m | DOWN | 0.48 | 0.4312 | -10% (-$0.25) |
| 15:25 | btc-updown-15m | DOWN | 0.48 | 0.4312 | -10% (-$0.25) |
| 16:12 | eth-updown-15m | DOWN | 0.47 | 0.4214 | -10% (-$0.26) |

3/3 lost. ExitExecutor cut each via stop_loss within 4 seconds. The
"reversal" signal at score 0.4-0.5 has no demonstrated edge yet — the
data is too thin. The allocator subsequently dropped scalper to $0
based on `exit_path_observed` + general `no_live_until_clean` gating.
This is the system working as designed.

## Phantom-MTM correction (end-of-session)

I reported `MTM ≈ $8.59` throughout the day. **It was wrong.** The
journal still listed two "open" filled positions (`169521`, `115755`)
with 5.10 and 33.18 shares respectively — but on-chain CTF balances
are dust:

| Token | journal shares | on-chain shares | mid | sell value |
|---|---|---|---|---|
| 169521 (BUY @ 0.997) | 5.10 | **0.0095** | 0.9975 | $0.0095 |
| 115755 (BUY @ 0.110) | 33.18 | **0.0033** | 0.1050 | $0.0003 |
| 110702 Arsenal NO | resolved | 0.0000 | n/a (404) | $0 |
| 781404 Man City YES | resolved | 0.0240 | 0.2050 | $0.0049 |

**Real portfolio: cash $58.53 + MTM $0.01 = ~$58.54.** The other agent
(reporting "$60-something liquid in wallet, nothing else") was right.

This explains the "+$53.89 cash mystery" — the two markets resolved
YES (probably) and paid out, the cash arrived, but nobody journalled
the resolution → the journal still shows those rows as `filled`. My
earlier reports double-counted: the resolved cash already in the
wallet, plus a phantom MTM derived from `journal_shares × mid`.

**Implication for future agents:** never trust journal-derived MTM
alone. Always cross-check with `get_balance_allowance(asset_type=
CONDITIONAL, token_id=...)` for tokens that the journal claims are
still held. A "resolution sync" step (write a `resolved_yes` /
`resolved_no` row when on-chain shows the position is gone) would
prevent this drift.

## Why "everyone except btc_daily is in shadow" — read this before tweaking

The user found this confusing. The short version: **the allocator
cannot give capital to an agent that hasn't generated any decisions
in the last 24 hours.** No decisions → no score → no budget. This is
chicken-and-egg by design — agents need to demonstrate themselves in
shadow before earning live capital.

Per-agent reasons (from the allocator's own gating logic):

| Agent | Gating reason | Plain English |
|---|---|---|
| scalper | `no_live_until_clean` + `exit_path_observed` | 3/3 losses today; allocator lost confidence |
| swarm_mean_reversion | `no_recent_signal` | Hasn't fired in 24h — BTC too quiet for its 0.3% trigger |
| swarm_nothing_happens | `no_recent_signal` | No NewsAPI fresh news_signals |
| swarm_ai_decision | `no_recent_signal` | LLM call path not exercised (key issue likely) |
| swarm_arbitrage | `no_recent_signal` | No arb opportunities found |
| swarm_market_maker | `no_live_until_clean` | Stale_state cleared, but gen gating still applies |
| trader (poly1 main) | `errors=1, veto_only=3` | Had one error; the markets it found all got vetoed |
| btc_daily | `live_allowed=yes` | Clean record + market_context=+0.63 |

The fix path (do not bypass): let agents run shadow, generate
decisions; the allocator's score will lift them organically as data
accumulates. The auto-sync daemon will then promote them live without
operator action.

## Open follow-ups

- **Allocator-sync as a Docker service**: currently a host process, no
  reboot survival. Wrap in `docker-compose.yml` under a new `allocator`
  service with bind-mounted .env files and `/var/run/docker.sock`.
- **Agent → allocator request channel**: per the user's directive,
  agents should be able to ask for more capital when they see strong
  profit signals. No mechanism yet.
- **Two stuck pre-experiment positions** (`169521...`, `115755...`)
  still hold ~$8.59 MTM. The dust-fix lets the position_manager retry
  closes, but the actual sells skip on `min_exit_notional=$1.0` because
  notional × current mid is too small to be worth the gas. They will
  resolve at market resolution.
- **Unexplained +$53.89 cash** that appeared during the morning
  containers-stopped window. Likely settlements from
  `RECONCILE_NEEDED` tokens that resolved Down=1, but not yet
  reconciled to a transaction-level account.
- **MarketBrain calibration**: 0/3 wins on its first approvals. The
  reversal signal at score 0.35-0.50 needs more data and likely a
  higher threshold or different feature set.
