"""Build the program logic page (docs/demo/program.html): flowcharts of bond_servicing as its code runs.

One chart per instruction and per function the instructions share, top to bottom in the order the code runs: what
Anchor checks on the accounts before the handler starts, then each check of the handler with the error it refuses
with, each calculation, each call into another program, each write and each event. Every box carries the code it
stands for, read from the source, and the line it is on.

The page is not written if
  - a box's code is not found in its file after the box before it (so the boxes follow the code's order);
  - a program error raised inside a charted function is not shown as the exit of the box it belongs to;
  - an account constraint (has_one, seeds, constraint, init, token or associated-token) of a charted accounts struct
    has no box;
  - one of the program's errors (error.rs) appears in no chart;
  - a charted source file has changes not committed.
Source pages with line anchors are written next to the page (docs/demo/code/).

Usage: .venv/bin/python3 tools/demo/build_program.py docs/demo/program.html
"""

import hashlib
import os
import re
import subprocess
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/demo"))
from build_timeline import esc, fill  # noqa: E402

SRC = "programs/bond_servicing/src/"
LIB, STATE, SETTLE = SRC + "lib.rs", SRC + "state.rs", SRC + "settlement.rs"
INS = SRC + "instructions/"
AMOUNT = "crates/bond_math/src/amount.rs"
TIME = "crates/bond_math/src/time.rs"

# Errors Anchor raises for its account checks (anchor-lang 1.1.2); the System program's for an existing account.
ANCHOR_ERRORS = {"AccountNotSigner", "AccountOwnedByWrongProgram", "AccountDiscriminatorMismatch", "InvalidProgramId",
                 "ConstraintHasOne", "ConstraintSeeds", "ConstraintRaw", "ConstraintAssociated", "ConstraintTokenOwner",
                 "ConstraintTokenMint", "account in use"}
TYPED = ("every typed account belongs to the expected program and has the expected type",
         ["AccountOwnedByWrongProgram", "AccountDiscriminatorMismatch", "InvalidProgramId"])


# Boxes. f: the code the box stands for, found in the file after the box before it; d: what it does; w: why (the
# bond's rule or the safety it gives); e: the errors it refuses with; x: a normal exit; to: the chart it calls.
def node(k, f, d, w="", e=(), x="", to=None, file=None, after=None):
    return {"k": k, "f": f, "d": d, "w": w, "e": [e] if isinstance(e, str) else list(e), "x": x, "to": to,
            "file": file, "after": after}


def start(f, d, w="", file=None):
    return node("start", f, d, w, file=file)


def phase(f, d, file=None):
    return node("phase", f, d, file=file)


def guard(f, d, e, w="", file=None):
    return node("guard", f, d, w, e, file=file)


def check(f, d, e, w="", file=None, to=None, after=None):
    return node("check", f, d, w, e, to=to, file=file, after=after)


def step(f, d, w="", file=None, after=None):
    return node("do", f, d, w, file=file, after=after)


def call(f, d, w="", e=()):
    return node("cpi", f, d, w, e)


def sub(f, d, to, w="", e=()):
    return node("sub", f, d, w, e, to=to)


def event(f, d):
    return node("emit", f, d)


def leave(f, d, x, w=""):
    return node("leave", f, d, w, x=x)


def split(f, d, cases, w="", after=None):
    n = node("split", f, d, w, after=after)
    n["cases"] = cases
    return n


def end(d):
    return node("end", None, d)


def typed():
    return guard(None, TYPED[0], TYPED[1])


OK = "Ok: every change above commits together"

CHARTS = [
    # ------------------------------------------------------------------------------------------------ overview
    {"id": "call", "group": "How a call runs", "title": "Any instruction", "kind": "runtime", "file": LIB,
     "start": "#[program]", "regions": [],
     "intro": "What happens from the moment a transaction names bond_servicing until its changes are kept or "
              "thrown away. Every chart below is one piece of this.",
     "nodes": [
         start("#[program]", "a transaction calls bond_servicing with instruction data and a list of accounts"),
         step("#[instruction(discriminator = ExecuteInstruction::SPL_DISCRIMINATOR_SLICE)]",
              "Anchor picks the instruction by the first 8 bytes of the data",
              "transfer_hook answers the transfer-hook interface's Execute instruction, so Token-2022 can call it "
              "on every transfer"),
         guard(None, "Anchor checks every account the instruction lists, field by field in the order its accounts "
                     "struct declares them, before any of the program's own code runs",
               ["AccountNotSigner", "AccountOwnedByWrongProgram", "ConstraintHasOne", "ConstraintSeeds"],
               "a wrong account (another bond's record, another event's vault, a missing signature) stops here"),
         step(None, "the handler runs the program's checks and changes, top to bottom"),
         call(None, "it calls Token-2022, SPL Token or the System program; where an authority is needed the Bond "
                    "account signs, with its seeds [\"bond\", bond token, bump]",
              "an address derived from seeds has no private key: only bond_servicing can sign for it, and only "
              "with these seeds"),
         end("Ok: every change commits together. An error anywhere: the runtime undoes every change of the "
             "transaction, the other programs' token moves included"),
     ]},

    # ------------------------------------------------------------------------------------------------ issue
    {"id": "create_bond", "group": "Issue", "title": "create_bond", "kind": "instruction", "who": "Registrar",
     "file": INS + "create_bond.rs", "start": "pub struct CreateBond<'info>",
     "regions": ["pub struct CreateBond<'info>", "pub fn handle_create_bond("],
     "intro": "Creates the bond: its token, the Bond account with the terms and the time table, and the list of "
              "accounts the transfer hook needs, in one transaction. The program creates the token itself, so how "
              "the token is configured is set here, not trusted.",
     "nodes": [
         start("pub fn create_bond(ctx: Context<CreateBond>, terms: BondTerms) -> Result<()> {",
               "create_bond(terms) is called", file=LIB),
         phase("pub struct CreateBond<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub registrar: Signer<'info>,", "the registrar signed (it pays the new accounts' rent)",
               "AccountNotSigner"),
         guard("pub mint: Signer<'info>,", "the new bond token's keypair signed: a fresh account",
               "AccountNotSigner"),
         guard("#[account(", "creates the Bond account at [\"bond\", bond token]", "account in use",
               "one bond per token, at an address anyone can compute"),
         guard("#[account(", "creates the hook's account list at [\"extra-account-metas\", bond token]",
               "account in use", "Token-2022 reads this list on every transfer to know which accounts to pass to "
                                 "the hook"),
         phase("pub fn handle_create_bond(", "Handler"),
         sub("validate_terms(&terms)?;", "check the terms and the payment schedule", "validate_terms",
             e=["InvalidTerms", "InvalidSchedule"]),
         sub("check_cash_mint(&ctx.accounts.cash_mint.to_account_info())?;", "check the cash token",
             "check_cash_mint", e="UnsupportedCashToken"),
         sub("create_bond_mint(&ctx)?;", "create and configure the bond token", "create_bond_mint"),
         step("write_extra_account_metas(&ctx.accounts.extra_account_meta_list.to_account_info())?;",
              "write the hook's list: the Bond account and both holders' records, each holder record's address "
              "taken from its token account's owner",
              "so every transfer brings the sender's and the receiver's records to the hook"),
         step("bond.registrar = ctx.accounts.registrar.key();",
              "store the registrar, the two tokens, the terms and the time table"),
         step("bond.chain_origin = Clock::get()?.unix_timestamp;",
              "chain origin = the chain clock now: bond time starts at bond_origin from this moment"),
         step("bond.supply = PositionHistory::new(0);", "supply 0; issue size = max_supply from the terms"),
         event("emit!(BondCreated {", "BondCreated"),
         end(OK),
     ]},
    {"id": "validate_terms", "group": "Issue", "title": "validate_terms", "kind": "check",
     "file": INS + "create_bond.rs", "start": "fn validate_terms(t: &BondTerms)", "regions": ["fn validate_terms("],
     "intro": "Called by create_bond. The registrar computes payment and record days off-chain with the bond's "
              "holiday calendar; the program checks everything it can without the calendar.",
     "nodes": [
         start("fn validate_terms(t: &BondTerms) -> Result<()> {", "validate_terms(terms)"),
         check("require!(", "face value, issue size, rate, redemption price, months per period and time scale "
                            "above zero; record and payment times inside a day; at least one business weekday",
               "InvalidTerms"),
         split("match t.basis {", "the coupon basis is consistent", [
             ("fixed per period", [check("periods_per_year > 0 && periods_per_year * t.months_per_period == 12,",
                                         "periods a year x months a period = 12", "InvalidTerms")]),
             ("30/360", [check("CouponBasis::Thirty360 { convention } => require!(convention <= 2, BondError::InvalidTerms),",
                               "convention 0, 1 or 2 (Bond Basis, European, USA)", "InvalidTerms")]),
         ]),
         check("let no_holidays = Calendar::new(&[], &[])",
               "recompute the regular schedule backward from maturity, unadjusted",
               ["InvalidTerms", "InvalidSchedule"], "a schedule whose first period is not a whole period (a stub) "
                                                    "is refused"),
         check("periods.len() == t.payment_days.len()",
               "one payment day and one record day per period, at most 40 periods", "InvalidSchedule"),
         step("let business_day = |day: i32|", "business day = not one of the bond's weekend days"),
         check("for ((p, &pay), &rec) in periods.iter()",
               "for each period: the payment day is within 14 days of the period's end and a business day; the "
               "record day is a business day; record moments strictly increase; the record moment is not after "
               "the payment moment", "InvalidSchedule",
               "a holder list cannot be fixed after the money is due, and every period has exactly one record moment"),
         end("return: the terms are accepted"),
     ]},
    {"id": "check_cash_mint", "group": "Issue", "title": "check_cash_mint", "kind": "check",
     "file": INS + "create_bond.rs", "start": "fn check_cash_mint(info: &AccountInfo)",
     "regions": ["fn check_cash_mint("],
     "intro": "Called by create_bond. The cash token must deliver exactly the amount sent, and only the program may "
              "move the cash in a vault.",
     "nodes": [
         start("fn check_cash_mint(info: &AccountInfo) -> Result<()> {", "check_cash_mint(cash token)"),
         leave("if *info.owner == anchor_spl::token::ID {", "a classic SPL Token mint?",
               "yes: accepted (SPL Token has no extensions)"),
         check("require_keys_eq!(*info.owner, spl_token_2022::ID, BondError::UnsupportedCashToken);",
               "otherwise it is a Token-2022 mint", "UnsupportedCashToken"),
         check("let mint = StateWithExtensions::<MintState>::unpack(&data)", "its extensions can be read",
               "UnsupportedCashToken"),
         check("let refused = [", "no transfer fee, transfer hook, non-transferability or permanent delegate",
               "UnsupportedCashToken",
               "a fee or a hook would change what a holder receives; a permanent delegate could take a vault's cash "
               "without the program. The cash issuer's freeze and pause stay possible: they can delay a payment, "
               "not take it"),
         end("return: the cash token is accepted"),
     ]},
    {"id": "create_bond_mint", "group": "Issue", "title": "create_bond_mint", "kind": "helper",
     "file": INS + "create_bond.rs", "start": "fn create_bond_mint(ctx: &Context<CreateBond>)",
     "regions": ["fn create_bond_mint("],
     "intro": "Called by create_bond. Every authority over the bond token goes to the Bond account, which only the "
              "program can sign for.",
     "nodes": [
         start("fn create_bond_mint(ctx: &Context<CreateBond>) -> Result<()> {", "create_bond_mint"),
         check("let space = ExtensionType::try_calculate_account_len::<MintState>(&[",
               "size of a token with a permanent delegate, a transfer hook and permissioned burn", "InvalidTerms"),
         call("system_program::create_account(", "System program: create the token account, owned by Token-2022; "
                                                 "the registrar pays the rent"),
         call("spl_token_2022::instruction::initialize_permanent_delegate(",
              "Token-2022: permanent delegate = Bond account",
              "lets the program burn a holder's bonds when it pays for them (redemption, buyback, tender) without "
              "the holder signing"),
         call("transfer_hook::instruction::initialize(", "Token-2022: transfer hook = bond_servicing",
              "every transfer of the bond calls this program"),
         call("data: permissioned_burn,", "Token-2022: permissioned burn, authority = Bond account",
              "holders cannot burn bonds themselves: every burn is the program's, paired with a payment"),
         call("spl_token_2022::instruction::initialize_mint2(",
              "Token-2022: the token itself, 0 decimals, mint authority = Bond account, no freeze authority",
              "only the program issues bonds; nobody can freeze a holder's bonds"),
         step("for ix in instructions.iter() {", "the four Token-2022 instructions run in this order, the token "
                                                 "itself last"),
         end("return"),
     ]},
    {"id": "register_holder", "group": "Issue", "title": "register_holder", "kind": "instruction",
     "who": "Registrar", "file": INS + "register_holder.rs", "start": "pub struct RegisterHolder<'info>",
     "regions": ["pub struct RegisterHolder<'info>", "pub fn handle_register_holder("],
     "intro": "Admits a wallet as a holder (KYC is simulated). The transfer hook needs a holder record on both sides, "
              "so only registered wallets can hold the bond.",
     "nodes": [
         start("pub fn register_holder(ctx: Context<RegisterHolder>, owner: Pubkey) -> Result<()> {",
               "register_holder(wallet) is called", file=LIB),
         phase("pub struct RegisterHolder<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub registrar: Signer<'info>,", "the registrar signed", "AccountNotSigner"),
         guard("#[account(has_one = registrar)]", "the signer is this bond's registrar", "ConstraintHasOne",
               "only the registrar admits holders"),
         guard("#[account(", "creates the holder record at [\"holder\", bond token, wallet]", "account in use",
               "one record per wallet and bond, created once"),
         phase("pub fn handle_register_holder(", "Handler"),
         sub("let passed = bond.passed_record_moments(bond.now()?)?;", "record moments already passed", "time"),
         step("holder.position = PositionHistory::new(passed);",
              "position: 0 at every record moment so far; live_from = record moments passed",
              "a holder registered late held nothing at earlier record moments"),
         step("holder.paid = 0;", "no coupon paid, nothing locked, nothing redeemed"),
         event("emit!(HolderRegistered", "HolderRegistered"),
         end(OK),
     ]},
    {"id": "place", "group": "Issue", "title": "place", "kind": "instruction", "who": "Registrar",
     "file": INS + "place.rs", "start": "pub struct Place<'info>",
     "regions": ["pub struct Place<'info>", "pub fn handle_place("],
     "intro": "Issues bonds to a registered holder, before the first record moment and up to the issue size.",
     "nodes": [
         start("pub fn place(ctx: Context<Place>, amount: u64) -> Result<()> {", "place(amount) is called",
               file=LIB),
         phase("pub struct Place<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub registrar: Signer<'info>,", "the registrar signed", "AccountNotSigner"),
         guard("#[account(mut, has_one = registrar, has_one = mint)]", "this bond's registrar and this bond's token",
               "ConstraintHasOne"),
         guard("#[account(mut, has_one = bond, seeds = [HOLDER_SEED", "the holder record of this bond, at its address",
               ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("associated_token::mint = mint,", "the holder's own bond token account",
               ["ConstraintAssociated", "ConstraintTokenOwner"]),
         guard("pub token_program: Program<'info, Token2022>,", "the real Token-2022 program", "InvalidProgramId"),
         phase("pub fn handle_place(", "Handler"),
         check("require!(bond.now()? < bond.record_time(0)?, BondError::PlacementClosed);",
               "bond time is before the first record moment", "PlacementClosed",
               "the issue is complete before any coupon's holder list is fixed, so no position history needs "
               "saving here"),
         check("let new_supply = bond.supply.quantity.checked_add(amount).ok_or(BondError::IssueTooLarge)?;",
               "new supply = supply + amount", "IssueTooLarge"),
         check("require!(new_supply <= bond.max_supply, BondError::ExceedsIssueSize);",
               "new supply is at most the issue size", "ExceedsIssueSize",
               "the registrar cannot issue more bonds than the terms allow"),
         sub("bond.check_serviceable(new_supply)?;",
             "every coupon and the principal on the new supply fit in a token amount", "serviceable",
             e="IssueTooLarge"),
         call("token_2022::mint_to(", "Token-2022 MintTo: amount bonds into the holder's account; the Bond account "
                                      "signs as mint authority"),
         check("holder.position.quantity = holder.position.quantity.checked_add(amount).ok_or(BondError::Overflow)?;",
               "holder's quantity + amount", "Overflow"),
         check("bond.supply.quantity = bond.supply.quantity.checked_add(amount).ok_or(BondError::Overflow)?;",
               "supply + amount", "Overflow", "the supply is counted by the program, so a coupon's budget never "
                                              "needs a count of holders"),
         event("emit!(BondsPlaced", "BondsPlaced"),
         end(OK),
     ]},

    # ------------------------------------------------------------------------------------------------ transfer
    {"id": "transfer_hook", "group": "Every transfer", "title": "transfer_hook", "kind": "instruction",
     "who": "Token-2022, inside a holder's transfer", "file": INS + "transfer_hook.rs",
     "start": "pub struct TransferHook<'info>",
     "regions": ["pub struct TransferHook<'info>", "pub fn handle_transfer_hook("],
     "intro": "Token-2022 calls this after it has moved the balances of every bond transfer, in the same "
              "transaction. Before the two holders' quantities change, each holder's quantity is kept for every "
              "record moment passed since its last change: this replaces the snapshot of holders.",
     "nodes": [
         start("pub fn transfer_hook(ctx: Context<TransferHook>, amount: u64) -> Result<()> {",
               "Token-2022 calls transfer_hook(amount)", file=LIB),
         phase("pub struct TransferHook<'info>", "Accounts: passed by Token-2022 from the hook's list"),
         typed(),
         guard("#[account(token::mint = mint)]", "the source is an account of the bond token", "ConstraintTokenMint"),
         guard("#[account(token::mint = mint)]", "the destination is an account of the bond token",
               "ConstraintTokenMint"),
         guard("#[account(seeds = [EXTRA_ACCOUNT_METAS_SEED", "the hook's list of this bond token",
               "ConstraintSeeds"),
         guard("#[account(seeds = [BOND_SEED", "the Bond account of this bond token", "ConstraintSeeds"),
         guard("#[account(mut, seeds = [HOLDER_SEED, mint.key().as_ref(), source_token.owner.as_ref()], bump)]",
               "the sender's holder record address", "ConstraintSeeds"),
         guard("#[account(mut, seeds = [HOLDER_SEED, mint.key().as_ref(), destination_token.owner.as_ref()], bump)]",
               "the receiver's holder record address", "ConstraintSeeds"),
         phase("pub fn handle_transfer_hook(", "Handler"),
         check("check_is_transferring(&a.source_token)?;", "Token-2022 marked the source account as being "
                                                           "transferred", "NotTransferring",
               "the hook runs only inside a real transfer: calling it directly to fake a change of position fails"),
         check("check_associated(&a.source_token, &a.mint)?;", "the source is its owner's associated account",
               "NotAssociatedTokenAccount", "one bond account per holder, so one holder record is one position"),
         check("check_associated(&a.destination_token, &a.mint)?;",
               "the destination is its owner's associated account", "NotAssociatedTokenAccount"),
         check("let mut source = read_record(&a.source_holder.to_account_info())?;",
               "the sender's holder record exists (bond_servicing owns it)", "AccountOwnedByWrongProgram",
               "an unregistered wallet has no record: it can neither send nor receive the bond"),
         check("let mut destination = if same", "the receiver's holder record exists, unless the sender sends to "
                                                "itself", "AccountOwnedByWrongProgram"),
         sub("let now = bond.now()?;", "bond time now", "time"),
         check("require!(now < bond.final_record_time()?, BondError::TransfersClosedAtMaturity);",
               "bond time is before the final record moment", "TransfersClosedAtMaturity",
               "redemption pays on the holdings at the final record moment, so they cannot move after it"),
         leave("if same { return Ok(()); }", "the same account on both sides?", "yes: nothing changes, return Ok"),
         sub("let passed = bond.passed_record_moments_from(now, ",
             "record moments passed (searched from the later of the two holders' last changes)", "time"),
         check("let source_before = a.source_token.amount.checked_add(amount).ok_or(BondError::Overflow)?;",
               "balances before the transfer: sender's now + amount, receiver's now - amount", "Overflow"),
         sub("update_record(&mut source, bond.key(), passed, source_before, a.source_token.amount)?;",
             "sender: check, save the history, set the new quantity", "update_record", e="PositionMismatch"),
         check("require!(source.locked <= a.source_token.amount, BondError::BondsLocked);",
               "the sender keeps at least its tendered bonds", "BondsLocked",
               "tendered bonds stay in the account and keep their coupons, but cannot leave it"),
         sub("update_record(destination, bond.key(), passed, destination_before, a.destination_token.amount)?;",
             "receiver: check, save the history, set the new quantity", "update_record", e="PositionMismatch"),
         step("source.try_serialize(", "write both holder records"),
         end("Ok: the transfer completes with both records updated; any error undoes the transfer itself"),
     ]},
    {"id": "update_record", "group": "Every transfer", "title": "update_record", "kind": "helper",
     "file": INS + "transfer_hook.rs", "start": "fn update_record(", "regions": ["fn update_record("],
     "intro": "Called by transfer_hook for the sender and the receiver.",
     "nodes": [
         start("fn update_record(", "update_record(record, passed, before, after)"),
         check("require!(record.position.quantity == before, BondError::PositionMismatch);",
               "the record's quantity equals the account's balance before the transfer", "PositionMismatch",
               "every balance change goes through the program, so they always agree; a disagreement means a change "
               "the program did not see, and the transfer is refused"),
         sub("if let Some((from_event, to_event)) = record.position.save(passed) {",
             "save the quantity for every record moment passed since the last change", "position"),
         event("emit!(PositionsSaved", "PositionsSaved: the holder held this quantity at those record moments"),
         step("record.position.quantity = after;", "quantity = the balance after the transfer"),
         end("return"),
     ]},

    # ------------------------------------------------------------------------------------------------ coupons
    {"id": "open_event", "group": "Coupons and redemption", "title": "open_event", "kind": "instruction",
     "who": "Anyone", "file": INS + "open_event.rs", "start": "pub struct OpenEvent<'info>",
     "regions": ["pub struct OpenEvent<'info>", "pub fn handle_open_event("],
     "intro": "Opens a coupon or the redemption once its record moment has passed. Everything it writes follows "
              "from the terms and the supply history, so it does not matter who sends it.",
     "nodes": [
         start("pub fn open_event(ctx: Context<OpenEvent>, index: u16) -> Result<()> {",
               "open_event(index) is called", file=LIB),
         phase("pub struct OpenEvent<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub payer: Signer<'info>,", "any wallet signed (it pays the rent)", "AccountNotSigner"),
         guard("#[account(has_one = cash_mint)]", "the bond's own cash token", "ConstraintHasOne"),
         guard("space = 8 + Event::INIT_SPACE,", "creates the event at [\"event\", bond, index]", "account in use",
               "each coupon and the redemption is opened once"),
         guard("seeds = [VAULT_SEED, event.key().as_ref()],",
               "creates the event's vault at [\"vault\", event]: a cash account whose authority is the Bond account",
               "account in use", "each event has its own cash, so one coupon cannot spend another's"),
         phase("pub fn handle_open_event(", "Handler"),
         sub("let period = bond.period_of(index)?;", "the index is a coupon (0 to n-1) or the redemption (n)", "time",
             e="EventOutOfRange"),
         sub("let record_time = bond.record_time(index)?;", "its record moment", "time"),
         check("require!(bond.now()? >= record_time, BondError::RecordMomentNotReached);",
               "the record moment has passed", "RecordMomentNotReached",
               "the supply at the record moment is final only once that moment has passed"),
         sub("let supply = bond.supply.at_moment(period);", "supply at the record moment, from the supply history",
             "position", "no holder is counted"),
         split("let budget = if is_redemption {", "the budget on the whole supply", [
             ("redemption", [sub("bond.principal(supply)?", "principal(supply)", "amounts")]),
             ("coupon", [sub("bond.coupon(supply, period)?", "coupon(supply, period)", "amounts")]),
         ], "rounded down once; each holder's amount is rounded down on its own, so the holders' amounts never add "
            "up to more than the budget"),
         step("event.record_time = record_time;", "store the kind, record moment, payment time, supply at the record "
                                                  "moment, budget and vault; funded = false"),
         event("emit!(EventOpened", "EventOpened"),
         end(OK),
     ]},
    {"id": "confirm_funding", "group": "Coupons and redemption", "title": "confirm_funding", "kind": "instruction",
     "who": "Anyone", "file": INS + "confirm_funding.rs", "start": "pub struct ConfirmFunding<'info>",
     "regions": ["pub struct ConfirmFunding<'info>", "pub fn handle_confirm_funding("],
     "intro": "After the issuer has put cash into the event's vault (an ordinary token transfer), anyone confirms that "
              "it covers the budget. No signer is needed.",
     "nodes": [
         start("pub fn confirm_funding(ctx: Context<ConfirmFunding>) -> Result<()> {",
               "confirm_funding is called", file=LIB),
         phase("pub struct ConfirmFunding<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("#[account(mut, has_one = vault, seeds = [EVENT_SEED", "the event at its address, and its own vault",
               ["ConstraintHasOne", "ConstraintSeeds"]),
         phase("pub fn handle_confirm_funding(", "Handler"),
         check("require!(!event.funded, BondError::AlreadyFunded);", "not confirmed before", "AlreadyFunded"),
         step("let vault_balance = ctx.accounts.vault.amount;", "read the vault's balance"),
         check("require!(vault_balance >= event.budget, BondError::InsufficientFunding);",
               "the vault holds at least the budget", "InsufficientFunding",
               "payment starts only when every holder can be paid"),
         step("event.funded = true;", "funded = true"),
         event("emit!(EventFunded", "EventFunded"),
         end(OK),
     ]},
    {"id": "pay_coupon", "group": "Coupons and redemption", "title": "pay_coupon", "kind": "instruction",
     "who": "Anyone", "file": INS + "pay_coupon.rs", "start": "pub struct PayCoupon<'info>",
     "regions": ["pub struct PayCoupon<'info>", "pub fn handle_pay_coupon("],
     "intro": "Pays one holder one coupon, at any time after payment opens, in any order, sent by anyone: the amount "
              "comes from the holder's own record and the terms, and the cash can only go to the holder.",
     "nodes": [
         start("pub fn pay_coupon(ctx: Context<PayCoupon>) -> Result<()> {", "pay_coupon is called", file=LIB),
         phase("pub struct PayCoupon<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub payer: Signer<'info>,", "any wallet signed (it pays the fee)", "AccountNotSigner"),
         guard("#[account(has_one = cash_mint)]", "the bond's own cash token", "ConstraintHasOne"),
         guard("#[account(has_one = bond, has_one = vault, seeds = [EVENT_SEED",
               "an event of this bond, at its address, with its own vault", ["ConstraintHasOne", "ConstraintSeeds"],
               "another event's vault is refused"),
         guard("#[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED",
               "the holder record of this wallet and bond", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("init_if_needed,", "the wallet's own associated cash account, created if missing",
               ["ConstraintAssociated", "ConstraintTokenOwner"],
               "whoever sends the transaction, the coupon can only go to the holder"),
         phase("pub fn handle_pay_coupon(", "Handler"),
         check("require!(event.kind == EventKind::Coupon, BondError::WrongEventKind);", "a coupon, not the redemption",
               "WrongEventKind"),
         check("require!(event.funded, BondError::NotFunded);", "the event is confirmed funded", "NotFunded"),
         check("require!(bond.now()? >= event.payment_time, BondError::PaymentNotOpen);",
               "bond time has reached the payment time", "PaymentNotOpen"),
         check("require!(!ctx.accounts.holder.is_paid(e), BondError::AlreadyPaid);", "coupon e not paid to this "
                                                                                     "holder yet", "AlreadyPaid"),
         sub("let position = ctx.accounts.holder.position.at_moment(e);", "the holder's position at record moment e",
             "position", "what it held at the record moment, whatever it holds now"),
         sub("let amount = bond.coupon(position, e)?;", "the coupon on that position", "amounts"),
         check("require!(amount > 0, BondError::NothingToPay);", "something is owed", "NothingToPay"),
         sub("pay_cash(", "pay from the event's vault to the holder's cash account", "pay_cash",
             e="MemoProgramMissing"),
         step("holder.paid |= 1u64 << e;", "mark coupon e paid",
              "in the same transaction as the payment: both happen or neither"),
         event("emit!(CouponPaid", "CouponPaid"),
         end(OK),
     ]},
    {"id": "redeem_holder", "group": "Coupons and redemption", "title": "redeem_holder", "kind": "instruction",
     "who": "Anyone", "file": INS + "redeem_holder.rs", "start": "pub struct RedeemHolder<'info>",
     "regions": ["pub struct RedeemHolder<'info>", "pub fn handle_redeem_holder("],
     "intro": "At maturity, burns one holder's bonds and pays their principal in one transaction: no bond is burned "
              "unpaid and no principal is paid for a live bond.",
     "nodes": [
         start("pub fn redeem_holder(ctx: Context<RedeemHolder>) -> Result<()> {", "redeem_holder is called",
               file=LIB),
         phase("pub struct RedeemHolder<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub payer: Signer<'info>,", "any wallet signed (it pays the fee)", "AccountNotSigner"),
         guard("#[account(mut, has_one = cash_mint, has_one = mint)]", "the bond's own tokens", "ConstraintHasOne"),
         guard("#[account(has_one = bond, has_one = vault, seeds = [EVENT_SEED",
               "an event of this bond, at its address, with its own vault", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("#[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED",
               "the holder record of this wallet and bond", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("associated_token::mint = mint,", "the wallet's own bond account",
               ["ConstraintAssociated", "ConstraintTokenOwner"]),
         guard("init_if_needed,", "the wallet's own cash account, created if missing",
               ["ConstraintAssociated", "ConstraintTokenOwner"]),
         guard("pub token_program: Program<'info, Token2022>,", "the real Token-2022 program", "InvalidProgramId"),
         phase("pub fn handle_redeem_holder(", "Handler"),
         check("require!(a.event.kind == EventKind::Redemption, BondError::WrongEventKind);", "the redemption event",
               "WrongEventKind"),
         check("require!(a.event.funded, BondError::NotFunded);", "the redemption is confirmed funded", "NotFunded"),
         check("require!(a.bond.now()? >= a.event.payment_time, BondError::PaymentNotOpen);",
               "bond time has reached the payment time", "PaymentNotOpen"),
         step("let passed = a.bond.periods();", "every record moment has passed"),
         check("let position = a.holder.position.quantity;", "the record's quantity equals the bond balance",
               "PositionMismatch"),
         check("let free = position.checked_sub(a.holder.locked).ok_or(BondError::Overflow)?;",
               "free = quantity - bonds accepted by a tender and not yet bought", "Overflow",
               "those are left to their offer, which pays for them"),
         check("require!(free > 0, BondError::NothingToPay);", "something to redeem", "NothingToPay"),
         check("let before = a.holder.redeemed;", "redeemed so far; after = before + free", "Overflow"),
         sub("let principal = a.bond.principal(after)? - a.bond.principal(before)?;",
             "principal = principal(after) - principal(before)", "amounts",
             "several partial redemptions round exactly like one"),
         sub("burn_bonds(", "burn the free bonds", "burn_bonds"),
         sub("pay_cash(", "pay the principal from the redemption vault", "pay_cash", e="MemoProgramMissing"),
         sub("retire_quantity(&mut a.holder, &mut a.bond, bond_key, passed, free)?;",
             "save both histories, then take the bonds off the holder and the supply", "retire_quantity",
             "the final coupon stays payable whether it is paid before or after", e="Overflow"),
         step("a.holder.redeemed = after;", "redeemed = after"),
         event("emit!(BondsRedeemed", "BondsRedeemed"),
         end(OK),
     ]},

    # ------------------------------------------------------------------------------------------------ offers
    {"id": "create_offer", "group": "Buyback and tender", "title": "create_offer", "kind": "instruction",
     "who": "Registrar", "file": INS + "offer.rs", "start": "pub struct CreateOffer<'info>",
     "regions": ["pub struct CreateOffer<'info>", "pub fn handle_create_offer("],
     "intro": "The issuer offers to buy bonds back at a price, up to a maximum, during a window: a buyback (first "
              "come) or a tender offer (pro rata).",
     "nodes": [
         start("pub fn create_offer(", "create_offer(id, kind, price, maximum, window) is called", file=LIB),
         phase("pub struct CreateOffer<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub registrar: Signer<'info>,", "the registrar signed", "AccountNotSigner"),
         guard("#[account(has_one = registrar, has_one = cash_mint)]", "this bond's registrar and cash token",
               "ConstraintHasOne"),
         guard("space = 8 + Offer::INIT_SPACE,", "creates the offer at [\"offer\", bond, id]", "account in use"),
         guard("seeds = [VAULT_SEED, offer.key().as_ref()],",
               "creates the offer's vault at [\"vault\", offer]: authority = Bond account", "account in use"),
         phase("pub fn handle_create_offer(", "Handler"),
         check("require!(", "price and maximum above zero; the window opens before it closes and closes by the final "
                            "record moment; price x maximum fits in a token amount", "InvalidOffer",
               "an offer must end while bonds can still move"),
         step("offer.kind = if pro_rata { OfferKind::ProRata } else { OfferKind::FirstCome };",
              "buyback (first come) or tender offer (pro rata)"),
         step("offer.total = 0;", "nothing bought or tendered, not funded, no tenders"),
         event("emit!(OfferCreated", "OfferCreated"),
         end(OK),
     ]},
    {"id": "confirm_offer_funding", "group": "Buyback and tender", "title": "confirm_offer_funding",
     "kind": "instruction", "who": "Anyone", "file": INS + "offer.rs", "start": "pub struct ConfirmOfferFunding<'info>",
     "regions": ["pub struct ConfirmOfferFunding<'info>", "pub fn handle_confirm_offer_funding("],
     "intro": "Anyone confirms that the offer's vault holds price x maximum. Only then may holders sell or tender.",
     "nodes": [
         start("pub fn confirm_offer_funding(ctx: Context<ConfirmOfferFunding>) -> Result<()> {",
               "confirm_offer_funding is called", file=LIB),
         phase("pub struct ConfirmOfferFunding<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("#[account(mut, has_one = vault, seeds = [OFFER_SEED", "the offer at its address, and its own vault",
               ["ConstraintHasOne", "ConstraintSeeds"]),
         phase("pub fn handle_confirm_offer_funding(", "Handler"),
         check("require!(!offer.funded, BondError::AlreadyFunded);", "not confirmed before", "AlreadyFunded"),
         check("let budget = offer.budget().ok_or(BondError::Overflow)?;", "budget = price x maximum", "Overflow"),
         check("require!(vault_balance >= budget, BondError::InsufficientFunding);",
               "the vault holds at least the budget", "InsufficientFunding",
               "holders sell or tender only into an offer that can pay for all of it"),
         step("offer.funded = true;", "funded = true"),
         event("emit!(OfferFunded", "OfferFunded"),
         end(OK),
     ]},
    {"id": "retirement", "group": "Buyback and tender", "title": "Retirement accounts", "kind": "accounts",
     "file": INS + "offer.rs", "start": "pub struct Retirement<'info>", "regions": ["pub struct Retirement<'info>"],
     "intro": "The accounts sell_to_offer and settle_tender share: whose bonds are burned and where the cash goes.",
     "nodes": [
         phase("pub struct Retirement<'info>", "Accounts: Anchor checks these first, in this order"),
         typed(),
         guard("pub payer: Signer<'info>,", "a wallet signed (it pays the fee)", "AccountNotSigner"),
         guard("#[account(mut, has_one = mint, has_one = cash_mint)]", "the bond's own tokens", "ConstraintHasOne"),
         guard("#[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED",
               "the holder record of this wallet and bond", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("associated_token::mint = mint,", "the wallet's own bond account",
               ["ConstraintAssociated", "ConstraintTokenOwner"]),
         guard("init_if_needed,", "the wallet's own cash account, created if missing",
               ["ConstraintAssociated", "ConstraintTokenOwner"],
               "the cash can only go to the holder whose bonds are burned"),
         guard("pub token_program: Program<'info, Token2022>,", "the real Token-2022 program", "InvalidProgramId"),
         end("the instruction's own accounts follow"),
     ]},
    {"id": "sell_to_offer", "group": "Buyback and tender", "title": "sell_to_offer", "kind": "instruction",
     "who": "Holder", "file": INS + "offer.rs", "start": "pub struct SellToOffer<'info>",
     "regions": ["pub struct SellToOffer<'info>", "pub fn handle_sell_to_offer("],
     "intro": "A holder sells bonds into a buyback: burned and paid for in one transaction, first come, first served "
              "until the maximum.",
     "nodes": [
         start("pub fn sell_to_offer(ctx: Context<SellToOffer>, quantity: u64) -> Result<()> {",
               "sell_to_offer(quantity) is called", file=LIB),
         phase("pub struct SellToOffer<'info>", "Accounts: Anchor checks these first"),
         sub("pub retirement: Retirement<'info>,", "the retirement accounts", "retirement"),
         guard("#[account(constraint = seller.key() == retirement.owner.key())]",
               "the signer is the holder whose bonds are sold", ["AccountNotSigner", "ConstraintRaw"]),
         guard("constraint = offer.bond == retirement.bond.key() && offer.vault == retirement.vault.key(),",
               "an offer of this bond, at its address, paying from its own vault", ["ConstraintRaw", "ConstraintSeeds"]),
         phase("pub fn handle_sell_to_offer(", "Handler"),
         sub("let now = a.retirement.bond.now()?;", "bond time now", "time"),
         check("require!(a.offer.kind == OfferKind::FirstCome, BondError::WrongOfferKind);", "a buyback (first come)",
               "WrongOfferKind"),
         check("require!(a.offer.funded, BondError::OfferNotFunded);", "the offer is confirmed funded",
               "OfferNotFunded"),
         check("require!(a.offer.open_time <= now && now < a.offer.close_time, BondError::OfferNotOpen);",
               "inside the window", "OfferNotOpen"),
         check("let total = a.offer.total.checked_add(quantity).ok_or(BondError::Overflow)?;",
               "total = bought so far + quantity", "Overflow"),
         check("require!(quantity > 0 && total <= a.offer.max_quantity, BondError::ExceedsOffer);",
               "quantity above zero and the total at most the maximum", "ExceedsOffer"),
         check("require!(quantity <= a.retirement.holder.unlocked(), BondError::InsufficientUnlocked);",
               "at most the holder's untendered bonds", "InsufficientUnlocked"),
         step("a.offer.total = total;", "offer total = total"),
         sub("buy_back(&mut a.retirement, a.offer.id, a.offer.price_units, quantity)?;",
             "burn the bonds and pay price x quantity", "buy_back", e=["PositionMismatch", "Overflow"]),
         end(OK),
     ]},
    {"id": "buy_back", "group": "Buyback and tender", "title": "buy_back", "kind": "helper", "file": INS + "offer.rs",
     "start": "fn buy_back(r: &mut Retirement", "regions": ["fn buy_back("],
     "intro": "Called by sell_to_offer and settle_tender.",
     "nodes": [
         start("fn buy_back(r: &mut Retirement", "buy_back(quantity, price)"),
         check("require!(r.holder_bonds.amount == r.holder.position.quantity, BondError::PositionMismatch);",
               "the record's quantity equals the bond balance", "PositionMismatch"),
         check("let paid = quantity.checked_mul(price_units).ok_or(BondError::Overflow)?;", "paid = quantity x price",
               "Overflow"),
         sub("let passed = r.bond.passed_record_moments(r.bond.now()?)?;", "record moments passed", "time"),
         sub("burn_bonds(", "burn the quantity", "burn_bonds"),
         sub("pay_cash(", "pay from the offer's vault", "pay_cash", e="MemoProgramMissing"),
         sub("retire_quantity(&mut r.holder, &mut r.bond, bond_key, passed, quantity)?;",
             "save both histories, then take the bonds off the holder and the supply", "retire_quantity",
             "a coupon whose record moment has passed is still paid on the bonds sold after it", e="Overflow"),
         event("emit!(BondsBoughtBack", "BondsBoughtBack"),
         end("return"),
     ]},
    {"id": "change_tender", "group": "Buyback and tender", "title": "change_tender", "kind": "instruction",
     "who": "Holder", "file": INS + "offer.rs", "start": "pub struct ChangeTender<'info>",
     "regions": ["pub struct ChangeTender<'info>", "pub fn handle_change_tender("],
     "intro": "A holder tenders bonds into a tender offer, or withdraws them, while the window is open. The bonds do "
              "not move: they are locked in the holder's account.",
     "nodes": [
         start("pub fn change_tender(ctx: Context<ChangeTender>, quantity: u64, withdraw: bool) -> Result<()> {",
               "change_tender(quantity, withdraw) is called", file=LIB),
         phase("pub struct ChangeTender<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("pub owner: Signer<'info>,", "the holder signed", "AccountNotSigner"),
         guard("#[account(mut, has_one = bond, seeds = [OFFER_SEED", "an offer of this bond, at its address",
               ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("#[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED",
               "the signer's own holder record", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("init_if_needed,", "its tender at [\"tender\", offer, wallet], created at its first tender",
               "ConstraintSeeds"),
         phase("pub fn handle_change_tender(", "Handler"),
         sub("let now = a.bond.now()?;", "bond time now", "time"),
         check("require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);",
               "a tender offer (pro rata)", "WrongOfferKind"),
         check("require!(a.offer.funded, BondError::OfferNotFunded);", "the offer is confirmed funded",
               "OfferNotFunded"),
         check("require!(a.offer.open_time <= now && now < a.offer.close_time, BondError::OfferNotOpen);",
               "inside the window", "OfferNotOpen"),
         check("require!(quantity > 0, BondError::ExceedsOffer);", "quantity above zero", "ExceedsOffer"),
         split("if tender.offer == Pubkey::default() {", "the holder's first tender in this offer?", [
             ("first", [check("tender.number = a.offer.tenders;",
                              "its number = tenders made so far; the offer counts one more", "Overflow")]),
             ("later", []),
         ], "the number fixes the order in which tenders are resolved"),
         split("if withdraw {", "withdraw or tender more?", [
             ("withdraw", [check("require!(quantity <= tender.quantity, BondError::ExceedsOffer);",
                                 "at most what it tendered", "ExceedsOffer"),
                           step("tender.quantity -= quantity;", "tender, lock and offer total - quantity")]),
             ("tender", [check("require!(quantity <= a.holder.unlocked(), BondError::InsufficientUnlocked);",
                               "at most its untendered bonds", "InsufficientUnlocked"),
                         check("tender.quantity = tender.quantity.checked_add(quantity).ok_or(BondError::Overflow)?;",
                               "tender, lock and offer total + quantity", "Overflow")]),
         ], "the lock keeps tendered bonds in the holder's account, so a coupon whose record moment falls inside the "
            "window is still paid on them"),
         event("emit!(TenderChanged {", "TenderChanged"),
         end(OK),
     ]},
    {"id": "resolve_tender", "group": "Buyback and tender", "title": "resolve_tender", "kind": "instruction",
     "who": "Anyone", "file": INS + "offer.rs", "start": "pub struct ResolveTender<'info>",
     "regions": ["pub struct ResolveTender<'info>", "pub fn handle_resolve_tender("],
     "intro": "After the window, fixes one tender's accepted part and releases the rest. No cash and no token "
              "account are involved, so the rejected bonds are free again even if the cash cannot move. No signer is "
              "needed.",
     "nodes": [
         start("pub fn resolve_tender(ctx: Context<ResolveTender>) -> Result<()> {", "resolve_tender is called",
               file=LIB),
         phase("pub struct ResolveTender<'info>", "Accounts: Anchor checks these first"),
         typed(),
         guard("#[account(mut, has_one = bond, seeds = [OFFER_SEED", "an offer of this bond, at its address",
               ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("#[account(mut, has_one = bond, seeds = [HOLDER_SEED, bond.mint.as_ref(), holder.owner.as_ref()]",
               "a holder record of this bond, at its address", ["ConstraintHasOne", "ConstraintSeeds"]),
         guard("#[account(mut, seeds = [TENDER_SEED", "that holder's tender in this offer", "ConstraintSeeds"),
         phase("pub fn handle_resolve_tender(", "Handler"),
         check("require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);",
               "a tender offer (pro rata)", "WrongOfferKind"),
         check("require!(a.bond.now()? >= a.offer.close_time, BondError::OfferStillOpen);", "the window has closed",
               "OfferStillOpen"),
         sub("resolve(a.bond.key(), &mut a.offer, &mut a.tender, &mut a.holder)", "resolve this tender", "resolve",
             e=["TenderOutOfOrder", "Overflow"]),
         end(OK),
     ]},
    {"id": "resolve", "group": "Buyback and tender", "title": "resolve", "kind": "helper", "file": INS + "offer.rs",
     "start": "fn resolve(bond_key: Pubkey", "regions": ["fn resolve(bond_key: Pubkey"],
     "intro": "Called by resolve_tender, and by settle_tender when nobody has resolved the tender yet.",
     "nodes": [
         start("fn resolve(bond_key: Pubkey", "resolve(offer, tender, holder)"),
         leave("if tender.resolved {", "already resolved?", "yes: left as it is, return"),
         check("require!(tender.number == offer.resolved, BondError::TenderOutOfOrder);",
               "this tender's number is the next to resolve", "TenderOutOfOrder",
               "tenders are resolved in the order they were made, so nobody gains by choosing when to resolve"),
         sub("let accepted = offer.accept_next(tender.quantity).ok_or(BondError::Overflow)?;",
             "the accepted part", "accept_next", e="Overflow"),
         check("holder.locked = holder.locked.checked_sub(rejected).ok_or(BondError::Overflow)?;",
               "rejected = tendered - accepted: unlocked at once", "Overflow"),
         step("tender.accepted = accepted;", "accepted is fixed; resolved = true"),
         event("emit!(TenderResolved", "TenderResolved"),
         end("return"),
     ]},
    {"id": "settle_tender", "group": "Buyback and tender", "title": "settle_tender", "kind": "instruction",
     "who": "Anyone", "file": INS + "offer.rs", "start": "pub struct SettleTender<'info>",
     "regions": ["pub struct SettleTender<'info>", "pub fn handle_settle_tender("],
     "intro": "After the window, buys one tender's accepted part: burned and paid for in one transaction, and "
              "released from the lock.",
     "nodes": [
         start("pub fn settle_tender(ctx: Context<SettleTender>) -> Result<()> {", "settle_tender is called",
               file=LIB),
         phase("pub struct SettleTender<'info>", "Accounts: Anchor checks these first"),
         sub("pub retirement: Retirement<'info>,", "the retirement accounts", "retirement"),
         guard("constraint = offer.bond == retirement.bond.key() && offer.vault == retirement.vault.key(),",
               "an offer of this bond, at its address, paying from its own vault", ["ConstraintRaw", "ConstraintSeeds"]),
         guard("seeds = [TENDER_SEED, offer.key().as_ref(), retirement.owner.key().as_ref()],",
               "the tender of the holder whose bonds are bought", "ConstraintSeeds"),
         phase("pub fn handle_settle_tender(", "Handler"),
         check("require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);",
               "a tender offer (pro rata)", "WrongOfferKind"),
         check("require!(a.retirement.bond.now()? >= a.offer.close_time, BondError::OfferStillOpen);",
               "the window has closed", "OfferStillOpen"),
         sub("resolve(a.retirement.bond.key(), &mut a.offer, &mut a.tender, &mut a.retirement.holder)?;",
             "resolve it first if nobody has", "resolve", e=["TenderOutOfOrder", "Overflow"]),
         check("require!(!a.tender.settled, BondError::AlreadySettled);", "not settled before", "AlreadySettled"),
         check("a.retirement.holder.locked = a.retirement.holder.locked.checked_sub(accepted).ok_or(BondError::Overflow)?;",
               "release the accepted part from the lock; settled = true", "Overflow"),
         leave("if accepted > 0 {", "anything accepted?", "no: done, return Ok"),
         sub("buy_back(&mut a.retirement, a.offer.id, a.offer.price_units, accepted)?;",
             "burn the accepted bonds and pay price x accepted", "buy_back", e=["PositionMismatch", "Overflow"]),
         end(OK),
     ]},

    # ------------------------------------------------------------------------------------------------ shared rules
    {"id": "time", "group": "Rules the instructions use", "title": "Bond time and record moments", "kind": "rule",
     "file": STATE, "start": "pub fn period_of(&self, event: u16)",
     "regions": ["pub fn period_of(", "pub fn now(", "fn local_moment(", "pub fn record_time(", "pub fn payment_time(",
                 "pub fn final_record_time(", "pub fn passed_record_moments_from("],
     "intro": "Every date rule compares bond time, read from the chain clock. Nobody passes a time in.",
     "nodes": [
         start("pub fn period_of(&self, event: u16) -> Result<u16> {", "period_of(event)"),
         check("require!(event <= self.redemption_event(), BondError::EventOutOfRange);",
               "the event exists: coupons 0 to n-1, the redemption n", "EventOutOfRange"),
         step("Ok(if event == self.redemption_event() { event - 1 } else { event })",
              "the redemption uses the final coupon period's dates"),
         step("let chain_now = Clock::get()?.unix_timestamp;", "now(): read the chain clock (Clock sysvar)",
              "the cluster's time; no instruction argument can change it"),
         check("chain_to_bond_time(chain_now, self.chain_origin, self.bond_origin, self.chain_seconds_per_bond_day)",
               "bond time = bond origin + (chain time - chain origin) x 86,400 / chain seconds per bond day",
               "Overflow", "86,400 is real time; the devnet runs compress a bond day into a few chain seconds"),
         step("i64::from(day) * SECONDS_PER_DAY + i64::from(seconds_local) - i64::from(self.utc_offset_seconds)",
              "a local moment = day x 86,400 + seconds after midnight - the bond's UTC offset",
              "09:00 in Almaty (UTC+5) is 04:00 UTC"),
         step("Ok(self.local_moment(self.record_days[period as usize], self.record_seconds_local))",
              "record moment of an event = its period's record day at the record time"),
         step("Ok(self.local_moment(self.payment_days[period as usize], self.payment_seconds_local))",
              "payment opens on the payment day at the payment time"),
         step("self.record_time(self.periods() - 1)", "final record moment = the last coupon's record moment",
              "transfers stop here; offers must close by it"),
         step("let passed = self.record_days[lo..].partition_point(",
              "record moments passed = how many record moments are at or before now (a binary search starting from "
              "the holder's last change)"),
         end("return"),
     ]},
    {"id": "position", "group": "Rules the instructions use", "title": "Position history", "kind": "rule",
     "file": STATE, "start": "pub fn save(&mut self, passed: u16)", "regions": ["pub fn save(", "pub fn at_moment("],
     "intro": "How the program knows what a holder held at a record moment without a snapshot. Every change to a "
              "holding (transfer, sale, tender settlement, redemption) calls save first. A holder record keeps the "
              "quantity now, live_from (how many record moments had passed at its last change) and at[e] for each "
              "record moment e. The bond's supply has the same history.",
     "nodes": [
         start("pub fn save(&mut self, passed: u16) -> Option<(u16, u16)> {", "save(passed): before every change"),
         leave("if passed <= self.live_from {", "no record moment passed since the last change?",
               "yes: nothing to save, return"),
         step("for e in from..passed {", "for every record moment passed since the last change: at[e] = quantity",
              "between two changes the quantity is constant, so this is exactly what was held at each of those "
              "moments, however late it is written"),
         step("self.live_from = passed;", "live_from = record moments passed"),
         split("pub fn at_moment(&self, e: u16) -> u64 {", "at_moment(e): the quantity at record moment e", [
             ("e < live_from", [step("self.at[e as usize]", "at[e]: saved by the first change after moment e")]),
             ("otherwise", [step("self.quantity", "the quantity now: no change since moment e")]),
         ], "a holder that never trades needs no transaction at all"),
         end("return"),
     ]},
    {"id": "amounts", "group": "Rules the instructions use", "title": "Coupon and principal", "kind": "rule",
     "file": STATE, "start": "pub fn coupon(&self, position: u64, period: u16)",
     "regions": ["pub fn coupon(", "pub fn principal("],
     "intro": "Amounts in the cash token's smallest unit, on the holder's whole position, in 128-bit integers, "
              "rounded down once (SCoRE corporate actions standard 4).",
     "nodes": [
         start("pub fn coupon(&self, position: u64, period: u16) -> Result<u64> {", "coupon(position, period)"),
         split("let amount = match self.basis {", "by the bond's coupon basis", [
             ("fixed per period", [step("fixed_coupon_amount(position, self.face_units, self.rate_bps, periods_per_year)",
                                        "position x face x rate / periods a year")]),
             ("30/360", [step("coupon_amount(position, self.face_units, self.rate_bps, self.accrual_basis()?, start, end)",
                              "position x face x rate x days(30/360) / 360, on the period's unadjusted dates")]),
         ]),
         check("amount.ok_or_else(|| error!(BondError::Overflow))", "the amount fits in a token amount", "Overflow"),
         step("let gross = u128::from(position).checked_mul(u128::from(face_units))?.checked_mul(u128::from(rate_bps))?;",
              "fixed per period, in bond_math: gross = position x face (cash units) x rate (basis points)",
              file=AMOUNT),
         step("u64::try_from(gross / (BPS_PER_UNIT * u128::from(periods_per_year))).ok()",
              "coupon = gross / (10,000 x periods a year), rounded down", "sample: 10 bonds x 1,000 x 10% / 2 = 500",
              file=AMOUNT),
         check("principal_amount(position, self.face_units, self.redemption_price_bps).ok_or_else",
               "principal(position) = position x face x redemption price / 10,000, rounded down", "Overflow"),
         end("return"),
     ]},
    {"id": "serviceable", "group": "Rules the instructions use", "title": "check_serviceable", "kind": "rule",
     "file": STATE, "start": "pub fn check_serviceable(&self, supply: u64)", "regions": ["pub fn check_serviceable("],
     "intro": "Called by place. Every amount the bond can owe fits in the cash token's amounts, so no budget can "
              "fail later.",
     "nodes": [
         start("pub fn check_serviceable(&self, supply: u64) -> Result<()> {", "check_serviceable(new supply)"),
         split("match basis {", "by the bond's coupon basis", [
             ("fixed per period", [check("fixed_coupon_amount(supply", "one coupon on the whole supply fits",
                                         "IssueTooLarge")]),
             ("30/360", [check("coupon_amount(supply", "each period's coupon on the whole supply fits",
                               "IssueTooLarge")]),
         ]),
         check("principal_amount(supply", "the principal on the whole supply fits", "IssueTooLarge",
               "checked when the supply grows; it only shrinks afterwards"),
         end("return"),
     ]},
    {"id": "accept_next", "group": "Rules the instructions use", "title": "Offer.accept_next", "kind": "rule",
     "file": STATE, "start": "pub fn accept_next(&mut self, quantity: u64)", "regions": ["pub fn accept_next("],
     "intro": "The tender rule. Tenders are taken in the order they were made; an oversubscribed offer gives each "
              "the pro-rata share of the running total minus what the earlier ones got.",
     "nodes": [
         start("pub fn accept_next(&mut self, quantity: u64) -> Option<u64> {", "accept_next(tendered)"),
         step("let before = self.resolved_quantity;", "before = bonds tendered by the tenders resolved so far"),
         step("let after = before.checked_add(quantity)?;", "after = before + this tender"),
         split("let accepted = if self.total <= self.max_quantity {", "is the offer oversubscribed?", [
             ("total at most the maximum", [step("quantity", "all of this tender is accepted")]),
             ("total above the maximum", [step("let share = |q: u64|", "share(q) = q x maximum / total, rounded down"),
                                          step("share(after) - share(before)", "accepted = share(after) - share(before)")]),
         ], "the shares telescope: over all tenders they add up to share(total) = the maximum, so the offer buys "
            "exactly its maximum; each tender gets its own share rounded down, or one bond more"),
         step("self.resolved_quantity = after;", "resolved_quantity = after; one more tender resolved"),
         end("return the accepted part"),
     ]},

    # ------------------------------------------------------------------------------------------------ moving value
    {"id": "pay_cash", "group": "Moving value", "title": "pay_cash", "kind": "helper", "file": SETTLE,
     "start": "fn requires_memo(account: &AccountInfo)", "regions": ["fn requires_memo(", "pub fn pay_cash("],
     "intro": "Pays from a vault: coupons, principal, buyback and tender payments.",
     "nodes": [
         start("pub fn pay_cash(c: &Cash, amount: u64, decimals: u8, memo: &str, seeds: &[&[u8]]) -> Result<()> {",
               "pay_cash(vault, holder's cash account, amount)"),
         step("if *account.owner != spl_token_2022::ID {", "requires_memo: an SPL Token account never requires a memo",
              after="fn requires_memo("),
         check("let state = StateWithExtensions::<TokenAccountState>::unpack(&data)",
               "a Token-2022 cash account is read for its memo setting", "UnsupportedCashToken"),
         split("if requires_memo(c.holder_cash)? {", "does the receiving account require a memo?", after="pub fn pay_cash(", cases=[
             ("yes", [check("let memo_program = c.memo_program.ok_or(BondError::MemoProgramMissing)?;",
                            "the Memo program was passed", "MemoProgramMissing"),
                      check("require_keys_eq!(memo_program.key(), MEMO_PROGRAM_ID, BondError::MemoProgramMissing);",
                            "it is the real SPL Memo program", "MemoProgramMissing"),
                      call("invoke(&ix, &[memo_program.clone()])?;", "Memo: \"bond_servicing: coupon e\" (or "
                                                                     "redemption, offer)")]),
             ("no", []),
         ]),
         call("token_interface::transfer_checked(",
              "cash token TransferChecked: vault to the holder's cash account, amount and decimals checked; "
              "authority = Bond account, signed with its seeds",
              "the vault's authority is the Bond account, which has no private key: only this program can move a "
              "vault's cash"),
         end("return"),
     ]},
    {"id": "burn_bonds", "group": "Moving value", "title": "burn_bonds", "kind": "helper", "file": SETTLE,
     "start": "pub fn burn_bonds<'info>(", "regions": ["pub fn burn_bonds<'info>("],
     "intro": "Retires bonds the program is paying for: redemption, buyback and tender settlement.",
     "nodes": [
         start("pub fn burn_bonds<'info>(", "burn_bonds(holder's bond account, quantity)"),
         step("let mut data = vec![PERMISSIONED_BURN_EXTENSION, PERMISSIONED_BURN_BURN_CHECKED];",
              "Token-2022 instruction 46/2: permissioned BurnChecked, quantity, 0 decimals"),
         call("invoke_signed(&burn,", "burn from the holder's bond account; the Bond account signs as burn authority "
                                      "and as permanent delegate",
              "the holder does not sign: the bonds are burned in the same transaction that pays for them"),
         end("return"),
     ]},
    {"id": "retire_quantity", "group": "Moving value", "title": "retire_quantity", "kind": "helper", "file": SETTLE,
     "start": "pub fn retire_quantity(", "regions": ["pub fn retire_quantity("],
     "intro": "After a burn: saves the histories first, so every right earned at a past record moment stays payable "
              "after the bonds are gone.",
     "nodes": [
         start("pub fn retire_quantity(", "retire_quantity(holder, bond, passed, quantity)"),
         sub("if let Some((from_event, to_event)) = holder.position.save(passed) {", "save the holder's history",
             "position"),
         event("emit!(PositionsSaved", "PositionsSaved (if anything was saved)"),
         check("holder.position.quantity = before.checked_sub(quantity).ok_or(BondError::Overflow)?;",
               "holder's quantity - quantity", "Overflow"),
         sub("bond.supply.save(passed);", "save the supply history", "position"),
         check("bond.supply.quantity = bond.supply.quantity.checked_sub(quantity).ok_or(BondError::Overflow)?;",
               "supply - quantity", "Overflow"),
         end("return"),
     ]},
]

GROUPS = ["How a call runs", "Issue", "Every transfer", "Coupons and redemption", "Buyback and tender",
          "Rules the instructions use", "Moving value"]
KIND_LABEL = {"runtime": "runtime", "instruction": "instruction", "check": "check", "helper": "function",
              "accounts": "accounts", "rule": "rule"}


def region_end(text, start):
    """End of the item (struct or function) whose first line holds `start`: its closing brace at the same indent."""
    line_start = text.rfind("\n", 0, start) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    m = re.compile(rf"^{re.escape(indent)}\}}", re.M).search(text, start)
    if not m:
        raise SystemExit(f"not building: no end for the item at {text[line_start:start + 40]!r}")
    return m.end()


def main(out_path):
    texts = {}

    def text_of(file):
        if file not in texts:
            if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", file], cwd=ROOT).returncode:
                raise SystemExit(f"not building: {file} has changes not committed")
            texts[file] = (ROOT / file).read_text()
        return texts[file]

    errors_defined = re.findall(r"^\s{4}(\w+),$", text_of(SRC + "error.rs"), re.M)
    shown, ids = set(), {c["id"] for c in CHARTS}

    # Find every box's code, in order, and check that the charts cover the code.
    for c in CHARTS:
        text = text_of(c["file"])
        if c["start"] not in text:
            raise SystemExit(f"not building: {c['id']}: start {c['start']!r} not in {c['file']}")
        pos = {c["file"]: text.index(c["start"])}
        flat = []
        for n in c["nodes"]:
            flat.append(n)
            for _, case in n.get("cases", []):
                flat += case
        for n in flat:
            if n["to"] and n["to"] not in ids:
                raise SystemExit(f"not building: {c['id']}: box calls unknown chart {n['to']}")
            for e in n["e"]:
                if e not in ANCHOR_ERRORS and e not in errors_defined:
                    raise SystemExit(f"not building: {c['id']}: {e} is neither a program nor an Anchor error")
                shown.add(e)
            if n["f"] is None:
                continue
            file = n["file"] or c["file"]
            t = text_of(file)
            if n["after"]:
                # The code runs a function written earlier in the file: the search restarts at it.
                if n["after"] not in t:
                    raise SystemExit(f"not building: {c['id']}: {n['after']!r} not in {file}")
                pos[file] = t.index(n["after"])
            at = t.find(n["f"], pos.get(file, 0))
            if at < 0:
                raise SystemExit(f"not building: {c['id']}: {n['f']!r} not found in {file} after the box before it")
            n["pos"], n["src"], n["line"] = at, file, t.count("\n", 0, at) + 1
            pos[file] = at + len(n["f"])
        boxes = sorted((n for n in flat if n.get("src") == c["file"]), key=lambda n: n["pos"])
        for anchor in c["regions"]:
            begin = text.find(anchor, text.index(c["start"]) if anchor == c["start"] else 0)
            if begin < 0:
                raise SystemExit(f"not building: {c['id']}: region {anchor!r} not in {c['file']}")
            finish = region_end(text, begin)
            # Every program error raised in the region is an exit of the box it follows.
            for m in re.finditer(r"BondError::(\w+)", text[begin:finish]):
                p = begin + m.start()
                owner = [n for n in boxes if n["pos"] <= p]
                if not owner or m[1] not in owner[-1]["e"]:
                    line = text.count("\n", 0, p) + 1
                    raise SystemExit(f"not building: {c['id']}: BondError::{m[1]} at {c['file']}:{line} is not shown")
            # Every account constraint has a box.
            for m in re.finditer(r"#\[account\((.*?)\)\]", text[begin:finish], re.S):
                if re.search(r"has_one|seeds|constraint|init|token::", m[1]):
                    a, b = begin + m.start(), begin + m.end()
                    if not any(a <= n["pos"] < b for n in boxes):
                        line = text.count("\n", 0, a) + 1
                        raise SystemExit(f"not building: {c['id']}: account constraint at {c['file']}:{line} has no box")
    missing = [e for e in errors_defined if e not in shown]
    if missing:
        raise SystemExit(f"not building: program errors in no chart: {', '.join(missing)}")

    # Source pages with line anchors.
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    out = Path(out_path)
    code_dir = out.parent / "code"

    def source_page(file):
        content = text_of(file)
        path = code_dir / f"{file}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        body = "\n".join(f'<span class="line" id="L{i}"><a href="#L{i}" aria-label="Line {i}">{i}</a><code>{escape(s)}</code></span>'
                         for i, s in enumerate(content.splitlines(), 1))
        back = os.path.relpath(out, path.parent)
        path.write_text(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(file)} · source</title><style>
:root{{color-scheme:light dark}}body{{margin:0;background:light-dark(#f4f5f2,#0f1011);color:light-dark(#0b0b0b,#eee);font:14px/1.5 system-ui}}
header{{padding:20px;position:sticky;top:0;background:light-dark(#fcfcfb,#1a1a19);border-bottom:1px solid #7775}}
a{{color:light-dark(#1c5cab,#6da7ec)}}h1{{font-size:18px;margin:8px 0}}p{{margin:4px 0;overflow-wrap:anywhere}}pre{{margin:0;padding:16px;overflow:auto}}
.line{{display:flex;scroll-margin-top:160px;min-height:21px}}.line:target{{background:#1c5cab22}}.line>a{{width:52px;flex:none;text-align:right;padding-right:16px;text-decoration:none;color:light-dark(#6f6d68,#aaa)}}code{{white-space:pre;font:13px/21px monospace}}
</style><header><a href="{escape(back)}">← Program logic</a><h1>{escape(file)}</h1>
<p>Source at revision {revision[:12]}, the source of the program deployed on devnet. SHA-256 of this file {digest}</p></header><pre>{body}</pre></html>''')
        return f"code/{file}.html"

    pages = {f: source_page(f) for f in sorted({n["src"] for c in CHARTS for n in c["nodes"] + [s for n in c["nodes"] for _, case in n.get("cases", []) for s in case] if n.get("src")})}

    def code_of(n):
        """The statement the box stands for, as written in the source."""
        t = texts[n["src"]]
        a = t.rfind("\n", 0, n["pos"]) + 1
        if n["k"] == "guard":
            attr = t.rfind("#[account(", 0, n["pos"] + 1)
            if attr >= 0 and ")]" not in t[attr:n["pos"]] and t.rfind("\n}", attr, n["pos"]) < 0:
                a = t.rfind("\n", 0, attr) + 1
        lines, depth = [], 0
        for line in t[a:].split("\n"):
            lines.append(line)
            depth += sum(line.count(o) for o in "([{") - sum(line.count(o) for o in ")]}")
            s = line.strip()
            if n["k"] == "guard" and re.match(r"pub \w+:", s):
                break
            if n["k"] == "guard":
                continue
            if n["k"] == "check" and n["e"] and not any(e in "\n".join(lines) for e in n["e"]) and any(
                    f"BondError::{e}" in t for e in n["e"]) and len(lines) < 9 and depth > 0:
                continue
            if depth <= 0 or s.endswith("{") or n["k"] in ("split", "leave", "start", "phase"):
                break
            if len(lines) >= 9:
                break
        indent = min(len(x) - len(x.lstrip()) for x in lines if x.strip())
        shown_lines = [x[indent:] for x in lines]
        if len(shown_lines) > 8:
            shown_lines = shown_lines[:6] + ["…"] + shown_lines[-1:]
        return "\n".join(shown_lines)

    title_of = {c["id"]: c["title"] for c in CHARTS}

    def exit_html(n):
        if n["e"]:
            label = "already exists" if n["e"] == ["account in use"] else "fails"
            errs = "".join(f'<code>{esc("System: account already in use" if e == "account in use" else e)}</code>'
                           for e in n["e"])
            return f'<div class="pf-x err"><span class="pf-arrow">{label}</span>{errs}</div>'
        if n["x"]:
            return f'<div class="pf-x out"><span class="pf-arrow">exit</span><span>{esc(n["x"])}</span></div>'
        return '<div class="pf-x"></div>'

    def box_html(n, inner=False):
        code = f'<pre class="pf-code">{esc(code_of(n))}</pre>' if n.get("src") else ""
        line = (f'<a class="pf-line" href="{esc(pages[n["src"]])}#L{n["line"]}" title="{esc(n["src"])}">'
                f'{esc(n["src"].rsplit("/", 1)[-1])}:{n["line"]}</a>') if n.get("src") else ""
        to = (f'<a class="pf-to" href="#{esc(n["to"])}">see {esc(title_of[n["to"]])}</a>') if n["to"] else ""
        cases = ""
        if n["k"] == "split":
            cols = []
            for label, case in n["cases"]:
                rows = "".join(box_html(s, inner=True) for s in case) or '<p class="pf-none">nothing else</p>'
                cols.append(f'<div class="pf-case"><b>{esc(label)}</b><ol class="pf-flow inner">{rows}</ol></div>')
            cases = f'<div class="pf-cases">{"".join(cols)}</div>'
        shape = (f'<div class="pf-n {n["k"]}"><div class="pf-top"><p class="pf-d">{esc(n["d"])}</p>{line}</div>'
                 f'{code}{to}{cases}</div>')
        why = f'<p class="pf-w">{esc(n["w"])}</p>' if n["w"] and not inner else ('<p class="pf-w"></p>' if not inner else "")
        if inner:
            return f'<li class="pf-row in"><div class="pf-c">{shape}</div>{exit_html(n)}{"<p class=pf-w>" + esc(n["w"]) + "</p>" if n["w"] else ""}</li>'
        return f'<li class="pf-row"><div class="pf-c">{shape}</div>{exit_html(n)}{why}</li>'

    sections, jumps = [], []
    for g in GROUPS:
        charts = [c for c in CHARTS if c["group"] == g]
        jumps.append(f'<span class="pf-jg">{esc(g)}</span>' + "".join(
            f'<a class="bs-btn" href="#{c["id"]}">{esc(c["title"])}</a>' for c in charts))
        cards = []
        for c in charts:
            who = f'<span class="who">sent by: <b>{esc(c["who"])}</b></span>' if c.get("who") else ""
            rows = []
            for n in c["nodes"]:
                if n["k"] == "phase":
                    link = (f'<a class="pf-line" href="{esc(pages[n["src"]])}#L{n["line"]}">{esc(n["src"].rsplit("/", 1)[-1])}:'
                            f'{n["line"]}</a>')
                    rows.append(f'<li class="pf-phase"><span>{esc(n["d"])}</span><code>{esc(n["f"].rstrip("(<"))}</code>{link}</li>')
                else:
                    rows.append(box_html(n))
            cards.append(
                f'<section class="pf-chart" id="{esc(c["id"])}"><div class="fl-head"><span class="bs-mark kept">'
                f'{esc(KIND_LABEL[c["kind"]])}</span><h3 class="bs-h2">{esc(c["title"])}</h3>{who}</div>'
                f'<p class="fl-plain">{esc(c["intro"])}</p>'
                f'<div class="pf-head" aria-hidden="true"><span>what the code does, in order</span><span>if not</span>'
                f'<span>why</span></div><ol class="pf-flow">{"".join(rows)}</ol></section>')
        sections.append(f'<section class="pf-group"><h2 class="bs-h2 pf-gh">{esc(g)}</h2>{"".join(cards)}</section>')

    files = len(pages)
    eyebrow = f"bond_servicing · {len(CHARTS)} charts from {files} source files · revision {revision[:7]}"
    foot = (f'<p>Every box\'s code is read from the source at revision {revision[:12]}, and each box links to its line. '
            f'The builder refuses to write this page if a box\'s code is not found after the box before it, if a '
            f'program error raised inside a charted function is not shown at its box, if an account constraint of a '
            f'charted accounts struct has no box, or if one of the program\'s {len(errors_defined)} errors appears in '
            f'no chart. The program deployed on devnet (2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf) is a build of '
            f'this source: its bytes equal the local build, sha256 327271f3b6f3c5e2… (compared on 2026-10-11).</p>'
            f'<p>Built by <code>tools/demo/build_program.py</code>.</p>')
    template = (ROOT / "tools/demo/program_template.html").read_text()
    html = fill(template, {"EYEBROW": esc(eyebrow), "JUMPS": "".join(f'<div class="pf-jrow">{j}</div>' for j in jumps),
                           "CHARTS": "".join(sections), "FOOT": foot})
    out.write_text(html)
    boxes = sum(1 for c in CHARTS for n in c["nodes"] if n["k"] != "phase")
    print(f"wrote {out_path}: {len(CHARTS)} charts, {boxes} boxes, {len(shown & set(errors_defined))} of "
          f"{len(errors_defined)} program errors shown, {files} source pages")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
