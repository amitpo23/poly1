# Postmortem — איך איבדנו $80 ב-13 שעות

Date: 2026-05-06
Status: $80 → $18.12 portfolio value (cash $0.72 + positions MTM $17.40)
Realized loss so far: ~$12.50 (5 worthless tokens)
Unrealized: ~$50 (4 positions still open, may swing)

זה מסמך כן. **לא** "the bots performed as expected", **לא** "market was tough."
מה הבוטים עשו לא נכון, מה היה צריך לעשות אחרת, ולמה התשתית הקיימת לא יכלה
למנוע את זה.

---

## הטריידים בפרטים מלאים

### poly1 main (LLM-driven) — 8 fills, $19.49 deployed

| Date | Market | Side | Entry | Cost | Confidence |
|---|---|---|---|---|---|
| 05-04 18:26 | 566188 (Man City EPL) | BUY YES | 0.38 | $2.00 | 0.65 |
| 05-04 18:26 | 566228 (Barcelona La Liga) | BUY YES | **0.997** | $1.95 | 0.65 |
| 05-04 21:01 | 566187 (Arsenal EPL) | SELL (=BUY-NO) | 0.565 | $1.90 | 0.75 |
| 05-04 21:32 | 653788 (OpenAI AGI 2027) | BUY YES | **0.11** | $1.85 | 0.75 |
| 05-05 04:10 | 653788 (OpenAI AGI 2027) | BUY YES | **0.11** | $1.80 | 0.75 |
| **05-06 05:19** | **566187 (Arsenal EPL)** | **SELL @ 0.795** | **0.205** | **$3.51** | **0.85** |
| **05-06 05:19** | **566188 (Man City EPL)** | **BUY @ 0.205** | **0.205** | **$3.33** | **0.60** |
| **05-06 07:43** | **566228 (Barcelona)** | **BUY @ 0.997** | **0.997** | **$3.16** | **0.65** |

**שלוש בעיות שאני יכול לזהות מיד:**

#### Bug A — Averaging down (תוקן Fix B)
בתאריכים 05-06 (היום אתמול) ה-LLM קנה שוב מאותם 3 שווקים שכבר היו לו בהם פוזיציה.

- Man City: קנה ב-0.38, ירד ל-0.205 → **קנה שוב** ב-0.205 (הכפיל חשיפה למפסיד)
- Arsenal: בנה NO ב-0.435, מחיר עלה ל-0.795 → **קנה שוב** ב-0.205 (= NO ב-0.205. הכפיל)
- Barcelona: קנה ב-0.997, **קנה שוב** ב-0.997 (כשהמחיר כמעט גג, אין upside)

**מה היה צריך לעשות אחרת:** dedupe על market_id ל-FILLED rows, בלי תלות בזמן. זה מה שתיקנתי ב-Fix B אחרי ההפסד.

**זה היה bug ידוע** — `has_active_trade_for_market(hours=6)` חוסם רק 6 שעות. אחרי 6 שעות ה-LLM יכול לחזור ולקנות אותו שוק. בלי maintain_positions שיודע למכור, "to re-enter" = "to double down."

#### Bug B — קנייה ב-$0.997 (R/R שלילי באופן קיצוני)

566228 Barcelona ב-0.997. אם מנצח: +$0.003 לשרא × 5.13 שרא = **+$0.015 max profit**. אם מפסיד: **-$5.11 max loss**.

אפילו ב-99.7% probability: EV = +$0.149 - $0.015 = **+$0.134**. Profit *expected* בכלל הזה $0.13 על $5 הון.

**מה היה צריך לעשות אחרת:** prompt של ה-LLM צריך לסנן markets ב-< 5% או > 95% — אין איפה edge.

#### Bug C — Long-shot OpenAI AGI ($3.65 על 8 חודשים)

653788 — "OpenAI announces AGI by 2027" ב-0.105.

- עלות: $3.65, payout מקסי: $30.33, payout בסבירות 10.5%: **$3.18 EV** (-$0.47 expected)
- זמן לקבל את הכסף בחזרה: **8 חודשים** (גם אם נצליח)
- $3.65 שכלוא 8 חודשים על EV של -$0.47

**מה היה צריך לעשות אחרת:** סינון markets לפי resolution time (אסור > 30 ימים) או לפי position size (long-shots צריכים להיות smaller, לא 5% של ה-bankroll).

---

### scalper — 4 pairs, $12.50 deployed, ~$12.50 realized loss

| Pair | State | Cost Up | Cost Down | Outcome |
|---|---|---|---|---|
| eth-updown-15m-1778087700 | expired | $2.50 | $2.50 | both legs filled, but resolved against us |
| sol-updown-15m-1778087700 | RECONCILE | $2.50 | $0 | leg1 only, market resolved Up=0 |
| xrp-updown-15m-1778087700 | RECONCILE | $2.50 | $0 | leg1 only, market resolved Up=0 |
| sol-updown-15m-1778088600 | RECONCILE | $2.50 | $0 | leg1 only, market resolved Up=0 |

#### Bug D — Stage 1 flipped without sufficient shadow data

ה-SPEC §15 דורש: *"Stage 0 (shadow): EXECUTE_SCALPER=false, **2-3 days**. Sanity check that triggers fire and pair counts are non-trivial."*

מה עשינו: shadow מ-בוקר עד אחה"צ → flip ל-Stage 1 (~9 שעות).
מה SPEC קרא: 48-72 שעות.

הסיבה הספציפית שהפסדנו: ה-SHADOW לא הספיק כדי לזהות שה-trigger יש לו negative edge על קריפטו 15-דק'.

**מה היה צריך לעשות אחרת:** לחכות 48 שעות. לבדוק את ה-SHADOW results לפי קריטריונים: ≥5 pairs/day, ≥8 shadow legs/day, **win-rate analysis on resolutions**. רק אז flip live.

#### Bug E — Trigger logic creates negative-edge entries

הסקלפר מטריגר על "sum_avg < 0.98 + reversal/depth". ההיגיון: השוק חזר מהקיצוני → קונים value.

**אבל:** במציאות, ה-Up token יורד ל-22% **כי המטבע (BTC/ETH/SOL וכו') זז למטה**. ה-trigger קנה Up בדיוק כשמומנטום נגד Up. תוצאה: 4 מ-5 leg1 fires היו על הצד המפסיד.

**מה היה צריך לעשות אחרת:** הbutton trigger צריך להיות **counter-momentum**: "Up is at 22% **but** the price feed says BTC is now reversing UP." בלי price feed validation, ה-orderbook לבדו לא נותן signal.

---

### swarm market_maker — 2 fills @ $0.595 each, $10 deployed

| Date | Market | Side | Price | Cost |
|---|---|---|---|---|
| 05-06 16:33 | Hormuz | BUY YES | 0.595 | $5 |
| 05-06 16:33 | Hormuz | BUY YES | 0.595 | $5 |

#### Bug F — Quoting INSIDE the spread on a 50/50 market

Hormuz ל-50/50 (סבירות = ~50%). market_maker קנה ב-$0.595. אם YES נצח: +$0.405 × 8.4 shares × 2 = $6.80. אם NO: -$10.

EV אם המחיר $0.50 fair: 0.5 × $0.405 × 16.8 - 0.5 × $0.595 × 16.8 = $3.40 - $5.00 = **-$1.60**.

**מה היה צריך לעשות אחרת:** market_maker צריך להציע quote בקצוות הספרד — buy LIMIT ב-0.49, sell LIMIT ב-0.51. הוא רץ במצב taker (שוקי הזמנה אגרסיבי) ולכן שילם את הספרד במקום לתפוס אותו.

---

## מה היינו צריכים לעשות **אחרת** (סיכום)

### לפני שהתחלנו לסחור היום

1. **לבנות maintain_positions קודם.** בלי exit logic, אין תיקון של טעות. כל position היא bet בינארי ארוך-טווח.
2. **לבנות dashboard עם MTM real-time.** אין דרך לזהות הפסד מיידית בלי זה.
3. **2-3 ימים shadow לכל אסטרטגיה חדשה** לפני flip live.
4. **prompt ל-LLM שמסנן: 5% < price < 95%, resolution date < 30 days.** אם ניהול הון לא מובנה ב-LLM, צריך לסנן ב-config.

### בזמן הסחר

5. **לאסור multiple agents on the same market.** poly1 main + swarm market_maker + scalper יכלו כולם לקנות את אותו שוק. צריך global lock.
6. **rate-limit per agent: 1 trade per market in 24h.** אם הbot טועה, לא להכפיל.
7. **MAX_DAILY_LOSS_PCT אמיתי שעובד על MTM, לא על cash.** הסף הקיים של 10% חסם רק כשcash ירד — וזה קרה אחרי שכבר הפסדנו עוד.

### אחרי הטעויות

8. **HALT אוטומטי אחרי 3 הפסדים רצופים.** המערכת המשיכה לסחור גם אחרי 5 RECONCILE_NEEDED.
9. **Postmortem אחרי כל 10% drawdown.** חייב.

## למה לא היה לנו את זה

- maintain_positions תמיד היה ב-roadmap, מעולם לא נבנה (`pass` מ-2024-08).
- dashboard נבנה ל-monitoring, לא ל-decision-making.
- scalper מומש לפי SPEC, אבל ה-SPEC עצמו לא חוייב את ה-Stage 0 דרישה של 48h.
- prompt של ה-LLM לא עודכן ליעד "find edge", רק "find best trade" (subjective).
- אין global market-lock.
- אין consecutive-loss circuit breaker.

## בלי זה — אסור לחזור לסחור

ההמלצה ברורה: **אל תסחור עם cash אמיתי עוד עד שכל הבעיות של "מה היה צריך לעשות אחרת" יוטמעו.**

ה-experiment של אתמול שווה את ה-$60 שאיבדנו אם — ורק אם — אנחנו לומדים מ-9 הבעיות הספציפיות לעיל ובונים את ההגנות.

זה יהיה ה-roadmap של הסשן הבא:

1. **maintain_positions** (עכשיו, 4-6h) — exit logic
2. **MTM dashboard panel** (1h) — visibility
3. **prompt update** (30 min) — LLM filter
4. **global market lock** (1h) — cross-bot dedupe
5. **consecutive-loss circuit breaker** (1h) — auto-halt
6. **proper Stage 0 enforcement** (config) — 48h shadow before live

סה"כ ~9-12 שעות עבודה לפני re-deployment של cash.
