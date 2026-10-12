# Bond corporate actions: which ones matter, and why buyback and tender offer

Quick check, 2026-10-08. Question: which bond corporate actions are the most common, so the most important ones get
implemented. Outcome: the issuer buyback and tender offer, implemented (`buyback-tender.md`).

## 1. Frequency: no public statistic found

No published count of bond corporate actions by type was found. Searched: DTCC, Euroclear, Clearstream, the Kazakh
central depository and KASE. So the ranking below rests on what standards list, what Kazakh law makes mandatory, and
what the KASE listing asks for, not on frequency.

## 2. What the sources list

| Source | What it says |
|---|---|
| KASE listing (side track) | Required: coupon on a record date and redemption at maturity. Plus one more: "early or partial redemption, dividend, tender offer or buyback, bondholder vote, change of terms, conversion, or the team's own" |
| ECB AMI-SeCo, SCoRE Corporate Actions rulebook, April 2025, Table 1 "Relevant events" (pp. 23-25) | Of 62 event types, most are marked relevant to debt instruments (the column layout does not survive text extraction, so no exact count). Payments and redemptions: INTR interest payment, REDM final maturity, MCAL full call / early redemption, PCAL / PRED partial redemption, BPUT put redemption, DRAW drawing. Offers: TEND tender / purchase offer, BIDS repurchase offer / issuer bid, DTCH Dutch auction. Holder decisions: BMET bondholder meeting, CONS consent. Others: CONV conversion, EXOF exchange, EXTM maturity extension, DFLT bond default |
| Kazakh Law No. 461-II "On the securities market", art. 18-4 p.1 | The issuer **may** buy back its placed non-state bonds on the secondary market |
| Same law, art. 18-4 p.2-4 | The issuer **must** buy back placed non-state bonds if: it decides to delist them; the exchange delists them for failing disclosure requirements; or it has had no bondholders' representative for over 60 days. A prospectus may add covenants with the same effect. The price is the **highest** of: (a) face value plus coupon accrued but unpaid; (b) the market price; (c) an appraiser's price. Point 2 does not apply to banks and insurers |
| Same law, art. 15 p.2 | During circulation the issuer must keep four covenants. Examples: no sale of assets above 25% of total assets, and no change of legal form. On a breach, the issuer "must, at the bondholders' demand, buy back the bonds at face value plus accrued coupon" |
| Same law, art. 18-2 | A bondholders' general meeting decides changes to the issue terms. The central depository compiles the voter list 3 working days before the meeting; one bond, one vote. If a listed holder sells afterwards, the vote passes to the buyer, who must prove ownership |
| Government Decree No. 473, p.16 (treasury bonds) | No early redemption; the issuer may buy bonds back at market price and redeem them (`bond-params.md`) |

Sources:
- ECB SCoRE CA rulebook 2025: <https://ecb.europa.eu/press/intro/publications/pdf/ecb.amiseco202504_corporateactions.en.pdf>
- Law 461-II: <https://zakon.uchet.kz/rus/docs/Z030000461_> (read 2026-10-08)

## 3. Candidates against the listing's criteria

| Action | Legal basis in KZ | Who starts it | What is new for us |
|---|---|---|---|
| **Buyback at holders' demand** (BPUT-like; covenant breach, delisting) | Mandatory for listed non-state bonds (art. 15 p.2, art. 18-4 p.2-4) | A trigger event, then each holder elects | Holder elections; price = max(face + accrued, market); accrued interest for a broken period |
| **Issuer buyback / tender offer** (TEND, BIDS) | Allowed (art. 18-4 p.1; Decree 473 p.16) | Issuer offers price and quantity; holders elect | Same holder elections; pro-rating if oversubscribed |
| Bondholders' meeting / vote (BMET, CONS) | Required to change terms (art. 18-2) | Issuer | Voter list 3 working days before; the vote follows a sold bond; tally |
| Early or partial call by the issuer (MCAL, PCAL, PRED) | Per prospectus; none for treasury bonds | Issuer | Little: reuses redemption, pro-rata |
| Change of terms | Needs the meeting first | Issuer and holders | Meeting plus term update |
| Conversion (CONV) | Not checked | Holder | Second token |

## 4. Derived pick

- The first two rows are one mechanism: a holder **elects** to sell bonds back to the issuer at a price, settled
  atomically, burning the bonds and paying cash. Only the trigger and the price rule differ: the issuer's offer, or
  the law's maximum.
- One implementation therefore covers the law's mandatory buyback (every listed corporate bond on KASE carries it)
  and the listing's named "tender offer or buyback".
- It also adds holder instructions: coupons and redemption are mandatory and identical for every holder, while selling
  or tendering is each holder's choice.
- Next by the same criteria: the bondholders' vote (art. 18-2). It has a Kazakh-specific record rule (the vote
  follows the bond), but it is more new work, and change of terms depends on it.

## 5. Hot spot

The mandatory buyback price includes "accrued coupon" for a broken period. The program has no such computation yet.
Decree 473 says only "30 days in a month / 360 days in a year", and which 30/360 variant applies is open
For corporate bonds the prospectus defines it, so the variant must come from a real
prospectus, not be chosen.
