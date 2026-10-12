# Technical overview

Corporate actions for tokenized bonds on Solana: coupon payment, redemption at maturity, and an issuer buyback and
tender offer. Program `bond_servicing`, id `2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf`, deployed on devnet.

The system services **any bond**: every market-specific value (record day and time, payment time, time zone, weekend,
holiday calendar, coupon basis) is a bond parameter. The sample bond is the KASE listing's example (B1: face 1,000,
10%, semi-annual, 2 years) with the Kazakh depository's rules: record list at 09:00 Almaty on the payment day,
Saturday/Sunday weekend, Kazakh holiday calendar.

Detail: `bond-processing.md` (every instruction, every guarantee and its test), `buyback-tender.md` (third action).

## 1. Architecture

| Part | Where | Role |
|---|---|---|
| On-chain program | `programs/bond_servicing` (Anchor 1.1.2) | Bond token, holder registry, positions per record moment, entitlements, vaults, payments, redemption, buyback, tender. The program is the register and the paying agent |
| Bond math | `crates/bond_math` | Schedule from the bond's calendar (payment and record days), day counts, amounts. Used by the program and off-chain; checked against QuantLib |
| Operator | `ops/src/scenario.ts` | Sends the instructions on schedule. Servicing is permissionless: any wallet can do this job (`docs/devnet-evidence-keeper.md`) |
| Reference check | `tools/reference/check_report.py` | Recomputes every amount in the operator's report with QuantLib and FinancePy |
| Ledger-only audit | `tools/audit/audit.py` | Reads only the public ledger (no program accounts, no operator report) and checks balances and every payment against the terms and the run's scenario (its placements, trades and offers) |
| Demonstration site | `tools/demo/build_app.py`, `build_stages.py`, `build_timeline.py`, `build_flow.py` -> `docs/demo/` (blum.caiborg.ai) | Pages built only from measured devnet data; `build_program.py` charts the program from its source |

Off-chain parts hold no rights and no keys that matter for payments: amounts and eligibility are decided on chain.

## 2. Record-date logic

**The rule.** A coupon belongs to whoever held the bond at the record moment, whatever they hold when the money
arrives. Trading stays open on coupon day; it closes at the final record moment for redemption.

**Record moment** = record day + record time (bond-local, with the bond's UTC offset).
- The record day is N business days before the payment day (N = 0 for the sample: 09:00 on the payment day).
- Payment and record days are computed off-chain from the terms and the bond's holiday calendar
  (`bond_math::schedule`). `create_bond` does not trust them: it recomputes the regular schedule and checks each day
  (within 14 days of its unadjusted date, not on the bond's weekend, record moments increasing, record not after
  payment).
- The program reads the chain clock (`Clock` sysvar) and converts it to bond time:
  `bond time = bond origin + (chain time - chain origin) x 86400 / chain seconds per bond day`.
  Real time is 86400; devnet demos compress a two-year bond into minutes or hours.

**Positions at the record moment, without a snapshot.** A program sees only the accounts passed to a transaction, so it
cannot list all holders at 09:00. Instead, every balance change saves the holder's quantity first:
- `HolderRecord.position` (`PositionHistory`): the current quantity, how many record moments had passed at the last
  change, and one saved quantity per record moment (up to 40);
- before each change, the quantity is written for every record moment passed since the previous change;
- quantity at record moment `e` = saved value if `e` passed before the last change, else the current quantity.

Between two changes a quantity is constant, so the next change saves exactly the quantity each passed record moment
had. Nothing expires; nobody has to act at the record moment.

**Every balance change goes through the program.**
- Transfers: Token-2022 calls the program's transfer hook in the same transaction. The hook requires registered
  holders on both sides (associated token accounts), checks the record matches the token balance, saves history,
  refuses transfers below a tendered (locked) quantity, and refuses all transfers from the final record moment on.
- Placement, redemption, buyback and tender settlement are program instructions.
- Holders cannot burn: the mint's permissioned burn makes Token-2022 refuse any burn not signed by the Bond PDA.

The bond's supply has the same history (`Bond.supply`), so an event's budget uses the supply at its record moment.

## 3. Entitlement calculations

All integer arithmetic in u128, rounded down, overflow reported as an error (`bond_servicing/src/state.rs`,
`bond_math`).

| Amount | Formula |
|---|---|
| Coupon of a holder for period e | position at record moment e x face x rate / periods per year (basis `FixedPerPeriod`), or by 30/360 (Bond Basis, European, USA) on the period's dates; rounded down once on the total position |
| Principal at maturity | position x face x redemption price; partial redemptions use principal(redeemed + now) - principal(redeemed), so they round exactly like one redemption |
| Event budget | the same formula on the supply at the record moment, rounded down once; the sum of holder amounts never exceeds it, and any residue stays visible in that event's vault |
| Buyback | price per bond x quantity sold, first come first served up to the offer maximum |
| Tender | everything tendered if the total fits the maximum; else tenders are resolved in the order they were made, each accepting floor(running total x maximum / total tendered) minus what the earlier ones accepted, so an oversubscribed offer buys exactly its maximum; paid at the offer price |

Sample (the listing's example): 10 bonds at the record moment -> coupon 10 x 1,000 x 10% / 2 = 500. At maturity:
10,000 principal plus the final coupon of 500. The ledger-only audit measures this example on chain (check C6).

Amounts are checked against QuantLib and FinancePy: `crates/bond_math/tests/reference.rs` (fixtures generated by
`tools/reference/generate_fixtures.py`) and, for every run, `tools/reference/check_report.py`.

## 4. Settlement flow

Each event (each coupon, and the redemption) has its own vault: a token account of the cash token whose authority is
the Bond PDA. Cash in one vault can pay only that event.

| Step | Instruction | Who | What |
|---|---|---|---|
| 1 | `create_bond` | registrar | Mint (Token-2022, 0 decimals, extensions below), Bond account, transfer hook account list, in one transaction. Refuses cash tokens with transfer fees, transfer hooks, non-transferable extensions or a permanent delegate |
| 2 | `register_holder` | registrar | Holder registry: one `HolderRecord` per wallet per bond |
| 3 | `place` | registrar | Mints bonds to a registered holder, only before the first record moment and up to the issue size; refuses an issue whose budgets would not fit the cash token's u64 amounts |
| 4 | transfers | holders | Through the transfer hook (section 2) |
| 5 | `open_event` | anyone, after the record moment | Creates the event and its vault; budget from the supply at the record moment |
| 6 | funding | issuer | Cash into the event vault; simulated: the test bank mints it there (section 6) |
| 7 | `confirm_funding` | anyone | Vault >= budget -> funded; nobody is paid from an event that cannot pay everyone |
| 8 | `pay_coupon` | anyone, any order, after the payment moment | Position from the holder's history, amount from the terms; transfer from the vault and the holder's paid bit in one transaction; creates the holder's cash account if missing |
| 9 | `redeem_holder` | anyone, after the redemption's payment moment | Burns the holder's free bonds (permissioned burn) and pays their principal in one transaction |
| 10 | `create_offer`, funding, `confirm_offer_funding` | issuer; confirmation by anyone | Buyback or tender: price, maximum, window closing by the final record moment; own vault funded with price x maximum first |
| 11 | `sell_to_offer` | holder | Buyback: burn and pay in one transaction |
| 12 | `change_tender` | holder | Tender or withdraw during the window; tendered bonds stay in the holder's account, locked by the hook, so coupons at record moments inside the window still go to the holder |
| 13 | `resolve_tender`, `settle_tender` | anyone, after the window | Fix the accepted part and release the rest (no cash needed); then burn and pay the accepted part |

**Nothing depends on the operator.** Steps 5, 7, 8, 9, 13 can be sent by any wallet at any later time; the amounts
follow from the positions, so the sender cannot change them. Devnet run `devnet-keeper`: our operator stopped after
coupon 0; a wallet with no role in the bond paid coupons 1-3 and redeemed everyone after the final record moment.

**On-chain record of each action.**
- State: each holder's paid bit per coupon, position history, supply history, bonds burned at redemption and in
  offers, event and offer records with their vaults.
- Program events in the transaction logs: `BondCreated`, `HolderRegistered`, `BondsPlaced`, `PositionsSaved`,
  `EventOpened`, `EventFunded`, `CouponPaid`, `OfferCreated`, `OfferFunded`, `BondsBoughtBack`, `TenderChanged`,
  `TenderResolved`, `BondsRedeemed`.
- Token movements recorded by the runtime (pre/post token balances), which the ledger-only audit reads.

## 5. Solana components

| Component | Use |
|---|---|
| Anchor 1.1.2 program (Solana CLI 3.1.10) | All instructions and accounts |
| Token-2022 mint with **transfer hook** (-> this program) | Every transfer updates both holders' position histories in the same transaction |
| Token-2022 **permanent delegate** and **permissioned burn** (Bond PDA) | Redemption and offers burn the holder's bonds with the payment; holders cannot burn outside the program |
| `ExtraAccountMetaList` (transfer hook interface) | Tells Token-2022 to pass the Bond and both holder records to the hook; seeds read from the token accounts' owner field |
| Program-derived addresses | Bond `["bond", mint]`, holder record `["holder", mint, owner]`, event `["event", bond, index]`, vaults `["vault", event or offer]`, offer `["offer", bond, id]`, tender `["tender", offer, owner]` |
| Associated token accounts | One bond account and one cash account per holder |
| SPL Token (cash) | Test cash token, 6 decimals; Token-2022 cash is also supported (memo-required recipients get a Memo instruction); refused at bond creation: a transfer fee, a transfer hook, non-transferability, a permanent delegate |
| SPL Memo | For cash recipients that require memos |
| `Clock` sysvar | Bond time (section 2) |

## 6. Implemented vs simulated

| Implemented (on chain, devnet) | Simulated or outside the prototype |
|---|---|
| Test bond as a Token-2022 token with the extensions above | Cash rail: a test SPL token (6 decimals, mirrors USDC) minted by a test "bank" key, standing for fiat or a stablecoin |
| Holder registry, positions at record moments, entitlements, event vaults, coupon payment, redemption with burn, buyback, tender offer | Issuer funding: the bank key mints cash into the vaults |
| On-chain record (state, program events, token movements) | Investor onboarding and KYC: the registrar registers wallets; no identity checks |
| Permissionless servicing: anyone can open, confirm, pay, redeem, settle | Holders: generated keypairs, labelled BH#1, BH#2, ...; trades are direct bond transfers between registered holders: no order book, and no cash leg between buyer and seller |
| Ledger-only audit from public ledger data | Time: devnet runs compress bond time (1 bond day = 1 to 20 chain seconds); real time is 86400 |
| | No integration with KASE or the central securities depository; the Kazakh calendar and 09:00 rule are bond parameters, used as the sample |

## 7. Evidence

Every run is on the program as deployed: a build of this repository gives it byte for byte (sha256 `327271f3...`).
Each devnet run has its own bond and cash token.

| Run (devnet) | Holders | Transactions | Report check | Ledger-only audit |
|---|---|---|---|---|
| `devnet-crowd-1000`: 600 trades, buyback, tender bought exactly its maximum (410 of 441 tendered), 4 coupons, redemption | 1000 | 2,318 | passed | 3,959 of 3,959 |
| `devnet-full8`: 8 trades, buyback, tender bought exactly its maximum (6 of 10 tendered), 4 coupons, redemption | 8 | 50 | passed | 43 of 43 |
| `devnet-keeper`: operator stops after coupon 0, an outside wallet finishes | 3 | 25 | passed | 23 of 23 |
| `devnet-hostile`: an attacker at every step; 35 of 35 attempts refused, each a failed transaction | 6 | 53 | passed | 35 of 35 |

- Tests: 36 LiteSVM lifecycle tests (11 of them hostile attempts beyond the devnet attacker run) and 5
  permissioned-burn probes (`programs/bond_servicing/tests`); every hostile attempt, devnet and local:
  `docs/hostile-attempts.md`; bond math:
  14 unit tests and 6 reference tests against QuantLib and FinancePy fixtures, including MUM108_0015's payment dates
  as published by KASE (`crates/bond_math/tests/reference.rs`).
- Per-run accounts and transactions: `devnet-evidence-crowd-1000.md`, `devnet-evidence-keeper.md`; the 8-holder run,
  transaction by transaction: the timeline and flow pages.
- Measured on the 1000-holder run (parties page; `devnet-evidence-crowd-1000.md`):
  - 4,000 of 4,000 holder-coupon amounts equal the record-moment holdings; paying the holder at payment time would
    have got 1,039 wrong;
  - paying one holder one coupon costs 833-2,500 lamports, with no new storage (up to 6 holders share one
    5,000-lamport transaction).
- Probes of the audit, on a local network (sample bond B1, 3 holders; the audit passes 23 of 23 checks first):
  - an extra 0.000001 cash sent into a vault from outside makes 2 of 23 checks fail, M4 and C4 (nothing was taken;
    the audit flags the unexpected deposit);
  - expecting 10.01% instead of the bond's 10% makes 7 of 23 fail: C1 and C7 for each of the 3 holders, and C4.

## 8. Limits

- Bond types: fixed rate above zero; `FixedPerPeriod` and 30/360 only (no Actual/Actual, Actual/360, Actual/365);
  regular schedules (no stub periods); at most 40 coupon periods; direct holders only (no omnibus custodians).
- The registrar is trusted to register holders and to place bonds up to the issue size before the first record
  moment; everything after registration can be done by anyone.
- The cash token's issuer can freeze a vault or pause its token: payments stop until it acts again; nothing owed is
  lost (`a_frozen_vault_pays_nobody_until_thawed`).
- Tender allocation: an oversubscribed offer buys exactly its maximum; which tenders receive a rounding bond follows
  the order the tenders were made.
- The program's upgrade authority is a single wallet. A multisig or an immutable program, and any pause or
  intervention path, are governance decisions not yet made.
- The audit checks the whole cash token (sum of balances = supply), which needs that token's full history. That fits
  a test token; for a widely used stablecoin the cash checks must be scoped to the bond's vaults.
- Throughput: each payment of one coupon writes that event's vault. Measured on devnet (8-holder run, flow page,
  step Pay): one transaction paying 5 holders used 119,365 compute units, about 24,000 per holder
  paid, so with Solana's per-account limit per block (12 million units in the current cost model) one vault takes
  about 500 holder payments per block. The devnet runs were slower for a
  different reason: one operator sending one transaction at a time through the rate-limited public RPC.
