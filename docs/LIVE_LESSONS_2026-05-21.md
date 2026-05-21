# Live Lessons - 2026-05-21

## Context

This document captures the live trading conclusions from the 2026-05-21
controlled probes so the next trading cycle starts from measured evidence,
not from another broad parameter release.

The server was left frozen at the end of the day. Do not restart live trading
from this document alone; use the normal preflight, runtime control, HALT, and
equity guard checks.

## What Worked

- `BUY` / Up-side entries were the only clearly positive side in today's live
  sample.
- The best executable entry band was `0.40 <= price < 0.50`.
- `scanner_executor` was the useful bridge from the broad evidence system into
  real orders because it consumed scanner approvals, checked live orderbook
  execution, then applied deterministic EV, risk, Kelly, and journal gates.
- Proven external sources such as AlphaInsider-style families, crypto tape, and
  proven wallet evidence are valuable as candidate generators, but only after
  they produce calibrated probability, live EV, and executable orderbook proof.

## What Did Not Work

- Broad `SELL` / Down-side entries lost money in the sample and should not be
  treated as symmetric with `BUY` until proven separately.
- Price band `0.50-0.60` performed poorly despite apparently strong scores.
- High score alone was not enough. The executor correctly rejected
  `probability_not_calibrated` candidates because score is ranking confidence,
  not tradable probability.
- Releasing all technical brakes increased activity, but not enough quality.

## Policy For The Next Controlled Cycle

Enable the scanner executor learning guard:

```env
SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED=true
SCANNER_EXECUTOR_LEARNING_PREFERRED_SIDE=BUY
SCANNER_EXECUTOR_LEARNING_MIN_ENTRY_PRICE=0.40
SCANNER_EXECUTOR_LEARNING_MAX_ENTRY_PRICE=0.50
SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_SIDE_OVERRIDE=false
SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_PRICE_OVERRIDE=false
```

Meaning:

- Default live entries from scanner approvals must be `BUY`.
- Default live entries must be executable in the `0.40-0.50` band.
- A strong source still needs calibrated probability and live EV; it does not
  bypass orderbook, risk, cooldown, or journal safety.
- Side/price overrides stay disabled until a separate backtest/shadow slice
  proves that the source deserves solo authority.

## Tomorrow's First Probe

Recommended starting shape:

- Keep position size small (`$1` per trade).
- Keep max open positions low (`2-4`) until the first hour shows clean exits.
- Run the brain indicator cycle with Tavily and LLM disabled unless a specific
  event requires them.
- Watch reject reasons, especially:
  - `today_lesson_side_blocked`
  - `today_lesson_price_band_blocked`
  - `probability_not_calibrated`
  - `orderbook_not_executable`
  - `risk_gate_blocked`
- Only consider widening the band after the new shadow/live markouts show that
  the extra slice is profitable.

## Next Improvement Loop

1. Build a daily scorecard by side, price band, signal source, market family,
   and entry hour.
2. Promote overrides only with enough samples and positive markout/PnL.
3. Keep all unproven agents in shadow until their markouts justify live budget.
4. Use the live day as data, not as an argument to loosen every gate.
