# SESSION 2026-05-24 (continued) — Freeze-Mode Shadow Gap Fix

**State:** Bot frozen since 2026-05-22 16:06. Cash $21.20. 0 positions.
This session's deliverable: shadow measurement pipeline can now accumulate
data WHILE the bot is frozen against live execution.

## The gap (recap)

The Week-1 shadow measurement plan (`agent_edge_report.py`) requires
SHADOW_ENTER rows in `decision_journal`. But on 2026-05-24, post-freeze
investigation showed:

- 7-day brain_decisions: 40k+ rows (bot's scanner agents still running)
- 7-day decision_journal SHADOW_ENTER: 55 rows, none since 2026-05-22 16:06
- Root cause: `risk_gate.reason()` returns
  `"runtime control mode=freeze blocks live entries"`. scanner_executor's
  pre-sweep and per-market gates both treat this as a hard veto and skip
  the per-market path entirely. The `_record_approval` /
  `insert_decision_journal` calls never execute → no SHADOW_ENTER.

Without the fix, Week-1 measurement is dead in the water.

## The fix

Surgical change in two files; no behavior change for any non-freeze state.

### `agents/application/risk_gate.py`

1. **`reason(*, skip_runtime=False, skip_halt=False)`** — keyword-only
   args let callers ask "ignoring the freeze flag (and its paired HALT
   marker), is any real risk gate firing?" Production callers
   (`risk_gate.ok()`, scanner_executor pre-sweep / per-market) keep the
   default (False, False) — production behavior unchanged.

2. **`is_freeze_only_block()`** — returns True iff:
   - runtime control mode is `freeze`, AND
   - HALT file (if present) contains `FREEZE_HALT_MARKER`
     (`"set by runtime_control.py freeze"`), AND
   - `reason(skip_runtime=True, skip_halt=True)` is None
     (no balance/drawdown/PM-guard/etc. veto)

   This correctly distinguishes:
   - A planned freeze (operator ran `runtime_control.py freeze`) →
     shadow allowed
   - An emergency stop (supervisor HALT, operator HALT with different
     content) → shadow blocked too
   - Other risks (balance below floor, drawdown limit, PM guard) →
     shadow blocked

3. **`FREEZE_HALT_MARKER`** class constant — matches the literal string
   that `scripts/runtime_control.py freeze()` writes into the HALT file.

### `agents/application/scanner_executor.py`

1. **Pre-sweep cycle check (line ~300)**: `cycle_blocked` only fires if
   `risk_gate.is_freeze_only_block()` returns False. A freeze-only block
   lets the cycle proceed to per-market routing.

2. **Per-market check (line ~645)**: When `risk_gate.reason()` is set:
   - If `is_freeze_only_block()` → set `force_shadow_from_freeze = True`
     and continue (don't reject)
   - Else → `_record_reject(..., "risk_gate_blocked", ...)` and skip
     (unchanged behavior for real risk vetoes)

3. **Shadow path trigger (line ~732)**: changed from
   `if not self.execute:` to
   `if not self.execute or force_shadow_from_freeze:`.

## Verification

### Tests (committed `8f95ab8` and `adda2ec`)

- 7 new tests in `TestRiskGate` (test_trader.py):
  - is_freeze_only_block true / false under each combination
    (only freeze, freeze+balance, freeze+emergency-HALT,
    freeze+freeze-paired-HALT, no runtime, live mode, skip_runtime
    semantics)
- 1 new test in `test_scanner_executor.py`:
  - freeze-only block → cycle continues, SHADOW_ENTER written,
    `execute_market_order` not called
- Existing pre-sweep test updated to set
  `is_freeze_only_block.return_value = False` for HALT-only case.
- Full suite: 691 tests, all green.

### Server deploy + live verification

- Pulled `adda2ec` on server.
- Rebuilt `poly1:local` image.
- Recreated `scanner-executor` and `brain-indicator-cycle` containers.
- Manually verified via container REPL:
  ```
  reason(): "runtime control mode=freeze blocks live entries"
  is_freeze_only_block(): True   # (with freeze-paired HALT marker)
  reason(skip_runtime=True, skip_halt=True): None
  runtime_control_reason(): "runtime control mode=freeze blocks live entries"
  ```
- Post-deploy `brain_decisions` rejects (sample of recent ~500):
  - 36 `market_recent_reject_quarantine`
  - 10 `today_lesson_side_blocked` (learning guard) ← downstream gate
    firing, meaning decisions ARE flowing through risk_gate now
  - 1 `orderbook_not_executable`
  - **0 `risk_gate_blocked`** (was the dominant blocker pre-fix)

### Safety verification

- `trades` table: 0 new rows in last 15 minutes (post-deploy).
- `runtime_control.json`: mode still `"freeze"`.
- HALT file still present.
- Bot cash: $21.20 (unchanged).
- `polymarket.execute_market_order`: not called (only `_fillable_market_buy`,
  `get_usdc_balance` — both read-only).

## What this enables

Going forward, while the bot stays frozen:
- `scanner_executor` runs the full gate chain (council, score, EV,
  quality, learning guard, quarantine, regime router, etc.) on every
  scanner approval.
- Decisions that pass the chain are logged as SHADOW_ENTER in
  `decision_journal` with `outcome_5m_json` populated by the markouts
  pipeline (with `BRAIN_INDICATOR_MARKOUT_LIVE_FALLBACK=true` deployed
  earlier this session).
- `scripts/agent_edge_report.py` accumulates per-source edge stats
  daily — feeding the Week-1 decision on which agents have measurable
  edge.

## Expected accumulation rate

From the 7 days preceding 2026-05-22 freeze:
- 53 ENTER + 55 SHADOW_ENTER = 108 decision_journal entries
- ≈ 15 entries/day through scanner_executor's gate chain

So Week-1 should grow the sample from 108 to ~150 per agent-week.
The current alphainsider edge measurement (16 markouts, +6.94% / in-band
+21.44%) should reach 30-50 markouts in 5-7 days — enough to start
distinguishing real edge from noise.

## What this does NOT do

- Does NOT enable live trading.
- Does NOT change risk_gate behavior for non-freeze blocks.
- Does NOT route external_conviction_* agents through this path (they
  don't go through scanner_executor; their shadow logging is separate).
- Does NOT backfill SHADOW_ENTER markouts for the 4-day frozen gap.

## Open items

- `#43` closed in this session.
- Wait 1-3 hours for first post-fix SHADOW_ENTER to appear; then verify
  full chain (decision_journal write → markouts pipeline → agent_edge_report
  shows new entries).
- Backup rotation, brain bias investigation, exit-logic gap (markout edge
  → closed PnL gap), consensus router prerequisites — all remain on the
  Week-2/Week-3 roadmap.
