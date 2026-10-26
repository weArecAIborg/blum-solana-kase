# bond_servicing: corporate actions for tokenized bonds on Solana

Built for the Superteam Kazakhstan x KASE Side Track, "Corporate Actions on Blockchain" (Colosseum Crypto World's
Fair).

**This branch (`demo-day-prep`) is work after the submission deadline**, for the Superteam Kazakhstan Demo Day on
27 Oct 2026. The judged state is `main` and the tag `submitted-2026-10-12`. The branch adds secondary trades settled
delivery versus payment through the Solana Foundation's DvP program, deployed on devnet under its own program ID
`tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i` (the submitted program `2UkTK...` is unchanged). The run of 23 Oct 2026:
[docs/devnet-evidence-demo.md](docs/devnet-evidence-demo.md). Anyone can repeat its ledger-only audit from public
devnet data with Python 3 and the audit's reference math (QuantLib, FinancePy); `scenarios/devnet-demo.public.json`
names the run's accounts by public key:

```bash
python3 -m pip install QuantLib financepy
python3 tools/audit/audit.py scenarios/devnet-demo.public.json https://api.devnet.solana.com history-accounts
```

**Operator screen:** [blum.caiborg.ai/operator-screen](https://blum.caiborg.ai/operator-screen) issues a bond with
your own terms on devnet (face, rate, coupons per year, term, holders, issue size). Every coupon and the redemption are
known from the terms: each runs by itself at its moment when its "auto" box is ticked, or step by step by its buttons.
The generated holders trade among themselves through Solana DvP as its documentation lays out
([solana.com/docs/defi/dvp-program](https://solana.com/docs/defi/dvp-program)): the venue records each trade and is its
settlement authority, the registrar as transfer agent admits the trade's escrow before it is funded, each party funds
its own leg and pays its own fees. Every record moment has trades one day before it, at it and one day after it;
besides those, each holder trades at random (at most T trades each, with a propensity to trade at the start and at the
end of the bond's life). Holders answer the buybacks and tenders the operator adds. The register shows
every holder's position at every record moment and every payment; after the redemption the ledger-only audit runs on
the page, and the page prints one command that runs the same audit of that bond on your machine. Service:
`ops/src/operator.ts`; the screen's wallet pays for every bond and is topped up from
[faucet.solana.com](https://faucet.solana.com).

A Solana program that services a tokenized bond through its whole life:

**identify eligible holders -> calculate entitlements -> execute -> record the outcome on chain**

for three corporate actions:
1. **coupon payment** to the holders at each record date;
2. **redemption at maturity**: principal and final coupon paid, bonds burned;
3. **issuer buyback and tender offer**: first-come buyback, and a pro-rata tender whose tendered bonds keep their
   coupon rights during the window.

It works for any fixed-rate bond: record day and time, payment time, time zone, weekend, holiday calendar and coupon
basis are bond parameters. The sample bond is the KASE listing's example (face 1,000, 10%, semi-annual, 2 years),
serviced by the Kazakh depository's rules (record list at 09:00 Almaty on the payment day).

**Deck and demo video:** [blum.caiborg.ai/deck.html](https://blum.caiborg.ai/deck.html) (17 slides in plain English,
one per requirement of the listing, each claim linked to its devnet transaction, line of code and site page) and
[blum.caiborg.ai/video.html](https://blum.caiborg.ai/video.html) (the demo video: the deck in 2:32, silent, each diagram
built in reading order; also on [YouTube](https://www.youtube.com/watch?v=ZaIaKe03jds)).

**Live demonstration:** [blum.caiborg.ai](https://blum.caiborg.ai) (what each party expects, what was kept and what
was refused, measured on devnet), [blum.caiborg.ai/stages.html](https://blum.caiborg.ai/stages.html) (nine stages of a
bond's life: the textbook rule beside the devnet measurement) and
[blum.caiborg.ai/timeline.html](https://blum.caiborg.ai/timeline.html) (one bond from issue to redemption, step by
step, every figure read from its transactions),
[blum.caiborg.ai/flow.html](https://blum.caiborg.ai/flow.html) (each instruction as Solana executed it: signers,
programs called, accounts read and written, checks, balances),
[blum.caiborg.ai/program.html](https://blum.caiborg.ai/program.html) (flowcharts of the program as its code runs,
each box linked to its line of source) and
[blum.caiborg.ai/overview.html](https://blum.caiborg.ai/overview.html) (this repository's technical overview).

## What is different

- **The market stays open on coupon day.** No snapshot and no trading freeze: every bond transfer goes through the
  program's Token-2022 transfer hook, which first saves both holders' positions for every record moment passed. Each
  coupon goes to whoever held the bond at the record moment. (The runs on the submitted program move bonds only; on
  this branch secondary trades settle delivery versus payment through Solana DvP, bonds and cash in one transaction.)
- **The program is the register and the paying agent.** It computes each payment from the terms and pays it from that
  event's own vault, in the same transaction that marks the holder paid. The issuer only deposits the cash.
- **Servicing does not depend on our operator.** Opening an event, confirming its funding, paying, redeeming and settling an
  offer can be sent by any wallet, at any later time, and the amounts cannot change.
- **Anyone can check it.** A ledger-only audit reads only the public ledger (no program accounts, no operator
  report) and checks every balance and payment against the bond's terms and the run's scenario.

## What the track asks for, and where it is measured

| The listing asks for | How bond_servicing does it | Measured on devnet, 1000-holder run ([evidence](docs/devnet-evidence-crowd-1000.md)) |
|---|---|---|
| Test tokenized instrument | Token-2022 bond, one token = one bond, with transfer hook, permanent delegate and permissioned burn; the listing's example terms | 10,251 bonds placed: face 1,000, 10%, semi-annual, 2 years |
| Holder registry | One holder record per wallet per bond; transfers only between registered holders | 1,000 registered holders |
| Record-date mechanism | Record moment from the terms and the holiday calendar; every transfer first saves both holders' positions for each record moment passed | 600 trades 4 to 452 s after record moments; trading never stopped before maturity |
| Entitlement calculation | Position at the record moment x face x rate / periods, integer, rounded down; checked against QuantLib and FinancePy | 4,000 of 4,000 holder-coupon amounts exact; BH#1: 10 x 1,000 x 10% / 2 = 500 |
| Coupon payment | One vault per coupon; any wallet pays, and the holder is marked paid in the same transaction | 4 coupons, 2,003,300.00 paid, every vault emptied |
| Bond redemption | Burn the holder's bonds and pay the principal in one transaction | 9,665 bonds from 918 holders; final supply 0 |
| Additional corporate action | Buyback (first come) and tender offer (pro rata; tendered bonds keep their coupon) | Buyback 176 bonds; tender 410 of 441 accepted, exactly its maximum |
| Verifiable on-chain record | Program state and events; a ledger-only audit of the public ledger against the terms and the scenario | 3,959 of 3,959 audit checks |

## Who can do what

| Role | Can | Cannot |
|---|---|---|
| Registrar | Create the bond, register holders, place bonds up to the issue size before the first record moment, open a buyback or tender offer; admit a DvP trade's escrow (transfer agent) | Move or burn holders' bonds; move cash out of a vault; change any amount owed |
| Venue (Solana DvP's settlement authority) | Record a trade and settle or cancel it (the DvP program; the 23 Oct run used the registrar's wallet in this role, the operator screen a venue of its own) | Redirect a trade's proceeds; move a holder's bonds outside a trade it funded |
| Issuer | Deposit cash into a coupon's, the redemption's or an offer's vault (no key in the program) | Take cash back: a vault pays only holders' own cash accounts |
| Holder | Transfer to registered holders until the final record moment; sell to a buyback; tender and withdraw during the window | Burn bonds; transfer tendered bonds; transfer from the final record moment |
| Anyone | Open an event, confirm its funding, pay holders, redeem, settle a tender | Change who is paid or how much: amounts follow from the positions recorded on chain |

The program's upgrade authority is a single wallet (see [Limits](#limits)).

## Market rules are bond parameters

| Market rule | Bond parameter | Sample bond: Kazakh depository rules |
|---|---|---|
| When the holder list is taken | Record day (business days before payment), record time, UTC offset | 09:00 Almaty (UTC+5) on the payment day; trades keep being registered (government securities rules, art. 8 p. 6) |
| Trading at maturity | Transfers close at the final record moment | Trade registration stops on the redemption day (art. 10 p. 6) |
| Non-working days | Weekend days and holiday calendar | Saturday and Sunday; [`data/calendars/kz-holidays.txt`](data/calendars/kz-holidays.txt); MUM108_0015's payment dates equal KASE's |
| Coupon amount | Basis: fixed per period, or 30/360 | Face x rate / 2 |
| Principal | Redemption price | At par |

Sources: [docs/bond-mechanics.md](docs/bond-mechanics.md).

## Live on devnet

Submitted program [`2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf`](https://explorer.solana.com/address/2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf?cluster=devnet):
a build of the tag `submitted-2026-10-12` gives it byte for byte (sha256 `327271f3...`). Demo Day program
[`tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i`](https://explorer.solana.com/address/tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i?cluster=devnet):
a build of this branch gives it byte for byte (sha256 `00733c95...`, upgraded 25 Oct 2026: a trade still open at the
final record moment returns its bonds to the seller, who is then redeemed). Every run has its own bond and cash token,
so each audit reads only that bond's transactions.

| Run | Holders | Transactions | Report check (QuantLib, FinancePy) | Ledger-only audit | Evidence |
|---|---|---|---|---|---|
| 8 holders: 8 trades at record moments, buyback, tender offer (10 tendered for 6: exactly 6 bought), 4 coupons, redemption | 8 | 50 | passed | 43 of 43 | [timeline](https://blum.caiborg.ai/timeline.html), `docs/demo/data/devnet-full8.json` |
| 1000 holders: 600 trades at record moments, buyback, tender offer (441 tendered for 410: exactly 410 bought), 4 coupons, redemption | 1000 | 2,318 | passed | 3,959 of 3,959 | [docs/devnet-evidence-crowd-1000.md](docs/devnet-evidence-crowd-1000.md) |
| Operator stops after coupon 0; a wallet with no role finishes the bond | 3 | 25 | passed | 23 of 23 | [docs/devnet-evidence-keeper.md](docs/devnet-evidence-keeper.md) |
| Demo Day program, before the 25 Oct upgrade: quarterly, 1 year, two trades settled by Solana DvP, operator stops after coupon 0 | 3 | 32 | passed | 23 of 23 | [docs/devnet-evidence-demo.md](docs/devnet-evidence-demo.md) |

Attacker run (`scenarios/devnet-hostile.json`, 6 holders): at every step of the bond's life an attacker, and holders
acting against the rules, try 35 ways to take bonds or cash or to act without authority. All 35 are refused, each a
failed transaction anyone can open; the audit of that run passes 35 of 35 checks. The list, with what each would
cost, the rule that stops it, 21 more attempts refused in local tests, and what the system trusts rather than stops:
[docs/hostile-attempts.md](docs/hostile-attempts.md).

Measured on the 1000-holder run:
- 4,000 of 4,000 holder-coupon amounts equal the record-moment holdings; paying whoever held the bond at payment time
  would have got 1,039 wrong;
- the listing's example: BH#1 held 10 bonds at the first record date and was paid 500.00;
- paying one holder one coupon costs 833-2,500 lamports and creates no storage.

**Demonstration pages:** `docs/demo/`, the site above, built only from measured devnet data by
`tools/demo/build_app.py`, `build_stages.py`, `build_timeline.py`, `build_flow.py` and `build_program.py`; the overview page by
`build_overview.py` from `docs/technical-overview.md`.

## Implemented vs simulated

| Implemented on Solana | Simulated |
|---|---|
| Bond token (Token-2022 with transfer hook, permanent delegate, permissioned burn), holder registry, positions at record moments, entitlements, event vaults, coupon payment, redemption with burn, buyback, tender offer, on-chain record | Cash rail: a test SPL token (6 decimals) minted by a test "bank" key, standing for fiat or a stablecoin; the issuer's funding is that bank minting into the vaults |
| Permissionless servicing and the ledger-only audit; secondary trades settled delivery versus payment through the Solana Foundation's DvP program | Investor onboarding and KYC (the registrar registers wallets); holders are generated wallets labelled BH#1, BH#2, ...; no order book: trades are generated, their cash leg is the test token |
| | Bond time is compressed on devnet (1 bond day = 1 to 20 seconds); no integration with KASE or the central securities depository |

## Documentation

| Document | Content |
|---|---|
| [docs/technical-overview.md](docs/technical-overview.md) | Architecture, record-date logic, entitlement calculations, settlement flow, Solana components, limits |
| [docs/bond-processing.md](docs/bond-processing.md) | Every instruction, every guarantee and the test that proves it |
| [docs/buyback-tender.md](docs/buyback-tender.md), [docs/corporate-actions.md](docs/corporate-actions.md) | The third corporate action, and why it was chosen |
| [expertise/independent-ledger-audit.md](expertise/independent-ledger-audit.md) | How the audit works |
| [docs/hostile-attempts.md](docs/hostile-attempts.md) | Every hostile attempt: what it would cost, the rule that refuses it, its transaction or test; what is trusted, not stopped |
| [docs/bond-mechanics.md](docs/bond-mechanics.md), [docs/bond-params.md](docs/bond-params.md), [docs/tradfi-vs-onchain.md](docs/tradfi-vs-onchain.md) | How bonds are serviced today (Kazakh depository, SCoRE, AFME); the sample bonds' terms, with sources |

## Repository

| Path | Content |
|---|---|
| `programs/bond_servicing` | The Anchor program; LiteSVM tests in `tests/`: `lifecycle.rs` (each rule), `random_operations.rs` (generated: random terms and random sequences of every operation, invariants measured from balances and the clock; `PROPTEST_CASES` sets the number of cases) |
| `crates/bond_math` | Schedules from a holiday calendar, day counts, amounts; tested against QuantLib and FinancePy |
| `ops/src/scenario.ts` | Operator: runs a scenario's whole lifecycle and writes a report |
| `ops/src/dvp.ts` | Solana DvP client: create, fund both legs, settle, cancel; `admit_trade` |
| `ops/src/operator.ts`, `docs/demo/operator-screen.html`, `ops/deploy/` | The operator screen: its service (bonds with the visitor's terms, corporate actions by time or by step, generated holders' trades and offer answers, audit) and its page |
| `scenarios/` | Bond terms and events per run (`b1`, `mum108` local; `devnet-*` devnet) |
| `tools/reference/check_report.py` | Checks a report's amounts with QuantLib and FinancePy |
| `tools/audit/audit.py` | Ledger-only audit from public RPC data |
| `tools/audit/audit_json.py` | The same audit as JSON, per holder, for the operator screen |
| `tools/demo/`, `docs/demo/` | The demonstration site: its builders, and the pages they write from measured devnet data (`docs/demo/data/`) |
| `tools/scenario/make_crowd.py` | Generates many-holder scenarios from a seed |
| `data/calendars/kz-holidays.txt` | Sample holiday calendar |

## Run it

**Prerequisites:** Rust (the version in `rust-toolchain.toml`), Solana CLI 3.1.10 with a default wallet
(`solana-keygen new`), Anchor 1.1.2, [Surfpool](https://github.com/txtx/surfpool), Node 22, `jq`, and Python 3.12
with [uv](https://github.com/astral-sh/uv).

```bash
uv venv .venv && uv pip install --python .venv QuantLib==1.43 financepy==1.1.2 holidays==0.106 markdown==3.11
(cd ops && npm ci)

# A fresh clone has its own program keypair: point the code at it, then build again.
anchor build && anchor keys sync && anchor build

# Program tests (LiteSVM); the generated tests run 256 cases, about 5 minutes
cargo test -p bond_servicing
cargo test -p bond_math

# Keypairs a scenario uses are not in the repository (keys/ is ignored); create them
for f in $(jq -r '.keys[], .wallets[]' scenarios/b1.json); do
  [ -f "$f" ] || { mkdir -p "$(dirname "$f")"; solana-keygen new --no-bip39-passphrase --silent -o "$f"; }
done

# Whole lifecycle on a local Surfpool network, then the report check and the ledger-only audit
scripts/run-local.sh b1
```

**Devnet:** `./deploy.sh https://api.devnet.solana.com` deploys the program; `scripts/run-devnet.sh <name>` runs
`scenarios/devnet-<name>.json` and both checks. Each run needs a new bond mint keypair (and a new cash mint keypair
for an audit of that bond alone), and the wallet pays the holders' account deposits (about 0.0059 SOL per holder in
the 1000-holder run, each holder's cash account included).

## Limits

Fixed-rate bonds only; `FixedPerPeriod` and 30/360 day counts; regular schedules; at most 40 coupon periods; direct
holders only. The registrar is trusted to register holders and to place bonds, up to the issue size, before the first
record moment. The cash token's issuer can freeze or pause its token, which stops payments until it acts again
(cash tokens with a permanent delegate, which could take a vault's cash, are refused). The program's upgrade authority
is a single wallet.
Details: [docs/technical-overview.md](docs/technical-overview.md), section 8.
