# SESSION 2026-05-23 — SL Audit + Deep Study

**מפעיל:** המפעיל (בעלים)
**Agent:** Claude Code (Opus 4.7, 1M context)
**משך:** ~3 שעות
**תקציר:** קריאה מעמיקה של פרודקשן + סשנים היסטוריים + audit אמפירי של SL events.

---

## 1. רקע

המפעיל ביקש עצירה מסודרת של כל עבודת ה־PR האחרונה (calibration discipline)
ובמקומה: ללמוד את השרת, את הסשנים, את הלוגים. להגדיר חוקים. לנהל גרסאות.
זה הסשן הראשון תחת הדיסציפלינה החדשה שעוגנה ב־`docs/POLY1_WORKING_DISCIPLINE.md`.

המפעיל ציין open question מ־22/5 18:48 שלא קיבל מענה ב־Codex session הקודם:
*"האם ה־SL hits מייצגים פוזיציות שהיו מתאוששות לרווח אם היו מוחזקות?"*

זה ה־question שה־audit כאן עונה עליו.

---

## 2. Findings — מה שמצאתי

### 2.1 מצב השרת (SSH מאומת 2026-05-23 17:38 UTC)

- HEAD: `c5ff2e6` (server) / `f0db42c` (local, 1 ahead) — אחרי commit `767b96c`
  של ה־discipline doc, local הוא 2 ahead.
- `runtime_mode=freeze`, `allowed_live_agents=[]`, `requires_halt=true`.
- `data/HALT` קיים מ־2026-05-22 16:31. freeze הופעל מחדש 2026-05-22 16:07 UTC
  (`manual-freeze-after-live-lab`).
- Equity: $24.34 USDC cash, 0 open positions.
- `MAY_HAVE_FIRED`: 0 ✅
- **`brain_decisions` ללא outcome ב־7d: 198,333** ⚠️ (resolution_sync backlog)
- **markout coverage ב־24h: 17 / 25,847 = 0.07%** ⚠️ (update_shadow_markouts לא רץ)

### 2.2 פעילות מסחר 24h אחרונות

| Status | Count |
|---|---|
| filled | 25 |
| closed_stop_loss | 19 |
| closed_take_profit | 2 |
| closed_timeout | 1 |
| resolved_loss | 2 |
| close_failed | 6 |
| exit_deferred | 59 |
| failed | 3 |
| supervisor_halt | 14 |
| skipped_gate | 379 |

**Win rate הקרוב ל־10% ב־24h האחרונות** (2 TP מול 19 SL). 25 fills נכנסו לפני
ה־freeze ב־16:07 — כל ה־SLs בוצעו בלולאה הזו.

### 2.3 SL Audit — 32 events ב־48h

| מטריקה | ערך |
|---|---|
| Total SLs | 32 |
| 100% SELL | 32/32 |
| Band 0.50-0.60 | 23/32 (72%) |
| **MFE < 0% (never positive)** | **23/32 (72%)** |
| MFE ≥ 4% (TP zone) | 1/32 (3%) |
| Hold < 2min | 12/32 (38%) |
| Hold < 5min | 20/32 (62%) |
| Recovery to entry +5m | 13/22 (59%) |
| Recovery to entry +60m | 15/25 (60%) |

**signal_source distribution:**
- 20 unknown (data quality issue — brain_decisions לא נמצא במצב approved=1)
- 10 opportunity_factory,alphainsider_proven (calibrated=True hardcoded)
- 2 meta_brain,manifold

### 2.4 פרשנות

**72% מהפוזיציות מעולם לא הראו רווח.** המחיר זז נגד הבוט מהדקה הראשונה. זה החתימה
של **adverse selection** — הבוט נכנס אחרי שמידע חדש כבר מצוי בשוק.

**60% מהמחירים חזרו ל־entry אחרי ה־SL** — אבל לא לרמת TP. הם התייצבו סביב 0.495-0.505
מול entry של 0.50. זה noise mean-reversion שלא נושא רווח.

**ה־SL הוא לא הבעיה.** הסטה ל־8% תעלה את ההפסד הממוצע ב־33% בלי לשנות את הסטטיסטיקה
של "פוזיציות שהיו מתאוששות לרווח."

**הבעיה היא הכניסות.** 72% היו SELL ב־0.50-0.60 — favorite-longshot bias.

---

## 3. Decisions — מה הוחלט בסשן

### החלטה 1: Discipline anchored
נכתב `docs/POLY1_WORKING_DISCIPLINE.md` עם 7 כללים + version management.
**Status:** committed locally as `767b96c`. **לא נדחף ל־remote.**

### החלטה 2: PR plan של calibration discipline — מבוטל
ה־8 סבבים של reviewer→user→PR plan על CalibrationRecord — מבוטלים זמנית.
הם concept-drift מה־real P0. נשארים כ־reference ב־memory file
`calibration_discipline_pr.md` לא לפעולה עכשיו.

### החלטה 3: ה־SL לא ישונה
לא יורידים את ה־`MAINTAIN_STOP_LOSS_PCT=0.06` ולא מעלים. ה־data לא תומך באף כיוון.

### החלטה 4 (פתוחה — לאישור המפעיל)
מה ה־P0 הבא? ארבע אפשרויות:

- **(א)** חסימת SELL ב־band 0.50-0.70 דרך `today_lesson_side_blocked` עם TTL.
  ההיגיון: 72% מה־SLs בדיוק שם. blocked-side לא דורש refactor; שינוי env var.
- **(ב)** הסרת `calibrated=True` hardcoded מ־`opportunity_factory.py` (lines 339, 449).
  ההיגיון: 31% מ־SLs מהמסלול הזה (10/32). מסיר את ה־bypass של score floor 0.54→0.80.
- **(ג)** תיקון `update_shadow_markouts` והפעלתו כ־service —
  לפתור את העיוורון לטווח ארוך. ה־P0 #3 של ה־LIVE_AUDIT אתמול.
- **(ד)** lookback על 20 ה־"unknown signal_source" — diagnose data quality
  לפני המלצה אסטרטגית.

---

## 4. Open Questions / Carry-over

1. **20 SLs עם signal_source unknown** — צריך לאמת איך הם בכלל נכנסו לפרודקשן.
   האם הם דרך opportunity_factory בלי matching brain_decisions? Or path אחר?
2. **6 close_failed + 59 exit_deferred ב־24h** — סטטוס פוזיציות תקועות?
   האם הן נסגרו מאז ה־freeze ב־16:07? צריך reconciliation check.
3. **3 קבצי orphan ב־`/srv/poly1/`** (אושר על־ידי המפעיל ש"הכול אני שמתי") —
   `decision_council.py`, `runtime_control.py`, `quant_price_fair_value.py`.
   האם הם מיועדים להישאר כ־scratch או לקבל commit?
4. **Codex session 530MB עדיין פתוח** מ־22/5 — האם להמשיך עליו או לפתוח חדש?

---

## 5. Version Trail

| Commit | Description |
|---|---|
| `f0db42c` | feat: add quant price fair value signal (לפני סשן זה) |
| `767b96c` | docs: add poly1 working discipline (7 rules + version mgmt) |
| `<next>` | docs: add SL audit session journal (this file) |

**Tags created in this session:** none (אין שינוי קוד; רק docs).

**Server commit:** עדיין `c5ff2e6`. לא נדחף שום שינוי.

---

## 6. Carry-over for Next Session

**SSH check first (חוק 7):**

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && git log -1 --oneline && \
  python3 scripts/runtime_control.py status && ls -la data/HALT'
```

**Expected state:**
- HEAD: `c5ff2e6`
- mode=freeze, allowed_live_agents=[]
- HALT file: present

**אם משהו שונה — לעצור ולשאול את המפעיל.**

**Open P0 items** (חוק 6):
1. בחירה בין (א)-(ד) של החלטה 4 (above)
2. תיקון resolution_sync backlog (198k brain_decisions)
3. תיקון markouts pipeline

---

## 7. Audit artifacts

- `/tmp/sl_audit.py` — v1 ניסיון ראשון (orderbook_snapshots empty for closed tokens)
- `/tmp/sl_audit_v2.py` — v2 הסקריפט שעבד (position_marks MFE/MAE + decision_journal post-exit)
- `/tmp/coverage_check.py` — diagnosis של data sources
- `/tmp/sl_audit_out.txt` — full output

(שמורים מקומית, לא ב־repo. אם רוצים שיהיו ב־repo — אעביר אותם ל־`scripts/audits/`
אחרי אישור.)
