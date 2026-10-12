# Bond parameters (reference)

Collected 2026-10-06. Status per value:
- **checked**: read in the source by Claude.
- **agent**: reported by the research agent, quote given, not re-read by Claude.

Servicing rules (record time, payment, redemption) are in `bond-mechanics.md`.

## B1. Listing example

Source: the KASE side-track listing, section "Example"
(<https://superteam.fun/earn/listing/superteam-kazakhstan-x-kase-side-track-corporate-actions-on-blockchain>).

| Parameter | Value | Status |
|---|---|---|
| Face value | 1,000 (the listing writes "$1,000") | checked |
| Coupon | 10% per year, fixed | checked |
| Frequency | semi-annual | checked |
| Tenor | 2 years (4 coupons) | checked |
| Coupon per bond | 1,000 x 10% / 2 = 50 | checked (listing formula) |
| Listing check values | 10 bonds: 500 per coupon; at maturity 10,000 principal plus the final coupon | checked |
| Not given by the listing | issue date, day count, record date or time, payment dates, business-day rule, currency of payment | - |

Real-world counterpart: MEOKAM (government medium-term treasury obligations), Decree 473, chapter 5:
term over 1 and up to 5 years (p.30), face value 1,000 KZT (p.28), day count 30/360 (p.32), coupon twice a year (p.33),
coupon `S = N*C*180/360` (p.34). With N = 10 x 1,000 and C = 10% this gives the listing's 500. Status: checked.

## B2. MUM108_0015 (MEUKAM, real KASE bond, long-tenor case)

| Parameter | Value | Source | Status |
|---|---|---|---|
| Ticker / ISIN | MUM108_0015 / KZKD00001277 | S1, S2 | checked |
| Type / issuer | MEUKAM (long-term treasury obligation) / Ministry of Finance RK | S1, S3 | checked |
| Currency | KZT | S2 ("Номинальная стоимость, тенге") | checked |
| Face value | 1,000 KZT | S2; S3 p.37 | checked |
| Coupon | 14.50% per year, fixed | S1 ("14,500"); S2 ("14,50") | checked |
| Frequency | once a year, 6 March | S2 ("1 раз в год", "06 марта ежегодно"); S3 p.42 | checked |
| Day count | 30/360 | S2 ("Временная база: 30 / 360"); S3 p.41 | checked |
| Coupon formula | `S = N*C` (N = total face value held, C = rate) | S3 p.43 | checked |
| Start of circulation | 06.03.2025 | S2 | checked |
| Maturity | 06.03.2034; 9 years (3,240 days) | S2 | checked |
| Last trading day | 05.03.2034 | S1 | agent |
| Volume in circulation | 161,893,138 bonds (as of the S2 notice) | S2 | checked |
| Coupon payment dates | 06.03 yearly 2026-2034; paid 09.03.27, 09.03.32, 07.03.33 (weekends) | S1 "Купонные выплаты" tab | agent |
| Record-date column on KASE | "–" for all 9 coupons | S1 | agent |
| Accrual period | from start of circulation to the day before maturity | S3 p.14 | checked |
| Non-working day | coupon and redemption move to the next working day | S3 p.14, p.15 | checked |
| Redemption | at par, first day after the end of circulation | S3 p.15 | checked |
| Paying agent | Central Securities Depository (Almaty) | S1; S3 p.3(6) | agent |
| Early redemption | none; issuer may buy back at market price and redeem | S3 p.16 | agent |
| Exchange for a new issue | only with holders' consent | S3 p.17 | agent |

Record moment for the coupon on 06.03.2034 (maturity day): 09:00 Almaty = 04:00 UTC = unix 2025230400. Almaty has
been UTC+5 since 2024-03-01 (tzdata, `zdump -v Asia/Almaty`; checked).

## Sources

- **S1** KASE security page: <https://kase.kz/ru/investors/gsecs/MUM108_0015>
- **S2** KASE news 1549814, auction terms for an additional placement: <https://kase.kz/ru/information/news/show/1549814>
- **S3** Government Decree No. 473 of 27.06.2025 (amended 14.07.2026, No. 615), rules for issuing and servicing
  treasury obligations: <https://adilet.zan.kz/rus/docs/P2500000473>. Read on the mirror
  <https://zakon.uchet.kz/rus/docs/P2500000473> (adilet serves the text via JavaScript).
- **S5** CSD rules «Правила обслуживания и погашения государственных казначейских обязательств»:
  <https://backend.kcsd.kz/storage/documents/files/rules-gov-securities-5-ru-6a8c2c9aa90b4.pdf>
