# SESSION 2026-05-24 — Bayesian Engine + Agent Landscape Handoff

**For the next agent (codex or otherwise) picking up this work.**
**Version after this session**: bot frozen, $21.00 cash, 0 open positions,
day total PnL **−$0.40 across 27 closed trades**.

---

## What was built today (commits in order)

```
b0d83ea — Day 1: TradeLog.annotate_brain_decisions_for_close + backfill script
648ba5e — Day 2: probability_calibrator.py + per-segment Wilson stats
ebebf8e — Day 3: bayesian_aggregator.py + EdgeResult
c56a176 — Day 4-5: wire bayesian gate into scanner_executor + cron refresh
```

Earlier session same day:
```
8e429db — agent_edge_report.py (first per-source edge measurement)
9fcd1ec — markout live-fallback
8f95ab8 — freeze-shadow gap fix (is_freeze_only_block)
adda2ec — FREEZE_HALT_MARKER recognition
1a4eb5b — MTM SELL bug fix (anchor_price inversion at risk_gate:227)
8c657da — float epsilon in meta_brain weighted scoring
0675c04 — backup rotation (4.5GB/day disk leak)
e19a6f9 — SPEC.md brain_indicator env vars + freeze-shadow rationale
f4c2df4 — max_hold 6h → 1h (data-driven exit cap)
43c674e — consensus_router infrastructure (default OFF)
0b032a5 — opportunity funnel open (4 agents in scanner_executor + consensus enabled)
```

15+ commits in one session. Suite: ~740 tests, all green.

---

## The Bayesian Engine (Days 1-7 of the probability plan)

Operator's design intent: for any opportunity, compute calibrated
P(win | all signals) and only trade when our calibrated probability
exceeds market-implied probability by a margin.

### Components

1. **Outcome annotation** (`TradeLog.annotate_brain_decisions_for_close`)
   - Called from `position_manager.py` after every TP/SL/timeout close
   - Backfilled via `scripts/backfill_brain_decisions_outcomes.py`
   - Result: 16,489 brain_decisions rows tagged with outcome_status
     (was 0 before today)

2. **Per-segment calibrator** (`agents/application/probability_calibrator.py`)
   - Reads decision_journal closes + their originating brain_decisions
   - Filters to decision_type='entry' AND action IN (BUY, SELL)
   - Computes (signal_source | market_type | action | price_band) winrates
   - Wilson 95% lower bound for small-sample-conservative point estimate

3. **Edge aggregator** (`agents/application/bayesian_aggregator.py`)
   - `compute_edge(candidate, calibration)` → EdgeResult
   - For each candidate: looks up most-specific calibrated segment
   - `edge = wilson_lower(p_win) - implied_p_win - min_edge_margin`
   - Returns actionable boolean

4. **scanner_executor integration**
   - New cfg: `bayesian_gate_enabled`, `bayesian_min_edge`, `bayesian_min_samples`
   - New reject reason: `bayesian_edge_below_threshold`
   - Calibration auto-refreshes via brain_indicator_cycle hourly step

### Empirical verdict on 30d data (81 matched closes)

```
SELL @ 0.50 alphainsider:  edge=-0.38  →  REJECT
BUY  @ 0.45 alphainsider:  edge=-0.25  →  REJECT
EVERY segment: edge < min_edge (0.05)
```

The engine **correctly refuses to fire any trade** because no segment
has Wilson lower bound > implied_p_win + 0.05.

**BUT** — the agent_edge_report on the same data shows:
- `opportunity_factory,alphainsider_proven,crypto_tape` IN-BAND markouts:
  18 samples, **+18.51% average return**
- Full sample (48 markouts): +1.65% average return

So there IS positive average return, but Wilson lower bound on raw
winrate (36%) gives ~0.20, which fails edge calc. The gate is
**winrate-based**, not **expected-value-based**. That's the gap to
fix next (see Task #62 in this session).

---

## THE BIG STRUCTURAL FINDING (audit of 22 entry agents)

`agent_edge_report` and `probability_calibrator` only see what passes
through `scanner_executor`. Audit on last 7 days:

| Agent | Approvals | with token_id | decision_journal ENTER | markouts |
|---|---:|---:|---:|---:|
| market_scanner | 19,913 | 4,025 | **0** | **0** |
| **scanner_executor** | **147** | **147** | **140** | **49** |
| btc_5min | 740 | 32 | 0 | 0 |
| scalper | 2,786 | 2,786 | 0 | 0 |
| external_conviction_api | 3,269 | 3,269 | 0 | 0 |
| external_conviction_crypto_tape | 926 | 926 | 0 | 0 |
| external_conviction_divergence | 104 | 104 | 0 | 0 |
| external_conviction_aggregator | 60 | 60 | 0 | 0 |
| external_conviction_debate | 52 | 52 | 0 | 0 |
| external_conviction | 21 | 21 | 0 | 0 |
| btc_daily | 25 | 25 | 0 | 0 |
| crypto_5m_market_maker_shadow | 16 | 16 | 0 | 0 |
| external_conviction_openbb | 4 | 4 | 0 | 0 |
| opportunity_factory | 0 | 0 | 0 | 0 |
| near_resolution | 0 | 0 | 0 | 0 |
| external_conviction_alpaca | 0 | 0 | 0 | 0 |
| external_conviction_gdelt | 0 | 0 | 0 | 0 |
| external_conviction_polifly | 0 | 0 | 0 | 0 |
| external_conviction_technical | 0 | 0 | 0 | 0 |
| external_conviction_tradingview | 0 | 0 | 0 | 0 |
| external_conviction_whale | 0 | 0 | 0 | 0 |
| external_conviction_crypto_deriv | 0 | 0 | 0 | 0 |

### Three pipelines, not one

1. **scanner_executor pipeline**: market_scanner approves → scanner_executor
   gates → decision_journal write → execution OR shadow_filled. This is
   the ONLY path that writes decision_journal entries and has markouts.

2. **Direct-execution pipeline**: btc_5min and scalper have their own
   `EXECUTE_BTC_5MIN` / `EXECUTE_SCALPER` flags and write straight to
   the trades table (skip decision_journal). They DO fire trades; they
   just don't show in agent_edge_report.

3. **Shadow-research pipeline**: external_conviction_* agents emit
   brain_decisions but have no execution path. They're read by
   meta_brain as input signals, never become trades on their own.

### What this means for the calibrator

The calibrator is correctly built but operates on a **biased slice**.
It only knows about scanner_executor's funnel. The 21 other agents are
invisible to it.

**To get true per-agent edge measurement:**
- btc_5min / scalper PnL: query `trades` directly by `cycle_id` pattern
  (their close rows have cycle_id like `btc_5min:...` or `scalper:...`)
- external_conviction_*: their brain_decisions need separate markout
  joining, NOT via decision_journal

---

## Today's live trading rounds

Operator wanted to "trade now" — armed 6 times, all small budgets:

| Round | Config | Fills | Closes | PnL |
|---|---|---:|---:|---:|
| 1 (morning) | scanner_executor only, $5 | 0 | 0 | $0 |
| 2 | + proven-override | 9 | 9 SL | −$0.16 |
| 3 | revert override | 0 | 0 | $0 |
| 4 | band 0.40-0.55 | 12 | 9 SL + 2 TP + 1 dust | ≈$0 |
| 5 | + bayesian gate (but module not in image!) | 5 | 1 SL + 4 deferred | −$0.10 |
| 6 | + bayesian (rebuilt) + override | 6 | 6 SL | −$0.14 |

Day total: 27 closes, **−$0.40** (1.9% of $21 wallet).

The 2-TP wins in Round 4 (+$0.31, +$0.32) validated that BUY @ 0.40-0.55
band CAN produce winners with 4.5x RR. The 9-loss SELL pattern in Round
2 / 6 confirmed favorite-longshot is bad.

---

## What's broken / pending for next session

1. **RR-aware bayesian gate** (Task #62 — being built now)
   - Current: rejects when wilson_lower(p_win) < implied + min_edge
   - Should also consider: E[return] = p_win × avg_win + p_loss × avg_loss
   - AlphaInsider 36% wr × +6% avg_win + 64% × -1% avg_loss = +1.52% per
     trade. That IS edge. Current gate misses it.

2. **Per-agent calibration for direct-execution agents** (btc_5min,
   scalper)
   - Need: separate query path that joins trades.cycle_id LIKE 'btc_5min%'
     to brain_decisions
   - Output: extends calibrator's per_signal_source view

3. **Per-agent calibration for shadow agents** (external_conviction_*)
   - These NEVER fire trades on their own
   - But they DO emit signals that feed meta_brain
   - Need: synthetic outcome — what would have happened if their signal
     was followed standalone?
   - This may require running their signals through a shadow-execution
     simulator

4. **Exit_deferred recovery** (4 rows from Round 6 — see ids 4220-4223)
   - Status: still `exit_deferred` (close attempts that didn't match)
   - The position_manager should retry; verify the retry actually fires

5. **Operator's morning question** — "65% winrate goal"
   - Today's data: alphainsider in-band 36% wr with 6:1 RR
   - 65% wr is not realistic without changing signal sources entirely
   - More realistic target: positive EV per trade (which we have on
     alphainsider in-band but Bayesian gate doesn't see it yet)

---

## File map (for handoff)

New code this session:
- `scripts/backfill_brain_decisions_outcomes.py` — one-shot backfill
- `agents/application/probability_calibrator.py` — segment stats
- `agents/application/bayesian_aggregator.py` — edge computation
- `scripts/refresh_probability_calibration.py` — daily-refresh wrapper
- Tests for each above

Modified:
- `agents/application/trade_log.py` — annotate_brain_decisions_for_close
- `agents/application/position_manager.py` — annotation hook after close
- `agents/application/scanner_executor.py` — bayesian_gate wiring, env loaders
- `scripts/brain_indicator_cycle.py` — calibration_refresh step
- `scripts/runtime_control.py` — BASE_ENV opens to 4 agents + consensus on
- `agents/application/risk_gate.py` — MTM SELL fix + is_freeze_only_block
- `agents/application/meta_brain.py` — float epsilon in weighted score
- `SPEC.md` — brain_indicator_cycle subsection
- `.env.example` — 10+ new env vars documented

Earlier today's docs:
- `docs/SESSION_2026-05-24_FREEZE_SHADOW_FIX.md`
- `docs/SESSION_2026-05-24_EXIT_ANALYSIS.md`
- `docs/SESSION_2026-05-24_MORNING_TEST_POSTMORTEM.md`
- `docs/SESSION_2026-05-24_SHADOW_PHASE.md`

---

## Cross-references in memory

- `memory/strategy_realignment_2026_05_24.md` — 3-week plan
- `memory/morning_test_2026_05_24_lessons.md` — first morning test
- `memory/alphainsider_positive_edge.md` — +6.94% finding (initial)
- `memory/freeze_mode_shadow_gap.md` — pre-fix state
- `memory/freeze_shadow_gap_fix.md` — fix details
- `memory/consensus_router_2026_05_24.md` — Week-2 deliverable
- `memory/live_launch_readiness_2026_05_24.md` — overall state mid-day
- `memory/pnl_formula_correction.md` — corrected PnL semantics
- `memory/working_discipline.md` — 7 rules (still apply)
- `memory/agent_scope_directive.md` — operator wants all agents enabled

---

## Quick orient for the next agent

1. **Bot state**: frozen, no positions, $21.00 cash.
2. **Calibration JSON**: `/srv/poly1/data/probability_calibration.json` —
   refreshes hourly. Currently 81 matched closes, 4 signal sources.
3. **Bayesian gate enabled** in env (`SCANNER_EXECUTOR_BAYESIAN_GATE_ENABLED=true`)
   but operator may want it OFF for shadow-only learning until RR-aware
   fix is in.
4. **Open question for operator**: should we trade with the win-rate-only
   Bayesian gate (refuses everything), build the RR-aware version first
   (Task #62), or shift focus to per-agent calibration for btc_5min /
   scalper (Task pending)?
