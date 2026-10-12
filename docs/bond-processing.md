# Bond processing: the logic the system uses now, why, and how it maps to Solana

The program as deployed on devnet. Program `bond_servicing`
(`programs/bond_servicing/src/`, id `2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf`), bond math in `crates/bond_math`.
Implemented actions: coupon payment, redemption at maturity, and the third action: issuer buyback and tender offer
(design: `buyback-tender.md`).

**Scope: the system must service any bond**, from any market, issuer, currency or calendar. Kazakhstan is used
**only as a sample**: the KASE listing's example bond B1, the MEUKAM bond MUM108_0015, and the Kazakh depository's
rules. Every market-specific value (record day and time, payment time, time zone, weekend, holiday calendar, coupon
formula) is a bond parameter, never a rule in the code. Section 10 lists where the code does not meet this yet.

## 1. The rule the whole design serves

Any bond pays a coupon to whoever held it at the **record moment**. After that moment trading may continue; the
holders at the record moment are paid, whatever they hold when the money arrives. At maturity, trading stops and
balances are frozen until the bonds are written off.

Sample (Kazakh CSD, `bond-mechanics.md`): the list is taken at 09:00 Almaty time on the payment day (art.8 p.6),
trading is not stopped on coupon day, and it stops on redemption day (art.10).

The obstacle on Solana: a program sees only the accounts passed into a transaction, so it cannot read every holder at
the record moment. The answer used here: **every balance change first saves the holder's quantity for every record
moment passed since its previous change**. Nobody has to look at the record moment, and nothing has a deadline.

## 2. On-chain objects

| Object | What it is | Address (PDA seeds) | Solana mechanism |
|---|---|---|---|
| Bond token | Token-2022 mint, 0 decimals, created by the program. Extensions: permanent delegate, transfer hook -> our program, **permissioned burn**. Mint authority, delegate and burn authority = Bond PDA | keypair chosen by the issuer | Token-2022 mint extensions |
| Bond | Terms, time table, time model, **supply history** | `["bond", mint]` | Program-derived address. It has no private key; only our program can sign for it |
| HolderRecord | One per registered holder: **position history** and a paid bit per coupon | `["holder", mint, owner]` | Its existence is the registration |
| Event | One per coupon, plus one for the redemption, created after its record moment: supply at the record moment, budget, vault, funded flag | `["event", bond, index]` | Read-only on the payment path |
| Event vault | Cash for that event only | `["vault", event]`, authority = Bond PDA | Token account of the cash token |
| Hook account list | Tells Token-2022 which extra accounts to pass to the hook; written by `create_bond` | `["extra-account-metas", mint]` | Transfer-hook interface; holder record seeds are read from the token accounts' owner field |

**Position history** (`state.rs`, `PositionHistory`), the pattern of OpenZeppelin's ERC20Snapshot fitted to a fixed
schedule:
- `quantity`: the quantity now;
- `live_from`: how many record moments had passed at the last change;
- `at[40]`: one saved quantity per record moment.

The quantity at record moment `e` is `at[e]` if `e < live_from`, else `quantity`. Before every change, `at` is filled
for all record moments passed since the last change. Nothing is overwritten and nothing expires.

## 3. Lifecycle, step by step

| # | Instruction | Who calls | What it does | Why | Solana mechanism |
|---|---|---|---|---|---|
| 1 | `create_bond` | registrar | Creates the mint with its extensions, the Bond account and the transfer hook's extra-account list (Bond PDA, source and destination holder records), in one transaction. Checks the payment and record days against the regular schedule recomputed from the terms: each payment within 14 days of its unadjusted date; payment and record days not on the bond's weekend; record moments strictly increasing; each record moment not after its payment moment. Refuses cash tokens that change or condition delivery, or whose permanent delegate could move a vault's cash. The issue size (`max_supply`) is a term | The configuration is created, not trusted; trading can never start without the hook's account list | CPIs to the system program and Token-2022; permissioned burn encoded by hand (the bundled interface crate predates it); `ExtraAccountMetaList` |
| 2 | `register_holder` | registrar | Creates a HolderRecord. Its history starts at the current record moment, so a late holder has zero at earlier ones. Reads the Bond, does not write it | Holder registry (simulated KYC); registrations do not contend with transfers | PDA `init` |
| 3 | `place` | registrar | Mints bonds to a registered holder, **only before the first record moment** and **up to the issue size**. Refuses a supply whose coupon or redemption budget would not fit the cash token's u64 amount. Updates holder quantity and bond supply | Supply changes only through the program; an issue that cannot be serviced is never created | CPI `mint_to`, signed by the PDA |
| 4 | *every transfer* -> `transfer_hook` | Token-2022, automatically | See section 5 | The record-moment rule with the market open | Transfer-hook extension, same transaction |
| 5 | `open_event` | **anyone**, after the record moment | Creates the Event and its vault. Supply at the record moment comes from the supply history. Budget = the event's amount for the whole supply, rounded down once | Everything it writes follows from the terms, so no authority is needed | PDA `init`; token account `init` |
| 6 | *issuer funds the vault* | issuer (bank) | Transfers cash into the event's vault | Fiat leg; may be simulated (listing) | Plain token transfer or mint |
| 7 | `confirm_funding` | **anyone** | Vault balance >= budget -> funded | No holder is paid from an event that cannot pay all | Reads the vault |
| 8 | `pay_coupon` | **anyone**, any order, any time after the payment moment | Position = holder's history at the record moment; amount from the terms; pays from the event vault and sets the paid bit, in one transaction. Creates the holder's cash account if missing. If that account requires memos (Token-2022), a Memo instruction is invoked right before the transfer | Writes only holder-local accounts and the event vault; Bond and Event are read-only, so payments do not conflict with trading. No bond account needed | CPI `transfer_checked` signed by the PDA; atomic transaction |
| 9 | `redeem_holder` | **anyone**, after the payment moment of the redemption | Saves history, then burns the holder's **free** bonds and pays their principal, in one transaction. Bonds still accepted by an unsettled offer are left to that offer. Principal = principal(redeemed so far + now) - principal(redeemed so far), so partial redemptions round exactly like one | No bond is burned unpaid, no principal paid for a live bond; a blocked offer cannot hold back the rest; the final coupon stays payable separately | Permissioned burn signed by the PDA as burn authority and permanent delegate |
| 10 | `create_offer` + funding + `confirm_offer_funding` | registrar (issuer); confirmation by anyone | An offer to buy back bonds at a price per bond, up to a maximum, in a window that closes no later than the final record moment. Its own vault must hold price x maximum before anyone can sell or tender | Offer cash and coupon cash cannot mix | PDA `["offer", bond, id]`, vault `["vault", offer]` |
| 11 | `sell_to_offer` (buyback) | holder | Within the window and the remaining maximum: burns the holder's bonds and pays the price, in one transaction | First come, first served | Shared burn-and-pay settlement (`settlement.rs`) |
| 12 | `change_tender` (tender offer) | holder | Within the window: tender more bonds or withdraw. Tendered bonds stay in the holder's account, **locked**: the hook refuses transfers below the locked quantity | The bonds never move, so coupon rights at record moments during the window stay with the holder | `HolderRecord.locked`; `Tender` PDA per holder |
| 13 | `resolve_tender` | **anyone**, after the window, in the order the tenders were made | Fixes the accepted part, everything tendered or, if oversubscribed, the running-total pro-rata share minus what earlier tenders received (the offer buys exactly its maximum), and releases the rejected part. Needs no cash and no token accounts; idempotent | A frozen or blocked offer vault cannot keep rejected bonds locked | `Tender.resolved`, `Tender.accepted` |
| 14 | `settle_tender` | **anyone**, after the window | Resolves first if nobody has (in order), then burns and pays the accepted bonds and unlocks them | Deterministic pro rata in whole bonds, exactly the maximum when oversubscribed | Same settlement as redemption (`settlement.rs`) |

## 4. Event states

An event is either not yet opened (its record moment has not passed, or nobody opened it), open, or funded. There is
no recording state. Whether a holder is paid is the holder's own bit. Any coupon can be opened,
funded and paid at any time after its moments, independently of every other coupon and the redemption.

## 5. The core algorithm

**Every balance change is seen by the program:**
- transfers through the hook;
- placement and redemption are program instructions;
- holders cannot burn: permissioned burn makes Token-2022 refuse every burn not signed by the Bond PDA.

**On every transfer the hook:**
1. checks the account was really being transferred (Token-2022's `transferring` flag);
2. requires both sides to be associated token accounts, so one holder means one position;
3. requires both sides to have a HolderRecord, so only registered wallets can hold bonds;
4. refuses the transfer from the final record moment on (trading closed for redemption);
5. for each side:
   - checks the record agrees with the token account before the transfer; any disagreement means a balance changed
     outside the program, and the transfer is refused;
   - saves the quantity for the record moments passed since the side's last change;
   - sets the new quantity.

**Why every position is exact.** Between two changes a holder's quantity is constant. Every record moment that passed
in between therefore had that quantity, and the next change saves exactly that quantity for exactly those moments.
Record moments after the last change have the current quantity. This holds however late anyone looks, and it needs no
operator.

**Why funding needs no holder count.** Supply changes only through the program, which keeps its own supply history.
The budget is the amount for the whole supply, rounded down once. Each holder's amount is rounded down separately, so
the sum of holder amounts never exceeds the budget. The difference, if any, stays visible in that event's vault.

Measured on devnet, 1000-holder run (`devnet-evidence-crowd-1000.md`):
- 600 trades settled 4 to 452 s after record moments;
- 4,000 of 4,000 holder-coupon amounts equal the record-moment holdings;
- paying by the balance at payment time would have been wrong in 1,039 of 4,000.

## 6. Time

- Per period: a **record day** and a **payment day**, computed off-chain with the bond's calendar (holiday file and
  weekend from the scenario; the sample uses `data/calendars/kz-holidays.txt`, Saturday and Sunday).
- Bond-level: record local time, payment-opening local time, UTC offset.
  - Record moment = record day + record time.
  - Payment moment = payment day + payment-opening time (sample: both 09:00 on the payment day, UTC+5).
  - A record day several business days before payment is supported and tested.
- The program reads the chain clock (`Clock` sysvar) and converts it to bond time:
  `bond time = bond_origin + (chain time - chain_origin) x 86400 / chain_seconds_per_bond_day`.
  - 86400 is real time.
  - Devnet demos use 1 to 20, sized so each phase between record moments fits its transactions on the public RPC:
    1 for 6 holders (a two-year bond in about 13 minutes), 20 for 1000 holders (about 4 hours).

## 7. Amounts

- Coupon on the holder's **total** position, rounded down once, with no per-bond rounding (SCoRE Standard 4).
- Two bases, chosen per bond: `FixedPerPeriod` (S = N x C / periods per year) and 30/360 (three variants).
- Principal = position x face x redemption price, rounded down.
- Event budget = the same formula on the whole supply.
- All integer arithmetic in u128, with overflow reported as an error. Checked against QuantLib and FinancePy
  (`crates/bond_math/tests/reference.rs`, `tools/reference/check_report.py`).

## 8. What cannot happen, and what stops it

| Cannot happen | Stopped by | Evidence (LiteSVM test) |
|---|---|---|
| A holder burns bonds outside the program (earned coupon or redemption lost) | Permissioned burn: Token-2022 refuses | `holder_cannot_burn_bonds_outside_the_program`; probes in `permissioned_burn.rs` |
| A closed or never-created bond account blocks payments | Positions come from the holder record, not the token account | `closed_bond_account_blocks_nobody` |
| A late operator loses or blocks rights | No recording step, no deadline; history never expires | `missed_coupons_stay_payable_after_later_record_moments` |
| Rounding residue reported as a liability | Budget per event; residue stays visible in that event's vault | `redemption_rounding_leftover_stays_in_its_vault` |
| Payment before the payment moment | `payment_time` check | `record_day_before_payment_day` |
| A cash token that delivers less than sent | Refused at bond creation | `cash_token_with_transfer_fee_is_refused` |
| A cash token whose permanent delegate could take a vault's cash | Refused at bond creation | `cash_token_with_permanent_delegate_is_refused` |
| Bonds placed beyond the issue size | `place` checks the new supply against `max_supply` | `placement_beyond_the_issue_size_is_refused` |
| A wallet outside the registry holds bonds | Hook requires a HolderRecord on both sides | `unregistered_wallet_cannot_receive_bonds` |
| Bonds placed after the first record moment | `place` time check | `placement_closes_at_first_record_moment` |
| Trading during redemption | Hook time check | `transfers_close_at_final_record_moment` |
| A coupon paid twice | Paid bit in the holder record | `coupon_cannot_be_paid_twice` |
| Payment from an underfunded event | `confirm_funding` | `open_before_record_moment_and_pay_before_funding_fail` |
| One unpayable holder (frozen cash account) blocks others | Payments are independent | `unpayable_holder_does_not_block_others_or_next_event` |
| A late-registered holder claims past coupons | History starts at registration | `late_registered_holder_gets_nothing_for_past_record_moments` |
| Anyone but the registrar registers | `has_one = registrar` | `non_registrar_cannot_register` |
| An issue too large to service (a budget beyond the cash token's u64) | `place` checks every coupon and the redemption budget | `issue_too_large_is_refused_at_placement` |
| A blocked offer vault keeps rejected tender bonds locked | `resolve_tender` releases them without cash | `rejected_tender_bonds_are_released_while_offer_cash_is_frozen` |
| An oversubscribed tender buys fewer bonds than its maximum, or a holder times its resolution to gain a bond | Running-total allocation, tenders resolved in the order they were made | `oversubscribed_tender_buys_exactly_its_maximum_in_tender_order` |
| A pending offer holds back the holder's redemption | Redemption pays the free bonds now | `redemption_pays_free_bonds_while_an_offer_is_pending` |
| Partial redemptions round differently from one | Principal on the cumulative quantity | `partial_redemptions_round_like_one` |
| A cash account that requires memos cannot be paid | Memo instruction invoked right before the transfer; a clear error if the Memo program is not passed, and the right stays unpaid | `memo_required_cash_account_is_paid` |
| A buyback buys more than offered, or before funding | Offer maximum, funded flag | `buyback_first_come_until_the_maximum` |
| Tendered bonds are sold to someone else, or tendered twice | Lock checked by the hook and by `change_tender` | `tender_pro_rata_keeps_coupon_rights_during_the_window` |
| A tender loses the coupon at a record moment inside the window | Bonds stay in the holder's account | same test: the coupon is paid on the locked bonds |
| An offer outlives trading | Window must close by the final record moment | `offer_must_close_before_the_final_record_moment` |
| A tendered bond is redeemed and also bought by the offer | Redemption refused while bonds are locked | `redemption_waits_for_tender_settlement` |

## 9. Off-chain parts

- **Operator** (`ops/src/scenario.ts`): sends the instructions on schedule. Opening, funding confirmation and payment
  are permissionless, so any keeper can take over.
- **Reference check** (`tools/reference/check_report.py`): checks the operator's report against QuantLib and
  FinancePy, using the bond's own calendar.
- **Independent audit** (`tools/audit/audit.py`): reads only the public ledger. It finds the event vaults as the cash
  accounts owned by the bond token's mint authority, and compares measured flows with the terms
  (`expertise/independent-ledger-audit.md`).

## 10. Limits of the current logic

**Where the code does not yet serve any bond:**
- **Fixed rate only, rate > 0.** Zero-coupon bonds are refused. Floating rate and amortizing bonds are not modelled.
- **Day counts:** only `FixedPerPeriod` and three 30/360 variants. Actual/Actual, Actual/360 and Actual/365 are
  missing.
- **Regular schedules only:** no stub periods.
- **At most 40 coupon periods.** A 30-year semi-annual bond (60) does not fit.
- **Direct holders only.** Bonds are held in holders' own associated token accounts. Omnibus custodians and trading
  venues' escrow accounts are not modelled.

**Other limits:**
- The registrar is trusted to register holders and to place bonds up to the issue size before the first record moment.
  Everything after registration can be done by anyone.
- Tender allocation: an oversubscribed offer buys exactly its maximum (running total, tenders resolved in the order
  they were made); which tenders receive a rounding bond follows that order.
- The program's upgrade authority is a single wallet (`deploy.sh` default). A multisig or an immutable program is a
  governance decision not yet made; so is any pause or intervention path.
- Each payment is one instruction per holder per event; the operator packs up to 6 into one transaction (redemption:
  4). All payments of one event write that event's vault, so one vault takes a bounded number of payments per block
  (`technical-overview.md`, section 8).
- The transfer hook loops over at most 40 record moments per side.
