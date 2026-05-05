# Strategy C — Short-Duration Crypto Up/Down Scalping (deep-dive spec)

Date: 2026-05-05
Source repos investigated:
- `PoDev-Juanthiago/Polymarket-Arbitrage-Bot` (TypeScript, CLOB v2) — cloned to
  `/tmp/poly-arb-ts`. Canonical algorithm in `src/order-builder/copytrade.ts`,
  config in `src/config/index.ts`.
- `ConteurShadow/Polymarket-Trading-Bot-Rust` — cloned to `/tmp/poly-rust-bot`.
  Canonical 15-minute strategy in `src/bin/main_dual_limit_045_same_size.rs`
  (PERIOD_DURATION = 900s); BTC-only 5-minute variant in
  `src/bin/main_dual_limit_045_5m_btc.rs`.

**Key framing up-front:** the two source repos implement *different* strategies
that share only the high-level "buy both sides cheap" idea. They are not
interchangeable. Section 1 documents both verbatim from their source so we can
make an informed pick in Section 6.

---

## 1. Exact algorithms from source

### 1.A — TypeScript hedged-arb (`copytrade.ts`)

Targets `*-updown-15m-*` slugs explicitly (see `redeem-holdings.ts:179`,
`copytrade.ts:220`). Both legs are **MARKET BUYs** with FAK semantics. No
limit orders, no SELL primitive used.

Defaults from `src/config/index.ts`:

| Param | Default | Meaning |
|-------|---------|---------|
| `threshold` (`ENTRY_THRESHOLD`) | `0.499` | Enter first leg when ask ≤ 0.499 |
| `reversalDelta` | `0.020` | Confirm reversal after price bounces by 2¢ |
| `reversalDeltaThresholdPercent` | `0.5` | 50% of `reversalDelta` used for dynamic threshold |
| `maxBuysPerSide` | `4` | Max attempts per side per slug |
| `sharesPerSide` (`SHARES_PER_ORDER`) | `5` | Outcome shares per leg (≈ $2.50 at $0.50) |
| `maxSumAvg` | `0.98` | Hard profitability gate: `avg_yes + avg_no` must end ≤ 0.98 |
| `priceBuffer` | `0.03` | Price buffer added to live ask when posting |
| `secondSideBuffer` | `0.01` | Buy second side when ask ≤ `(1 − firstBuyPrice) − 0.01` |
| `secondSideTimeThresholdMs` | `200` | OR after price has been ≤ dynamic threshold for 200 ms |
| `dynamicThresholdBoost` | `0.04` | After leg 1 fills, raise leg-2 threshold to `1 − fillPrice + 0.04` |
| `depthBuyDiscountPercent` | (varies) | Buy immediately when ask drops X% below tracking `tempPrice` |
| `useFakOrders` | `true` | FAK (allows partial fills) |
| `pollMs` (adaptive) | `minPollMs..maxPollMs` | 50–500 ms typical |

**Core decision tree (one slug, one period).** Critically, `threshold (0.499)`
is an **eligibility ceiling**, not the buy trigger. Once `currentPrice ≤
threshold`, the bot starts tracking a running low (`tempPrice`) and fires the
buy on a reversal or a depth-drop, not on the threshold cross itself.

1. **Eligibility:** ignore the slug entirely while `currentPrice > threshold (0.499)`.
2. **Track running low:** while eligible, on every poll, if
   `currentPrice < tempPrice` then `tempPrice = currentPrice` (`copytrade.ts:1066`).
3. **Leg-1 BUY fires on EITHER condition:**
   - **Reversal trigger** (`copytrade.ts:1132`):
     `currentPrice ≥ tempPrice + reversalDelta (0.020)` — i.e. price has bounced
     2¢ off the recent low.
   - **Depth trigger** (`copytrade.ts:1128`):
     `currentPrice ≤ tempPrice × (1 − depthBuyDiscountPercent)` — i.e. price has
     dropped a further X% below the running low (default ≈ 5%, env-tunable).
4. **After leg-1 fill at `firstBuyPrice`:**
   - `dynamicThreshold = 1 − firstBuyPrice + dynamicThresholdBoost (0.04)`
   - Reset `tempPrice` for the opposite token; track its running low.
   - Leg 2 BUY fires IF either:
     a. `oppositeAsk ≤ dynamicThreshold − secondSideBuffer (0.01)` (immediate),
     OR
     b. `oppositeAsk ≤ dynamicThreshold` continuously for ≥ `secondSideTimeThresholdMs (200ms)`.
5. **Profitability hard gate before EACH buy** (overrides all triggers):
   `currentAvgOtherSide + buyPrice ≤ maxSumAvg (0.98)`, computed as
   `maxAcceptablePrice = 0.98 − currentAvgOtherSide`. If violated, skip.
6. Cap: `attemptCount{YES,NO} ≤ maxBuysPerSide (4)` per slug.
7. **Exit / settlement: HOLD TO EXPIRY.** No SELL leg in the trading loop. The
   shares redeem $1 each on the winning side after oracle resolution;
   redemption is handled by separate `redeem.ts`/`redeem-holdings.ts` scripts.

### 1.B — Rust dual-limit-045 (`main_dual_limit_045_same_size.rs`)

15-minute period, BTC focus (config toggles ETH/SOL/XRP). Uses **LIMIT BUYs at
0.45** placed at period start, plus a multi-stage hedge ladder that uses **MARKET
BUYs (FAK)** if only one limit fills, plus **LIMIT SELLs** for low-price exits.

Constants from `src/bin/main_dual_limit_045_same_size.rs:21..66`:

```
LIMIT_PRICE                 = 0.45
PERIOD_DURATION             = 900s
DEFAULT_HEDGE_AFTER_MINUTES = 10        # standard hedge starts at 10:00
DEFAULT_HEDGE_PRICE         = 0.80      # standard hedge: buy unfilled at market if bid ≥ 0.80
NINETY_SEC_AFTER_SECONDS    = 120       # "2-min" ladder rung starts at 2:00
TWO_MIN_AFTER_SECONDS       = 240       # "4-min" ladder rung starts at 4:00
FOUR_MIN_HEDGE_MIN_PRICE    = 0.50
FOUR_MIN_HEDGE_MAX_PRICE    = 0.65
FOUR_MIN_HEDGE_BUY_OVER_PRICE = 0.75    # >0.75 → buy NOW at market
EARLY_HEDGE_MIN_PRICE       = 0.65
EARLY_HEDGE_MAX_PRICE       = 0.85
NINETY_SEC_HEDGE_MAX_PRICE  = 0.50      # 2-min hedge active when unfilled < 0.50
LOW_PRICE_THRESHOLD         = 0.10      # post-hedge exit trigger
SELL_LOW_PRICE              = 0.05      # limit-sell loser at 0.05
SELL_HIGH_PRICE             = 0.99      # limit-sell winner at 0.99
LIMIT_SELL_AFTER_SECONDS    = 600       # exit limits only after 10:00
MAX_HEDGE_PRICE             = 0.54      # don't hedge if unfilled ask > 0.54 (locks in loss)
config.fixed_trade_amount   = 4.5       # $4.50 per leg
config.dual_limit_shares    = 10.0
check_interval_ms           = 500
```

**Core decision tree (one slug, one period):**

0. Place TWO limit BUYs at 0.45 (Up and Down) within first 15s of period.
1. Both fill → done (avg cost 0.45+0.45 = 0.90, +10¢ profit guaranteed). Stop.
2. One fills, opposite still pending. Cancel pending opposite limit immediately.
3. Hedge ladder for the unfilled side, in priority order:
   - **2-min rung (t ≥ 120s, before 4 min, unfilled bid < 0.50):** trailing-on-ask,
     market-buy at ask. Skip if `ask > MAX_HEDGE_PRICE (0.54)`.
   - **4-min rung (t ≥ 240s):** if unfilled bid > 0.75 → market-buy now.
     Else if 0.50 < bid < 0.65 → trailing-on-bid, market-buy at ask.
   - **Early rung (0.65 < bid < 0.85):** trailing-on-bid, market-buy at ask.
   - **Standard rung (t ≥ 600s, both limits still pending, unfilled bid ≥ 0.80):**
     market-buy now.
4. Post-hedge exit (t ≥ 600s): if any side trades < 0.10, place limit-sells at
   0.05 (loser) and 0.99 (winner) — recovers a few cents instead of waiting for
   full settlement.
5. Otherwise hold to expiry; redemption is separate.

**Profitability formula in both repos:**
`avg_yes + avg_no ≤ profit_threshold` where threshold ≈ 0.98 (TS) or implicit
0.90 (Rust dual-fill). The redemption pays $1.00 to the winning side, so
expected gross profit per pair = `1.00 − (avg_yes + avg_no)` (before
redemption gas).

---

## 2. Viability TODAY (2026-05-05)

Verified live by direct `gamma-api.polymarket.com` and `clob.polymarket.com` calls.

**Are the markets live?** YES, abundantly.
- Today's tag-21 (crypto) events sorted by endDate produce 61 active `*-updown-*`
  markets in a single 30-event window.
- Variants seen live: `5m`, `15m`, `4h`. Assets: BTC, ETH, SOL, XRP, DOGE, BNB,
  HYPE — i.e. 7 underlyings × 3 cadences.
- A new 5m market is created **every 5 minutes per asset** (e.g. timestamps
  `1778051700, 1778052000, 1778052300, ...` = +300 s). 15m markets stagger
  similarly (every 15 minutes per asset).

**Sample market (`hype-updown-15m-1778052600`):**
- `acceptingOrders: true`, `enableOrderBook: true`, `negRisk: false`.
- `orderMinSize: 5` (shares; ≈ $2.55 USDC at ask 0.51 — well below our $5 leg).
- `orderPriceMinTickSize: 0.01`.
- `liquidity: ~$2,300`. Spread quoted as `0.02` (i.e. 2¢, or 4% of mid).

**Orderbook snapshot (15m HYPE token, fetched live):**
```
best_bid 0.49 size 115     best_ask 0.51 size 115
next_bid 0.48 size  20     next_ask 0.52 size  20
       0.47       160            0.53       160
       0.46       120            0.54       120
       0.45       119            0.55       119
```
The book is symmetric and surprisingly deep: $115 USDC notional sitting at
top-of-book on each side — easily absorbs $5–$50 legs without significant
slippage.

**CLOB version:** V2. Both the TS `copytrade.ts` (`@polymarket/clob-client-v2`)
and our existing `polymarket.py` target V2. Zero compatibility concern there.

**Fees:** Gamma reports `makerBaseFee=1000, takerBaseFee=1000` for every market,
including the heavily-traded "Jesus 2027" market. This number is therefore
**not basis points** (would imply 10%, which would dead-stop arbitrage volume
that we observe is happening). Cross-referencing `polymarket.py:500` which
hard-codes `feeRateBps="1"` (= 0.01%), and Polymarket's public stance that
protocol fees are zero, the realistic friction model is: **fees ≈ 0**, friction
= bid-ask spread (2¢ = ~4% per round-trip on a 0.50 mid). This needs a one-line
empirical check on the first live trade before scaling.

**Can FOK suffice?** NO for the Rust strategy (needs LIMIT + LIMIT-SELL). YES
for the TS strategy if and only if we accept that "buy at ask" via FOK gives us
the same leg fills the TS bot achieves — at the cost of paying the spread
instead of working a limit. **This is the central practical tradeoff.**

**$1–$5 per leg viable?** Yes. `MIN_MARKET_ORDER_USDC=1.0` is the existing
poly1 floor; gamma's `orderMinSize=5` shares × ~$0.50 mid ≈ $2.50 actual notional.
A $5 leg fits comfortably above both floors.

---

## 3. Implementation blueprint — `agents/application/scalper.py`

Pick **TS-style algorithm** as the implementation target. Justification:
1. It uses only MARKET BUYs (FAK), which our existing `polymarket.py` already
   has via `_fillable_market_buy` (we'd need to add `OrderType.FAK` next to the
   current `OrderType.FOK`, ~5 lines).
2. The Rust algorithm needs LIMIT-BUY, LIMIT-SELL and order cancellation — three
   primitives that don't exist today and would each be a multi-day build with
   real risk.
3. The TS algorithm's expected-edge-per-pair (~2¢ at sumAvg=0.98) is the same
   order of magnitude as the Rust strategy's worst-case path. The Rust extra
   complexity is for *recovering* from one-sided fills; the TS strategy avoids
   that branch by waiting until both sides are visibly cheap before entering.

### Module shape (~250–350 LOC, no LLM)

```
agents/application/scalper.py
  class ScalpPair:           # in-memory tracking row, mirrors TS CopytradeStateRow
    # per-side-tracking fields (the part that drives entry logic)
    temp_price_up: float | None      # running low for UP token (None = ineligible)
    temp_price_down: float | None    # running low for DOWN token
    last_update_up_ms: int           # for the 200 ms continuous-below check
    last_update_down_ms: int
    below_threshold_since_ms: int | None  # leg-2 timer
    # pair state
    qty_up, qty_down, cost_up, cost_down, attempts_up, attempts_down
    state: 'tracking'|'leg1_filled'|'both_filled'|'expired'|'redeemed'
  class ScalperConfig:       # env-loaded, mirrors TS HedgedArbConfig defaults
  class ScalperEngine:
    .__init__(client: Polymarket, log: TradeLog, gate: RiskGate, cfg)
    .discover_markets()      # gamma /events?tag_id=21, slug filter '-updown-15m-'
    .poll_book(token_id)     # CLOB book endpoint, 250 ms cadence
    .evaluate_entry(pair)    # implements (eligibility + track-low + reversal/depth) trigger
    .evaluate_second_leg(pair, fill_price)
    .place_leg(pair, token, side, shares)  # FAK; writes scalp_pending → scalp_filled
    .check_profit_gate(pair, candidate_price) → bool   # ≤ 0.98 − other_avg
    .reap_period(pair)       # at end-of-window: mark expired, queue redemption
  def run_loop(engine):      # blocking; called from a separate process
```

**`evaluate_entry` reference logic (matches Section 1.A steps 1–3):**

```
on each poll for one side of one slug:
  ask = best_ask()
  if ask > threshold:        # 0.499 — ineligible; reset tracker
      pair.temp_price_side = None
      return None
  if pair.temp_price_side is None or ask < pair.temp_price_side:
      pair.temp_price_side = ask           # update running low
      return None                           # no entry yet
  if ask >= pair.temp_price_side + reversalDelta:    # 0.020 reversal
      return EntrySignal(reason='reversal', price=ask)
  if ask <= pair.temp_price_side * (1 - depthBuyDiscountPercent):
      return EntrySignal(reason='depth', price=ask)
  return None
```

### Main loop

- Single asyncio loop. Per-slug state machine.
- Outer cadence 1 s — scan gamma for new `-updown-15m-*` markets.
- Inner cadence 250 ms (configurable, matches TS `pollMs`) — fetch CLOB book
  for currently-tracked tokens, evaluate entry/second-leg.
- Stop tracking a slug once: both legs filled, OR period ends, OR
  `attemptCount` cap hit on both sides.

### Storage decision: NEW TABLE `scalper_pairs`, not new status enum

TS state is fundamentally pair-shaped (`qtyYES, qtyNO, costYES, costNO,
attemptCountYES, attemptCountNO, lastBuyPriceYES, lastBuyPriceNO`). The
existing `trades` table is one-row-per-attempt. Trying to overlay pair state
on `trades` via a new `pair_id` column requires every reader to do a
self-join to reconstruct the pair — fragile, easy to corrupt during partial
fills.

**Schema (in same `data/trades.sqlite`, new table):**
```
CREATE TABLE scalper_pairs (
    slug TEXT PRIMARY KEY,
    period_ts INTEGER NOT NULL,
    up_token TEXT NOT NULL,
    down_token TEXT NOT NULL,
    qty_up REAL NOT NULL DEFAULT 0,
    qty_down REAL NOT NULL DEFAULT 0,
    cost_up REAL NOT NULL DEFAULT 0,
    cost_down REAL NOT NULL DEFAULT 0,
    attempts_up INTEGER NOT NULL DEFAULT 0,
    attempts_down INTEGER NOT NULL DEFAULT 0,
    last_price_up REAL, last_price_down REAL,
    state TEXT NOT NULL,    -- 'tracking'|'leg1_filled'|'both_filled'|'expired'|'redeemed'
    opened_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    error TEXT
);
```

Each FAK order attempt **also** writes a normal `trades` row with
`market_id=slug` and a new status `SCALPER_LEG` (added to enum, NOT in
`ACTIVE_STATUSES` — scalper has its own dedupe via `scalper_pairs.state`).
This preserves the auditing/PnL paths the rest of the bot relies on.

### RiskGate compatibility

- Scalper does NOT share `MAX_TRADES_PER_HOUR` with the LLM Trader by default.
  At 4 attempts × N concurrent slugs over 15 min, we'd exceed any reasonable
  hourly count. Add **`MAX_SCALP_TRADES_PER_HOUR`** as a separate gate (default 60).
- Scalper DOES share `MAX_DAILY_LOSS_USDC` and the global `HALT` file. If the
  daily loss gate trips, scalper halts entries (open pairs still settle).
- Scalper has its own pre-entry check: `available_balance ≥ leg_cost × 2 +
  reserve` to avoid stranding leg 1 if leg 2 can't fund.

### Failure handling — leg 1 fills, leg 2 fails

This is the highest-risk branch. TS strategy logic + our adaptation:

1. Leg 2 entry condition is "price ≤ dynamic threshold". If price never hits,
   we *deliberately* hold leg 1 to expiry. Worst case: leg 1 was on the losing
   side → lose entire leg ($5). Documented, accepted risk.
2. Leg 2 entry condition triggers but FAK fails (book moved). Retry up to
   `maxBuysPerSide=4` total attempts at the next favorable tick, but only while
   the profit gate `existing_avg_other + new_price ≤ 0.98` holds.
3. Leg 2 entry triggers, FAK partial-fills (FAK allows partials). Update
   `qty_down, cost_down`; re-evaluate gate; may need 2nd attempt.
4. Crash mid-leg-2 (process dies between FAK send and book write): on restart,
   `scalper_pairs.state='leg1_filled'` is the resume marker. Reconcile against
   exchange position via `client.get_positions(token_id)` BEFORE attempting any
   new order. **This is the analogue of `MAY_HAVE_FIRED` for scalper.**

### Integration with the existing daemon

Run as **separate process** (not separate thread, not separate cron). Reasons:
- Different cadence (250 ms vs. LLM trader's minutes-long cycle).
- Process isolation: a scalper bug or hot loop must not stall the LLM trader.
- Easier to halt independently (`docker compose stop scalper`).

Add a new service to `docker-compose.yml`:
```
scalper:
  build: .
  command: python -m agents.application.scalper
  env_file: .env
  volumes: [./data:/app/data]
  restart: unless-stopped
```
Both services share the SQLite file via the `./data` bind-mount. SQLite handles
two-process WAL-mode access fine (already enabled in `trade_log.py` if not, add
`PRAGMA journal_mode=WAL`).

A new `EXECUTE_SCALPER=false|true` env gates live trading just as `EXECUTE`
does for the LLM trader.

---

## 4. Capital-recycling estimate

Assumptions: `STARTING_BALANCE_USDC = 80`, `leg_size = 5` USDC, target 4 legs
per pair on average (2 entry + retries), Stage-1 caps: 2–3 concurrent open
pairs.

**Capital-lockup window per pair:** entry → window close → oracle → redeem.
Window is 15 min, oracle finality is typically < 5 min, redemption is a manual
on-chain call we already have via `redeem.ts`/`redeem-holdings.ts` analogue.
Round-trip realistic = **20–30 min**, not 15.

**Concurrency:** With $80 - reserve $10 = $70 working capital, max ≈ 7 legs
outstanding × $5 = $35 frequently locked (one full pair = $10). Assume 3
concurrent pairs steady state (≈ $30 locked + $40 buffer for retries / leg-2
funding).

**Daily expected throughput:** Markets refresh every 15 min × 24 h = 96
windows/day per asset. With 7 underlyings × 96 = 672 candidate slugs/day, but
only **a fraction will satisfy** the entry chain (eligibility `≤ 0.499` AND
either reversal or depth trigger). In practice, the 5/15m crypto Up/Down
markets sit near 0.50/0.50; eligibility opens any time crypto has a recent move
and the market overshoots. Realistic *floor* estimate: **5–15 entered pairs/day**,
each generating 2–4 fills, so **15–50 fills/day total**. At ~$30 capital
locked vs. $80 balance = **~38% of capital busy at any moment** in steady
state.

This is a floor — the reversal trigger fires more often than `ask ever drops
below 0.499` would suggest, since once eligibility opens, EVERY ≥ 2¢ bounce
within the eligible window is a buy. Real throughput could be 2–3× the floor
estimate. Stage-1 sizing should be set assuming the higher number to avoid
running out of working capital.

**Edge per pair:** at maxSumAvg=0.98 the structural max edge is 2¢ × 5 shares =
$0.10/pair gross. This is before any spread cost or oracle slippage. Net edge
is plausibly $0.02–$0.08/pair. At 10 pairs/day, gross daily edge ≈ $0.20–$0.80.
**This is small compared to the $5 capital-loss risk on a one-sided expiry.**

---

## 5. Risk register

| # | Risk | Mitigation |
|---|------|------------|
| 1 | **Stale prices.** Book moves between read and FAK send; FAK partial-fills at worse price. | Re-fetch book within ≤ 50 ms of order send; abort if `best_ask > intended_price + tickSize`. |
| 2 | **Leg-2 never triggers.** Leg 1 ends up on losing side → $5 lost. | Hard structural risk. Mitigated only by: (a) `secondSideTimeThresholdMs=200ms` for fast leg-2; (b) sizing constraint ≤ $2-5 per leg in Stage-1; (c) per-day loss cap. |
| 3 | **Oracle dispute / late resolution.** Capital locked for hours. | Cap concurrent pairs at 3 so a single bad oracle doesn't immobilize the whole bot. |
| 4 | **FOK kill on thin upper-book.** Less likely on these markets given depth, but possible during a crypto spike. | Use FAK (allows partials) instead of FOK. Means adding `OrderType.FAK` path to `polymarket.py:execute_market_order`. |
| 5 | **Race condition between scalper and LLM trader.** Both compete for USDC; double-spend → one fails after balance check. | Separate process, but pre-entry balance check uses `available - locked_in_other_module`. Cheapest: scalper holds a fixed sub-balance reserve (`SCALPER_RESERVE_USDC=20`). |
| 6 | **Capital fragmentation.** $30 spread across 6 small markets → can't fund a Trader entry with $5+ requirement. | Hard scalper-pair count cap; per-day USDC budget for scalper. |
| 7 | **CLOB rate-limit.** 250 ms book polling × N tokens = many req/s. | Subscribe to CLOB websocket if available; otherwise share book reads via cache; default `pollMs=500`. |
| 8 | **Crash mid-leg-2.** Open one-sided position. | New `state='leg1_filled'` row + restart-time reconcile against exchange positions, modeled on `MAY_HAVE_FIRED`. |
| 9 | **Settlement gas / failure on redeem.** Stake locked but no payout. | Already a known issue (existing `redeem.ts` retries); inherit. |
| 10 | **Profit gate gaming itself.** maxSumAvg=0.98 means every entry is razor-thin; one bad fill makes the pair unprofitable. | Tighten initial gate to 0.97 in Stage-1 to leave margin for slippage. |

---

## 6. Honest go / no-go

**Conditional GO — build it, but treat as Stage-2 effort, not pre-launch.**

Reasoning, separating "in source" from "we'd build fresh":

**What's in the source we can take directly:**
- The full TS decision tree (entry threshold, dynamic threshold, second-side
  trigger, profitability gate, attempt cap) — algorithmic spec is complete and
  unambiguous. ~150 LOC of decision logic plus polling/state.

**What we'd build fresh:**
- All the integration glue with `polymarket.py`, `trade_log.py`, `risk_gate.py`,
  Docker, and the existing daemon. ~150–200 LOC.
- A new `scalper_pairs` table, schema migration in `trade_log.py`.
- An `OrderType.FAK` code path in `polymarket.py` (currently FOK-only).
- Restart-time reconciliation against exchange positions.
- A separate process / supervisor entry.

**Why conditional:**
- Live markets exist, have depth, accept $5 orders. Confirmed empirically.
- Algorithm is well-defined and copyable from MIT-licensed sources (TS repo
  appears to be without license; cite as inspiration, re-implement clean).
- Expected daily edge is small ($0.20–$0.80 gross at $80 balance) and the
  one-sided-loss risk is large per pair ($5). The strategy needs **dozens of
  successful pairs** to outpace a single bad one — and given the maxSumAvg=0.98
  gate, the model is essentially "many small wins, occasional full-leg loss."

**Why NOT pre-launch (Sunday 2026-05-03, already 2 days past):**
- The CLAUDE.md invariant set is tuned for the LLM trader's monthly cadence.
  Adding a 250-ms polling, 60-trades/hour module on day-zero of live trading
  would multiply blast-radius by 50× before we have any operational data.
- Restart-time reconciliation is not optional and is genuinely hard to get
  right — the existing `MAY_HAVE_FIRED` pattern took several iterations.
- Capital is shared with the Trader; until we know the Trader's real-money
  behavior, isolating scalper capital well is guesswork.

**Recommended sequencing:**
1. Land Tier-1 fixes from the prior research (Kelly, MIN_EDGE_PCT, calibration).
2. Run Trader live for 2–4 weeks; observe actual fees and slippage on small fills.
3. Build scalper as a dry-run-only module first (`EXECUTE_SCALPER=false`,
   logs hypothetical pairs into `scalper_pairs.state='shadow'`). Compare
   predicted vs. real book moves over a week.
4. Flip scalper live with a hard $20 sub-budget, separate from Trader.

**Dead-end conditions that would flip this to no-go:**
- If empirical taker fees on the first $5 fill turn out to be >1% (i.e. the
  `feeRateBps="1"` reading is wrong) — strategy edge evaporates.
- If average book depth at top-of-book drops below ~$50 for 15m crypto
  markets — FAK partial fills become the norm and the pair-completion rate
  collapses.
- If oracle resolution times stretch past 1 hour for 15m markets — capital
  recycling math breaks.

These are all empirically checkable with one or two test runs **before**
investing in the full module.
