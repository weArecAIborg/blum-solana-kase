# Buyback and tender offer: design (third corporate action)

Status: implemented and run on devnet. Builds on the rights model of `bond-processing.md` (position history, permissioned burn, per-event vaults).

The system must serve any bond; the sample references are the KASE listing ("tender offer or buyback") and the
Kazakh rules in `corporate-actions.md`.

## 1. What the action is

| | Buyback | Tender offer |
|---|---|---|
| Issuer offers | a price per bond, up to a maximum quantity | a price per bond, up to a maximum quantity, during a window |
| Holder | sells at once | tenders a quantity during the window and may withdraw it before the window closes |
| Allocation | first come, first served until the maximum is reached | after the window: all of it if the total tendered fits the maximum, otherwise pro rata |
| Settlement | the same transaction: bonds burned, price paid | after the window, per holder: accepted bonds burned, price paid, the rest unlocked |

Both use one engine, an **Offer** whose allocation rule is either first-come or pro-rata.

## 2. Rules the design must keep

1. **Coupon rights stay with the holder until the bonds are actually bought.**
   - Tendered bonds are **locked in the holder's own account**, not moved to an escrow. The holder record carries a
     `locked` quantity, and the transfer hook refuses any transfer that would take the balance below it.
   - The bonds never change accounts, so every record moment during the window is saved to the holder as before.
     The holder keeps those coupons.
2. **No bond is retired unpaid.** Settlement burns (permissioned burn, Bond PDA) and pays in one transaction, exactly
   as redemption does. The position history is saved first, so past coupons stay payable.
3. **Supply history stays exact.** Every buyback or tender burn updates the bond's supply history. Later events'
   budgets therefore reflect the smaller supply automatically.
4. **The offer is fully funded before anyone can accept or tender.** The offer has its own vault
   (`["vault", offer]`), and the budget is price x maximum quantity. Offer cash cannot pay coupons, and coupon cash
   cannot pay the offer.
5. **No double use of a bond.** Locked bonds cannot be tendered twice or transferred. Settlement happens once per
   holder per offer.
6. **Deterministic pro-rata in whole bonds, exactly the maximum.**
   - Tenders are numbered as they are made and resolved in that order (`TenderOutOfOrder` otherwise).
   - Accepted = floor(running total x maximum / total tendered) - floor(running total before this tender x maximum /
     total tendered). The shares telescope: an oversubscribed offer buys exactly its maximum.
   - Each tender receives at least floor(tendered x maximum / total tendered), never more than it tendered, and
     differs from its exact share by less than one bond. The order is fixed when tenders are made, so nobody can gain
     by resolving at a chosen moment.
7. **Deadlines.** The window must close before the final record moment, when trading closes for redemption. After
   the window anyone resolves the tenders, in the order they were made, and settles each accepted part, in any order
   once resolved.

## 3. Accounts and instructions

| Item | Content |
|---|---|
| `Offer` PDA `["offer", bond, id]` | kind (FirstCome / ProRata), price per bond (cash base units), maximum quantity, window open and close (bond time), total tendered or bought, funded, tenders made, tenders resolved and the quantity they tendered |
| Offer vault `["vault", offer]` | Cash token account, authority = Bond PDA |
| `Tender` PDA `["tender", offer, owner]` | Its number in the offer (the resolution order), quantity tendered, resolved flag and accepted quantity, settled flag (pro-rata only) |
| `HolderRecord.locked` | Bonds locked in open tenders |

| Instruction | Who | Does |
|---|---|---|
| `create_offer(id, pro_rata, price_units, max_quantity, open_time, close_time)` | registrar (issuer) | Creates the offer and its vault; checks the window closes by the final record moment |
| `confirm_offer_funding` | anyone | Vault >= price x max -> funded |
| `sell_to_offer(quantity)` (FirstCome) | holder | Within the window and the remaining maximum: save history, burn, pay price x quantity |
| `change_tender(quantity, withdraw)` (ProRata) | holder | Within the window. Tender: quantity <= unlocked balance; lock it; add to the offer total (a holder's first tender takes the next number). Withdraw: at most what was tendered; unlock it; subtract it |
| `resolve_tender` | anyone, after close, in tender order | Fixes the accepted part (rule 6) and unlocks the rest; no cash (section 3a) |
| `settle_tender` | anyone, after close | Resolves first if nobody has; save history, burn the accepted part, pay, unlock it |

## 3a. Outcomes after the window

Each tender has two separate outcomes after the window:

| Outcome | Instruction | Needs |
|---|---|---|
| Accepted part fixed, rejected part released | `resolve_tender` (anyone, idempotent) | The closed offer, the tender, the holder record. No cash, no token accounts |
| Accepted part bought: burned and paid | `settle_tender` (anyone; resolves first if nobody has) | The offer vault's cash. Takes the offer read-only |

If the offer's cash cannot move (for example, the cash issuer froze the vault), the rejected bonds are still released,
traded and redeemed. Only the accepted bonds wait for the offer. At maturity, redemption pays the holder's free bonds
and leaves accepted ones to their offer. Principal is computed on the cumulative quantity redeemed, so partial
redemptions round exactly like one.

## 4. What it demonstrates

- Holder elections: coupons and redemption are the same for every holder; selling or tendering is the holder's choice.
- Rights preserved across a corporate action:
  - a holder who tenders before a record moment still receives that coupon;
  - a holder whose bonds are bought keeps coupons already earned.
- The same evidence chain as coupons and redemption: LiteSVM probes, a devnet run, the report check and the
  independent audit.

## 5. Not included

- **Price.** The design takes an all-in price per bond set by the issuer. A price "face + accrued interest" needs
  the bond's day-count variant for a broken period (`corporate-actions.md`, hot spot), so it is not included.
- **After purchase.** The design cancels the bonds at once (burn). Holding them in treasury is not included.
