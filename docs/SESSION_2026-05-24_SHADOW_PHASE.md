# SESSION 2026-05-24 — Shadow Measurement Phase Start

**State:** Bot frozen since 2026-05-24 07:03 UTC. Cash $21.20. 0 positions.

## What we agreed (operator-confirmed)

After three rounds of backtest analysis, the operator confirmed the
3-week plan:
- **Week 1**: Build shadow measurement infrastructure. No live trading.
- **Week 2**: Activate proven agents + build consensus_router (only if
  shadow data shows ≥2 agents with measurable edge on overlapping markets).
- **Week 3+**: Controlled live probe.

## Why we did NOT go live today

Three proposed tactical fixes were each rejected by backtest:

1. **4 narrow fixes (BUY/SELL band + score floor + quarantine + TTL)** —
   backtest on 7d showed +10% candidate flow, dominated by `risk_gate`
   blocks which turned out to be `freeze mode` itself. Real impact: 1-3
   fills in a 30-min probe.

2. **Consensus router** — feasibility check on 7d data: only 6 distinct
   markets had any 2-source agreement, all from shadow `external_conviction_*`
   pairs producing placeholder data at price 0.000. Real consensus events
   in profit zone: **0**. The router has nothing to operate on because
   agents look at non-overlapping markets.

3. **Just unfreeze and run** — bot's natural fill rate is ~1.6/hour (38
   per day × 7 days = ~265 fills, of which closes show 30% winrate and
   −$4.85 7d PnL). Running 30min would yield 0-1 fills with no
   statistical power and known negative EV.

## What we did instead

### Built `scripts/agent_edge_report.py`

A daily-run script that turns the `decision_journal` markouts into a
per-signal-source edge report. Markdown output to
`docs/SHADOW_REPORT_YYYY-MM-DD.md`.

Reads:
- `brain_decisions`: approvals per agent (last N days)
- `decision_journal`: ENTER + SHADOW_ENTER rows with `outcome_5m_json`
  markouts

Computes:
- Per signal source: decisions, in-profit-band count, with-markout count,
  hypothetical wins/losses, avg PnL %, in-band PnL %
- BUY PnL: `(best_bid_5m / entry) − 1`
- SELL PnL: `((1 − best_bid_5m) / (1 − entry)) − 1`

Tests: `tests/test_agent_edge_report.py` (8 unit tests, all green).

### First report findings (`docs/SHADOW_REPORT_2026-05-24.md`)

The biggest discovery from running this on 7d of real data:

| Signal source | Decisions | In band | Markouts | Wins | Losses | Avg PnL% | In-band PnL% |
|---|---:|---:|---:|---:|---:|---:|---:|
| `opportunity_factory,alphainsider_proven,crypto_tape` | 47 | 16 | 16 | 7 | 9 | **+6.94%** | **+21.44%** |
| `meta_brain` | 53 | 2 | 0 | 0 | 0 | — | — |
| `meta_brain,manifold,manifold:manifold` | 8 | 4 | 1 | 0 | 1 | −2.13% | −2.13% |

**First real per-source edge measurement we've ever had.** AlphaInsider
proven path shows positive hypothetical 5-min edge at +6.94% (in-band
+21.44%) over 16 entries. 16 entries is small but the in-band signal is
strong.

This contradicts the "all sources losing" mental model. AlphaInsider
calibrated path is the only signal with measurable positive edge so far.

## Caveats

- 5-min markout is a proxy for short-term edge, not closed PnL.
- Actual closed PnL last 7d: −$4.85. So holding past 5 min likely
  introduces adverse selection. The TP/SL strategy is the gap between
  hypothetical edge and realised loss.
- Sample sizes are small (16 markouts is barely above noise floor).
- 53 `meta_brain` SHADOW_ENTER decisions have NO markouts — this is a
  bug in the markouts pipeline that needs investigation. Without
  SHADOW markouts, we can't compare paths fairly.

## Next session priorities

1. **Fix SHADOW_ENTER markouts capture** — verify token_id presence on
   SHADOW_ENTER rows; likely the markouts script's `token_id IS NOT NULL`
   filter is excluding them.
2. **Run agent_edge_report daily** for 5+ days to grow sample size.
3. **Investigate `external_conviction_alpaca` 0 approvals** — 12,864
   decisions, 0 approvals over 7 days.
4. **Verify all 15 entry agents producing brain_decisions** in shadow.
5. **Wire agent_edge_report into brain_indicator_cycle** as a daily
   automatic step.
6. **Decide on AlphaInsider expansion** — given +21.44% in-band signal,
   should this path get higher trade allocation? Not without bigger
   sample.

## What stays FROZEN

- Live trading. Operator has explicitly chosen shadow path.
- Any "loosening" of gates or score thresholds. Backtest shows none
  will produce the operator's mission goal.
- Consensus_router build. Backtest shows no consensus exists in current
  agent design.
