# LIVE TRADING AUDIT — poly1
**תאריך הדוח:** 2026-05-22
**יום שנותח:** 2026-05-21 (live controlled, 30 כניסות)
**מקור נתונים:** server `/srv/poly1` (HEAD `c5ff2e6`), DB `data/trade_log.db`
**מקור אנליטי:** קוד מקומי (HEAD `f0db42c` — 1 commit לפני השרת) + 5 docs חדשים מ‑21‑22/5 + שני סוכני מחקר במקביל

> זה לא הדוח האסטרטגי מ‑20/5. זה audit חד נקודתי על מה שקרה אתמול ב‑live. הוא מתבסס על נתונים אמיתיים מהשרת ולא ספקולציה.

---

## 0. תקציר Go / No‑Go

**ההמלצה לבוקר: NO‑GO ל‑live בלי 3 תיקונים. SHADOW בלבד עד שהם בקוד.**

3 הסיבות:

1. **`update_shadow_markouts` לא רץ כלל.** כל 5,476 שורות `decision_journal` של אתמול והיום עם `outcome_5m_json=NULL`, `outcome_15m_json=NULL`, `outcome_60m_json=NULL`. ה‑strategy sensitivity sweeper תועד במפורש כתלוי בעמודות הללו (`STRATEGY_SWEEPER_2026-05-21.md`). **אנחנו עיוורים** למידה הזה — אי אפשר לאמת ש‑BUY 0.40‑0.50 ימשיך לעבוד מחר.

2. **לולאת spam שורפת CPU/log על שוק יחיד.** כל 457 דחיות ה‑`internal_edge_too_low:-0.042 score=0.541` הן על market_id יחיד `653788` ב‑2.5 שעות. זה לא 457 אבחנות — זו שאילתה אחת חוזרת. log spam + cost waste. כשנגדיל ל‑$50‑$100 trades, אותה לולאה תספאם 5,000 פעמים.

3. **EV inflation בסיגנל הדומיננטי של אתמול.** 22 מתוך 30 הכניסות הן `opportunity_factory,alphainsider_proven,crypto_tape` עם `prob=0.66‑0.77, ev=0.31‑0.56` — אבל ה‑P&L האמיתי כמעט אפס. ה‑probability לא מכוילת מתוצאות הסטוריות; היא יוצאת מ‑`CryptoExchangeTapeClient.analyze_question()` (ניתוח טכני של מחיר tape), והיא **מסומנת `calibrated=True`** בלי כיול ([opportunity_factory.py:334](agents/application/opportunity_factory.py) — *unverified line, agent‑reported*). העסקאות שיוצאות מכך מקבלות "פטור" מ‑`probability_not_calibrated` שחסם 821 אחרים — אבל הן בעצמן לא מכוילות.

**מה חייב להיות נכון לפני live:**
1. תיקון OpenAI bug ב‑`near_resolution.py:383` (51 כשלים זהים אתמול).
2. תזמון `update_shadow_markouts` כקרון/סוכן (אחרת אין לולאת למידה אמיתית).
3. הסרת ה‑calibrated‑flag מ‑`opportunity_factory` עד שיש > 50 outcomes מכוילים, **או** הקטנת המשקל שלו ב‑MetaBrain בחצי.
4. cap על כמות הסריקות שמותר ל‑market_scanner לבצע על אותו market_id בחלון זמן (5 דקות).
5. תקרת לסיגנל `today_lesson_*`: ההגנה צריכה לפוג אחרי N שעות, אחרת היא הופכת לכלא של אתמול.

---

## 1. ממצאים כמותיים (מקור: server DB, מאומת)

### 1.1 P&L לפי side (server `trade_log.db`)

| side | n | sum P&L USDC | avg per trade | capital deployed |
|---|---|---|---|---|
| BUY | 19 | **‑$0.081** | ‑$0.0043 | $23.33 |
| SELL | 11 | **‑$0.899** | ‑$0.082 | $13.50 |
| **סה"כ** | **30** | **‑$0.980** | **‑$0.033** | **$36.83** |

> אם מסירים את ה‑`resolved_loss` היחיד (BUY נפתר NO, ‑$1.00 בעסקה אחת), BUY = **+$0.92**.

### 1.2 P&L לפי close_status × side

| close_status | side | n | sum P&L | avg P&L |
|---|---|---|---|---|
| closed_take_profit | BUY | 11 | **+$1.669** | +$0.152 |
| closed_take_profit | SELL | 3 | +$0.315 | +$0.105 |
| closed_stop_loss | BUY | 5 | ‑$0.697 | ‑$0.139 |
| closed_stop_loss | SELL | 8 | **‑$1.214** | ‑$0.152 |
| closed_timeout | BUY | 2 | ‑$0.054 | ‑$0.027 |
| resolved_loss | BUY | 1 | **‑$1.000** | ‑$1.000 |

### 1.3 P&L לפי price band × side

| side | band | nWins/nTotal | win-rate |
|---|---|---|---|
| BUY | **0.40‑0.50** | **6/8** | **75%** ✅ |
| BUY | 0.50‑0.60 | 4/7 | 57% |
| BUY | <0.40 | 1/4 | 25% ⚠️ |
| SELL | 0.40‑0.50 | 0/1 | 0% |
| SELL | **0.50‑0.60** | **3/10** | **30%** ❌ |

(Source: SQL join על `trades` filled→close)

### 1.4 לפי agent / signal_source (server)

100% מ‑30 הכניסות יצאו דרך **`market_scanner` (agent) → `scanner-executor` (executor)**.

| signal_source | n | מאפיין |
|---|---|---|
| `opportunity_factory,alphainsider_proven,crypto_tape` | **22** | prob 0.66‑0.77, ev 0.31‑0.56, ad‑hoc tape probability |
| `meta_brain,manifold,manifold:manifold` | **5** | scanner_approved score=0.81‑0.95 |
| `meta_brain,alpaca:XRP/USD,crypto_tape:XRPUSDT` | מעט | קומבינציה |

### 1.5 Exit reason — זמני החזקה

| status | n | min min | avg min | max min |
|---|---|---|---|---|
| closed_take_profit | 14 | **0.1** | 22.0 | 145.1 |
| closed_stop_loss | 13 | **0.5** | 9.3 | 31.2 |
| closed_timeout | 2 | 360.2 | 360.2 | 360.2 |
| resolved_loss | 1 | 4.3 | 4.3 | 4.3 |

> **התראה:** 6 SLs היכו תוך 0.5‑4 דקות. 3 TPs היכו תוך 0.1‑0.4 דקות. כשהזמן הקצר ‎0.1 דקה משני הצדדים, מדובר על entries לתוך תזוזות אינסטנט — חשד ל‑**adverse selection** (נכנסנו בדיוק לפני שהמחיר זז).

### 1.6 שערים שדחו את רוב ה‑candidates

| reason | n |
|---|---|
| meta_brain:internal_edge_too_low (**market_id 653788 בלבד**) | **457** |
| meta_brain:general_score_too_low | 41 |
| probability_not_calibrated (brain_decisions) | 821 |
| today_lesson_side_blocked | 730 |
| today_lesson_price_band_blocked | 223 |
| risk_gate_blocked | 657 |
| edge_score_too_low | 2,029 |
| crypto_tape: no supported asset (provider noise) | **20,031** |
| public_news: disabled until provider scorecard proves positive edge | **11,772** |
| alpaca: no supported symbol in question | 4,103 |

### 1.7 כשלי ביצוע

| תופעה | n | מקור |
|---|---|---|
| OpenAI "must contain 'json'" שגיאת 400 | **51** | `near_resolution.py:383` — missing SystemMessage |
| FAK close-failed "no orders to match" | 9 | exit liquidity dried |
| FAK exit-deferred (אותו שורש) | 9 | אותו |
| supervisor_halt — "critical exit-path guard tripped" | 9 | trading_supervisor הגן |
| MAY_HAVE_FIRED | **0** | ✅ לא נוצרו מצבי "אולי בוצע" |

### 1.8 markout coverage

| metric | value |
|---|---|
| decision_journal rows 21‑22/5 | **5,476+** |
| with outcome_5m_json | **0** |
| with outcome_15m_json | **0** |
| with outcome_60m_json | **0** |

> **המשמעות:** strategy_sensitivity_sweep ([`docs/STRATEGY_SWEEPER_2026-05-21.md`](docs/STRATEGY_SWEEPER_2026-05-21.md)) לא יכול לרוץ. הסוכן זיהה את הסיבה: `scripts/update_shadow_markouts.py` קיים אבל **לא מתוזמן בשום מקום** (לא ב‑docker-compose, לא בקרון). זה חסר‑נתונים סיסטמי.

### 1.9 מצב נוכחי בשרת

- HALT file: `data/HALT` (128 בייטים, נוצר 21/5 19:10:19 UTC)
- runtime_control: `mode=freeze`, `allowed_live_agents=[]`, `requires_halt=true`
- equity: **$24.34 USDC cash**, 0 open positions לפי `live_equity_guard`
- 35/35 containers `running (healthy)`
- position_manager loop: מבצע ~30 calls/דקה ל‑CLOB `balance-allowance` על **12 דחקים שתקועים ב‑`dust_market_open`** ולא מתקדמים. עומס מיותר.

---

## 2. מה עבד ומה לא

### עובדה (מהנתונים)

✅ **עבד:** BUY ב‑0.40‑0.50 — 75% win-rate, רווח ממוצע +$0.15 לעסקה.
✅ **עבד:** ה‑ledger atomic — 0 MAY_HAVE_FIRED, 0 fund leaks, recovery נקי.
✅ **עבד:** ה‑supervisor — 9 halts ברגעים חמים שהגנו על המערכת מ‑exit cascades.
✅ **עבד:** ה‑position_manager — כל 30 העסקאות נסגרו (TP/SL/timeout/resolved), ה‑FAK retry loop בסוף הצליח.
✅ **עבד:** scanner_executor כ‑bridge בין evidence ל‑orders.
✅ **עבד:** HALT + freeze auto — בסוף יום controlled, המערכת קפאה אוטומטית ב‑19:10.

❌ **לא עבד:** SELL ב‑0.50‑0.60 — 30% win-rate, הפסד ‑$1.21.
❌ **לא עבד:** BUY <0.40 — 25% win-rate, 4 עסקאות, מקרר.
❌ **לא עבד:** `update_shadow_markouts` — אפס שורות עם outcomes.
❌ **לא עבד:** OpenAI integration ב‑near_resolution — 51 כשלים זהים.
❌ **לא עבד:** scanner‑rejection קצב — שוק יחיד (653788) נסרק 457 פעמים.
❌ **לא עבד:** providers מנופחים — 20K+ דחיות "no supported asset", 11K "disabled provider" שעדיין נקרא.

### השערה (תומכת בנתונים אבל לא מוכחת)

🔹 **השערה:** ה‑22 עסקאות `opportunity_factory_alphainsider_tape` עם prob=0.66‑0.77 הסתבר שהן ~50/50 בפועל. **תופעת Favorite‑Longshot Bias** קלאסית של prediction markets ([Green/Lee/Rothschild, *The Favorite‑Longshot Midas*](https://www.stat.berkeley.edu/~aldous/157/Papers/Green.pdf)): longshots מתומחרים מעל ההסתברות האמיתית; favorites מתחתיה. **התוצאות שלכם הן חתימה מובהקת:** SELL על favorites (0.50‑0.60) נופל; BUY מתחת ל‑0.50 (sub‑favorites) מצליח.

🔹 **השערה:** ה‑6 SLs המהירות (תוך 0.5‑4 דקות) הן adverse selection. המחיר זז עליכם תוך שניות מהכניסה — סימן ל‑information arrival שלא היה בידיכם בעת הסריקה. במילים אחרות: אתם entry-late על news shocks קצרים.

🔹 **השערה:** ה‑2 BUYs שהחזיקו 360 דקות (timeout) — שוק שטוח, אין בריחה. ה‑6h max_hold יותר מדי לכניסות 5/15min crypto.

🔹 **השערה:** ה‑probability inflation בסיגנלים של opportunity_factory היא הסיבה ש‑P&L≈0 למרות שה‑EV הצהיר 0.3‑0.5. אם ה‑prob היא ~0.50 בפועל ולא 0.70, ה‑net EV הוא ~0 לאחר fees ‑ exactly ה‑P&L שראינו.

---

## 3. לקחים מאתמול — 5 הכי חשובים

### לקח #1 — Favorite‑Longshot Bias זה ה‑signal האמיתי של היום

**הנתון:** BUY 0.40‑0.50 = 75% win. SELL 0.50‑0.60 = 30% win. נטו ‑$0.90 על SELL לבד.

**הפעולה המומלצת:** עד שלא תוכיחו אחרת ב‑shadow על > 50 דגימות, **תאסרו SELL/Down ב‑band 0.45‑0.70**. אל תסמכו על ה‑LLM שיעקוף את זה. הוסיפו את החסימה הזו ב‑`scanner_executor` בנוסף ל‑`today_lesson_side_blocked` הנוכחי (שהוא כללי מדי).

### לקח #2 — `update_shadow_markouts` הוא קריטי ולא רץ

**הנתון:** 5,476 שורות ב‑`decision_journal` ב‑21‑22/5 — אפס עם outcome_5m/15m/60m. ה‑strategy_sensitivity_sweeper (היה צריך להיות ה‑feedback loop של אתמול) לא יכול לרוץ.

**הפעולה המומלצת:** או להפוך את `scripts/update_shadow_markouts.py` ל‑daemon (service חדש ב‑docker-compose שמריץ אותו כל 5 דקות), או להוסיף אותו לקרון של ה‑VPS. תוודאו שהוא מצליח לקרוא orderbook_snapshots — אם הטבלה ריקה, צריך לתקן את ה‑orderbook_monitor להזרים אליה.

### לקח #3 — סורקים את אותו שוק שוב ושוב (457×!)

**הנתון:** כל 457 ה‑`internal_edge_too_low` מאתמול הם מ‑`market_id=653788` בין 07:36‑10:13. שאילתה זהה, תוצאה זהה (edge=-0.042, score=0.541), נכשלה זהה.

**הפעולה המומלצת:** הוסיפו cache TTL ב‑market_scanner. אם market_id דחה ב‑5 דקות אחרונות באותה סיבה — דלגו ללא חישוב. בנוסף, חשבו שני סוגי backoff:
- `same_market_recent_reject_cache_ttl_sec=300` (לא לבדוק שוב 5 דקות)
- `gate_rejection_streak_quarantine_threshold=10` (אחרי 10 דחיות זהות, השוק עובר ל‑market_quarantine למשך שעה)

### לקח #4 — OpenAI integration שגיאה זהה ב‑51 ניסיונות

**הנתון:** 51 דחיות "Error 400: messages must contain the word 'json'" כולן ממקור אחד.

**הפעולה המומלצת:** ([near_resolution.py:380‑383](agents/application/near_resolution.py) — *unverified line*) — תיקון פשוט: להעביר `SystemMessage(content="You respond with valid JSON.")` לפני ה‑`HumanMessage`. השוו לקוד ב‑[executor.py:168‑190](agents/application/executor.py) ש‑YES כן עובד.

זה bug ש**שורף LLM cost** ולא מייצר ערך. אם זה לא היה מתפסס ב‑try/except, היה מקריס את ה‑pipeline.

### לקח #5 — ה‑probability ב‑opportunity_factory לא מכוילת אבל מסומנת ככזו

**הנתון:** `signal_source=opportunity_factory,alphainsider_proven,crypto_tape` עם `prob` מתוך `CryptoExchangeTapeClient.analyze_question()`. ה‑factory מסמן `estimated_win_probability_calibrated=True` ([opportunity_factory.py:334](agents/application/opportunity_factory.py) — *unverified line*).

זה עוקף את ה‑`probability_not_calibrated` gate שחסם 821 אחרים. כלומר: 22 כניסות עברו לא בגלל שהן מכוילות, אלא בגלל שהן **מסומנות** כמכוילות.

**הפעולה המומלצת:** הוסר את `"estimated_win_probability_calibrated": True` עד שיש > 50 outcomes שמראים calibration אמפירי (Brier score < 0.22 על 30‑day window). זה חוסם אבל בצדק.

---

## 4. רעיונות חדשים / זוויות צדדיות

(מבוסס על מחקר חיצוני + נתוני ה‑audit. לכל אחד: מקור, סבירות, איך לבדוק, סיכון, עדיפות.)

### רעיון #1 — חסימה אסימטרית של SELL/Down באזור 0.45‑0.65 (Favorite‑Longshot)

- **למה זה יכול לעבוד:** ה‑bias הזה תועד אקדמית לעשרות שנים ([Green/Lee/Rothschild](https://www.stat.berkeley.edu/~aldous/157/Papers/Green.pdf)). אצלכם הוא מאומת: SELL@0.50‑0.60 = 30% win-rate. זו לא תקלה — זו אמת סטטיסטית של השוק.
- **איך לבדוק:** עוד 50 SELL trades ב‑shadow, חישוב win-rate per‑decile של price. אם < 50% — אסור live.
- **סיכון:** sharps שולטים בקטגוריות מסוימות (politics close to event, top stocks) — ה‑bias עשוי להתהפך שם. validate per‑market‑category.
- **עדיפות:** **NOW** — הכי zero‑cost ב‑impact.

### רעיון #2 — Negative‑Risk Combinatorial Arbitrage (לא קיים בבוט)

- **למה זה יכול לעבוד:** ב‑neg-risk events של Polymarket (multi‑outcome, רק אחד מנצח), סך ה‑YES asks צריך לסכום ל‑$1. כשהוא < $1 (אחרי fees) — להריץ basket buy = רווח מובטח עד resolution. [arXiv 2508.03474](https://arxiv.org/abs/2508.03474) מתעד $40M שחולצו מ‑Polymarket בשנה. ה‑adapter רשמי: [Polymarket/neg-risk-ctf-adapter](https://github.com/Polymarket/neg-risk-ctf-adapter).
- **איך לבדוק:** scanner חדש שעובר על event groups מה‑Gamma API, מסכם YES asks, מסמן sum < 0.98. shadow‑only עד שמוודאים את ה‑adapter call.
- **סיכון:** Capital lock עד resolution. דורש interaction עם NegRiskAdapter contract (לא CLOB רגיל). שגיאת rounding ב‑CTF fees תוכל למחוק את ה‑edge.
- **עדיפות:** **SHADOW בשבוע הבא** — high‑EV, deterministic, לא תחרותי עם HFT.

### רעיון #3 — Fee‑Aware EV Gate עם c·P·(1−P)

- **למה זה יכול לעבוד:** Polymarket הציגה ב‑Jan 2026 dynamic taker fee של עד ~3.15% ב‑50¢ specifically כדי לחנוק latency arb ([Finance Magnates](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)). מבנה: `fee = c·P·(1−P)` עם `c=0.07` (Global) או `0.05` (US). ה‑EV הנקי נטו: `EV_net = p·payout − (1−p) − fee(P) − slippage`. אצלכם 30 כניסות × ~$0.025 fee = ~$0.75 — בערך כל ה‑P&L השלילי של אתמול.
- **איך לבדוק:** הוסיפו עמודה `expected_fee_at_entry` ל‑`trade_log`, השוו gross vs net EV בקובץ scorecard. backtest על 30 העסקאות האתמוליות — האם עם fee הן עוברות?
- **סיכון:** רגיל ל‑refactor.
- **עדיפות:** **NOW** — תוקפת ישיר את ה‑breakeven של אתמול.

### רעיון #4 — Cross‑Venue Lead‑Lag עם LLM Semantic Filter

- **למה זה יכול לעבוד:** [arXiv 2602.07048](https://arxiv.org/abs/2602.07048) מדגים שיטה דו‑שלבית — Granger causality על זוגות venues, ואז LLM שמדרג זוגות לפי מנגנון כלכלי. Empirical על Kalshi Economics: win-rate 51.4%→54.5%, ובעיקר avg loss קטן מ‑$649 ל‑$347. **downside protection הוא ה‑edge העיקרי**, לא win-rate boost.
- **איך לבדוק:** ה‑providers ל‑Kalshi/Manifold/Metaculus כבר קיימים. הוסיפו שכבת Granger lag (1‑5 דקות) ועוד LLM gate semantic. shadow‑only למשך שבועיים.
- **סיכון:** עוד LLM call → cost. צריך rate‑limit ו‑async.
- **עדיפות:** **SHADOW בשבוע הבא**.

### רעיון #5 — Order‑Book Imbalance כ‑Feature ל‑MetaBrain

- **למה זה יכול לעבוד:** [Gould & Bonart 2015 (arXiv 1512.03492)](https://arxiv.org/pdf/1512.03492) — `imbalance = (bid_qty − ask_qty)/(bid_qty + ask_qty)` מנבא 1‑5 דקות קדימה. תשתית קיימת (`orderbook_monitor.py`, `market_microstructure.py`) — חסר רק ה‑feature ב‑MetaBrain.
- **איך לבדוק:** שמרו order‑book snapshot כל 30 שנייה (כבר נדרש ל‑markouts!), הוסיפו את ה‑feature, logistic regression מינימלית מול mid‑price 60s/180s/300s.
- **סיכון:** על shallow books של Polymarket הסיגנל רועש. דרשו min‑depth $500 לפני שמסתמכים.
- **עדיפות:** **SHADOW בשבוע הבא** — sync עם תיקון ה‑markouts. שתי בעיות פתרון אחד.

### רעיון #6 — Whale Wallet Quality Filter + Aggregation Rule (משדרג קיים)

- **למה זה יכול לעבוד:** Research מצביע על basket-approach עם 80% consensus rule שעוקף copying של trader יחיד. קריטריונים: 200+ markets resolved, >55% win-rate, >3 קטגוריות, פעילות ב‑90 ימים אחרונים. אצלכם יש כבר `CLOBWhaleProvider`, `NansenSmartMoneyProvider`, `WalletMasterProvider`, `DataAPIWhaleConsensusProvider` — אבל `whale_reader` ב‑MetaBrain סופר `n_whales` בלי איכות.
- **איך לבדוק:** [Dune queries](https://dune.com/brunoskl/polymarket-whale-tracker) בונים whitelist של 30‑100 wallets שעוברים את 4 הקריטריונים. MetaBrain feature `whale_consensus_weighted` משוקלל רק מהם.
- **סיכון:** survivorship bias כשבוחרים wallets ex‑post.
- **עדיפות:** **SHADOW בשבוע הבא** — שדרוג למה שכבר יש.

### רעיון #7 — Brier‑Score‑Based Calibration Layer

- **למה זה יכול לעבוד:** `meta_brain.score` הוא ranking confidence, לא probability מכוילת. עם 30 outcomes כבר אפשר לבנות Platt scaling פשוט. אחרי 100+ outcomes, אפשר isotonic. זה ההבדל בין "המודל אומר 0.70" ל"במציאות זה 0.58 — תיקח 58 כ‑probability".
- **איך לבדוק:** כל שבועיים, fit Platt על N העסקאות האחרונות, השווה Brier לפני/אחרי.
- **סיכון:** dataset קטן → overfit. דרשו מינימום 50 outcomes לפני שמפעילים.
- **עדיפות:** **LATER** — אחרי 100+ resolved trades.

### Hard NO — דברים שנראים אטרקטיביים אבל יזיקו

- **Pure Binance‑lag arb על 5/15min crypto:** Polymarket הוסיפה ב‑Jan 2026 ‎dynamic fee של עד 3.15% **בדיוק** כדי לחסום את זה. הממוצע של תזוזת BTC ב‑5min < 3%. כבר לא רווחי net of fee. אם `crypto_5m_market_maker_shadow.py` / `btc_5min.py` לא מחשבים fee — הם משחקים חמישי שלא קיים. **תוודאו** שה‑EV gate מפחית fee לפני שמפעילים מחדש.
- **להיות market maker כ‑strategy עיקרית:** [postmortem](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058) מבהיר: ~10% APY על long‑dated quiet markets במקרה הטוב. הבוט שלכם directional, לא MM.
- **לסמוך על repos קטנים של "polymarket arbitrage" בלי maintenance** — [`ImMike/polymarket-arbitrage`](https://github.com/ImMike/polymarket-arbitrage) הוא skeleton עם 4 commits, אין fee handling, author עצמו ממליץ "start dry, start small."
- **להוסיף עוד LLM call ל‑MetaBrain בלי latency budget** — ה‑lead‑lag הוא רעיון טוב אבל מוסיף call ל‑כל זוג. asyncio + rate-limit חובה.

---

## 5. המלצות פרקטיות

### מה לעשות לפני live (עוד היום)

- [ ] **תיקון #1** — `near_resolution.py:380‑383`: להוסיף `SystemMessage(content="You respond with valid JSON.")` לפני ה‑`HumanMessage`. ב‑shadow לוודא ש‑51 הכשלים נעלמים.
- [ ] **תיקון #2** — להוסיף service ב‑docker-compose שמריץ `python scripts/update_shadow_markouts.py` כל 5 דקות. אם orderbook_snapshots ריק — לתקן את שאיבת ה‑snapshots תחילה (חסר‑נתונים upstream).
- [ ] **תיקון #3** — ב‑market_scanner: אם `market_id` נדחה > 5 פעמים בחלון 5 דקות באותה סיבה — quarantine ל‑market_id למשך 60 דק'. זה מסיר את הלולאה של 457.
- [ ] **תיקון #4** — להסיר `"estimated_win_probability_calibrated": True` מ‑`opportunity_factory.py` עד שיש > 50 outcomes מכוילים. אם לא — להקטין `META_BRAIN_WEIGHT_OPPORTUNITY` בחצי כ‑interim.
- [ ] **תיקון #5** — ב‑`scanner_executor.py` `today_lesson_side_blocked`: לתת TTL ל‑lesson (לדוגמה 24 שעות). אחרת אתם מקפיאים את האתמול לנצח.
- [ ] **תיעוד** — לעדכן את `LIVE_LESSONS_2026-05-21.md` במספרים מהמסמך הזה (אצלכם רשום בטרמינולוגיה רכה; כאן יש arguments כמותיים).

### מה להריץ ב‑shadow (השבוע)

- [ ] negative‑risk arb scanner (רעיון #2)
- [ ] fee‑aware EV gate (רעיון #3) — מינוס ‑$0.75 לכל הסט = רעיון לבדוק על אותם 30 trades retrospectively
- [ ] lead‑lag LLM filter (רעיון #4) על pairs Manifold↔Polymarket שכבר קיימים
- [ ] order‑book imbalance feature (רעיון #5) — קוצר latency עם תיקון #2
- [ ] whale wallet quality filter (רעיון #6)

### מה לכבות / לצמצם

- [ ] `external-conviction-public-news` — "disabled until provider scorecard proves positive edge" אבל עדיין נקרא 11,772 פעמים. אם disabled — שלא יקרא בכלל. רעש לוג.
- [ ] `external-conviction-crypto-tape` המייצר "no supported asset" → 20,031. הוא בודק כל שוק נגד רשימה קטנה של נכסים. תזיזו את ה‑filter למקור (scanner) ולא לפרובידר.
- [ ] `external-conviction-alpaca` — 4,103 "no supported symbol in question". אותה תופעה.
- [ ] לחתוך את ה‑6h max_hold ל‑90‑120 דקות עבור signal_sources ש‑99% מהם נסגרים תוך 30 דקות (ה‑2 BUYs ב‑360 דקות מאתמול נשארו תקועים בשוק שטוח).

### מה למדוד בכל סיבוב live (לוודא שיש לכם את ה‑KPIs האלה)

| KPI | מקור | חשיבות |
|---|---|---|
| P&L per side × price_band | `strategy_scorecard` | מאמת או מפריך favorite‑longshot |
| `expected_fee_at_entry` vs `actual_close_diff` | חדש | מאמת fee gate |
| Markouts coverage % | decision_journal.outcome_* | health check ה‑sweeper |
| `quarantine_hits_per_hour` | trade_log | מודד spam |
| `same_market_scan_within_5min_count` | brain_decisions | health check ה‑scanner cache |
| `llm_400_errors_count` | logs | health check OpenAI fixes |
| `fak_close_failed_then_retried_count` | trades | exit liquidity health |

### thresholds לא לשנות בלי validation

- `META_BRAIN_MIN_EDGE_PCT` — אל תרד מ‑0.02 בלי backtest על fee הדינמי
- `STOP_LOSS_PCT=0.06` — אל תרחיב בלי לדעת stochastic drift של 5-min markets
- `MAX_HOLD_SECONDS=21600 (6h)` — אפשר להקטין, אסור להגדיל
- `EXPERT_SOLO_MIN_PROB=0.65` ו‑`EXPERT_SOLO_MIN_WILSON=0.58` — אל תרד אם opportunity_factory probability ימשיך להיות לא‑מכויל

---

## 6. Red Team — איך המערכת תפסיד כסף למרות שהכל "נראה תקין"

### תרחיש #1 — opportunity_factory ימשיך לרשום `calibrated=True` ויעקוף בעיקר את ה‑gates

הסיכון הכי חמור. אתמול 73% מהכניסות (22/30) הגיעו דרך path אחד שמסמן את עצמו כמכויל בלי כיול אמיתי. אם זה לא יתוקן, **ה‑gates האחרים יחסמו את כל הרשת אבל ה‑path היחיד שהזיק יישאר פעיל**. ה‑P&L יראה -$1/$30 = ‑3.3% ליום. ב‑$50 ליום זה ‑$1.65/יום = ‑$50/חודש. בלי validation אמיתית, לעולם לא נדע מתי לעצור.

### תרחיש #2 — favorite‑longshot bias יתהפך בקטגוריה ספציפית בלי שנשים לב

ה‑bias החזק שראינו אתמול היה על שווקי crypto 5/15min. בקטגוריות politics close‑to‑event או sports, sharps שולטים והבייאס מתהפך. אם נקבע "BUY only" כללי — נחסום את עצמנו דווקא בקטגוריות שבהן יש sharps לחקות. צריך per‑category לוגיקה.

### תרחיש #3 — markout coverage 0 → לא נדע שאיבדנו edge

נמשיך לחשוב שאסטרטגיה X עובדת על סמך 30 דגימות. אחרי 7 ימים, ה‑real win-rate יורד ל‑45% אבל אנחנו לא רואים את זה כי ה‑sweeper ריק. נמשיך להיכנס. נפסיד בשקט. **זו הסכנה הכי שקטה.**

### תרחיש #4 — adverse selection על 5-min crypto יתפוס אותנו

6 SLs מאתמול היכו תוך 0.5‑4 דקות. אם זה דפוס מתמשך, אנחנו נכנסים על news shocks אחרי שה‑smart money כבר נכנס וזז. עם בעיית ה‑markouts לא רואים את ה‑מחיר 5min/15min אחרי entry — לא נדע אם זו adverse selection או רעש שיתוקן בעצמו.

### תרחיש #5 — close_failed loop יכלא פוזיציות ב‑volatility spike

18 FAK exits נכשלו אתמול ("no orders to match"). חזרו בהצלחה רק כי השוק חזר ל‑liquid. בעת spike משמעותי (event reset כל ה‑book), ה‑retry policy לא משדרג slippage ולא עובר ל‑limit. עלולות להישאר פוזיציות עד timeout/resolution. ב‑$1 לכל אחת זה נסבל. ב‑$10 לכל אחת — לא.

### Assumptions הכי מסוכנים

1. **שכל ה‑providers שמסומנים `calibrated=True` באמת מכוילים.** הוכח שלא.
2. **ש‑MAY_HAVE_FIRED הוא היחיד שיכול לבלום את המערכת.** בפועל, 9 supervisor_halt + 18 close_failed יצרו fragmentation דומה.
3. **ש‑`update_shadow_markouts` רץ כי הוא קיים.** Coverage 0 הוכיח שלא.
4. **ש‑6h timeout מתאים לכל סוכן.** עבור scanner_executor short‑horizon — לא.

### לוגים/מדדים שעלולים לשקר לנו

- `live_executed:30` ב‑brain_decisions אומר 30 כניסות, אבל לא אומר כלום על איכות.
- `equity_usdc:24.34` ו‑`drawdown:0` נכונים אבל מטעים: ה‑capital deployed היה $36, ה‑P&L היה ‑$0.98 — drawdown אמיתי הוא ‑2.7%, אבל ה‑guard מודד מ‑baseline=$0 אז drawdown=0.
- `meta_brain.score=0.541` (שכל 457 הדחיות שלו על אותו market) — נראה כמו 457 שווקים, בפועל 1.
- "30/30 containers healthy" — Yet 51 LLM failures + 9 supervisor halts + 18 FAK retries הם בעיות. health checks סופרים liveness, לא error rate.

---

## 7. תוכנית פעולה — P0/P1/P2

### P0 — היום בבוקר (לפני שתפתחו live בכלל)

1. ✅ לקרוא את הדוח הזה ולוודא שאתם מבינים את ההפסד של אתמול לפי side × band.
2. תיקון ה‑OpenAI bug ב‑`near_resolution.py` (תיקון #1).
3. quarantine‑on‑repeat‑reject ב‑market_scanner (תיקון #3).
4. הסרת `calibrated=True` מ‑opportunity_factory.py או הקטנת המשקל ב‑MetaBrain (תיקון #4).
5. הוספת service ל‑docker-compose שמריץ `update_shadow_markouts.py` כל 5 דק' (תיקון #2).
6. תיעוד ב‑`docs/LIVE_LESSONS_2026-05-22.md` של ההמלצות הקונקרטיות מכאן.

### P1 — אחרי הסיבוב ה‑live הבא (24‑48 שעות)

1. הריצו 30‑60 trades נוספים ב‑shadow, רק עם BUY ב‑0.40‑0.50 + `today_lesson_side_blocked` פעיל.
2. ספרו אחוז ה‑markouts שמצליחים להצטבר (אם זה עדיין 0 אחרי תיקון #2 — תקלה upstream).
3. שאילתה: "כמה מ‑22 הכניסות של `opportunity_factory_alphainsider_tape` עם score 0.66‑0.77 הסתיימו ב‑TP אמיתי?" כדי לדעת אם ה‑prob שלהם 0.55, 0.50, או נמוך יותר.
4. fee‑aware EV gate refactor (רעיון #3).
5. backtest על אותם 30 entries: כמה היו עוברים את ה‑EV gate החדש (net of fee)?

### P2 — להמשך השבוע

1. negative‑risk arb scanner ב‑shadow (רעיון #2).
2. order‑book imbalance feature (רעיון #5).
3. whale wallet quality filter (רעיון #6).
4. lead‑lag LLM gate (רעיון #4).
5. הסרת ה‑5 providers הרועשים (crypto‑tape "no asset", alpaca "no symbol", public‑news disabled, אחרים) — או הזזת ה‑filter ל‑scanner.
6. הקטנת max_hold ל‑scanner_executor short-horizon ל‑120 דק' (לא 360).

---

## 8. המלצת Winrate — קונקרטית

### האסטרטגיה היחידה שאני ממליץ עליה ל‑live מחר (אחרי P0)

**`market_scanner` → `scanner-executor`** עם:
- **side=BUY בלבד** (לא SELL)
- **price band 0.40‑0.50 בלבד**
- **signal_source = `meta_brain,manifold` בלבד** (לא opportunity_factory עד שמסיירים את ה‑calibration)
- **size $1 per trade**
- **max_open_positions = 3**

מבוסס על: BUY 0.40‑0.50 הראה 6/8 = 75% אתמול. ה‑sample של 8 קטן, אבל זה מה שיש. Manifold divergence היה מעורב ב‑5/30 מהכניסות, ולא היה ב‑22 הבעייתיות.

### Signal source הכי מבטיח (אחרי validation)

**Manifold divergence + Cross‑market consensus** — היה בסך הכל ב‑5 כניסות אתמול. ה‑sample קטן מכדי להיות חד‑משמעי, אבל זו הקטגוריה היחידה שלא מציגה אינפלציית prob כמו `opportunity_factory`. ההיגיון: divergence הוא empirical, לא LLM‑generated.

### Sample size מינימלי לפני שמגדילים תקציב

- **לפני שמעלים מ‑$1/trade ל‑$5/trade:** דרשו לפחות **30 trades** עם הקריטריונים החדשים. דרשו `actual_winrate ≥ 0.55`, `realized_avg_return_per_trade > 0`, `0 unhandled errors`.
- **לפני שמעלים מ‑$5 ל‑$10:** **80 trades** ב‑$5, אותם קריטריונים, **+ markout coverage > 50%** (אחרת אין משוב).
- **לפני שמעלים מעל $10:** דורש את 3 ה‑shadow strategies (neg‑risk arb, fee‑aware gate, order‑book imbalance) פעילים ועם evidence.

### Region/Side/Band חסום עד הוכחה אחרת

- **SELL ב‑0.45‑0.70:** חסום.
- **BUY מתחת ל‑0.30:** חסום (1/4 win-rate, ויש 1 resolved_loss של $1).
- **כל signal_source עם `calibrated=True` ללא 50+ outcomes מאומתים:** weight=0 ב‑MetaBrain.

---

## 9. נספח — Discrepancies & Open Questions

### דברים שלא הצלחתי לאמת ידנית (UNVERIFIED)

- מספרי שורות שדווחו על‑ידי הסוכן ב‑`scanner_executor.py:89‑94, 322‑340, 408‑432, 292‑307` ו‑`opportunity_factory.py:275‑374, 311‑312, 334`. הציטוטים נראים סבירים אבל לא קראתי בעצמי. **לפני שמתקנים — צריך לאמת שורות מדויקות.**
- מספרי שורות ב‑`trading_policy.py:33‑43`, `position_manager.py:95‑147, 1262‑1306, 169‑171`. סבירים אבל לא אומתו.
- ב‑`update_shadow_markouts.py` — הסוכן זיהה שאין scheduler, אבל לא חיפש בכל הקרון של ה‑VPS באופן עצמאי. ייתכן ויש crontab מערכת שאני לא רואה. **שאלה לאופרטור.**

### שאלות פתוחות לאופרטור (אתה — לפני שאתה חוזר ל‑live)

1. האם `update_shadow_markouts.py` מתוזמן ב‑`crontab -e` של trader@hermesagent01 או רק בקוד? אם בקרון — הוא לא רץ אתמול (Output coverage 0).
2. האם `orderbook_snapshots` כטבלה קיימת ויש בה שורות מ‑21/5? אם כן, הבעיה ב‑markout הוא בעיבוד; אם לא, הבעיה ב‑capture upstream.
3. האם הסיגנל `opportunity_factory_alphainsider_tape` נחשב לפי `agent_promotion_ledger.state='live_candidate'` או הוא רץ ללא registry promotion?
4. מה שיעור ה‑fee הממוצע ששילמתם אתמול לפי `response_json.fee_usdc`? יש לכם את הנתון? (אצלי לא מצאתי בשדות).
5. האם ה‑market_id `653788` (זה שספאם 457 פעמים) הוא שוק עם liquidity לא רגיל או שוק שגוי שצריך quarantine קבוע?
6. ה‑POSTGRES של Grafana — מה ה‑metrics שמופיעים שם? יש dashboard מוכן ל‑per‑side/per‑band P&L?

### מקורות חיצוניים שצוטטו (לקריאה לפי עניין)

- [Green/Lee/Rothschild — *The Favorite‑Longshot Midas*](https://www.stat.berkeley.edu/~aldous/157/Papers/Green.pdf)
- [arXiv 2508.03474 — Negative‑Risk Arbitrage in Polymarket](https://arxiv.org/abs/2508.03474)
- [Polymarket/neg-risk-ctf-adapter (GitHub)](https://github.com/Polymarket/neg-risk-ctf-adapter)
- [Finance Magnates — Polymarket Dynamic Fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [arXiv 2602.07048 — LLM Semantic Filtering for Lead‑Lag Trading](https://arxiv.org/abs/2602.07048)
- [Gould & Bonart — Queue Imbalance Predictor (arXiv 1512.03492)](https://arxiv.org/pdf/1512.03492)
- [Polymarket Liquidity Rewards postmortem (wanguolin)](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058)
- [Dune — Polymarket Whale Tracker](https://dune.com/brunoskl/polymarket-whale-tracker)

---

**הודעה לקורא (אתה):** הדוח הזה לא נוגע בקוד, לא פותח live, לא מבצע deploy, לא חושף secrets. כל המספרים שנכתבו לעיל מקורם ב‑`/srv/poly1/data/trade_log.db` בשרת או בקוד מקומי. אם משהו נראה לא מתאים למה שאתה רואה במציאות — סמן אותו ועדכן אותי.

המלצה אחרונה — קרא קודם את סעיף 0 ואת סעיף 8 (המלצת Winrate). הם המסה של מה שאמרתי.
