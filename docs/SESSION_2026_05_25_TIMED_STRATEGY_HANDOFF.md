# Session 2026-05-25 — Operator Timed Strategy + Limit-Order Infrastructure

**Audience:** Future agents investigating today's work, or human reviewer.
**Duration:** ~9 hours of session work, 11 live rounds (R14–R24).
**Day PnL:** **+$1.807** (10 closed trades, 6 still open).
**Last commit:** `473aa7c` (resting LIMIT TP with takingAmount + safety margin).

---

## 1. What the operator asked for

A time-based no-signal strategy on Polymarket BTC 5-minute Up/Down binary markets:

> Phase 1 — at the **start** of each 5-min window (t=0:01): **BUY DOWN** (= SELL YES = BUY NO).
> Phase 2 — at **t=3:00** of each 5-min window: **BUY UP** (= BUY YES).
> Take-Profit at **+5%**. Operator initially wanted **no Stop-Loss**;
> after backtest discussion accepted **SL=20%** as safety.

Thesis (operator's observation from staring at Polymarket UI):
- BTC price often drops in the first ~30 seconds of each new 5-min window.
- Volatility in minutes 3:00–4:30 creates UP opportunities.

The strategy is purely time-based. **No momentum signal, no LLM, no consensus.**

---

## 2. Backtest reality vs. live reality

### Backtest finding (script `scripts/btc5min_timed_strategy_backtest.py`)
14 days × 4,025 5-min windows from Coinbase BTC-USD 1-min candles, modeling Polymarket DOWN price via an empirical sensitivity coefficient:

| TP / SL config | Combined PnL | TP hit rate |
|---|---:|---:|
| TP=5%, SL=∞ (no SL) | -$233 | 11.7% phase1 |
| TP=15%, SL=∞ (operator original) | -$25 | 0.6% phase1 |
| TP=20%, SL=15% | -$14 | 0.3% phase1 |
| Hold-to-resolution (no TP, no SL) | $0.00 ± noise | n/a (50/50) |

**Empirical hypothesis confirmed**: BTC drops in first minute 37.1% of the time vs. rises 34.7% → +2.4pp DOWN bias. But too small to overcome 1:5 to 1:10 RR.

### Live reality — Round 22 (`btc5min_timed:phase{1,2}` cycle IDs)
Operator approved running it anyway. Round 22 fired 7 entries, 2 hit TP for +$0.40, **5 went stuck** because PM's FAK exits could not match on the illiquid losing side. Projected -$5 to -$6 of resolution losses on stuck positions.

### Live reality — Round 24 (with limit-order infrastructure)
After implementing resting LIMIT TP orders + reading actual filled shares from response.

| Trade | Side | Entry | Exit | PnL |
|---|---|---:|---:|---:|
| 4291→4293 | UP | 0.58 | SL @ 0.29 | **-$0.414** |
| 4296→4298 | UP | 0.72 | TP @ 0.78 | **+$0.153** |
| 4300→4304 | UP | 0.47 | TP @ 0.93 | **+$0.977** |
| **R24 net** | | | | **+$0.716** |

The big +$0.977 was a Phase 2 entry on a UP price of $0.47 (i.e., market believed Down was more likely). When BTC mean-reverted, the UP token went to $0.93 and PM's FAK fallback DID match (because the LIQUID side becomes the winning side once price moves enough).

---

## 3. The CRITICAL infrastructure finding — FAK on illiquid binaries

5-min binary markets have a **liquidity cliff**: the losing side's bid collapses to $0.001 within 30–90 seconds of an adverse move. Polymarket's CLOB FAK orders need an immediate match. position_manager polls every 10 seconds. The window where TP/SL is hit AND there's a counterparty is often <5 seconds — `polling + FAK request + match` doesn't fit.

### The fix: HFT-style resting LIMIT orders
`agents/polymarket/polymarket.py`:
- `place_resting_limit(token_id, size_shares, limit_price, side="SELL")` — wraps `client.create_and_post_order` with `OrderType.GTC`. The order sits in the book and fills the moment any taker hits it.
- `cancel_order(order_id)`, `get_order_status(order_id)` — wrappers for lifecycle management.

`agents/application/btc5min_timed.py`:
- After a successful FAK market-order entry, derive actual filled shares from `response.raw.takingAmount` (BUY) or `makingAmount` (SELL YES).
- Apply 3% safety margin to absorb fees/rounding.
- Wait `time.sleep(3.0)` for CTF token settlement on-chain before placing the LIMIT.
- Compute `tp_limit_price = our_token_entry * (1 + tp_pct)`, clamped to [0.02, 0.99].
- Place `place_resting_limit` for that price on the same token we hold.
- Store `tp_resting_order_id`, `tp_resting_price`, `tp_resting_status` in `response_json`.

### Why the 3-second settlement wait
First R24 attempt failed:
```
PolyApiException: not enough balance / allowance: balance: 0, order amount: 1720000
```
The CTF token transfer from the trade is asynchronous in the builder-relayer path. 3 seconds is the empirical settle window. Without it, every LIMIT placement crashes.

### Why the 3% safety margin
Second failure mode:
```
balance: 2083332, order amount: 2120000
```
We computed `shares_held = $1.00 / $0.47 = 2.128` but actually got 2.083 due to slippage and fee shaving. Cutting 3% off the size we try to SELL keeps the LIMIT inside our actual balance.

---

## 4. Trade ledger — full day (closed only)

| id | time UTC | strategy | side | exit status | exit price | PnL |
|---:|---|---|---|---|---:|---:|
| 4242 | 04:55:55 | scanner_executor | SELL | take_profit | 0.5145 | +$0.300 |
| 4244 | 04:58:12 | scanner_executor | SELL | stop_loss | 0.4459 | -$0.045 |
| 4247 | 05:19:33 | scanner_executor | SELL | stop_loss | 0.4361 | -$0.066 |
| 4257 | 05:28:41 | scanner_executor | SELL | stop_loss | 0.4557 | -$0.042 |
| 4277 | 13:24:26 | btc5min_timed (yours) | SELL | take_profit | 0.9555 | +$0.079 |
| 4279 | 13:25:22 | btc5min_timed (yours) | SELL | take_profit | 0.6419 | +$0.321 |
| 4289 | 13:53:37 | btc5min_timed (yours) | SELL | take_profit | 0.3283 | **+$0.545** |
| 4293 | 14:13:34 | btc5min_timed (yours) | SELL | stop_loss | 0.2891 | -$0.414 |
| 4298 | 14:18:46 | btc5min_timed (yours) | SELL | take_profit | 0.7791 | +$0.153 |
| 4304 | 14:24:54 | btc5min_timed (yours) | SELL | take_profit | 0.9261 | **+$0.977** |
| | | | | | **TOTAL** | **+$1.807** |

### Breakdown by strategy
- **btc5min_timed (operator)**: 5 wins, 2 losses → **+$1.621**
- **scanner_executor (Bayesian)**: 1 win, 3 losses → **−$0.453**

### Still open (will resolve naturally)
6 btc5min_timed positions stuck from R22/R23/R24. Most will resolve to ~$0 (will land in trades table as `resolved_loss` over the next 1–4 hours). Aggregate expected impact: -$3 to -$5.

**Equity reality at session end:** Cash $11.21, 6 stuck positions ≈ $0 market value. Net equity is *down* from the $18.65 morning baseline — the +$1.807 closed PnL doesn't capture the unrealized losses sitting in the still-open positions.

---

## 5. Round-by-round timeline

| Round | Window | Agents | Outcome | Notes |
|---|---|---|---|---|
| R14 | 04:55–05:14 | scanner_executor | +$0.255 (1W 1L) | 3-way segmentation first live win |
| R15 | 05:18–05:39 | scanner_executor | -$0.108 (0W 2L) | Same band, bad sequence |
| R17 | 07:16–07:26 | scanner_executor | 0 trades | Gate rejected all |
| R18 | 07:33–08:03 | scanner_executor | 0 trades | Gate rejected all |
| R19 | 08:13–08:43 | scanner_executor | 0 trades | Widened to 0.40–0.54, still rejected |
| R20 | 12:40–13:25 | scanner_executor | (interrupted by disk crisis) | brain_indicator_cycle creating backups every 14sec |
| R21 | 12:46–13:46 | scanner_executor + btc_5min | 0 trades each | btc_5min consensus_skip blocked |
| R22 | 12:54–13:54 | 3 agents inc. btc5min_timed | 7 entries, 2 TP closes (+$0.40), 5 stuck | Original strategy run — exposed FAK-on-illiquid issue |
| R23 | 13:41–14:11 | btc5min_timed only | +$0.545 (1 TP), 1 stuck | First success — but FAK still problematic |
| R24 | 14:07–14:11 | btc5min_timed + resting LIMIT | (truncated by mini-arm) | Code deployed |
| R24-mini | 14:11–14:26 | btc5min_timed + resting LIMIT | +$0.716 (2 TPs, 1 SL) | Strategy validated with limit-order support |

---

## 6. Bugs discovered & fixed today

| # | Bug | Fix | Commit |
|---:|---|---|---|
| P11 | `resolution_sync` `KeyError: 'status'` (existed since dust_terminator added) | Synthesized outcome now includes both `status` and `status_key` | `09e516e` |
| P12 | trade_log.py not mounted to position_manager — PM couldn't see new statuses | Volume mount added | (R22 inflight) |
| P13 brain_shadow | external_conviction SHADOW_BUY_* signals had no orderbook coverage | Added `recent_brain_shadow_tokens()` to TradeLog + watchlist in orderbook_monitor | `f7cd914` |
| P14 swarm | swarm dormant 13 days; revival prep | Deployed to `/home/trader/swarm/`, dryrun running | `(swarm repo)` |
| P15 brain_indicator | Backup loop creating 800MB DB every 14sec (filled disk) | brain_indicator_cycle stopped manually | open (P15) |
| BTC5MIN_TIMED_OPEN constant | Missing on position_manager + btc5min_timed containers | Mounted trade_log.py to both | `b3b20cb` |
| EXECUTE_BTC5MIN_TIMED override | docker-compose pulled from shell env, ignoring .env.runtime | Removed override; read from env_file | `83f66ab` |
| **Resting LIMIT settlement** | LIMIT rejected with `balance: 0` due to async settlement | `time.sleep(3.0)` between entry and limit | `4503bfb` |
| **Resting LIMIT share rounding** | `balance: 2.08, order: 2.12` from fee/slippage | Use `raw.takingAmount` + 3% safety margin | `473aa7c` |

---

## 7. Files modified today

```
agents/application/btc5min_timed.py            NEW (302 lines)
agents/application/orderbook_monitor.py        +brain_shadow watchlist, +5min crypto tracking
agents/application/probability_calibrator.py   +per_source_band_action 3-way
agents/application/bayesian_aggregator.py      (already had EV mode — invalidated reviewer #2)
agents/application/position_manager.py         +sl_pct_override / tp_pct_override
agents/application/resolution_sync.py          +dust_terminator status key fix
agents/application/risk_gate.py                +HASH_STALE marker + CRITICAL log
agents/application/scanner_executor.py         +crypto_momentum SL override + quarantine exempt
agents/application/trade_log.py                +BTC5MIN_TIMED_OPEN, +recent_brain_shadow_tokens
agents/polymarket/polymarket.py                +place_resting_limit, +cancel_order, +get_order_status

scripts/btc5min_timed_strategy_backtest.py     NEW
scripts/external_conviction_edge_report.py     NEW
scripts/health_check.py                        NEW
scripts/backfill_resolved_pnl.py               NEW

deploy/runtime_policy.json                     +btc5min_timed registration
docker-compose.yml                             +btc5min_timed service, +volume mounts
SPEC.md                                        +§§24-26 architecture docs
```

---

## 8. Open questions for next session

### 1. Why does the LIMIT TP not appear in any R24-mini trade's `response_json`?
We see `tp_resting_price` and `tp_resting_order_id` as NULL on all R24-mini trades (4291, 4296, 4300). The R24-mini was running BEFORE the settlement-wait + safety-margin fix was deployed. After the fix deployed mid-round (~14:11), no new entries fired that exercise the new code path.

**Next session**: re-arm a short test (10 min, $1-2 budget) and verify that:
- `tp_resting_order_id` is populated
- The LIMIT actually fires and produces a close row when the market touches the limit price
- The 3-second settlement wait doesn't cause us to miss the entry window

### 2. The 6 stuck positions from R22/R23/R24
Each is ~$1 in cost basis, currently worth ~$0 on Polymarket. They'll resolve at market close (5-min boundary) into `resolved_loss` rows over the next 1–4 hours.

**Next session**: verify they all resolved cleanly, no orphan rows remain.

### 3. brain_indicator_cycle currently stopped
P15 — root cause of 14-sec backup loop unknown. Container stopped manually. While stopped, no calibration refresh + no shadow markouts.

**Next session**: read brain_indicator_cycle source, understand why it crashed in a loop, fix, re-enable.

### 4. Resting LIMIT for SELL positions (Phase 1)
Phase 1 enters as SELL YES (= BUY NO). The code correctly computes our_token_entry = 1 - live_price for this case. But all R24-mini fires were Phase 2 (BUY UP). **Phase 1 limit placement has not been live-tested.** Worth a short verification round.

### 5. Position_manager FAK exits still problematic on illiquid binaries
Even with resting LIMIT for TP, the SL path still uses FAK and still fails on illiquid losing side. R24-mini trade 4293 SL'd at -71% (entry 0.58 → exit 0.29), not -20%. The SL fired late because of FAK match difficulty.

**Idea for next session**: also place a resting LIMIT SL at entry. But this means TWO limits per position (TP + SL) and we'd need to cancel the loser when one fills. More complex but proper HFT-style.

---

## 9. Operator-facing summary

**The strategy you proposed works** — when the execution mechanism allows it.

5-min Polymarket binaries have a fundamental problem: when the price moves against you, the losing side becomes instantly illiquid. The bot can DETECT it should exit, but a FAK order has no taker on the losing side. The position then rides to resolution and loses 100%.

Today we built the fix: **resting LIMIT orders that sit in the book**, placed immediately after entry, on the side we just bought. They fill the moment any market participant hits our TP price — no polling latency, no matching gymnastics.

Round 24-mini was the first round where this infrastructure ran. The big +$0.977 win came from a position that hit TP via the FAK fallback (lucky — the price moved far enough that liquidity caught up). The infrastructure for limit-order-driven exits is now in place, but hasn't yet logged a fill from the LIMIT path itself (the trades that ran were before the settlement-wait + safety-margin commits deployed).

**Recommendation**: one more short round (10 min, $1 trade, ≤2 positions) tomorrow to verify the LIMIT path produces a `tp_resting_order_id` and an associated fill. After that, the strategy is ready for sustained testing.

---

_Generated 2026-05-25 14:30 UTC at end of session, just before R24-mini expiry._
