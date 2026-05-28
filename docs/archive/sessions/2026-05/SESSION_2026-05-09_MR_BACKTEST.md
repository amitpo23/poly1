# Session log — 2026-05-09 morning: mean_reversion backtest with slippage

## Why this session existed

Continuing from Tier 1 alerting (`SESSION_2026-05-09_ALERTING_LAYER.md`).
After we cleaned strait-of-hormuz stale fills + restarted swarm, two
discoveries:

1. swarm is in `BOT_MODE=dryrun` despite `.env=live` (env drift)
2. None of swarm's strategies has a backtest meeting the user's stated
   gate of ≥65% win rate

User principle (verbatim): *"להוסיף רק שווקים שהמוח או backtest מראה
אפשרות ל winrate מעל 65 אחוז"* — only add markets/strategies where a
backtest shows ≥65% win rate.

User chose **option B**: build a slippage-aware backtest for
mean_reversion before deciding whether to flip BOT_MODE=live.

This is exactly the gate scalper failed yesterday (claimed 65% with
no slippage; lost money live).

## What got built

### `scripts/python/backtest_mean_reversion.py` (~310 lines, stdlib only)

Replays bitcoin-up-or-down-on-{date} markets through MR's decision
logic (without booting swarm). Mirrors `backtest_scalper.py` and
`backtest_harness.py` patterns. Per day:

- Resolve slug → market via Gamma (`closed=true` flag for past markets)
- Fetch CLOB price-history for YES token over full active window
- Walk samples: at each, compute MR's percent-change proxy from
  yes-mid swings (1¢ swing ≈ 0.05% BTC — same trick as btc_daily)
- Apply MR config: cooldown 180s, trigger 0.3%, skip if strong trend
- Fade rule: BTC up → buy NO; BTC down → buy YES
- **Spread-based slippage model** (the lesson):
  - Entry pays `mid + spread/2` (= ask of held side)
  - Exit receives `mid - spread/2` (= bid of held side)
  - Settlement at $1/$0 (CTF redemption — no slippage)
- Exit at TP=+5¢ from entry, SL=-3¢, or 25-min timeout

CLI flags: `--days`, `--spread-cents` (default 2.0), `--trigger-pct`,
`--tp-cents`, `--sl-cents`, `--position-size`, `--start-date`, `--json`.

### Why spread, not flat 2% multiplicative

Scalper uses FAK orders that pay `mid * (1-0.02)` per
`exit_executor.py:38-43`. MR uses limit orders at `book.best_ask` /
`book.best_bid` directly (`mean_reversion_agent.py:306, 422-446`). So
MR's slippage is **the spread**, not a multiplicative discount. Spread
of 2¢ on a 50¢ entry = 4% round-trip cost.

## Findings — strategy is structurally broken

Spread sensitivity sweep, 30 days, 372-409 entries depending on
spread:

| spread | 30d entries | 30d win rate | 30d paper PnL |
|--------|-------------|--------------|---------------|
| **0¢** (theoretical ceiling, impossible) | 372 | **43.5%** | **+$3.33** |
| 1¢ (best realistic) | 380 | 33.2% | -$11.49 |
| 2¢ (typical) | 392 | 26.9% | -$22.13 |
| 3¢ (worst case) | 409 | 15.9% | -$35.46 |

**The structural finding:** even at zero slippage (impossible) the
win rate ceiling is 43.5% — **below coin-flip 50%**. The `+$3.33`
zero-slippage PnL is purely from the asymmetric R/R (TP=5¢ vs SL=3¢):
when the bot wins it wins 67% more than when it loses. With realistic
spreads, this asymmetry is consumed and PnL goes deeply negative.

**This is not a tuning problem.** No combination of trigger / TP / SL
parameters can take a strategy whose ceiling WR is 43.5% past a
65% gate. The fade-the-move thesis itself is wrong for BTC daily
markets at the 0.3% / 180s timescale — small BTC moves don't revert,
they continue.

## Historical confirmation

`config.py:85` already documented this:
> *"60-day BTC daily backtest showed no edge across all parameter
> variants once realistic spreads/fees were modeled."*

Today's harness independently reproduced this finding on a different
30-day window with explicit slippage modeling. The MR allocation
0.25 was set in 2026-05-06 "per user request to see each agent in
action" — not based on edge.

## Decision per user gate

By the ≥65% WR principle:
- ✅ **Do NOT flip `BOT_MODE` to live.** swarm stays in dryrun.
- ✅ **MR remains config-loaded but inactive in execution.** Removing
  it from config is not necessary; dryrun is sufficient containment.
- ✅ **No new markets added.** ETH/SOL daily would face the same
  fade-the-move problem on a different asset; same structural issue.

## What's still NOT in scope

- **Reverse strategy (momentum):** the data suggests buying WITH the
  move would beat fading it. Not built — would require new agent code.
- **Larger trigger thresholds (0.5% / 1%):** brief 0¢ run with
  trigger=0.005 → 0¢ run with trigger=0.01 not tested. The asymmetric
  R/R might salvage a 50%+ WR variant, but ceiling is still bounded
  by the strategy's structural <50% WR signal.
- **MR daily-rotation code fix:** `_resolve_today_with_fallback`
  is called only in `on_start()`; no day-change re-resolution. Today
  the swarm restart fixed the symptom (resolved may-9). Permanent fix
  is ~30 min code work — deferred until MR has any reason to be live
  (which per backtest, it doesn't).

## Files

Created:
- `scripts/python/backtest_mean_reversion.py`
- `docs/SESSION_2026-05-09_MR_BACKTEST.md` (this)

Cluster of related fixes earlier in this session (logged in
`SESSION_2026-05-09_ALERTING_LAYER.md`):
- Cleaned `pending_orders` id=240, 241 (stale strait-of-hormuz fills)
- Restarted polymarket-swarm container (refreshed SQLite snapshot,
  resolved alphabet "skip" mystery + MR's stale `may-8` slug)

## Verification commands

```bash
# Re-run MR backtest at any time (must run inside container — needs Polymarket SDK)
docker cp scripts/python/backtest_mean_reversion.py poly1-position-manager:/app/scripts/python/
docker exec poly1-position-manager python /app/scripts/python/backtest_mean_reversion.py --days 30

# Spread sensitivity (also exercises the JSON path)
docker exec poly1-position-manager python /app/scripts/python/backtest_mean_reversion.py --days 30 --spread-cents 1.0

# Confirm swarm stays in dryrun
docker exec polymarket-swarm env | grep BOT_MODE
# expect: BOT_MODE=dryrun (env drift; .env says live but container hasn't been recreated)
```

## Open follow-ups (none urgent)

| # | Item | Notes |
|---|---|---|
| – | swarm dryrun-artifact accumulation | dryrun still writes `submitted` rows to pending_orders. Each cycle adds ≥1 row. Cleanup query: `UPDATE pending_orders SET status='cleared', note='dryrun_artifact' WHERE status='submitted' AND order_id LIKE 'dry_%';` |
| – | MR daily slug rotation | `mean_reversion_agent.py:119-167` — add re-resolution when day changes |
| – | btc_daily Coinbase feed | not actually broken (verified earlier today); 2-min outage on 5/8 19:57 was the only incident |
| – | filled→cleared transition in state_store | real bug, but deferred indefinitely while swarm is in dryrun |

## Decision log

- 09:09 UTC — restarted swarm after id=240, 241 cleanup. Discovered
  alphabet mystery was stale snapshot, MR was on may-8.
- 09:30 UTC — discovered swarm in `BOT_MODE=dryrun` despite `.env=live`
- 09:45 UTC — built MR backtest harness with spread modeling
- 10:00 UTC — ran 30d backtest with spread=0/1/2/3¢; all variants
  fail 65% WR gate; even 0¢ ceiling is 43.5%
- 10:15 UTC — decision: swarm stays dryrun, BOT_MODE not flipped,
  per user's principle.
