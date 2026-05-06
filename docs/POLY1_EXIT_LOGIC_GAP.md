# poly1 main trader — exit logic gap

Date: 2026-05-06
Author: documented at user request
Status: known limitation, not currently being fixed

---

## What's missing

The `poly1` **main trader** (the LLM-driven agent in `agents/application/trade.py`)
**has no exit logic**. It only opens positions. It never closes them.

Specifically:

- No take-profit: if a position rises in price, it is NOT sold.
- No stop-loss: if a position falls in price, it is NOT sold.
- No time-based exit: positions are not closed after N hours/days.
- No dashboard alert when a position becomes deeply unprofitable.

The relevant code is `agents/application/trade.py:292`:

```python
def maintain_positions(self):
    # Stub - read-only here; full close logic out of scope for v1.
    pass
```

That's it. A literal `pass`. Documented as out-of-scope in `CLAUDE.md`:

> "What's intentionally NOT in scope (don't add without discussion):
> - Position close / `maintain_positions` (stub)."

---

## What this means in practice

When `poly1` opens a position, the position lives until the **market resolves**
on Polymarket. Resolution is whenever the underlying question gets a definitive
answer. Examples:
- A daily up/down market resolves at the end of that UTC day.
- An election market resolves on/after election day.
- A long-dated question (e.g., "Will X happen by Dec 31") resolves at the deadline.

At resolution, each share of the **winning** outcome pays $1.00. Each share
of the **losing** outcome pays $0.00.

So if `poly1` buys 10 shares of YES at $0.40 (cost $4.00):
- Market resolves YES → 10 × $1.00 = **$10.00 received** (profit $6.00).
- Market resolves NO → 10 × $0.00 = **$0.00 received** (loss $4.00).
- Mid-stream price changes are **invisible to PnL** because we never sell.

This is why we can have an open position whose mark-to-market value is half
of cost (e.g., position 566188 entered at $0.38, current MTM at $0.21) — it
just sits and waits.

---

## Concrete current state (2026-05-06)

| Market | Side | Entry | Current MTM | "Paper" P/L |
|---|---|---|---|---|
| 566188 | BUY | $0.38 | $0.205 | **-$0.92 unrealized** |
| 566228 | BUY | $0.997 | $0.997 | $0.00 (flat) |
| 566187 | SELL→BUY-No | $0.435 | $0.205 | **-$1.00 unrealized** |
| 653788 | BUY | $0.11 | $0.105 | -$0.17 |

If the bot had exit logic, it would have either:
- Cut losses on 566188 and 566187 when they dropped, OR
- Locked in profits if they had risen.

Neither happens today.

---

## Dashboard — what it shows and what it doesn't

The Streamlit dashboard at `http://localhost:8050` has a "P&L" tab.

**What the dashboard currently shows:**
- Filled trade count
- Total USDC deployed (sum of `size_usdc` for filled trades)
- Daily capital deployed (chart)
- Cumulative capital deployed (chart)
- Per-trade detail: entry price, USDC paid, confidence, market ID

**What the dashboard does NOT show:**
- Per-position **mark-to-market** value (current price vs entry).
- Per-position **unrealized P&L**.
- Whether each position is currently winning or losing.
- Whether a market has resolved.
- Final P&L after resolution.

The dashboard itself flags this with a warning at the top of the P&L tab:

> ⚠️ P&L is approximate. We track capital deployed (USDC paid per filled
> trade). Actual settlement profit requires outcome data — not yet
> tracked in DB.

So **today**, looking at the dashboard does not tell you whether a position
is profitable or losing. You only see what was paid.

To check actual MTM, you have to run the manual MTM script (the one we
used in the morning audit) — query `trade_log.db` for filled positions, hit
Polymarket's `get_midpoint` for each token_id, multiply by shares.

---

## What it would take to add exit logic

A minimum-viable position-close module would need:

1. **Position registry** — periodically read filled trades from `trade_log.db`,
   group by token_id, sum shares, track entry price.
2. **MTM watcher** — fetch current midpoint per token, compute unrealized P&L.
3. **Exit policy** — config-driven rules: take-profit at +X%, stop-loss at
   -Y%, max-hold of Z hours.
4. **Sell executor** — when a rule triggers, call `Polymarket.execute_market_order`
   with the opposite side. Polymarket has no "close position" primitive; you
   sell by buying the opposite outcome at `1 - target_price`.
5. **Idempotency** — a sell must not be re-fired across crashes. Same
   `pending_orders`-style contract poly1 already has for entries.
6. **Risk gate integration** — sells should still respect `MAX_TRADES_PER_HOUR`
   and `MIN_USDC_FLOOR`.
7. **Tests** — unit tests for each policy, integration test that mocks
   Polymarket and verifies a full open-close cycle.

Realistic effort: **4-6 hours of focused work** plus a shadow-mode soak
period before flipping live.

This is *not* trivial because Polymarket FOK semantics, slippage caps, and
the `MAY_HAVE_FIRED` recovery contract all interact with sells exactly the
same way they do with buys.

---

## Why it's not being done today

The user explicitly stated (2026-05-06): they don't like money sitting in
unrealized losing positions, but they're not asking for the strategy to be
built right now. They want this gap **clearly documented** so it can be
prioritized later.

This file is that documentation.

---

## Comparison — the other agents

| Agent | Has exit logic? | Mechanism |
|---|---|---|
| poly1 main trader | ❌ No | Holds until market resolution |
| poly1 scalper | Partial | Scalper exits on `BOTH_FILLED` (both legs filled = closed pair) or `EXPIRED` (period passed). No mid-pair stop-loss. |
| poly1 news_signal | N/A | Dry-run analytics only — no positions. |
| swarm `mean_reversion_agent` | ✅ **Yes** | take_profit_cents, stop_loss_cents, max_hold_minutes — full intraday entry+exit cycle. |
| swarm `market_maker_agent` | ✅ Yes | Two-sided quotes, captures spread per round-trip. |
| swarm `nothing_happens_agent` | ✅ Yes | Time-based exits per strategy spec. |

The mean reversion agent in the swarm is what the user is pointing at when
they say "I want fast in/out — enter and exit same day". That's exactly
what it does. **Activating it requires the swarm to be funded and approved
(see `docs/RUNBOOK_2026-05-07.md`).**

---

## Where this lives

- This document: `docs/POLY1_EXIT_LOGIC_GAP.md`
- Linked from: `deploy/CURRENT_STATUS.md`
- Code reference: `agents/application/trade.py:292`
- Spec reference: `CLAUDE.md` — "What's intentionally NOT in scope"
