# Hostile attempts: what the system refuses, and what it trusts

Every attempt below tries to take bonds or cash from someone, to act without authority, or to break the record of who
is owed what. Each one is listed with what it would cost if it worked and the rule that stops it, so that all of them
can be shown on the public pages: a refused attempt is a failed transaction anyone can open.

Three groups:
1. **On devnet** (35): refused on the public chain; each row links to its failed transaction.
2. **Local tests, not yet on devnet** (21 refused, 3 with no effect, 2 timing checks): run on LiteSVM against the
   compiled program (`programs/bond_servicing/tests/lifecycle.rs`). To show them
   on the pages they still have to be added to the devnet attacker run.
3. **Trusted, not stopped**: what the parties the system relies on can still do.

## 1. On devnet: 35 of 35 refused

Attacker run `scenarios/devnet-hostile.json` (B1 terms, 6 holders; bond mint `AEKVnujjMtA6hdG2WjLv6EWmbs8LevaeUdyDNoFaGWFb`; run 2026-10-11;
its own cash token `7CWX7PdNzDemiDmBjsUKQ15J5q2pz3JQmgPrpfJuYs1p`),
`ops/src/attacks.ts`. Every attempt is sent without simulation, so each refusal is a failed transaction on the chain.
Data: `docs/demo/data/devnet-hostile-attempts.json`; grouped by whose loss it is about, as on the app's party tabs
(`tools/demo/build_app.py`, `PARTY_ATTEMPTS`).

What they show, and what they do not:
- An attempt counts as refused only if its transaction failed and the logged reason contains the expected guard. This
  is a substring match: `claim-as-owner` expects `Constraint`; its logged reason is `ConstraintSeeds`.
- "Naming the program" means the attacker lists the program's account as a signer without its signature. It tests the
  response to missing authorization; nothing is forged.
- Some refusals come from Solana's token programs (wrong owner, missing signature, no freeze authority) rather than
  from the bond's own rules. The bond-specific ones are the registry, the one-account rule, the forged position, the
  short funding, the redirected payments, the double payments, the late buyer, the offer limits, the locked tender,
  the wrong vault and the wrong event kind.
- Several use a rightful party's real signature: the holder in `send-unregistered`, `second-account`, `skip-hook`,
  `sell-tendered` and `trade-at-maturity`; the registrar in `fee-cash-token`, `delegate-cash-token`, `place-late` and
  `place-beyond-issue`; the bank's real freeze
  in `frozen-cash`, followed by a thaw and the payment.
- The run also has 11 transactions the attacker paid for that succeeded: opening its own token accounts, then serving
  the bond as an outside keeper (opening events, confirming funding, paying coupons 2 and 3, redeeming all six
  holders), which anyone may do. Cash went only to holders; the attacker received 0 cash and 0 bonds.
- A failed transaction changes no state (fees aside), so unchanged balances are expected; the evidence is the reason
  it failed. The list is not exhaustive: other compositions of instructions, adversarial timing and a malicious upgrade
  by the real upgrade authority are not covered.

### Holder (11)

| Attempt | When | Actor | Tries | Would cost | Refused by | Transaction |
|---|---|---|---|---|---|---|
| `take-bonds` | before the first record moment | attacker | moves BH#1's bonds to itself | BH#1 loses its bonds | `owner does not match` | [2fKzqobL...](https://explorer.solana.com/tx/2fKzqobL1uhvEFzJrq6K9G3Ts3LtEv4DWeUVQjtdP5EhXt56geWR4GUWD6tAyFEWrv19PK3atYJ4giGmNBxfJs81?cluster=devnet) |
| `take-as-delegate` | before the first record moment | attacker | moves BH#1's bonds naming the program, the permanent delegate | BH#1 loses its bonds | `MissingRequiredSignature` | [2V4Af4SV...](https://explorer.solana.com/tx/2V4Af4SVcjvPK9xCFymsjv9Heu4stz67pEDw8XhJLYSSBAMfgkvjgjjYPo9eoXpMmr39gVRWCkCqqsz8iMPzSvDV?cluster=devnet) |
| `burn-bonds` | before the first record moment | attacker | burns BH#1's bonds | BH#1 loses coupons and principal | `Invalid instruction` | [5i5hd1y9...](https://explorer.solana.com/tx/5i5hd1y9jwSNJbNeVCU1nbe4cj83Y43ynCQbB7aiMpoVgpieQdQnjhgVfyn9eaZAkXoiwWnWqteHTpiBsZ9RQdJ7?cluster=devnet) |
| `burn-permissioned` | before the first record moment | attacker | burns BH#1's bonds with the permissioned burn, naming itself burn authority | BH#1 loses coupons and principal | `InvalidAccountData` | [3JUYUn1n...](https://explorer.solana.com/tx/3JUYUn1nZkWWfDY2GqrMTWX4Q5XmDpBDAtPZQYuSDpxKsL8vsq3edvNnYvqHsvQWE8uiMNbKA7NjrBMsUFiUSLPd?cluster=devnet) |
| `burn-as-program` | before the first record moment | attacker | burns BH#1's bonds with the permissioned burn, naming the program, the burn authority | BH#1 loses coupons and principal | `MissingRequiredSignature` | [4U2bieNT...](https://explorer.solana.com/tx/4U2bieNTNDWDsmz6WSPRV3UVXFjAomquUhb2SChbbiysTpqqZkprvtB2bNyvmYXcrTWfqTZUg7y1A6qAsJT8ErUB?cluster=devnet) |
| `freeze-holder` | before the first record moment | attacker | freezes BH#1's bond account | BH#1 cannot sell | `cannot freeze` | [SiWEENmK...](https://explorer.solana.com/tx/SiWEENmKknQUggVoaHMWhqVnXVDsxqe9Bv1iH2mxewv85P7onm4xiBt6R8deKbXdRyRCRFare3GqTRxuhn2acHd?cluster=devnet) |
| `redirect-coupon` | coupon 0, payment open | attacker | pays BH#1's coupon into its own cash account | BH#1's coupon | `ConstraintTokenOwner` | [5Six9uRX...](https://explorer.solana.com/tx/5Six9uRXdZkBfriTMfwVweNtWwy7SqM6DUH1oCyVn6kp3B8tUh2WqNY21g9qmLPsjSHRHoxNDK7ZTgk6WbdKvEUj?cluster=devnet) |
| `claim-as-owner` | coupon 0, payment open | attacker | presents BH#1's holder record as its own | BH#1's coupon | `Constraint` | [adM2PAkx...](https://explorer.solana.com/tx/adM2PAkx5KGtpxMajhKyJtheCQBp1CjeSkT4d5dJugMGWhSXDe8o4EQpyomvL9kSkHYEm2VyCnLVewuwzvQC4ma?cluster=devnet) |
| `redirect-principal` | redemption, payment open | attacker | redeems BH#1's bonds into its own cash account | BH#1's principal | `ConstraintTokenOwner` | [aBbj6FtF...](https://explorer.solana.com/tx/aBbj6FtFiuaVBkP7v33Uc9em8U8eMFM8fYT8asAPfrrpS9n8nEtaC3KaTzpfZbDCD8DBGb31xpWir9nJZo8YjMA?cluster=devnet) |
| `sell-others-bonds` | buyback offer 0 open | attacker | sells BH#1's bonds to the buyback | BH#1's bonds sold without its consent | `ConstraintRaw` | [4LyRjuw6...](https://explorer.solana.com/tx/4LyRjuw6JSxAeDAxGUqRpNFpXA5snch6XnXVeMpm8rpKemcFZ5KbU7JsZMJvkHKpK2b3mZKqKsEYNqvQuFqXJYoz?cluster=devnet) |
| `delegate-cash-token` | before the first record moment | registrar | creates a bond paid in a cash token whose permanent delegate is the attacker | the delegate empties the vaults before holders are paid | `UnsupportedCashToken` | [gSxKKA9N...](https://explorer.solana.com/tx/gSxKKA9NsgTq9TBRuCVH6TYMCoPJ7cwkp72kjSZw8tCgs8jhtFdofeSaS55riBEpy8kN7DssZAi8TrGsmKoySjU?cluster=devnet) |

### Issuer (13)

| Attempt | When | Actor | Tries | Would cost | Refused by | Transaction |
|---|---|---|---|---|---|---|
| `mint-bonds` | before the first record moment | attacker | mints 1,000 bonds to itself | the issuer owes coupons and principal on bonds it never sold | `owner does not match` | [4kHz8Q3F...](https://explorer.solana.com/tx/4kHz8Q3F5iBS3TPeeCVJmHNfWyTdr6814sFpCTXvswDL7nytcjrTgkSmGvWzDAkKhXLppgHrDiqLnJW2GKi1AUnA?cluster=devnet) |
| `mint-as-program` | before the first record moment | attacker | mints 1,000 bonds naming the program as mint authority | the issuer owes coupons and principal on bonds it never sold | `MissingRequiredSignature` | [2wUBApJT...](https://explorer.solana.com/tx/2wUBApJTdyXx1Se1qppSF87iyY5cPBo5tPKKNVKT9h3iKeU4Z3ogZxAG7oBSny1n8771bTKD8RoXQrogVMeDnEwA?cluster=devnet) |
| `place-late` | after the first record moment | registrar | places 100 new bonds with BH#2 | bonds issued after the issue closed | `PlacementClosed` | [5QXqo4ar...](https://explorer.solana.com/tx/5QXqo4ar7Jq1TEt7yDjbEqpbPWdBT9ucfpXfHFcJ1dGQF8Lv6VS4cC9ypKQjwU12StduVHTKPVwrpGw2uFqn8gmf?cluster=devnet) |
| `place-beyond-issue` | before the first record moment | registrar | places 1 more bond with BH#2, past the issue size of 27 | bonds beyond the issue the terms announced | `ExceedsIssueSize` | [3ZpPPxy6...](https://explorer.solana.com/tx/3ZpPPxy6t3zSX6haWwXrs1vMg9Vs6zRVVkTFUEnM3Gt5XRFukahnyCq2MpZhDuGjQtvkW6EbhiveJMu9d2Mc2wL3?cluster=devnet) |
| `pay-twice` | coupon 0, paid | attacker | pays BH#1's coupon again | the issuer pays a coupon twice; the last holders find the vault empty | `AlreadyPaid` | [5xay6pMk...](https://explorer.solana.com/tx/5xay6pMkpuvJ9AuWcUyJQW36MWNPzsM1czQRLryZAbMbLpsfbzELe6UwTjFPJjy5j6WoEWVeB2amZr9TicdQihNE?cluster=devnet) |
| `late-buyer` | coupon 0, paid | BH#3 | bought after the record moment and asks for the coupon | the seller's coupon paid again to the buyer | `NothingToPay` | [2Rf9ET9C...](https://explorer.solana.com/tx/2Rf9ET9CJZr8ShaNDVehrrJ13ZH83YNdKfpS9qDmTsJStcpGVw3Nu9hyTVyFQa5xBDuKg2TxrBFPMkVahFbEfrtM?cluster=devnet) |
| `redeem-twice` | redemption, paid | attacker | redeems BH#1 again | principal paid twice | `NothingToPay` | [2UNSXXKd...](https://explorer.solana.com/tx/2UNSXXKdVfFnVM7kCqEFX8JEb7NbJHzqsrPugiHtC5G66V6tmFoSrA2S9ZqcHjEVkhdQzJnGyoa2PJnNXcnkRoFP?cluster=devnet) |
| `other-vault` | coupon 1, payment open | attacker | pays BH#1's coupon 1 from coupon 0's vault | one coupon's cash spent on another | `ConstraintHasOne` | [5WSuAifw...](https://explorer.solana.com/tx/5WSuAifw8rTCXpTqY849Zea79Yts7aqHsy7864YB9B5MSEtfx4QKuncV9JtBtHhkX1Dw14GemWcCjQ4BUPg2jnCs?cluster=devnet) |
| `coupon-from-principal` | redemption, payment open | attacker | pays BH#1 a coupon from the redemption vault | principal cash paid out as coupons | `WrongEventKind` | [3tFEC6k3...](https://explorer.solana.com/tx/3tFEC6k3jjgw2JfBt3c3Srut439xGUdRxw3JmgDtrNGpCyHvt3P4qZPSDp4RCEyPd1uKcXEDeXQ4mABmsPpBYsjj?cluster=devnet) |
| `over-maximum` | buyback offer 0 open | BH#1 | sells 1 bond to the buyback after its maximum is reached | the issuer buys more bonds than it offered | `ExceedsOffer` | [2bp1FWTT...](https://explorer.solana.com/tx/2bp1FWTThQzDh1zw6VkLHsHRPKEi6rj7BsjJ4XyFxotk1S3VUuSXdgsRuzB9XzUtAPB8omugG5Rdi9FW1mN64t9W?cluster=devnet) |
| `sell-tendered` | tender offer 1 open | BH#1 | transfers all its bonds to BH#2, including those tendered to the issuer | the same bonds sold twice: to the issuer and to a buyer | `BondsLocked` | [345NJWPm...](https://explorer.solana.com/tx/345NJWPmJRZ343jvWJmzT7PsoRNBc8khREiBLGGdsEWNdbYM3a763Vg35nJkfumjgsbPYPnZHFmmNYEAGPAYMCN7?cluster=devnet) |
| `drain-vault` | coupon 0, vault funded short by 0.000001 | attacker | moves the vault's cash to itself | the coupon's cash is gone | `owner does not match` | [sijk72Rf...](https://explorer.solana.com/tx/sijk72Rf1difQikgZvU8FWBSh6teoy8BTmMeqqxMRGNJTAq7b4EfpULVLXQyHbXMHDHp2CZz2jepBAL7VxaYWfH?cluster=devnet) |
| `drain-as-program` | coupon 0, vault funded short by 0.000001 | attacker | moves the vault's cash to itself naming the program, the vault's owner | the coupon's cash is gone | `MissingRequiredSignature` | [3TCGLtmK...](https://explorer.solana.com/tx/3TCGLtmKGXY29yNB4X4WJpFc3fh51rQhuC9kCBE7BkNvaoVqcjUMfESUhR8Fb9txp6KNx7vXFQvubRDmziLbYPV5?cluster=devnet) |

### Depository (registrar, paying agent) (8)

| Attempt | When | Actor | Tries | Would cost | Refused by | Transaction |
|---|---|---|---|---|---|---|
| `register-self` | before the first record moment | attacker | registers itself as a holder, signing as registrar | an unvetted wallet in the register | `ConstraintHasOne` | [5WDjripi...](https://explorer.solana.com/tx/5WDjripiJDyYPXyYbcenpS6ZTnbrtCouvVRuYM94daEXeHfXd8Q9TCguMXjLtYttmVLVt72sJcGAAyanaP2hTqr3?cluster=devnet) |
| `send-unregistered` | before the first record moment | BH#1 | sends a bond to an unregistered wallet (the attacker) | bonds held outside the register | `AccountOwnedByWrongProgram` | [2RZZ181c...](https://explorer.solana.com/tx/2RZZ181cPxuSw4sQWRqG57jwubi2uRCHdg1StpSK84akNcVX3zvUmX8ogHATMy9JzZVp3WDhkcpKkHx33hz85TfQ?cluster=devnet) |
| `second-account` | before the first record moment | BH#1 | moves a bond to a second account of its own | a position the register does not see | `NotAssociatedTokenAccount` | [4cfaxez9...](https://explorer.solana.com/tx/4cfaxez9p8M6c37BA7ZKa5cXEWer318pnEwuVZKZSqNzw1nwofZG7Ch9MK9VvgYX5By4p9ZSv343YP4WoKywiFN4?cluster=devnet) |
| `skip-hook` | before the first record moment | BH#1 | sends a bond to BH#2 with Token-2022's plain Transfer instruction, which skips the transfer hook | a bond moves without its record-moment position being saved | `custom program error: 0x1f` | [2AcN3WN7...](https://explorer.solana.com/tx/2AcN3WN7FQu2Z9zUnDP5YPaWpSFKEysazfJMe8r1rg7U69JLyvsQgWMpApoz3pXtvoUp68zfmsGFohxhVKkSiP83?cluster=devnet) |
| `forge-position` | before the first record moment | attacker | calls the transfer hook directly to add 5 bonds to BH#2's record | coupons paid on bonds nobody holds | `NotTransferring` | [2iFCWWPD...](https://explorer.solana.com/tx/2iFCWWPDu6qe7NTMWVGM5eGLHkxypsUeKfAty2yDtF6j2egwbMQykmFH79mj6eWQKJtRc8anjX38rNoRbUm2YU1U?cluster=devnet) |
| `confirm-short` | coupon 0, vault funded short by 0.000001 | attacker | confirms the coupon funded | the last holders find the vault empty | `InsufficientFunding` | [4ZRUD95e...](https://explorer.solana.com/tx/4ZRUD95evjq9drgjhq1RnAULLkN4DZZrAQei63ih34ZJf8PTVHCCHHKxVXJ8RUdoEs9LFABmzrmGBuzktRGJBMD4?cluster=devnet) |
| `pay-unfunded` | coupon 0, vault funded short by 0.000001 | attacker | pays BH#1 from the unconfirmed vault | the last holders find the vault empty | `NotFunded` | [2EsUoxPu...](https://explorer.solana.com/tx/2EsUoxPudM5cSHsfYiZsjYRrxGZqsMVjCEpoYCL2qSbsXedPghAzRdpg77N49NPm5CbKzhYWNjAzpcHivYpwdZuc?cluster=devnet) |
| `take-program` | before the first record moment | attacker | makes itself the program's upgrade authority | a replaced program could empty every vault | `Incorrect authority` | [VCLVP9r8...](https://explorer.solana.com/tx/VCLVP9r8beMLCpJKA5JBLnwoC9ww3vWGW7bAJpZ5SeSWZDKYis6naYnjSZfJrTRYbwQFnnEQZNYvtdbdBe1uWQ4?cluster=devnet) |

### Bank (cash token) (2)

| Attempt | When | Actor | Tries | Would cost | Refused by | Transaction |
|---|---|---|---|---|---|---|
| `fee-cash-token` | before the first record moment | registrar | creates a bond paid in a cash token that keeps a 1% fee on every transfer | holders receive 1% less than the terms | `UnsupportedCashToken` | [63Ma2XhY...](https://explorer.solana.com/tx/63Ma2XhYghAgrvLiHVwEMm6qRz2F8CsLK1nPhBgSBqJoJjTEPuSVNSbGzfbGFcJBwR9W5coNdZ6V1YhSKsVZ5MtG?cluster=devnet) |
| `frozen-cash` | coupon 1, paid | anyone | pays BH#5, whose cash account the bank froze | nothing: the others are already paid, and the right stays open | `Account is frozen` | [tddRDsBo...](https://explorer.solana.com/tx/tddRDsBodPWfY22XrwPuvacUocqLYJXe1iKa9XnXZVzEBpxQpd13TQarrXMVSFt2pCFNUb5NFfaBPr9hU5v8UW7?cluster=devnet) |

### Exchange (1)

| Attempt | When | Actor | Tries | Would cost | Refused by | Transaction |
|---|---|---|---|---|---|---|
| `trade-at-maturity` | after the final record moment | BH#2 | sells 1 bond to BH#1 | a bond changes hands while its principal is being paid | `TransfersClosedAtMaturity` | [6kT7VRwv...](https://explorer.solana.com/tx/6kT7VRwvjVPm7jNGrzPG7p4Ft8LgS8hRfhEokYF488KmY6uUvXPY7JYKiXHzLdAccfa16Hi5CxkS4n7S9xRseGz?cluster=devnet) |

## 2. Local tests, not yet on devnet

Run: `cargo test -p bond_servicing --test lifecycle` (all pass). Each refused attempt is checked for the
reason given below, not only for failing.

### Taking control of the bond token or a vault (11 refused)

Test `nobody_takes_over_the_bond_mint_or_a_vault`. "Naming the program" means the attacker lists the program's account
(the Bond PDA, which holds every authority) as signer without its signature; only the program can sign for it.

| Attempt | Party | Actor | Tries | Would cost | Rule that stops it | Refused with |
|---|---|---|---|---|---|---|
| `repoint-hook` | Depository | attacker | points the bond token's transfer hook to a program of its own | transfers skip the holder records; coupons no longer follow the record moment | only the bond's authority (the program) can change the hook | `owner does not match` |
| `repoint-hook-as-program` | Depository | attacker | the same, naming the program | the same | the program never signs this | `MissingRequiredSignature` |
| `take-permanent-delegate` | Holder | attacker | makes itself the bond token's permanent delegate | it could move any holder's bonds | only the program holds the delegate | `owner does not match` |
| `take-permanent-delegate-as-program` | Holder | attacker | the same, naming the program | the same | the program never signs this | `MissingRequiredSignature` |
| `take-mint-authority` | Issuer | attacker | makes itself the bond token's mint authority | it could mint bonds the issuer owes coupons on | only the program mints | `owner does not match` |
| `take-hook-authority` | Depository | attacker | makes itself the authority over the transfer hook | it could re-point the hook later | only the program holds it | `owner does not match` |
| `close-vault` | Issuer | attacker | closes a coupon's empty vault before funding | the coupon cannot be funded or paid | only the program owns a vault | `owner does not match` |
| `close-vault-as-program` | Issuer | attacker | the same, naming the program | the same | the program never signs this | `MissingRequiredSignature` |
| `approve-on-vault` | Issuer | attacker | approves itself to spend a vault's cash | it could take the coupon's cash | only the program owns a vault | `owner does not match` |
| `approve-on-vault-as-program` | Issuer | attacker | the same, naming the program | the same | the program never signs this | `MissingRequiredSignature` |
| `take-vault` | Issuer | attacker | makes itself a vault's owner | it could take the coupon's cash | only the program owns a vault | `owner does not match` |

After all 11, the vault balance is unchanged, the hook still records transfers and the coupon pays.

### A second bond on the same cash token (4 refused)

Test `a_second_bond_on_the_same_cash_token_cannot_reach_the_first_bonds_vaults`. Anyone may create a bond; the attacker
creates its own on the same cash token, places 1,000 bonds with itself, opens its own coupon, and mixes its accounts with
the first bond's.

| Attempt | Party | Actor | Tries | Would cost | Rule that stops it | Refused with |
|---|---|---|---|---|---|---|
| `second-bond-holder` | Issuer | attacker | presents its holder record from its own bond on the first bond's coupon | the first bond's coupon cash paid to the attacker | a holder record belongs to one bond | `ConstraintSeeds` |
| `second-bond-vault` | Issuer | attacker | pays its own bond's coupon from the first bond's vault | the same | an event pays only from its own vault | `ConstraintHasOne` |
| `second-bond-event` | Issuer | attacker | pays the first bond's coupon with its own bond's event | the same | an event belongs to one bond | `ConstraintSeeds` |
| `second-bond-confirm` | Issuer | attacker | confirms its own coupon funded with the first bond's vault | its coupon marked funded with another bond's cash | an event is funded only from its own vault | `ConstraintHasOne` |

### Funding and double commitment (6 refused)

Tests `funding_is_confirmed_only_from_the_events_own_vault`, `the_same_bonds_cannot_be_committed_twice`.

| Attempt | Party | Actor | Tries | Would cost | Rule that stops it | Refused with |
|---|---|---|---|---|---|---|
| `confirm-elsewhere` | Depository | keeper | confirms a coupon funded with another account that holds the whole budget | holders paid from a vault that cannot pay them all | an event is funded only from its own vault | `ConstraintHasOne` |
| `tender-then-sell` | Issuer | BH#1 | tenders all its bonds, then sells one to a buyback | the same bonds sold twice: to the tender and to the buyback | tendered bonds are locked | `InsufficientUnlocked` |
| `tender-twice` | Issuer | BH#1 | tenders all its bonds, then tenders one into a second offer | the same bonds bought by two offers | tendered bonds are locked | `InsufficientUnlocked` |
| `tender-then-transfer` | Issuer | BH#1 | tenders all its bonds, then transfers one | a bond sold to a buyer and to the issuer | tendered bonds are locked | `BondsLocked` |
| `withdraw-more` | Issuer | BH#1 | withdraws 11 bonds from a tender of 10 | unlocks bonds it never tendered; the offer's total goes wrong | a withdrawal is at most what was tendered | `ExceedsOffer` |
| `settle-twice` | Issuer | keeper | settles the same tender again | the issuer pays twice for the same bonds | a tender is settled once | `AlreadySettled` |

After them, BH#1 is paid once, by the one offer its bonds were tendered into.

### Blocking its own retirement (3 with no effect)

Test `cpi_guard_on_a_bond_account_does_not_block_retirement`. A holder turns on Token-2022's CPI Guard on its bond
account, which stops programs from using the owner's authority over it.

| Attempt | Party | Actor | Tries | Would cost | Why it has no effect |
|---|---|---|---|---|---|
| `guard-blocks-buyback` | Issuer | BH#1 | blocks the burn when it sells to a buyback | a sale paid without the bonds retired | the program burns with its own authority (burn authority and permanent delegate), which the guard does not restrict |
| `guard-blocks-settlement` | Issuer | BH#1 | blocks settlement of its accepted tender | the issuer's offer cash and the holder's bonds stuck; principal funded twice if settled after the final record moment | the same |
| `guard-blocks-redemption` | Issuer | BH#1 | blocks its redemption | the bond cannot be finished | the same |

### Timing checks (2)

| Check | Test | Result |
|---|---|---|
| A transfer in the record second (09:00:00) | `a_transfer_in_the_record_second_counts_as_after_the_record` | One second before, the coupon moves with the bonds; in that second, it counts as after the record: the seller keeps the coupon on the bonds it sold |
| Transfers of zero bonds and to oneself, before and after a record moment | `zero_and_self_transfers_change_no_entitlement` | No position and no payment changes |

## 3. Trusted, not stopped

What the parties the system relies on can still do. The tests record what happens today.

| Party | Can | What happens | Test or source |
|---|---|---|---|
| Bank with a freeze authority (as USDC has) | freeze a coupon's vault, even after it was confirmed funded | nobody is paid from it until the bank thaws it; then everyone is, nothing owed is lost | `a_frozen_vault_pays_nobody_until_thawed` |
| Bank with a pause switch on the cash token | pause the cash token | payments stop while it is paused | `cash_issuer_pause` |
| Bank | freeze one holder's cash account | that holder waits; the others are paid (devnet `frozen-cash`) | `unpayable_holder_does_not_block_others_or_next_event` |
| Registrar | register any wallet (KYC is simulated), place bonds up to the issue size until the first record moment, open offers the issuer then funds | supply is fixed from the first record moment on; the registrar never moves bonds or cash | `placement_closes_at_first_record_moment`, `placement_beyond_the_issue_size_is_refused`, devnet `place-late`, `place-beyond-issue` |
| Upgrade authority: one wallet, [`81tD4k2C...`](https://explorer.solana.com/address/81tD4k2CsL8Dt9P95WcTiVuaRvN9yTu61HYCcG8gk6qK?cluster=devnet), the wallet that sent the devnet runs | replace the program | everything above depends on the program not being replaced | README, Limits; [program data](https://explorer.solana.com/address/6bZTEKyTPdf5zCG2RpjNAqbe4c6ymBmeno6JCAuWFxuA?cluster=devnet); devnet `take-program` |
| Nobody (no instruction) | return cash left in a vault to the issuer | rounding leftovers, overfunding, and the redemption principal for tender bonds settled after the final record moment stay in their vaults | `redemption_rounding_leftover_stays_in_its_vault`, `redemption_pays_free_bonds_while_an_offer_is_pending` |

The program refuses cash tokens with a transfer fee, a transfer hook, non-transferability or a
permanent delegate (`create_bond.rs`, `check_cash_mint`; devnet `fee-cash-token`, `delegate-cash-token`). It accepts a freeze
authority and a pause switch: they can stop payments, not take the cash.

## 4. Putting them on the public pages

1. Add each attempt of section 2 to the attacker run (`ops/src/attacks.ts`): an id, the actor, what it tries, what it
   would cost, and the guard it must be refused by; the run fails if any attempt is refused by anything else.
2. The second-bond attempts need the attacker to create its own bond on devnet first (rent paid by the attacker).
3. The CPI Guard attempts succeed as transactions: the pages need a "no effect" state next to "refused".
4. The bank's powers (freeze, pause) need a cash token with a pause switch: a separate devnet scenario; on the pages
   they belong under "trusted, not stopped", not under "refused".
5. Rebuild `docs/demo/data/devnet-hostile-attempts.json`, then add each new id to `PARTY_ATTEMPTS` and its rule to the
   guard texts in `tools/demo/build_app.py`.
