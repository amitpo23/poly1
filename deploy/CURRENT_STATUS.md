# Current Status

Date: 2026-05-08

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

**Swarm pnl_events** — confirmed *not a bug*. The swarm's only fill is on market
`0x348cd9...` ("Strait of Hormuz traffic returns to normal by end of June?") which
is still open (closes 2026-06-30). `_sync_swarm_resolutions` correctly writes
nothing. pnl_event will appear automatically when the market resolves.

---

## Deferred recommendations — review before next sprint

The following items were evaluated but deferred (not blocking, or not
"small action + meaningful improvement" at this stage). Review before any
strategy tuning or next live-capital increase:

### D1 — Dockerize `allocator_sync` daemon
**Risk**: allocator_sync runs as host `nohup`, dies on reboot. Currently
`scripts/python/allocator_sync.py` is not in docker-compose.yml.
**What to do**: add a `poly1-allocator-sync` service to `docker-compose.yml` with
`restart: unless-stopped`, bind-mount poly1/.env and swarm/.env as volumes,
set `ALLOC_SYNC_ENFORCE=true`. ~15 lines.
**Deferred because**: requires testing bind-mount paths on VPS, and the daemon
is healthy today. Do before next VPS reboot or $100 live-capital event.

### D2 — Allocator anti-churn (hysteresis / cooldown)
**Risk**: allocator_sync restarts a container every 5 min whenever allocation
shifts by $0.01, causing unnecessary container thrash.
**What to do**: add `ALLOC_SYNC_MIN_DELTA_USDC` (default `0.50`). Only write
env + restart if `abs(new - old) > threshold`.
**Deferred because**: low urgency at current budget; can be tuned empirically
once we observe churn in the logs over a week.

### D3 — Backtest harness for btc_daily
**Risk**: the 5 parameter changes in `btc_daily.py` (trigger_pct, trend_threshold,
min_candidate_price, TP%, SL%) were shipped as "unvalidated guesses" per the
advisor review. Without a replay harness, further tuning is noise.
**What to do**: build a thin replay loop in `tests/` that feeds historical
CLOB snapshots through `BtcDailyAgent._evaluate` and measures entry/exit
counts + PnL on 30 days of data.
**Deferred because**: medium complexity (~2-3h). Do before any further
btc_daily parameter changes.

### D4 — Trader structured output (JSON-schema enforcement)
**Risk**: `parse_trade_recommendation` uses regex fallback (`_parse_trade_fields`)
when the LLM produces narrative prose instead of JSON. The prompt already demands
JSON but doesn't pass a response_format / tool_use schema.
**What to do**: pass `response_format={"type": "json_object"}` (GPT-4o) or use
a LangChain tool schema so the LLM is structurally constrained to emit the
`{price, size_fraction, side, confidence}` shape.
**Deferred because**: the current failure mode is `no asks available` (CLOB), not
parse failures — fixing D4 now would be solving the wrong problem. Revisit after
the trader accumulates 20+ evaluated markets.

### D5 — Dashboard realized-PnL column per agent
**What to do**: add a `realized_pnl_usdc` column to the allocator table in
`agents/application/monitor.py` (the data is already in `AgentScore.realized_pnl_usdc`).
**Deferred because**: cosmetic at this stage. Do when the user wants richer
dashboard before a capital-increase decision.

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
