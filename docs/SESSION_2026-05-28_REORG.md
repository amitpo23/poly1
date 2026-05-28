# Session 2026-05-28 — Kamatera → Hetzner migration + repo reorganization

## Findings (server first, per working discipline rule 5)

Before any change, verified:

- Kamatera (83.229.82.193): containers down, `HALT` present (since 2026-05-27 16:24), `runtime_control.json` mode=freeze, 1.1 GB `trade_log.db` clean
- Hetzner (167.233.27.32, new this session): provisioned CPX22 / Ubuntu 24.04 / FSN1, `ssh poly1` alias works, swarm BOT_MODE=dryrun, hermes-telegram in standalone container polling, all `.env` files present and 600-permissioned
- Repo at HEAD `8556667` + 43 modified/untracked files in working tree before this session

## Decisions

### Migration (Kamatera → Hetzner)

1. Provisioned Hetzner CPX22 (€9.49 + $0.60 IPv4) directly via Hetzner Console — operator-authorized financial transaction.
2. SQLite migrated via consistent `.backup` snapshot (not hot tar of WAL).
3. Kamatera's uncommitted diff archived at `data/migration_archive/kamatera_uncommitted_20260528.patch` (148 KB). Verified all 19 modified files in the patch are already in Mac's commits → patch is a no-op for Hetzner. Kept for audit.
4. Swarm bot moved to `/srv/swarm`, BOT_MODE flipped live → dryrun for safety.
5. Hermes-Telegram (hotel-booking concierge — unrelated to trading) moved to `/srv/hermes-telegram/` as a **standalone container** with its own Dockerfile + compose, NOT enabled as systemd. Currently UP and polling.
6. Hermes-Forecast (poly1's internal forecast microservice on :8097) stays inside poly1's compose — it is called by sibling agents via Docker internal DNS, not for standalone extraction.
7. Extra data (poly1.db, trades.db, 14 JSONL signal archives, home-dir backup folders, polifly_bridge.py) transferred to Hetzner, deduplicated, archived under `data/migration_archive/` where appropriate.
8. Kamatera shutdown is operator-only — system policy blocks me from terminating the server or modifying billing. Operator received the 4-step checklist + recommendation to keep Kamatera off for 24-48h as fallback.

### Reorganization (housekeeping under P0 freeze — explicit operator approval)

After migration, operator flagged the codebase as feeling overloaded and disorganized. Three explore agents mapped the sprawl:

- 52 services in `docker-compose.yml`, 15 near-identical `external-conviction-*` blocks
- 106 docs files (31 `SESSION_*.md`, sessions 16+ days old next to today's)
- 71 files in `agents/application/` flat, including 3 `btc5min_timed_v*` and 3 `_router.py` files whose names suggested duplicates

Plan agent designed the conservative scope; operator confirmed via AskUserQuestion. Three phases executed.

## Commits this session

| SHA | Subject |
|---|---|
| `41f2db1` | docs: archive 18 stale SESSION_* + POSTMORTEM_* files |
| `ff501ce` | compose: factor 15 external-conviction services into x-ec-base anchor |
| `e5a684d` | docs: add AGENTS_MAP.md — group 71 agents/application/ files by role |

Snapshot tags (revert anchors):
- `pre-docs-archive-20260528-1657`
- `pre-ec-anchors-20260528-1700`
- `pre-agents-map-20260528-1715` (approx)

## Verification

- Phase 1: `git log --follow` history preserved on sampled moved file; 0 broken code references after sed-update of 3 doc-pointing files (`scripts/python/scout.py`, `scripts/python/backtest_nothing_happens.py`, `deploy/CURRENT_STATUS.md`)
- Phase 2: `docker compose config --profile <all-20>` on Hetzner produced **byte-identical** output before vs after the anchor refactor (after filtering the benign `x-ec-base` block emitted by docker-compose's normalizer). 46 services across 21 profiles verified.
- Phase 3: 71/71 files in `agents/application/` covered by `AGENTS_MAP.md` (verified by directory diff).
- End-state: bot still HALT + freeze on Hetzner (`docker exec` confirms `mode=freeze`, `data/HALT` present).

## Deliberately deferred (carry-over to next session or later)

These are real but out-of-scope under the P0 freeze. Documented but not addressed:

- `agents/application/` subdirectory restructuring (would break 15 compose commands + every import statement)
- `scripts/` + `scripts/python/` unification (needs cron/systemd audit)
- 5 open P0s from `AGENT_AUDIT_2026_05_26.md`: SL audit, resolution_sync 198k backlog, markouts pipeline (0.07% coverage), exit_deferred recovery, opportunity_factory inflation
- 22 entry agents but only `scanner_executor` produces `decision_journal` → calibrator blind to 21 pipelines
- Manifold weighted equal to Kalshi (signal-quality issue)
- MTM SELL math possibly 5-6x off
- Anthropic credit balance for swarm's API key is low (LLM agents error 400 — billing issue, not migration)
- Hetzner: ufw inactive (only SSH exposed; recommend before opening dashboard ports). Hetzner Cloud Firewall in Console is the other path.
- Kamatera billing/termination — operator-only

## Push status

All three commits are local only. Per working discipline rule 3, no `git push` without operator explicit "push" in chat.
