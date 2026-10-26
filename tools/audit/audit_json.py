"""The ledger-only audit (tools/audit/audit.py) as JSON for the operator screen (docs/demo/operator-screen.html):
every check, and per holder what the audit calculated from the terms and what it measured on the ledger, with the
transactions behind every measured number. Unlike the deck's worksheet it reports a failing audit as it is.

The audit itself does not know which event a payment belongs to (it compares ordered lists); the operator service
passes the signatures of the payments it sent with what each was for (a coupon's index, "redemption" or "offer N"),
and only the labels of measured payments come from that file.

Usage: .venv/bin/python3 tools/audit/audit_json.py <public scenario.json> <rpc url> <payments.json: {signature: label}>
"""
import contextlib
import io
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit as A  # noqa: E402

scenario_path, rpc_url, payments_path = sys.argv[1], sys.argv[2], sys.argv[3]
with contextlib.redirect_stdout(io.StringIO()) as log:
    code = A.main(scenario_path, rpc_url, "history-accounts")
output = log.getvalue()
checks = [{"id": c, "label": l, "ok": ok} for c, l, ok in A.results]
event_of = json.loads(Path(payments_path).read_text())

scn = json.loads(Path(scenario_path).read_text())
t = scn["terms"]
rpc = A.Rpc(rpc_url)
wallets = {A.pubkey_of(f): name for name, f in scn["wallets"].items()}
bond_mint, cash_mint = A.pubkey_of(scn["keys"]["bondMint"]), A.pubkey_of(scn["keys"]["cashMint"])
authority, vaults = A.bond_vaults(rpc, bond_mint, cash_mint)
txs = A.measure_history(rpc, [bond_mint, cash_mint, authority, *vaults])
vault_set = set(vaults)
creation_time = A.signatures(rpc, bond_mint)[-1]["blockTime"]
A.resolve(scn, txs, creation_time)  # operations named by signature: phases from the ledger, as in audit.main
local = timezone(timedelta(hours=t["utcOffsetHours"]))
origin = datetime.fromisoformat(scn["bondTimeAtCreation"]).timestamp()
scale = scn["chainSecondsPerBondDay"]
bond_day = lambda tx: datetime.fromtimestamp(origin + (tx["blockTime"] - creation_time) * 86_400 / scale, local).strftime("%Y-%m-%d")

# Measured, as in audit.main: each holder's bond movements; cash from this bond's vaults (coupons, and redemption
# when the holder's bonds burn in the same transaction).
moves, coupons_in, retired_in = defaultdict(list), defaultdict(list), defaultdict(list)
for tx in txs:
    escrow_side = any(d["mint"] == bond_mint and d["delta"] != 0 and d["owner"] not in wallets for d in tx["deltas"].values())
    for d in tx["deltas"].values():
        h = wallets.get(d["owner"])
        if d["mint"] == bond_mint and h and d["delta"]:
            kind = ("placement" if tx["minted"].get(bond_mint) else "redemption" if tx["burned"].get(bond_mint)
                    else "DvP settlement" if A.DVP_PROGRAM in tx["programs"] else "into DvP escrow" if escrow_side else "transfer")
            moves[h].append({"q": d["delta"], "sig": tx["signature"], "day": bond_day(tx), "kind": kind})
    if A.vault_paid_out(tx, vault_set) <= 0:
        continue
    burned = {wallets[d["owner"]] for d in tx["deltas"].values()
              if d["mint"] == bond_mint and d["delta"] < 0 and tx["burned"].get(bond_mint) and d["owner"] in wallets}
    for d in tx["deltas"].values():
        h = wallets.get(d["owner"])
        if d["mint"] == cash_mint and h and d["delta"] > 0:
            row = {"amount": d["delta"], "sig": tx["signature"], "day": bond_day(tx)}
            (retired_in if h in burned else coupons_in)[h].append(dict(row, label=event_of.get(tx["signature"])))

# Calculated, as in audit.calculate: positions at each record moment from the scenario, amounts from the terms.
calc = A.calculate(scn)
record_times = [A.record_moment_unix(d, t["recordTimeLocal"], t["utcOffsetHours"]) for d in calc["record_dates"]]
tl = A.timeline(scn["placements"], scn["trades"], scn["offers"], record_times)
holders = {}
for h in scn["wallets"]:
    owed = dict(calc["coupons"][h])
    events = [{"kind": "coupon", "k": k, "pos": held.get(h, 0), "expected": owed.get(k, 0), "measured": 0}
              for k, held in enumerate(tl["at_record"])]
    for m in coupons_in[h]:
        k = m["label"]
        if isinstance(k, int) and k < len(events):
            events[k]["measured"] += m["amount"]
            events[k].setdefault("sigs", []).append(m["sig"])
    redeemed = [m for m in retired_in[h] if m["label"] == "redemption"]
    events.append({"kind": "redemption", "pos": tl["final"].get(h, 0), "expected": calc["principal"].get(h, 0),
                   "measured": sum(m["amount"] for m in redeemed), "sigs": [m["sig"] for m in redeemed]})
    # buyback sales and settled tenders: calculated (bonds, cash) in ledger order beside the measured arrivals
    offers = {"expected": [{"bonds": q, "amount": cash} for q, cash in tl["retire"][h]],
              "measured": [m for m in retired_in[h] if m["label"] != "redemption"]}
    holders[h] = {"events": events, "offers": offers, "moves": moves[h],
                  "unlabelled": [m for m in coupons_in[h] if not isinstance(m["label"], int)],
                  "expected": sum(e["expected"] for e in events) + sum(x["amount"] for x in offers["expected"]),
                  "measured": sum(m["amount"] for m in coupons_in[h]) + sum(m["amount"] for m in retired_in[h]),
                  "checks": [c for c in checks if c["label"].startswith(h + ":")]}
print(json.dumps({"passed": code == 0, "checks": checks, "transactions": A.last_audit.get("transactions"),
                  "recordDates": calc["record_dates"], "payDates": calc["pay_dates"], "holders": holders,
                  "output": output}, ensure_ascii=False))
