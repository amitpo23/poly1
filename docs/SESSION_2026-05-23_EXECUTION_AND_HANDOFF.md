# SESSION 2026-05-23 — Execution & Handoff

**מפעיל:** המפעיל (בעלים)
**Agent:** Claude Code (Opus 4.7, 1M context)
**משך כולל:** ~5 שעות
**תקציר:** מסשן הקריאה והאודיט (`SESSION_2026-05-23_SL_AUDIT.md`)
ועד ה־execution הלילה — 11 commits מקומיים, אפס push, freeze מוחזק.

---

## 1. מה שבוצע היום (11 commits, כל אחד מקומי, אפס push)

| Commit | Type | Description |
|---|---|---|
| `767b96c` | docs | Working discipline (7 rules + version mgmt) |
| `0ea226d` | docs | SL audit session journal |
| `bf9387e` | docs | Unified plan for the session |
| `85cac28` | feat | **P0 #1**: learning guard in BASE_ENV (`runtime_control.py`) |
| `504270d` | docs | Rule 2 amendment — diff verification + 4-criteria authorization |
| `f8d6aea` | feat | Canonical signal contract + decision council EV gate |
| `adbb108` | fix | near_resolution OpenAI SystemMessage (51 errors / day) |
| `4f465f5` | feat | Shadow markouts service + perf tearsheet + brain cycle + audit docs |
| `01809d6` | feat | Scanner enhancements + opportunity factory (**C-2/I-1/I-2 flagged**) |
| `3162d2f` | docs | .env.example documentation update |
| `1274307` | chore | .env.runtime regenerated as clean freeze state |

**Tags:**
- `pre-learning-guard-20260523-1845` → `bf9387e` (before code change)
- `pre-codex-batch-20260523-1859` → `85cac28` (before Codex batch)
- (server) `pre-merge-20260523-1912` → server `c5ff2e6` (before any merge)

---

## 2. Findings — מה למדנו

### 2.1 Root cause של ה־30 SLs מאתמול

**LIVE_AUDIT_2026-05-22.md + SESSION_2026-05-23_SL_AUDIT.md הראו**:
- 100% SELL, 72% ב־band 0.50-0.60.
- 72% של ה־MFE היה <0% — הפוזיציות מעולם לא הראו רווח.
- 60% מהמחירים חזרו ל־entry אחרי SL, אבל רק לרמת noise (לא TP).

**Code review של ה־uncommitted Codex work** גילה:
- ה־scanner_executor learning guard קיים בקוד ופועל (lines 400-412, 517-533).
- ה־env vars שמפעילים אותו (`SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED` ו־4 נוספים)
  הוגדרו **רק ב־`live_hour()`** ב־runtime_control.py:631-653.
- **`freeze()` ו־`live_probe()` לא הגדירו אותם** → ה־.env.runtime לא הכיל
  אותם → ה־guard היה inert.

**הפיתרון (committed as `85cac28`):** הוספת 7 keys ל־BASE_ENV כך ש־freeze
ו־live-probe מחזיקים את ה־guard בין הפעלות.

### 2.2 4-criteria framework (commit `504270d`)

המפעיל הוסיף mandate: ה־agent יבצע autonomously שינויים שעונים בבירור על
4 מטרות: רווח, validate, test+execute, winrate+learning. Excluded: שינויי
live config, מחיקות, push, SPEC/CLAUDE changes. ראה
`docs/POLY1_WORKING_DISCIPLINE.md` §2.2.

---

## 3. Decisions שהתקבלו

| # | Decision | Status |
|---|---|---|
| 1 | Calibration discipline PR plan — DEFERRED | ה־P0 הם תשתיתיים, לא ה־CalibrationRecord |
| 2 | SL parameters not changed | Data shows entries are the problem, not SL |
| 3 | Rule 2 amended with diff-verification + 4-criteria standing auth | Committed |
| 4 | Learning guard fix is P0 #1 | Committed `85cac28` |
| 5 | Codex's 124+205+9 lines of in-flight work — captured topically | 5 commits |
| 6 | `.env.runtime` regenerated as clean freeze | Committed `1274307` |
| 7 | Server merge — deferred to next session | Backup taken `pre-merge-20260523-1912` |
| 8 | No push tonight | Freeze holds; learning guard not yet on server |

---

## 4. Critical constraint (advisor verified)

**FREEZE MUST HOLD UNTIL MERGE.** The learning guard fix is local-only
(commit `85cac28`). The server is still at `c5ff2e6`, which does **not**
have the learning guard in BASE_ENV. If anyone runs `runtime_control.py
live-probe` on the server tomorrow without first pulling the merged
code:

- `.env.runtime` is regenerated from server's BASE_ENV (no learning guard)
- `SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED` defaults to `False` (code default)
- Scanner accepts SELL @ 0.50-0.60 again
- Yesterday's 30-SL pattern repeats

**Do not arm a live probe until the server has 85cac28 (or equivalent).**

---

## 5. Server state — handoff to next session

### Current
- HEAD: `c5ff2e6` (12 commits behind local main)
- mode: `freeze`, allowed_live_agents: [], HALT present, equity $24.34
- Working tree: 24 modified files + 7 untracked

### Server-only changes (not in any local commit)
- `agents/application/crypto_exchange_tape.py` (modified)
- `agents/application/meta_brain.py` (modified)
- `agents/application/sizing.py` (modified)
- `tests/test_meta_brain.py` (modified)
- `agents/application/quant_price_fair_value.py` (untracked — local has it
  in commit `f0db42c` but server treats it as new)
- `tests/test_quant_price_fair_value.py` (untracked, same)
- `/srv/poly1/decision_council.py` (root orphan, operator-placed)
- `/srv/poly1/runtime_control.py` (root orphan, operator-placed)

### Conflicting changes (server has additional work on files I committed)
- `scripts/runtime_control.py` — server 194 lines vs my 124-line commit
  (server has ~70 extra lines)
- `agents/application/scanner_executor.py` — server 352 lines vs my
  205-line commit (server has ~147 extra lines)
- `agents/application/opportunity_factory.py` — server 34 lines vs my
  9-line commit (server has ~25 extra lines — **read these first; may
  contain something worse than C-2 calibrated=True hardcode**)

### Backup
```
/home/trader/poly1_backups/
  20260523-1912-status.txt       (1.1KB)
  20260523-1912-tracked.patch    (108KB — full git diff)
  20260523-1912-untracked.tar.gz (20KB)
```
Server tag: `pre-merge-20260523-1912`.

---

## 6. Carry-over — next session's playbook

### Step 0: Pre-session verification (Rule 7)

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && \
  git log -1 --oneline && \
  python3 scripts/runtime_control.py status && \
  ls -la data/HALT && \
  ls -la /home/trader/poly1_backups/ | grep 20260523-1912'
```

Expected: HEAD `c5ff2e6`, mode=freeze, HALT present, 3 backup files present.

### Step 1: Review the patch (priority order)

1. `opportunity_factory.py` extra 25 lines — **read first** (advisor flagged
   may contain worse than the C-2 hardcode).
2. `scanner_executor.py` extra 147 lines — what did Codex add beyond what
   I committed?
3. `runtime_control.py` extra 70 lines — does it touch BASE_ENV lines
   290-310? If yes, conflict with my commit `85cac28`.
4. The 4 server-only files (crypto_exchange_tape, meta_brain, sizing,
   test_meta_brain) — what's there?
5. 2 root orphan files — keep or move?

### Step 2: Decide merge direction

Per the discipline rules + advisor:
- Independent server-only files → bring to local as new commits.
- Conflicting files (3 files where both sides changed) → review line-by-line.
- Push the consolidated history once.

### Step 3: After merge, server-side regenerate

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && git pull && \
  python3 scripts/runtime_control.py freeze --note "post-merge regenerate" && \
  grep SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED deploy/.env.runtime'
```

Expected: `SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED="true"` present.

### Step 4: Tier 0b items (after merge stable)

1. Fix C-2 — `opportunity_factory.py:449` hardcoded `calibrated=True`
2. Fix I-1 — `scanner_executor.py:258` `Polymarket(live=True)` in shadow
3. Fix I-2 — missing per-market RiskGate.ok() recheck
4. Schedule `update_shadow_markouts.py` as docker-compose service
5. SPEC.md sync — document new env vars per CLAUDE.md convention

### Step 5: Tier 0c — PRE_LIVE_QA_REVIEW Critical blockers

C-1 (recover_stranded_pendings race), C-2 (calibration loop test),
C-3 (tenacity HTTPError test), C-4 (EXECUTE_MAINTAIN heartbeat).

### Step 6: Live probe

Only after Tier 0a + 0b complete. Per LIVE_LESSONS_2026-05-21.md:
- `$1` per trade, max 2-4 open, 15-min window initially
- `SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED=true` (now in BASE_ENV — verify)
- Monitor `today_lesson_side_blocked` and `today_lesson_price_band_blocked`
- If clean → expand. If bad → freeze.

---

## 7. Open questions (still unresolved)

1. **20 SLs with signal_source unknown** (from SL audit) — data quality
   issue not yet diagnosed.
2. **6 close_failed + 59 exit_deferred** in 24h — status of stuck positions
   not verified post-freeze.
3. **198k brain_decisions without outcome_status** — resolution_sync
   backlog cause not investigated.
4. **Tavily disabled but called 11,772×** per LIVE_AUDIT — kill-switch
   propagation broken.

---

## 8. Version Trail summary

```
1274307 chore: regenerate .env.runtime as freeze state (post-codex batch)
3162d2f docs: env.example — document learning guard + repeat-reject vars
01809d6 feat: scanner executor enhancements + opportunity factory routing  ⚠️ C-2/I-1/I-2 flagged
4f465f5 feat: shadow markouts service + performance tearsheet + brain cycle
adbb108 fix: near_resolution OpenAI SystemMessage (51 errors / day)
f8d6aea feat: canonical signal contract + decision council EV gate
504270d docs: amend Rule 2 with diff-verification + 4-criteria authorization
85cac28 feat: persist scanner_executor learning guard across runtime modes   ← P0 #1
bf9387e docs: add unified plan for 2026-05-23 session
0ea226d docs: add session journal for SL audit (2026-05-23)
767b96c docs: add poly1 working discipline (7 rules + version mgmt)
f0db42c feat: add quant price fair value signal  (pre-session)
```

`f0db42c..HEAD` = 11 commits this session. Server at `c5ff2e6` is 12
behind (the additional one is `f0db42c` itself which predates session).

---

## 9. Lessons for the next agent

1. **`git add <file>` stages the WHOLE file** — including pre-existing
   modifications from other sessions. Always `git diff <file>` BEFORE
   `git add` to verify the diff matches the approved edit.
2. **Don't trust commit messages from earlier sessions** — verify in code.
   The "learning guard" was documented as "enabled" in LIVE_LESSONS but
   the env vars were never persisted.
3. **Server can have work that local doesn't** — pre-session SSH check
   (Rule 7) catches this BEFORE you build assumptions.
4. **The advisor catches things you miss** — call it before declaring
   complete. Today's main save: the explicit "freeze MUST hold until
   merge" warning, prevented potential live-probe re-run with broken
   guard.
