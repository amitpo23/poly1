# PRE-LIVE QA REVIEW — poly1
**תאריך:** 2026-05-21
**סטטוס בוט:** post-launch (2026-05-03), `EXECUTE="false"` shadow mode
**מטרת המסמך:** סקירה עצמאית של נכונות, בטיחות כסף, ארכיטקטורה ומוכנות לפני `EXECUTE=true`. אינו תוכנית מימוש — אבחון.

---

## 0. Executive Summary — Go/No-Go

**המלצה כללית:** **NO-GO זמני** למעבר ל‑live בקפיטל הנוכחי ($100) או מעבר אליו, עד שיטופלו 4 חוסמים קריטיים (מפורט בסעיף 3). אחרי שיטופלו — go למסחר ב‑Stage-1 ($1–$5/עסקה) למשך 7 ימי shadow→live בקנה מידה מוגבל, ואז re-evaluate.

**הסטטוס שלך בקצרה:**
- ✅ ארכיטקטורה מהונדסת ומבוססת invariants כתובים (CLAUDE.md, SPEC.md, PREFLIGHT.md).
- ✅ ledger ב‑SQLite עם דדופ אמיתי, `MAY_HAVE_FIRED` שאינו נקרא אוטומטית, recover_stranded_pendings.
- ✅ הפרדת קפיטל ברורה לכל אסטרטגיה (X_RESERVE_USDC + RiskGate.available_for_trader).
- ⚠️ מערכת מאוד מורכבת (57 מודולים, ~10 קונטיינרים, ~150 env vars) — שטח‑תקיפה תפעולי גדול.
- ⚠️ כיסוי טסטים סביר ביחידה, חלש מאוד באינטגרציה בין‑קונטיינרים ותרחישי כשל.
- 🔴 4 חוסמים קריטיים: ראה סעיף 3.

---

## 1. Methodology

המסמך נכתב מ‑5 audits עצמאיים שרצו במקביל (read-only Explore agents), בתוספת עבודה עצמאית מתורים קודמים:

| Audit | היקף | טקטיקה |
|---|---|---|
| **Deploy & Ops** | docker-compose, Dockerfile, deploy/, .env.example, CI | קריאה מלאה + השוואת .env.example מול .env.runtime |
| **Trading Strategies** | trade.py + 10 אסטרטגיות + scanner/router | per-strategy review (edge, reserve, dedupe, side mapping, exit) |
| **Decision Pipeline** | meta_brain, market_brain, decision_council, meta_arbiter, EvidenceRouter, prompts, trading_policy, sizing | מיפוי זרימה + בדיקת invariants |
| **Money-Safety & Execution** | executor, exit_executor, position_manager, settlement_reconciler, resolution_sync, polymarket, execution_lock, risk_gate | זיהוי תרחישי race + double-fill + leak |
| **Test Coverage** | 47 קבצי טסט + CI + pre-commit | invariant-by-invariant coverage + matrix per‑module |

**הסתייגות חשובה:** חלק ממספרי השורות מהסוכנים לא אומתו ידנית — ודאתי באמתחת רק את הממצאים הכי קריטיים. סמן `[unverified]` במקומות שלא יכולתי לאמת אישית.

---

## 2. תיקון של ניתוחים קודמים בשיחה זו

לפני שאחווה דעות חדשות — מסמך זה מתקן שני שגיאות שעשיתי בתורים קודמים:

### 2.1 הלולאה‑מתה: רק חצי הייתה מתה

**בתור קודם טענתי:** "`update_brain_decision_outcome` ללא קוראים בפרודקשן → `brain_decisions.outcome_status` לעולם לא נכתב → הלולאה מתה לגמרי."

**מה שנכון:** `resolution_sync.py:_annotate_brain_decisions` כן קורא ל‑`update_brain_decision_outcome` בכל פעם ששוק נסגר ([resolution_sync.py:517-575](agents/application/resolution_sync.py#L517)). הטענה שלי נבעה מתבנית grep פגומה (הקריאה מתפרשת על מספר שורות והפילטר חיפש "update" על אותה שורה כמו "outcome_status").

**מה שעדיין נכון:** טבלת `decision_reflections` עצמה (לא `brain_decisions`) **כן** מתה בפרודקשן — אין קוראים שמייצרים רפלקציות LLM ואין שום פרומפט שמזריק לקחים. כלומר:
- ✅ **win-rate calibration loop**: חי. WinRateAdvisor יוכל לעבוד על `brain_decisions.outcome_status`.
- ❌ **LLM reflection-injection loop**: עדיין מת. אין הזרקת לקחים לפרומפט, ולכן אין למידה‑בין‑עסקאות ברמת ה‑LLM.

### 2.2 גודל ה‑codebase

טענתי `meta_brain.py ≈ 1484 שורות`. בפועל **2805 שורות**. הקובץ גדל פי‑2 וכולל היום `EvidenceRouter`, `EvidenceClaim`, מסלולי `solo`/`consensus`/`anchor`, וכל זוג ה‑`EXPERT_SOLO_*`/`EXPERT_CONFLICT_*` env vars. ה‑codebase שלך מתפתח מהר.

---

## 3. 🔴 CRITICAL — חוסמים לפני `EXECUTE=true`

מספור לפי סדר עדיפות לטיפול. אלה חוסמים = אסור להפעיל live בלעדיהם.

### C-1. `recover_stranded_pendings()` ב‑`TradeLog.__init__` → race startup חוסמת שווקים לנצח

**מקור:** [trade_log.py:453](agents/application/trade_log.py#L453) (קריאה ב‑`__init__`), [trade_log.py:317, 1693-1717](agents/application/trade_log.py#L317) (`MAY_HAVE_FIRED` חוסם ללא הגבלת זמן).

**התרחיש:** rolling deploy של ~10 קונטיינרים. כולם בו‑זמנית `__init__` → כולם קוראים `recover_stranded_pendings`. שורת PENDING שזמן רב הוצאה לעיבוד מסומנת `MAY_HAVE_FIRED` על‑ידי קונטיינר A. רגעים מאוחר יותר ה‑order באמת fillים על‑שרשרת. השורה נשארת `MAY_HAVE_FIRED` לנצח, חוסמת כל מסחר עתידי על אותו שוק. מצריך התערבות אופרטור ידנית בכל פעם.

**למה זה חוסם:** הזרם הזה יכול להיתפס בלולאה של "restart→חוסם שוק→restart→חוסם שוק נוסף" עד ש‑5–10 שווקים חסומים והבוט נראה תקין אבל לא נכנס לכלום.

**מה לעשות:** להעביר את הקריאה ממוקם `__init__` למקום אחד — או startup hook מבוקר עם file-lock, או daemon ייעודי שמריץ זאת באופן תקופתי. בנוסף, להמיר את ה‑auto-mark מ‑`MAY_HAVE_FIRED` למשהו זמני (`PENDING_REVIEW`) שמופעל רק אם query פעיל ל‑CLOB נכשל בלזהות את ה‑order.

---

### C-2. הלולאה‑חצי‑חיה אומרת ש‑calibration קיים — אבל אין לזה טסטים בפועל

**מקור:** [resolution_sync.py:561](agents/application/resolution_sync.py#L561), [trade_log.py:1501](agents/application/trade_log.py#L1501); אין בטסטים שום integration שמוודא ששורות `brain_decisions.outcome_status` באמת מקבלות ערך בעת סגירה.

**למה זה חוסם:** WinRateAdvisor עובד תחת הנחה שיש דאטה. אם `resolution_sync` נכשל בשקט (RPC timeout, market mismatch, sync rate slow) — אף אחד לא ידע. תוצאה: MetaBrain ימשיך להראות `winrate_source="trades"` (fallback גס) במקום `"brain_decisions"`, אבל בלי שאף התראה תקפוץ. הקליברציה תיראה תקינה אבל לא תהיה.

**מה לעשות:** טסט אינטגרציה שמסמל: כניסה→מילוי→`brain_decisions` נכתב→שוק נפתר→`resolution_sync._annotate_brain_decisions` רץ→`outcome_status` ≠ NULL. בנוסף, התראה תפעולית: אם אחוז `brain_decisions` עם `outcome_status IS NOT NULL` נופל מתחת ל‑X% בחלון 7 ימים → התראה.

---

### C-3. Tenacity retry — חסר טסט שמוודא שאינו retryמ‑HTTPError

**מקור:** [polymarket.py:968-980](agents/polymarket/polymarket.py#L968) — הקוד נראה תקין (retries רק על `httpx.TimeoutException`, `httpx.NetworkError`, `requests.Timeout`, `requests.ConnectionError`), אך **אין שום טסט שמוודא זאת**. CLAUDE.md מסמן זאת invariant קריטי כי הוספת `HTTPError` או `Exception` תוביל ל‑double-fill על FOK fills.

**למה זה חוסם:** ה‑invariant הוא הגנה היחידה מ‑double-fill. ללא טסט, גרסה עתידית של מישהו ש"ירחיב את ה‑retry" כדי לטפל ב‑500 transient תהרוס אותו בלי שאף אחד ישים לב. PR שמוסיף `HTTPError` לרשימה יעבור CI.

**מה לעשות:** טסט שמעלה `httpx.HTTPError` בתוך `_post()` של ה‑mock CLOB ומוודא שהפונקציה נכשלת **בלי retry** (מספר קריאות = 1, לא 3). אותה הגנה ל‑`requests.HTTPError`, `ValueError`, `Exception`.

---

### C-4. הפרדה לא‑מסונכרנת בין `EXECUTE` ו‑`EXECUTE_MAINTAIN` — מצב "כניסה‑אבל‑בלי‑יציאה"

**מקור:** [trade.py:820-843](agents/application/trade.py#L820) (Trader.maintain_positions stub), position_manager הוא קונטיינר עצמאי ב‑profile `positions`, כל אסטרטגיה (btc_daily/news_shock/near_resolution/wallet_follow/trader) מאצילה יציאות ל‑position_manager.

**התרחיש:** אופרטור מפעיל `EXECUTE_BTC_DAILY=true` אבל שוכח לאתחל `EXECUTE_MAINTAIN=true` (או profile `positions` לא רץ). אסטרטגיות הכניסה ימשיכו לפתוח פוזיציות. position_manager לא ירוץ. הפוזיציות לעולם לא ייצאו — עד שהשוק יתפרק.

**למה זה חוסם:** אין circuit-breaker. אין assertion ב‑boot שמוודא שלכל סוכן‑כניסה חי קיים position_manager חי באותה wallet. ה‑heartbeat‑monitoring קיים אבל **לא הופך לחסימת כניסות**.

**מה לעשות:** שני שינויים. (א) ב‑startup של כל סוכן כניסה: אם `EXECUTE_X=true`, לבדוק `data/heartbeat_position_manager` < N דקות → אחרת לסרב להעלות. (ב) trading_supervisor יחסום entries חדשות אם position_manager_heartbeat ישנה מ‑180s. בקצור — לא לתת לבוט לפתוח כשידוע שאי אפשר לסגור.

---

## 4. 🟠 HIGH — חשוב מאוד לפני live, אבל ניתן לתת זמן של ימי shadow לתיקון

### תחום: Money-Safety / Execution

**H-1. Polymarket(live=True) ב‑settlement_reconciler** — [settlement_reconciler.py:509](agents/application/settlement_reconciler.py#L509) [unverified line — אמת לפני תיקון]. ה‑reconciler רק קורא state ולא שולח orders, אז `live=True` מיותר ויוצר API-key creation מיותר וסיכון crash אם derivation נכשלת. אמור להיות `live=False` בהתאם ל‑invariant ב‑CLAUDE.md.

**H-2. אין בדיקת allowance/approval לפני שליחת order** — [polymarket.py:877-900](agents/polymarket/polymarket.py#L877). אם USDC/CTF allowance נשלל (יכול לקרות ידנית ב‑MetaMask של האופרטור), ה‑CLOB יחזיר 400. הקוד יסמן FAILED אבל בלי דיאגנוסטיקה ברורה. כל הכניסות יכשלו בשקט עד שאופרטור יזהה. תיקון: בדיקת `allowance()` עם cache 60s לפני submit.

**H-3. FAK partial fills לא מטופלים כראוי** — [exit_executor.py:45-84](agents/application/exit_executor.py#L45). FAK יכול להחזיר match חלקי. הקוד מסמן closure מלאה גם כאשר רק 10% מהמניות נמכרו. ה‑on-chain balance לא נסגר אבל ה‑journal חושב שכן. dust-override של position_manager בסופו של דבר תופס זאת אבל עם פיגור.

**H-4. SQLite WAL + busy_timeout=10s לא מספיק תחת עומס כתיבה רב‑קונטיינרי** — [trade_log.py:457](agents/application/trade_log.py#L457). תחת 10+ writers בו‑זמנית checkpoint יכולה לבלום הכול. השאר את ה‑schema אבל הגדל ל‑30s ושקול `PRAGMA wal_autocheckpoint=1000`. שקלול ארוך‑טווח: serializ‑proxy של כתיבות.

**H-5. RiskGate per-market check מקפיץ פעם אחת בסוף ה‑cycle בלבד** — [scanner_executor.py:397-400](agents/application/scanner_executor.py#L397). החלטה שעברה בתחילת cycle יכולה לרוצץ את הסף עד submit. תיקון: לקרוא ל‑`risk_gate.ok()` שוב מיד לפני `execute_market_order`. (קיים ב‑trade.py הראשי, חסר במסלולים אחרים.)

**H-6. position_mtm caching לחלוקת 60s** — [risk_gate.py:165-218](agents/application/risk_gate.py#L165). MTM stale → drawdown חורג עד 60 שניות בלי שיופעל kill switch. הקטן ל‑10s או invalidate ב‑every entry check.

**H-7. סוג Scalper לא קורא ל‑RiskGate.ok()** — Scalper מנהל reserve משלו (`SCALPER_RESERVE_USDC`) אבל מ‑audit הסוכן: אין קריאה מפורשת ל‑RiskGate. תיאורטית 20 pairs ב‑$5 כל אחד = $200 שיכול לחרוג מ‑$14 reserve. תיקון: לוודא ש‑`ScalperEngine.place_leg()` קורא ל‑risk_gate.ok() או בודק reserve‑balance עצמאית.

### תחום: Decision Pipeline

**H-8. משקלי MetaBrain לא מנורמלים ל‑1.0** — [meta_brain.py — section EvidenceRouter weights, unverified exact line]. הסוכן מצא ש‑סכום משקלי ברירת‑מחדל ≈ 1.38. תחת informed-only normalization ([meta_brain.py:2459-2473](agents/application/meta_brain.py#L2459)) זה גורם להטיה לעבר רכיבי הניקוד בעלי המשקל הגבוה. אמת את הסכום במזומן, ואז או נרמל ל‑1.0 או תעד למה האסימטריה מכוונת.

**H-9. anchor logic — score override לפני EV gate** — [meta_brain.py:2484-2598](agents/application/meta_brain.py#L2484). כש‑`route.mode == "solo"`, ה‑score מוחלף ב‑`route.probability`. סף `min_weighted_score_with_anchor=0.40` יותר חופשי. EV gate בהמשך כן יסנן את הגרועים, אבל הסדר הזה מאפשר שורות החלטה רועשות לעבור את הסף הראשון. תיקון: validate EV לפני שמשחררים את סף ה‑score.

**H-10. `META_BRAIN_ANCHOR_THRESHOLD` נקרא אבל לא משפיע** — [meta_brain.py:2479](agents/application/meta_brain.py#L2479). הסוכן מצא ש‑`legacy_anchor_candidate` מחושב ואינו נצרך; ה‑anchor האמיתי מסונן ב‑`EvidenceRouter._is_solo_eligible` עם `EXPERT_SOLO_*` env vars. אופרטור שיכוון `META_BRAIN_ANCHOR_THRESHOLD` יחשוב שהוא משנה התנהגות אבל לא. או למחוק את ה‑var, או לחבר אותו פיזית כ‑hard floor.

**H-11. CrossMarketSignalFeed שותק כשל API** — [market_brain.py:333-397](agents/application/market_brain.py#L333). אם 2 מתוך 3 (Kalshi/Metaculus/Manifold) נכשלים, ה‑"consensus" מחושב ממקור יחיד אבל מסומן `fresh=True`. הבוט יסמוך עליו כעל אמת. תיקון: להחזיר n_sources ולסרב consensus אם < 2.

**H-12. Kelly sizing edge cases — אין טסטים** — [sizing.py:62-115](agents/application/sizing.py#L62). הקוד עצמו מטפל בקצוות אבל אין קובץ test_sizing.py. תיקון: לכתוב טסט עם property-based ל‑(p, c) על תחום `[0,1]²`, לבדוק שהפלט ב‑`[0, balance]` תמיד.

### תחום: Deploy & Ops

**H-13. allocator-sync כותב ל‑.env בלי atomic write** — קונטיינר ה‑allocator עושה bind‑mount של `.env:rw` ([docker-compose.yml:304-311](docker-compose.yml#L304) [unverified line]). כתיבה‑חלקית עקב crash תהרוס את `.env`. תיקון: write-to-tmp ואז `os.replace()` (atomic על POSIX). אלטרנטיבה עדיפה: לאחסן הקצאות ב‑`data/allocations.json` ולקרוא משם.

**H-14. EXECUTE נקרא פעם אחת ב‑startup, לא מנוטר ב‑runtime** — [deploy/run.py:44](deploy/run.py#L44). אופרטור שמשנה `.env` ל‑`EXECUTE=false` ב‑emergency ולא עושה `docker compose down` — הקונטיינר הקיים ימשיך לעבוד עם הדגל הישן. תיעוד+טסט: היחיד שעוצר ידנית הוא `touch data/HALT`.

**H-15. אין retention/rotation מתועד ל‑data/trade_log.db** — הקובץ 11MB אחרי שבועיים. ב‑$100 bot זה בסדר אבל בלי backup‑restore drill מתועד אין הוכחה שניתן לשחזר. backup_trade_log.py קיים — חסר drill שמשחזר ומוודא קונסיסטנטיות.

**H-16. Drift בין `.env.example` ל‑`.env.runtime`** — Audit מצא לפחות 5 vars ב‑runtime שאינם ב‑example. SPEC.md דורש סנכרון. תיקון: pre-commit hook שמשווה.

### תחום: Strategies

**H-17. Scalper HTTP/2 robustness לא הוכח‑עומס** — fix של per-request client rotation נוסף 2026-05-08 אבל לא עבר 72h stress עם 20+ pairs. תיקון: shadow test עם MOCK_HIGH_FREQ profile למשך 72h, אפס 404/connection-drop בלוג.

**H-18. BTC_5Min — window יציאה צר** — `max_hold_seconds=120` אבל `MAINTAIN_POLL_SEC=60`, אז במקרה הגרוע יציאה מתבצעת על המסגרת. אם liquidity יבש, settlement clash. תיקון: לבחון העברת לוגיקת יציאה לתוך btc_5min.py עצמו (45s pre-resolution forced exit).

**H-19. News_Shock — אין SLA latency** — Edge הוא 30 דקות; אם news_signal+Tavily מוסיפים 15‑20 דקות, ה‑edge מתאיין. אין מדידה. תיקון: instrument end-to-end (headline_ts → entry_ts), אם > 15 min → abort.

**H-20. Wallet_Follow — מקור הארנקים לא מתועד** — registry מסמן `live_candidate`. איך נבחרים הארנקים? מה מקור profit data? איך מתעדכנים? בלי תיעוד, סיכון "לעקוב אחר ארנק מת" קיים.

---

## 5. 🟡 MEDIUM / LOW — שדרוגים שלא חוסמים live אבל ישפרו

| # | קטגוריה | ממצא | קובץ |
|---|---|---|---|
| M-1 | Decision | טסט מפורש לטרנספורמציית מחיר SELL (`1 − price`) חסר | test_btc_5min, test_polymarket_fak |
| M-2 | Decision | טסט dual-call של RiskGate.ok() באותו cycle חסר | test_trader |
| M-3 | Decision | טסט cross-agent token_id dedupe (אגנט A קונה YES, אגנט B מנסה) | test_trader, test_external_conviction |
| M-4 | Decision | טסט escalation אחרי N `close_failed` (קיים supervisor logic, לא נטסט) | test_position_manager, test_trading_supervisor |
| M-5 | Money | אין הבחנה בשגיאות CLOB בין "rejected by exchange" ל‑"network error" | polymarket.py:976 |
| M-6 | Money | resolution_sync מניח on-chain‑balance כ‑ground-truth, מטפל ב‑RPC error כ‑"still held" → phantom positions נתקעים | resolution_sync.py:171-175 |
| M-7 | Ops | אין Prometheus/structured metrics — כל observability מבוססת logs ו‑telegram | infra-wide |
| M-8 | Ops | אין rollback אוטומטי ב‑deploy.sh, צריך checkout ידני | deploy/deploy.sh |
| M-9 | Ops | אין secret-scanning ב‑CI (גם לא pre-commit) | .github/workflows |
| M-10 | Ops | אין NTP-sync check; clock drift > 5min יגרום לכל timestamps של CLOB להידחות | container init |
| M-11 | Ops | אין rate-limit fallback ל‑OpenAI/Anthropic (קיים fallback אבל לא backoff מותאם) | meta_brain hermes_forecast |
| M-12 | Strategies | Trader דדופ של 6h ו‑reentry של 12h — חופפים? הסיכון של reentry "באחורי 12h" וtrader רואה הזדמנות חוזרת | trade.py:340-376 |
| M-13 | Strategies | Opportunity Router gates Trader entries — לא בוצע A/B vs router off; ייתכן שמוריד win-rate | trade.py:541-565 |
| M-14 | Strategies | Capital aggregation: סך הרזרבות ≈ 68 USDC מתוך 100 → Trader נשאר עם 32. אם כל הקונטיינרים פעילים בו‑זמנית, מצב contention | risk_gate.available_for_trader |
| L-1 | Money | wallet_follow + אגנטים אחרים מאתחלים `Polymarket(live=True)` עצמאית — latency מיותרת | wallet_follow.py:608 |
| L-2 | Ops | secrets ב‑docker inspect לכל מי שיש לו SSH ל‑VPS — נכון לכל docker-deployment אבל ראוי לתעד ב‑PREFLIGHT | docker-compose env_file |

---

## 6. ✅ מה מצוין — אל תאבד את זה ברפקטור

ייחודיות חיובית של הפרויקט הזה מול חברים בקטגוריה (כולל הריפו [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) שבחנו קודם):

1. **Ledger‑first idempotency.** דפוס `insert_pending → mark(FILLED) → mark(closed_*)` עם `MAY_HAVE_FIRED` שאינו נקרא אוטומטית הוא בדיוק הדבר הנכון לבוט שמזיז כסף. הרבה bots פותחים פוזיציות בלי ledger אטומי. אצלך זה הליבה.
2. **invariants מתועדים.** CLAUDE.md מציין במפורש "אל תפר את אלה" (side mapping, ACTIVE_STATUSES, tenacity scope, RiskGate double‑call, live=False). זה מצוין — רוב הפרויקטים מסתמכים על "כל מתחזק חדש יבין מהקוד."
3. **שכבת brain עם evidence provenance מלא.** כל סיגנל ב‑MetaBrain מתויג ב‑source/age/confidence. ניתן לבצע forensics לכל החלטה.
4. **EvidenceRouter עם reliability thresholds מובנים.** `EXPERT_SOLO_MIN_WILSON` ו‑`EXPERT_CONFLICT_MIN_SAMPLES` הם הגנה אמיתית מ‑experts לא‑מכוילים שיופעלו כ‑anchors. רוב המערכות עושות naive averaging.
5. **DecisionCouncil מאמת EV אחרי המסלול הראשי.** "ההחלטה עברה את ה‑score אבל לא את ה‑EV" אינה אפשרית בקוד שלך. שני שערים בלתי‑תלויים.
6. **MarketBrain — שכבת gate ללא LLM.** rule‑based, מהיר, ניתן לטסטים דטרמיניסטיים, לא יוצר עלות. אבסולוטית הדבר הנכון.
7. **דדופ מבוסס ledger עם cross-agent token_id matching** ([trade_log.py:622](agents/application/trade_log.py#L622)). תיקון "Trader רואה market_id, external_conviction רואה token_id" שיכול היה לחזור ולעקוץ — תוקן בצורה אמיתית.
8. **execution_lock עם file‑lock רב‑קונטיינרי** ([execution_lock.py:16-30](agents/application/execution_lock.py#L16)). שני קונטיינרים לא יכולים לשלוח orders בו‑זמנית. דבר נדיר במערכות בקטגוריה.
9. **resolution_sync עם payout reconciliation אמיתי** ([resolution_sync.py:485-515](agents/application/resolution_sync.py#L485)). תקשורת ל‑ledger של ה‑P&L הממומש, לא הסתמכות על cost basis.
10. **per-strategy reserves כ‑hard constraint.** RiskGate.available_for_trader מחסר את כל הרזרבות. אגנט לא יכול לאכול קפיטל של אגנט אחר. ארכיטקטורה נכונה.
11. **כיסוי tests יחידה צמוד למודולים קריטיים.** test_trader, test_market_brain, test_settlement_reconciler, test_position_manager — כולם משתמשים ב‑SQLite אמיתי, לא mock-only. בכלל את הקריטיקה (כיסוי אינטגרציה חסר) זה עדיין מצוין ביחס לסטנדרט.
12. **תיעוד ברמת SPEC.md + PREFLIGHT.md + CHANGELOG + CURRENT_STATUS.** מקצועי. נדיר.
13. **Dual‑bot architecture עם הפרדה מוחלטת.** הזרמה לארנק נפרד, ledger נפרד, container נפרד, decision חוצה‑בוטים מתועדת ב‑OPERATIONS.md. סיכון contagion מינימלי.

---

## 7. הביקורת שלי — מבט מעל

אלה דעות, לא ממצאים. קרא בהתאם.

### 7.1 מורכבות גבוהה מדי לבוט של $100

57 מודולים, 10 קונטיינרים, ~150 env vars, 47 קבצי טסט, EvidenceRouter עם 5+ thresholds, anchor logic, Kelly sizing, decision_council, meta_arbiter, trading_supervisor, hermes_forecast, news_signal, news_shock כסוכן נפרד, external_conviction עם 17+ providers. **זה לא בוט $100, זו תשתית $100k.**

המורכבות הזו לא רעה כשלעצמה אבל יוצרת בעיה ספציפית: **שטח‑התקיפה התפעולי גדל מהר יותר מהיכולת לאמת.** אופרטור יחיד לא יכול להחזיק במהירות בראש איזה env var מגדיר מה, ואיזה דגל מבטל איזה. ה‑bus factor הוא 1 ושטח התקיפה מתאים ל‑10. זה לא race-bug — זה risk-bug.

**הצעה (לא דחופה):** מסמך אחד "Architecture@2026-05-21" שמפרט את ה‑critical path בלבד (3‑5 רכיבים שאם אחד נשבר אז כסף נעלם), ואת השאר מסמן "experimental/research." בנוסף, prune ל‑agnets שאינם מספקים evidence-profitable signal תוך 30 ימים.

### 7.2 חוסר וודאות לגבי איזו אסטרטגיה באמת רווחית

ה‑GOAL_PROFITABLE_AGENT_LOOP מתועד אבל הסטטוס הרב‑אסטרטגי בפועל מעורפל. החלוקה ב‑audit:
- **`live_candidate`:** wallet_follow, position_manager.
- **שאר אסטרטגיות הכניסה:** "exploratory" / "unvalidated" / "not in agent_registry."

זה אומר ש‑5+ אסטרטגיות שמסמנות EXECUTE_X=true עוברות ל‑live בלי שכבת validation פורמלית. CLAUDE.md אומר "$100 bot... Stage-1 trade size $1-$5", שזה נכון — אבל זה לא מחליף שאלה: **איזה מהאסטרטגיות הוכיחה edge חיובי בנתונים שלך?** בלי תשובה רשומה ומדידה, הקפיטל יתפזר בין winners ו‑losers ללא יכולת ניהול.

**הצעה:** לפני flip ל‑EXECUTE=true, להפעיל את ה‑allocator עם sample size מינימלי (n=10 outcomes כל אסטרטגיה) ולעצור כל אסטרטגיה שמתחת לסף, גם אם זה אומר שמתחילים רק עם 1–2 אסטרטגיות.

### 7.3 הפער בין tests יחידה ל‑tests אינטגרציה רב‑קונטיינרית

הטסטים מצוינים ברמת מודול. **אין** טסט שמדמה את התרחיש: "Trader פותח position → position_manager בנפרד רואה את ה‑fill → exit_executor שולח sell → settlement_reconciler מסנכרן on-chain → resolution_sync כותב outcome_status." זה ה‑path היחיד שמשנה כסף בפרודקשן, ואין לו test.

ה‑audit מצא race conditions ספציפיות (סעיף 4 — H-1, H-3, H-5; וסעיף 3 — C-1). כולן לא יכולות להיתפס על‑ידי unit tests. נדרש docker-compose integration suite. זה השקעה של ימי‑עבודה, אבל ערכה גדל ב‑order-of-magnitude מעל unit tests נוספים.

### 7.4 הסתמכות על "LLM ידע בקצב" בנתיב שמזיז כסף

יש מספר נקודות שבהן LLM נקרא בתוך החלטות שמשפיעות על כסף (Hermes forecast, position_manager evaluate_exit, meta_brain straddle llm fallback). זה לא בהכרח רע, אבל יוצר תלות בלתי‑דטרמיניסטית. אם OpenAI/Anthropic נופלים או מתגרים, ההתנהגות יכולה להיות:
- אגנטים נכנסים בלי brain approval (אם fallback רופף)
- position_manager לא יכול לפנות exit (אם dependent)
- אגנטים נחסמים לגמרי (אם fail-safe)

האם זה ה‑intent? בכל אחד מהמקומות, אמורה להיות החלטה מפורשת על default behavior כש‑LLM למטה.

### 7.5 רמת ה‑observability לא מתאימה למורכבות

logs + telegram + healthchecks.io טובים לבוט יחיד או 2 אסטרטגיות. ל‑10 קונטיינרים אינטראקטיביים עם ledger משותף — חסר metrics‑backend. השאלה "כמה decisions נחסמו השבוע ע"י gate X" אמורה להיענות ב‑30 שניות. כרגע — לא ניתן בקלות.

**הצעה (לא לפני live, אבל בקרוב):** Prometheus exporter פשוט שקורא את ה‑SQLite ומוציא counters: brain_decisions_total{agent, approved, reason}, trades_total{status}, risk_gate_blocks_total{reason}. Grafana dashboard 1‑עמוד.

---

## 8. רשימת Pre‑Live Go/No‑Go

לפני flip ל‑`EXECUTE=true`, לעבור על הרשימה הזו לפי הסדר. כל אחד מהם מבוטל = NO-GO.

### 8.1 חוסמים אבסולוטיים (סעיף 3)
- [ ] **C-1.** העברת `recover_stranded_pendings` ממוקם `__init__` למקום מבוקר/leadered.
- [ ] **C-2.** טסט אינטגרציה: כניסה→fill→שוק‑נסגר→`brain_decisions.outcome_status` ≠ NULL. + התראה תפעולית על אחוז annotation.
- [ ] **C-3.** טסט שמוודא ש‑`execute_market_order` **אינו** retryמ‑HTTPError/generic Exception (max 1 attempt).
- [ ] **C-4.** assertion ב‑startup של כל agent_X: אם EXECUTE_X=true → position_manager_heartbeat < N min, אחרת abort.

### 8.2 חשובים מאוד (sub‑set מסעיף 4)
- [ ] **H-1.** `Polymarket(live=False)` ב‑settlement_reconciler.
- [ ] **H-3.** טיפול ב‑FAK partial fills: לבדוק matched amount, לעדכן shares ב‑position, לסמן closed רק אם matched ≥ remaining − ε.
- [ ] **H-4.** `busy_timeout` ל‑30s + `wal_autocheckpoint=1000`.
- [ ] **H-12.** `test_sizing.py` עם kelly edge cases.
- [ ] **H-13.** allocator-sync atomic write ל‑`.env`.

### 8.3 ולידציה תפעולית (לא קוד — drill)
- [ ] **D-1.** Backup‑restore drill: שחזור מ‑`./data/backups/` ל‑instance נפרדת, וידוא קונסיסטנטיות.
- [ ] **D-2.** Kill‑switch drill: `touch data/HALT` באמצע cycle; וידוא שכל הקונטיינרים מפסיקים entries בתוך < 60s.
- [ ] **D-3.** Network‑outage drill: blokc Polymarket מ‑iptables ל‑10 דקות; וידוא שאף MAY_HAVE_FIRED לא נוצר, ושאף phantom position לא נשאר.
- [ ] **D-4.** Multi-container shadow load: 48h עם 10x market rate ב‑shadow; ספירת DB locks ב‑lo gs (אמורים להיות 0).
- [ ] **D-5.** End-to-end shadow: cycle אחד ב‑EXECUTE=false, וידוא ש‑resolution_sync רץ ו‑`brain_decisions.outcome_status` מתעדכן על שוק שנפתר ב‑shadow.

### 8.4 קיימות (לא חוסמים, אבל לתעד)
- [ ] רשימת אגנטים מאושרים ל‑live (לפחות 1 עם sample size ≥ N).
- [ ] רשימת אגנטים disabled (`EXECUTE_X=false`).
- [ ] תיעוד יחיד "מה לעשות אם …" (network down / DB locked / position stuck / allowance revoked / wallet drift).

---

## 9. שאלות פתוחות שדורשות אימות אופרטור

הסוכנים והניתוח שלי הם read-only — לא הרצתי שום דבר. הנקודות הבאות דורשות אימות שלך לפני קבלת החלטה:

1. **האם `EXECUTE_SCANNER_EXECUTOR` נוסף ל‑.env.runtime?** ה‑audit מצא שזה var שייתכן שצריך להיות שם אבל אינו. אם אינו → scanner רץ אבל executor לא יורה.
2. **כמה `MAY_HAVE_FIRED` שורות קיימות כרגע ב‑`./data/trade_log.db`?** `SELECT COUNT(*) FROM trades WHERE status='may_have_fired'` — אם > 0, יש markets חסומים שצריך לפתוח ידנית לפני live.
3. **כמה `brain_decisions` יש עם `outcome_status IS NULL` בני 7+ ימים?** אם הרבה → resolution_sync לא מצליח לעמוד בקצב, ולכן WinRateAdvisor יעבוד על fallback לא מדויק.
4. **האם `EXECUTE_MAINTAIN=true` בשעת flip?** ראה C-4.
5. **CURRENT_STATUS.md (77KB) מתעד 3 proofs.** מה הצליח? מה לא? יש open issues שלא נכנסו לסעיף הזה?
6. **agent_registry.json — מי כרגע `live_candidate` ומי `shadow`?** אם כל האגנטים `live_candidate` באותו זמן, ניהול הסיכון מורכב; אם רק 1–2, יותר בטוח לשלב הראשון.

---

## 10. סיכום

הבוט שלך מהונדס היטב לתחום שלו. תקני האיכות גבוהים יחסית: ledger אטומי, invariants כתובים, evidence provenance, EV gating, dual‑agent isolation. ההישגים האלה לא טריוויאליים.

**מה שמעקב flip ל‑live:** 4 חוסמים אמיתיים (סעיף 3), בעיקר סביב טסטים אינטגרטיביים שאינם קיימים ל‑critical paths, ו‑race condition אחת ב‑startup שיכולה לחסום markets לנצח.

**מה שצריך לשפר עוד 30 יום אחרי flip:** validation discipline ברמת אגנט (איזה אגנט מאושר? למה?), reduction של מורכבות (האם 17 conviction providers באמת תורמים?), observability מודרני (Prometheus/Grafana), ו‑drills תפעוליים מובנים.

**ההמלצה הסופית:** טפל ב‑4 ה‑CRITICAL וב‑5 ה‑HIGH הראשונים. אחר כך הפעל 48h shadow chaos drill. אם עובר — flip עם Stage-1 ($1–$5/trade) למשך 7 ימים על אגנט יחיד מאומת. אחר כך scale.

---

**הערה:** המסמך הזה לא נוגע בקוד. הוא לא תוכנית מימוש. הוא אבחון. השלב הבא, אם תרצה — להפוך את ה‑checklist בסעיף 8 לטסקים בני‑מימוש, להתחיל מ‑C‑1 או C‑3 (זולים, בעלי השפעה גבוהה).
