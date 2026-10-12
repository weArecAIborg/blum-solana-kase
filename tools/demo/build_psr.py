"""Build the problem-solution-result demonstration page from data measured on devnet.

Input: the measured data that tools/demo/build_demo.py writes (docs/demo/data/<scenario>.json): ledger transactions
classified by the token movements the runtime recorded, record moments, and the independent audit's verdicts.
Every number on the page is computed here from that data; nothing is typed in by hand.

Usage: .venv/bin/python3 tools/demo/build_psr.py docs/demo/data/devnet-full8.json docs/demo/data/devnet-keeper.json \
           docs/demo/data/tamper-probe-local.json docs/demo/psr.html
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def held_series(events, name, start, end, origin):
    """Bonds held by `name` from `start` to `end` (chain time), as step points relative to `origin` (seconds)."""
    held = sum(e["moves"].get(name, {}).get("bonds", 0) for e in events if e["t"] < start)
    xs, ys = [start - origin], [held]
    for e in events:
        if start <= e["t"] <= end and e["moves"].get(name, {}).get("bonds", 0):
            held += e["moves"][name]["bonds"]
            xs.append(e["t"] - origin)
            ys.append(held)
    xs.append(end - origin)
    ys.append(held)
    return xs, ys


def card_market_open(D):
    """Card 1: trading stays open through the record moment, and the coupon still follows the record moment."""
    records = [r["chainTime"] for r in D["records"]]
    events = D["events"]
    trades = [e for e in events if e["kind"] == "trade"]
    lags = []
    for e in trades:
        passed = [r for r in records if r <= e["t"]]
        if passed:
            lags.append(e["t"] - passed[-1])
    rec = records[0]
    first = next(e for e in trades if e["t"] >= rec)
    seller = next(n for n, m in first["moves"].items() if m["bonds"] < 0)
    buyer = next(n for n, m in first["moves"].items() if m["bonds"] > 0)
    pay = next(e for e in events if e["kind"] == "coupon" and e["coupon"] == 0 and seller in e["moves"])
    g0 = D["grid"][0]
    cells = [(g, n) for g in D["grid"] for n in g["expected"]]
    exact = sum(1 for g, n in cells if abs(g["paid"][n] - g["expected"][n]) < 1e-9)
    naive_wrong = sum(1 for g, n in cells if abs(g["naive"][n] - g["expected"][n]) > 1e-9)
    start, end = rec - 10, pay["t"] + 8
    sx, sy = held_series(events, seller, start, end, rec)
    bx, by = held_series(events, buyer, start, end, rec)
    return {
        "seller": seller, "buyer": buyer,
        "sold": -first["moves"][seller]["bonds"], "saleLag": round(first["t"] - rec),
        "sellerAtRecord": g0["atRecord"][seller], "buyerAtRecord": g0["atRecord"][buyer],
        "sellerPaid": g0["paid"][seller], "buyerPaid": g0["paid"][buyer], "perBond": D["comparison"]["perBond"],
        "payLag": round(pay["t"] - rec),
        "saleLink": first["link"], "payLink": pay["link"],
        "trades": len(lags), "lagMin": round(min(lags)), "lagMax": round(max(lags)),
        "cells": len(cells), "exact": exact, "naiveWrong": naive_wrong, "couponsPaid": len(D["grid"]),
        "series": {"seller": {"x": sx, "y": sy}, "buyer": {"x": bx, "y": by}},
    }


# Step names for servicing transactions (program instruction as logged -> label).
STEP = {
    "CreateBond": "setup", "InitHookAccounts": "setup", "RegisterHolder": "setup", "Place": "placement",
    "TransferHook": "trade", "OpenEvent": "open event", "ConfirmFunding": "confirm funding", "PayCoupon": "pay coupon",
    "RedeemHolder": "redeem", "CreateOffer": "offer setup", "ConfirmOfferFunding": "offer setup",
    "SellToOffer": "buyback sale", "ChangeTender": "tender", "ResolveTender": "tender settlement", "SettleTender": "tender settlement",
}
STEP_ORDER = ["setup", "placement", "trade", "open event", "issuer cash", "confirm funding", "pay coupon",
              "offer setup", "buyback sale", "tender", "tender settlement", "redeem"]


def step_of(tx):
    if tx["instructions"]:
        return STEP[tx["instructions"][0]]
    return "issuer cash" if tx["funding"] else "setup"  # token-account creation for holders


def explorer(cluster, signature):
    return f"https://explorer.solana.com/tx/{signature}?cluster={cluster}"


def card_nothing_mirrored(D):
    """Card 2: every lifecycle step is a ledger transaction; one coupon from record moment to last holder paid."""
    S = D["servicing"]
    rec = D["records"][0]["chainTime"]
    paid0 = {e["signature"] for e in D["events"] if e["kind"] == "coupon" and e["coupon"] == 0}
    last_pay = max(x["t"] for x in S if x["signature"] in paid0)
    trail = [x for x in S if rec <= x["t"] <= last_pay and step_of(x) in ("open event", "issuer cash", "confirm funding")
             or x["signature"] in paid0]
    origin = min(x["t"] for x in S)
    counts = {}
    for x in S:
        counts[step_of(x)] = counts.get(step_of(x), 0) + 1
    holders0 = sum(1 for e in D["events"] if e["kind"] == "coupon" and e["coupon"] == 0
                   for m in e["moves"].values() if m["cash"] > 0)
    return {
        "coupon0Txs": len(trail), "coupon0Seconds": round(last_pay - rec),
        "coupon0Issuer": sum(1 for x in trail if step_of(x) == "issuer cash"),
        "coupon0Payments": holders0, "holders": len(D["holders"]),
        "trail": [{"step": step_of(x), "s": round(x["t"] - rec), "link": explorer(D["cluster"], x["signature"])} for x in trail],
        "total": len(S), "counts": counts, "order": [k for k in STEP_ORDER if k in counts],
        "points": [{"step": step_of(x), "m": round((x["t"] - origin) / 60, 2)} for x in S],
        "records": [round((r["chainTime"] - origin) / 60, 2) for r in D["records"]],
        "issuerTxs": counts.get("issuer cash", 0),
    }


def card_cost(D):
    """Card 3: what servicing costs on the network, measured from the fee payer's lamports."""
    S = D["servicing"]
    by = {}
    for x in S:
        b = by.setdefault(step_of(x), {"n": 0, "fee": 0, "deposit": 0})
        b["n"] += 1
        b["fee"] += x["fee"]
        b["deposit"] += x["deposit"]
    pay = [x for x in S if step_of(x) == "pay coupon"]
    opens = [x for x in S if step_of(x) == "open event"]
    # Payments are sent several holders per transaction: the cost of paying one holder is the transaction's fee
    # divided by the holders it paid (from the measured cash movements of that transaction).
    paid_in = {e["signature"]: sum(1 for m in e["moves"].values() if m["cash"] > 0) for e in D["events"] if e["kind"] == "coupon"}
    per_holder = [x["fee"] / paid_in[x["signature"]] for x in pay if paid_in.get(x["signature"])]
    rec = D["records"][0]["chainTime"]
    paid0 = {e["signature"] for e in D["events"] if e["kind"] == "coupon" and e["coupon"] == 0}
    c0 = [x for x in S if x["signature"] in paid0 or (rec <= x["t"] <= max(y["t"] for y in S if y["signature"] in paid0)
                                                       and step_of(x) in ("open event", "confirm funding"))]
    return {
        "payFee": sorted({x["fee"] for x in pay}), "payDeposit": sorted({x["deposit"] for x in pay}),
        "perHolderMin": min(per_holder), "perHolderMax": max(per_holder),
        "holdersPerTx": max(paid_in.values()), "payments": sum(paid_in.values()),
        "openDeposit": sorted({x["deposit"] for x in opens}),
        "coupon0Lamports": sum(x["fee"] + x["deposit"] for x in c0), "coupon0Holders": sum(paid_in[s] for s in paid0),
        "totalFees": sum(x["fee"] for x in S), "totalDeposits": sum(x["deposit"] for x in S), "total": len(S),
        "by": by, "order": [k for k in STEP_ORDER if k in by],
    }


def card_operator_failure(K):
    """Card 4: our operator stops after coupon 0; a wallet with no role services the rest, late; amounts exact."""
    S = K["servicing"]
    operator = next(x["feePayer"] for x in S if x["instructions"] == ["CreateBond"])
    keeper = K["keeperAddress"]
    stop = K["handover"]["operatorStopsAfterCoupon"]
    origin = min(x["t"] for x in S)
    serv = [x for x in S if step_of(x) in ("open event", "confirm funding", "pay coupon", "redeem")]
    by_keeper = [x for x in serv if x["feePayer"] == keeper]
    cells = [(g, n) for g in K["grid"] for n in g["expected"]]
    exact = sum(1 for g, n in cells if abs(g["paid"][n] - g["expected"][n]) < 1e-9)
    final_rec = K["records"][-1]["chainTime"]
    coupon_of = {e["signature"]: e["coupon"] for e in K["events"] if e["kind"] == "coupon"}
    pays = [{"row": f"coupon {coupon_of[x['signature']]}", "m": round((x["t"] - origin) / 60, 2),
             "who": "outside wallet" if x["feePayer"] == keeper else "operator"}
            for x in S if x["signature"] in coupon_of]
    pays += [{"row": "redemption", "m": round((x["t"] - origin) / 60, 2),
              "who": "outside wallet" if x["feePayer"] == keeper else "operator"} for x in S if step_of(x) == "redeem"]
    later = [x for x in by_keeper if x["t"] >= final_rec]
    trades_without_operator = sum(1 for x in S if step_of(x) == "trade" and x["t"] > K["records"][stop]["chainTime"] + 1)
    return {
        "operator": operator, "keeper": keeper, "stop": stop, "keeperTxs": len(by_keeper),
        "keeperAllAfterFinal": len(later) == len(by_keeper),
        "keeperFees": sum(x["fee"] + x["deposit"] for x in by_keeper),
        "minutesLate": round((min(x["t"] for x in by_keeper) - K["records"][stop + 1]["chainTime"]) / 60, 1),
        "exact": exact, "cells": len(cells), "tradesWithoutOperator": trades_without_operator,
        "pays": pays, "records": [{"row": f"coupon {i}", "m": round((r["chainTime"] - origin) / 60, 2)}
                                  for i, r in enumerate(K["records"])],
        "keeperLink": f"https://explorer.solana.com/address/{keeper}?cluster={K['cluster']}",
        "audit": {"passed": sum(1 for c in K["audit"]["checks"] if c["ok"]), "checks": len(K["audit"]["checks"])},
    }


def card_audit(D, probe):
    """Card 5: an outside auditor recomputes everything from public data; a one-unit tampering is caught."""
    checks = D["audit"]["checks"]
    return {
        "passed": sum(1 for c in checks if c["ok"]), "checks": len(checks), "transactions": D["audit"]["transactions"],
        "modelFree": sum(1 for c in checks if c["id"].startswith("M")),
        "vsTerms": sum(1 for c in checks if c["id"].startswith("C")),
        "measured": {n: D["finalCash"]["measured"][n] for n in D["sample"]},
        "calculated": {n: D["finalCash"]["calculated"][n] for n in D["sample"]},
        "holders": len(D["holders"]),
        "holdersExact": sum(1 for n, v in D["finalCash"]["measured"].items() if abs(v - D["finalCash"]["calculated"][n]) < 1e-9),
        "probe": probe["probe"], "probeBefore": probe["before"], "probeAfter": probe["after"],
        "probeNetwork": "local network",
    }


def lifecycle(D):
    """The bond's life for the sampled holders: holdings and cash received after every ledger event that changed
    them, with bond dates, record moments and what happened. The rest of the holders are one aggregate."""
    origin, scale = D["creationTime"], D["scale"]
    bond_date = lambda t: datetime.fromtimestamp(D["bondOrigin"] + (t - origin) * 86_400 / scale, timezone.utc).strftime("%Y-%m-%d")
    sample = D["sample"]
    steps, last = [], None
    for point, event in zip(D["timeline"], D["events"]):
        state = (tuple(point["bonds"][n] for n in sample), tuple(round(point["cash"][n], 6) for n in sample))
        if state == last:
            continue
        last = state
        movers = [n for n in sample if n in event["moves"]]
        steps.append({"date": bond_date(point["t"]), "kind": event["kind"], "movers": movers,
                      "bonds": [point["bonds"][n] for n in sample], "cash": [point["cash"][n] for n in sample],
                      "otherBonds": point["otherBonds"], "m": round((point["t"] - origin) / 60, 2)})
    return {
        "sample": sample, "holders": len(D["holders"]), "steps": steps,
        "records": [{"date": bond_date(r["chainTime"]), "m": round((r["chainTime"] - origin) / 60, 2)} for r in D["records"]],
        "series": {n: {"m": [s["m"] for s in steps], "bonds": [s["bonds"][i] for s in steps]} for i, n in enumerate(sample)},
    }


def main(data_path, keeper_path, probe_path, out_path):
    D = json.loads(Path(data_path).read_text())
    K = json.loads(Path(keeper_path).read_text())
    probe = json.loads(Path(probe_path).read_text())
    page = {
        "cluster": D["cluster"], "programLink": D["programLink"], "bondMint": D["bondMint"], "bondMintLink": D["bondMintLink"],
        "keeperBondMintLink": K["bondMintLink"],
        "audit": {"passed": sum(1 for c in D["audit"]["checks"] if c["ok"]), "checks": len(D["audit"]["checks"]),
                  "transactions": D["audit"]["transactions"]},
        "generated": D["generated"], "source": [str(Path(data_path)), str(Path(keeper_path)), str(Path(probe_path))],
        "life": lifecycle(D),
        "card1": card_market_open(D),
        "card2": card_nothing_mirrored(D),
        "card3": card_cost(D),
        "card4": card_operator_failure(K),
        "card5": card_audit(D, probe),
    }
    template = (ROOT / "tools/demo/psr_template.html").read_text()
    Path(out_path).write_text(template.replace("/*__PAGE__*/null", json.dumps(page)))
    print(f"wrote {out_path}")
    skip = ("series", "points", "trail", "pays", "records", "by", "steps", "measured", "calculated")
    for k in ("card1", "card2", "card3", "card4", "card5"):
        print(k, json.dumps({a: b for a, b in page[k].items() if a not in skip}))


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: build_psr.py <data/devnet-full8.json> <data/devnet-keeper.json> <data/tamper-probe-local.json> <out.html>",
              file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
