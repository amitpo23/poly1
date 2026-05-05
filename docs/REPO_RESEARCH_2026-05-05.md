# מחקר ריפו פוליימרקט — 2026-05-05

ניתוח עמוק של 7 ריפו (האחד נהרג לפני השלמה). כל ממצא מוכן לפעולה.

---

## סיכום מנהלים

| ריפו | שפה | ורדיקט | עדיפות |
|------|-----|---------|--------|
| pmxt-dev/pmxt | TypeScript+Python | שימוש לנתונים בלבד | גבוה |
| warproxxx/poly-maker | Python | גנוב 3 פונקציות | בינוני |
| Polymarket/poly-market-maker | Python | עיין בדפוסים | גבוה |
| PoDev-Juanthiago/Polymarket-Arbitrage-Bot | TypeScript | עיין בלבד / מודול עתידי | נמוך |
| suislanchez/polymarket-kalshi-weather-bot | Python | גנוב Kelly + calibration | גבוה |
| ConteurShadow/Polymarket-Trading-Bot-Rust | Rust | עיין בלבד | נמוך |
| CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot | Python MIT | גנוב Kalshi client | בינוני |

---

## TIER 1 — שיפורים מיידיים (כל אחד < יום עבודה)

### 1. Kelly Sizing — החלפת MAX_POSITION_FRACTION שטוח

**מקור:** `suislanchez/polymarket-kalshi-weather-bot` (MIT)

**מה חסר היום:** `MAX_POSITION_FRACTION=0.025` קבוע — לא מביא בחשבון ודאות, מחיר, או יתרון.

**מה לעשות:**
```python
# להוסיף ל-agents/application/trade.py
def kelly_size(win_prob: float, market_price: float, bankroll: float,
               max_fraction: float = 0.05, kelly_fraction: float = 0.15) -> float:
    """15% fractional Kelly with hard bankroll cap."""
    b = (1.0 - market_price) / market_price  # implied payout odds
    lose_prob = 1.0 - win_prob
    kelly_raw = (win_prob * b - lose_prob) / b
    if kelly_raw <= 0:
        return 0.0
    return min(kelly_raw * kelly_fraction, max_fraction) * bankroll
```
משתמשים ב-`win_prob` מה-`recommendation.confidence` של ה-LLM, `market_price` מה-`recommendation.price`.

### 2. MIN_EDGE_PCT Gate — חסימת עסקאות ללא יתרון מספיק

**מקור:** `suislanchez` (8% למזג אוויר, 2% לקריפטו)

**מה לעשות:** הוסף ל-`.env.example`:
```
MIN_EDGE_PCT="0.05"  # block trades where |model_prob - market_price| < 5%
```
ב-`RiskGate.ok()` — בדיקה: `abs(recommendation.confidence - recommendation.price) < min_edge`.

### 3. Calibration Tracking — מעקב אחרי איכות ה-LLM

**מקור:** `suislanchez` (Brier score, predicted vs actual)

**מה לעשות:** הוסף לסכמת `trades` ב-`trade_log.py`:
```python
# שדות חדשים ב-TradeRecord
model_prob: float | None = None      # confidence שהועבר ל-LLM
edge_at_entry: float | None = None   # model_prob - market_price
outcome_correct: bool | None = None  # True/False לאחר settlement
brier_score: float | None = None     # (model_prob - actual_outcome)^2
```
מאפשר בעתיד: `cli.py inspect-calibration` — Brier score על כלל העסקאות.

### 4. Kalshi כ-Signal — תמחור צולב

**מקור:** `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` (MIT)

**מה לעשות:** הוסף קריאה ל-Kalshi public API (ללא אותנטיקציה) לפני LLM call:
```python
# agents/polymarket/kalshi_client.py (חדש, ~40 שורות)
def fetch_kalshi_btc_price(event_ticker: str) -> dict | None:
    """GET https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=..."""
    # מחזיר yes_ask/no_ask ב-0.0-1.0 (מחלקים ב-100)
```
אם Kalshi מתמחר שוק ב-5%+ שונה מ-Polymarket — להכניס לפרומפט כ-"market consensus divergence".

---

## TIER 2 — שיפורים לטווח קצר (1-3 ימים)

### 5. pmxt — Order Book + Slippage לפני הוראה

**מקור:** `pmxt-dev/pmxt` (TypeScript sidecar + Python SDK)

**מה מספקת:** `fetch_order_book(outcome_id)`, `get_execution_price(outcome_id, side, amount)`, `fetch_ohlcv`.

**שימוש:**
- לפני כל `execute_market_order` — בדוק slippage צפוי. אם > 3%, דלג על העסקה.
- `fetch_ohlcv` — תן ל-LLM היסטוריית מחירים של השוק הספציפי.

**אזהרה:** אל תשתמש ב-pmxt לביצוע הוראות — מסתיר את מיפוי token_ids[0]/[1] שהוא invariant קריטי.

**עבודה:** הוסף Node.js ל-Dockerfile (~80MB), `pip install pmxt`, כתוב `MarketDataClient` wrapper.

### 6. Stop-Loss + Volatility Cooldown פר-שוק

**מקור:** `warproxxx/poly-maker` (MIT)

**3 פונקציות שכדאי לגנוב:**

```python
# מ-trading_utils.py — תמחור queue-jumping עם floor במחיר ממוצע
def get_order_prices(best_bid, best_ask, avg_price, tick_size=0.01):
    bid = best_bid + tick_size
    ask = max(best_ask - tick_size, avg_price)  # לא למכור מתחת לעלות
    if bid >= ask:
        bid, ask = best_bid, best_ask
    return bid, ask

# מ-trading_utils.py — sizing עם ניהול מלאי
def get_buy_sell_amount(position, trade_size, max_size, min_size=5.0):
    buy = min(trade_size, max_size - position) if position < max_size else 0.0
    sell = min(position, trade_size) if position >= min_size else 0.0
    return buy, sell

# דפוס stop-loss + cooldown מ-trading.py
# אחרי הפסד > stop_loss_threshold: כתוב risk_off.json, חסום קניות ל-sleep_period שעות
```

### 7. Lifecycle Timer + SIGTERM נקי

**מקור:** `Polymarket/poly-market-maker`

`lifecycle.every(1800, sync)` + `lifecycle.on_shutdown(cancel_all)` — גרסה נקייה יותר מה-`while True` הנוכחי ב-`run.py`.

---

## TIER 3 — אסטרטגיות חדשות (דיון נדרש לפני CLAUDE.md)

### 8. 15-Min Crypto Up/Down — מודול נפרד, ללא LLM

**מקור:** `PoDev-Juanthiago` + `ConteurShadow/Rust`

**הרעיון:** שוקי `btc-updown-15m-*` הם מוצר שונה לחלוטין מהשוקים שpoly1 בוחר.
אין צורך ב-LLM — סיגנל טהור ממחיר:

```python
# כניסה כשהמחיר < 0.499 (רגל ראשונה)
# סף לרגל שנייה: 1.0 - fill_price + 0.04
# בדיקת רווחיות: avg_yes + avg_no < 0.98
```

יכול לרוץ כמודול parallel ב-Executor, ללא שינוי ב-LLM flow הקיים.
**אפשרות נוספת מ-Rust:** hedge ladder (2-min/4-min/10-min) לניהול פוזיציות לאחר מילוי.

### 9. Polymarket/Kalshi Cross-Exchange Arb Scanner

**מקור:** `CarlosIbCu` (MIT)

הנוסחה הנקייה:
```python
# Poly_strike > Kalshi_strike:
#   Buy Poly DOWN + Buy Kalshi YES → guaranteed $1 payout
# margin = 1.0 - (poly_down_ask + kalshi_yes_ask) - fees
# אם margin > 0: הזדמנות
```

**מה חסר לביצוע:** auth + order placement ל-Kalshi, leg-sequencing, abort path.
**שווה לבנות כ-scanner בלבד עכשיו** — אולי להאכיל כ-signal ל-LLM.

---

## מה לדלג עליו

| ריפו | סיבה |
|------|------|
| warproxxx — ארכיטקטורה כללית | global state, Google Sheets config, passive-both-sides — לא תואם |
| Official MM — auth/web3 | CLOB v1 בלבד, מת ב-2024 |
| שני weather bots — domain logic | ספציפי מדי למזג אוויר, simulation only |
| Rust bot — קוד ישיר | אין רישיון, אלגוריתם בסיסי |
| TS Arbitrage bot — קוד ישיר | אין רישיון (אסטרטגיה ניתנת לפורט עצמאי) |

---

## ממצא דחוף: MIN_SIZE = 15.0 USDC

ה-official MM bot מגדיר `MIN_SIZE = 15.0` כ-CLOB protocol constant.
ה-`STARTING_BALANCE_USDC=80.0` עם `MAX_POSITION_FRACTION=0.025` = **$2.00 per trade**.

**צריך אימות:** האם ה-CLOB באמת דורש $15 מינימום? יש לנו עסקה filled ב-$1.80 — אולי ה-15 הוא רק self-imposed minimum של אותו ריפו.
**פעולה:** הרץ `python -c "from py_clob_client.clob_types import ... print(MIN_SIZE)"` או בדוק בדוקומנטציית CLOB.

---

## מפת דרכים מוצעת

```
שבוע 1 (עכשיו):
  ✓ הוסף kelly_size() ל-trade.py
  ✓ הוסף MIN_EDGE_PCT ל-RiskGate.ok()
  ✓ הוסף calibration fields ל-TradeRecord
  ✓ בדוק MIN_SIZE בפועל מ-CLOB

שבוע 2:
  □ kalshi_client.py — Kalshi public read API
  □ pmxt — MarketDataClient wrapper (order book + OHLCV)

שבוע 3+:
  □ 15-min crypto module (דיון ראשון)
  □ Cross-exchange arb scanner (דיון ראשון)
```
