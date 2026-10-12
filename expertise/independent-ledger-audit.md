# Independent ledger audit: measure balances, do not calculate them

The audit every run passes: each devnet run in the README, and the local runs and probes in section 8.

## 1. Problem

Two kinds of tests already pass:
- unit and lifecycle tests, which check the program against hand arithmetic;
- a report checker (`tools/reference/check_report.py`), which recomputes amounts with QuantLib and FinancePy.

Both read data that our own system produced: the operator's report, our program's accounts, our IDL. If the system
records something wrong in a consistent way, the system and its checks agree and the error stays hidden.

What is needed is an observer that:
- stands outside the system;
- looks only at what the ledger itself records;
- **measures** every wallet's balances instead of calculating them;
- compares those measurements with what the bond terms say should have happened.

Measured and calculated must match. Any difference proves the system behaved other than expected.

## 2. Principle

> The ledger is the witness. The auditor measures token balances and balance changes as the runtime recorded them,
> then sums and compares. It never asks the system what it did.

## 3. Independence rules

| The auditor MAY read | The auditor MUST NOT read |
|---|---|
| Public mint addresses (bond token, cash token) | Our program's accounts (Bond, HolderRecord, Event, Offer, Tender) |
| Standard Solana RPC: `getProgramAccounts`, `getMultipleAccounts`, `getTokenSupply`, `getSignaturesForAddress`, `getTransaction` | Our IDL or Anchor client |
| The **scenario input**: bond terms, wallet addresses, placements, trades, offers, the bond's calendar origin. This is the same file the operator acts on | The operator's report or logs |
| Reference math (QuantLib, FinancePy), for the calculated side only | Any code shared with the operator or the program |

Implementation constraints that follow:
- **Measurement side:** Python standard library only, with no Solana SDK, so no shared client code can bias it.
- **Classification by public token data, not by program knowledge.** Example: the bond's vaults are identified as "the
  cash accounts owned by the bond token's mint authority". The mint authority is a public field of the bond mint; the
  auditor does not derive it from our program's PDA seeds.

## 4. Two measurement channels

Both come from the ledger, and neither depends on our code.

| Channel | RPC | What it yields |
|---|---|---|
| **S, state** | `getProgramAccounts(token program, filter mint at offset 0, jsonParsed)` and `getTokenSupply` on a local network; on public devnet, which refuses that scan, `getMultipleAccounts` over every token account seen in the history (`history-accounts`) | Every token account of a mint, with owner and amount as parsed by the node, plus the mint supply. A snapshot of all wallets |
| **H, history** | `getSignaturesForAddress(mint)` then `getTransaction(signature, jsonParsed)` | Every transaction that touched the mint, with `meta.preTokenBalances` / `postTokenBalances` recorded by the runtime for every account involved, `blockTime`, and the node-parsed instructions, including inner (CPI) instructions such as `mintTo` and `burn` |

## 5. Checks

### 5.1 Model-free invariants (no bond math; failure means the ledger contradicts itself or the system)

| Id | Check |
|---|---|
| M1 Completeness | For each mint, the sum of all measured account balances (S) = measured supply |
| M2 Conservation | For every transaction, the sum of balance changes of a mint (H) = amount minted - amount burned in that transaction (node-parsed `mintTo` / `burn` instructions, inner ones included). A plain transfer must sum to zero |
| M3 Channels agree | For every account, the sum of its balance changes over history (H) = its current balance (S) |
| M4 Vaults and strangers | The bond's vaults are the cash accounts owned by the bond token's mint authority (one per event and one per offer). Cash enters them only through the bank's mints and leaves them only to scenario wallets. Every bond account with any activity belongs to a scenario wallet |

### 5.2 Measured against calculated (exact integers in base units, no tolerance)

The calculated side uses only the scenario input and reference math. It derives each holder's position at each
record moment from placements, trades and offers (section 9), the coupon per bond from FinancePy FIXED (the Kazakh
decree formula), and principal = face x position.

| Id | Check |
|---|---|
| C1 Coupons | Each holder's measured cash arrivals from the bond's vaults, in time order and excluding principal, equal the calculated list of non-zero coupons |
| C2 Bond movements | Each holder's measured bond balance changes, in time order, equal the calculated list: placement +, trades -/+, retirements - |
| C3 Retirements | Every transaction in which a holder's bonds are burned and the holder receives cash from a vault (buyback sale, tender settlement, redemption): the measured (burned, cash) pairs, in ledger order, equal the calculated ones (offer price x bonds, or face x price x bonds at redemption) |
| C4 Vaults | Measured inflow into the vaults (bank mints) = measured outflow + final balances; outflow = sum of calculated coupons, principal and offer payments; final balances = the calculated leftover (each event is funded with its amount for the whole supply rounded down once, holders are rounded down one by one; unfilled offer cash) |
| C5 Timing | Each payment's bond time = blockTime - creation blockTime, scaled by the run's chain seconds per bond day, + bond time at creation (from the scenario input). Real time (86,400 chain seconds per bond day): each coupon is paid on its payment date, between the record time and the payment deadline of the bond's terms (sample: 09:00 and 17:00 Almaty, KZ CSD art.8 p.6). Compressed time, as on every devnet run: each coupon is paid after its record moment and before the next one; when the scenario declares that the operator stops, the later coupons after the final record moment. The 09:00-17:00 window is then a fraction of a minute and is not checked |
| C6 Listing example (B1) | The holder with 10 bonds at the first record moment measurably receives 500 |
| C7 Final balances | Each holder's measured cash from the bond's vaults, and bonds held now (channel S), equal the calculated totals. C1 sees only arrivals; C7 also catches value a holder sent away or destroyed |

## 6. Why a failure is certain proof

- Both sides are integers in token base units. Measured values come from the runtime's own records; calculated
  values come from the bond terms. Any non-zero difference is a mismatch, with no rounding tolerance to argue over.
- The model-free checks (M1-M4) need no interpretation of the bond terms at all. If one fails, the system moved value
  in a way the ledger itself does not reconcile, whatever the terms mean.
- The auditor cannot be fooled by a consistent error inside the system, because it never reads the system's own
  records.

## 7. Limits

- **The RPC node is the witness.** The devnet audits read one provider, public devnet RPC; explorer.solana.com is a
  second, human-readable witness for every transaction.
- **The calculated side is only as right as the reading of the terms.** A shared misreading (by the program and the
  checker) would pass C1-C7. M1-M4 are immune to that, and the terms reading is checked separately against QuantLib,
  FinancePy and KASE's published payment dates (`crates/bond_math/tests/reference.rs`).
- **The scenario is the reference.** The audit proves that the ledger matches the scenario's placements, trades and
  offers, not that those were the intended ones.
- **History depth.** `getSignaturesForAddress` is paginated, and public RPC nodes may prune old history. The auditor
  pages until exhausted and reports the count. If history is incomplete, M3 fails, so the gap cannot pass silently.
- **Only this bond is judged** (section 9). Holders' cash movements that do not touch the bond's vaults (other bonds
  paid in the same cash token, a holder's own transfers) are listed, not judged.
- **The whole cash token is summed** (M1), which needs that token's full history: right for a test token; for a widely
  used stablecoin the cash checks must be scoped to the bond's vaults.

## 8. Implementation and results

**Files.**
- `scenarios/*.json`: the scenario input. Bond terms, calendar file, bond time at creation, key files (bond mint, cash
  mint, bank), holder wallets, placements, trades and offers. The operator (`ops/src/scenario.ts`) acts on it; the
  auditor measures against it. Wallets and the bond mint are fixed keypairs (`keys/`, not in the repository), so every
  address is known before anything runs.
- `tools/audit/audit.py`: the measurement side is standard library only. Public keys are read from the keypair files'
  last 32 bytes, base58 is encoded by hand, and RPC is raw JSON over `urllib`. The calculated side is QuantLib and
  FinancePy, run on the scenario input only.
- `scripts/run-local.sh <scenario>` (a fresh local network with the program deployed) and `scripts/run-devnet.sh <name>`
  (the program deployed on devnet) create the cash token, then run the operator, the report checker and the audit.

**Results.** Each devnet run has its own bond and cash token, so its audit reads only that bond's transactions.

| Run | Network | Result |
|---|---|---|
| `devnet-crowd-1000`, 1000 holders | devnet | AUDIT PASSED, 3,959 of 3,959 checks |
| `devnet-full8`, 8 holders | devnet | AUDIT PASSED, 43 of 43 |
| `devnet-keeper`, operator stops after coupon 0 | devnet | AUDIT PASSED, 23 of 23 |
| `devnet-hostile`, attacker at every step | devnet | AUDIT PASSED, 35 of 35 |
| `b1`, 3 holders | local (Surfpool) | AUDIT PASSED, 23 of 23 checks, 24 transactions |

**Probes.** On the local `b1` ledger, after the audit passed 23 of 23:

| Probe | Ledger change | Audit result |
|---|---|---|
| BH#1 sends 0.000001 cash straight into one of the bond's vaults (`tools/audit/tamper_probe.py`; `docs/demo/data/tamper-probe-local.json`) | real transfer, outside the program and not from the bank | FAILED, 2 of 23: M4 "cash enters the vaults only from the bank's mints" and C4 |
| The scenario expects 10.01% instead of the bond's 10% | none | FAILED, 7 of 23: C1 and C7 for each of the 3 holders (BH#1 measured 500, 350, 350, 350 vs calculated 500.5, 350.35, 350.35, 350.35), and C4 |

## 9. How the calculated side follows the bond

- **One bond.** M1-M3 stay global over both mints: the whole ledger must reconcile. M4, C1 and C4 count only flows
  through the bond's vaults. C7 compares, per holder, the cash received from the vaults plus the bonds held now with
  the calculated totals; cash movements outside this bond are listed as "outside this bond: N cash movements, net X".
- **Vaults.** One per event and one per offer, all found as the cash accounts owned by the bond token's mint authority
  (a public field of the mint).
- **Burns.** Retirements use Token-2022's PermissionedBurn instruction (46). A node that does not parse it returns the
  raw instruction; the auditor decodes it (base58, standard library): sub-instruction 1 or 2, amount in bytes 2..10,
  mint = second account. M2 (balance changes = minted - burned) holds over every retirement.
- **Record dates.** The scenario may put the record day business days before the payment day
  (`recordBusinessDaysBeforePayment`). The calculated side takes record dates from the bond's calendar (holiday file
  and weekend from the scenario).
- **Offers.** The calculated side replays the scenario in the operator's order (`timeline` in `audit.py`): after each
  record moment, trades, then offer actions. A buyback sale retires bonds at once. A tender offer is settled in the
  first phase after its window closes: in tender order, each tender taking the running-total pro-rata share minus that
  of the tenders before it. Offer budgets (price x maximum) enter C4, so unfilled offer cash is the expected leftover.
