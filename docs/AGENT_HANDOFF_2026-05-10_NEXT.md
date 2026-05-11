# Handoff for the next agent (last update 2026-05-10 end-of-day)

## Read order

1. `deploy/CURRENT_STATUS.md` — top section is the 2026-05-10 EOD summary
2. `docs/SESSION_2026-05-10_SCOUT_PLAN.md` — full design + execution log
3. `~/coding/poly vault/Decisions/2026-05-10_three_strategies_failed.md`
   — verdict on today's 3 candidate strategies
4. `docs/SESSION_2026-05-09_*.md` — yesterday's MR + scalper sweep work
5. `docs/AGENT_HANDOFF_2026-05-08_NEXT.md` — earlier baseline (still relevant for posture)

## Current live posture (end of 2026-05-10)

```
              poly1                         swarm
              ─────                         ─────
  btc_daily LIVE $12.50  (only proven horse)
  scalper SHADOW $1.50   (sweep killed it)
  position_manager run   (sidecar, exits only)
                                            All 5 sub-agents DRYRUN
                                              - market_maker, mean_reversion,
                                                nothing_happens, ai_decision,
                                                arbitrage stub
                                            Combined alloc $6
  cash $54.26 stable for 70+ hours
```

## What's running automatically

- **state_watcher** at :17 / :47 each hour — silent unless something
  changed (fills, container down, RECONCILE_NEEDED, MAY_HAVE_FIRED,
  scout opportunities)
- **scout** at :23 each hour — scans Gamma + Tavily, writes to
  `data/scout.db`. Opportunities surface to the watcher's :47 cycle.
- **allocator_sync** every 5 min — recomputes per-agent budget from
  PnL + WR.
- **position_manager** every 60s — TP/SL/timeout checks for any
  open btc_daily / scalper positions.

## Today's 8 commits + headline finding

| commit | what |
|---|---|
| `ab041d1` | wins/losses/win_rate per agent + cash-PnL on closed_* |
| `001f849` | scout cron (Tavily + Gamma scan) |
| `62ca40e` | code-review fixes + nothing_happens backtest harness |
| `73b4202` | session docs + #5/#9 strategy backtests |
| `438e3c8` | handoff doc |
| `b2c7f96` | date attribution fix |
| `e328042` | manual entry CLI + per-position TP override |
| `cf4f1fb` | momentum backtest verdict (no agent built) |

**Headline:** 4 strategies tested via the new data-api harness today.
**0/4 pass** the user's 55% WR + stability gate.

| strategy | 0-30d | 30-60d | 60-90d | verdict |
|---|---|---|---|---|
| nothing_happens | 32.4% / +$8 | 4.5% / -$3.9 | 12.5% / -$14 | regime-dep ❌ |
| #5 Resolution Drift | n=0 | n=0 | n=1 | inconclusive |
| #9 Range-Bound | 22% / -$40 | 20% / -$64 | 24% / -$97 | broken ❌ |
| BTC daily momentum chase | 30.4% / -$1.2 | 33.3% / -$0.8 | n=0 (gap) | fails ❌ |

## Manual entry capability (new today)

`scripts/python/manual_entry.py` lets the operator place directional
bets on any Polymarket slug with auto-exit at +X% TP. Used for
user-conviction trades that don't fit any backtested algorithmic
strategy. Backtest gates apply only to AUTO-firing agents; manual
trades are the operator's explicit risk.

```
docker exec poly1-position-manager python /app/scripts/python/manual_entry.py \
  --slug <slug> --side YES|NO --size-usdc 2.5 --tp-pct 0.20 [--no-sl] --execute
```

position_manager handles auto-exit via `tp_pct_override` field on
response_json. No trades placed via this tool yet today; ready for
operator use.

## Critical thing the next agent should know

**btc_daily on 2026-05-08 (last day it traded): cash-PnL +$0.44 BUT
strategy-PnL -$2.63** on 3 closes. The +$0.44 came from dust
monetization (8.13/9.68 shares sold per close even though only 6
were paid for per open). The 60.7% backtest WR may overstate the
actual edge.

**2026-05-09 and 2026-05-10: 0 btc_daily trades.** The market both
days had one side too cheap (NO < 0.30 floor) — bot correctly skipped.
The 60+ hour silence is the strategy working as designed, not broken.

**Action:** wait for 30+ live btc_daily trades. Watch
`strategy_pnl_usdc` field on closed_* rows (added today via A1
commit). If strategy_pnl stays negative across 30+ trades while
cash_pnl is positive, the strategy is monetizing dust not winning
on signal — defund.

## Key technical discovery

**`data-api.polymarket.com/trades?market=<conditionId>` returns full
paginated trade history for ANY market.** CLOB's `/prices-history`
returns 0 samples for high-volume political markets. data-api is
the canonical history source going forward. Used in:
- `scripts/python/backtest_nothing_happens.py`
- `scripts/python/backtest_strategy_5_9.py`

## What's NOT done (deliberately)

- **No new agents activated.** btc_daily is the only LIVE strategy.
- **swarm BOT_MODE not flipped** (still `dryrun` despite `.env=live`
  — env drift; intentional, no agent passes the gate).
- **No code merger** between poly1 and swarm. CLAUDE.md prohibition
  stands.
- **No Tavily wired into nothing_happens entry.** The Tavily
  integration is in `agents/application/news_signal.py` (poly1
  side) but `nothing_happens` lives in swarm and would need a
  bridge. Deferred until a backtest justifies it.

## Open follow-ups (none urgent)

| # | Item | Effort | Why deferred |
|---|---|---|---|
| – | btc_daily live data trend | passive, watching | Need 30+ trades, days |
| – | Strategy #5 with looser filter | 30min | Priors say still fails |
| – | Strategy #6 Correlated Pairs | 3-4h | Needs cross-market sync |
| – | Strategy #7 Whale Tracking | 1-2 days | Needs Polygon RPC scraper |
| – | Resolution-driven `filled→cleared` in swarm state_store | 1h | Real bug, but moot while DRYRUN |
| – | mean_reversion daily slug rotation | 30min | Restart fixes; moot while DRYRUN |
| – | Many untracked core files | hours | Scope creep — separate cleanup task |

## 2026-05-11 addition: research committee sidecar

A TradingAgents-inspired committee brain was added, but only as a read-only
research layer.

- Code: `agents/application/research_committee.py`
- Scout integration: `scripts/python/scout.py`
- Scout DB table: `data/scout.db:research_reports`
- Memory table: `data/trade_log.db:decision_reflections`
- Full session note: `docs/SESSION_2026-05-11_RESEARCH_COMMITTEE.md`

Important safety invariant: `approved_for_live` is hard-blocked to `0`.
Committee output can recommend research, paper trading, or backtesting only.
It cannot allocate capital or place trades.

Latest real row written by a one-off scout pass:

```text
bitcoin-up-or-down-on-may-11-2026 | mean_reversion |
reject_live_backtest_required | final_score=0.086 | risk_score=0.64 |
approved_for_live=0
```

## Quick health-check commands

```bash
export PATH=$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin

# Containers
docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'poly1|swarm|btc'

# Allocator with WR (today's A1 work)
docker exec poly1-position-manager python /app/scripts/python/capital_allocator.py --hours 168

# Cash on-chain
docker exec poly1-position-manager python -c \
  "from agents.polymarket.polymarket import Polymarket; \
   print(f'\${Polymarket(live=True).get_usdc_balance():.4f}')"

# Scout opportunities (today's discovery layer)
sqlite3 ~/coding/poly1/data/scout.db \
  "SELECT date(ts), strategy_match, score, market_slug \
   FROM scout_opportunities ORDER BY id DESC LIMIT 10;"

# Price snapshots accumulating (Phase B, since 2026-05-10)
sqlite3 ~/coding/poly1/data/scout.db \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM price_snapshots;"
```

## What can change while you're not watching

- `state_watcher` will alert if btc_daily fires or any container falls
- `scout` will accumulate opportunities; watcher surfaces them
- `price_snapshots` will grow — useful for own-data backtest in ~30 days
- `allocator_sync` may defund btc_daily if realized_pnl crosses
  -$2 on 24h window (currently far from there)

## End of handoff

Patient, disciplined, no risky moves. The data is what it is.
btc_daily is the horse; we wait. The infrastructure built today
(scout, harness, vault) accelerates the next wave of testing
when there's a new strategy idea worth investing in.
