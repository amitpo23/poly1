# Scalper Strategy C — Implementation Log

**Tag:** `v0.3.0-scalper-built`  
**תאריך:** 2026-05-05  
**סטטוס:** Stage 0 (shadow mode) — מוכן להפעלה

---

## רקע

הסקאלפר הוא agent מסחר עצמאי שני בתוך poly1. הוא פועל ללא LLM, תוקף
שווקי up/down בפוליימרקט, ומבצע reversals קצרים. הוא **לא** מחובר ל-Trader
ולא חולק איתו לוגיקה — שיתוף הוא רק SQLite ו-wallet.

---

## קבצים שנוצרו / שונו

| קובץ | סטטוס | תיאור |
|------|--------|-------|
| `agents/application/scalper_pairs.py` | חדש | DAO ל-`scalper_pairs` — CRUD + state machine |
| `agents/application/scalper.py` | חדש | `ScalperConfig`, `ScalpPair`, `ScalperEngine`, `ScalperDaemon` |
| `agents/application/trade_log.py` | שונה | טבלת `scalper_pairs`, WAL mode, `SCALPER_LEG` status |
| `agents/application/risk_gate.py` | שונה | `scalper_reserve_usdc`, `available_for_trader()` |
| `agents/polymarket/polymarket.py` | שונה | פרמטר `order_type` ב-`execute_market_order` |
| `scripts/python/scalper_inspect.py` | חדש | CLI לבדיקת מצב שוטף |
| `tests/test_scalper.py` | חדש | 14 בדיקות — ScalpPair logic |
| `tests/test_scalper_pairs.py` | חדש | 8 בדיקות — DAO |
| `tests/test_scalper_engine.py` | חדש | 18 בדיקות — engine tick/leg/shadow/reap |
| `tests/test_scalper_daemon.py` | חדש | 1 בדיקה — daemon stop signal |
| `docker-compose.yml` | שונה | שירות `scalper` עם `profiles: ["scalper"]` |
| `.env.example` | שונה | משתני סביבה חדשים לסקאלפר |
| `SPEC.md` | שונה | §15 — Scalper module |
| `CLAUDE.md` | שונה | הנחיות לסקאלפר |
| `docs/SCALPER_STAGE_0_RUNBOOK.md` | חדש | runbook הפעלה |

**סה"כ קוד:** ~1,300 שורות (ייצור + בדיקות)  
**בדיקות:** 58 עוברות

---

## ארכיטקטורה

```
ScalperDaemon (main loop, every poll_ms)
    ├── discover_markets()        ← Gamma API, מסנן updown-15m
    ├── ScalperEngine.tick()      ← per-pair
    │       ├── entry trigger     ← reversal + depth + profit gate
    │       ├── place_leg()       ← CLOB FAK order (execute=True)
    │       └── shadow log        ← SCALPER_LEG row (execute=False)
    ├── reap_expired()            ← state cleanup per period end
    └── heartbeat write           ← /app/data/scalper_heartbeat
```

### State machine של pair

```
TRACKING → LEG1_FILLED → LEG2_FILLED (terminal)
                       ↘ RECONCILE_NEEDED (terminal, operator clears)
         ↘ EXPIRED (terminal)
         ↘ RECONCILE_NEEDED (expired while LEG1_FILLED)
```

---

## commit-by-commit

| SHA | תיאור |
|-----|-------|
| `0d11659` | `scalper_pairs` table + `SCALPER_LEG` + WAL |
| `af2f30b` | `ScalperPairsDAO` — state machine + tests |
| `faecf16` | תיקונים ל-DAO — rowcount guards |
| `af77104` | `ScalpPair` + `ScalperConfig` defaults |
| `90ea363` | entry trigger — eligibility + reversal + depth |
| `83a0d1c` | profit gate — `sum_avg <= max_sum_avg` |
| `f761ec0` | second-leg trigger — dyn threshold + 200ms timer |
| `e71eb16` | הסרת epsilon מ-reversal trigger, הוספת side guard |
| `63b56f0` | `discover_markets` — Gamma scan, מסנן updown-15m |
| `a125304` | `ScalperEngine.place_leg` + audit row ב-trade_log |
| `62ef3b4` | tick orchestration + rate limit gate |
| `e016b3e` | guard double-fill + canonical token_ids + FAK import guard |
| `a8feedc` | `SCALPER_RESERVE_USDC` ב-RiskGate |
| `7c5636e` | `TestScalperRateLimit` — rate gate coverage |
| `74fcfe1` | restart reconciliation — `LEG1_FILLED` → `RECONCILE_NEEDED` |
| `b0b33ae` | `reap_expired` + `TestShadowMode` + `TestReapPeriod` |
| `0b110ef` | תיקון קריטי: `reap_expired` מגן על `RECONCILE_NEEDED` |
| `521ad70` | `ScalperDaemon` — SIGTERM-aware main loop |
| `ee72204` | תיקון: `_book_client` נפרד, signal safety, exception guards |
| `c25b264` | docker-compose service + `scalper_inspect` CLI |
| `ed0079b` | `SPEC.md` §15 + `.env.example` + `CLAUDE.md` |
| `d34085e` | Stage-0 runbook |

---

## באגים קריטיים שתוקנו

### 1. `reap_expired` מוחק פוזיציות on-chain ללא הזהרה
**בעיה:** הקוד המקורי קרא `set_state(slug, EXPIRED)` לכל pair פתוח, כולל
`LEG1_FILLED`. Row שעבר ל-EXPIRED יוצא מ-`list_open()` — האופרטור לא ידע
שיש פוזיציה קיימת ב-CLOB.  
**תיקון:** `LEG1_FILLED` → `RECONCILE_NEEDED` + log warning. `RECONCILE_NEEDED`
לא נדרס לעולם — זה analog של `MAY_HAVE_FIRED` בטריידר.

### 2. Shadow mode שקט לחלוטין
**בעיה:** `Polymarket(live=False).client = None`. הדמון קרא
`self.client.client.get_order_book()` → `AttributeError` נבלע ב-try/except.
ה-trigger לא הופעל אף פעם.  
**תיקון:** `_book_client` נפרד — `ClobClient(host=..., chain_id=137)` ללא
credentials לקריאת order book ציבורי.

### 3. Signal handlers ב-`__init__`
**בעיה:** `signal.signal()` מעלה `ValueError` ב-non-main thread. טסטים
שהריצו `ScalperDaemon.__init__` ב-thread נפלו.  
**תיקון:** handlers הועברו לתחילת `run()` עם `try/except (ValueError, OSError)`.

### 4. Vacuous assertion ב-shadow mode test
**בעיה:** `execute_market_order.assert_not_called()` עובר גם אם trigger לא
ירה כלל (כי `execute=False` מונע קריאה ממילא).  
**תיקון:** הוסף assertion שנרשם ≥1 `SCALPER_LEG` rows לפני בדיקת ה-CLOB.

---

## משתני סביבה חדשים

| משתנה | ברירת מחדל | תיאור |
|--------|-----------|-------|
| `EXECUTE_SCALPER` | `false` | shadow mode / live |
| `SCALPER_RESERVE_USDC` | `0` | הון שמור מהטריידר |
| `MAX_SCALP_TRADES_PER_HOUR` | `60` | rate limit |
| `SCALP_LEG_USDC_CAP` | `5.0` | גודל leg מקסימלי |
| `SCALP_REVERSAL_PCT` | `0.025` | threshold לכניסה |
| `SCALP_PROFIT_TARGET_PCT` | `0.02` | יעד רווח ל-leg2 |
| `SCALP_MAX_SUM_AVG` | `0.97` | profit gate |
| `SCALP_POLL_MS` | `500` | קצב polling |
| `SCALP_DISCOVER_EVERY_SEC` | `300` | תדירות discover |
| `SCALPER_HEARTBEAT_PATH` | `data/scalper_heartbeat` | קובץ heartbeat |

---

## הפעלה

### Stage 0 — shadow mode (עכשיו)

```bash
# ודא .env: EXECUTE_SCALPER="false", SCALPER_RESERVE_USDC="20"
docker compose --profile scalper up -d scalper
docker compose logs -f scalper
```

### בדיקה יומית

```bash
docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 50
docker compose logs scalper --since 1h | grep -E "ERROR|exception"
```

### קריטריוני מעבר ל-Stage 1 (אחרי 48 שעות)

| קריטריון | סף |
|----------|----|
| pairs שנוצרו ביום | ≥ 5 |
| shadow legs שעברו profit gate | ≥ 8/יום |
| שורות RECONCILE_NEEDED | 0 |
| exceptions ב-logs | 0 |
| heartbeat staleness | לעולם לא > 30s |

ראה `docs/SCALPER_STAGE_0_RUNBOOK.md` לפרטים מלאים.

---

## מה **לא** נבנה (בכוונה)

- Prometheus / Grafana — out of scope לפי CLAUDE.md
- Position close / leg2 auto-sell — Stage 2
- Multi-wallet — out of scope
- Kalshi arbitrage (Strategy D) — spec בלבד, לא קוד
