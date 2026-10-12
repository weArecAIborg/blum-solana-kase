"""Measure a run from the ledger into docs/demo/data/<scenario>.json, the input of the demonstration pages, and
chart it (charts V1-V3).

Measurement reuses tools/audit (standard RPC only, no program accounts, no operator report); each transaction is
classified purely by the token movements the runtime recorded. Charts are plotly.js (owner's rule: plotly only).

Usage: .venv/bin/python3 tools/demo/build_demo.py <scenarios/devnet-NAME.json> <rpc url> <cluster> <out.html>
  e.g. ... scenarios/devnet-crowd-1000.json https://api.devnet.solana.com devnet docs/demo/charts.html
  (the data is written next to the page, in docs/demo/data/)
The output is a standalone HTML file (open it in a browser); it loads plotly.js from cdnjs and fonts from Google Fonts.
"""

import base64
import contextlib
import hashlib
import io
import random
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
import audit as A  # noqa: E402


SAMPLE_SEED = 20261008
SAMPLE_SIZE = 10


def explorer(kind, value, cluster):
    return f"https://explorer.solana.com/{kind}/{value}?cluster={cluster}"


def classify(t, bond_mint, cash_mint, wallets, vaults, final_record_time):
    """Name a transaction by what moved, as measured. Holder cash counts only when one of this bond's vaults takes
    part: the same wallets may hold the cash token from other bonds or their own transfers."""
    vault_delta = sum(t["deltas"][v]["delta"] for v in vaults if v in t["deltas"])
    holder_moves = {}
    for acct, d in t["deltas"].items():
        name = wallets.get(d["owner"])
        if name and d["delta"] and (d["mint"] == bond_mint or vault_delta):
            key = "bonds" if d["mint"] == bond_mint else "cash"
            holder_moves.setdefault(name, {"bonds": 0, "cash": 0})[key] += d["delta"]
    if t["burned"].get(bond_mint):
        # Bonds retired against cash: before the final record moment by an offer (buyback, tender), after it at
        # redemption.
        return ("redemption" if t["blockTime"] >= final_record_time else "buyback"), holder_moves
    if t["minted"].get(bond_mint):
        return "placement", holder_moves
    if t["minted"].get(cash_mint) and vault_delta > 0:
        return "funding", holder_moves
    bond_moves = [n for n, m in holder_moves.items() if m["bonds"]]
    if len(bond_moves) == 2:
        return "trade", holder_moves
    if vault_delta < 0 and any(m["cash"] > 0 for m in holder_moves.values()):
        return "coupon", holder_moves
    return "other", holder_moves


# Program instruction names as the program logs them (Anchor: "Program log: Instruction: <Name>").
PROGRAM_INSTRUCTIONS = {
    "CreateBond", "InitHookAccounts", "RegisterHolder", "Place", "TransferHook", "OpenEvent", "ConfirmFunding", "ResolveTender",
    "PayCoupon", "RedeemHolder", "CreateOffer", "ConfirmOfferFunding", "SellToOffer", "ChangeTender", "SettleTender",
}


# Anchor event logged by open_event: discriminator sha256("event:EventOpened")[:8], then bond (32), event u16,
# supply_at_record u64, budget u64, vault (32), little-endian (programs/bond_servicing/src/events.rs).
EVENT_OPENED = hashlib.sha256(b"event:EventOpened").digest()[:8]


def event_opened(logs):
    for line in logs:
        if line.startswith("Program data: "):
            raw = base64.b64decode(line.split(": ", 1)[1])
            if raw[:8] == EVENT_OPENED and len(raw) >= 8 + 32 + 2 + 8 + 8 + 32:
                return {"event": int.from_bytes(raw[40:42], "little"), "vault": A.b58encode(raw[58:90])}
    return None


def measure_servicing(rpc, addresses, vaults):
    """Every transaction touching the bond's own accounts (bond PDA, bond token, vaults): who signed and paid the
    fee, the fee, the storage deposit the fee payer made (lamports it spent beyond the fee), the program instructions
    it ran (from the runtime logs), and whether it funded a vault (the issuer's cash). Standard RPC only."""
    seen = {}
    for address in addresses:
        for sig in A.signatures(rpc, address):
            if sig["err"] is None:
                seen[sig["signature"]] = sig
    out = []
    for signature in seen:
        tx = rpc.call("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                     "commitment": "confirmed"}], seen[signature]["slot"])
        keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
        meta = tx["meta"]
        names = []
        for line in meta["logMessages"]:
            if line.startswith("Program log: Instruction: "):
                name = line.split(": ", 2)[2]
                if name in PROGRAM_INSTRUCTIONS and name not in names:
                    names.append(name)
        funded = any(
            ix.get("parsed", {}).get("type") in ("mintTo", "mintToChecked", "transfer", "transferChecked")
            and ix["parsed"]["info"].get("account", ix["parsed"]["info"].get("destination")) in vaults
            for ix in tx["transaction"]["message"]["instructions"] if isinstance(ix.get("parsed"), dict))
        spent = meta["preBalances"][0] - meta["postBalances"][0]
        out.append({"signature": signature, "slot": tx["slot"], "t": tx["blockTime"], "feePayer": keys[0],
                    "fee": meta["fee"], "deposit": spent - meta["fee"], "instructions": names, "funding": funded,
                    "opened": event_opened(meta["logMessages"])})
    out.sort(key=lambda x: (x["slot"], x["t"]))
    return out


def main(scenario_path, rpc_url, cluster, out_path):
    scn = json.loads(Path(scenario_path).read_text())
    rpc = A.Rpc(rpc_url)
    unit = 10 ** scn["terms"]["cashDecimals"]
    wallets = {A.pubkey_of(f): name for name, f in scn["wallets"].items()}
    names = list(scn["wallets"])
    bond_mint, cash_mint = A.pubkey_of(scn["keys"]["bondMint"]), A.pubkey_of(scn["keys"]["cashMint"])

    # Run the full independent audit first (its own measurement), keep its verdicts for the page.
    with contextlib.redirect_stdout(io.StringIO()) as audit_out:
        audit_exit = A.main(scenario_path, rpc_url, "history-accounts")
    audit_checks = [{"id": cid, "label": label, "ok": ok} for cid, label, ok in A.results]

    authority, vault_list = A.bond_vaults(rpc, bond_mint, cash_mint)
    if not vault_list:
        sys.exit("no vault owned by the bond token's mint authority")
    vaults = set(vault_list)
    txs = A.measure_history(rpc, [bond_mint, cash_mint, authority, *vault_list])
    creation_time = A.signatures(rpc, bond_mint)[-1]["blockTime"]
    calc = A.calculate(scn)

    servicing = measure_servicing(rpc, [authority, bond_mint, *sorted(vaults)], vaults)
    vault_event = {x["opened"]["vault"]: x["opened"]["event"] for x in servicing if x["opened"]}

    # Record moments in chain time: creation time + (bond time - bond origin) scaled by the scenario's compression.
    scale = scn["chainSecondsPerBondDay"]
    bond_origin = datetime.fromisoformat(scn["bondTimeAtCreation"]).timestamp()
    rec_h, rec_m = map(int, scn["terms"]["recordTimeLocal"].split(":"))
    offset = scn["terms"]["utcOffsetHours"] * 3600

    def record_bond_time(k):
        y, m, d = map(int, calc["record_dates"][k].split("-"))
        return datetime(y, m, d).timestamp() - datetime(1970, 1, 1).timestamp() + rec_h * 3600 + rec_m * 60 - offset

    records = [{"coupon": k, "payDate": calc["pay_dates"][k],
                "chainTime": creation_time + (record_bond_time(k) - bond_origin) * scale / 86_400}
               for k in range(len(calc["pay_dates"]))]

    # The holders shown one by one: a fixed random sample (seed below) of those who ever held bonds; everyone else is
    # aggregated as "other holders". Every count and check still covers all holders.
    held_any = sorted({wallets[d["owner"]] for t in txs for d in t["deltas"].values()
                       if d["mint"] == bond_mint and d["owner"] in wallets and d["delta"]}, key=names.index)
    sample = sorted(random.Random(SAMPLE_SEED).sample(held_any, min(SAMPLE_SIZE, len(held_any))), key=names.index)
    others = [n for n in names if n not in sample]

    # Replay measured movements in ledger order.
    bonds = {n: 0 for n in names}
    cash = {n: 0 for n in names}
    timeline = []
    events = []
    for t in txs:
        kind, moves = classify(t, bond_mint, cash_mint, wallets, vaults, records[-1]["chainTime"])
        before = dict(bonds)
        for n, m in moves.items():
            bonds[n] += m["bonds"]
            cash[n] += m["cash"]
        if kind == "other":
            continue
        coupon = None
        if kind == "coupon":
            # The coupon a payment belongs to: the event whose vault paid it (from the program's EventOpened log).
            paying = [a for a, d in t["deltas"].items() if a in vault_event and d["delta"] < 0]
            coupon = vault_event[paying[0]] if paying else None
        timeline.append({"t": t["blockTime"], "bonds": {n: bonds[n] for n in sample},
                         "cash": {n: cash[n] / unit for n in sample},
                         "otherBonds": sum(bonds[n] for n in others), "otherCash": sum(cash[n] for n in others) / unit})
        events.append({
            "t": t["blockTime"], "kind": kind, "coupon": coupon, "signature": t["signature"],
            "link": explorer("tx", t["signature"], cluster),
            "moves": {n: {"bonds": m["bonds"], "cash": m["cash"] / unit} for n, m in moves.items()},
            "bondsBefore": {n: before[n] for n in sample},
        })

    # Every coupon x every holder, from measured balances:
    #   expected = coupon per bond (from the terms) x bonds held at the record moment (the depository's 09:00 list);
    #   paid     = cash the holder measurably received for that coupon;
    #   naive    = coupon per bond x bonds held just before that coupon's first payment ("pay whoever holds now").
    t = scn["terms"]
    per_bond = Decimal(t["faceUnits"]) * t["rateBps"] / 10_000 / t["periodsPerYear"] / unit

    def bonds_at(cut, inclusive):
        held = {n: 0 for n in names}
        for e in events:
            if e["t"] <= cut if inclusive else e["t"] < cut:
                for n, m in e["moves"].items():
                    held[n] += m["bonds"]
        return held

    grid = []
    for r in records:
        pays = [e for e in events if e["kind"] == "coupon" and e["coupon"] == r["coupon"]]
        at_record = bonds_at(r["chainTime"], True)
        at_payment = bonds_at(min(e["t"] for e in pays), False) if pays else at_record
        paid = {n: sum(e["moves"].get(n, {}).get("cash", 0) for e in pays) for n in names}
        grid.append({
            "coupon": r["coupon"], "payDate": r["payDate"], "recordTime": r["chainTime"],
            "atRecord": at_record, "atPayment": at_payment, "paid": paid,
            "expected": {n: float(per_bond * at_record[n]) for n in names},
            "naive": {n: float(per_bond * at_payment[n]) for n in names},
        })
    g0 = grid[0]
    comparison = {"perBond": float(per_bond), "atRecord": g0["atRecord"], "atPayment": g0["atPayment"],
                  "naive": g0["naive"], "depository": g0["expected"], "measured": g0["paid"]}
    calculated_final = {n: (sum(a for _, a in calc["coupons"][n]) + sum(c for _, c in calc["retirements"][n])) / unit
                        for n in names}

    data = {
        "scenario": scn["name"],
        "scenarioFile": scenario_path,
        "cluster": cluster,
        "rpc": rpc_url,
        "scale": scale,
        "creationTime": creation_time,
        "programLink": explorer("address", "2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf", cluster),
        "bondMint": bond_mint,
        "bondMintLink": explorer("address", bond_mint, cluster),
        "sample": sample,
        "bondOrigin": bond_origin,
        "holders": [{"name": n, "address": pk, "link": explorer("address", pk, cluster)}
                    for pk, n in wallets.items()],
        "records": records,
        "timeline": timeline,
        "events": events,
        "comparison": comparison,
        "grid": grid,
        "finalCash": {"measured": {n: cash[n] / unit for n in names}, "calculated": calculated_final},
        "audit": {"passed": audit_exit == 0, "checks": audit_checks, "transactions": A.last_audit["transactions"]},
        "servicing": servicing,
        "bondAuthority": authority,
        "handover": scn["handover"],
        "keeperAddress": A.pubkey_of(scn["handover"]["keeper"]) if scn["handover"] else None,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    template = (ROOT / "tools/demo/template.html").read_text()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(template.replace("/*__DATA__*/null", json.dumps(data)))
    # The measured data on its own, so the other builders (tools/demo/build_app.py and the rest) use it without
    # measuring again.
    data_path = Path(out_path).parent / "data" / f"{Path(scenario_path).stem}.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {out_path}: {len(events)} events, audit {'PASSED' if audit_exit == 0 else 'FAILED'}")
    print(json.dumps(comparison, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: build_demo.py <scenarios/devnet-NAME.json> <rpc url> <cluster> <out.html>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
