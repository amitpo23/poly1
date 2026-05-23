# poly1 — Working Discipline

**נוסח: 2026-05-23 על־ידי Claude Code, על־פי בקשה מפורשת של המפעיל.**

מסמך זה מגדיר את כללי העבודה של agents (Claude Code, Codex, וכל agent אחר) על `poly1`.
הכללים תקפים עד שיוחלף בכתב על־ידי המפעיל.

**עדיפות:** User explicit instruction > CLAUDE.md > מסמך זה > default behavior של ה־agent.

---

## חוק 1 — Snapshot לפני שינוי קוד

לפני שינוי קוד ב־poly1 (לא כולל מסמכי docs/), יוצר נקודת חזרה:

- **שינוי קטן** (קובץ אחד, ≤50 שורות): `git tag pre-<topic>-YYYYMMDD-HHMM`
- **שינוי גדול** (multi-file, refactor, >50 שורות): branch ייעודי `study/<topic>-YYYYMMDD`

**אסור ללא אישור מפורש בכתב:**
- `git push --force` / `--force-with-lease`
- `git reset --hard` על `main`
- `git rebase` שמשנה היסטוריה שכבר נדחפה
- `git commit --amend` על commit שכבר נדחף
- מחיקת branches שלא צוין במפורש

---

## חוק 2 — Diff + Go לפני כל שינוי

לפני שינוי קוד / docs / config / .env:

1. ה־agent מציג בצ'אט: ה־diff (לקובץ קיים) או את התוכן המלא (לקובץ חדש).
2. ה־agent ממתין ל־"go" / "אישור" / "מאשר" מפורש.
3. רק אז מבוצעת הכתיבה.

**חריגים שאינם דורשים אישור pre-write:**
- קריאה (Read tool, ls, cat).
- חקירה (grep, find).
- SQL **read-only** על שרת.
- TaskCreate / TaskUpdate (state ניהול פנימי).

**אסור:** "תיקון קטן" שמבוצע "בלי לטרוח לאשר".

---

## חוק 3 — Deploy Gate

אין `git push` ל־remote בלי המפעיל רואה את ה־commits במפורש:

1. `git log <range> --stat` מוצג בצ'אט.
2. המפעיל אומר "push" / "deploy".
3. רק אז `git push`.

**אסור:** push אוטומטי, גם אם המפעיל אמר "go" ל־commit עצמו.

**Server is canonical:** `trader@83.229.82.193:/srv/poly1` הוא source of truth ל־runtime.
Local הוא staging/dev/review בלבד. ה־server נשמר נקי דרך `scripts/verify_server_source_of_truth.sh`
(חוץ מ־`deploy/.env.runtime` המאושר).

---

## חוק 4 — Session Journal

כל סשן עבודה משמעותי כותב `docs/SESSION_YYYY-MM-DD_<topic>.md`.

תוכן מינימלי:

- **Findings** — מה למדנו (ground truth, לא ספקולציה)
- **Decisions** — מה הוחלט + rationale קצר
- **Version trail** — אילו tags/branches/commits נוצרו
- **Carry-over** — מה הצעדים הבאים, מי בעל ה־action

**מטרה:** סשן הבא ימשיך בלי "מה קרה אתמול?"

---

## חוק 5 — Server First, Speculation Second

לפני כל המלצה תפעולית — agent קורא:

- `runtime_control.py status` בשרת (mode, allowed agents, config_hash, HALT)
- `git log -1` בשרת (HEAD)
- `data/HALT` קיים?
- Logs רלוונטיים ל־24h האחרונים

**אסור:** המלצה מבוססת זיכרון, audit doc ישן, או memory file בלי אימות שרת.

---

## חוק 6 — P0 Discipline

כל זמן שיש P0 פתוחים, אסור לעבוד על feature חדש, refactor גדול, או אופטימיזציה.

**P0 פתוחים נכון ל־2026-05-23:**

1. **SL audit** — שאלת המפעיל מ־22/5 18:48 (פתוחה): האם SL=6% חזק מדי?
   האם פוזיציות שיצאו ב־SL היו מתאוששות לרווח?
2. **resolution_sync backlog** — 198,333 brain_decisions ללא outcome ב־7 ימים.
3. **markouts pipeline broken** — `update_shadow_markouts.py` לא רץ;
   markout coverage 0.07% ב־24h.
4. **stuck exits** — 6 `close_failed` + 59 `exit_deferred` ב־24h האחרונות שלא נסגרו.
5. **opportunity_factory inflation** — `estimated_win_probability_calibrated=True`
   hardcoded; אחראי ל־73% מהכניסות ב־21/5 עם prob inflation לא־מכוילת.

**אסור:** "אני יודע שזה לא P0 אבל זה ייקח רק 10 דקות".

---

## חוק 7 — Pre-Session Verification

בתחילת כל סשן (אחרי /clear, restart, או שינוי context):

1. SSH check:
   ```bash
   ssh trader@83.229.82.193 'cd /srv/poly1 && git log -1 --oneline && \
     python3 scripts/runtime_control.py status && \
     ls -la data/HALT'
   ```
2. אם השרת לא ב־freeze, או HEAD/HALT/config_hash לא תואמים למה שהמפעיל זוכר —
   **עוצרים ושואלים.**
3. רק אם מאמתים — ממשיכים לעבודה החדשה.

**מטרה:** לא לעבוד בעיוור. אם משהו זז מאז הסשן הקודם — acknowledgment ראשון.

---

## Version Management — איך חוזרים לכל נקודה

עץ ה־git:

- `main` — current local state
- `prod-YYYYMMDD-HHMM` — VPS releases (per CLAUDE.md)
- `v0.X.Y-prod-prep` — pre-production milestones (per CLAUDE.md)
- `study/<topic>-YYYYMMDD` — branches לחקירות (חדש)
- `pre-<topic>-YYYYMMDD-HHMM` — restore-point tags לפני שינוי (חדש)

**לחזור לנקודה:**

```bash
# מקומית
git checkout <tag-or-branch>

# בשרת (רק אחרי "deploy" מפורש)
ssh trader@83.229.82.193 'cd /srv/poly1 && git fetch && git checkout <tag>'
```

**Restore SQLite:** `/srv/poly1/data/backups/` בשרת — backups לילית
(`PREFLIGHT_REQUIRE_DB_BACKUP=true` ב־`.env.runtime`).

---

## חריגים

המפעיל יכול:

- לבטל/לעדכן כלל בכתב מפורש.
- לתת **ad-hoc override** לחוק (production incident, חקירה דחופה).

ad-hoc override:

- מתועד ב־session journal (חוק 4) בתת־סעיף "Overrides".
- חד־פעמי; לא הופך לכלל הבא.

---

## היסטוריה

| תאריך | שינוי | מאשר |
|---|---|---|
| 2026-05-23 | יצירה ראשונה (7 כללים, version mgmt, חריגים) | (ממתין לאישור) |
