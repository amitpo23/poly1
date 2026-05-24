# SESSION 2026-05-24 — Exit Logic Gap Analysis

**Question:** Why does the +6.94% / in-band +21.44% 5-min markout edge
(see `docs/SHADOW_REPORT_2026-05-24.md`) turn into a −$4.85 closed PnL
over the same 7 days?

**Answer:** Holding past the 5-min window destroys the edge. Forced
exits at 1h would have flipped the period from −$0.85 to +$0.52.

## Data

Closed trades, last 7 days, n=86:

| Hold bucket | n | Winrate | Total PnL | Avg per trade | Exit mix |
|---|---:|---:|---:|---:|---|
| **<5min** | 38 | **32%** | **+$0.86** | +$0.023 | 27 SL, 11 TP |
| 5-60min | 31 | 35% | −$0.34 | −$0.011 | 14 SL, 12 TP, 5 timeout |
| 1-4h | 12 | 8% | −$0.31 | −$0.026 | 3 SL, 1 TP, 8 timeout |
| >4h | 5 | 0% | −$0.27 | −$0.053 | 2 SL, 2 TP, 1 timeout |

(SL = stop_loss, TP = take_profit, timeout = closed_timeout)

Exit trigger distribution (7d): 67 stop_loss, 36 take_profit, 21 timeout,
10 resolved_loss, 1 dust.

## Diagnosis

The first 5 minutes is where the edge plays out. Beyond that:

1. **Negative drift dominates.** 5-60min bucket: 35% winrate is barely
   above the <5min bucket but the magnitude of losses exceeds gains
   (avg −$0.011/trade vs +$0.023/trade).
2. **Timeout sweep grows.** From 0 timeouts in <5min, to 5 in 5-60min,
   to 8 in 1-4h. These are positions the bot held past its hold-limit
   without hitting either TP or SL — pure mean reversion losses.
3. **Late losses are catastrophic.** >4h bucket: 0% winrate, −$0.053/trade.

The bot's empirical edge is short-horizon. The TP/SL infrastructure
designed for longer holds (6h max) is bleeding value back into the
market.

## Why this is the right window

`scripts/agent_edge_report.py` measures hypothetical 5-min markout edge.
The signal sources (especially `opportunity_factory,alphainsider_proven`)
show +6.94% / in-band +21.44% over 5 minutes. That's the operative
horizon for the bot's current architecture.

Beyond 5 min, the alpha decays. The market moves toward equilibrium and
the bot's directional bet — which was right at entry — becomes
indistinguishable from noise.

## What the existing config does

`scripts/runtime_control.py` BASE_ENV defaults (before this commit):
- `POLY1_MAX_HOLD_SECONDS=21600` (6 hours)
- `MAINTAIN_MAX_HOLD_HOURS=6`
- `MAINTAIN_TAKE_PROFIT_PCT=0.25` (25% — only at full-resolution scale)
- `MAINTAIN_PREFERRED_TAKE_PROFIT_PCT=0.04` (4%)
- `MAINTAIN_STOP_LOSS_PCT=0.06` (6%)

So a trade with no signal extreme enough to hit ±4-25% TP or ±6% SL
sits in the book for up to 6 hours. The data shows that's the
losing path.

## What this commit changes

`scripts/runtime_control.py` BASE_ENV defaults:
- `POLY1_MAX_HOLD_SECONDS`: 21600 → **3600** (1 hour)
- `MAINTAIN_MAX_HOLD_HOURS`: 6 → **1**

Effect (counterfactual on the 7-day sample):
- Trades capped at 1h: keep <5min and 5-60min buckets, drop 1-4h
  and >4h losses.
- Net PnL would have been **+$0.52** (vs actual −$0.85). +$1.37 swing
  across 65 trades that would have been kept.

TP/SL thresholds are NOT changed in this commit. Those need separate
study — they affect the within-bucket fire rate and might trade off
differently. The hold-cap change is the highest-confidence, lowest-risk
adjustment.

## What this commit does NOT change

- TP thresholds (4-25% range remain).
- SL threshold (6% remains).
- Brain exit authority (`MAINTAIN_BRAIN_MAX_HOLD_EXTENSION_HOURS` stays
  at the value the operator deployed earlier this session).
- Live trading state — bot stays frozen.
- Per-agent overrides (btc_5min has its own holds at 120/210 seconds;
  unchanged).

## Open questions for next session

1. **Tiered TP** — make TP threshold decline with time (5% in first
   minute, 3% in minutes 2-5, 1% in 5-15min). Would capture wins that
   currently drift back into losses.
2. **Tighter SL** — 67 SLs vs 36 TPs is a heavy loss ratio. Possibly
   3-4% SL would cut losses faster, but might also kill winners
   prematurely. Need n>30 of each variant before judging.
3. **Trailing stop tuning** — `MAINTAIN_TRAILING_STOP_PCT=0.02`. When
   does it activate? Could be tightened for short-horizon edge capture.

## Cross-references

- `docs/SHADOW_REPORT_2026-05-24.md` — first per-source edge report
- `memory/alphainsider_positive_edge.md` — +6.94% markout finding
- `memory/pnl_formula_correction.md` — corrected PnL semantics
- `docs/SESSION_2026-05-24_FREEZE_SHADOW_FIX.md` — measurement pipeline
  enabling future re-analysis
