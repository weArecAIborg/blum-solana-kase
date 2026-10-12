"""Build the program flow page (docs/demo/flow.html): every step of one bond's life as Solana executed it.

Each step is a real transaction of the 8-holder devnet run (docs/demo/data/devnet-full8.json),
fetched from public RPC (tools/audit keeps confirmed replies on disk) and read four ways:
  - the call chain from the runtime's log: which program ran which instruction, at which depth (a program calling
    another), the compute units it used and the events it logged;
  - the accounts of each instruction: bond_servicing's named by the program's interface (target/idl), the token
    programs' by their parsed form; every account labelled by what the chain stores in it (account type, holder,
    event, offer, token account owner);
  - token balances before and after;
  - the program's events.
What each instruction checks is written here from the source (programs/bond_servicing/src), with the error each check
returns. The page is not written if the log and the transaction disagree on the instructions executed, or if an
account the steps use cannot be labelled from the chain.
Design: Blum (docs/demo/blum/).

Usage: .venv/bin/python3 tools/demo/build_flow.py docs/demo/data/devnet-full8.json https://api.devnet.solana.com \\
           docs/demo/flow.html
"""

import base64
import hashlib
import json
import re
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
sys.path.insert(0, str(ROOT / "tools/demo"))
import audit as A  # noqa: E402
from build_timeline import EVENTS, SIZE, UNPACK, esc, explorer, fill  # noqa: E402

# Events the timeline does not read, with their fields in order (programs/bond_servicing/src/events.rs).
EVENTS = {**EVENTS,
          "BondCreated": [("bond", "pk"), ("mint", "pk"), ("registrar", "pk"), ("periods", "u16")],
          "HolderRegistered": [("bond", "pk"), ("owner", "pk")],
          "EventFunded": [("bond", "pk"), ("event", "u16"), ("vault_balance", "u64"), ("budget", "u64")],
          "OfferFunded": [("bond", "pk"), ("offer", "u16"), ("vault_balance", "u64"), ("budget", "u64")]}
EVENT_OF = {hashlib.sha256(f"event:{name}".encode()).digest()[:8]: name for name in EVENTS}
ACCOUNT_OF = {hashlib.sha256(f"account:{name}".encode()).digest()[:8]: name
              for name in ("Bond", "HolderRecord", "Event", "Offer", "Tender")}

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
PROGRAMS = {
    TOKEN_2022: "Token-2022",
    SPL_TOKEN: "SPL Token",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token Account program",
    "11111111111111111111111111111111": "System program",
    "ComputeBudget111111111111111111111111111111": "Compute Budget program",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo program",
}

# The steps, in the bond's order. `pick` chooses the run's first transaction of that kind (after `after`, a record
# moment index, when given). `who` is who may send it by the program's rules; `checks` are the program's checks in the
# order the source makes them, each with the error it returns.
STEPS = [
    {"id": "create", "stage": "Issue", "title": "create_bond", "pick": ["CreateBond"], "who": "Registrar",
     "src": "instructions/create_bond.rs",
     "plain": "The registrar creates the bond in one transaction: the bond token, the bond account that holds the terms "
              "and the time table, and the list of accounts the transfer hook needs. The program makes the bond "
              "account the token's mint authority, permanent delegate and burn authority, sets itself as the "
              "transfer hook, and gives the token no freeze authority.",
     "checks": [("InvalidTerms", "face value, issue size, rate, redemption price, period and time compression above "
                 "zero; record and payment times inside a day; payments a year x months a period = 12"),
                ("InvalidSchedule", "payment and record days match the schedule computed from issue and maturity: "
                 "business days, payment at most 14 days from the period end, record moments in order and not after "
                 "payment, at most 40 periods"),
                ("UnsupportedCashToken", "the cash token is SPL Token, or Token-2022 without a transfer fee, transfer "
                 "hook, non-transferability or permanent delegate"),
                ("account exists", "the bond account and the hook list are new accounts at their seeds")]},
    {"id": "place", "stage": "Issue", "title": "register_holder + place", "pick": ["RegisterHolder", "Place"],
     "who": "Registrar", "src": "instructions/place.rs",
     "plain": "The registrar admits a wallet (KYC is simulated) and places bonds with it: a holder record is created "
              "for the wallet, its bond account is opened, and the program mints the bonds into it, signing as mint "
              "authority. The holder record and the bond's supply count the new bonds.",
     "checks": [("ConstraintHasOne", "only the bond's registrar registers and places"),
                ("account exists", "one holder record per wallet and bond, at [\"holder\", bond token, wallet]"),
                ("PlacementClosed", "placement only before the first record moment"),
                ("ExceedsIssueSize", "supply + new bonds within the issue size from the terms"),
                ("IssueTooLarge", "every coupon and the principal of the new supply fit in a token amount")]},
    {"id": "offer", "stage": "Offer", "title": "create_offer", "pick": ["CreateOffer"], "who": "Registrar",
     "src": "instructions/offer.rs",
     "plain": "The registrar opens a buyback (first come) with a price, a maximum and a window. The program creates "
              "the offer and a vault for its cash, a cash account whose authority is the bond account.",
     "checks": [("ConstraintHasOne", "only the bond's registrar opens offers"),
                ("InvalidOffer", "price and maximum above zero; the window opens before it closes and closes by the "
                 "final record moment; price x maximum fits")]},
    {"id": "offer-cash", "stage": "Offer", "title": "the bank pays into the offer's vault", "pick": [], "nth": 0,
     "who": "Bank", "src": None,
     "plain": "Not a bond_servicing instruction. The bank, the cash token's mint authority, credits the vault: an SPL "
              "Token MintTo. In real markets this is the issuer's payment to its paying agent; here the bank stands "
              "for it.",
     "checks": [("owner does not match", "SPL Token: only the cash token's mint authority mints")]},
    {"id": "offer-funded", "stage": "Offer", "title": "confirm_offer_funding", "pick": ["ConfirmOfferFunding"],
     "who": "Anyone", "src": "instructions/offer.rs",
     "plain": "Anyone confirms that the vault holds price x maximum. Only then may holders sell or tender.",
     "checks": [("AlreadyFunded", "not confirmed before"),
                ("InsufficientFunding", "the vault holds at least price x maximum")]},
    {"id": "record", "stage": "Record", "title": "record moment: no transaction", "pick": None, "who": "Nobody",
     "src": "state.rs"},
    {"id": "trade", "stage": "Trade", "title": "transfer (Token-2022) -> transfer_hook", "pick": ["TransferHook"],
     "after": 0, "who": "Holder", "src": "instructions/transfer_hook.rs",
     "plain": "A holder sends bonds with Token-2022's TransferChecked. Token-2022 moves the balances, then calls "
              "bond_servicing's transfer hook with the accounts listed at bond creation. The hook keeps each holder's "
              "quantity for every record moment passed since its last change (PositionsSaved), then records the new "
              "quantities. This is how the register knows who held what at 09:00 without anyone taking a list.",
     "checks": [("NotTransferring", "runs only inside a real Token-2022 transfer"),
                ("NotAssociatedTokenAccount", "both sides are the holders' associated bond accounts"),
                ("AccountOwnedByWrongProgram", "both wallets have holder records (are registered)"),
                ("TransfersClosedAtMaturity", "before the final record moment"),
                ("PositionMismatch", "each holder record agrees with its balance before the transfer"),
                ("BondsLocked", "the seller keeps at least its tendered bonds")]},
    {"id": "sell", "stage": "Offer", "title": "sell_to_offer", "pick": ["SellToOffer"], "who": "Holder",
     "src": "instructions/offer.rs",
     "plain": "A holder sells bonds to the buyback. In one transaction the program burns them (Token-2022 permissioned "
              "burn, the bond account signing as burn authority), pays price x quantity from the offer's vault (SPL "
              "Token TransferChecked, the bond account signing as the vault's authority), and takes them off the "
              "holder's record and the supply, saving both histories first.",
     "checks": [("ConstraintRaw", "the signer is the holder whose bonds are sold"),
                ("WrongOfferKind", "a first-come buyback"),
                ("OfferNotFunded", "the offer is confirmed funded"),
                ("OfferNotOpen", "inside the window"),
                ("ExceedsOffer", "quantity above zero and the offer's total within its maximum"),
                ("InsufficientUnlocked", "not more than the holder's untendered bonds"),
                ("PositionMismatch", "the holder record agrees with its bond balance")]},
    {"id": "open", "stage": "Fund", "title": "open_event (coupon)", "pick": ["OpenEvent"], "who": "Anyone",
     "src": "instructions/open_event.rs",
     "plain": "After a record moment anyone opens its coupon. The program reads the supply at that moment from the "
              "bond's supply history, fixes the budget (coupon on the whole supply, rounded down once) and creates "
              "the event and its own vault. No holder is counted.",
     "checks": [("RecordMomentNotReached", "the coupon's record moment has passed"),
                ("account exists", "one event per index, at [\"event\", bond, index], with its vault at "
                 "[\"vault\", event]")]},
    {"id": "event-cash", "stage": "Fund", "title": "the bank pays into the coupon's vault", "pick": [], "nth": 2,
     "who": "Bank", "src": None,
     "plain": "Not a bond_servicing instruction: the bank credits the coupon's vault with the budget (SPL Token "
              "MintTo). In real markets, the issuer pays the coupon to the paying agent.",
     "checks": [("owner does not match", "SPL Token: only the cash token's mint authority mints")]},
    {"id": "funded", "stage": "Fund", "title": "confirm_funding", "pick": ["ConfirmFunding"], "who": "Anyone",
     "src": "instructions/confirm_funding.rs",
     "plain": "Anyone confirms that the vault covers the budget. Payment starts only then, so no holder is paid from "
              "an event that cannot pay all of them.",
     "checks": [("AlreadyFunded", "not confirmed before"),
                ("InsufficientFunding", "the vault holds at least the budget")]},
    {"id": "pay", "stage": "Pay", "title": "pay_coupon", "pick": ["PayCoupon"], "who": "Anyone",
     "src": "instructions/pay_coupon.rs",
     "plain": "Anyone pays a holder its coupon. The amount comes from the holder's own record (its quantity at the "
              "record moment) and the terms; the cash goes from the event's vault to the holder's cash account "
              "(created if missing), and the holder's paid bit is set in the same transaction. One transaction can "
              "pay several holders, one instruction each.",
     "checks": [("WrongEventKind", "a coupon event"),
                ("NotFunded", "the event is confirmed funded"),
                ("PaymentNotOpen", "bond time has reached the payment time"),
                ("AlreadyPaid", "the holder's paid bit for this coupon is not set"),
                ("NothingToPay", "the position at the record moment gives a coupon above zero"),
                ("ConstraintHasOne", "the holder record belongs to this wallet and bond; the vault is the event's")]},
    {"id": "tender", "stage": "Offer", "title": "change_tender", "pick": ["ChangeTender"], "who": "Holder",
     "src": "instructions/offer.rs",
     "plain": "A holder tenders bonds into the tender offer. The bonds do not move: the holder record locks them, so "
              "a coupon whose record moment falls in the window is still paid on them. The tender is numbered in the "
              "order it was made.",
     "checks": [("ConstraintHasOne", "the signer owns the holder record"),
                ("WrongOfferKind", "a pro-rata tender offer"),
                ("OfferNotFunded", "the offer is confirmed funded"),
                ("OfferNotOpen", "inside the window"),
                ("ExceedsOffer", "quantity above zero; a withdrawal not more than tendered"),
                ("InsufficientUnlocked", "not more than the holder's untendered bonds")]},
    {"id": "resolve", "stage": "Offer", "title": "resolve_tender", "pick": ["ResolveTender"], "who": "Anyone",
     "src": "instructions/offer.rs",
     "plain": "After the window anyone resolves the tenders, in the order they were made. If the offer is "
              "oversubscribed, each takes the pro-rata share of the running total, rounded down, minus what the "
              "earlier ones took, so the offer buys exactly its maximum. The rejected part is unlocked. No cash or "
              "token account is involved.",
     "checks": [("WrongOfferKind", "a pro-rata tender offer"),
                ("OfferStillOpen", "the window has closed"),
                ("TenderOutOfOrder", "this tender's number is the next to resolve")]},
    {"id": "settle", "stage": "Offer", "title": "settle_tender", "pick": ["SettleTender"], "who": "Anyone",
     "src": "instructions/offer.rs",
     "plain": "Anyone settles a resolved tender: the accepted bonds are burned and paid for in one transaction, as in "
              "the buyback, and released from the lock.",
     "checks": [("WrongOfferKind", "a pro-rata tender offer"),
                ("OfferStillOpen", "the window has closed"),
                ("AlreadySettled", "not settled before"),
                ("PositionMismatch", "the holder record agrees with its bond balance")]},
    {"id": "redeem", "stage": "Redeem", "title": "redeem_holder", "pick": ["RedeemHolder"], "who": "Anyone",
     "src": "instructions/redeem_holder.rs",
     "plain": "At maturity, after the redemption is opened and funded like a coupon, anyone redeems a holder: the "
              "program burns its bonds and pays the principal from the redemption vault in one transaction, so no "
              "bond is burned unpaid and no principal is paid for a live bond.",
     "checks": [("WrongEventKind", "the redemption event"),
                ("NotFunded", "the redemption is confirmed funded"),
                ("PaymentNotOpen", "bond time has reached the payment time"),
                ("PositionMismatch", "the holder record agrees with its bond balance"),
                ("NothingToPay", "the holder has untendered bonds")]},
]

# What each kind of entity is and who controls it.
KINDS = {
    "wallet": ("Wallets", "they sign transactions"),
    "program": ("Programs", "they run instructions"),
    "state": ("bond_servicing accounts", "only the program writes them; addresses derived from seeds"),
    "token": ("Tokens", "mints and token accounts"),
}


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def flat(accounts):
    out = []
    for a in accounts:
        out += flat(a["accounts"]) if "accounts" in a else [a]
    return out


def decode_event(line):
    raw = base64.b64decode(line[len("Program data: "):])
    name = EVENT_OF.get(raw[:8])
    if not name:
        return None
    at, event = 8, {"name": name}
    for field, kind in EVENTS[name]:
        chunk = raw[at:at + SIZE[kind]]
        at += SIZE[kind]
        event[field] = A.b58encode(chunk) if kind == "pk" else struct.unpack(UNPACK[kind], chunk)[0]
    return event


def executed(tx):
    """Every instruction in the order the runtime ran it: each top-level instruction, then the ones it invoked."""
    inner = {i["index"]: i["instructions"] for i in tx["meta"]["innerInstructions"] or []}
    out = []
    for i, ix in enumerate(tx["transaction"]["message"]["instructions"]):
        out += [ix] + inner.get(i, [])
    return out


def call_tree(tx):
    """The runtime's log as a list of invocations (depth, program, instruction, compute units, events)."""
    ixs, nodes, stack = executed(tx), [], []
    for line in tx["meta"]["logMessages"]:
        if m := re.fullmatch(r"Program (\w+) invoke \[(\d+)\]", line):
            if len(nodes) == len(ixs):
                raise SystemExit("not building: the log has more invocations than the transaction has instructions")
            ix = ixs[len(nodes)]
            if ix.get("programId") != m[1]:
                raise SystemExit(f"not building: log and transaction disagree at invocation {len(nodes)}")
            node = {"program": m[1], "depth": int(m[2]), "ix": ix, "name": (ix.get("parsed") or {}).get("type") if
                    isinstance(ix.get("parsed"), dict) else None, "cu": None, "events": []}
            nodes.append(node)
            stack.append(node)
        elif m := re.fullmatch(r"Program (\w+) consumed (\d+) of \d+ compute units", line):
            stack[-1]["cu"] = int(m[2])
        elif re.fullmatch(r"Program (\w+) (success|failed.*)", line):
            stack.pop()
        elif line.startswith("Program log: Instruction: "):
            stack[-1]["name"] = line[len("Program log: Instruction: "):]
        elif line.startswith("Program data: ") and (event := decode_event(line)):
            stack[-1]["events"].append(event)
    if len(nodes) != len(ixs):
        raise SystemExit("not building: the log has fewer invocations than the transaction has instructions")
    return nodes


def main(data_path, rpc_url, out_path):
    D = json.loads(Path(data_path).read_text())
    scn = json.loads((ROOT / D["scenarioFile"]).read_text())
    idl = json.loads((ROOT / "target/idl/bond_servicing.json").read_text())
    program = idl["address"]
    idl_ix = {ix["name"]: flat(ix["accounts"]) for ix in idl["instructions"]}
    T = scn["terms"]
    unit = 10 ** T["cashDecimals"]
    rpc = A.Rpc(rpc_url)
    get = lambda e: rpc.call("getTransaction", [e["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,  # noqa: E731
                                                                 "commitment": "confirmed"}], e["slot"])
    if not D["audit"]["passed"]:
        raise SystemExit("not building: the run's audit did not pass")
    origin = datetime.fromisoformat(scn["bondTimeAtCreation"])
    bond_time = lambda t: origin + timedelta(seconds=(t - D["creationTime"]) * 86_400 / scn["chainSecondsPerBondDay"])  # noqa: E731

    # Pick each step's transaction.
    servicing = sorted(D["servicing"], key=lambda e: (e["slot"], e["t"]))
    picked = {}
    for s in STEPS:
        if s["pick"] is None:
            continue
        after = D["records"][s["after"]]["chainTime"] if "after" in s else None
        found = [e for e in servicing if e["instructions"] == s["pick"] and (after is None or e["t"] >= after)]
        if s["pick"] == []:
            found = [e for e in found if e["funding"]]
        if not found:
            raise SystemExit(f"not building: no transaction for step {s['id']}")
        picked[s["id"]] = found[s.get("nth", 0)]
    txs = {k: get(e) for k, e in picked.items()}
    trees = {k: call_tree(tx) for k, tx in txs.items()}

    # Name every account the steps use, from what the chain stores in it.
    bond_mint, bond_pda = D["bondMint"], D["bondAuthority"]
    holders = {h["address"]: h["name"] for h in D["holders"]}
    create = next(n for n in trees["create"] if n["program"] == program)
    roles = dict(zip([a["name"] for a in idl_ix["create_bond"]], create["ix"]["accounts"]))
    registrar, cash_mint, hook_list = roles["registrar"], roles["cash_mint"], roles["extra_account_meta_list"]
    keys = sorted({k["pubkey"] for tx in txs.values() for k in tx["transaction"]["message"]["accountKeys"]})
    info = {}
    for i in range(0, len(keys), 100):
        values = rpc.call("getMultipleAccounts", [keys[i:i + 100], {"encoding": "jsonParsed"}])["value"]
        info.update(zip(keys[i:i + 100], values))
    bank = info[cash_mint]["data"]["parsed"]["info"]["mintAuthority"]
    label, kind, group = {}, {}, {}
    for key, name in PROGRAMS.items():
        label[key], kind[key], group[key] = name, "program", name
    label[program], kind[program], group[program] = "bond_servicing", "program", "bond_servicing"
    for key, name in holders.items():
        label[key], kind[key], group[key] = name, "wallet", "Holders"
    label[registrar], kind[registrar], group[registrar] = "Registrar", "wallet", "Registrar"
    label[bank], kind[bank], group[bank] = "Bank", "wallet", "Bank"
    label[bond_mint], label[cash_mint] = "Bond token (mint)", "Cash token (mint)"
    kind[bond_mint] = kind[cash_mint] = "token"
    group[bond_mint], group[cash_mint] = "Bond token (mint)", "Cash token (mint)"
    label[hook_list], kind[hook_list], group[hook_list] = "Hook account list", "state", "Hook account list"
    vaults = {}
    for key, acct in info.items():
        if not acct or acct["owner"] != program or key in label:
            continue
        raw = base64.b64decode(acct["data"][0])
        name = ACCOUNT_OF.get(raw[:8])
        if name == "Bond":
            label[key], group[key] = "Bond account", "Bond account"
        elif name == "HolderRecord":
            label[key], group[key] = f"Holder record · {holders[A.b58encode(raw[40:72])]}", "Holder records"
        elif name == "Event":
            index, redemption = struct.unpack("<H", raw[40:42])[0], raw[42] == 1
            label[key], group[key] = f"Event {index} · {'redemption' if redemption else 'coupon'}", "Events"
            vaults[A.b58encode(raw[75:107])] = f"Vault · {'redemption' if redemption else f'coupon {index}'}"
        elif name == "Offer":
            index, pro_rata = struct.unpack("<H", raw[40:42])[0], raw[42] == 1
            label[key], group[key] = f"Offer {index} · {'tender' if pro_rata else 'buyback'}", "Offers"
            vaults[A.b58encode(raw[83:115])] = f"Vault · offer {index} ({'tender' if pro_rata else 'buyback'})"
        elif name == "Tender":
            label[key], group[key] = f"Tender · {holders[A.b58encode(raw[40:72])]}", "Tenders"
        else:
            raise SystemExit(f"not building: unknown bond_servicing account {key}")
        kind[key] = "state"
    if bond_pda not in label:
        raise SystemExit("not building: the bond account is not among the steps' accounts")
    for key, acct in info.items():
        if key in label or not acct or acct["owner"] not in (TOKEN_2022, SPL_TOKEN):
            continue
        parsed = acct["data"]["parsed"]
        if parsed["type"] != "account":
            raise SystemExit(f"not building: unexpected token account type {parsed['type']} at {key}")
        owner, mint = parsed["info"]["owner"], parsed["info"]["mint"]
        if owner == bond_pda:
            if key not in vaults:
                raise SystemExit(f"not building: a cash account of the bond account is no event's or offer's vault: {key}")
            label[key], group[key] = vaults[key], "Vaults"
        else:
            label[key] = f"{label[owner]} · {'bonds' if mint == bond_mint else 'cash'}"
            group[key] = "Bond accounts" if mint == bond_mint else "Cash accounts"
        kind[key] = "token"
    for tx in txs.values():
        for k in tx["transaction"]["message"]["accountKeys"]:
            if k["pubkey"] not in label:
                if k["pubkey"].startswith("Sysvar"):
                    label[k["pubkey"]], kind[k["pubkey"]], group[k["pubkey"]] = k["pubkey"].split("1")[0], "program", "Sysvar"
                    continue
                if k["signer"]:
                    # A fresh keypair that signs once: the new bond mint's own key (create_bond), or a wallet.
                    raise SystemExit(f"not building: unlabelled signer {k['pubkey']}")
                raise SystemExit(f"not building: unlabelled account {k['pubkey']}")

    def chip(key, role=None, flags=""):
        text = f"{role}: " if role else ""
        return (f'<a class="fl-chip {kind.get(key, "")}" href="{esc(explorer("address", key))}" target="_blank" rel="noopener" '
                f'title="{esc(key)}">{esc(text)}<b>{esc(label[key])}</b>{flags}</a>')

    def money(units):
        return f"{units / unit:,.2f}"

    def event_text(e):
        who = lambda k: label.get(k, k[:6])  # noqa: E731
        n = e["name"]
        return {
            "BondCreated": lambda: f"BondCreated: {e['periods']} coupon periods",
            "HolderRegistered": lambda: f"HolderRegistered: {who(e['owner'])}",
            "BondsPlaced": lambda: f"BondsPlaced: {e['amount']} bonds to {who(e['owner'])}",
            "PositionsSaved": lambda: (f"PositionsSaved: {who(e['owner'])} held {e['quantity']} at record moment"
                                       + (f"s {e['from_event']}-{e['to_event'] - 1}" if e['to_event'] - e['from_event'] > 1
                                          else f" {e['from_event']}")),
            "EventOpened": lambda: f"EventOpened: event {e['event']}, supply at the record moment {e['supply_at_record']}, "
                                   f"budget {money(e['budget'])}",
            "EventFunded": lambda: f"EventFunded: event {e['event']}, vault {money(e['vault_balance'])} of budget {money(e['budget'])}",
            "CouponPaid": lambda: f"CouponPaid: {who(e['owner'])}, coupon {e['event']}, on {e['position']} bonds, {money(e['amount'])}",
            "OfferCreated": lambda: f"OfferCreated: offer {e['offer']}, {'tender' if e['pro_rata'] else 'buyback'}, "
                                    f"up to {e['max_quantity']} at {money(e['price_units'])}",
            "OfferFunded": lambda: f"OfferFunded: offer {e['offer']}, vault {money(e['vault_balance'])} of {money(e['budget'])}",
            "TenderChanged": lambda: f"TenderChanged: {who(e['owner'])} tenders {e['tendered']}; offer total {e['offer_total']}",
            "TenderResolved": lambda: f"TenderResolved: {who(e['owner'])}, {e['accepted']} accepted, {e['rejected']} released",
            "BondsBoughtBack": lambda: f"BondsBoughtBack: {who(e['owner'])}, {e['burned']} burned, {money(e['paid'])} paid",
            "BondsRedeemed": lambda: f"BondsRedeemed: {who(e['owner'])}, {e['burned']} burned, {money(e['principal'])} principal",
        }[n]()

    def accounts_of(node):
        """The accounts of one invocation, each with the role it plays in that instruction."""
        ix = node["ix"]
        if node["program"] == program:
            names = idl_ix[snake(node["name"])]
            out = []
            for a, key in zip(names, ix["accounts"]):
                if a.get("optional") and key == program:
                    continue  # an optional account left empty (Anchor passes the program's id)
                flags = (' <i class="s">signs</i>' if a.get("signer") else "") + (' <i class="w">writes</i>' if a.get("writable") else "")
                out.append(chip(key, a["name"], flags))
            return out
        parsed = ix.get("parsed")
        if isinstance(parsed, dict):
            out = []
            for role, value in parsed.get("info", {}).items():
                if isinstance(value, str) and value in label:
                    out.append(chip(value, role))
                elif role == "tokenAmount" and isinstance(value, dict):
                    out.append(f'<span class="fl-chip amt">amount: <b>{esc(value["uiAmountString"])}</b></span>')
                elif role == "amount":
                    decimals = 0 if parsed["info"]["mint"] == bond_mint else T["cashDecimals"]
                    out.append(f'<span class="fl-chip amt">amount: <b>{esc(f"{int(value) / 10 ** decimals:,.{decimals}f}")}</b></span>')
            return out
        return [chip(k) for k in ix.get("accounts", []) if k in label]

    def tree_html(nodes):
        rows = []
        for n in nodes:
            prog = "bond_servicing" if n["program"] == program else PROGRAMS.get(n["program"], n["program"])
            if n["program"] == "ComputeBudget111111111111111111111111111111":
                continue
            cls = "ours" if n["program"] == program else ""
            events = "".join(f'<li>{esc(event_text(e))}</li>' for e in n["events"])
            cu = f'<span class="cu">{n["cu"]:,} CU</span>' if n["cu"] is not None else ""
            row = (f'<li class="fl-node {cls}" style="--d:{n["depth"] - 1}"><div class="fl-nh"><span class="dep">{n["depth"]}</span>'
                   f'<b>{esc(prog)}</b><span class="ixn">{esc(n["name"] or "")}</span>{cu}</div>'
                   f'<div class="fl-acc">{"".join(accounts_of(n))}</div>')
            if events:
                row += f'<ul class="fl-ev">{events}</ul>'
            rows.append(row + "</li>")
        return f'<ol class="fl-tree">{"".join(rows)}</ol>'

    def balances_html(tx):
        keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
        rows = {}
        for side in ("pre", "post"):
            for b in tx["meta"][f"{side}TokenBalances"]:
                r = rows.setdefault(keys[b["accountIndex"]], {"mint": b["mint"], "pre": 0, "post": 0})
                r[side] = int(b["uiTokenAmount"]["amount"])
        out = []
        for key, r in rows.items():
            if r["pre"] == r["post"]:
                continue
            show = (lambda v: f"{v:,}") if r["mint"] == bond_mint else money
            d = r["post"] - r["pre"]
            out.append(f'<tr><td>{chip(key)}</td><td class="r">{show(r["pre"])}</td><td class="r">{show(r["post"])}</td>'
                       f'<td class="r {"up" if d > 0 else "down"}">{"+" if d > 0 else "-"}{show(abs(d))}</td></tr>')
        if not out:
            return '<p class="fl-none">No token balance changed: only program accounts were written.</p>'
        return ('<table class="bs-ledger fl-bal"><thead><tr><th>Account</th><th class="r">Before</th><th class="r">After</th>'
                f'<th class="r">Change</th></tr></thead><tbody>{"".join(out)}</tbody></table>')

    def signers(tx):
        return [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k["signer"]]

    def first_of_batch(nodes):
        """A transaction that repeats the same instructions for several holders: the first holder's instructions with
        what they invoked, a note naming what ran for the other holders, the first holder and how many more."""
        groups = []
        for n in nodes:
            if n["depth"] == 1:
                groups.append([n])
            else:
                groups[-1].append(n)
        ours = [i for i, g in enumerate(groups) if g[0]["program"] == program]
        if not ours:
            return nodes, "", None, 0
        first = groups[ours[0]][0]["name"]
        starts = [i for i in ours if groups[i][0]["name"] == first]
        if len(starts) < 2:
            return nodes, "", None, 0

        def owner(span):
            for g in span:
                for n in g:
                    for e in n["events"]:
                        if "owner" in e:
                            return label[e["owner"]]
            raise SystemExit("not building: a repeated instruction logged no holder")

        bounds = starts + [len(groups)]
        spans = [groups[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
        before = groups[:starts[0]]

        def names(span):
            return " + ".join(dict.fromkeys(g[0]["name"] for g in span if g[0]["program"] == program))

        # Holders grouped by what the transaction ran for them: a holder may be registered without a placement.
        ran = {}
        for span in spans[1:]:
            ran.setdefault(names(span), []).append(owner(span))
        others = "; ".join(f'{n} for {", ".join(hs)}' for n, hs in ran.items())
        note = (f'<p class="fl-note">The same transaction also ran {esc(others)}; only {esc(owner(spans[0]))}\'s '
                f'{esc(names(spans[0]))} is drawn. Balances are the whole transaction\'s.</p>')
        return [n for g in before + spans[0] for n in g], note, owner(spans[0]), len(spans) - 1

    src_base = "audit/source/programs/bond_servicing/src/"

    def src_link(path):
        if path is None:
            return ""
        page = f"{src_base}{path}.html"
        if (ROOT / "docs/demo" / page).exists():
            return f'<a class="bs-proof" href="{esc(page)}">source: {esc(path)}</a>'
        return f'<span class="fl-src">source: programs/bond_servicing/src/{esc(path)}</span>'

    # The record moment: what the first transfer after it saved.
    trade_saved = [e for n in trees["trade"] for e in n["events"] if e["name"] == "PositionsSaved"]
    rec0 = D["records"][0]

    cards, jumps, lane_rows = [], [], {}
    for i, s in enumerate(STEPS, 1):
        jumps.append(f'<a class="bs-btn" href="#{s["id"]}"><span class="k">{i}</span> {esc(s["title"])}</a>')
        head = (f'<div class="fl-head"><span class="no">{i}</span><span class="bs-mark kept">{esc(s["stage"])}</span>'
                f'<h2 class="bs-h2">{esc(s["title"])}</h2><span class="who">may be sent by: <b>{esc(s["who"])}</b></span></div>')
        if s["id"] == "record":
            moments = ", ".join(f"{r['payDate']} {T['recordTimeLocal']}" for r in D["records"])
            saved = "".join(f"<li>{esc(event_text(e))}</li>" for e in trade_saved)
            cards.append(
                f'<section class="fl-step" id="{s["id"]}">{head}'
                f'<p class="fl-plain">Nothing is sent at a record moment, and no program runs: a Solana program runs only '
                f'inside a transaction someone sends. This bond\'s record moments are {esc(moments)} (bond time, '
                f'UTC{T["utcOffsetHours"]:+d}). Each holder record keeps a position history: the quantity now, '
                f'<code>live_from</code> (how many record moments had passed at its last change) and <code>at[e]</code>, '
                f'the quantity saved for record moment e. The next change to a holding, a transfer, a sale or a '
                f'redemption, first copies the current quantity into <code>at[]</code> for every record moment passed '
                f'since <code>live_from</code>. Between two changes a holding is constant, so that copy is exactly the '
                f'holding at those moments, however late anyone looks.</p>'
                f'<div class="bs-col fl-box"><div class="bs-colhead"><span class="tag">Measured</span><span class="sub">'
                f'record moment 0 at chain time {datetime.fromtimestamp(rec0["chainTime"], timezone.utc):%H:%M:%S} UTC; '
                f'the first transfer after it (step {STEPS.index(next(x for x in STEPS if x["id"] == "trade")) + 1}) logged</span></div>'
                f'<ul class="fl-ev big">{saved}</ul>'
                f'<p class="fl-note">A coupon is paid on <code>at_moment(e)</code>: <code>at[e]</code> if the holding changed '
                f'after moment e, else the quantity now. A holder that never trades needs no transaction at all.</p></div>'
                f'<p class="bs-links">{src_link("state.rs")}</p></section>')
            continue
        e, tx, nodes = picked[s["id"]], txs[s["id"]], trees[s["id"]]
        when = bond_time(e["t"])
        fee_payer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
        others = " ".join(chip(k) for k in signers(tx) if k != fee_payer) or "none"
        cu = sum(n["cu"] or 0 for n in nodes if n["depth"] == 1)
        drawn, batch_note, batch_first, batch_more = first_of_batch(nodes)
        lane_rows[s["id"]] = (drawn, when, e["signature"], batch_first, batch_more)
        checks = "".join(f'<li><code>{esc(err)}</code><span>{esc(cond)}</span></li>' for err, cond in s["checks"])
        cards.append(
            f'<section class="fl-step" id="{s["id"]}">{head}'
            f'<p class="fl-plain">{esc(s["plain"])}</p>'
            f'<p class="fl-meta"><span>bond time {when:%Y-%m-%d %H:%M}</span><span>chain time '
            f'{datetime.fromtimestamp(e["t"], timezone.utc):%Y-%m-%d %H:%M:%S} UTC</span><span>slot {e["slot"]:,}</span>'
            f'<span>fee {e["fee"]:,} lamports, paid by {chip(fee_payer)}</span><span>other signers: {others}</span>'
            f'<span>{cu:,} compute units</span></p>'
            f'<div class="fl-grid"><div class="bs-col kept fl-box"><div class="bs-colhead"><span class="tag">Execution</span>'
            f'<span class="sub">programs in the order they ran; indented = called by the one above</span></div>{tree_html(drawn)}{batch_note}</div>'
            f'<div class="fl-side"><div class="bs-col refused fl-box"><div class="bs-colhead"><span class="tag">Checks</span>'
            f'<span class="sub">refused with this error if not met</span></div><ul class="fl-checks">{checks}</ul></div>'
            f'<div class="bs-col fl-box"><div class="bs-colhead"><span class="tag">Balances</span><span class="sub">token accounts that changed</span>'
            f'</div>{balances_html(tx)}</div></div></div>'
            f'<p class="bs-links"><a class="bs-proof" href="{esc(explorer("tx", e["signature"]))}" target="_blank" rel="noopener">'
            f'transaction</a>{src_link(s["src"])}</p></section>')

    # The swimlane: who may send each step, then the programs; one arrow per call between them, in the order the
    # runtime ran them (the same calls as each step's card).
    lanes = ["Registrar", "Holder", "Bank", "Anyone", "bond_servicing", "Token-2022", "SPL Token"]
    lane_kind = ["wallet"] * 4 + ["state", "program", "program"]
    lane_of = {program: "bond_servicing", TOKEN_2022: "Token-2022", SPL_TOKEN: "SPL Token"}
    compute_budget = "ComputeBudget111111111111111111111111111111"

    def calls(nodes, who):
        """Arrows (caller lane, callee lane, instruction) for the calls between lanes; how often each program outside
        the lanes ran; and how many lane calls those programs made (not drawn either)."""
        arrows, other, hidden, stack = [], {}, 0, []
        for n in nodes:
            while stack and stack[-1][0] >= n["depth"]:
                stack.pop()
            caller = who if n["depth"] == 1 else stack[-1][1]
            callee = lane_of.get(n["program"])
            if callee is None and n["program"] != compute_budget:
                name = PROGRAMS.get(n["program"], n["program"])
                other[name] = other.get(name, 0) + 1
            elif callee is not None and caller is None:
                hidden += 1
            if caller is None or callee is None:
                stack.append((n["depth"], None))
                continue
            arrows.append((caller, callee, n["name"] or ""))
            stack.append((n["depth"], callee))
        # The section's title: every call into Token-2022 comes from bond_servicing, or is a transfer that Token-2022
        # hands straight back to bond_servicing's hook.
        for j, (caller, callee, name) in enumerate(arrows):
            if callee == "Token-2022" and caller != "bond_servicing" and arrows[j + 1:j + 2] != [("Token-2022", "bond_servicing", "TransferHook")]:
                raise SystemExit(f"not building: {caller} calls Token-2022 {name} without bond_servicing")
        return arrows, other, hidden

    def writes(nodes):
        out = []
        for n in nodes:
            if n["program"] != program:
                continue
            for a, key in zip(idl_ix[snake(n["name"])], n["ix"]["accounts"]):
                if a.get("writable") and kind.get(key) == "state" and key not in out:
                    out.append(key)
        return out

    def arrow(row, caller, callee, name):
        a, b = lanes.index(caller) + 1, lanes.index(callee) + 1
        lo, hi = min(a, b), max(a, b)
        cls = ("r" if b > a else "l") + (" ours" if callee == "bond_servicing" else "")
        return (f'<div class="sl-msg {cls}" style="grid-column:{lo} / {hi + 1};grid-row:{row};--n:{hi - lo + 1};--i:{row - 2}">'
                f'<span>{esc(name)}</span></div>')

    trade_no = next(i for i, x in enumerate(STEPS, 1) if x["id"] == "trade")
    bands = []
    for i, s in enumerate(STEPS, 1):
        if s["id"] == "record":
            when_txt = f'{rec0["payDate"]} {T["recordTimeLocal"]}'
            arrows, rows = [], [f'<div class="sl-quiet" style="grid-column:1 / -1;grid-row:2">Record moment 0, {esc(when_txt)} '
                                f'bond time: nobody sends anything and no program runs. The next transfer saves the holdings '
                                f'for it (step {trade_no}).</div>']
            title_tail, cap = "", ("Nothing is sent at a record moment and no program runs. The next change to a holding "
                                   "first saves the quantity each holder had at every record moment passed since its last change.")
        else:
            drawn, when, sig, first, more = lane_rows[s["id"]]
            arrows, other, hidden = calls(drawn, s["who"])
            when_txt = f"{when:%Y-%m-%d %H:%M}"
            rows = [arrow(j + 2, *a) for j, a in enumerate(arrows)]
            foot = []
            if w := writes(drawn):
                foot.append('<span class="k">bond_servicing writes</span>' + "".join(chip(k) for k in w))
            if other:
                made = f", and the {hidden} call{'s' if hidden != 1 else ''} they made" if hidden else ""
                foot.append('<span class="k">not drawn</span>'
                            + esc(", ".join(f"{name} ({count})" for name, count in other.items()) + made))
            if foot:
                rows.append(f'<div class="sl-foot" style="grid-column:1 / -1;grid-row:{len(arrows) + 2}">'
                            + "".join(f"<p>{x}</p>" for x in foot) + "</div>")
            batch = f" · drawn for {first}; the transaction did the same for {more} more" if more else ""
            title_tail = (f'<span class="t">{esc(batch)}</span><a class="bs-proof" href="{esc(explorer("tx", sig))}" '
                          f'target="_blank" rel="noopener">transaction</a>')
            cap = s["plain"]
        lives = "".join(f'<i class="sl-life {lane_kind[c]}" style="grid-column:{c + 1};grid-row:2 / {len(arrows) + 2}"></i>'
                        for c in range(len(lanes))) if arrows else ""
        title = (f'<div class="sl-title" style="grid-column:1 / -1;grid-row:1"><a class="no" href="#{s["id"]}">{i}</a>'
                 f'<span class="bs-mark kept">{esc(s["stage"])}</span><b>{esc(s["title"])}</b>'
                 f'<span class="t">bond time {esc(when_txt)}</span>{title_tail}</div>')
        bands.append(f'<section class="sl-band" data-n="{len(arrows)}" data-cap="{esc(cap)}" data-when="bond time {esc(when_txt)}" '
                     f'data-title="{esc(s["title"])}">{title}{lives}{"".join(rows)}</section>')
    lane_head = ('<div class="sl-lanes"><span class="g" style="grid-column:1 / 5">may send the step</span>'
                 '<span class="g" style="grid-column:5 / 8">programs</span>'
                 + "".join(f'<span class="{lane_kind[c]}">{esc(name)}</span>' for c, name in enumerate(lanes)) + "</div>")
    swimlane = f'<div class="sl">{lane_head}{"".join(bands)}</div>'

    # The parts: every entity the steps use, one row per kind of account.
    describe = {
        "Registrar": "the issuer's agent: creates the bond, registers holders, places bonds, opens offers",
        "Holders": "bond holders' wallets; each signs its own transfers, sales and tenders",
        "Bank": "the cash token's mint and freeze authority (simulated): credits the vaults",
        "bond_servicing": "this program: the bond's rules",
        "Token-2022": "runs the bond token; calls bond_servicing on every bond transfer",
        "SPL Token": "runs the cash token",
        "Associated Token Account program": "opens a wallet's token account for a mint",
        "System program": "creates accounts and moves SOL for their rent",
        "Compute Budget program": "sets the transaction's compute limit; no part of the bond's logic",
        "Memo program": "adds a memo to a payment when the receiving cash account requires one",
        "Bond account": "[\"bond\", bond token]: terms, time table, supply history; signs as mint, burn and vault authority",
        "Hook account list": "[\"extra-account-metas\", bond token]: tells Token-2022 which accounts the hook needs",
        "Holder records": "[\"holder\", bond token, wallet]: position history, paid bits, locked and redeemed bonds",
        "Events": "[\"event\", bond, index]: one per coupon and the redemption: supply at the record moment, budget, vault, funded",
        "Offers": "[\"offer\", bond, id]: kind, price, maximum, window, total, vault, tender counters",
        "Tenders": "[\"tender\", offer, wallet]: number in the offer, quantity, accepted, settled",
        "Bond token (mint)": "Token-2022; transfer hook = bond_servicing; mint authority, permanent delegate and burn "
                             "authority = bond account; no freeze authority",
        "Cash token (mint)": "SPL Token, 6 decimals; the bank mints it",
        "Bond accounts": "Token-2022 associated accounts: one per holder",
        "Cash accounts": "SPL Token associated accounts: where coupons and principal arrive",
        "Vaults": "[\"vault\", event or offer]: cash accounts whose authority is the bond account",
    }
    parts = []
    for k in ("wallet", "program", "state", "token"):
        rows = {g: [] for g in describe}
        for key in sorted((key for key in label if kind.get(key) == k and group[key] != "Sysvar"),
                          key=lambda key: (len(label[key]), label[key])):
            rows[group[key]].append(key)
        rows = {g: keys_g for g, keys_g in rows.items() if keys_g}
        items = "".join(f'<li><span class="fl-acc">{"".join(chip(key) for key in keys_g)}</span>'
                        f'<span><b>{esc(g)}</b>: {esc(describe[g])}</span></li>' for g, keys_g in rows.items())
        parts.append(f'<div class="bs-col fl-box"><div class="bs-colhead"><span class="tag">{esc(KINDS[k][0])}</span>'
                     f'<span class="sub">{esc(KINDS[k][1])}</span></div><ul class="fl-parts">{items}</ul></div>')

    eyebrow = f"Read from devnet · {len(D['holders'])} holders, {len(servicing)} transactions"
    foot = (f'<p>Every example is a transaction of the 8-holder run (<code>docs/demo/data/devnet-full8.json</code>), the '
            f'same run as the timeline. Programs, accounts and events are read from the transaction and the accounts on '
            f'chain; account roles are bond_servicing\'s interface (<code>target/idl/bond_servicing.json</code>); the '
            f'checks are written from the source. In this run the registrar\'s wallet sent and paid for every '
            f'transaction, including the ones anyone may send; the bank co-signed its own credits.</p>'
            f'<p>Built by <code>tools/demo/build_flow.py</code>.</p>')
    template = (ROOT / "tools/demo/flow_template.html").read_text()
    html = fill(template, {"EYEBROW": esc(eyebrow), "LANES": swimlane, "PARTS": "".join(parts), "JUMPS": "".join(jumps),
                           "STEPS": "".join(cards), "FOOT": foot})
    Path(out_path).write_text(html)
    print(f"wrote {out_path}: {len(STEPS)} steps, {len(label)} labelled accounts")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: build_flow.py <measured-dataset.json> <rpc url> <out.html>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
