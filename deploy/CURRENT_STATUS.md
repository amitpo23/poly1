# Current Status

Date: 2026-05-12

## Active /goal

`/goal` is now defined in
`docs/GOAL_PROFITABLE_AGENT_LOOP.md`:

> every approved agent must become evidence-profitable before it receives
> scalable capital.

The loop is tracked with:

```bash
python scripts/python/goal_status.py --hours 24
```

or continuously:

```bash
python scripts/python/goal_status.py --hours 24 --watch --interval 900
```

Current interpretation: the goal is open until every approved agent is either
`profitable` or intentionally unfunded/disabled. This is a discipline loop,
not a promise of profit and not permission to loosen gates blindly.

## Latest changes (2026-05-12 13:47 IDT, live funnel diagnosis)

**Question answered:** why are agents healthy but still not producing regular
live trades/profit?

### Findings

1. **Main trader brain was crashing before market selection.**
   The `poly1` trader fetched 297 tradeable events, then failed inside
   Chroma/OpenAI embeddings with OpenAI `insufficient_quota`. This stopped the
   cycle before ranked markets or orders were reached.

2. **news_signal was creating false-looking neutral volume.**
   The news source and market matcher worked, but classifier calls hit
   `insufficient_quota`. The old degraded mode inserted rows as
   `status=news_signal`, `direction=neutral`, `materiality=0.0`. That made the
   dashboard look busy while `news_shock` had no actionable bullish/bearish
   signals.

3. **near_resolution is working but blocked by confidence.**
   It repeatedly finds 3 candidates, then rejects all at Tavily confidence
   `0.50 < 0.65`. This is conservative; lowering the gate would create trades
   without confirmed edge.

4. **wallet_follow has no fresh source trades.**
   `wallet_watcher` tracks 20 wallets and scans every ~2 minutes, but added
   0 fresh `wallet_signals` in the observed window, so `wallet_follow` has
   nothing to act on.

5. **swarm is live but stuck on its own safety brake.**
   One market-maker order was reconciled as filled. The bot now repeatedly
   skips that Hormuz market because a submitted/filled pending row exists. That
   prevents duplicate exposure, but it also means no repeated trading there
   until exit/settlement handling releases the market.

6. **position exits are noisy but no longer actively looping.**
   The 552 `close_failed` rows came from market `572733` before escalation to
   `resolved_loss`. Recent position-manager logs show `skipped_already_closed`,
   so this specific loop is stopped.

### Changes shipped in this pass

- `agents/application/trade.py`
  - OpenAI quota failures in event filtering, market filtering, or per-market
    trade analysis now write `skipped_gate` with `ai_filter_unavailable` or
    `ai_analysis_unavailable` instead of crashing the daemon or marking the
    market as a strategy failure.
- `agents/application/news_signal.py`
  - Classification quota failures now write `status=classifier_failed` rather
    than actionable `news_signal` neutral rows.
  - Added a cooldown after `insufficient_quota` so one bad quota state does not
    hammer the API for every matched headline.
- `agents/application/capital_allocator.py`
  - `fresh_news_signals` now counts only `status='news_signal'` rows, so
    `classifier_failed` diagnostics do not inflate allocator confidence.
- `.dockerignore`
  - Added to keep `.env`, local DBs, screenshots, and local caches out of the
    built image. Runtime env still comes from docker-compose `env_file`.
- `tests/test_news_signal.py` and `tests/test_trader.py`
  - Added quota-degradation tests and isolated RiskGate reserve tests from live
    env vars.

### Current interpretation

This is not a Docker health problem. The system is alive, but the alpha funnel
is blocked by missing/failed intelligence:

- AI-dependent paths cannot trade while OpenAI quota is exhausted.
- Tavily-only near-resolution has candidates but no >0.65 confidence proof.
- Wallet-follow has no fresh copied trades.
- Scalper remains mostly shadow/no-edge; recent brain decisions are dominated
  by `too_close_to_expiry` and `edge_score_too_low`.

The right next step is not to loosen gates blindly. The next profitable-work
step is to add a non-OpenAI research fallback/router that can produce a
structured probability estimate from Tavily/RSS/Gamma/DB evidence, then let
only positive-EV candidates reach paper/live probe.

## Latest changes (2026-05-12 morning, unblock inactive agents)

**Goal:** All 4 silent agents were reporting healthy but doing nothing.
Root-cause diagnosed and fixed. Commits `437f9f6` + `08b1914`.

### Changes shipped

1. **wallet_watcher — Polymarket API migration** (`437f9f6`)
   Leaderboard endpoint moved from `/leaderboard` (404) to
   `/v1/leaderboard?timePeriod=ALL&orderBy=PNL&limit=N`.
   Response schema changed: `pnl` replaces `profit`/`profitLoss`;
   `tradesCount` field removed (now stored as `trades_30d: 0`).
   Result: `leaderboard scan done — added=20 total_watched=20` ✅

2. **news_signal — min_hits lowered 2→1** (`437f9f6`)
   `match_news_to_markets()` was requiring 2+ keyword overlaps between
   headlines and market question — too strict for short headlines.
   Lowered to `min_hits=1`. Pipeline now reaches classification stage
   and writes rows: `inserted=12` per scan ✅
   *Note:* OpenAI `gpt-4o-mini` is currently returning many 429
   `insufficient_quota` responses. Some calls still returned 200 during
   the checked window, but this should be treated as a quota/billing
   capacity issue, not just a harmless transient rate limit. Rows are
   still written with `direction=neutral` on failure — acceptable
   degraded mode for signal capture, not for high-confidence trading.

3. **near_resolution — dual filter fix** (`437f9f6` + `08b1914`)
   Two independent filters were eliminating all candidates:
   - `NEAR_RESOLUTION_MIN_LIQUIDITY` 3000→500 USDC (`.env` + `.env.example`)
   - `NEAR_RESOLUTION_MAX_HOURS` 36→336h (2 weeks). All active Polymarket
     binary markets currently resolve 280–1924h from now; the 36h window
     captured nothing. 336h catches Europa League, EPL, Texas primaries, etc.
   Added diagnostic INFO log when 0 candidates pass time filter so the
   agent is observable without code dives.
   Result: `3 candidates after filters` ✅
   Tavily filters them at `confidence 0.50 < 0.65` — conservative but correct.

4. **swarm — mode=live** (previous session)
   `BOT_MODE` env was stale (`mode=dryrun`). Fixed via `force-recreate`.
   Swarm now boots `mode=live | capital=$4.5`.
   Follow-up check on 2026-05-12 found one live submitted market-maker
   order in `pending_orders`; `scripts/reconcile_orders.py --execute`
   confirmed it was matched and wrote the local fill. The row now remains
   `filled` as the intended safety brake until exit/settlement tracking
   releases that market.

### Operational status (2026-05-12)

All 11 containers `Up (healthy)`:

| Container | Status |
|-----------|--------|
| poly1 (trader) | ✅ healthy, EXECUTE=false |
| btc_daily | ✅ healthy, live |
| position_manager | ✅ healthy |
| near-resolution | ✅ healthy, 3 candidates/scan, Tavily filtering |
| news-shock | ✅ healthy |
| news-signal | ✅ healthy, inserted=12/scan, 429 transient |
| wallet-watcher | ✅ healthy, 20 wallets tracked |
| wallet-follow | ✅ healthy |
| scalper | ✅ healthy |
| dashboard | ✅ healthy |
| grafana | ✅ healthy |

**btc_daily** remains the only strategy with live execution evidence.
**swarm** is live (`mode=live`) but small capital ($4.5). One matched
market-maker fill was reconciled locally on 2026-05-12.
**near_resolution** is scanning and filtering — will execute when
Tavily confidence crosses 0.65 threshold on a real event.

### Open issues
- `news_signal` OpenAI 429: logs include `insufficient_quota`; monitor
  billing/quota before trusting high-confidence classification volume.
- `near_resolution` Tavily heuristic scores 0.50 on most results due
  to common words ("yes", "no", "will") appearing in all news text.
  Consider improving the confidence heuristic if agent stays idle for
  >1 week once window has viable candidates.
- Swarm market-maker now has one `filled` safety-brake row after
  reconciliation. Exit/settlement handling is still needed before that
  market should be reused.

See `docs/SESSION_2026-05-11_AGENT_TRADING_STABILIZATION.md` for previous session.

## Latest changes (2026-05-11, agent trading stabilization)

**Goal:** get the agents unstuck without pretending weak strategies are
ready for live capital.

**Shipped today:**

1. **Swarm dry-run order unblocking** in `/Users/mymac/Desktop/poly/bot`.
   Stale synthetic `order_id LIKE 'dry_%'` submitted rows now auto-clear
   after `SWARM_DRYRUN_SUBMITTED_TTL_SECONDS` (default 30s). This fixed
   the market_maker loop that was stuck on old dry-run submitted rows.

2. **Swarm SQLite hardening.** Added SQLite timeout/busy_timeout,
   read-only healthcheck, and rebuilt the active DB with `VACUUM INTO`.
   Current swarm DB integrity is `ok`.

3. **Swarm dry-run book realism.** The mock orderbook no longer changes
   randomly on every immediate call, so the slippage guard can be tested
   instead of rejecting fake 20-30 cent jumps.

4. **Poly1 broken-market retry suppression.** Repeated hard execution
   failures (`404`, no orderbook, no asks, live price above recommended)
   now route to `skipped_gate` / pre-skip instead of polluting strategy
   failure metrics.

5. **OpportunityRouter.** New router maps scout/research rows to
   `live_probe`, `backtest`, `paper`, or `reject`. Current scout output
   routes BTC mean reversion to `backtest`, not live.

6. **Dashboard Router tab.** Streamlit now shows OpportunityRouter
   routes plus 24h "why no trade" blockers and brain veto counts. The
   dashboard was rebuilt/restarted and its health endpoint returned `ok`.

7. **EV-first routing policy.** OpportunityRouter now computes:
   `estimated_true_probability - entry_price - slippage - error_margin`.
   Live probes are blocked unless probability is explicit/proven,
   historical/paper edge is positive, liquidity/spread gates pass, and EV
   clears the live threshold. Current BTC mean-reversion candidate is
   persisted as `backtest` with EV `-0.025`, not live.

**Validation:**
- `poly1:local` rebuilt.
- `python -m unittest tests.test_trader tests.test_opportunity_router tests.test_research_committee tests.test_brain_journal -v`
  passed: **40 tests OK**.
- Main `poly1` trader recreated on the new image and remains
  `EXECUTE=false`.
- Swarm recreated and remains `BOT_MODE=dryrun`.
- Dashboard container recreated on the new image.

**Operational status:**
- `btc_daily` remains the only live strategy with prior backtest support.
- `swarm` is active but dry-run only.
- `scalper` is still high risk; latest realistic backtests did not
  justify scaling it.
- Do not flip swarm to live until OpportunityRouter emits `live_probe`
  and the exit agent has a matching exit thesis.

See `docs/SESSION_2026-05-11_AGENT_TRADING_STABILIZATION.md`.

## Latest changes (2026-05-10 evening, manual_entry + momentum)

**Two more pieces shipped today:**

1. **Manual entry CLI** (`e328042`) — `scripts/python/manual_entry.py`
   for user-driven directional bets ($2.50/trade, +20% TP, optional
   no-SL). position_manager now supports per-position `tp_pct_override`
   via response_json on the filled row. Algorithmic agents inherit
   the brain's compound exit logic unchanged.

2. **Momentum backtest verdict** (`cf4f1fb`) — chase the BTC move
   instead of fading it. WR 30.4% / 33.3% / n=0 across 3×30d windows.
   **Fails the gate.** No agent built. Harness stays for re-runs.

End-of-day strategy-test count: **4 candidates today (nothing_happens,
#5, #9, momentum), 0 pass.** Plus yesterday's scalper (0/19) and
mean_reversion (structurally broken). btc_daily remains the only
strategy with backtest evidence.

## Earlier changes (2026-05-10 afternoon, full sweep)

**3 strategies tested today via new data-api harness; 0/3 pass the
55% WR + stability gate.**

| strategy | 0-30d WR | 30-60d WR | 60-90d WR | verdict |
|---|---|---|---|---|
| nothing_happens | 32.4% | 4.5% | 12.5% | regime-dependent ❌ |
| #5 Resolution Drift | n=0 | n=0 | n=1 | inconclusive (filter too tight) |
| #9 Range-Bound | 21.9% | 20.2% | 23.7% | structurally broken ❌ |

**Discovery:** CLOB `/prices-history` returns 0 samples for high-
volume political markets. `data-api.polymarket.com/trades?market=
<conditionId>` returns full paginated trade history for any market.
This unlocked nothing_happens + #9 backtests; would also support any
future per-market backtest.

**Commits today:**
- `ab041d1` — Phase A: WR infrastructure (wins/losses/win_rate +
  pnl_usdc_real on closed_*)
- `001f849` — Scout S1-S3: Tavily integration, hourly cron,
  watcher alerts
- `62ca40e` — Code review fixes (3 bugs caught) + nothing_happens
  backtest harness

**Cron jobs running:**
- `1590c7bb` — state_watcher at :17/:47 (silent unless change)
- `131149fb` — scout at :23 (writes opportunities + price_snapshots)

**Decision:** btc_daily stays the only LIVE strategy. swarm stays
DRYRUN. Scout continues surfacing candidates for human review.
Phase B price_snapshots table starts collecting from today; can be
used for follow-up backtests in ~30 days.

## Earlier (2026-05-10 morning, Phase A + Scout plan)

**Committed `ab041d1`:** WR infrastructure for `CapitalAllocator`.
- AgentScore now tracks `wins`/`losses`/`win_rate`
- `position_manager` augments closed_* response_json with
  `pnl_usdc_real` (cash) + `strategy_pnl_usdc` (matched-shares)
- 3 historical close rows from 2026-05-08 backfilled
- Output: `position_manager` reports W/L=2/1 WR=66.7% PnL=+$0.44

**Surprise finding from backfill:** btc_daily's 3 closes yesterday had
**cash-PnL +$0.44** but **strategy-PnL -$2.63**. The cash positive
came from dust monetization (8.13/9.68 shares sold at new sell price
when only 6 were paid for per open). The 60.7% backtest WR may
overstate actual edge; need 30+ live trades to confirm.

**Scout plan approved (not yet built):** Hourly cron to scan
Gamma + Tavily across all strategies, score by replayable heuristics,
write to `scout_opportunities` table. `state_watcher` alerts on new
rows. **Does NOT auto-activate any agent** — surfaces candidates for
human review + backtest. Replaces the "auto-tuner" pattern that bit
us with scalper yesterday.

See `docs/SESSION_2026-05-10_SCOUT_PLAN.md` for the full plan.

## Earlier changes (afternoon 2026-05-09, exhaustive strategy sweep)

User asked: *"בטוח שיש שווקים שאחת מהאסטרטגיות שלנו תעבוד בלמעלה מ 55
winrate"*. Answer after exhaustive testing: **no**.

Built 3 backtest harnesses today (MR + scalper sweep + market sweep
across 5 categories). Combined with morning's MR backtest, we now have
empirical evidence on every strategy concept replayable from CLOB
price-history.

**Headline result:** market_sweep on 90-day window initially showed
2 cells passing 55% WR (`sports`/`other` × `no_bias_hold`). Split
test on 30/30/30 day windows revealed instability:

| sports / no_bias_hold | 0-30d | 30-60d | 60-90d |
|---|---|---|---|
| WR | 64.0% | 57.5% | **44.9%** |
| PnL | +$21.84 | +$4.26 | **-$7.55** |

The 90-day average was a regime-specific artifact (recent underdogs
won more than expected). Earlier window broke. **No strategy passed
55% WR with stability.**

CLOB only retains ~90 days of price-history (verified — 1 of 10
markets aged 90-180d returned data). Can't validate longer windows.

**Decision:** btc_daily remains the only strategy with stable
backtest evidence (60.7% / 30d). Everything else fails. swarm stays
in dryrun. See `docs/SESSION_2026-05-09_MARKET_SWEEP.md`.

## Earlier (morning 2026-05-09, MR backtest + alerting layer)

Three concrete outcomes this morning:

1. **Tier 1 alerting layer built** — `scripts/python/state_watcher.py`
   replaces the verbose 30-min cron with diff-driven alerts. Silent on
   no-change; alerts on new fills, container down, RECONCILE_NEEDED,
   MAY_HAVE_FIRED. Cron updated to fire short prompt (`שקט.` when no
   change). See `docs/SESSION_2026-05-09_ALERTING_LAYER.md`.

2. **swarm dryrun discovery** — `.env` says `BOT_MODE=live` but the
   running container reports `mode=dryrun` (env drift, never recreated
   after `.env` change). Side effect: market_maker writes `submitted`
   rows to `pending_orders` for every paper-quote, never sends to CLOB.
   These accumulate as dryrun artifacts (`order_id LIKE 'dry_%'`).

3. **mean_reversion backtest with slippage modeling — strategy fails
   65% win-rate gate.** `scripts/python/backtest_mean_reversion.py`
   built with spread-based slippage (entry=ask, exit=bid). 30 days, 372
   entries:

| spread | 30d win rate | 30d paper PnL |
|---|---|---|
| **0¢** (ceiling, impossible) | **43.5%** | +$3.33 |
| 1¢ (best realistic) | 33.2% | -$11.49 |
| 2¢ (typical) | 26.9% | -$22.13 |
| 3¢ (worst) | 15.9% | -$35.46 |

Even at zero slippage the WR ceiling is **below 50%**. Fade-the-move at
0.3% / 180s on BTC daily is structurally a losing strategy — small BTC
moves don't revert, they continue. Confirms the pre-existing comment
in `config.py:85`: "60-day BTC daily backtest showed no edge across
all parameter variants once realistic spreads/fees were modeled."

**Decision per user's principle (≥65% WR gate):**
- swarm stays in `BOT_MODE=dryrun`
- MR allocation 0.25 left as-is (config-loaded, dryrun-contained)
- BOT_MODE flip blocked until any swarm strategy passes 65% WR with
  realistic slippage

See `docs/SESSION_2026-05-09_MR_BACKTEST.md`.

## Earlier (late evening 2026-05-08, scalper disabled — backtest correction)

**Honest correction.** The "first data-driven tuning" earlier today
(scalper threshold 0.35 → 0.60 based on backtest showing 65% win rate)
was based on a **flawed backtest** that didn't model live FAK SELL
slippage. The advisor caught this; re-running the backtest with the
2% slippage that `exit_executor.py:38-43` actually applies produced:

| threshold | Entries | Win Rate | Paper PnL (2% slippage) |
|---|---|---|---|
| 0.35 | 110 | 27.4% | **-$5.92** |
| 0.50 | 73 | 21.7% | **-$4.50** |
| 0.60 | 47 | 23.3% | **-$1.57** |

Every threshold loses money once realistic execution costs are
modeled. The strategy's edge (5-7%) is smaller than the round-trip
slippage cost (4%) on current Polymarket 15m spreads. **Scalper is
structurally unprofitable at current spreads.**

**Action taken:**
- `EXECUTE_SCALPER="true" → "false"` in `.env`
- `poly1-scalper` recreated; will scan but not place orders
- Stale market_maker pending row (id=244, dry_1) cleared in swarm.db
- The "65% win rate" claim from the earlier backtest is retracted

**The lesson:** every backtest claim needs to model real execution
costs (slippage, fees) explicitly. The harness now takes a
`--slippage` flag (default 0.02 = matches live).

## Latest changes (evening 2026-05-08, backtest harnesses + first data-driven tuning)

User asked "we don't see the path to scale" — the specific trigger that
finally justified building a backtest harness (yesterday it was YAGNI;
today it's the evidence-shaped question we needed). Per advisor:
"build it, btc_daily only, three windows for stability."

- **`scripts/python/backtest_harness.py`** (~470 lines) — replays
  historical bitcoin-up-or-down markets through `BtcDailyEngine.maybe_enter`.
  Pulls full active-period CLOB price-history per day (start_ts/end_ts
  works for resolved markets too; `closed=true` Gamma flag exposes them).
  Runs paper TP/SL exits at MAINTAIN_*_PCT thresholds, settles open
  positions at terminal mid. Output: per-day PnL + 7d/14d/30d window
  summaries.
- **btc_daily backtest verdict**: positive paper PnL across all windows.
  30 days = 28 entries, 60.7% win rate, +$0.61. 14d = 14 entries, 57.1%,
  +$0.03 (barely positive). 7d = 7 entries, 57.1%, +$0.16. Edge is real
  but thin: ~$0.02/day on $3 trades = 0.7%/day return. Strategy is worth
  keeping live; scaling to larger size is the next decision (post-experiment).
- **`scripts/python/backtest_scalper.py`** (~280 lines) — replays
  historical 15-min crypto markets through `MarketBrain.evaluate_scalper_entry`
  using local `scalper_pairs` table (1234 expired pairs, ~2 days). Per pair:
  fetch UP+DOWN price-history, walk ticks, simulate paired-leg entry +
  TP/SL/expiry exit. v2 added; reuses MarketBrain directly.
- **scalper backtest verdict — threshold sweep findings**:
  - `min_edge_score=0.35` (current default): 115 entries, 45.9% win
    rate, +$0.06 PnL → essentially noise.
  - `min_edge_score=0.50`: 69 entries, 46.8%, +$0.73.
  - `min_edge_score=0.60`: 46 entries, **65.4% win rate**, **+$2.10**.
- **Action taken (data-driven, advisor-validated)**: changed
  `MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE` 0.35 → 0.60. Scalper container
  recreated; new threshold live. This is the first parameter change
  ever made on backtest evidence rather than intuition. Expected:
  ~50% fewer entries, ~40% higher win rate.
- **`mean_reversion`, `nothing_happens`, `ai_decision`, `market_maker`,
  `arbitrage`** are NOT replayable from price-history alone (they need
  news feeds, LLM context, orderbook depth, or hardcoded pairs). Out
  of v1/v2 scope.

## Latest changes (afternoon 2026-05-08, B1 + B2-mini)

User identified that 4 of 8 swarm sub-agents were either broken or
gated. We fixed two and excluded one rather than building from scratch:

- **B1.1: OpenAI fallback in `core/ai_advisor.py`.** `AIAdvisor` now
  takes `openai_api_key` + `openai_model` and routes to OpenAI with
  strict `response_format={"type": "json_object"}` when
  `ANTHROPIC_API_KEY` is empty. Eliminates the "no ANTHROPIC_API_KEY
  set" SKIP loop. `requirements.txt` now includes `openai>=1.40.0`.
  swarm/.env gained `OPENAI_API_KEY` + `OPENAI_MODEL=gpt-4o-mini`.
- **B1.2: market_maker fixes.** `refresh_interval_seconds: 30 → 5`,
  added `quote_slippage_cents: 0.5`, and a pre-placement slippage
  guard in `market_maker_agent.py`: refetch the book just before
  submitting and drop the order if the live ref-price drifted past
  `quote_slippage_cents`. Should kill the 226/228 rejection rate.
- **B2-mini: exclude `swarm_arbitrage`.** It's a 130-line stub that
  only logs candidates; gives it `entry_strategy = False` in
  `capital_allocator._score_agent` so exploration_floor doesn't waste
  $1.50 on a no-op. Building a real arbitrage trader is gated on D3.
- **Swarm `$20 floor → $1`.** After B2-mini moved $1.50 back to the
  pool, swarm got allocated $4.50 (3 sub-agents × $1.50 exploration)
  but the swarm config validator crashed boot demanding
  `TOTAL_CAPITAL >= $20`. User chose to drop the floor and lower
  per-agent order sizes (5 → 1) rather than force dryrun. Trade-off:
  higher % fee+slippage on $1 trades, but real fills beat dryrun.
  All 4 swarm sub-agents now live.

**Final allocation as of this session end:**
```
btc_daily=$14.00 (live, +1 winning trade today: closed_take_profit
                   at 10:19 UTC, $3 → $3.087)
scalper=$1.50    (live, HTTP/2 rotation fix from earlier today; brain
                   approved 16 trades in 30m, fills pending)
swarm=$4.50      (live; 3 sub-agents × $1.50, $1 order sizes)
trader=$0        (waiting for errors=1 to roll off allocator window)
```

7 agents trading (counting 4 swarm sub-agents). arbitrage stub
excluded.

## 24-hour wait period (in progress)

The 24h `$20` experiment is running with the full feedback loop active
(resolution_sync + P&L→allocator + exploration mode + swarm pnl-event
sync). Capital split is `btc_daily=$12.50, scalper=$1.50, swarm=$6.00`,
auto-rebalanced every 5 min by `allocator_sync` daemon.

**Deliberate inaction.** No new infrastructure today. The advisor and
the user both pushed back against pre-emptively building backtest
harness, journal-analyzer, on-chain-reconciler — all over-engineering
without a specific parameter we want to tune. YAGNI.

**What to check tomorrow morning (~24h from now):**
1. `tail data/logs/allocator_sync.log` — which agents earned/lost,
   which got auto-defunded.
2. `docker exec poly1-position-manager python /app/scripts/python/capital_allocator.py --hours 24`
   — full per-agent breakdown including `realized_pnl_usdc`.
3. Verify trader `errors` count fell to 0 after `errors=1` rolled off
   the 24-hour window (Task #36 fix is in place — see below).
4. Decide based on observed data whether any single parameter looks
   wrong empirically. **Only then** does building backtest harness
   become essential rather than premature.

---

## Latest changes (evening 2026-05-08)

### Fix: illiquid-market FAILED → SKIPPED_GATE (`agents/application/trade.py`)

Root cause of the `errors=1` that blocked the trader at `$0` allocation: when the
CLOB orderbook had no asks, `execute_market_order` raised `ValueError("no asks
available for token_id=...")`. The `except Exception` catch wrote a `FAILED` row,
which counts as an error in `_score_agent` and sets `live_allowed=False` for 24 h.

Fix: split the exception handler. A `ValueError` whose message contains
`"no asks available"` now writes `SKIPPED_GATE` (veto, 0.06-penalty) instead of
`FAILED` (error, 0.45-penalty + hard block). Any other exception still writes
`FAILED`. Test added in `tests/test_trader.py::test_illiquid_market_writes_skipped_gate_not_failed`.

**Verified end-to-end 2026-05-08 evening (this session):** code at
`agents/application/trade.py:310-318` matches expected fix. Test passed
inside the docker image:
```
docker compose run --rm --entrypoint python btc_daily \
  -m unittest tests.test_trader.TestTraderTopN.test_illiquid_market_writes_skipped_gate_not_failed
# OK
```
Trader will become `live_allowed=True` automatically when the existing
`errors=1` row drops out of the 24-hour allocator window. Task #36 closed.

**Swarm pnl_events** — confirmed *not a bug*. The swarm's only fill is on market
`0x348cd9...` ("Strait of Hormuz traffic returns to normal by end of June?") which
is still open (closes 2026-06-30). `_sync_swarm_resolutions` correctly writes
nothing. pnl_event will appear automatically when the market resolves.

---

## Deferred recommendations — review before next sprint

The following item was evaluated but deferred (not "small action" — requires
replay data infrastructure):

### D3 — Backtest harness for btc_daily
**Risk**: the 5 parameter changes in `btc_daily.py` (trigger_pct, trend_threshold,
min_candidate_price, TP%, SL%) were shipped as "unvalidated guesses" per the
advisor review. Without a replay harness, further tuning is noise.
**What to do**: build a thin replay loop in `tests/` that feeds historical
CLOB snapshots through `BtcDailyAgent._evaluate` and measures entry/exit
counts + PnL on 30 days of data.
**Deferred because**: medium complexity (~2-3h). Do before any further
btc_daily parameter changes.

---


- `docs/SESSION_2026-05-08_INFRA_FIXES.md` — full action log including the
  self-investigation that drove these decisions.
- **Two infrastructure fixes** (build before more strategy work):
  1. **resolution_sync** (`agents/application/resolution_sync.py`, ~280
     lines) — detects markets that resolved naturally, writes
     `RESOLVED_YES/NO/LOSS` rows with realized payout. Stops the
     phantom-journal class of bug. Wired into `PositionManager`,
     runs each cycle (60s).
  2. **P&L → CapitalAllocator score** — realized PnL from `closed_*`
     and `resolved_*` rows now drives agent scoring, with
     `ALLOCATOR_DEFUND_FLOOR_USDC` (default `-2.0`) hard-defunding any
     agent that bleeds past the floor. Asymmetric weighting: losses
     penalize harder than wins reward.
- **Exploration mode** added: `ALLOCATOR_EXPLORATION_USDC` env knob
  (set to `1.50` in `.env`). Every entry-strategy agent that's not
  bleeding/erroring gets a minimum allocation. Solves chicken-and-egg
  where `$0` agents could never demonstrate signals.
- **Live allocation now distributed across 3 systems** (was 1):
  - `btc_daily` $12.50 (proven via score)
  - `scalper` $1.50 (exploration)
  - `swarm` $6.00 — 4 sub-agents × $1.50 (exploration; aggregated as
    `SWARM_RESERVE_USDC=6.0` in poly1, `TOTAL_CAPITAL=6.0` +
    `BOT_MODE=live` in `~/Desktop/poly/bot/.env`)
  - allocator_sync auto-applied env changes and restarted scalper,
    btc_daily, polymarket-swarm containers.
- **Self-investigation (advisor-vetted)** revealed 6 failure patterns
  driving the rebuild: deploying live without validation, config
  drift, phantom journals, allocator scoring on signals not money,
  untested strategy assumptions, and tuning on n<10 noise. The
  advisor's seventh point — "your btc_daily fixes are also unvalidated
  guesses" — is in the doc; treat the just-shipped 5-change bundle as
  watching, not validated.
- **swarm P&L feedback loop closed (late-morning)** — `resolution_sync`
  extended to scan `swarm.db.fills` and write `pnl_events` for resolved
  markets. The swarm volume is now mounted read-write into
  position_manager. Without this, swarm sub-agents could lose all $6 of
  their allocation without `ALLOCATOR_DEFUND_FLOOR_USDC` triggering.
  Wiring already existed on the read side; this filled the write side.
  When Hormuz (swarm's only open market) resolves, a `pnl_event` will
  flow automatically into the allocator score.
- **Open follow-ups** (down to 3 — #4 was completed):
  1. Trader still at `$0` blocked by `errors=1` (task #36 in tracker)
  2. Resolution-sync coverage gap on long-stuck dust positions (125
     positions where market is still open but on-chain is dust)
  3. Backtest harness missing — every "tuning" is data-fitting until
     it exists

Latest session (evening 2026-05-07, allocator auto-sync + dust-bug fix):

- `docs/SESSION_2026-05-07_ALLOCATOR_AUTO_SYNC.md` — full session log.
- **Bug fix**: `position_manager._already_closed` was returning True
  forever after a tiny dust-close ($0.0034 timeout fill on token
  `115755`) marked the position closed in the journal even though
  ~33 shares were still on-chain. Fix: also check on-chain CTF balance;
  if > 1 share, treat as still open and retry. Verified working — the
  next cycle correctly logs "dust close detected, retrying close".
- **DB cleanup**: cleared the stale `pending_orders.id=243` row in
  `swarm.db` (dryrun residue with `order_id="dry_1"`); this lifted
  `swarm_market_maker.stale_state` from 1 → 0 in the allocator.
- **Reserves aligned with allocator's verdict** (per user instruction
  not to bypass): `BTC_DAILY_RESERVE_USDC` 6 → 20, `SCALPER_RESERVE_USDC`
  14 → 0, `EXECUTE_SCALPER` true → false. Allocator says btc_daily
  gets the entire $20; everyone else is `no_live_until_clean`.
- **New autonomous mechanism**: `scripts/python/allocator_sync.py`
  daemon now runs every 5 min, reads the `CapitalAllocator`
  recommendation, writes the new values to both poly1 and swarm
  `.env` files, and restarts affected containers. The user's directive
  was that the allocator should manage the $20 budget without
  consulting them on internal redistributions. Currently running as a
  host `nohup` process (PID logged in `data/logs/allocator_sync.log`);
  TODO is to wrap as a Docker service for reboot survival.
- **Swarm state synced**: `~/Desktop/poly/bot/.env` is now
  `BOT_MODE=dryrun`, `TOTAL_CAPITAL=0.0` per allocator. Will be lifted
  back to live by the daemon if/when allocator score improves.
- 3 scalper trades from earlier today went 0/3 (each -10% reversal at
  $0.48 entry → $0.4312 exit). ExitExecutor + Brain.evaluate_exit
  cut each in ~4 seconds. Allocator subsequently scored scalper to $0.
- $41.74 of the wallet remains untouched outside the $20 cap, per
  user's hard rule.
- **Phantom-MTM correction (end of session)**: I reported MTM ≈ $8.59
  throughout the day, derived from `journal_shares × mid`. On-chain
  verification at end-of-day showed the journal "open" positions
  (`169521`, `115755`, `110702`, `781404`) all hold dust on-chain
  (<0.03 shares each). The markets resolved during the day — most
  likely both 169521 and 115755 paid out YES → that's the source of
  the unexplained $53.89 cash inflow earlier. The journal never had
  resolution events written, so the rows still appear `filled`.
  **Real portfolio at session end: cash $55.32 + real MTM $0.01 =
  ~$55.33.** Future agents must cross-check journal MTM against
  `get_balance_allowance(asset_type=CONDITIONAL, token_id=...)` for
  any "open" position older than a day.
- **24h experiment running**: btc_daily made 5 BUY entries today @
  $0.50 each ($15 deployed across token 847709). No closes yet. All
  other agents in shadow per allocator. Daemon at PID 55204 reports
  every 5 min in `data/logs/allocator_sync.log`. User explicitly
  parked the experiment overnight and will check tomorrow.

Latest session (morning 2026-05-07, MarketBrain first slice):

- Afternoon 2026-05-07 smart-exit completion:
  - `MarketBrain.evaluate_exit()` now supports smart profit holds for crypto
    15m positions. It can hold a profitable position beyond the static TP only
    when Coinbase momentum supports the held side, drawdown from peak is small,
    and expiry is not too close.
  - Hard exits still win: stop-loss, trailing-stop-after-profit, timeout, and
    local scalper expiry protection are not overridden by smart hold logic.
  - `ScalperEngine` now routes one-leg exits through `MarketBrain` and journals
    every exit decision in `brain_decisions` with `decision_type='exit'`.
  - Added focused smart-exit tests for `market_brain` and `scalper`; verification
    passed: `python3 -m unittest tests.test_scalper_engine tests.test_market_brain tests.test_capital_allocator -v`
    ran 39 tests successfully.
  - Rebuilt `poly1:local` and restarted `poly1-scalper` plus
    `poly1-position-manager`.
  - Pre-restart live check: CLOB open orders `0`, deposit-wallet balance
    `$62.137265`, no active `LEG1_FILLED` scalper exposure. Older
    `RECONCILE_NEEDED` scalper journal rows remain for separate operator review.
  - Follow-up reconciliation cleared the 3 older `RECONCILE_NEEDED` rows:
    `sol-updown-15m-1778087700`, `xrp-updown-15m-1778087700`, and
    `sol-updown-15m-1778088600`. Gamma showed all three resolved with
    `Up=0`, `Down=1`; the wallet held Up tokens, so they were recorded as
    `scalper_reconciled_lost` and the local pair states were moved to
    `expired`. Backup before mutation:
    `data/trade_log.before_reconcile_2026-05-07.db`.
  - Post-reconcile scalper states: `tracking=14`, `expired=530`,
    `exited=2`, `reconcile_needed=0`.
  - Post-reconcile allocator changed materially: it no longer blocks on stale
    scalper reconciliation and currently recommends `btc_daily` as the only
    clean candidate for the next `$20` live allocation. This remains advisory;
    `btc_daily` is still shadow unless `EXECUTE_BTC_DAILY=true` is explicitly
    enabled.
  - User approved using the `$20` live experiment budget for approved agents
    over the next 24h. Active allocation now:
    - `scalper`: live, `$14` reserve, `$2.50` legs.
    - `btc_daily`: live, `$6` reserve, `$3.00` position size.
    - `position_manager`: live exit-only.
    - `swarm`: reserve set to `$0` while it is not live-clean.
    - `trader`: still shadow (`EXECUTE=false`).
  - `position_manager` was extended to manage real `btc_daily_open` rows as
    open positions, while ignoring old shadow `btc_daily_open` rows. This keeps
    `btc_daily` from entering without a live exit path.
  - Verification: `tests.test_position_manager`, `tests.test_btc_daily`, and
    `tests.test_capital_allocator` passed; then focused
    `tests.test_position_manager` passed after the shadow-row fix.
  - Runtime check after restart: `btc_daily` starts with `execute=True`,
    `RiskGate.reason()` is `None`, total reserves are exactly `$20`
    (`scalper=14`, `btc_daily=6`, `swarm=0`), wallet balance is `$61.736726`,
    and CLOB open orders are `0`.

- `docs/MARKET_BRAIN_STRATEGY_2026-05-07.md` — shared brain/veto design
  and operating rule for all agents.
- Added `agents/application/market_brain.py`: deterministic
  classify/approve/veto layer. It does not place orders.
- Wired `ScalperDaemon` to construct `MarketBrain`; `ScalperEngine.tick()`
  now asks the brain before first-leg and second-leg entries.
- Added `brain_decisions` table/API in `TradeLog`. Scalper approvals/vetoes
  are now journaled with reason, score, market_type, asset, and features.
- Added `MarketBrain.evaluate_exit()` for decision-only TP/trailing/SL/timeout
  and `CryptoSignalFeed` for Coinbase-backed BTC/ETH/SOL/XRP evidence.
- Added `agents/application/exit_executor.py`: FAK SELL executor. The
  position manager now records closed rows only when a sell response is
  `matched`/`filled`; `live`/`delayed`/rejected/exception responses are
  `close_failed` and do not mark the position closed.
- `PositionManager` now uses `MarketBrain.evaluate_exit()` and journals exit
  brain decisions before attempting a close.
- Added `.env.example` knobs:
  `MARKET_BRAIN_ENABLED`, `MARKET_BRAIN_STRICT_UNKNOWN`,
  `MARKET_BRAIN_SCALPER_MIN_SECONDS_TO_EXPIRY`,
  `MARKET_BRAIN_SCALPER_MAX_ENTRY_PRICE`,
  `MARKET_BRAIN_SCALPER_MAX_PAIR_ASK_SUM`,
  `MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE`, `MARKET_BRAIN_EXIT_*`,
  `MARKET_BRAIN_CRYPTO_MIN_SAMPLES`.
- Verification: focused `unittest` suite passed
  (`tests.test_brain_journal`, `tests.test_market_brain`,
  `tests.test_scalper`, `tests.test_scalper_engine`; 49 tests). Full
  discovery still requires missing local deps such as
  `requests`, `dotenv`, `langchain_openai`, and `pytest`.
- Verification after FAK exit executor: focused `unittest` suite passed
  (`tests.test_exit_executor`, `tests.test_market_brain`,
  `tests.test_brain_journal`, `tests.test_position_manager`,
  `tests.test_scalper`, `tests.test_scalper_engine`; 66 tests).
- Live restart rule: entry agents remain stopped until a shadow
  `position_manager` cycle and wallet/open-order preflight pass.
- `.env` safety posture after this patch: entry execution is off
  (`EXECUTE=false`, `EXECUTE_SCALPER=false`, `EXECUTE_BTC_DAILY=false`);
  exit manager remains enabled (`EXECUTE_MAINTAIN=true`) with TP 5%,
  trailing 2%, SL 7%, 15s poll.

Latest session (morning 2026-05-07, exit-logic activation + first SL closes):

- `docs/SESSION_2026-05-07_EXIT_LOGIC_ACTIVATION.md` — full action log.
- Built `agents/application/position_manager.py` (~400 lines) +
  `tests/test_position_manager.py` (13 tests). 10% symmetric TP/SL,
  720h max hold, 60s poll. Activated as `position_manager` service
  under `profiles: ["positions"]`.
- Two bugs hit + fixed during activation:
  1. `setup_deposit_wallet.py` was missing `NEG_RISK_ADAPTER` from
     `CTF.setApprovalForAll` loop — sport/political markets rejected
     SELL with "allowance is not enough -> spender 0xd91E80cF...".
     Fixed + re-ran (tx
     0xdebf62acc8ee220f310b3e58f4ea0ea4f1947179d3f131c15f4660cfaee8474a).
  2. `_on_chain_shares()` used a plain dict instead of
     `BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, ...)` —
     SDK silently returned None, clamp didn't fire, sells rejected
     for 21.49 vs 21.09 share mismatch. Fixed + log severity raised
     from DEBUG to WARNING.
- **Two SL closes executed on-chain:** Arsenal NO @ $0.20 (+$4.32),
  Man City YES @ $0.20 (+$4.23). Cash $0.72 → $9.03. Portfolio
  $19.15 → $17.62 (slippage of ~$1.50 vs MTM, expected for fast SL).
- 2 positions remain open inside the 10% threshold (`169521...` and
  `115755...`).
- Profit-window forensic scan across all 4 bots: 4 of 10 real
  positions had ≥5% profit windows missed (Man City +17%, scalper
  XRP/SOL/ETH UP-legs at 17:15). Most other "trades" were shadow /
  failed-orders / dryrun and didn't represent real money at risk.

Latest dashboard/swarm handoff:

- `docs/AGENT_HANDOFF_2026-05-05_DASHBOARD_SWARM.md`

Latest research/review handoff:

- `docs/POLYAGENT_REVIEW_2026-05-06.md` — PolyAgent read-only review;
  added `news_signals` table + dry-run news classification module
  (no live wiring to executor or risk gate).

Latest fixes (evening 2026-05-06, Tier 1 — exit strategy activation):

- **Tier 1.1** — `MeanReversionConfig.take_profit_cents`: 2.0 → **5.0**.
  Matches user's design intent of "exit at >10% profit" (5¢ on 50¢
  entry = 10%). Asymmetric R/R 5:3 (stop_loss stays at 3¢).
- **Tier 1.2** — `MeanReversionConfig.btc_move_pct_trigger`: 0.4% →
  **0.3%**. More entries — needed because BTC was calm at 0.4%.
- swarm rebuilt + restarted. 179 tests pass.
- swarm boot log confirms config loaded: `mean_reversion` agent
  registered, BTC daily slug resolved.
- **Tier 2 deferred:** building `maintain_positions` for poly1 main
  is the foundational fix. 4-6 hours, separate session.

Latest fixes (evening 2026-05-06, after advisor review):

- **Fix A** — scalper daemon now skips RECONCILE_NEEDED/EXPIRED pairs
  before fetching orderbook. Drops 6250+ errors/30min → 0.
  `agents/application/scalper.py:545-552`.
- **Fix B** — poly1 main blocks reopening on a market it already holds
  a FILLED position on, regardless of dedupe age. Prevents the
  averaging-down behavior observed today (566187/566188 from 0.38 →
  0.205). New helper `TradeLog.has_filled_position_for_market()`,
  called from `Trader._evaluate_market`.
- **Fix C** — btc_daily trigger lowered 0.4% → 0.2% to generate
  observations.
- Tests: 103/103 passing. All 3 containers (poly1, scalper, btc_daily)
  recreated cleanly, env reload confirmed.

Latest activation push (evening 2026-05-06):

- `docs/ACTIVATION_LOG_2026-05-06_EVENING.md` — scalper bug fixes
  (limit=200 + V2_INSTALLED sentinel), threshold loosening, scalper
  flipped Stage 1 LIVE, new `btc_daily` agent built (470 lines + 12
  tests) running in shadow. 103 poly1 tests passing. Sections 11-15
  cover post-activation: 30-min monitoring cron, T+0 report, advisor
  consultation, on-chain RECONCILE_NEEDED verification, full session
  arc summary. **Wallet near-empty $3.31** — capital recycling pending
  via 17:30Z scalper resolutions.

Latest unified-wallet migration:

- `docs/AGENT_HANDOFF_2026-05-06.md` — handoff for next agent: state,
  invariants, verification commands, outstanding work. **Read first.**
- `docs/MIGRATION_LOG_2026-05-06.md` — full execution log of swarm → V2
  + signature_type=3 + shared deposit wallet migration. swarm now
  live trading on the same wallet as poly1. Follow-up review on
  2026-05-06 corrected the live swarm budget to `$20`: four funded
  agents at `$5` each; arbitrage observational at `$0`.

Latest A/B review completion:

- `docs/AGENT_HANDOFF_2026-05-06.md` and
  `docs/MIGRATION_LOG_2026-05-06.md` now include the follow-up review
  completion section: SELL MTM fix, reserve setter, V2 order response
  hardening, arbitrage 404 fix, market-maker duplicate-order guard, and
  the final live swarm restart.

Latest dashboard completion:

- Streamlit dashboard (`http://localhost:8050`) now has a **Swarm** tab
  reading `~/Desktop/poly/bot/data/swarm.db` through the Docker mount
  `/swarm/data/swarm.db`. It shows pending/submitted/filled/failed order
  rows, live CLOB submitted rows needing reconciliation, local swarm fills,
  and NothingHappens journal rows.
- Grafana (`http://localhost:3000`) capital ledger now reflects the
  corrected allocation: poly1 `$40`, swarm `$20`, scalper `$0`, total
  `$60`. A new "Swarm — Submitted Orders Needing Reconciliation" panel
  is wired to the swarm DB. It is currently empty after reconciliation.
- `monitor_web.py` / `/data.json` now exposes
  `submitted_unreconciled_count` and renders a warning table for those
  rows. Old `dry_*` rows are filtered out of that reconciliation count.
  Current value: `0`.
- Swarm tab now includes an "Agent money summary" table per agent with:
  allocation (configured via `SWARM_AGENT_ALLOCATIONS_JSON`), executed
  notional (from `fills`), remaining budget, utilization %, and
  submitted/filled/failed/cleared ledger counts.

Latest swarm reconciliation completion:

- Added `~/Desktop/poly/bot/scripts/reconcile_orders.py`.
- Swarm-side handoff for other agents:
  `~/Desktop/poly/bot/docs/ORDER_RECONCILIATION_2026-05-06.md`.
- Reconciled the verified live market-maker rows:
  - two CLOB `MATCHED` orders were recorded in local `fills` and moved
    to `pending_orders.status='filled'`;
  - one CLOB `CANCELED` order was moved to `cleared`;
  - nine stale `dry_*` submitted rows were cleared.
- `filled` is now a restart-safety brake in swarm `StateStore`, so a
  restarted agent does not layer a duplicate position on the same market
  before exit/settlement tracking releases it.
- Current swarm DB: `submitted_unreconciled_count=0`, `fills=2`,
  `pending_by_status={cleared:10, failed:229, filled:2}`.

## Trading review — 2026-05-06

**poly1 (LLM Trader)**

- 3 fills today, 8 fills all-time, $19.49 total deployed
- 05:19 UTC — pair trade EPL: SELL Arsenal YES @ 79.5¢ ($3.51) + BUY Man City YES @ 20.5¢ ($3.33)
- 07:43 UTC — BUY Barcelona YES (La Liga) @ 99.7¢ ($3.16), near-certainty position
- Markets: 566187 (Arsenal EPL), 566188 (Man City EPL), 566228 (Barcelona La Liga), 653788 (OpenAI AGI 2027)
- All positions open; no P&L realized yet (sports markets settle end of season)
- 14 `skipped_dedupe` rows — same three markets blocked correctly between cycles
- LLM usage today: 6 calls, 3,988 tokens, $0.0122 (`est_cost_usd` field in jsonl)
- LLM all-time: 78 calls, 51,232 tokens, $0.1563

**swarm (market_maker)**

- No new fills today; fills=2 remain from yesterday's reconciliation
- Market: `0x348cd9...` — "Strait of Hormuz traffic returns to normal by end of June?"
- market_maker BUY YES @ 59.5¢ ×2 lots, ~$5 each (total ~$10 notional)
- `submitted_unreconciled_count=0`, `pending_by_status={cleared:10, failed:229, filled:2}`
- No pnl_events yet (positions open)

**LLM cost tracking confirmed correct** — `llm_usage.jsonl` uses `est_cost_usd` (not `cost_usd`);
dashboard reads the correct field name. No bug.

Tomorrow's runbook:

- `docs/RUNBOOK_2026-05-07.md` — morning health check, swarm live
  activation steps (operator-only), scalper Stage 1 criteria, halt
  rules. Written 2026-05-06 after MTM-aware risk gate deployed.

Known gaps:

- `docs/POLY1_EXIT_LOGIC_GAP.md` — poly1 main trader has no exit
  logic; positions held until market resolution. Dashboard does not
  show per-position P/L. User-flagged for future work, not being
  built today.
- Swarm runtime risk summary still reports `Open positions: 0`; the local
  DB/dashboard are the reliable source for reconciled swarm fills until
  risk-state recovery from SQLite is implemented.

## Summary

poly1 and the sister swarm bot now share the same Polymarket CLOB v2
deposit wallet. poly1 uses journal-based drawdown accounting so swarm
spend does not count as poly1 loss.

The swarm bot at `~/Desktop/poly/bot` is live under Docker Compose with
`TOTAL_CAPITAL=20`: market_maker, mean_reversion, nothing_happens, and
ai_decision each have `$5`; arbitrage is registered but observational
with `$0`. During the follow-up review, market_maker placed real CLOB
orders: two matched and one canceled. There are no open CLOB orders, and
the two matched market-maker rows are now local `filled` rows and remain
blocking to prevent duplicate MM quotes until exit/settlement tracking is
implemented.

Full handoff for future agents:

- `docs/AGENT_HANDOFF_2026-05-04.md`

Release checkpoint:

- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`

Overnight and multi-week operating plan:

- `docs/OVERNIGHT_AND_MULTI_WEEK_OPERATION.md`

Trading log and journal:

- `docs/TRADING_LOG_AND_JOURNAL.md`

## Verified

- Docker image builds.
- Tests pass: `25 passed`.
- CLOB L2 auth works.
- Legacy proxy was deployed/funded:
  `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882`.
- Deposit wallet is deployed, funded, and approved:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`.
- Bot env uses:
  - `POLYMARKET_DEPOSIT_WALLET=0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
  - `POLYMARKET_SIGNATURE_TYPE=3`
- Current deposit-wallet balance: `70.185044`.
- Authenticated open orders: `[]`.
- Smoke order succeeded:
  - Status: `matched`
  - Order: `0x3c97624b9fa44cc77fb7661c038af530ab62f33e65d5688394ec3998df00127e`
  - Tx: `0x0bad489f3ad313b0ca811478f03e45028a484d27bfa8fd8b8373df87ac695651`
- Bot order succeeded:
  - Market: `566188`
  - Status: `matched`
  - Order: `0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495`
  - Tx: `0xe5b321d2d81b7b06495b67d950c8a17136c16097a5ef787a1c2d6b72f99139df`
  - CLOB order status: `MATCHED`
  - CLOB maker address: `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Bot order succeeded:
  - Market: `566228`
  - Status: `matched`
  - Order: `0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253`
  - Tx: `0x9a491a4a9015bc475fd306fd19d3c997dc529f66a3d03246cee3d51e0aa5ef1e`
  - CLOB order status: `MATCHED`
  - CLOB maker address: `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Bot order succeeded:
  - Market: `566187`
  - Status: `matched`
  - Order: `0xd8d8c88f0a9fcaee07af6a4eb6c418fb4101a79c75769633e3e01f4752f9a98d`
  - Tx: `0xb2e255a8689e0ebba2a29a24ef5ba5f4e3b34e283272d9a87dd3d071c505d039`
- Bot order succeeded:
  - Market: `653788`
  - Status: `matched`
  - Order: `0xf810656bc6c0292541c35018bf34bc584ac32b88da22887dc46c0d2be6aae816`
  - Tx: `0xc71b4e09ab20ac72ca89cccb932aa1d85723ec7d0315e36453157e32852cd2bb`
- Bot order succeeded:
  - Market: `653788`
  - Status: `matched`
  - Order: `0x54fdf93cb074a73dc34e3edc5e8a289619133d850ca769f2f409359b94db2315`
  - Tx: `0xaa0cf16d7787ef93dd3095dc0de04657dba30504e32820b99af6fcb728c0b161`
- Daemon container `poly1` is healthy.
- Sister swarm container `polymarket-swarm` is healthy in dry-run mode:
  - path: `~/Desktop/poly/bot`
  - image rebuilt: `polymarket-swarm:latest`
  - `TOTAL_CAPITAL=100`
  - Docker restart policy: `unless-stopped`
  - no live swarm trading enabled

## Current Daemon Config

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.0625"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
EXECUTE_SCALPER="false"
SCALP_LEG_USDC="5.0"
```

This targets about `$5` per `poly1` trade and `$5` per scalper leg. The main
trader remains live-capable but is blocked by the risk gate. The scalper remains
shadow-only. The sister swarm remains dry-run only and was observed simulating
about `$5` per position (`4` open simulated positions, `$20.00` at risk).

The first daemon cycle after migration placed no filled bot trade. Two FOK
orders were killed by CLOB because they could not be fully filled immediately
at the recommended price. This was fixed by making market buys orderbook-aware:
the bot now prices FOK orders from the live ask book, caps slippage, and reduces
size when available liquidity is thin.

The daemon is currently sleeping after the latest cycle. The risk gate is
blocking new entries because the cash-balance drawdown calculation is above
`MAX_DAILY_LOSS_PCT=10%`. This calculation compares current cash balance against
`STARTING_BALANCE_USDC=80.0` and does not mark positions to market, so deployed
capital can appear as drawdown. Keep the pause in place until positions and
mark-to-market value are reviewed.

## Useful Commands

## Live Checkpoint - 2026-05-07 11:05 UTC

Current live posture after guarded restart:

- `dashboard` is running and healthy on port `8050`.
- `position_manager` is running live with exit-only authority.
- `scalper` is running live with strict `MarketBrain` gating.
- Main `trader` remains disabled: `EXECUTE="false"`.
- `btc_daily` remains disabled: `EXECUTE_BTC_DAILY="false"`.
- Current CLOB balance check: `62.922603` USDC.
- Current CLOB open orders: `0`.
- No new live scalper entry was created during the first monitored window after
  strict gating was enabled.
- The only live actions observed in this checkpoint were stale/dust position
  cleanup attempts by `position_manager`.

Risk limits currently intended for the live scalper:

```env
EXECUTE_SCALPER="true"
SCALPER_RESERVE_USDC="20.0"
SCALP_LEG_USDC="2.5"
MAX_SCALP_TRADES_PER_HOUR="4"
MARKET_BRAIN_ENABLED="true"
MARKET_BRAIN_STRICT_UNKNOWN="true"
SCALP_EXIT_TAKE_PROFIT_PCT="0.05"
SCALP_EXIT_TRAILING_STOP_PCT="0.02"
SCALP_EXIT_STOP_LOSS_PCT="0.07"
```

Operational interpretation: the system is live but conservative. It is allowed
to trade only through the scalper/exit path, with small entries and no resting
orders observed. If the scalper opens a position, `position_manager` and scalper
exit logic must remain online until that position is closed or settled.

## 24h All-Agent Experiment - Started 2026-05-07 11:12 UTC

The user requested comparable results from all agents within 24 hours. The run
is active, but only the already-approved scalper path is risking live capital.

Current roles:

- `scalper`: live small, strict `MarketBrain`, `$20` reserve, `$2.50` leg.
- `position_manager`: live exit-only.
- `trader`: shadow, `EXECUTE=false`.
- `btc_daily`: shadow, `EXECUTE_BTC_DAILY=false`.
- `swarm`: dryrun, launched with explicit `BOT_MODE=dryrun`.

Shadow-only agents now use:

```env
SHADOW_IGNORE_RISK_GATE="true"
```

This lets dry-run agents keep producing paper decisions even when the live risk
gate blocks entries because of existing portfolio drawdown. It does not bypass
the risk gate for live execution.

First experiment checkpoint:

- CLOB balance: `62.922603` USDC.
- CLOB open orders: `0`.
- `trader` completed one shadow cycle: 297 tradeable events, 11 mapped markets,
  4 filtered markets, 3 top markets skipped due existing filled-position
  dedupe.
- `btc_daily` is healthy in shadow and waiting for a BTC trigger.
- `swarm market_maker` created one dryrun submitted order after stale local
  submitted state was reconciled/cleared.
- `swarm ai_decision` is running but currently skips because no Anthropic key is
  configured in the swarm environment.

Runbook: `docs/EXPERIMENT_2026-05-07_24H_ALL_AGENTS.md`.

Scoreboard:

```bash
python3 scripts/python/strategy_report.py --hours 24 --limit 50
```

Capital allocator:

```bash
python3 scripts/python/capital_allocator.py --hours 24 --budget 20
```

The allocator is read-only/advisory. It currently recommends no additional live
allocation because:

- `scalper` has `reconcile_needed` stale state;
- `swarm_market_maker` has a dryrun submitted row;
- `trader` produced veto/dedupe-only decisions;
- `position_manager` is exit-only and has recent close errors;
- `btc_daily` and other swarm agents have not produced enough signal yet.

Market intelligence is now connected to the allocator. Each run includes:

- Coinbase spot context for BTC/ETH/SOL/XRP;
- Gamma crypto-market liquidity and 24h volume;
- fresh `brain_decisions` approval count;
- fresh `news_signals` count.

Latest observed allocator context: BTC/ETH/SOL/XRP feeds were fresh, Gamma
returned `5` crypto markets with about `$116k` average liquidity and about
`$14k` average 24h volume. This boosted crypto-agent scores, but did not
override stale/reconcile blockers.

Periodic snapshots are written by `poly1-strategy-reporter` to:

- `data/strategy_report_24h.md`
- `data/capital_allocator_24h.md`

## Research Committee Brain - 2026-05-11

TradingAgents-inspired research structure was added as a read-only sidecar, not
as a live executor.

- `agents/application/research_committee.py` produces bull, bear, risk, and
  portfolio-manager assessments.
- `scripts/python/scout.py` now writes advisory rows to
  `data/scout.db:research_reports`.
- `agents/application/trade_log.py` now includes `decision_reflections` for
  lessons learned from decisions and outcomes.
- `approved_for_live` is hard-blocked to `0`; reports can only recommend
  research, paper trading, or backtest work.

Latest real scout committee row:

```text
bitcoin-up-or-down-on-may-11-2026 | mean_reversion |
reject_live_backtest_required | final_score=0.086 | risk_score=0.64 |
approved_for_live=0
```

Full handoff: `docs/SESSION_2026-05-11_RESEARCH_COMMITTEE.md`.

Tail logs:

```bash
docker logs --tail 120 -f poly1
```

Check status:

```bash
docker compose ps
```

Check balance/open orders:

```bash
docker compose run --rm trader python -c "import json; from agents.polymarket.polymarket import Polymarket; p=Polymarket(live=True); print(json.dumps({'balance': p.get_usdc_balance(), 'open_orders': p.client.get_open_orders()}, default=str, indent=2))"
```

Stop live daemon:

```bash
docker compose stop trader
```

Disable live mode:

```env
EXECUTE="false"
```

## Sister Swarm Commands

The swarm is separate from `poly1`; it has its own code, DB, wallet config, and
Docker Compose project under `~/Desktop/poly/bot`.

Check swarm status:

```bash
cd ~/Desktop/poly/bot
/Applications/Docker.app/Contents/Resources/bin/docker compose ps
```

Tail swarm logs:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 120 polymarket-swarm
```

Restart swarm dry-run service:

```bash
cd ~/Desktop/poly/bot
BOT_MODE=dryrun LOG_LEVEL=INFO /Applications/Docker.app/Contents/Resources/bin/docker compose up -d swarm
```

## Runtime Control Stabilization - 2026-05-13

Status: **freeze active; no live entry agents running**.

Why: a stale container kept old live env after `.env` changed and opened one
BTC daily trade on 2026-05-13. The system now uses a generated runtime control
layer instead of manual env edits.

Current control files:

- `deploy/.env.runtime` - generated Docker override env, no secrets.
- `data/runtime_control.json` - shared file read by `RiskGate` before entries.
- `data/HALT` - physical brake; must exist during freeze.

Current runtime hash:

```text
18e59d789c9ae259
```

Current running containers:

- `poly1-position-manager` - healthy, exit-only.
- `poly1-trading-supervisor` - healthy, `enforce_halt=true`, `open_positions=0`.
- `poly1-settlement-reconciler` - healthy, checked 0 active positions.
- dashboards/read-only services may run.

Entry containers must stay stopped during freeze:

- `poly1`
- `poly1-btc-daily`
- `poly1-scalper`
- `poly1-news-shock`
- `poly1-near-resolution`
- `poly1-wallet-follow`
- `polymarket-swarm`

Use these commands only:

```bash
.venv/bin/python scripts/runtime_control.py freeze \
  --note "stability freeze before live probe"

.venv/bin/python scripts/trading_stability_preflight.py --mode freeze
```

For a live probe, generate exactly one approved agent and budget first. Do not
arm it until the operator approves:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe"
```

After approval only:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe" \
  --arm

/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile positions --profile btc_daily up -d --force-recreate \
  position_manager trading-supervisor settlement-reconciler btc_daily

.venv/bin/python scripts/trading_stability_preflight.py --mode live
```

Verification completed in this session:

- Docker Compose config renders successfully with `.env` plus
  `deploy/.env.runtime`.
- Docker image `poly1:local` rebuilt successfully.
- Safety containers recreated from the new image.
- Container env shows `RUNTIME_MODE=freeze`,
  `RUNTIME_CONFIG_HASH=18e59d789c9ae259`, all entry execute flags false, all
  entry reserves zero, and `EXECUTE_MAINTAIN=true`.
- `trading_stability_preflight.py --mode freeze` passes.
- `trading_stability_preflight.py --mode live` is blocked as expected while
  freeze/HALT are active.
- `tests.test_trader.TestRiskGate` passes, including runtime-control freeze
  and stale-hash blocking tests.

Agent handoff:

- `docs/AGENT_HANDOFF_2026-05-13_RUNTIME_CONTROL.md`

## First Live Probe After Runtime Stabilization - 2026-05-13

Status: **completed and paused back to freeze for review**.

Activated by operator request after the runtime-control stabilization work.

Scope:

- approved live agent: `btc_daily`
- budget: `$5.00`
- runtime mode: `live_probe`
- runtime hash: `848149a2cf114b38`
- `data/HALT`: absent by design while probe is armed
- all other entry agents remain disabled:
  - `EXECUTE_SCALPER=false`
  - `EXECUTE_NEAR_RESOLUTION=false`
  - `EXECUTE_NEWS_SHOCK=false`
  - `EXECUTE_WALLET_FOLLOW=false`

Running services:

- `poly1-btc-daily` - live probe entry agent
- `poly1-position-manager` - live exit manager
- `poly1-trading-supervisor` - halt-enforcing safety daemon
- `poly1-settlement-reconciler` - reconciliation safety daemon

Activation commands used:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "operator approved first live probe after runtime stabilization" \
  --arm

.venv/bin/python scripts/trading_stability_preflight.py --mode live

/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile positions --profile btc_daily up -d --force-recreate \
  position_manager trading-supervisor settlement-reconciler btc_daily
```

Verification immediately after activation:

- `trading_stability_preflight.py --mode live`: ok
- `poly1-btc-daily`: healthy
- `poly1-position-manager`: healthy
- `poly1-trading-supervisor`: healthy
- `poly1-settlement-reconciler`: healthy
- `trading_supervisor_status.json`: `status=ok`, `open_positions=0`,
  `enforce_halt=true`
- `poly1-btc-daily` env confirms:
  - `RUNTIME_AGENT=btc_daily`
  - `RUNTIME_MODE=live_probe`
  - `RUNTIME_CONFIG_HASH=848149a2cf114b38`
  - `EXECUTE=true`
  - `EXECUTE_BTC_DAILY=true`
  - `BTC_DAILY_RESERVE_USDC=5.0`

Initial trade result:

- `btc_daily` opened one live probe position:
  - entry row: `2388`
  - time: `2026-05-13T13:36:13.670579+00:00`
  - market: `2231495`
  - side: `BUY`
  - recommended/logged price: `0.50`
  - CLOB order price: `0.34`
  - estimated average fill: `0.33`
  - size: `$3.00`
- `position_manager` closed the position:
  - close row: `2389`
  - time: `2026-05-13T13:36:30.613499+00:00`
  - reason: `closed_stop_loss`
  - close price logged: `0.3038`
  - actual proceeds: `$1.80`
  - strategy PnL logged: about `-$1.18`
- `trading_supervisor` remained healthy:
  - `status=ok`
  - `open_positions=0`
  - `enforce_halt=true`

Important finding:

- The entry row stores `price=0.50`, while the execution response reports
  `order_avg_price_estimate=0.33` and `order_price=0.34`.
- The position-manager stop-loss decision used `entry_price=0.50` and
  `current_price=0.31`, producing `pnl_pct=-0.38`.
- This means the exit calculation likely used the recommendation anchor
  instead of the actual fill price for this `btc_daily_open` row.
- Before another live probe, fix/verify entry-price accounting so exit logic
  compares current market price to actual fill price, not the strategy anchor.

Accounting fix applied:

- `btc_daily` now marks successful live entries with:
  - `price=order_avg_price_estimate`
  - `size_usdc=amount_usdc`
  - response metadata `actual_entry_price`
  - response metadata `price_accounting=actual_token_fill_price`
- `position_manager` now prefers `actual_entry_price` /
  `order_avg_price_estimate` from `response_json` when aggregating open
  positions. It only falls back to legacy `BUY price` / `SELL 1-price`
  semantics when no actual fill price is available.
- Regression tests cover the probe case:
  - strategy anchor `0.50`
  - actual fill `0.33`
  - current midpoint `0.31`
  - expected result: no false stop-loss at `-38%`; entry is treated as `0.33`.

Rollback completed:

- `runtime_control.py freeze --note "pause after first btc_daily live probe for trade review"`
- `data/HALT` exists again.
- `poly1-btc-daily` is stopped.
- `trading_stability_preflight.py --mode freeze` passes.

Rollback to freeze:

```bash
.venv/bin/python scripts/runtime_control.py freeze \
  --note "stop first live probe"

/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile positions --profile btc_daily up -d --force-recreate \
  position_manager trading-supervisor settlement-reconciler btc_daily

/Applications/Docker.app/Contents/Resources/bin/docker compose stop btc_daily

.venv/bin/python scripts/trading_stability_preflight.py --mode freeze
```

## Second BTC Daily Live Probe - 2026-05-13

Status: **active live probe**.

Purpose: verify the fill-price accounting fix from commit `e51f20a` under a
small live probe.

Scope:

- approved live agent: `btc_daily`
- budget: `$5.00`
- trade size: `$3.00`
- runtime mode: `live_probe`
- runtime hash: `848149a2cf114b38`
- `data/HALT`: absent by design while probe is armed
- all other entry agents remain disabled

Activation commands used:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "second btc_daily live probe after fill-price accounting fix" \
  --arm

.venv/bin/python scripts/trading_stability_preflight.py --mode live

/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile positions --profile btc_daily up -d --force-recreate \
  position_manager trading-supervisor settlement-reconciler btc_daily
```

Immediate verification:

- `trading_stability_preflight.py --mode live`: ok
- `poly1-btc-daily`: healthy
- `poly1-position-manager`: healthy
- `poly1-trading-supervisor`: healthy
- `poly1-settlement-reconciler`: healthy
- `trading_supervisor_status.json`: `status=ok`, `open_positions=0`,
  `enforce_halt=true`
- no new trade row during the first short observation window;
- latest trade id remains `2389`.

Expected behavior:

- `btc_daily` should wait for its BTC trigger and not force a trade.
- If a new trade opens, the entry row should store actual fill accounting:
  `price=order_avg_price_estimate` and response metadata
  `price_accounting=actual_token_fill_price`.
- `position_manager` should evaluate TP/SL against actual fill price, not the
  0.50 strategy anchor.
