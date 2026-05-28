# CLAUDE.md — guidelines for working in poly1

This is a money-handling Polymarket bot. Treat every change as a potential
production incident.

> **Sister project:** `~/Desktop/poly/bot` is the user's other Polymarket
> bot — a multi-agent swarm. They share the paper-trade journal and
> 10-strategy doc in `~/Desktop/poly/`, but **no code**. Each runs on its
> own wallet, its own SQLite, its own Docker container. Don't propagate
> changes between them; don't import or reference swarm modules from here.
>
> For shared launch / risk decisions (e.g. "$100 each on Sunday"), see
> `~/Desktop/poly/OPERATIONS.md`.

## Read first

- `SPEC.md` — what the bot does, contracts between modules, env vars.
- `deploy/PREFLIGHT.md` — operator checklist before live trading.
- `docs/HETZNER_SERVER_ACCESS.md` — **how to reach the production server**
  (`ssh poly1` → `167.233.27.32`). Old Kamatera host `83.229.82.193` was
  retired 2026-05-28. Required reading before any `ssh`/`scp` to "the server".
- `docs/AGENTS_MAP.md` — what each module in `agents/application/` does
  (with the gotcha that `btc5min_timed_v2/v3` are independent strategies,
  not version-bumps).
- `docs/POLY1_WORKING_DISCIPLINE.md` — 8 mandatory rules for any agent
  making changes here (snapshots, diff-before-commit, deploy gate, session
  journal, server-first verification, P0 discipline, container-env truth).
- `~/Desktop/poly/OPERATIONS.md` — joint operations playbook for both bots.
- `~/Desktop/poly/HANDOFF.md` — pre-launch decision brief for the next
  reviewer (alongside `~/Desktop/poly/reviews/swarm_review_2026-05-01.md`).
  Read this if you arrive in the codebase between 2026-05-01 and the
  Sunday launch and there's still an open A/B/C decision.
- `agents/application/prompts.py:one_best_trade` — the LLM contract that
  determines side semantics. **Any change here must be matched in
  `agents/polymarket/polymarket.py:execute_market_order` and vice versa.**

## Don't break these invariants

1. **Side ↔ token mapping is paired with prompt semantics.** `outcomes[0]` is
   the primary outcome. `BUY` → `token_ids[0]` at `recommendation.price`.
   `SELL` → `token_ids[1]` at `1.0 - recommendation.price`. Changing one
   side without the other = silently wrong trades.

2. **Idempotency is the SQLite ledger.** The dedupe contract is
   `ACTIVE_STATUSES` — anything in `ACTIVE_STATUSES` blocks the same market
   for `dedupe_hours`. Never change `MAY_HAVE_FIRED` to `FAILED` or remove
   it from `ACTIVE_STATUSES` — that opens a double-fill window after a crash.

3. **Tenacity retries on network errors only.** Adding `HTTPError` or generic
   `Exception` to the retry predicate can double-submit orders. The CLOB has
   no retract primitive for FOK fills.

4. **`RiskGate.ok()` is called twice per cycle.** Pre-sweep (entry guard)
   AND per-market (between LLM call and execute). Both calls matter — the
   per-market call catches rate-limit and balance changes that happened
   during the LLM call.

5. **Don't re-introduce `Polymarket()` without `live=` argument.** Read-only
   paths (Executor's `map_api_to_market`) must use `live=False` to avoid
   requiring a private key in test/dry contexts.

## Code conventions

- New env vars: add to `.env.example` AND `SPEC.md` §7. Document the
  default and what it gates.
- New trade statuses: extend the enum in `trade_log.py`, decide if it
  belongs in `ACTIVE_STATUSES`, and update SPEC §6.
- New LLM call sites in `executor.py`: use `_invoke_tracked(messages, tag)`
  not `self.llm.invoke` — otherwise the cost gate undercounts.
- Logging: use `logger = logging.getLogger(__name__)`, not `print`.
  `print` is reserved for one-shot CLI output (`scripts/python/cli.py`).
- New persistent files: write under `./data/` and ensure the bind-mount in
  `docker-compose.yml` covers them.

## How to test

The repo's pinned `requirements.txt` requires Python ≥3.9.10 (system
Python on macOS is often older). Two ways:

1. Inside Docker (canonical):
   ```bash
   docker compose build
   docker compose run --rm trader python -m unittest discover -s tests -v
   ```

2. Locally with stdlib-only subset (covers TradeLog + RiskGate + parser):
   ```bash
   python3 -m unittest tests.test_executor tests.test_trader.TestTradeLog tests.test_trader.TestRiskGate -v
   ```

Always run tests before opening a PR. CI runs `black --check`,
`unittest discover -s tests`, and a `cli --help` smoke test.

## How to run a dry-run cycle

```bash
docker compose run --rm trader python deploy/run.py
# or, single-shot via CLI:
docker compose run --rm trader python scripts/python/cli.py run-autonomous-trader
```

`EXECUTE=false` (default) writes `skipped_dry_run` rows. Inspect with:
```bash
docker compose run --rm trader python scripts/python/cli.py inspect-trades --limit 30
```

## How to flip to live trading

NEVER without going through `deploy/PREFLIGHT.md`. Specifically:

1. Run CTF/USDC approvals once per wallet:
   ```bash
   docker compose run --rm trader python -c \
     "from agents.polymarket.polymarket import Polymarket; \
      p = Polymarket(live=True); p._init_approvals(True)"
   ```
   Record the tx hashes.

2. Verify `STARTING_BALANCE_USDC` matches actual wallet balance.

3. Run shadow mode (24 h) with `EXECUTE=false` and inspect the journal:
   markets vary, side/token alignment looks right, hypothetical trade
   count is non-zero.

4. Set `EXECUTE=true` with `MAX_POSITION_FRACTION=0.05` and small capital
   ($5–$50). Re-evaluate at every order of magnitude.

## What's intentionally NOT in scope (don't add without discussion)

- Position close / `maintain_positions` (stub).
- Kubernetes / Helm.
- Prometheus / Grafana / structured-metrics export.
- Adaptive position sizing.
- Multi-wallet operation.

## Scalper module (Strategy C)

The scalper is a SECOND, INDEPENDENT trading agent in this repo. It runs
in its own container (`profiles: scalper` in docker-compose) and shares
only the SQLite ledger and the Polymarket wallet with the Trader. Capital
isolation is enforced by `SCALPER_RESERVE_USDC`.

When working on scalper code:

- Do not couple scalper logic to the Trader's LLM pipeline. The whole
  point of the scalper is that it runs without LLM calls.
- The dedupe contract for the scalper is `scalper_pairs.state`, NOT
  `ACTIVE_STATUSES`. Adding `SCALPER_LEG` to `ACTIVE_STATUSES` would
  break the Trader's dedupe of unrelated markets.
- `RECONCILE_NEEDED` is to the scalper what `MAY_HAVE_FIRED` is to the
  Trader: do not auto-clear it; the operator must verify on-chain.

## Versioning

- Tag prefix `prod-YYYYMMDD-HHMM` for VPS releases.
- Tag prefix `v0.X.Y-prod-prep` for pre-production milestones.
- Keep `SPEC.md` in lockstep with the tagged version.

## Sunday 2026-05-03 launch — $100 in this bot

Per the dual-bot plan in `~/Desktop/poly/OPERATIONS.md`:

- This bot gets **$100 USDC** on its own wallet (`POLY1_WALLET` in `.env`).
- Sister swarm bot gets **$100** on a separate wallet — independent.
- Both flip to live the same morning, but failures are independent.
- Cross-bot policy: if BOTH lose >5% on the same day, halt both. If only
  one halts, the other keeps going (each bot's RiskGate decides).
- Stage-1 trade size for the first 24 h is small ($1-$5 / trade) to
  observe real CLOB fees before scaling. Configured via
  `MAX_POSITION_FRACTION` in `.env`.

## Hebrew/English

The user prefers Hebrew prose for explanations and updates; identifiers,
file paths, code, and commit messages stay in English. Be precise about
technical terms even in Hebrew.
