# Current Status

Date: 2026-05-06

Latest dashboard/swarm handoff:

- `docs/AGENT_HANDOFF_2026-05-05_DASHBOARD_SWARM.md`

Latest research/review handoff:

- `docs/POLYAGENT_REVIEW_2026-05-06.md` — PolyAgent read-only review;
  added `news_signals` table + dry-run news classification module
  (no live wiring to executor or risk gate).

Latest unified-wallet migration:

- `docs/AGENT_HANDOFF_2026-05-06.md` — handoff for next agent: state,
  invariants, verification commands, outstanding work. **Read first.**
- `docs/MIGRATION_LOG_2026-05-06.md` — full execution log of swarm → V2
  + signature_type=3 + shared deposit wallet migration. swarm now
  live trading on the same wallet as poly1. Follow-up review on
  2026-05-06 corrected the live swarm budget to `$20`: four funded
  agents at `$5` each; arbitrage observational at `$0`.

Latest A/B review completion:

- `docs/AGENT_HANDOFF_2026-05-06.md` and
  `docs/MIGRATION_LOG_2026-05-06.md` now include the follow-up review
  completion section: SELL MTM fix, reserve setter, V2 order response
  hardening, arbitrage 404 fix, market-maker duplicate-order guard, and
  the final live swarm restart.

Tomorrow's runbook:

- `docs/RUNBOOK_2026-05-07.md` — morning health check, swarm live
  activation steps (operator-only), scalper Stage 1 criteria, halt
  rules. Written 2026-05-06 after MTM-aware risk gate deployed.

Known gaps:

- `docs/POLY1_EXIT_LOGIC_GAP.md` — poly1 main trader has no exit
  logic; positions held until market resolution. Dashboard does not
  show per-position P/L. User-flagged for future work, not being
  built today.
- Swarm local DB reconciliation is still manual: recent market-maker
  submitted rows include two matched CLOB orders and one canceled CLOB
  order. They intentionally remain blocking to prevent duplicate quotes
  until an order-status sweep is implemented. The swarm runtime risk
  summary currently reports `Open positions: 0`; that is not reliable
  until reconciliation is automated.

## Summary

poly1 and the sister swarm bot now share the same Polymarket CLOB v2
deposit wallet. poly1 uses journal-based drawdown accounting so swarm
spend does not count as poly1 loss.

The swarm bot at `~/Desktop/poly/bot` is live under Docker Compose with
`TOTAL_CAPITAL=20`: market_maker, mean_reversion, nothing_happens, and
ai_decision each have `$5`; arbitrage is registered but observational
with `$0`. During the follow-up review, market_maker placed real CLOB
orders: two matched and one canceled. There are no open CLOB orders, and
the local submitted rows remain blocking to prevent duplicate MM quotes
until reconciliation is automated.

Full handoff for future agents:

- `docs/AGENT_HANDOFF_2026-05-04.md`

Release checkpoint:

- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`

Overnight and multi-week operating plan:

- `docs/OVERNIGHT_AND_MULTI_WEEK_OPERATION.md`

Trading log and journal:

- `docs/TRADING_LOG_AND_JOURNAL.md`

## Verified

- Docker image builds.
- Tests pass: `25 passed`.
- CLOB L2 auth works.
- Legacy proxy was deployed/funded:
  `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882`.
- Deposit wallet is deployed, funded, and approved:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`.
- Bot env uses:
  - `POLYMARKET_DEPOSIT_WALLET=0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
  - `POLYMARKET_SIGNATURE_TYPE=3`
- Current deposit-wallet balance: `70.185044`.
- Authenticated open orders: `[]`.
- Smoke order succeeded:
  - Status: `matched`
  - Order: `0x3c97624b9fa44cc77fb7661c038af530ab62f33e65d5688394ec3998df00127e`
  - Tx: `0x0bad489f3ad313b0ca811478f03e45028a484d27bfa8fd8b8373df87ac695651`
- Bot order succeeded:
  - Market: `566188`
  - Status: `matched`
  - Order: `0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495`
  - Tx: `0xe5b321d2d81b7b06495b67d950c8a17136c16097a5ef787a1c2d6b72f99139df`
  - CLOB order status: `MATCHED`
  - CLOB maker address: `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Bot order succeeded:
  - Market: `566228`
  - Status: `matched`
  - Order: `0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253`
  - Tx: `0x9a491a4a9015bc475fd306fd19d3c997dc529f66a3d03246cee3d51e0aa5ef1e`
  - CLOB order status: `MATCHED`
  - CLOB maker address: `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Bot order succeeded:
  - Market: `566187`
  - Status: `matched`
  - Order: `0xd8d8c88f0a9fcaee07af6a4eb6c418fb4101a79c75769633e3e01f4752f9a98d`
  - Tx: `0xb2e255a8689e0ebba2a29a24ef5ba5f4e3b34e283272d9a87dd3d071c505d039`
- Bot order succeeded:
  - Market: `653788`
  - Status: `matched`
  - Order: `0xf810656bc6c0292541c35018bf34bc584ac32b88da22887dc46c0d2be6aae816`
  - Tx: `0xc71b4e09ab20ac72ca89cccb932aa1d85723ec7d0315e36453157e32852cd2bb`
- Bot order succeeded:
  - Market: `653788`
  - Status: `matched`
  - Order: `0x54fdf93cb074a73dc34e3edc5e8a289619133d850ca769f2f409359b94db2315`
  - Tx: `0xaa0cf16d7787ef93dd3095dc0de04657dba30504e32820b99af6fcb728c0b161`
- Daemon container `poly1` is healthy.
- Sister swarm container `polymarket-swarm` is healthy in dry-run mode:
  - path: `~/Desktop/poly/bot`
  - image rebuilt: `polymarket-swarm:latest`
  - `TOTAL_CAPITAL=100`
  - Docker restart policy: `unless-stopped`
  - no live swarm trading enabled

## Current Daemon Config

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.0625"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
EXECUTE_SCALPER="false"
SCALP_LEG_USDC="5.0"
```

This targets about `$5` per `poly1` trade and `$5` per scalper leg. The main
trader remains live-capable but is blocked by the risk gate. The scalper remains
shadow-only. The sister swarm remains dry-run only and was observed simulating
about `$5` per position (`4` open simulated positions, `$20.00` at risk).

The first daemon cycle after migration placed no filled bot trade. Two FOK
orders were killed by CLOB because they could not be fully filled immediately
at the recommended price. This was fixed by making market buys orderbook-aware:
the bot now prices FOK orders from the live ask book, caps slippage, and reduces
size when available liquidity is thin.

The daemon is currently sleeping after the latest cycle. The risk gate is
blocking new entries because the cash-balance drawdown calculation is above
`MAX_DAILY_LOSS_PCT=10%`. This calculation compares current cash balance against
`STARTING_BALANCE_USDC=80.0` and does not mark positions to market, so deployed
capital can appear as drawdown. Keep the pause in place until positions and
mark-to-market value are reviewed.

## Useful Commands

Tail logs:

```bash
docker logs --tail 120 -f poly1
```

Check status:

```bash
docker compose ps
```

Check balance/open orders:

```bash
docker compose run --rm trader python -c "import json; from agents.polymarket.polymarket import Polymarket; p=Polymarket(live=True); print(json.dumps({'balance': p.get_usdc_balance(), 'open_orders': p.client.get_open_orders()}, default=str, indent=2))"
```

Stop live daemon:

```bash
docker compose stop trader
```

Disable live mode:

```env
EXECUTE="false"
```

## Sister Swarm Commands

The swarm is separate from `poly1`; it has its own code, DB, wallet config, and
Docker Compose project under `~/Desktop/poly/bot`.

Check swarm status:

```bash
cd ~/Desktop/poly/bot
/Applications/Docker.app/Contents/Resources/bin/docker compose ps
```

Tail swarm logs:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 120 polymarket-swarm
```

Restart swarm dry-run service:

```bash
cd ~/Desktop/poly/bot
BOT_MODE=dryrun LOG_LEVEL=INFO /Applications/Docker.app/Contents/Resources/bin/docker compose up -d swarm
```
