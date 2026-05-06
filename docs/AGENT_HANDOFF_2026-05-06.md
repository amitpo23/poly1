# Agent Handoff: Unified-Wallet Migration

Date: 2026-05-06
Author: documenting end-of-session state for the next agent (or future me).

This handoff covers the full unification of poly1 + swarm onto a single
deposit wallet, completed in this session. If you're picking up cold,
start here and follow the cross-references.

---

## Executive Summary

**What changed:** the swarm bot at `~/Desktop/poly/bot` was migrated from
its old (unused) EOA-direct path to share the SAME deposit wallet that
poly1 uses (`0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`). Both bots now
draw from the same pUSD pool. Capital allocation is centralized via
poly1's `RiskGate.reserves` dict.

**Why:** the user's mental model is "one wallet, many strategies." The
prior architecture had the swarm wallet (the EOA) holding $0 while the
deposit wallet held all the pUSD — swarm couldn't trade. The user
explicitly directed: do not send any new funds; migrate code instead.

**Result:** swarm is **live** trading on the shared wallet. All 5 agents
registered. No fills yet (agents waiting for entry triggers). poly1
unaffected — its own slice + drawdown calculation are independent.

---

## State at Handoff

### Containers

```
poly1                Up 30+ min  healthy   live   journal-based drawdown
poly1-scalper        Up 4+ hours healthy   shadow $2.50/leg (Stage 0)
polymarket-swarm     Up 15+ min  healthy   LIVE   sig type 3, $40
poly1-dashboard-1    Up          healthy   Streamlit at port 8050
poly1-grafana        Up          healthy   Grafana at port 3000
```

### Dashboard visibility update (money per swarm agent)

The Streamlit Swarm tab now has a dedicated per-agent money summary with:

- allocation (USD) from `SWARM_AGENT_ALLOCATIONS_JSON`
- executed notional from reconciled `fills` (`price_cents/100 * size`)
- remaining allocation and utilization %
- ledger counts: submitted / filled-brake / failed / cleared

This is the quickest source to answer "who traded how much" during live ops.

### Capital ledger (single source of truth)

| Slice | Env var | $ | Notes |
|---|---|---|---|
| poly1 main | `STARTING_BALANCE_USDC` | 40 | journal-based drawdown |
| swarm | `SWARM_RESERVE_USDC` (poly1) + `TOTAL_CAPITAL` (swarm) | 20 | corrected after live positions reduced cash; four funded agents get $5 each |
| scalper | `SCALPER_RESERVE_USDC` | 0 | shadow only; reserve until Stage 1 flip |
| **Total budgeted** | | **60** | poly1 main $40 + swarm $20 |
| Actual on-chain pUSD | (live cash) | ~$13.69 | rest is deployed in live positions/orders |

### Swarm internal allocation (within the $40)

| Agent | % | $ | Status |
|---|---|---|---|
| mean_reversion | 25% | $5 | live, waiting BTC > 1% in 3 min |
| market_maker | 25% | $5 | live; two recent CLOB orders matched and are reconciled as local `filled` brake rows |
| nothing_happens | 25% | $5 | live, scanning Gamma |
| ai_decision | 25% | $5 | live but skips: `no ANTHROPIC_API_KEY set` |
| arbitrage | 0% | $0 | observational only (capital_allocation hardcoded to 0 in `main.py:141`) |

### Approvals (V2, on-chain, UNLIMITED)

All set on the deposit wallet via builder relayer (gasless):
- pUSD → CTF Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`)
- pUSD → NegRisk CTF Exchange (`0xe2222d279d744050d28e00520010520000310F59`)
- pUSD → NegRisk Adapter (`0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`)
- CTF.setApprovalForAll on both V2 exchanges from deposit wallet

Builder relayer tx that set the missing approvals:
`0x363e85beba2b058e8c466853ea1c28983c8974047571cc5dc8c4f34b61181c16`

---

## What to Read First (in order)

1. **`docs/MIGRATION_LOG_2026-05-06.md`** — full execution log, every file
   touched, every error hit, every test count, every tx hash. The
   authoritative source for "what happened."
2. **`~/.claude/plans/vectorized-dancing-cupcake.md`** — the approved plan
   that the migration log executed against.
3. **`docs/POLY1_EXIT_LOGIC_GAP.md`** — known gap: poly1 main has no
   close logic. Positions held until market resolution. User wants this
   fixed eventually but explicitly NOT today.
4. **`docs/RUNBOOK_2026-05-07.md`** — was written before the migration;
   parts are now superseded (e.g., the swarm Stage-1 flip path) but the
   morning health-check commands are still valid.

---

## Verification commands (read-only, run any time)

### 1. Container health

```bash
/Applications/Docker.app/Contents/Resources/bin/docker ps --format \
  'table {{.Names}}\t{{.Status}}'
```

Expected: all 5 containers `healthy`.

### 2. Both bots authenticated against the same deposit wallet

```bash
# poly1
docker compose run --rm trader python -c "import json; from \
  agents.polymarket.polymarket import Polymarket; p=Polymarket(live=True); \
  print(json.dumps({'balance': p.get_usdc_balance()}, default=str))"

# swarm
docker logs polymarket-swarm 2>&1 | grep "signature_type=3" | tail -1
```

The boot log line should read:
```
CLOB client connected: signature_type=3, funder=0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb
```

### 3. On-chain approvals state

```bash
cd ~/Desktop/poly/bot && docker compose run --rm swarm python -c "
from web3 import Web3
w3 = Web3(Web3.HTTPProvider('https://polygon-bor-rpc.publicnode.com'))
deposit = '0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb'
pusd = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
abi = [{'constant':True,'inputs':[{'name':'o','type':'address'},{'name':'s','type':'address'}],
        'name':'allowance','outputs':[{'name':'','type':'uint256'}],'type':'function'}]
c = w3.eth.contract(address=Web3.to_checksum_address(pusd), abi=abi)
for name, sp in [('CTF V2','0xE111180000d2663C0091e4f400237545B87B996B'),
                 ('NegRisk V2','0xe2222d279d744050d28e00520010520000310F59'),
                 ('NegRisk Adapter','0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296')]:
    a = c.functions.allowance(deposit, Web3.to_checksum_address(sp)).call()
    print(f'  {name}: {\"UNLIMITED\" if a > 2**128 else a/1e6}')
"
```

Expected: all three UNLIMITED.

### 4. Swarm DB freshness

```bash
sqlite3 ~/Desktop/poly/bot/data/swarm.db "PRAGMA integrity_check;"
sqlite3 ~/Desktop/poly/bot/data/swarm.db \
  "SELECT agent, COUNT(*), SUM(size) FROM fills GROUP BY agent"
```

Expected: integrity ok. Fills count by agent.

### 5. Swarm live activity

```bash
docker logs --since 1h polymarket-swarm 2>&1 | \
  grep -iE "MR entry|MM entry|NH entry|fill|FAILED" | tail -20
```

If zero output for >2h after the BTC has had a >1% move, mean_reversion
isn't firing for some reason — investigate.

### 6. Tests still green

```bash
# poly1
docker compose run --rm trader python -m unittest discover -s tests
# expected: Ran 89 tests in ... OK

# swarm
cd ~/Desktop/poly/bot
docker compose run --rm \
  -v "$(pwd)/tests:/app/tests:ro" \
  -v "$(pwd)/conftest.py:/app/conftest.py:ro" \
  -v "$(pwd)/pytest.ini:/app/pytest.ini:ro" \
  swarm pytest tests/ -q -m "not live"
# expected: 173 passed, 2 deselected
```

---

## Key Code Locations Touched This Session

### Poly1 (`~/coding/poly1`)

- `agents/application/risk_gate.py` — refactored to `reserves` dict;
  added journal-based `portfolio_value_usdc()`. `scalper_reserve` is
  now a `@property` aliasing `self.reserves["scalper"]` for backward
  compat with existing tests/callers.
- `agents/application/trade_log.py` — `filled_positions()` helper added
  earlier today (morning session).
- `tests/test_trader.py` — 3 RiskGate tests adjusted for journal-based
  drawdown.
- `grafana/dashboards/poly1.json` — added 4 panels under "Unified
  Capital Tracking" row at y=70-79.
- `.env` — `STARTING_BALANCE_USDC=40`; `SWARM_RESERVE_USDC` was
  corrected to `20` in the follow-up review after swarm opened live
  positions and wallet cash no longer supported a $40 free-cash budget.

### Swarm (`~/Desktop/poly/bot`)

- `requirements.txt` — added `py_clob_client_v2==1.0.1rc1`,
  `py-builder-relayer-client==0.0.2rc1`, pinned `eth-typing==4.4.0` and
  related to avoid web3-pytest plugin breakage.
- `config.py` — added `polymarket_deposit_wallet` and
  `polymarket_signature_type` fields. Lowered `total_capital` floor
  validator from 50 to 30. Updated agent allocations (MM 0.30, MR 0.35,
  NH 0.15, AI 0.15). Enabled all 5 agents by default. Lowered MR
  `position_size_usd` 30 → 5. Lowered NH `max_concurrent_positions` 2 → 1.
  Enabled `AIAdvisorConfig.enabled` by default.
- `core/client.py` — added `deposit_wallet` and `signature_type` ctor
  params, signature-type resolution branch in `connect()` (mirrors
  poly1's `agents/polymarket/polymarket.py:85-93`), allowance guard
  extension. `get_orderbook` rewritten to handle V2 SDK's dict response
  shape and to resolve condition_id → YES token_id internally.
  `cancel_all_orders` rewritten for V2's `OrderMarketCancelParams`
  signature.
- `main.py` — `PolymarketClient` constructor now passes the new params.
- `tests/test_config_validation.py` — one test updated for new defaults.
- `.env` — `POLYMARKET_DEPOSIT_WALLET`, `POLYMARKET_SIGNATURE_TYPE=3`,
  builder/relayer credentials copied verbatim from poly1's `.env` (same
  EOA, same builder identity). `BOT_MODE=live`, `TOTAL_CAPITAL=20`.

---

## Critical Invariants (don't break these)

1. **One private key, one EOA.** Both `.env` files use the same
   `PRIVATE_KEY` / `POLYGON_WALLET_PRIVATE_KEY`. Don't reuse for any
   other bot or wallet.

2. **The deposit wallet `0x16577fEc7...` is the trading entity.** The
   EOA `0x14a2E2...` is just the owner — it has $0 and that's correct.
   Don't try to fund the EOA "to fix swarm." Don't move funds between
   addresses without explicit user authorization.

3. **`RiskGate.portfolio_value_usdc()` is journal-based, not cash-based.**
   This is essential for the shared-wallet model. Reverting to
   `cash + mtm` will halt poly1 every time swarm spends pUSD. Test
   coverage: `tests/test_trader.py::TestRiskGate::test_drawdown_*`.

4. **Reserves dict is the single source of truth for capital.**
   `RiskGate.reserves = {"scalper": ..., "swarm": ..., ...}`. Adding a
   new bot/strategy means: one env var + one entry in this dict. Don't
   create parallel allocation mechanisms.

5. **Builder relayer pays gas. Don't ask the operator for MATIC.**
   Approvals, deposit-wallet redeploys, pUSD transfers within Polymarket
   — all gasless from the EOA's POV via `setup_deposit_wallet.py`.

6. **V2 SDK semantics differ from V1.** Notable:
   - `get_order_book(token_id)` — positional; returns dict not object.
   - `cancel_market_orders(OrderMarketCancelParams(...))` — payload object.
   - `create_or_derive_api_key()` — try-create then fallback-derive;
     a 400 "Could not create api key" on boot is expected when the EOA
     already has keys (non-fatal).

---

## Outstanding Work (deferred)

- **Swarm fills exist** — two market-maker CLOB `MATCHED` rows are now
  recorded in `~/Desktop/poly/bot/data/swarm.db` as local fills.
- **404 noise** — `py_clob_client_v2` logs spurious 404s for token IDs
  the resolver doesn't have cached. Source unclear (likely arbitrage
  or NH probing). Doesn't block the configured markets. Investigation
  deferred.
- **`ANTHROPIC_API_KEY` not set in swarm `.env`** — ai_decision agent
  skips every cycle with `reason=no ANTHROPIC_API_KEY set`. If user
  wants Claude-driven trades, set this.
- **poly1 exit logic** — `maintain_positions` is still a `pass`. See
  `docs/POLY1_EXIT_LOGIC_GAP.md`. User said: document, don't build now.
- **Cleanup** — `py-clob-client 0.13` still in swarm `requirements.txt`
  because `scripts/init_approvals.py` references it. Remove after that
  script is rewritten or removed (not needed anymore — `setup_deposit_wallet.py`
  in poly1 covers approvals via relayer).

---

## How to Halt (if something goes wrong)

```bash
# Halt poly1 (RiskGate reads this file)
touch ~/coding/poly1/data/HALT

# OR via the loopback control surface:
curl -X POST http://localhost:8765/control/poly1/halt

# Halt swarm: stop the container (graceful drain via SIGTERM)
cd ~/Desktop/poly/bot
docker compose stop swarm
```

To resume: `rm ~/coding/poly1/data/HALT` for poly1, `docker compose up -d swarm`
for swarm.

If both bots show >5% drawdown in the same day → halt both. Each bot's
RiskGate handles its own slice; cross-bot halt is manual policy.

---

## Follow-up Review Completion (2026-05-06 ~12:00 UTC)

This section completes the A/B review items that were left unfinished by
the prior agents.

**Risk fixes landed:**
- `agents/application/risk_gate.py`: SELL MTM now uses the actual logged
  token entry price. In this codebase a SELL recommendation is implemented
  by buying the opposite CLOB token, so applying `1.0 - price` again was
  wrong.
- `agents/application/risk_gate.py`: `scalper_reserve` now has a
  backwards-compatible setter that writes through to `reserves["scalper"]`.
- `~/Desktop/poly/bot/core/client.py`: `place_limit_order()` now accepts
  V2 response ID shapes (`orderID`, `orderId`, `order_id`, `id`), returns
  `None` on rejection without an ID, normalizes side/outcome, and rejects
  invalid price/size before signing.
- `~/Desktop/poly/bot/core/client.py`: order-book 404s now log
  `market_id` and `token_id` context.
- `~/Desktop/poly/bot/agents/arbitrage_agent.py`: the arbitrage stub no
  longer polls placeholder `market-a` / `market-b` live. It stays
  registered but skips until real market IDs are configured.
- `~/Desktop/poly/bot/agents/market_maker_agent.py`: market maker now
  skips a market if `pending_orders` already has `pending` or `submitted`
  rows for that market, preventing duplicate live quotes after restart.
- `~/Desktop/poly/bot/core/client.py`: deposit-wallet restarts now warn,
  not crash, when cash is below `TOTAL_CAPITAL`; lower cash can mean funds
  are already deployed in matched positions.

**Capital correction:** the original `$40` swarm cash budget no longer fit
the wallet after live positions were opened. Swarm is now configured with
`TOTAL_CAPITAL=20`, and the four funded agents are equalized to 25% each:
market_maker `$5`, mean_reversion `$5`, nothing_happens `$5`,
ai_decision `$5`. Arbitrage remains observational at `$0`.
`SWARM_RESERVE_USDC` in poly1 `.env` was reduced to `$20` to match.

**Live reconciliation:** after the first real swarm activity, CLOB showed
no open orders. The DB had three recent market-maker submitted rows:
two CLOB orders were `MATCHED`, one was `CANCELED`. The reconciliation
script moved the matched rows to local `filled` rows and recorded two
fills; the canceled row was moved to `cleared`. Stale `dry_*` submitted
rows were also cleared.

**Current swarm state after restart:** `polymarket-swarm` is healthy,
`mode=live`, `signature_type=3`, deposit wallet
`0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`, cash `$13.69`, capital
`$20.0`. Boot logs show all expected agents registered; AI still skips
because `ANTHROPIC_API_KEY` is not set.

**Tests run:**
- poly1: `python3 -m unittest tests.test_trader.TestRiskGate tests.test_trader.TestTradeLog -v` → 18 passed.
- swarm: Docker `pytest tests/test_client.py tests/test_config_validation.py tests/test_arbitrage_agent.py tests/test_market_maker_agent.py -q` → 34 passed.

**Still open:** swarm runtime risk-state recovery from SQLite is not
implemented. The runtime status panel can still show `Open positions: 0`;
use the DB/dashboard ledger for reconciled swarm fills until recovery is
added.

**Dashboard update after this review:** Streamlit now has a Swarm tab
that reads the mounted swarm DB (`/swarm/data/swarm.db` inside the
dashboard container) and shows the submitted-order brake, live CLOB rows
needing reconciliation, recent local fills, and NothingHappens journal.
Grafana's capital ledger is corrected to poly1 `$40` + swarm `$20` +
scalper `$0` = `$60`, and it has a reconciliation-needed table for the
live CLOB order IDs; it is currently empty after reconciliation.
`monitor_web.py` also exposes/renders these same unreconciled live rows
while filtering old `dry_*` IDs. Current `submitted_unreconciled_count=0`.

---

## Where Things Live (cross-reference)

- Plan file: `~/.claude/plans/vectorized-dancing-cupcake.md`
- Migration log: `~/coding/poly1/docs/MIGRATION_LOG_2026-05-06.md`
- This handoff: `~/coding/poly1/docs/AGENT_HANDOFF_2026-05-06.md`
- Current status (top-level pointer): `~/coding/poly1/deploy/CURRENT_STATUS.md`
- Plan history: `~/coding/poly1/docs/AGENT_HANDOFF_2026-05-04.md`,
  `~/coding/poly1/docs/AGENT_HANDOFF_2026-05-05_DASHBOARD_SWARM.md`
- Joint operations: `~/Desktop/poly/OPERATIONS.md`
- Pre-launch decisions: `~/Desktop/poly/HANDOFF.md`
- Poly1 code invariants: `~/coding/poly1/CLAUDE.md`
- Swarm code invariants: `~/Desktop/poly/bot/CLAUDE.md` (now partly
  outdated — the "two wallets" guidance no longer applies)
