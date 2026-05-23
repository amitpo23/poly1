# SESSION 2026-05-23 — Morning Live-Test Plan

**Written by:** Claude Code, late night 2026-05-23
**For:** the next morning's live test
**Status:** server is in freeze; all changes from tonight are deployed.

---

## What changed tonight that affects tomorrow

| Commit | Effect on next live |
|---|---|
| `85cac28` | scanner_executor learning guard is now ACTIVE in BASE_ENV. MAX_ENTRY_PRICE=0.49, PREFERRED_SIDE=BUY. **The biggest historical loss zone (owned-token 0.50-0.60) is now blocked on scanner_executor path.** |
| `adbb108` | near_resolution OpenAI SystemMessage fix. 51 prior failures/day → expected 0. |
| `31d1297` | opportunity_factory wallet path no longer self-labels calibrated=True. Scanner now requires score≥0.80 (not 0.54) for wallet candidates. |
| `54644e4` | Scanner_executor pre-sweep RiskGate.ok(). HALT/balance/budget triggers cycle-skip immediately. |
| `c93d713` | DB backups run every 4h (preflight passes). |
| `0ee4840` + `453a80a` | Loss analysis docs (with correction). |

---

## Corrected pre-test reality check

The bot has been **net negative −$1.92 / −1.41% ROI over 30 days** (not
+$6.54 as a buggy formula reported). See
`docs/SESSION_2026-05-23_DEEPER_DRILL.md` "CORRECTION" section.

The one profitable band historically: **owned-token 0.40-0.50** (+$1.72,
+8.40% ROI, 42% win rate over 19 trades). The learning guard now confines
scanner_executor entries to that band.

**Hypothesis to test tomorrow:** if we run scanner_executor only with
the learning guard active, the bot's PnL should improve vs the historical
−$1.92 baseline. If the historical losses came from entries OUTSIDE the
0.40-0.50 band, blocking them should help.

---

## Morning checklist (operator)

### 1. Pre-test verification

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  git log -1 --oneline && \
  python3 scripts/runtime_control.py status && \
  ls -la data/HALT && \
  python3 scripts/trading_stability_preflight.py --mode freeze 2>&1 | tail -5'
```

Expected:
- HEAD: `873db41` (or later if more docs pushed)
- mode=freeze
- HALT present
- preflight green (all 15 OK)

### 2. Live-probe arming (ONLY scanner_executor)

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  python3 scripts/runtime_control.py live-hour \
    --budget 5 \
    --wallet-balance 24.34 \
    --equity-balance 24.34 \
    --minutes 15 \
    --max-open 4 \
    --agents scanner_executor \
    --position-size-usdc 1.00 \
    --max-daily-token-usd 1.0 \
    --scanner-allow-wait \
    --scanner-wait-min-score 0.79 \
    --arm \
    --note "post-tier-0bc-test-with-learning-guard"'
```

Key params:
- $5 budget total
- 15-minute window
- max 4 open positions
- $1 per trade
- ONLY scanner_executor enabled (NOT btc_5min, near_resolution, news_shock,
  wallet_follow, external_conviction). These paths may lack the 0.49 cap
  per F1' carry-over.

### 3. Bring up services

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  docker compose --profile scanner --profile positions --profile supervisor \
    --profile settlement --profile monitoring up -d \
    scanner-executor market_scanner trader position_manager \
    trading-supervisor settlement-reconciler telegram-reporter'
```

### 4. Monitor (every 5 min during the probe)

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  python3 -c "
import sqlite3
conn = sqlite3.connect(\"data/trade_log.db\")
print(\"trades 15min:\", conn.execute(
    \"SELECT status, COUNT(*) FROM trades WHERE ts > datetime(\\\"now\\\",\\\"-15 minutes\\\") GROUP BY status\"
).fetchall())
print(\"reject reasons:\", conn.execute(
    \"SELECT reason, COUNT(*) FROM brain_decisions WHERE approved=0 AND ts > datetime(\\\"now\\\",\\\"-15 minutes\\\") GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 5\"
).fetchall())
"'
```

Watch for:
- `today_lesson_side_blocked` counts (learning guard side-block firing)
- `today_lesson_price_band_blocked` counts (learning guard band-block firing)
- `probability_not_calibrated` rejects (calibrated bypass closed)
- Actual `filled` trades — should be in 0.40-0.50 owned-token band

### 5. After 15 minutes

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  python3 -c "
import sqlite3
conn = sqlite3.connect(\"data/trade_log.db\")
print(conn.execute(\"SELECT status, COUNT(*) FROM trades WHERE ts > datetime(\\\"now\\\",\\\"-30 minutes\\\") GROUP BY status\").fetchall())
"'
```

Expected outcomes:
- 0-5 filled trades (small window, narrow filters)
- 0 trades with entry above 0.49 (learning guard working)
- All filled trades on BUY side (learning_preferred_side=BUY)

### 6. Hard stop conditions

Halt the test immediately if ANY of these occur:
- More than 2 SL closes within first 5 minutes
- Any failed close (close_failed status)
- Any supervisor_halt event
- `near_resolution` 400 errors recur (OpenAI fix not deployed correctly)

To halt:
```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  python3 scripts/runtime_control.py freeze --note "manual halt during test"'
```

---

## Open questions for next session (post-test)

1. **Did learning guard fire?** Count `today_lesson_*_blocked` rejects. If
   zero firings, the guard wasn't actively gating — check why.
2. **Per-band PnL on test trades?** Did 0.40-0.50 band still produce
   positive expectancy?
3. **Verify F1' coverage:** which agent paths skipped the 0.49 cap? Is
   the learning guard the only protection or do others have separate caps?
4. **Trailing stop firing?** Did MAINTAIN_TRAILING_STOP_PCT=0.02 capture
   any peaks?
5. **OpenAI failures rate?** Should be 0 with `adbb108`.

---

## What NOT to do in tomorrow's test

- Don't enable multiple agents (btc_5min, etc.) — only scanner_executor.
- Don't raise position size beyond $1.
- Don't extend the window beyond 15 min on first test.
- Don't change SL or TP parameters (data shows they're correct).
- Don't change `MAX_ENTRY_PRICE` even if no trades execute (the protection
  is what we're testing).

---

## What to write in tomorrow's session journal

After the test, capture:
- Number of trades attempted vs filled
- Per-trade entry band (owned-token), side, signal_source
- PnL via response_json.pnl_usdc_real (NEVER use exit/entry-1)
- Reject-reason distribution
- Total PnL vs cost
- Any unexpected behavior (failures, deferred exits, halts)

Per Rule 4: write `docs/SESSION_2026-05-24_MORNING_TEST.md`.

Good luck.
