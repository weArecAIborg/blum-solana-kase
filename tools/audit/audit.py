"""Independent ledger audit: measure every wallet's balances on the ledger and compare with the bond terms.

Method and rules: expertise/independent-ledger-audit.md.
  Measured side: Python standard library + standard Solana RPC only. It never reads the bond program's accounts,
                 its IDL, or the operator's report.
  Calculated side: the scenario input (scenarios/*.json) + reference math (QuantLib, FinancePy).

Usage: .venv/bin/python3 tools/audit/audit.py <scenarios/NAME.json> <rpc url> <program-accounts|largest-accounts|history-accounts>
Exit code 1 if any check fails.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
DVP_PROGRAM = "dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq"  # Solana DvP (solana-foundation/dvp): trade escrows
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

results = []
last_audit = {}  # what the last audit measured (read by tools/demo/build_demo.py)


def check(cid, label, ok, detail=""):
    results.append((cid, label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {cid} {label}{(' - ' + detail) if detail else ''}")


# ---------------------------------------------------------------- measurement side (stdlib only)

def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


# Token-2022 PermissionedBurn (instruction 46, sub-instructions Burn = 1, BurnChecked = 2), from the
# solana-program/token-2022 source. A node may not parse it yet; then it is read from the raw instruction.
PERMISSIONED_BURN = 46


def pubkey_of(key: str) -> str:
    """A wallet or mint in the scenario: its public key (base58), or a Solana keypair file (64 bytes: secret seed,
    then public key). Public keys let anyone run the audit without the run's key files (scenarios/*.public.json)."""
    if not key.endswith(".json"):
        if len(b58decode(key)) != 32:
            raise SystemExit(f"not a public key: {key}")
        return key
    data = json.loads((ROOT / key).read_text())
    return b58encode(bytes(data[32:64]))


class Rpc:
    """Raw JSON-RPC client. Confirmed transactions do not change, so `getTransaction` replies are kept on disk
    (ops/out/rpc-cache/<host>/), and measuring the same history again does not download it again.
    A cached reply is used only if its slot is the slot the ledger reports now for that signature: fresh local
    networks at the same URL replay identical transactions with identical signatures in other slots (measured
    2026-10-08: 186 of 243 cached Surfpool replies carried the previous network's slot and block time)."""

    def __init__(self, url):
        self.url = url
        host = urllib.parse.urlparse(url).netloc.replace(":", "_")
        self.cache = ROOT / "ops/out/rpc-cache" / host

    def call(self, method, params, slot=None):
        if method == "getTransaction":
            path = self.cache / f"{params[0]}.json"
            if path.exists():
                cached = json.loads(path.read_text())
                if cached["slot"] == slot:
                    return cached
            result = self._call(method, params)
            if result is not None:
                self.cache.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result))
            return result
        return self._call(method, params)

    def _call(self, method, params):
        """JSON-RPC call. Public RPC nodes rate-limit (HTTP 429) and sometimes time out: wait and retry the same
        request, up to 8 times."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(8):
            req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    reply = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                time.sleep(2 ** attempt)
                continue
            except (urllib.error.URLError, TimeoutError) as e:
                if not isinstance(getattr(e, "reason", e), TimeoutError) and not isinstance(e, TimeoutError):
                    raise
                time.sleep(2 ** attempt)
                continue
            if "error" in reply and reply["error"].get("code") == 429:
                time.sleep(2 ** attempt)
                continue
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
            return reply["result"]
        raise RuntimeError(f"{method}: still rate-limited or timing out after 8 attempts")


def measure_state(rpc, mint, program, source, history_accounts=()):
    """Channel S: every token account of the mint (owner, amount) as parsed by the node, and the supply.

    source = "program-accounts": getProgramAccounts filtered by mint (complete; local nodes allow it).
    source = "history-accounts": getMultipleAccounts over every token account seen in channel H (public devnet RPC
    rate-limits getTokenLargestAccounts persistently). Completeness again rests on M1 and M3.
    source = "largest-accounts": getTokenLargestAccounts + getMultipleAccounts (public devnet RPC refuses the
    program-wide scan). It returns at most 20 accounts and may omit zero balances; M1 (sum = supply) and M3 (history
    of every account seen) prove nothing with a balance was missed.
    """
    state = {}
    if source == "program-accounts":
        accounts = rpc.call("getProgramAccounts", [program, {"encoding": "jsonParsed",
                                                            "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}])
        parsed_list = [(a["pubkey"], a["account"]["data"]["parsed"]) for a in accounts]
    elif source == "largest-accounts":
        addresses = [a["address"] for a in rpc.call("getTokenLargestAccounts", [mint])["value"]]
        infos = rpc.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])["value"] if addresses else []
        parsed_list = [(addr, info["data"]["parsed"]) for addr, info in zip(addresses, infos) if info]
    elif source == "history-accounts":
        # Every token account the runtime recorded in channel H, read now. Tokens held anywhere else would make
        # M1 (sum = supply) fail; a change history missed would make M3 fail.
        addresses = sorted(history_accounts)
        parsed_list = []
        for i in range(0, len(addresses), 100):
            batch = addresses[i:i + 100]
            infos = rpc.call("getMultipleAccounts", [batch, {"encoding": "jsonParsed"}])["value"]
            parsed_list += [(addr, info["data"]["parsed"]) for addr, info in zip(batch, infos) if info]
    else:
        raise ValueError(f"unknown state source {source}")
    for pubkey, parsed in parsed_list:
        if parsed["type"] != "account" or parsed["info"]["mint"] != mint:
            continue
        state[pubkey] = {"owner": parsed["info"]["owner"], "amount": int(parsed["info"]["tokenAmount"]["amount"])}
    supply = int(rpc.call("getTokenSupply", [mint])["value"]["amount"])
    return state, supply


def signatures(rpc, address):
    sigs, before = [], None
    while True:
        opts = {"limit": 1000, "commitment": "confirmed"}
        if before:
            opts["before"] = before
        page = rpc.call("getSignaturesForAddress", [address, opts])
        sigs += page
        if len(page) < 1000:
            return sigs
        before = page[-1]["signature"]


def measure_history(rpc, addresses):
    """Channel H: every transaction touching the addresses, with runtime-recorded token balances before/after.

    The addresses are the two mints plus the bond's authority and its vaults (all public). The union matters: on
    2026-10-08 public devnet RPC returned the mints' histories with a contiguous block of 60 finalized transactions
    missing, while the vault's history returned them. A transaction no address lists would still be caught by M3."""
    seen = {}
    for address in addresses:
        for s in signatures(rpc, address):
            seen[s["signature"]] = s
    txs = []
    for sig in seen:
        tx = rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                               "commitment": "confirmed"}], seen[sig]["slot"])
        keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
        meta = tx["meta"]
        balances = defaultdict(lambda: {"pre": 0, "post": 0, "owner": None, "mint": None})
        for side in ("pre", "post"):
            for b in meta[f"{side}TokenBalances"]:
                entry = balances[keys[b["accountIndex"]]]
                entry[side] = int(b["uiTokenAmount"]["amount"])
                entry["owner"], entry["mint"] = b.get("owner"), b["mint"]
        minted, burned = defaultdict(int), defaultdict(int)
        instructions = list(tx["transaction"]["message"]["instructions"])
        for inner in meta.get("innerInstructions") or []:
            instructions += inner["instructions"]
        for ix in instructions:
            parsed = ix.get("parsed")
            if ix.get("programId") == TOKEN_2022 and "data" in ix and not parsed:
                raw = b58decode(ix["data"])
                if len(raw) >= 10 and raw[0] == PERMISSIONED_BURN and raw[1] in (1, 2):
                    burned[ix["accounts"][1]] += int.from_bytes(raw[2:10], "little")
                continue
            if not isinstance(parsed, dict) or ix.get("program") not in ("spl-token", "spl-token-2022"):
                continue
            info = parsed.get("info", {})
            amount = info.get("amount") or (info.get("tokenAmount") or {}).get("amount")
            kind = parsed["type"].lower()
            if kind in ("mintto", "minttochecked"):
                minted[info["mint"]] += int(amount)
            elif "burn" in kind and "mint" in info and amount is not None:
                burned[info["mint"]] += int(amount)
        txs.append({
            "signature": sig, "slot": tx["slot"], "blockTime": tx["blockTime"], "failed": meta["err"] is not None,
            "deltas": {acct: {"owner": b["owner"], "mint": b["mint"], "delta": b["post"] - b["pre"]}
                       for acct, b in balances.items()},
            "minted": dict(minted), "burned": dict(burned),
            "programs": {ix.get("programId") for ix in instructions if ix.get("programId")},
        })
    txs.sort(key=lambda t: (t["slot"], t["blockTime"] or 0))
    return txs


# ---------------------------------------------------------------- calculated side (scenario + reference math)

def record_moment_unix(date_iso, local_hhmm, utc_offset_hours):
    y, m, d = map(int, date_iso.split("-"))
    h, mi = map(int, local_hhmm.split(":"))
    return int(datetime(y, m, d, h, mi, tzinfo=timezone(timedelta(hours=utc_offset_hours))).timestamp())


def timeline(placements, trades, offers, record_times):
    """Positions over the bond's life from the scenario alone, in the operator's order. After each record moment k:
    the trades of phase k, then the offer actions of phase k (buyback sales retire bonds at once; tenders and
    withdrawals only count). A tender offer is settled in the first phase whose next record moment comes after the
    offer's close: everything tendered if the offer is not oversubscribed, else, in tender order, each tender's
    running-total pro-rata share minus that of the tenders before it.
    Returns the positions at each record moment, the final positions, each holder's bond movements and retirements
    (burned, cash) in ledger order, and the offers' total budget."""
    pos = defaultdict(int, placements)
    moves = defaultdict(list)
    for h, n in placements.items():
        moves[h].append(n)
    retire = defaultdict(list)
    at_record = []
    tendered = {o["id"]: {} for o in offers}
    settled = set()

    def close_time(o):
        return record_times[o["close"]["recordMoment"]] + o["close"]["plusDays"] * 86_400

    def retire_now(h, q, price):
        pos[h] -= q
        moves[h].append(-q)
        retire[h].append((q, q * price))

    for k in range(len(record_times)):
        at_record.append(dict(pos))
        for tr in [x for x in trades if x["afterRecordMoment"] == k]:
            pos[tr["from"]] -= tr["amount"]
            pos[tr["to"]] += tr["amount"]
            moves[tr["from"]].append(-tr["amount"])
            moves[tr["to"]].append(tr["amount"])
        for o in offers:
            for a in [x for x in o["actions"] if x["afterRecordMoment"] == k]:
                if "sell" in a:
                    retire_now(a["holder"], a["sell"], int(o["priceUnits"]))
                else:
                    t = tendered[o["id"]]
                    t[a["holder"]] = t.get(a["holder"], 0) + a.get("tender", 0) - a.get("withdraw", 0)
        nxt = record_times[k + 1] if k + 1 < len(record_times) else float("inf")
        for o in offers:
            if o["kind"] != "tender" or o["id"] in settled or close_time(o) >= nxt:
                continue
            t = tendered[o["id"]]
            total, mx = sum(t.values()), o["maxQuantity"]
            # The program resolves tenders in the order they were made (each holder's first tender) and gives each the
            # running-total pro-rata share minus what the earlier ones received, so the offer buys exactly its maximum.
            before = 0
            for h, q in t.items():
                accepted = q if total <= mx else (before + q) * mx // total - before * mx // total
                before += q
                if q > 0 and accepted > 0:
                    retire_now(h, accepted, int(o["priceUnits"]))
            settled.add(o["id"])
    budgets = sum(int(o["priceUnits"]) * o["maxQuantity"] for o in offers)
    return {"at_record": at_record, "final": dict(pos), "moves": moves, "retire": retire, "offer_budgets": budgets}


def calculate(scn):
    import QuantLib as ql
    from financepy.products.bonds.bond import Bond as FpBond, CouponType
    from financepy.utils.date import Date as FpDate
    from financepy.utils.day_count import DayCountTypes
    from financepy.utils.frequency import FrequencyTypes

    t = scn["terms"]
    face_units = int(t["faceUnits"])

    def qd(s):
        y, m, d = map(int, s.split("-"))
        return ql.Date(d, m, y)

    cal = ql.BespokeCalendar("bond")
    for day in scn["weekend"]:
        cal.addWeekend(getattr(ql, day))
    for line in (ROOT / scn["calendar"]).read_text().splitlines():
        if line and not line.startswith("#"):
            cal.addHoliday(qd(line))
    sched = ql.Schedule(qd(t["issue"]), qd(t["maturity"]), ql.Period(t["monthsPerPeriod"], ql.Months), cal,
                        ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False)
    pay_dates = [cal.adjust(sched[i], ql.Following) for i in range(1, len(sched))]
    lag = t["recordBusinessDaysBeforePayment"]
    record_dates = [cal.advance(d, -lag, ql.Days) if lag else d for d in pay_dates]
    pay_dates = [f"{d.year():04d}-{d.month():02d}-{d.dayOfMonth():02d}" for d in pay_dates]
    record_dates = [f"{d.year():04d}-{d.month():02d}-{d.dayOfMonth():02d}" for d in record_dates]

    freq = {1: FrequencyTypes.ANNUAL, 2: FrequencyTypes.SEMI_ANNUAL, 4: FrequencyTypes.QUARTERLY, 12: FrequencyTypes.MONTHLY}[t["periodsPerYear"]]
    y, m, d = map(int, t["issue"].split("-"))
    y2, m2, d2 = map(int, t["maturity"].split("-"))
    fp = FpBond(FpDate(d, m, y), FpDate(d2, m2, y2), t["rateBps"] / 10_000, freq, DayCountTypes.THIRTY_360_BOND,
                cpn_type=CouponType.FIXED)
    per_bond = [Decimal(str(f)) * face_units for f in fp.flow_amounts[1:]]

    record_times = [record_moment_unix(d, t["recordTimeLocal"], t["utcOffsetHours"]) for d in record_dates]
    tl = timeline(scn["placements"], scn["trades"], scn["offers"], record_times)
    coupons = {h: [] for h in scn["wallets"]}  # (coupon index, amount)
    budgets_total = tl["offer_budgets"]  # each event's budget: the amount for the whole supply, rounded down once
    for k, amount_per_bond in enumerate(per_bond):
        held = tl["at_record"][k]
        budgets_total += int((Decimal(sum(held.values())) * amount_per_bond).to_integral_value(rounding=ROUND_FLOOR))
        for h in scn["wallets"]:
            if held.get(h, 0) > 0:
                coupons[h].append((k, int((Decimal(held[h]) * amount_per_bond).to_integral_value(rounding=ROUND_FLOOR))))
    price = Decimal(t["redemptionPriceBps"]) / 10_000
    final = tl["final"]
    budgets_total += int((Decimal(sum(final.values())) * face_units * price).to_integral_value(rounding=ROUND_FLOOR))
    bond_moves = {h: list(tl["moves"][h]) for h in scn["wallets"]}
    retirements = {h: list(tl["retire"][h]) for h in scn["wallets"]}
    principal = {}
    for h, pos in final.items():
        if pos > 0:
            principal[h] = int((Decimal(pos) * face_units * price).to_integral_value(rounding=ROUND_FLOOR))
            bond_moves[h].append(-pos)
            retirements[h].append((pos, principal[h]))
    return {"pay_dates": pay_dates, "record_dates": record_dates, "budgets_total": budgets_total,
            "coupons": coupons, "bond_moves": bond_moves, "principal": principal, "retirements": retirements,
            "face_units": face_units}


def bond_vaults(rpc, bond_mint, cash_mint):
    """The bond's vaults (one per event), identified from public token data only: the cash accounts owned by the
    bond token's mint authority. Returns (authority, [vault addresses])."""
    info = rpc.call("getAccountInfo", [bond_mint, {"encoding": "jsonParsed"}])["value"]["data"]["parsed"]["info"]
    owned = rpc.call("getTokenAccountsByOwner", [info["mintAuthority"], {"mint": cash_mint}, {"encoding": "jsonParsed"}])
    return info["mintAuthority"], [a["pubkey"] for a in owned["value"]]


# ---------------------------------------------------------------- audit

def main(scenario_path, rpc_url, state_source):
    scn = json.loads(Path(scenario_path).read_text())
    rpc = Rpc(rpc_url)
    unit = 10 ** scn["terms"]["cashDecimals"]
    wallets = {pubkey_of(f): name for name, f in scn["wallets"].items()}
    bond_mint, cash_mint = pubkey_of(scn["keys"]["bondMint"]), pubkey_of(scn["keys"]["cashMint"])
    print(f"{scn['name']}\nbond mint {bond_mint}\ncash mint {cash_mint}\nRPC {rpc_url}  (state source: {state_source})")

    print("\nMeasure: channel S (state of all token accounts) and channel H (transaction history)")
    authority, vaults = bond_vaults(rpc, bond_mint, cash_mint)
    txs = measure_history(rpc, [bond_mint, cash_mint, authority, *vaults])
    # Public devnet's address indexes can miss transactions (2026-10-09: the cash mint's index lacked transactions of
    # an earlier bond paid in the same cash token, while the holders' own accounts listed them). Where the two
    # channels disagree on an account, read that account's own index and add what the others did not list; repeat
    # until no account is left to consult. What still disagrees afterwards fails M1/M3 below.
    consulted, added = set(), 0
    while True:
        seen = {mint: {a for t in txs for a, d in t["deltas"].items() if d["mint"] == mint} for mint in (bond_mint, cash_mint)}
        bond_state, bond_supply = measure_state(rpc, bond_mint, TOKEN_2022, state_source, seen[bond_mint])
        cash_state, cash_supply = measure_state(rpc, cash_mint, TOKEN, state_source, seen[cash_mint])
        summed = defaultdict(int)
        for t in txs:
            for acct, d in t["deltas"].items():
                summed[(acct, d["mint"])] += d["delta"]
        disagree = {acct for state, mint in ((bond_state, bond_mint), (cash_state, cash_mint))
                    for acct in set(state) | {a for a, m in summed if m == mint}
                    if summed[(acct, mint)] != state.get(acct, {"amount": 0})["amount"]} - consulted
        if not disagree:
            break
        known = {t["signature"] for t in txs}
        extra = [t for t in measure_history(rpc, sorted(disagree)) if t["signature"] not in known]
        consulted |= disagree
        added += len(extra)
        txs = sorted(txs + extra, key=lambda t: (t["slot"], t["blockTime"] or 0))
    print(f"  {len(bond_state)} bond accounts, {len(cash_state)} cash accounts, {len(txs)} transactions"
          + (f" ({added} listed only by the own indexes of {len(consulted)} accounts where the channels disagreed)"
             if consulted else ""))
    bond_txs = [t for t in txs if any(d["mint"] == bond_mint for d in t["deltas"].values()) or bond_mint in t["minted"]
                or bond_mint in t["burned"]]
    creation = signatures(rpc, bond_mint)[-1]  # oldest transaction of the bond mint = its creation
    creation_time = creation["blockTime"]

    print("\nModel-free invariants")
    for label, state, supply in (("bond", bond_state, bond_supply), ("cash", cash_state, cash_supply)):
        total = sum(a["amount"] for a in state.values())
        check("M1", f"{label}: sum of all account balances = supply", total == supply, f"{total} vs {supply}")
    bad = []
    for t in txs:
        for mint in (bond_mint, cash_mint):
            net = sum(d["delta"] for d in t["deltas"].values() if d["mint"] == mint)
            # A failed transaction changes no balance, whatever mint or burn instructions it carried (refused attacks).
            expected = 0 if t["failed"] else t["minted"].get(mint, 0) - t["burned"].get(mint, 0)
            if net != expected:
                bad.append(f"{t['signature'][:12]} {mint[:6]} net {net} vs mint-burn {expected}")
    check("M2", "every transaction: balance changes = minted - burned", not bad, "; ".join(bad[:3]))
    history_balance = defaultdict(int)
    for t in txs:
        for acct, d in t["deltas"].items():
            history_balance[(acct, d["mint"])] += d["delta"]
    # Every account seen in either channel: its history sum must equal its measured balance (0 if absent from S).
    mismatched = [acct for state, mint in ((bond_state, bond_mint), (cash_state, cash_mint))
                  for acct in set(state) | {a for a, m in history_balance if m == mint}
                  if history_balance[(acct, mint)] != state.get(acct, {"amount": 0})["amount"]]
    check("M3", "history sums = current balances (both channels agree)", not mismatched, ", ".join(mismatched[:3]))
    # Many bonds may be paid in the same cash token and investors may hold several bonds, so every servicing check
    # below is scoped to this bond's vaults (found above); M1-M3 above stay global over both mints.
    vault_set = set(vaults)
    check("M4", f"the bond's vaults = cash accounts owned by the bond token's mint authority ({authority[:8]}...)",
          bool(vaults), f"{len(vaults)} vaults")
    vault_delta = lambda t: sum(t["deltas"][v]["delta"] for v in vault_set if v in t["deltas"])
    vault_txs = [t for t in txs if any(v in t["deltas"] and t["deltas"][v]["delta"] for v in vault_set)]
    bad_in = [t["signature"][:12] for t in vault_txs
              if any(t["deltas"][v]["delta"] > 0 for v in vault_set if v in t["deltas"]) and not t["minted"].get(cash_mint)]
    check("M4", "cash enters the vaults only from the bank's mints", bool(vaults) and not bad_in, ", ".join(bad_in[:3]))
    leaks = [f"{a} (owner {d['owner']})" for t in vault_txs if vault_delta(t) < 0
             for a, d in t["deltas"].items() if d["mint"] == cash_mint and d["delta"] > 0 and a not in vault_set
             and d["owner"] not in wallets]
    check("M4", "cash leaves the vaults only to scenario wallets", bool(vaults) and not leaks, "; ".join(leaks[:3]))
    # A secondary trade settled by Solana DvP (solana-foundation/dvp) escrows the bonds in an account owned by the
    # trade's SwapDvp record, which that program owns. Read from the ledger: the owner of an unknown bond account is
    # an account owned by the DvP program. Bonds may leave such an escrow only to scenario wallets.
    # The SwapDvp record is closed at settlement, so its owner cannot be read afterwards; the ledger still shows
    # which program moved the escrow's bonds out: SettleDvp, CancelDvp and ReclaimDvp are DvP instructions.
    unknown_owners = {d["owner"] for t in txs for d in t["deltas"].values()
                      if d["mint"] == bond_mint and d["delta"] != 0 and d["owner"] not in wallets}
    dvp_escrow_owners = {o for o in unknown_owners
                         if all(DVP_PROGRAM in t["programs"] for t in txs
                                if any(d["owner"] == o and d["mint"] == bond_mint and d["delta"] < 0 for d in t["deltas"].values()))
                         and any(d["owner"] == o and d["mint"] == bond_mint and d["delta"] < 0 for t in txs for d in t["deltas"].values())}
    bond_strangers = [f"{a} (owner {d['owner']})" for t in txs for a, d in t["deltas"].items()
                      if d["mint"] == bond_mint and d["delta"] != 0 and d["owner"] not in wallets and d["owner"] not in dvp_escrow_owners]
    check("M4", "no account outside the scenario wallets moved bonds, except Solana DvP trade escrows"
          + (f" ({len(dvp_escrow_owners)} escrows)" if dvp_escrow_owners else ""), not bond_strangers, "; ".join(bond_strangers[:3]))
    escrow_leaks = [f"{a} (owner {d['owner']})" for t in txs
                    if any(d["mint"] == bond_mint and d["delta"] < 0 and d["owner"] in dvp_escrow_owners for d in t["deltas"].values())
                    for a, d in t["deltas"].items() if d["mint"] == bond_mint and d["delta"] > 0 and d["owner"] not in wallets]
    if dvp_escrow_owners:
        check("M4", "bonds leave a DvP escrow only to scenario wallets", not escrow_leaks, "; ".join(escrow_leaks[:3]))

    print("\nMeasured vs calculated (exact integers, base units)")
    calc = calculate(scn)
    # Measured per holder, in ledger order.
    cash_in = defaultdict(list)    # (tx, amount) coupon arrivals from this bond's vault
    bond_moves = defaultdict(list)
    retirements = defaultdict(list)  # holder -> [(burned, cash received in the same transaction)], ledger order
    outside = defaultdict(list)    # holder cash movements in transactions that do not involve this bond's vault
    vault_in = vault_out = 0
    for t in txs:
        touches_vault = any(v in t["deltas"] for v in vault_set)
        from_vault = vault_delta(t) < 0
        for acct, d in t["deltas"].items():
            if d["delta"] == 0:
                continue
            holder = wallets.get(d["owner"])
            if d["mint"] == bond_mint and holder:
                bond_moves[holder].append(d["delta"])
            if d["mint"] == cash_mint and acct in vault_set:
                vault_in += max(d["delta"], 0)
                vault_out += max(-d["delta"], 0)
            if d["mint"] == cash_mint and holder and not touches_vault:
                outside[holder].append(d["delta"])
        if not from_vault:
            continue
        burned_by = {wallets[d["owner"]]: -d["delta"] for d in t["deltas"].values()
                     if d["mint"] == bond_mint and d["delta"] < 0 and t["burned"].get(bond_mint) and d["owner"] in wallets}
        for d in t["deltas"].values():
            holder = wallets.get(d["owner"])
            if d["mint"] == cash_mint and holder and d["delta"] > 0:
                if holder in burned_by:
                    retirements[holder].append((burned_by[holder], d["delta"]))
                else:
                    cash_in[holder].append((t, d["delta"]))

    for h in scn["wallets"]:
        expected = [amount for _, amount in calc["coupons"][h]]
        measured = [amount for _, amount in cash_in[h]]
        check("C1", f"{h}: coupon arrivals from the vault", measured == expected,
              f"measured {[x / unit for x in measured]} vs calculated {[x / unit for x in expected]}")
        check("C2", f"{h}: bond movements", bond_moves[h] == calc["bond_moves"][h],
              f"measured {bond_moves[h]} vs calculated {calc['bond_moves'][h]}")
        if calc["retirements"][h] or retirements[h]:
            got = [(b, c / unit) for b, c in retirements[h]]
            exp = [(b, c / unit) for b, c in calc["retirements"][h]]
            check("C3", f"{h}: bonds retired against cash in one transaction (buyback, tender, redemption) = calculated",
                  got == exp, f"measured (burned, cash) {got} vs calculated {exp}")
    total_out = (sum(a for h in calc["coupons"] for _, a in calc["coupons"][h])
                 + sum(c for h in calc["retirements"] for _, c in calc["retirements"][h]))
    final_vaults = sum(cash_state.get(v, {"amount": 0})["amount"] for v in vault_set)
    # Each event is funded with its budget: the amount for the whole supply, rounded down once; each offer with price x
    # maximum. Holders are rounded down one by one and offers may not fill, so what stays in the vaults is the rounding
    # difference plus unfilled offer cash, and nothing else.
    leftover = calc["budgets_total"] - total_out
    check("C4", "vaults: inflow = outflow + final balance; outflow = calculated payments; final = calculated leftover",
          bool(vaults) and vault_in == vault_out + final_vaults and vault_out == total_out and final_vaults == leftover,
          f"in {vault_in / unit}, out {vault_out / unit}, final {final_vaults / unit}, calculated out {total_out / unit}, "
          f"calculated leftover {leftover / unit}")

    # C7: per holder, what this bond measurably delivered (cash from its vault, bonds held now) against the terms.
    # Holder cash moved outside this bond (other bonds paid in the same cash token, the holder's own transfers) is
    # the holder's own business; it is shown, not judged.
    owner_bonds = defaultdict(int)
    for a in bond_state.values():
        owner_bonds[a["owner"]] += a["amount"]
    for pk, h in wallets.items():
        expected_cash = sum(a for _, a in calc["coupons"][h]) + sum(c for _, c in calc["retirements"][h])
        received = sum(a for _, a in cash_in[h]) + sum(c for _, c in retirements[h])
        expected_bonds = sum(calc["bond_moves"][h])
        check("C7", f"{h}: cash from this bond and bonds held now = calculated",
              received == expected_cash and owner_bonds[pk] == expected_bonds,
              f"cash {received / unit} vs {expected_cash / unit}, bonds {owner_bonds[pk]} vs {expected_bonds}"
              + (f"; outside this bond: {len(outside[h])} cash movements, net {sum(outside[h]) / unit:+,.6f}"
                 if outside[h] else ""))

    # C5 timing. Bond time of a transaction = bond time at creation + elapsed chain time scaled by the scenario's
    # compression (chain seconds per bond day), measured from the bond mint's creation transaction.
    almaty = timezone(timedelta(hours=scn["terms"]["utcOffsetHours"]))
    bond_origin = datetime.fromisoformat(scn["bondTimeAtCreation"]).timestamp()
    scale = scn["chainSecondsPerBondDay"]
    rec_h, rec_m = map(int, scn["terms"]["recordTimeLocal"].split(":"))
    dl_h, dl_m = map(int, scn["terms"]["paymentDeadlineLocal"].split(":"))

    def record_moment(k):
        y, m, d = map(int, calc["record_dates"][k].split("-"))
        return datetime(y, m, d, rec_h, rec_m, tzinfo=almaty).timestamp()

    late = []
    handover = scn["handover"]
    for h in scn["wallets"]:
        for (k, _), (t, _) in zip(calc["coupons"][h], cash_in[h]):
            bond_ts = bond_origin + (t["blockTime"] - creation_time) * 86_400 / scale
            bond_time = datetime.fromtimestamp(bond_ts, almaty)
            if scale == 86_400:
                local = bond_time.hour * 60 + bond_time.minute
                ok = bond_time.date().isoformat() == calc["pay_dates"][k] and rec_h * 60 + rec_m <= local < dl_h * 60 + dl_m
            elif handover is not None and k > handover["operatorStopsAfterCoupon"]:
                # Declared operator failure: these coupons are paid late, after the final record moment.
                ok = bond_ts >= record_moment(len(calc["pay_dates"]) - 1)
            else:
                nxt = record_moment(k + 1) if k + 1 < len(calc["pay_dates"]) else float("inf")
                ok = record_moment(k) <= bond_ts < nxt
            if not ok:
                late.append(f"{h} coupon {k} at bond time {bond_time.isoformat()} (due {calc['pay_dates'][k]})")
    if scale == 86_400:
        check("C5", "every coupon paid on its payment date between 09:00 and 17:00 Almaty", not late, "; ".join(late[:3]))
    else:
        rule = ("before the next one" if handover is None else
                f"before the next one; coupons after {handover['operatorStopsAfterCoupon']} after the final record moment "
                f"(declared operator failure)")
        check("C5", f"every coupon paid after its record moment and {rule} (compressed time: 1 bond day = "
                    f"{scale} s, so the 8-hour 09:00-17:00 window is {scale / 3:.1f} s and is not checked)",
              not late, "; ".join(late[:3]))

    if 10 in scn["placements"].values() and (scn["terms"]["periodsPerYear"], scn["terms"]["rateBps"], scn["terms"]["faceUnits"]) == (2, 1000, "1000000000"):
        h = next(name for name, n in scn["placements"].items() if n == 10)
        first = cash_in[h][0][1] if cash_in[h] else None
        check("C6", f"listing example: {h} held 10 bonds at the first record moment, measured first coupon = 500",
              first == 500 * unit, f"{first / unit if first is not None else None}")

    failed = [r for r in results if not r[2]]
    last_audit["transactions"] = len(txs)
    print(f"\n{'AUDIT PASSED' if not failed else f'AUDIT FAILED: {len(failed)} checks'}  "
          f"({len(results)} checks, {len(txs)} transactions measured)")
    return 0 if not failed else 1


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: audit.py <scenarios/NAME.json> <rpc url> <program-accounts|largest-accounts|history-accounts>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
