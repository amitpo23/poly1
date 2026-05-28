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

## חוק 2 — Diff + Go + 4-Criteria Authorization

עודכן 2026-05-23 אחרי תקלה ב־commit שבו `git add <file>` בלע שינויים
pre-existing של agent קודם (Codex), והרחבת מנדט ל־"4-criteria standing
authorization" לבקשת המפעיל.

### 2.1 — Diff verification (חובה לפני כל commit)

לפני שינוי קוד / docs / config / .env:

1. ה־agent מבצע את ה־Edit.
2. ה־agent מריץ `git diff <file>` (לפני `git add`) ומאמת:
   - ה־diff מכיל בדיוק את השורות של ה־edit, **לא יותר**.
   - אם יש pre-existing changes (modifications של agent אחר או של המפעיל
     שלא נסקרו בסשן הנוכחי) — לעצור, להציג ל־operator, ולשאול:
     - (a) לכלול במקביל עם documentation בקומיט message
     - (b) Surgical stash + commit clean + restore pre-existing
     - (c) להשאיר uncommitted ולעבור הלאה
3. רק לאחר שה־diff נקי → `git add` → commit.

**חריגים שאינם דורשים אישור pre-write:**
- קריאה (Read tool, ls, cat, head).
- חקירה (grep, find).
- SQL **read-only** על שרת.
- TaskCreate / TaskUpdate (state ניהול פנימי).

### 2.2 — Standing Authorization for Goal-Aligned Changes

ה־agent יכול לבצע autonomously (בלי לעצור לאישור pre-write) שינויים
שמ־**clearly** עונים על כל ארבע מטרות הפרויקט:

1. **רווח ממסחר** — המטרה הסופית של ה־bot.
2. **בחינה שרכיבים טכניים עובדים** — כל מה שנכתב יבחן מולו.
3. **יכולת לבחון אסטרטגיות + לממש אותן** — testability + executability.
4. **winrate גבוהה + למידה** — feedback loops + measurement.

**תנאים לשימוש ב־standing authorization:**

- ה־agent חייב להיות **בטוח** שהשינוי עונה על כל 4 (לא 3 מתוך 4).
- חייב tag לפני שינוי (חוק 1).
- חייב diff מלא ב־commit message + הסבר איך זה עונה על 4 ה־criteria.
- חייב memory update אחרי commit.
- חייב session journal entry (חוק 4).

**אם אחד מתנאים אלו לא מתקיים, או יש ספק** → התנהגות 2.1: diff + go.

**שינויים שאסור לעשות תחת standing authorization (גם אם לכאורה goal-aligned):**

- שינוי runtime config שמשפיע ישירות על trading live (`RUNTIME_MODE`,
  `EXECUTE_*`, `HALT`).
- מחיקת קבצים או branches.
- `git push` (חוק 3 ממשיך כמו שהוא).
- שינוי SPEC.md או CLAUDE.md (דורש אישור מפורש).
- שינוי שמסיר invariants מ־CLAUDE.md.

**אסור (חוצה הכללים):** "תיקון קטן" שמבוצע "בלי לטרוח לאשר ובלי לתעד".

---

## חוק 3 — Deploy Gate

אין `git push` ל־remote בלי המפעיל רואה את ה־commits במפורש:

1. `git log <range> --stat` מוצג בצ'אט.
2. המפעיל אומר "push" / "deploy".
3. רק אז `git push`.

**אסור:** push אוטומטי, גם אם המפעיל אמר "go" ל־commit עצמו.

**Server is canonical:** `root@167.233.27.32:/srv/poly1` (alias `ssh poly1`) הוא source of truth ל־runtime. השרת הקודם (Kamatera `trader@83.229.82.193`) הוחלף ב-2026-05-28 — ראה `docs/HETZNER_SERVER_ACCESS.md` לרענון מלא.
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
   ssh poly1 'cd /srv/poly1 && git log -1 --oneline && \
     python3 scripts/runtime_control.py status && \
     ls -la data/HALT'
   ```
2. אם השרת לא ב־freeze, או HEAD/HALT/config_hash לא תואמים למה שהמפעיל זוכר —
   **עוצרים ושואלים.**
3. רק אם מאמתים — ממשיכים לעבודה החדשה.

**מטרה:** לא לעבוד בעיוור. אם משהו זז מאז הסשן הקודם — acknowledgment ראשון.

## חוק 8 — Live Runtime Truth Must Be Checked Inside Containers

נוסף 2026-05-27 אחרי live discovery שבו `.env.runtime` היה נכון, אבל
`docker-compose.yml` hardcoded ערך מנוגד ולכן הקונטיינר רץ עם gate שגוי.

לפני אמירה שהמסחר "עובד תקין" או שכל הסוכנים "פעילים", חובה לבדוק בשרת:

```bash
python3 scripts/runtime_control.py status
docker compose ps <services>
docker compose logs --since=5m --tail=120 trading-supervisor
docker compose exec -T <service> printenv | grep '<RELEVANT_ENV_PREFIX>'
```

אם בוצע hotfix לקוד שמורץ בקונטיינר, חובה לוודא שהקובץ באמת קיים בתוך
`/app/...` בקונטיינר:

```bash
docker compose exec -T <service> grep -n '<new-code-marker>' /app/path/to/file.py
```

כל position שנפתח live חייב supervisor-visible exit path:

- `position_manager` healthy.
- אין `data/HALT`.
- קיימת החלטת exit/HOLD ב־`brain_decisions` עבור הפוזיציה, או שה-supervisor
  מדווח `ok`.

אין להסיק "אין סיגנלים" רק כי אין עסקאות. קודם מסווגים את החסימה:
אין candidates, quality gate, learning guard, orderbook/EV, risk/HALT,
max-open, או compose override.

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
ssh poly1 'cd /srv/poly1 && git fetch && git checkout <tag>'
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
| 2026-05-23 | יצירה ראשונה (7 כללים, version mgmt, חריגים) | המפעיל |
| 2026-05-23 | חוק 2 הורחב: diff verification חובה + 4-criteria standing authorization | המפעיל |
| 2026-05-27 | חוק 8 נוסף: live runtime truth נבדק בתוך containers + exit-path evidence | המפעיל |
