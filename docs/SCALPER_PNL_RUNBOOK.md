# Scalper — P&L Runbook + How It Works

## חלק א׳ — איך הכול עובד

### רצף חיי עסקה אחת

```
[כל 250ms — SCALP_POLL_MS]
  ↓
ScalperDaemon.tick()
  ↓
1. DISCOVER: Gamma API → שווקי btc/eth-updown-15m שפתוחים
   ↓ (כל SCALP_DISCOVER_EVERY_SEC = 60 שניות)
2. ORDER BOOK: ClobClient.get_order_book(up_token) + get_order_book(down_token)
   → best ask לכל צד
   ↓
3. ENTRY TRIGGER (ב-ScalpPair.apply_tick):
   תנאי: up_ask + down_ask < threshold (0.499)
   כלומר: sum < 1.0 − margin → יש "חלל" בספר
   ↓
4. REVERSAL CHECK:
   מחיר עלה מ-temp לפחות reversal_delta (0.020)?
   כן → leg1 מוכן
   ↓
5. PROFIT GATE:
   sum_avg = cost_up/(qty_up+ε) + cost_down/(qty_down+ε)
   sum_avg <= max_sum_avg (0.98)?
   כן → מותר לפתוח עוד
   ↓
6. PLACE LEG1 (FAK order):
   EXECUTE_SCALPER=true  → CLOB order → fills → רשום ב-scalper_pairs + trade_log
   EXECUTE_SCALPER=false → רק רשום SHADOW ב-trade_log (אין כסף אמיתי)
   ↓
7. [state: TRACKING → LEG1_FILLED]
   ↓
8. PLACE LEG2 (אחרי 200ms או כש-second_side_buffer מתמלא):
   EXECUTE_SCALPER=true  → CLOB order → fills
   ↓
9. [state: LEG1_FILLED → LEG2_FILLED] ✅ סגור
   ↓
10. REAP: period_ts עבר? → EXPIRED / RECONCILE_NEEDED
```

### State machine

```
            open
              │
         [TRACKING]
              │
    leg1 fills│
              ▼
        [LEG1_FILLED]
         │         │
  leg2   │         │ period expired
  fills  │         ▼
         │  [RECONCILE_NEEDED]  ← אופרטור מנקה ידנית
         ▼
   [LEG2_FILLED]  ← terminal ✅

[EXPIRED] = terminal, לא היה fill
```

### מה הרווח התיאורטי בעסקה?

```
cost_up   = מחיר ל-leg1 (למשל $4.75 עבור 10 מניות Up @ 0.475)
cost_down = מחיר ל-leg2 (למשל $4.80 עבור 12 מניות Down @ 0.40)

בפוליימרקט: Up + Down = $1.00 בפקיעה
אז אם sum_avg = 0.475 + 0.40 = 0.875
רווח לפני עמלות = (1.0 − 0.875) × qty = $0.125 × min(10,12) = $1.25
עמלה CLOB ≈ 2% מה-notional ← הגדול ביותר
רווח נטו ≈ $1.25 − fees
```

> **הסקאלפר לא מחכה לפקיעה.** הוא מוכר leg2 לפני הפקיעה ברגע שהמחיר עולה
> מספיק (second_side_buffer). אם leg2 לא ירה לפני פקיעה → RECONCILE_NEEDED.

---

## חלק ב׳ — Runbook P&L

### בדיקה יומית שוטפת

```bash
# מצב כולל — pairs + legs
docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 50

# P&L summary — raw SQL
docker compose run --rm trader sqlite3 data/trade_log.db "
SELECT
  state,
  COUNT(*) AS pairs,
  ROUND(SUM(cost_up),2)   AS spent_up,
  ROUND(SUM(cost_down),2) AS spent_down,
  ROUND(SUM(cost_up + cost_down),2) AS total_spent
FROM scalper_pairs
GROUP BY state;
"
```

### חישוב P&L על עסקאות סגורות

```bash
docker compose run --rm trader sqlite3 data/trade_log.db "
SELECT
  slug,
  ROUND(cost_up,3)   AS cost_up,
  ROUND(cost_down,3) AS cost_down,
  ROUND(qty_up,2)    AS qty_up,
  ROUND(qty_down,2)  AS qty_down,
  ROUND(cost_up/(qty_up+0.0001) + cost_down/(qty_down+0.0001), 4) AS sum_avg,
  ROUND(1.0 - cost_up/(qty_up+0.0001) - cost_down/(qty_down+0.0001), 4) AS gross_edge_per_share,
  closed_ts,
  state
FROM scalper_pairs
WHERE state = 'leg2_filled'
ORDER BY closed_ts DESC
LIMIT 20;
"
```

**פענוח:**
- `sum_avg` < 1.0 = נכנסנו בפחות מהמחיר המלא → רווח אפשרי
- `gross_edge_per_share` = רווח גולמי לכל מניה שהוחזקה
- כפל ב-min(qty_up, qty_down) = רווח גולמי כולל

### SHADOW mode — כמה היינו מרוויחים?

```bash
docker compose run --rm trader sqlite3 data/trade_log.db "
SELECT
  COUNT(*) AS shadow_legs,
  ROUND(SUM(size_usdc),2) AS hypothetical_spent,
  market_id
FROM trades
WHERE status = 'scalper_leg'
  AND (error LIKE '%SHADOW%')
  AND ts > datetime('now','-1 day')
GROUP BY market_id
ORDER BY hypothetical_spent DESC;
"
```

### זיהוי בעיות

```bash
# שורות RECONCILE_NEEDED — מחייבות בדיקה on-chain
docker compose run --rm trader sqlite3 data/trade_log.db "
SELECT slug, cost_up, cost_down, qty_up, qty_down, error, opened_ts
FROM scalper_pairs
WHERE state = 'reconcile_needed';
"

# האם heartbeat חי?
python3 -c "
import os, time
hb = 'data/scalper_heartbeat'
age = time.time() - os.path.getmtime(hb) if os.path.exists(hb) else 9999
print(f'heartbeat age: {age:.0f}s — {\"OK\" if age < 30 else \"STALE!\"}')
"

# שגיאות בשעה האחרונה
docker compose logs scalper --since 1h | grep -E "ERROR|exception|RECONCILE"
```

---

## חלק ג׳ — Runbook: מה לעשות ב-RECONCILE_NEEDED

**זה המקרה הקריטי:** leg1 בוצע (כסף יצא) אבל leg2 לא בוצע לפני הפקיעה.

```
1. הפסק את הסקאלפר זמנית:
   docker compose --profile scalper stop scalper

2. בדוק on-chain מה נרכש:
   slug = btc-updown-15m-XXXX
   token_id = up_token מהשורה ב-DB
   כנס ל: https://polymarket.com/profile
   → ראה positions → האם יש מניות Up?

3. אפשרויות:
   א. פוזיציה פקעה ב-1.0 → רווח מלא (leg1 בלבד, כי Up ניצחה)
      UPDATE scalper_pairs SET state='leg2_filled' WHERE slug='...';
   ב. פוזיציה פקעה ב-0.0 → הפסד מלא (cost_up הלך)
      UPDATE scalper_pairs SET state='expired' WHERE slug='...';
   ג. פוזיציה עדיין פתוחה → מכור ידנית מ-UI
      אחר-כך: UPDATE scalper_pairs SET state='leg2_filled' WHERE slug='...';

4. הפעל מחדש:
   docker compose --profile scalper up -d scalper
```

---

## חלק ד׳ — Stage 0 → Stage 1 → Stage 2

| Stage | מה פועל | EXECUTE_SCALPER | הון |
|-------|---------|-----------------|-----|
| **0 — Shadow** | הכול, ללא orders | `false` | $0 |
| **1 — Live small** | orders אמיתיים | `true` | $20 reserve, $5 max/leg |
| **2 — Scale** | orders + position close | `true` | להחליט |

### מעבר ל-Stage 1

```bash
# תנאים (לאחר 48 שעות shadow):
# ≥5 pairs/day, ≥8 shadow legs/day, 0 RECONCILE_NEEDED, 0 exceptions

# שנה ב-.env:
EXECUTE_SCALPER="true"
SCALPER_RESERVE_USDC="20"
SCALP_LEG_USDC="3"      # קטן בשלב ראשון

# tag לפני live:
git tag stage1-scalper-$(date -u +%Y%m%d-%H%M)

# הפעל:
docker compose --profile scalper up -d scalper
```

---

## תזכורת: ניטור יומי בשורה אחת

```bash
docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 30 && \
docker compose logs scalper --since 24h | grep -c "shadow leg" && \
docker compose logs scalper --since 24h | grep -E "ERROR|RECONCILE" | wc -l
```

פלט תקין: `0` בשורה האחרונה.
