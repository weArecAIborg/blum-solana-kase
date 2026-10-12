# How TradFi services bonds, and how each step maps to Solana

Compiled 2026-10-06 by Claude from primary documents, downloaded and read in full text. Sections 1 and 2 are facts with
their sources. Section 3 maps them to Solana: the Solana facts carry sources, while the "easier / harder" column is
Claude's assessment and is labelled as such.

## Sources

| Id | Document | Where |
|---|---|---|
| SCoRE | ECB AMI-SeCo, "Corporate Actions - Single Collateral Management Rulebook for Europe", December 2021 (first published 2019) | <https://www.ecb.europa.eu/press/intro/publications/pdf/amiseco202112_corporateevents.en.pdf> |
| AFME | CAJWG / AFME, "Market Standards for Corporate Actions Processing", revised version 2012 | <https://www.afme.eu/portals/0/globalassets/downloads/standard-forms-and-documents/afme-market-standards-for-corporate-actions-processing-revised-version-2012.pdf> |
| T2S-MC | T2S Corporate Actions Sub-Group, "T2S Corporate Actions Standards - Market Claims" | <https://www.ecb.europa.eu/paym/target/t2s/governance/pdf/casg/ecb.targetseccasg130316_T2SMarketClaimStandards.en.pdf> |
| T2S-NOTE | T2S CASG, "Market claims and transformations in T2S" (dated 30 November 2016) | <https://www.ecb.europa.eu/paym/target/t2s/governance/pdf/casg/ecb.targetseccasg161130_ExplanatoryNoteOnClaimsAndTransformations.en.pdf> |
| DTC | DTC, "Corporate Actions Distributions Service Guide", as of 6 May 2026 | <https://www.dtcc.com/-/media/Files/Downloads/legal/service-guides/Service-Guide-Distributions.pdf> |
| KZ-CSD | Kazakhstan CSD rules on servicing government bonds | `bond-mechanics.md`, source S5 |
| SOL | solana.com docs: transactions, pausable, permanent-delegate, getProgramAccounts; docs.rs `solana_clock` | links inline |

Not retrieved: the Euroclear/Clearstream "International Securities Operational Market Practice Book" (the download
returned an error page). The ICSD record-date rule for Eurobonds (reported elsewhere as the business day before
payment) is therefore **not verified**.

## 1. The TradFi servicing model (facts)

### 1.1 Who keeps the register

- **The (I)CSD.** An (I)CSD "(1) enables securities transactions to be processed and settled by book entry,
  (2) provides custodial services (e.g. the administration of corporate actions and redemptions), and (3) plays an
  active role in ensuring the integrity of securities issues" (SCoRE, glossary).
- **Holding is tiered.** The issuer CSD knows its participants, including investor CSDs that hold omnibus accounts.
  Each level informs the next "until the information reaches the End Investor" (AFME, standard 9; T2S-NOTE, the
  "cascade" effect).
- **DTC works at participant level.** It "systemically captures the position ... for each Participant as of the
  record date" and calculates each Participant's entitlement (DTC, Entitlements/Allocations).

### 1.2 Lifecycle of every corporate-action event (SCoRE, Standard 1)

`NOTIFY -> INSTRUCT -> ADVISE -> CONFIRM -> REVERSE`

| Stage | What happens |
|---|---|
| Notify | Notification, updates, cancellation |
| Instruct | Holders' instructions; elective events only |
| Advise | Entitlements calculated, preliminary advice sent |
| Confirm | Payment confirmed |
| Reverse | Reversal of a payment, if needed |

### 1.3 Key dates

- **Record date:** "Date on which settled positions are struck in the books of the issuer (I)CSD at close of business
  to determine the entitlement to the proceeds of a corporate action" (SCoRE and AFME glossaries).
- **Payment date:** "Date at which the movement is due to take place (cash and/or securities)" (SCoRE).
- **Bonds have two key dates, shares three.** "a) for Distributions relating to securities in nominal (e.g. bonds):
  Record Date and Payment Date. b) for Distributions relating to securities in units (e.g. shares): Ex Date, Record
  Date and Payment Date" (AFME, Market Standards for Cash Distributions, standard 12).
- **Spacing:** "The Ex Date should precede the Record Date by one Settlement Cycle minus one Business Day" (AFME 15).
  "The Payment Date should be as close as possible to the Record Date, preferably the next Business Day" (AFME 16).
- **Settled positions decide:** entitlements are "determined from the actual settled positions in the Issuer
  (I)CSD's books on Record Date" (AFME, introduction).
- **Kazakhstan government bonds** use no separate record date: the holder list is taken at 09:00 on the payment day
  and payment is made by 17:00 the same day (KZ-CSD, art.8 p.6).

### 1.4 Entitlement calculation and rounding (SCoRE, Standard 4)

- Apply "the ratio applicable to the entire issue ... to the total holding ... on the securities account. There
  should be no intermediate step in which a ratio per 1,000 nominal is calculated and then applied."
- Pass the ratio on unchanged through the chain: "no rounding up, or rounding down, or truncation, of the ratio".
- Round cash amounts down to the currency's decimals ("EUR 2.556 should therefore be rounded down to EUR 2.55"), at the
  CSD "and all the way down the custody chain to the last intermediary".

### 1.5 Business-day rule (SCoRE, Standard 6)

- A payment date that falls on a non-business day moves to the next business day. If that crosses into the next month
  and the payment must stay in the same month, Modified Following applies.
- "Payment amounts are always calculated on the end-of-day positions on the record date."

### 1.6 Payment time (SCoRE, Standard 8)

- European currencies: no later than 12:00 noon on the payment date. Non-European currencies: no later than 16:30
  (issuer CSD time).
- "If the corporate action event would result in both cash and security movements, then the transactions for the
  debit (or credit) of securities and credit (or debit) of cash proceeds must be executed simultaneously."
- Kazakhstan: the issuer funds the CSD by 16:00 (KZ-CSD, art.8 p.7, agent-reported) and the CSD pays by 17:00.

### 1.7 Interest payment and final maturity (SCoRE, section 4)

| Event | Cash movement | Securities movement | Key dates |
|---|---|---|---|
| INTR, interest payment (4.36) | credit ELIG x INTP (eligible holding x interest rate used for payment) | none | record date, payment date |
| REDM, final maturity (4.49) | credit ELIG x RATE x OFFR (redemption price) | debit ELIG | record date, payment date |

Rules around these events:
- "Redemptions should be processed separately from Interest Payments even if their Payment Dates coincide" (AFME,
  Market Standards for Mandatory Reorganisations, which cover "e.g. stock split, redemption", standard 15).
- For reorganisations, "the last trading date ... should precede the Record Date by at least one Settlement Cycle"
  (AFME, Mandatory Reorganisations, standard 12).
- Kazakhstan: on the redemption day the CSD stops registering trades, takes the 09:00 list, pays, and writes the bonds
  off (KZ-CSD, art.10 p.6).

### 1.8 Market claims (AFME; T2S-MC)

- **What it is:** "a process to reallocate the proceeds of a distribution to the contractually entitled party"
  (T2S-MC 1).
- **When it applies to bonds:** "from the seller to the buyer, if the Intended Settlement Date is on or before the
  Record Date but there is a Pending Transaction at close of business on Record Date" (AFME, Market Claims, standard
  1b).
- **Detection:** after close of business on the record date, for up to 20 T2S opening days (T2S-MC, standards 3-4).

### 1.9 Standard events relevant to the listing's third action (SCoRE section 4; ISO 20022 event codes)

| Code | Event |
|---|---|
| MCAL | Full call / early redemption |
| PCAL | Partial redemption without pool factor reduction |
| PRED | Partial redemption with pool factor reduction |
| DRAW | Drawing |
| BIDS | Repurchase offer / issuer bid / reverse rights |
| TEND | Tender / acquisition / takeover / purchase offer |
| BMET | Bondholder meeting |
| CONS | Consent |
| CHAN | Change |
| CONV | Conversion |
| EXTM | Maturity extension |
| DFLT | Bond default |
| DVCA | Cash dividend |

### 1.10 Reversal

- AFME: "If a Payment needs to be reversed, an announcement, including the reason for such reversal, should be made
  by the Issuer (I)CSD to all affected parties prior to processing the reversal."
- SCoRE has a separate reversal standard (Standard 13), not read in detail.

## 2. Why TradFi does it this way, where the sources say so

| Practice | Stated reason | Source |
|---|---|---|
| Market claims | The settled holder at record date can differ from the contractually entitled party when a trade is still pending | T2S-MC definition; AFME |
| Ex date = record date minus (settlement cycle minus 1 business day) | Formula stated. The CAJWG adds that "harmonisation of Settlement Cycles in Europe is required" to reap the full benefits | AFME 15, introduction |
| Issue ratio on total holding, round down, no rounding of the ratio | "cash and securities movements calculated by all account servicers and custodians in the custody chain must be in line with the ratio announced by the issuer" | SCoRE 4 |
| Cash and securities moved simultaneously | rule only, no reason stated | SCoRE 8 |
| No ex date for bonds | rule only, no reason stated | AFME 12 |
| Redemption kept separate from interest | rule only, no reason stated | AFME, Mandatory Reorganisations 15 |

## 3. Mapping to Solana

Solana facts used (SOL):
- **S1.** A transaction's instructions are atomic: "All instructions succeed or all revert". Size limit is 1,232
  bytes; "the v1 transaction format increases the size constraint to 4,096 bytes"
  (<https://solana.com/docs/core/transactions>).
- **S2.** A program reads only the accounts passed into the transaction; compute is capped at 1.4M units per
  transaction (<https://solana.com/docs/core/transactions>; the limit: <https://solana.com/docs/core/fees>, "Limits").
- **S3.** Off-chain, the RPC method `getProgramAccounts` returns every account owned by a program, with filters
  including `tokenAccountState` for Token / Token-2022 accounts
  (<https://solana.com/docs/rpc/http/getprogramaccounts>).
- **S4.** Token-2022 Pausable: when paused, the token program rejects "Transfers, Mints, Burns". Only the pause
  authority can pause and resume, and the extension must be set at mint creation
  (<https://solana.com/docs/tokens/extensions/pausable>).
- **S5.** Token-2022 Permanent Delegate: "can authorize transfers and burns for any token account for that mint".
  Holders cannot revoke it, and it must be set at mint creation
  (<https://solana.com/docs/tokens/extensions/permanent-delegate>).
- **S6.** The chain clock is "the approximate real world time of the current slot". A program runs only when a
  transaction calls it (docs.rs `solana_clock`).

The "On Solana" column states mechanisms that exist. The last column is Claude's assessment.

| # | Function | TradFi | On Solana | Easier or harder on-chain (assessment) |
|---|---|---|---|---|
| 1 | Register | Issuer CSD books plus a custody-chain cascade (1.1) | The mint's token accounts are the register; the full list is available off-chain via S3 | **Easier for direct holders:** one public register and no cascade to reconcile. **Same as TradFi for omnibus wallets:** the program sees only the omnibus account, as DTC sees only participants. |
| 2 | Record-date position | "Settled positions struck ... at close of business" (1.3); Kazakhstan takes the 09:00 list while trading continues | A program cannot read all holders in one transaction (S2). Positions must be frozen (S4), snapshotted off-chain (S3) and committed on-chain, or checkpointed on every transfer | **Harder.** A CSD reads its whole database at one instant; a Solana program cannot. |
| 3 | Ex date and market claims | Exist because settlement lags the trade (1.8, 2) | A transfer and its settlement are the same atomic transaction (S1); no pending trade exists at the record moment | **Much easier,** provided trading settles on-chain. The whole market-claim mechanism has nothing to act on. Off-chain trading with later on-chain settlement brings the lag back. |
| 4 | Entitlement and rounding | Issue ratio x total holding per account, rounded down (1.4) | Integer arithmetic in token base units with floor division, once per token account | **Same rule, easy to apply exactly.** Who keeps the rounding remainder is not stated in SCoRE. |
| 5 | Funding and payment | Issuer funds the CSD by a deadline; the CSD pays through the payment system by noon, 16:30 or 17:00 (1.6) | Issuer deposits a cash token into a vault owned by the program; the program transfers to each holder | **Faster per payment, but split across transactions.** Size and compute limits (S1, S2) force batching, so each holder must be marked paid to prevent double payment. The cash leg must be an on-chain token; the fiat rail is simulated, which the listing allows. |
| 6 | Redemption | Trading stops; cash credit and securities debit at the same time (1.6, 1.7) | Burn (holder-signed, or by a permanent delegate, S5) and payment in one atomic transaction (S1). Trading can be stopped with Pausable (S4) | **Atomic delivery-versus-payment is native, so easier.** Constraint: Pausable also blocks burns (S4), so a paused mint cannot be redeemed by burning until it is resumed. |
| 7 | Business days and calendars | Following / Modified Following on holiday calendars (1.5) | A program has no calendar; dates must be computed off-chain and stored as terms | **Same.** Calendar data lives off-chain in both worlds. The sample's Kazakh calendar comes from the python `holidays` package (`data/calendars/kz-holidays.txt`). |
| 8 | Notification | ISO 20022 messages cascaded down the chain (1.2) | Terms and event state are on-chain accounts anyone can read, plus program logs | **Easier transparency, no cascade.** ISO 20022 messaging to custodians is not native. |
| 9 | Elective events (tender, vote) | Instructions travel up the custody chain against deadlines (1.2) | The holder signs an instruction to the program directly | **Easier for direct holders. Same as TradFi for omnibus holders.** |
| 10 | Reversal | Announced, then processed by the CSD (1.10) | A confirmed transaction is not undone; a correction is a new transaction. Getting cash back from a holder needs the holder's signature or a delegate on the cash token | **Harder.** (No doc quote collected for "not undone"; it follows from S1 plus commitment levels.) |
| 11 | Time and triggering | Business days and local cut-off times; the CSD's own operations run the steps | The clock is approximate; there is no scheduler, so each step needs a transaction from someone (S6) | **Harder.** An operator or keeper must trigger each step. |
| 12 | Holder eligibility | CSD accounts belong to known, KYC'd participants | Wallets are pseudonymous; eligibility needs an allowlist (default-frozen accounts, the token-acl program) | **Harder.** It needs an extra component. |
