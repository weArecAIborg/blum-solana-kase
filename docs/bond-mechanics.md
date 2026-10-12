# Bond servicing mechanics in Kazakhstan (government bonds on KASE)

Collected 2026-10-06. Status per item: **checked** = read in the source by Claude; **agent** = reported by the
research agent with a quote, not re-read. Bond parameters are in `bond-params.md`; sources S1-S8 are listed at the end.

## 1. Coupon payment day (S5, article 8, paragraph 6; checked)

On the payment day the Central Securities Depository (CSD):
1. Compiles the list of holders entitled to the coupon "по состоянию на 09:00 алматинского времени указанного дня"
   (as of 09:00 Almaty time that day).
2. Does NOT suspend registration of secondary-market trades, but pays only the holders on that list.
3. Calculates the coupon amount for each holder on the list.
4. By 17:00 Almaty time transfers the money received from the issuer to the holders' bank accounts.

There is no separate record date: KASE shows "–" in the record-date column (S1, agent). The record moment is
09:00 on the payment day.

## 2. Redemption day (S5, article 10, paragraph 6; checked)

On the redemption day the CSD:
1. Stops registering trades in the issue.
2. Compiles the holder list as of 09:00 Almaty time.
3. Calculates the face value due to each holder.
4. By 17:00 pays the face value and writes the bonds off ("списывает ГКО") the holders' sub-accounts.

The write-off is the equivalent of the listing's "burn, retire, lock, or otherwise mark the tokens as redeemed".

## 3. Supporting rules

| Rule | Source | Status |
|---|---|---|
| Issuer must credit the full amount to the CSD by 16:00 on the payment day | S5 art.8 p.7 | agent |
| CSD notifies the issuer no later than 3 working days before payment | S5 art.8 p.3 | agent |
| A holder is a person on whose account the bonds sit at the moment the list is compiled | S6 art.159 p.3 | agent |
| Payment falling on a non-working day moves to the next working day (coupon and redemption) | S3 p.14, p.15 | checked |
| Interest accrues from the first day of circulation to the day before maturity | S3 p.14 | checked |
| The last coupon is paid on the redemption day | S3 p.14 | checked |
| Redemption at par on the first day after the end of circulation | S3 p.15 | checked |
| MEOKAM coupon: `S = N*C*180/360`, twice a year, 30/360 | S3 p.32-34 | checked |
| MEUKAM coupon: `S = N*C`, once a year, 30/360 | S3 p.41-43 | checked |
| Issuer may buy back all or part of an issue at market price on the secondary market and redeem it | S3 p.16 | agent |
| Exchange for a new issue only with holders' consent | S3 p.17 | agent |
| Ministry of Finance bonds are excluded from the CSD paying-agent rules for other issuers | S7 art.2 p.1(1) | agent |
| Corporate bonds: holder list as of the date and time in the issuer's request | S7 art.9 p.3 | agent |
| The CSD rulebook governs depository servicing of government securities | S8 p.32 | agent |

Regular coupon amounts are fixed per period (rate x face / frequency), so the day count does not change them.
The day count matters for broken periods.

## 4. Facts that constrain the design

- **Coupon day:** trading continues after the 09:00 list is taken. Entitlement therefore depends on each holder's
  balance at 09:00, not at the moment the payment is executed.
- **Redemption day:** trading stops, so balances are frozen from 09:00 until write-off.
- **Settlement:** payment to holders happens on the same day, before 17:00.

## 5. Not found

- An official holiday calendar. 06.03.2027 and 06.03.2032 are Saturdays and 06.03.2033 is a Sunday; KASE shows payment
  on Tuesday 09.03.2027 and 09.03.2032 (agent), skipping Monday 08.03, a public holiday in Kazakhstan. The sample
  calendar `data/calendars/kz-holidays.txt` is taken from the python `holidays` package and reproduces KASE's payment
  dates for MUM108_0015 (`crates/bond_math/tests/reference.rs`).
- Rounding of the coupon per holder.
- Extra interest for a delayed payment.
- The issue decision and the first-auction notice for MUM108_0015.

## Sources

- **S1** KASE security page MUM108_0015: <https://kase.kz/ru/investors/gsecs/MUM108_0015>
- **S2** KASE news 1549814 (auction terms): <https://kase.kz/ru/information/news/show/1549814>
- **S3** Government Decree No. 473 of 27.06.2025, amended 14.07.2026 (No. 615):
  <https://adilet.zan.kz/rus/docs/P2500000473> (read on the mirror <https://zakon.uchet.kz/rus/docs/P2500000473>)
- **S5** CSD «Правила обслуживания и погашения ГКО» (approved 02.11.2012, in force from 16.01.2017):
  <https://backend.kcsd.kz/storage/documents/files/rules-gov-securities-5-ru-6a8c2c9aa90b4.pdf>
- **S6** CSD rules on registrar and depository activity (special part), art.159-160:
  <https://backend.kcsd.kz/storage/documents/files/rules-particularpart-depositary-registrar-10ru-6a6c40435e8ac.pdf>
- **S7** CSD rules on paying-agent services:
  <https://backend.kcsd.kz/storage/documents/files/rules-payment-3ru-6973702da8134.pdf>
- **S8** National Bank Board Resolution No. 307 of 29.11.2018, p.32: <https://adilet.zan.kz/rus/docs/V1800017920>
