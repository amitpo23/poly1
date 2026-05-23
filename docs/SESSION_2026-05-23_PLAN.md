# SESSION 2026-05-23 — Unified Plan

**מפעיל:** המפעיל (בעלים)
**Agent:** Claude Code (Opus 4.7, 1M context)
**מקור:** קוד-ריביו + 18 doc digests + SL audit + PRE_LIVE_QA_REVIEW + LIVE_AUDIT
**תקציר:** תוכנית מאוחדת לטיפול ב־P0 לפני live trading הבא, בכל ההמלצות מ־5+
ימים אחרונים, מבוססת ground truth (SL audit) ולא ספקולציה.

---

## הממצא שמכריע את הסדרי עדיפויות

ה־code review גילה: **`SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED` חסר ב־
`deploy/.env.runtime`.** ה־guard קיים בקוד (`scanner_executor.py:806`),
ה־`LIVE_LESSONS_2026-05-21.md` כותב במפורש להפעיל אותו, אבל ה־env vars
לא נוספו. הסשן של Codex עשה 80% מהעבודה ושכח את ה־1% האחרון.

**ה־30 SLs של 21/5 + 22/5 היו 100% SELL, 72% ב־band 0.50-0.60 — בדיוק מה
שה־learning guard היה אמור לחסום.** ההפסד אינו כשל אסטרטגי; הוא כשל config.

זה ה־P0 #1. 4 env vars = 30%+ מההפסד היומי נמנע.

---

## תוכנית — Tier 0a → Tier 2

### Tier 0a — Config only (15 דקות, אפס סיכון קוד)

| # | פעולה | קובץ | סיבה |
|---|---|---|---|
| 1 | הפעלת learning guard (4 env vars) | `.env.runtime` או `runtime_control.py` | מבטל 72% מה־SL pattern |
| 2 | TTL ל־`today_lesson_*` (24h) | `.env.runtime` / config | מונע "אתמול כלא לנצח" |

### Tier 0b — קוד קטן + tests (יום עבודה)

| # | פעולה | קובץ:שורה | סיכון |
|---|---|---|---|
| 3 | הסרת `calibrated=True` hardcoded — wallet path | `opportunity_factory.py:449` | נמוך |
| 4 | Schedule `update_shadow_markouts` כ־service | `docker-compose.yml` + cron | נמוך |
| 5 | תיקון OpenAI SystemMessage (51 כשלים זהים) | `near_resolution.py:380-383` | אפס |
| 6 | per-market RiskGate re-check ב־scanner_executor | `scanner_executor.py:~623` | נמוך |
| 7 | תיקון `Polymarket(live=True)` ב־settlement_reconciler | `settlement_reconciler.py:509` | נמוך |

### Tier 0c — Critical Blockers מ־PRE_LIVE_QA_REVIEW (יום-יומיים)

| # | פעולה | מקור | סיכון |
|---|---|---|---|
| 8 | `recover_stranded_pendings` race fix | C-1 (PRE_LIVE_QA) | בינוני |
| 9 | EXECUTE_MAINTAIN heartbeat circuit-breaker | C-4 | בינוני |
| 10 | calibration loop integration test | C-2 | אפס |
| 11 | tenacity HTTPError guard test | C-3 | אפס |

### Tier 1 — Quality fixes (אחרי first live)

12. `SCANNER_EXECUTOR_REQUIRE_PROMOTABLE_STRATEGY=true` — לחסום אגנטים לא־מאומתים
13. SELL band block ב־0.50-0.70 (TTL) — רק אם learning guard לא מספיק
14. SQLite `busy_timeout=30s` + `wal_autocheckpoint=1000` (H-4)
15. Allocator-sync atomic write (H-13)
16. Allowance check + cache 60s לפני order submit (H-2)
17. MetaBrain weight normalization to 1.0 (H-8)
18. FAK partial-fill handling (H-3)

### Tier 2 — Research (לא לפני live)

- Fee-aware EV gate (~$0.75 מ־$0.98 ההפסד היה fees)
- Negative-risk arb scanner
- Order-book imbalance feature
- Reduce max_hold ל־90-120 דק' לסוכני short-horizon
- CalibrationRecord refactor (ה־PR plan המקורי — deferred)

---

## הערות קריטיות

### 1. ה־`deploy/.env.runtime` המקומי **מסוכן ל־deploy**

```
RUNTIME_MODE="live"           ← השרת ב־freeze
EXECUTE_SCANNER_EXECUTOR="true"
```

אם זה ידחף בטעות (חוק 3 אמור למנוע) — הוא יוריד את ה־freeze. **כלל deploy:
לא לדחוף `.env.runtime` ישירות; להריץ `scripts/runtime_control.py freeze`
בשרת אחרי deploy של קוד.**

### 2. commit `7086767 fix: let proven calibrated signals pass` הוא ה־culprit

זה ה־commit שהוסיף את ה־bypass של score 0.80→0.54 ל"proven calibrated".
**לא reverted — הקוד טוב אם ה־calibration אמיתי. אבל בלי learning guard,
ה־bypass הפך לחור שאפשר ל־opportunity_factory להתפזר.** הפתרון אינו revert
אלא: (א) הפעלת learning guard, (ב) הסרת `True` hardcoded ב־wallet path.

### 3. PR של calibration discipline — נשאר deferred

לפי ה־cross-check, 4 CRITICAL מ־PRE_LIVE_QA_REVIEW לא קשורים ל־CalibrationRecord.
הם תשתיתיים. CalibrationRecord עדיין שווה — אבל P2, לא P0.

### 4. 20 SLs עם signal_source unknown

62% מה־SL audit sample חסרים provenance. Data quality issue שצריך
diagnose — אולי `brain_decisions` רישום לא תואם ל־trades, או שה־signal_source
column הוא NULL במצבים מסוימים. **רושם זאת כ־Carry-over.**

### 5. Critical Gaps מ־doc digest

- **Tavily disabled but called 11,772x** — kill-switch לא מתפשט לכל הקוראים.
- **6 close_failed + 59 exit_deferred** ב־24h — סטטוס פוזיציות תקועות לא ברור.
- **198k brain_decisions backlog** — אין alert על depth, נמצא בשקט.

---

## Decisions Carried Over From Previous Sessions

| Source | Recommendation | Status here |
|---|---|---|
| LIVE_LESSONS_2026-05-21 §"Policy For Next Cycle" | Enable learning guard | **Tier 0a #1** |
| LIVE_LESSONS_2026-05-21 §"Tomorrow's First Probe" | $1/trade, 2-4 open, 15-min window | Deployment guide |
| LIVE_AUDIT_2026-05-22 P0 #1 (`near_resolution.py:380-383`) | Add SystemMessage | **Tier 0b #5** |
| LIVE_AUDIT_2026-05-22 P0 #2 (markouts service) | Schedule update_shadow_markouts | **Tier 0b #4** |
| LIVE_AUDIT_2026-05-22 P0 #3 (scanner cache) | Quarantine repeat rejects | ✅ Already in-flight |
| LIVE_AUDIT_2026-05-22 P0 #4 (opportunity calibrated) | Remove hardcoded `True` | **Tier 0b #3** |
| LIVE_AUDIT_2026-05-22 P0 #5 (today_lesson TTL) | Add TTL | **Tier 0a #2** |
| PRE_LIVE_QA_REVIEW C-1 (recover_stranded_pendings) | Move from __init__ | **Tier 0c #8** |
| PRE_LIVE_QA_REVIEW C-2 (calibration loop test) | Write test | **Tier 0c #10** |
| PRE_LIVE_QA_REVIEW C-3 (tenacity guard) | Write test | **Tier 0c #11** |
| PRE_LIVE_QA_REVIEW C-4 (EXECUTE sync) | Heartbeat assertion | **Tier 0c #9** |

---

## Deploy Strategy לפי החוקים

**Phase 1 (היום או מחר):** Tier 0a בלבד — config change בלי קוד.
- Branch: `study/learning-guard-on-20260523`
- Tag: `pre-learning-guard-20260523-HHMM`
- Diff מוצג, אישור, commit.
- Push רק אחרי אישור deploy.
- בשרת: `git pull` → `runtime_control.py freeze` ואז `runtime_control.py
  live-probe` → 15 דקות observation.

**Phase 2 (יום אחרי):** Tier 0b — code fixes קטנים אחד-אחד. כל אחד tag נפרד.

**Phase 3 (3-5 ימים):** Tier 0c — Critical blockers.

**Phase 4:** Tier 1 בהדרגה.

---

## Carry-over for Next Sessions

1. **20 SLs unknown signal_source** — diagnose data quality.
2. **6 close_failed + 59 exit_deferred** — reconciliation check.
3. **198k brain_decisions backlog** — resolution_sync depth investigation.
4. **Tavily kill-switch propagation** — provider still called 11k×.
5. **3 server orphan files** (`/srv/poly1/decision_council.py`,
   `runtime_control.py`, `quant_price_fair_value.py`) — decide commit vs leave.

---

## Version Trail (this session)

| Commit | Description |
|---|---|
| `f0db42c` | feat: add quant price fair value signal (pre-session) |
| `767b96c` | docs: add poly1 working discipline (7 rules) |
| `0ea226d` | docs: add SL audit session journal |
| `<next>` | docs: add unified plan (this file) |

**Server commit unchanged:** `c5ff2e6`. No pushes from this session.
