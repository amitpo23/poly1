# Session 2026-05-26 — Operator Handoff

End-to-end work log for May 26 session. Wallet migration, $11.21 redemption,
dashboard pro upgrade, agent audit, strategy tuning, sequenced testing.

## TL;DR for next agent

- **Wallet:** consolidated. $99.13 in `0x16577fEc...` (deposit_wallet).
  Was $7.04 + $83 in two wallets + 10 stuck winners ($11.21).
- **Dashboard:** professional terminal at `http://83.229.82.193:8090` —
  8 tabs, Plotly charts, gauges, live macro from yfinance, on-chain
  positions, Claude analyst.
- **Amit strategy code (v1 + v2):** TP=7%, SL=10%, max_hold dynamic
  (≥30s before resolution), Phase 2 momentum gate (YES≥0.55 to enter).
  v2 also has Layer 1 (spread/depth) + Layer 2 (5-level cascade FAK).
- **Liquidity reality:** Polymarket BTC 5-min markets show $0.20 of
  ask-side fillable today. Bot's MIN_MARKET_ORDER_USDC=$1 (v1) or
  $0.50 (v2) blocks most entries during thin periods. Three windows
  this session got fills (Gal A, R31, partial); two were blocked
  entirely (R30 morning, R30v2 noon, v2 13:55).
- **Bot state at write time:** Sub-round B (Amit v1, $5, 15min) is
  live, expires 14:30 UTC. After it expires the scheduled wakeup
  freezes and reports.

## 1. Wallet architecture — corrected

### What I found on entry

Three derived addresses from the bot's single EOA private key:

| Role | Address | Holds funds? |
|---|---|---|
| EOA (signer) | `0x14a2E262fCE33BbeF4cb507Df0caEE343412c55d` | No |
| legacy_proxy (Privy UI deposit target) | `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882` | $83 fresh deposit |
| deposit_wallet (CLOB v2 trading) | `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb` | $7.04 + 32 CTF positions |

Initial confusion: I first thought legacy_proxy was the new wallet and tried
to migrate the bot to use it. Reverted after running
`setup_deposit_wallet.py --dry-run` which clarified the roles:

- `legacy_proxy` = where Polymarket UI sends deposits (Privy-backed)
- `deposit_wallet` = the proper CLOB v2 trading address the bot uses

### What I did

1. Ran `scripts/python/setup_deposit_wallet.py` with `EXECUTE=true`. This
   used the Builder relayer to transfer $83.08 of pUSD from legacy_proxy
   to deposit_wallet (gasless). Also (re-)approved EXCHANGE, NEG_RISK,
   NEG_RISK_ADAPTER spenders.
2. Verified all bot containers see `get_usdc_balance() = $90.12`.
3. Updated `docs/WALLET_ARCHITECTURE.md` with the correct mental model.

### Recovering the 10 stuck winners ($11.21)

The deposit_wallet held 32 resolved CTF positions; 10 had positive payouts
totaling $11.21. Direct `CTF.redeemPositions()` from the deposit_wallet via
`relayer.execute_deposit_wallet_batch()` returned **PayoutRedemption events
with $0 payouts every time** — root cause appears to be Polymarket's CTF
using EC-point compression in `getCollectionId`, so the positionId I
computed off-chain didn't match what `redeemPositions` derives on-chain.

**Working flow (operator-suggested)**:

1. `scripts/python/move_ctf_to_proxy.py` — transfers all 10 winner CTF
   tokens from deposit_wallet → legacy_proxy via Builder relayer.
2. Operator logs into Polymarket UI as legacy_proxy and clicks the
   "You won $11.21" master Redeem button → UI converts CTFs to pUSD
   automatically.
3. Re-run `setup_deposit_wallet.py` to sweep redeemed pUSD legacy →
   deposit_wallet.

End state: $101.33 in deposit_wallet. (Now $99.13 after this session's
trades.)

## 2. Dashboard — professional terminal

Built `scripts/dashboard_server.py` (stdlib HTTP server, no Flask /
Streamlit) at port 8090. Three iterations through the day:

### Phase 1 — basic HTML dashboard
Initial build: agents table, recent trades, runtime mode, heartbeats.

### Phase 2 — Polymarket live integration
3 layers added via cached API endpoints:
- **A.** Live Polymarket markets (top 20 by 24h volume, Gamma API, 60s TTL)
- **B.** On-chain CTF positions (Data API, 30s TTL) — shows the true
  holdings as Polymarket sees them, decoupled from local journal
- **C.** Live order books per held token (CLOB API, 15s TTL per token)

### Phase 3 — Pro upgrade (master prompt principles)
Applied the "Claude Finance Terminal" doc principles:

- 8 tabs: Live / Markets / Agents / Portfolio / Order Books / Macro / News / AI Analyst
- Plotly.js charts (dark theme, tabular numerics)
- Plotly gauges (0–100, red/yellow/green bands) per agent
- Sparklines in 9 macro cards (BTC/ETH/SOL/^GSPC/^NDX/^VIX/^TNX/DXY/Gold)
- Live macro from Yahoo v8 chart endpoint (60s cache)
- News feeds for BTC/ETH/SPX (yfinance search, 5min cache)
- Claude analyst with neutral-language system prompt (no buy/sell)
- Compliance disclosure footer on every page

### Phase 4 — flicker fix
Original refresh did `tbody.innerHTML = ''` then appended rows one by
one → visible flash every 5s. Switched to atomic updates: build the
HTML string in a variable, assign innerHTML once. Also added:

- Pulsing green LIVE dot in header (turns red on >12s no refresh)
- Friendly empty-state messages ("no trades yet") instead of blank tables

### Phase 5 — dashboard `_attribute` coverage

Was missing 18 of the 24 agent prefixes. Added: btc_daily, trader,
trading_supervisor, news_shock, all 13 external_conviction variants,
opportunity_factory, resolution_sync, phantom_sweep, market_scanner,
brain_indicator. Also added regex-based UUID detection for
wallet_follow (which uses fresh UUIDs per cycle).

## 3. Calibration restart

brain-indicator-cycle was failing silently for ~24h because the disk
was 88% full (`OSError: [Errno 28] No space left on device`).

### What I did
- `docker builder prune -af` → reclaimed 1.17 GB
- Deleted `data/trade_log-before-dust-repair-20260520T074502Z.db` (67 MB)
- Compressed three large `external_convictions_*.jsonl` files (gzip -9)
  — **caveat:** if EC agents reopen these files for read they may
  break. Untested. Worth flagging next session if EC issues appear.
- Force-recreated `poly1-brain-indicator-cycle`

### Current calibration state (post-restart)
- `data/probability_calibration.json` updates every cycle (~5min)
- 199 total closes, 93 matched markouts
- Sections populated: per_action, per_signal_source (4 sources),
  per_direct_execution_agent (3 agents), per_source_band (10),
  per_source_band_action (12), shadow_research_visibility (7)
- **Edge highlights**:
  - `opportunity_factory,alphainsider_proven,crypto_tape | 0.4 band` —
    **n=18, WR 50%, EV +$0.058** (strongest single cell)
  - `BUY` action: n=58, WR 31%, EV +$0.016
  - `SELL` action: n=35, WR 14%, EV -$0.028
  - `btc_5min`: n=46, WR 34.8%, **EV -$0.045** (TP/SL needs retune)
  - `scanner_executor`: n=121, WR 19.8%, EV -$0.013

### Known unresolved
- `shadow_research_visibility` shows all 7 EC variants with
  `n=0 markouts=0`. The `update_shadow_markouts.py` script reports
  `missing_snapshot=452` — orderbook snapshots aren't captured at
  decision time, so we can't measure EC edge from shadow data. EC
  evaluation requires real trades.

## 4. Per-agent audit — 24 agents

See `docs/AGENT_AUDIT_2026_05_26.md` for the full table. Tier summary:

- **Tier A** (3 agents, ready): scanner_executor, btc5min_timed (v1),
  btc5min_timed_v2 (v2)
- **Tier B** (1 agent, retune needed): btc_5min — EV -$0.045
- **Tier C** (6 agents, untested): btc_daily, scalper, near_resolution,
  news_shock, wallet_follow, opportunity_factory, trader
- **Tier D** (13 EC variants, signal-only): all produce brain_decisions,
  none execute, shadow markouts broken

Position size unification: all per-agent `*_POSITION_SIZE_USDC` env
vars set to `1.00` in `.env`. This was preserved across this session.

## 5. Strategy tuning (operator decisions today)

| Change | Was | Now | Reason |
|---|---|---|---|
| TP both phases | 5% | **7%** | Better risk/reward, operator decision |
| SL both phases | 20% | **10%** | Tighter, operator decision |
| max_hold (static) | 120s | **dynamic** | Phase 2 was closing AT resolution |
| Phase 2 entry rule | always BUY UP | **only if YES≥0.55** | Phase 2 WR=19% — operator gated |
| v2 position size | $1.00 | **$0.50** | Operator-requested |
| v2 MIN_MARKET_ORDER_USDC | $1.00 (global) | **$0.50 (per-container)** | Required to allow $0.50 trades |

### max_hold dynamic — bug-fix detail (commit `cbcdcde`)

```python
safe_close_deadline = period_ts + 270   # 30 sec before resolution
seconds_to_safe_close = safe_close_deadline - time.time()
max_hold = max(15, min(120, int(seconds_to_safe_close)))
```

- Phase 1 entry at t=1s: max_hold caps at 120 (well within window).
- Phase 2 entry at t=180s: max_hold caps at ~90s (closes at t=270s).

Previously Phase 2 + static max_hold=120 → close at t=300s = exactly when
the market resolves. That's why R31's position #4358 ended as a $1 loss
to resolution rather than as TP/SL on the orderbook.

### Phase 2 momentum gate (commit `949360f`)

```python
if phase == "phase2":
    if live_price < self.cfg.phase2_min_momentum_price:
        # skip + log + mark phase2_fired
        return False
```

Tunable per agent:
- `BTC5MIN_TIMED_PHASE2_MIN_MOMENTUM_PRICE` (default 0.55)
- `BTC5MIN_TIMED_V2_PHASE2_MIN_MOMENTUM_PRICE` (default 0.55)

### v2 per-container MIN_ORDER_USDC (commit `0d9f96f`)

In `docker-compose.yml` `btc5min_timed_v2:`:
```yaml
environment:
  POLYMARKET_MIN_ORDER_USDC: "0.50"
```
This overrides the global $1 minimum for v2 only. Other agents keep $1.

## 6. Live trading runs this session

| Round | Time UTC | Agent | Budget | Fires | Result |
|---|---|---|---|---|---|
| R30 (morning) | 09:36 → 09:51 | v2 only | $5 | 0 | All 8 attempts blocked by `fillable<min` |
| Gal A | 11:46 → 12:46 | v1+v2+scanner_executor+btc_5min | $5 | 3 (v1 only) | 2 TP + 1 SL = -$0.76 |
| R30v2 (noon) | 12:35 → 12:48 | v2 only | $5 | 0 | 1× Layer 1 spread hit + 7× fillable blocks |
| R31 | 12:55 → 13:10 | v1 only | $5 | 5 | 2 TP + 2 SL + 1 stuck = -$1.44 |
| Sub A | 13:55 → 14:10 | v2 only | $5 | **0** | All thin-liquidity skips |
| Sub B | 14:15 → 14:30 | v1 only | $5 | (in progress) | |

## 7. The stuck position (R31 #4358)

Phase 2 BUY UP @ 0.22 at 13:03:02. Market closed at 13:05:00. The bot's
old static max_hold=120s would have force-closed at 13:05:02 — exactly
when the market resolves — so the PM never got a chance to file a sell
order. UP didn't win, position resolved as -$1.00 loss.

This is fixed for future trades via the dynamic max_hold (closes at
t=270s, i.e. 30 sec before resolution). It also wouldn't have entered
under the new Phase 2 momentum gate (0.22 << 0.55).

The position itself: still in DB as `btc5min_timed_open` because
resolution_sync hasn't yet marked it `resolved_loss`. Wallet is correct
($99.13 reflects the $1 loss already).

## 8. Liquidity observation

5-min crypto markets on Polymarket showed wildly variable depth today:

- **07:46 ET (Gal A)** — $1+ fillable on multiple cycles → 3 trades fired
- **08:55 ET (R31)** — $1+ fillable → 5 trades fired
- **09:36, 12:35, 13:55, 14:15 ET windows** — only $0.20 fillable → blocks

Best guess: market makers pull liquidity during certain micro-windows.
Operator may want to time live tests around earlier ET windows for now.

## 9. Files changed this session (commits in `main`)

- `0d9f96f` — feat(amit-v2): per-container POLYMARKET_MIN_ORDER_USDC=0.50
- `949360f` — feat(amit): Phase 2 momentum gate — skip BUY UP if YES < 0.55
- `cbcdcde` — fix(amit): dynamic max_hold to close ≥30s before market resolves
- `148ce00` — fix(dashboard): atomic DOM updates — no more flicker
- `2d78210` — fix(dashboard): expand _attribute prefix map to cover all 24 agents
- `c661b60` — feat(redeem): working consolidation flow via legacy_proxy + UI
- `bce2715` — fix(wallet): correct migration — sweep legacy_proxy → deposit_wallet
- `c7492af` — docs(wallet): document old-proxy recovery options
- `c133661` — feat(wallet): migrate to new Polymarket proxy (initial, later corrected)
- `1607143` — feat(redeem): script to claim resolved-winner CTF positions
- `f15de2b` — fix(dashboard): point swarm volume to /home/trader/swarm/data
- `7f7e4a3` — feat: real-time live dashboard at port 8090

Plus deployed `scripts/python/move_ctf_to_proxy.py`,
`scripts/python/redeem_via_proxy.py`, `scripts/python/redeem_winnings.py`,
`scripts/dashboard_server.py`, and updates to `docker-compose.yml`,
`docs/WALLET_ARCHITECTURE.md`, `docs/AGENT_AUDIT_2026_05_26.md`.

## 10. Open questions for next operator session

1. **Phase 2 momentum gate threshold**: 0.55 is a guess. Should validate
   with backtest of historical Phase 2 trades by entry-price bucket.
2. **EC shadow markouts**: the `missing_snapshot=452` bug needs the
   orderbook_monitor to capture snapshots at decision time. Currently
   broken — no edge measurable for 13 EC variants.
3. **btc_5min TP/SL retune**: known EV -$0.045 with TP=5%. Backtest
   needed to find the right TP/SL/RR combination.
4. **R31 stuck position (#4358)**: should be auto-marked `resolved_loss`
   by resolution_sync within a couple of cycles. If still `_open` next
   session, run `python3 scripts/sweep_stale_phantom_open.py` (or
   resolution_sync manually).
5. **Compressed EC JSONL files**: gzipped three large files for disk
   space — verify EC agents don't need to read them. If they do, need
   to either decompress or rotate-properly.
6. **Long-term Phase 2 evaluation**: with the momentum gate now in
   place, need a much larger sample (n=50+) of Phase 2 entries to know
   if 0.55 threshold is good or needs adjustment.

## 11. Quick commands cheat sheet

```bash
# Verify bot state
ssh trader@83.229.82.193 'cat /srv/poly1/data/runtime_control.json | head -20'

# Freeze
ssh trader@83.229.82.193 'cd /srv/poly1 && python3 scripts/runtime_control.py freeze'

# Arm a specific agent
ssh trader@83.229.82.193 'cd /srv/poly1 && python3 scripts/runtime_control.py live-hour \
  --agents <name> --budget 5 --wallet-balance <X> --minutes 15 --arm'

# Inspect recent trades
ssh trader@83.229.82.193 'sqlite3 -header -column /srv/poly1/data/trade_log.db \
  "SELECT id, ts, cycle_id, side, status FROM trades ORDER BY id DESC LIMIT 20"'

# Wallet check
ssh trader@83.229.82.193 'docker exec poly1-live-dashboard python3 -c \
  "from agents.polymarket.polymarket import Polymarket; \
   p=Polymarket(live=True); print(p.get_usdc_balance())"'

# Restart specific agent container
ssh trader@83.229.82.193 'cd /srv/poly1 && docker compose up -d --force-recreate <service>'

# Live dashboard
# Browser → http://83.229.82.193:8090
```
