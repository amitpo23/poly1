# Session log — 2026-05-18: 6 Trading Fixes to Prevent Losses

## Context

Following the forensic audit (SESSION_2026-05-17_AUDIT.md), the bot resumed
trading on 2026-05-18 with external_conviction live-enabled. In one day it
executed ~16 trades and lost ~$0.77 across 12 losers, partially offset by one
+$1.97 winner. The loss pattern revealed systematic entry-quality problems:

| Root cause | Count | Mechanism |
|---|---|---|
| Timeout sell at spread cost | 9 | Bot enters, price doesn't move, timeout forces sell — loses the bid-ask spread |
| No bid-side liquidity | 4 | Entered markets where no one is bidding, couldn't exit at any price |
| Duplicate entry | 1 | Same market entered twice in one cycle |
| Penny tokens (0.01–0.09) | several | Round-trip spreads of 40–77% guarantee loss |

**Net result:** -$0.77 realized (before the +$1.97 winner).

## What was implemented

Six fixes, ordered by implementation sequence:

### Fix 6: In-memory duplicate guard per cycle

**Problem:** external_conviction's `collect_once()` iterates over candidates
and can encounter the same market_id twice (e.g. same market returned by
multiple providers or listed twice in Gamma results). The DB dedupe check
happens before the fill is committed, so the second candidate passes the
check and also gets filled.

**Solution:**
- Added `self._live_entries_this_cycle: set = set()` to
  `ExternalConvictionAgent.__init__`
- Reset at start of candidate loop in `collect_once()`
- Check/add in `_maybe_execute_live()`: if `market_id` already in set, skip;
  after successful fill, add to set

**Files:** `agents/application/external_conviction.py` (3 edits: init, loop
reset, guard + set.add)

**Risk:** Zero — pure additive guard, no behavior change for non-duplicate
cases.

---

### Fix 5: Cross-agent dedupe via token_id

**Problem:** The Trader stores market IDs as numeric strings (e.g. `"566187"`)
from Chroma metadata, while external_conviction stores hex token IDs (e.g.
`"0x7976abcdef"`) from Gamma API. When the Trader calls
`has_active_trade_for_market("566187")`, it never finds the external_conviction
row keyed by `"0x7976..."` — string mismatch means no cross-agent deduplication.

**Solution:**
- Added optional `token_id: Optional[str] = None` parameter to both
  `TradeLog.has_filled_position_for_market()` and
  `TradeLog.has_active_trade_for_market()`
- When `token_id` is provided, SQL WHERE becomes:
  `(market_id = ? OR (token_id = ? AND token_id IS NOT NULL))`
- Added index: `CREATE INDEX IF NOT EXISTS idx_token_id_status ON
  trades(token_id, status, ts)` to SCHEMA
- Updated callers:
  - `trade.py:_evaluate_market()` — extracts `token_ids[0]` from Chroma
    metadata BEFORE dedupe checks (moved up from line ~451) and passes it
  - `external_conviction.py:_maybe_execute_live()` — passes
    `plan.token_id` to both dedupe calls

**Files:**
- `agents/application/trade_log.py` — schema index + 2 method signatures
- `agents/application/trade.py` — early token_id extraction + 2 call sites
- `agents/application/external_conviction.py` — 2 call sites

**Risk:** Low — backward compatible. Without `token_id` param, behavior is
identical to before.

---

### Fix 1: Minimum entry price guard

**Problem:** Penny tokens (best ask $0.01–$0.09) have 40–77% round-trip
spreads. Even if the trade thesis is correct, the spread cost guarantees a
loss on timeout.

**Solution:** In `_fillable_market_buy()`, after sorting asks:
```python
if best_ask < MIN_ENTRY_PRICE:
    raise ValueError(f"below MIN_ENTRY_PRICE: best_ask={best_ask:.4f} < {MIN_ENTRY_PRICE}")
```

**Default:** `MIN_ENTRY_PRICE=0.10` (env-configurable)

---

### Fix 2: Bid-side depth guard

**Problem:** 4 trades entered markets with zero or near-zero bid-side
liquidity. The bot could buy but couldn't sell back — position_manager
eventually force-closed at a huge loss or the position resolved against us.

**Solution:** In `_fillable_market_buy()`, after the min-price check:
```python
bids = sorted(book bids by price descending)
total_bid_usdc = sum(price * size for each bid level)
if not bids or total_bid_usdc < MIN_BID_DEPTH_USDC:
    raise ValueError(f"insufficient bid depth: ...")
```

**Default:** `MIN_BID_DEPTH_USDC=20.0` (env-configurable)

---

### Fix 3: Spread width guard

**Problem:** Wide bid-ask spreads mean immediate unrealized loss on entry.
A 10% spread means the position starts -10% and must move 10%+ just to
break even.

**Solution:** In `_fillable_market_buy()`, after bid depth check:
```python
spread_pct = (best_ask - best_bid) / best_ask
if spread_pct > MAX_ENTRY_SPREAD_PCT:
    raise ValueError(f"spread too wide: {spread_pct:.4f} > {MAX_ENTRY_SPREAD_PCT}")
```

**Default:** `MAX_ENTRY_SPREAD_PCT=0.05` (5%, env-configurable)

---

**Fixes 1-3 shared infrastructure:**

All three checks live in `agents/polymarket/polymarket.py:_fillable_market_buy()`
and raise `ValueError` on rejection. The error messages are caught in
`agents/application/trade.py:_evaluate_market()` where the existing
`ValueError` handler was extended:

```python
# Before (only 2 patterns):
if "no asks available" in msg or "live ask price" in msg:

# After (5 patterns):
if any(s in msg for s in (
    "no asks available",
    "live ask price",
    "below MIN_ENTRY_PRICE",
    "insufficient bid depth",
    "spread too wide",
)):
```

Matched errors are written as `SKIPPED_GATE` (not `FAILED`), so:
- The broken-market counter doesn't penalize these markets unfairly
- The allocator doesn't score the agent down for market-quality rejections
- The market can be retried next cycle if conditions improve

**Constants added to module top of `polymarket.py`:**
```python
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.10"))
MIN_BID_DEPTH_USDC = float(os.getenv("MIN_BID_DEPTH_USDC", "20.0"))
MAX_ENTRY_SPREAD_PCT = float(os.getenv("MAX_ENTRY_SPREAD_PCT", "0.05"))
```

---

### Fix 4: Smart timeout grace for flat positions

**Problem:** 9 of 12 losses were timeouts. The bot enters a position, price
doesn't move significantly, and after `exit_max_hold_seconds` (30 min) the
position_manager force-sells. Selling a flat position at timeout means paying
the bid-ask spread for zero gain — guaranteed small loss every time.

**Solution:** Added a grace period: if the position's P&L is within a
configurable threshold of zero ("flat"), extend the hold time instead of
immediately selling.

In `market_brain.py:BrainConfig`:
```python
exit_timeout_flat_grace_pct: float = 0.01   # +/-1% = "flat"
exit_timeout_grace_seconds: int = 3600      # 1 extra hour
```

In `evaluate_exit()`, the timeout block now checks:
```python
if age_seconds >= exit_max_hold_seconds:
    if (abs(pnl_pct) < grace_pct
        and age_seconds < max_hold + grace_seconds):
        return BrainDecision(False, "timeout_grace_flat", ...)  # hold
    return BrainDecision(True, "timeout", ...)  # sell
```

**Key safety property:** Stop-loss (line 397) fires BEFORE the timeout
check, so a position losing more than `exit_stop_loss_pct` (7%) is never
protected by grace. The grace only applies to positions that are genuinely
flat — not losing money, just not moving.

**Files:** `agents/application/market_brain.py` — 2 new config fields,
2 new env parsers in `from_env()`, modified timeout block in `evaluate_exit()`

**Defaults:** `MARKET_BRAIN_TIMEOUT_FLAT_GRACE_PCT=0.01`,
`MARKET_BRAIN_TIMEOUT_GRACE_SECONDS=3600`

---

## New environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MIN_ENTRY_PRICE` | `0.10` | Skip tokens with best ask below this. Prevents penny-token spread losses. |
| `MIN_BID_DEPTH_USDC` | `20.0` | Require this much bid-side USDC depth. Ensures exit liquidity exists. |
| `MAX_ENTRY_SPREAD_PCT` | `0.05` | Reject entry when bid-ask spread exceeds 5%. |
| `MARKET_BRAIN_TIMEOUT_FLAT_GRACE_PCT` | `0.01` | P&L threshold for "flat" (±1%). Grace applies below this. |
| `MARKET_BRAIN_TIMEOUT_GRACE_SECONDS` | `3600` | Extra seconds to hold flat positions at timeout (1 hour). |

All added to `.env.example` and `SPEC.md` §7.

## Files modified

| File | Lines changed | What changed |
|---|---|---|
| `agents/polymarket/polymarket.py` | +31 | 3 constants + 3 entry guards in `_fillable_market_buy()` |
| `agents/application/trade.py` | +27 -1 | Early token_id extraction + extended ValueError matching |
| `agents/application/trade_log.py` | +38 -6 | token_id index + optional param on 2 dedupe methods |
| `agents/application/market_brain.py` | +15 | 2 config fields + env parsing + grace logic in `evaluate_exit()` |
| `agents/application/external_conviction.py` | +17 -1 | In-memory guard + token_id in dedupe calls |
| `.env.example` | +7 | 5 new env vars documented |
| `SPEC.md` | +8 | 5 new env vars in §7 risk gates table |
| `tests/test_trader.py` | +135 -1 | `TestEntryGuards` (4 tests) + `TestTokenIdDedupe` (3 tests) + mock fix |
| `tests/test_market_brain.py` | +71 | `TestTimeoutGrace` (4 tests) |
| `tests/test_external_conviction.py` | +39 | `TestInMemoryDuplicateGuard` (1 test) |

**Total: +375 lines, -13 lines across 10 files.**

## Tests

12 new tests added (11 new test methods + 1 mock infrastructure fix):

### TestEntryGuards (test_trader.py)
- `test_rejects_penny_token` — best ask $0.05 < MIN_ENTRY_PRICE → ValueError
- `test_rejects_thin_bid_depth` — bid depth $2.40 < $20 threshold → ValueError
- `test_rejects_wide_spread` — spread 33% > 5% threshold → ValueError
- `test_passes_good_book` — healthy book passes all guards → returns prices

### TestTokenIdDedupe (test_trader.py)
- `test_filled_position_found_by_token_id` — hex market_id + token_id inserted;
  query by different market_id + same token_id → found
- `test_active_trade_found_by_token_id` — same pattern for pending trades
- `test_backward_compatible_without_token_id` — old-style call without
  token_id still works

### TestTimeoutGrace (test_market_brain.py)
- `test_flat_at_timeout_gets_grace` — +0.6% pnl at 1801s → `timeout_grace_flat`
- `test_grace_expired_forces_timeout` — same position at 5401s → `timeout`
- `test_losing_at_timeout_no_grace` — -4% pnl at 1801s → `timeout` (no grace)
- `test_stop_loss_fires_before_grace` — -8% at 1801s → `stop_loss` (not grace)

### TestInMemoryDuplicateGuard (test_external_conviction.py)
- `test_duplicate_market_blocked_in_same_cycle` — two identical market
  candidates, only one order placed

### Existing test mock fix
- `TestExecuteMarketOrderSideMapping._book()` — added default bids to mock
  order book so existing tests pass the new bid-depth guard

**Full suite: 172 tests pass (126 + 46 from other modules), 0 failures.**

## Expected impact

Based on today's 16-trade sample:

| Fix | Trades it would have blocked | Saved loss |
|---|---|---|
| Fix 1 (min price) | 3-4 penny-token entries | ~$0.20-$0.30 |
| Fix 2 (bid depth) | 4 no-bid entries | ~$0.25 |
| Fix 3 (spread) | 2-3 wide-spread entries | ~$0.15 |
| Fix 4 (timeout grace) | 0 blocked, 9 delayed | ~$0.40 (avoided spread-cost exits) |
| Fix 5 (token_id dedupe) | 0-1 cross-agent duplicates | ~$0.05 |
| Fix 6 (cycle guard) | 1 duplicate entry | ~$0.05 |

Conservative estimate: these fixes would have prevented 9-12 of today's 12
losing trades, saving ~$0.70-$1.10 of the $0.77 loss.

## What was NOT changed

- No changes to LLM prompts or strategy logic
- No changes to position_manager exit logic
- No changes to risk_gate thresholds
- No changes to Docker or deployment configuration
- No changes to the scalper module
- No changes to any other strategy agent (btc_daily, near_resolution, etc.)

## Next steps (not done — require user decision)

1. **Deploy** — rebuild Docker image and restart containers
2. **Monitor** — watch first 24h of trades with new guards active
3. **Tune thresholds** — if too many markets are being rejected, consider
   loosening `MIN_BID_DEPTH_USDC` from $20 to $10 or `MAX_ENTRY_SPREAD_PCT`
   from 5% to 8%
4. **Exit logic improvement** — the timeout grace buys time but doesn't fix
   the fundamental issue that flat positions have no positive expected value.
   Consider adding a "market moved toward us" condition for grace extension.
5. **Spread-aware position sizing** — instead of binary accept/reject, could
   reduce position size proportional to spread width
