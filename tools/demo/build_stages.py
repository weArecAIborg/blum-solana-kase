"""Build the stage-by-stage comparison (docs/demo/stages.html): for each stage of a bond's life, the textbook bond
(left) beside the same stage tokenized on Solana (right), each with its own picture, and the outcome they share.

Left side: what corporate-finance practice says, with its source (docs/tradfi-vs-onchain.md, docs/bond-mechanics.md).
Right side: what the program did, measured on devnet. Every number is computed here from the measured data.

Inputs (all measured): docs/demo/data/devnet-crowd-1000.json, devnet-keeper.json, tamper-probe-local.json,
devnet-hostile-attempts.json.
Auditor supplement required beside the output: data/stages-probe-ledger.json (cached finalized probe responses).

Usage: .venv/bin/python3 tools/demo/build_stages.py docs/demo/data/devnet-crowd-1000.json \
           docs/demo/data/devnet-keeper.json docs/demo/data/tamper-probe-local.json \
           docs/demo/data/devnet-hostile-attempts.json docs/demo/stages.html
"""

import json
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/demo"))
from build_psr import card_audit, card_market_open  # noqa: E402
from stages_audit import build_evidence, RULES  # noqa: E402


# ---------- HTML: every text is written into the file; the page's script draws only the pictures ----------

def esc(s):
    return escape(str(s), quote=True)


def tx(href, label="transaction", evidence=None, cls="", chip=True):
    classes = " ".join(c for c in ("bs-proof" if chip else "", "audit-link" if evidence else "", cls) if c)
    attrs = (f' data-evidence="{esc(evidence)}"' if evidence else "") + (f' class="{esc(classes)}"' if classes else "")
    return f'<a href="{esc(href)}" target="_blank" rel="noopener"{attrs}>{esc(label)}</a>'


def proof(evidence, key, label, cls="", chip=True):
    return tx(evidence[key]["refs"][0]["href"], label, key, cls, chip)


# Stage names, as in the Claude Design stages kit: the strip "1 Issue" ... "9 Audit", headings "Record: ...".
NAMES = {"issue": "Issue", "trade": "Trade", "record": "Record", "calculate": "Calculate", "fund": "Fund", "pay": "Pay",
         "redeem": "Redeem", "offer": "Offer", "audit": "Audit"}


def index_html(stages):
    return "".join(f'<a href="#{s["id"]}" data-id="{s["id"]}"{" aria-current=\"true\"" if i == 0 else ""}>{i + 1} {NAMES[s["id"]]}</a>'
                   for i, s in enumerate(stages))


def record_tables(d, evidence):
    """Stage 3 as the kit lays it out: the 09:00 list (textbook) and the ledger entries around it (on Solana)."""
    after_s, after_b = d["sellerAtRecord"] - d["sold"], d["buyerAtRecord"] + d["sold"]
    book = (f'<table class="bs-register"><thead><tr><th>Holder</th><th class="r">Bonds at 09:00</th></tr></thead><tbody>'
            f'<tr><td>{esc(d["seller"])}</td><td class="r">{d["sellerAtRecord"]}</td></tr>'
            f'<tr><td>{esc(d["buyer"])}</td><td class="r">{d["buyerAtRecord"]}</td></tr></tbody></table>')
    chain = (f'<table class="bs-ledger"><thead><tr><th>Time</th><th>Entry</th><th class="r">{esc(d["seller"])}</th>'
             f'<th class="r">{esc(d["buyer"])}</th></tr></thead><tbody>'
             f'<tr><td>09:00:00</td><td>record moment 0</td><td class="r">{d["sellerAtRecord"]}</td><td class="r">{d["buyerAtRecord"]}</td></tr>'
             f'<tr class="hl"><td>+{d["saleLag"]} s</td><td>hook: positions saved for 09:00</td><td class="r">{d["sellerAtRecord"]}</td>'
             f'<td class="r">{d["buyerAtRecord"]}</td></tr>'
             f'<tr><td>+{d["saleLag"]} s</td><td>transfer {d["sold"]} → {esc(d["buyer"])}</td><td class="r">{after_s}</td>'
             f'<td class="r">{after_b}</td></tr></tbody></table>')
    links = f'<p class="bs-links">{proof(evidence, d["saleKey"], "the sale")} {proof(evidence, d["payKey"], "the payment")}</p>'
    return book, chain + links


def receipts(d):
    """Stage 4 as the kit lays it out: the same sum on a coupon statement and in the cash token's smallest unit."""
    units = 10 ** d["decimals"]
    row = lambda k, v, cls="": f'<div class="bs-rl{cls}"><span>{esc(k)}</span><span>{esc(v)}</span></div>'  # noqa: E731
    book = ('<div class="bs-receipt book">' + row("Holding", f'{d["bonds"]} bonds') + row("Face value", f'{d["face"]:,.2f}')
            + row("Interest", f'{d["rateBps"] / 100:g}% a year') + row("Payments a year", d["perYear"])
            + row("Coupon", f'{d["bonds"]} × {d["face"]:,.0f} × {d["rateBps"] / 100:g}% / {d["perYear"]}')
            + row("Rounded down to", "0.01") + row("Owed", f'{d["paid"]:,.2f}', " total") + "</div>")
    chain = ('<div class="bs-receipt chain">' + row("position", d["bonds"]) + row("face_units", f'{d["faceUnits"]:,}')
             + row("rate_bps", d["rateBps"]) + row("periods", d["perYear"])
             + row("amount", f'{d["bonds"]} x {d["faceUnits"]:,} x {d["rateBps"]} / 10,000 / {d["perYear"]}')
             + row("units", f'{round(d["paid"] * units):,}') + row("paid", f'{d["paid"]:.{d["decimals"]}f}', " total")
             + row("QuantLib, FinancePy", f'{d["paid"]:,.2f}', " check") + "</div>")
    return book, chain


def screens_html(stages, evidence):
    def refused(rows):
        return f"""<ul class="bs-ref">{"".join(f'''<li><span class="bs-mark refused">Refused</span>
            <span>{esc(r["actor"][0].upper() + r["actor"][1:])} {esc(r["tries"])} · {tx(r["explorer"], 'transaction', 'attack:' + r['id'])}</span></li>''' for r in rows)}</ul>""" \
            if rows else ""

    def bodies(i, s):
        if s["id"] == "record":
            return record_tables(s["data"], evidence)
        if s["id"] == "calculate":
            return receipts(s["data"])
        return (f'<div class="viz" data-stage="{i}" data-side="left"></div>', f'<div class="viz" data-stage="{i}" data-side="right"></div>')

    def sim(s):
        if s["id"] != "record":
            return ""
        d = s["data"]
        return (f'<p class="bs-sim"><b>Simulated</b><span>Devnet time is compressed: 1 bond day = {d["scale"]} chain seconds, '
                f'so “{d["saleLag"]} s after 09:00” is {d["saleLag"]} chain seconds.</span></p>')

    out = []
    for i, s in enumerate(stages):
        left, right = bodies(i, s)
        out.append(f"""
  <section class="bs-screen" id="{s["id"]}">
    <div class="bs-sh"><span class="no">{i + 1} / {len(stages)}</span><h2>{NAMES[s["id"]]}: {esc(s["title"][0].lower() + s["title"][1:])}</h2></div>
    <div class="bs-lr">
      <div class="bs-side book"><span class="label">Textbook</span>
        {left}
        <p class="cap">{esc(s["left"]["caption"])}</p><p class="src">{esc(s["left"]["source"])} ·
          {" · ".join(tx(url, name, 'rule:' + s['id'], chip=False) for name, url in RULES[s['id']])}</p></div>
      <div class="bs-side chain"><span class="label">On Solana, measured</span>
        {right}
        <p class="cap">{esc(s["right"]["caption"])}</p>
        <div class="bs-value">{proof(evidence, 'stage:' + s['id'], s['right']['value'], 'metric-link', chip=False)}</div>
        {refused(s["right"]["refused"])}
        <p class="bs-links">{proof(evidence, 'stage:' + s['id'], 'inspect evidence')}
          {tx('data/devnet-crowd-1000.json', 'measured data', 'stage:' + s['id'])}
          {proof(evidence, 'keeper', 'outside keeper') if s['id'] == 'pay' else ''}
          {tx('data/devnet-hostile-attempts.json', f'all {s["right"]["probes"]} probes', 'stage:audit') if s['id'] == 'audit' else ''}</p></div>
    </div>
    <p class="bs-now ok"><b>Same outcome</b><span>{esc(s["verdict"])}</span></p>
    {sim(s)}
  </section>""")
    return "".join(out)


def foot_html(page):
    return (f'Program {tx(page["programLink"], "2UkTKYYz...8sHf", "program")} on Solana devnet. Right-hand numbers are measured\n'
            f'  from the ledger (1000-holder run, {esc(page["generated"])}; attacks from the attacker run). Left-hand rules: '
            f'{tx("audit/source/docs/tradfi-vs-onchain.md.html", "TradFi comparison", "rule:issue")}, '
            f'{tx("audit/source/docs/bond-mechanics.md.html", "bond mechanics", "rule:issue")}. '
            f'{tx("audit/evidence.json", "Auditor evidence index", "stage:audit")} · '
            f'{tx("audit/review.html", "How the probes work and what they prove")}')


def fill(template, parts):
    for key, html in parts.items():
        marker = f"<!--__{key}__-->" if key != "PAGE" else "/*__PAGE__*/null"
        assert template.count(marker) == 1, f"template must hold {marker} once"
        template = template.replace(marker, html)
    return template


def main(crowd_path, keeper_path, probe_path, hostile_path, out_path):
    D = json.loads(Path(crowd_path).read_text())
    K = json.loads(Path(keeper_path).read_text())
    probe = json.loads(Path(probe_path).read_text())
    H = json.loads(Path(hostile_path).read_text())
    scn = json.loads((ROOT / D["scenarioFile"]).read_text())
    T = scn["terms"]
    unit = 10 ** T["cashDecimals"]
    face = int(T["faceUnits"]) / unit
    per_bond = D["comparison"]["perBond"]
    c1, c5 = card_market_open(D), card_audit(D, probe)
    att = {a["id"]: a for a in H["attempts"]}
    failing = [c["id"] for c in D["audit"]["checks"] if not c["ok"]] + [a["id"] for a in H["attempts"] if not a["byGuard"]]
    if failing or c1["exact"] != c1["cells"]:
        raise SystemExit(f"not building: failing {failing}, exact {c1['exact']} of {c1['cells']}")

    records = [r["chainTime"] for r in D["records"]]
    events = D["events"]
    tender = next(o for o in scn["offers"] if o["kind"] == "tender")
    buyback = next(o for o in scn["offers"] if o["kind"] == "buyback")
    tender_price = int(tender["priceUnits"]) / unit

    # Bonds over the bond's life (supply) and what retired them.
    supply, s_t, s_v, retired = 0, [], [], {"buyback": 0, "tender": 0, "redemption": 0}
    for e in events:
        change = sum(m["bonds"] for m in e["moves"].values()) if e["kind"] != "trade" else 0
        if not change:
            continue
        supply += change
        s_t.append(e["t"])
        s_v.append(supply)
        if e["kind"] == "buyback":
            for m in e["moves"].values():
                if m["bonds"]:
                    retired["tender" if round(m["cash"] / -m["bonds"], 6) == tender_price else "buyback"] -= m["bonds"]
        elif e["kind"] == "redemption":
            retired["redemption"] -= change
    placed = sum(m["bonds"] for e in events if e["kind"] == "placement" for m in e["moves"].values())

    # Trades: seconds after the record moment before them (bond days at the run's scale).
    lags = []
    for e in events:
        if e["kind"] == "trade":
            passed = [r for r in records if r <= e["t"]]
            if passed:
                lags.append(round(e["t"] - passed[-1], 1))

    # Coupon 0: every payment transaction, seconds after the first one; holders paid in each.
    c0 = [e for e in events if e["kind"] == "coupon" and e["coupon"] == 0]
    t_first = min(e["t"] for e in c0)
    pay_batches = sorted((e["t"] - t_first, sum(1 for m in e["moves"].values() if m["cash"] > 0)) for e in c0)
    supply0 = sum(D["grid"][0]["atRecord"].values())

    # The listing's example holder through redemption.
    bh1_red = next(e for e in events if e["kind"] == "redemption" and "BH#1" in e["moves"])
    # Tender: who tendered (the scenario's instructions), what was accepted (measured), coupon in the window.
    tendered_by = {}
    for a in tender["actions"]:
        tendered_by[a["holder"]] = tendered_by.get(a["holder"], 0) + a.get("tender", 0) - a.get("withdraw", 0)
    tendered_by = {h: q for h, q in tendered_by.items() if q > 0}
    accepted_by = {}
    for e in events:
        if e["kind"] == "buyback":
            for h, m in e["moves"].items():
                if m["bonds"] and round(m["cash"] / -m["bonds"], 6) == tender_price:
                    accepted_by[h] = accepted_by.get(h, 0) - m["bonds"]
    window = tender["close"]["recordMoment"]
    kept_coupon = sum(1 for h in tendered_by if D["grid"][window]["paid"].get(h, 0) > 0)
    total_tendered = sum(tendered_by.values())
    holders_tender = sorted(tendered_by, key=lambda h: -tendered_by[h])

    keeper_exact = sum(1 for g in K["grid"] for n in g["expected"] if abs(g["paid"][n] - g["expected"][n]) < 1e-9)
    keeper_cells = sum(len(g["expected"]) for g in K["grid"])
    serviced = [s for s in H["serviced"] if s["cashTo"]]
    evidence, payment_events, sources = build_evidence(D, K, probe, H, scn, Path(out_path).parent)

    stages = [
        {"id": "issue", "title": "A fixed supply, on a known register",
         "left": {"caption": "The issuer sells bonds on fixed terms. The central securities depository keeps the register "
                             "of who holds them.",
                  "source": "SCoRE glossary: the CSD ensures the integrity of securities issues"},
         "right": {"caption": "The terms live in the bond's account on chain. Only the program can mint, and only before "
                              "the first record moment. The token has no freeze authority.",
                   "value": f"{placed:,} bonds, {len(D['holders']):,} holders",
                   "refused": [att[i] for i in ("mint-bonds", "place-late", "place-beyond-issue", "register-self")]},
         "verdict": f"Same register: {placed:,} bonds placed with {len(D['holders']):,} holders; nobody else can add one.",
         "draw": "certificate"},
        {"id": "trade", "title": "The securities leg settles in one transaction",
         "left": {"caption": "A trade settles a settlement cycle later. If it is still pending on the record date, the "
                             "coupon goes to the seller and is then reclaimed: a market claim.",
                  "source": "AFME Market Claims, standard 1b; T2S market claims standards"},
         "right": {"caption": "Bond-token transfer and the register update are one transaction. No securities transfer "
                              "is left pending at the record moment. This run does not demonstrate a cash trade leg.",
                   "value": f"{len(lags)} trades, 0 pending",
                   "refused": [att[i] for i in ("send-unregistered", "skip-hook", "take-bonds")]},
         "verdict": "Same register after every trade; no market claims to process.",
         "draw": "settlement", "data": {"lags": lags, "scale": D["scale"]}},
        {"id": "record", "title": "Who held the bond at 09:00",
         "left": {"caption": "On the record date the depository strikes the settled positions, and the coupon follows that "
                             "list whatever trades come after. Sample bond: the list is taken at 09:00 on payment day "
                             "while trades keep being registered.",
                  "source": "SCoRE and AFME glossaries, record date; sample: Kazakhstan CSD rules for government bonds, "
                            "art. 8 p. 6"},
         "right": {"caption": f"No list is taken. {c1['seller']}'s sale {c1['saleLag']} s after 09:00 first saved the "
                              f"{c1['sellerAtRecord']} bonds it held at 09:00; the coupon used that.",
                   "value": f"{c1['exact']:,} of {c1['cells']:,} exact",
                   "refused": [att["forge-position"]]},
         "verdict": f"Same entitlement: {c1['seller']} paid on {c1['sellerAtRecord']}, {c1['buyer']} on "
                    f"{c1['buyerAtRecord']}, as the 09:00 list says.",
         "draw": "record", "data": {"series": c1["series"], "seller": c1["seller"], "buyer": c1["buyer"], "saleLag": c1["saleLag"],
                                    "sellerAtRecord": c1["sellerAtRecord"], "buyerAtRecord": c1["buyerAtRecord"],
                                    "sellerPaid": c1["sellerPaid"], "buyerPaid": c1["buyerPaid"],
                                    "saleLink": c1["saleLink"], "payLink": c1["payLink"], "sold": c1["sold"], "scale": D["scale"]}},
        {"id": "calculate", "title": "The coupon statement",
         "left": {"caption": "Coupon = holding x face x rate / payments a year, on the holder's total holding, rounded "
                             "down to the currency's smallest unit.",
                  "source": "SCoRE, standard 4; government bond terms: S = N x C"},
         "right": {"caption": "The program computes it in integers of the cash token's smallest unit, rounded down once "
                              "per holder. How we checked: we read every transaction of this bond from the public devnet "
                              "RPC (the data Solana Explorer shows), add up the token balance changes each one recorded, "
                              f"and get each holder's bonds at the record moment and the cash it received. Bonds x "
                              f"{per_bond:,.2f} = cash received. QuantLib and FinancePy, run on the bond's terms, give the "
                              "same amounts.",
                   "value": f"{c1['exact']:,} of {c1['cells']:,} equal",
                   "refused": [att["late-buyer"]]},
         "verdict": f"Same amount: BH#1, {D['grid'][0]['atRecord']['BH#1']} bonds, {D['grid'][0]['paid']['BH#1']:,.2f}.",
         "draw": "receipt",
         "data": {"bonds": D["grid"][0]["atRecord"]["BH#1"], "face": face, "faceUnits": int(T["faceUnits"]), "rateBps": T["rateBps"],
                  "perYear": T["periodsPerYear"], "decimals": T["cashDecimals"], "paid": D["grid"][0]["paid"]["BH#1"]}},
        {"id": "fund", "title": "No payment until all the money is there",
         "left": {"caption": "Holders are paid on the payment date by a set hour; SCoRE sets 12:00 for European currencies. "
                             "Sample bond: the issuer pays the whole amount to the CSD by 16:00, and the CSD pays holders "
                             "by 17:00.",
                  "source": "SCoRE, standard 8; sample: Kazakhstan CSD rules, art. 8 p. 6-7"},
         "right": {"caption": "Each coupon has its own vault and a budget fixed by the supply at 09:00. Anyone may confirm "
                              "the funding; it is refused until the vault covers the budget.",
                   "value": f"{supply0 * per_bond:,.2f} budget",
                   "refused": [att[i] for i in ("confirm-short", "pay-unfunded", "drain-vault")]},
         "verdict": f"Same rule: coupon 0's budget, {supply0:,} bonds x {per_bond:,.2f} = {supply0 * per_bond:,.2f}, "
                    f"fully in the vault before anyone is paid.",
         "draw": "vault", "data": {"budget": supply0 * per_bond, "supply": supply0, "perBond": per_bond,
                                   "short": att["confirm-short"]["explorer"]}},
        {"id": "pay", "title": "Every holder paid, exactly once",
         "left": {"caption": "On the payment date every holder on the record-date list is credited its holding x the rate, "
                             "once. Sample bond: the CSD pays by 17:00.",
                  "source": "SCoRE event INTR; sample: Kazakhstan CSD rules, art. 8 p. 6"},
         "right": {"caption": "Any wallet may pay any holder, in any order. Payment and the holder's paid flag for that "
                              "coupon are one transaction, so it cannot happen twice.",
                   "value": f"{len(pay_batches)} transactions, {sum(n for _, n in pay_batches):,} holders",
                   "refused": [att[i] for i in ("pay-twice", "redirect-coupon", "other-vault")]},
         "verdict": f"Same result: {sum(n for _, n in pay_batches):,} holders paid coupon 0 once; when our operator stopped, "
                    f"a wallet with no role paid the rest ({keeper_exact} of {keeper_cells} exact).",
         "draw": "waffle", "data": {"batches": pay_batches, "keeperLink": f"https://explorer.solana.com/address/{K['keeperAddress']}?cluster={K['cluster']}"}},
        {"id": "redeem", "title": "Burned the moment the principal is paid",
         "left": {"caption": "Trading stops before the record date; at maturity the face value is paid and the bonds are "
                             "written off, cash and securities at the same time. Sample bond: on redemption day the CSD "
                             "stops registering trades, takes the 09:00 list, pays and writes the bonds off.",
                  "source": "AFME, mandatory reorganisations, standard 12; SCoRE, standard 8; sample: Kazakhstan CSD "
                            "rules, art. 10 p. 6"},
         "right": {"caption": "Trading closes at the final record moment. One transaction burns the holder's bonds and pays "
                              "the principal: neither can happen without the other.",
                   "value": f"{retired['redemption']:,} bonds burned, supply 0",
                   "refused": [att[i] for i in ("redeem-twice", "redirect-principal", "trade-at-maturity")]},
         "verdict": f"Same end: every bond retired against cash; supply {placed:,} to 0.",
         "draw": "supply", "data": {"t": s_t, "v": s_v, "records": records, "retired": retired, "placed": placed,
                                    "bh1": {"burned": -bh1_red["moves"]["BH#1"]["bonds"], "cash": bh1_red["moves"]["BH#1"]["cash"],
                                            "link": bh1_red["link"]}}},
        {"id": "offer", "title": "Tendered bonds stay put and keep their coupon",
         "left": {"caption": "In an issuer bid or tender offer, holders' instructions travel up the custody chain to the "
                             "issuer before a deadline. Bonds bought back are redeemed.",
                  "source": "ISO 20022 events BIDS and TEND; sample: Kazakhstan government bond rules, p. 16"},
         "right": {"caption": "Holders tender directly. Tendered bonds stay in their own accounts, locked, so a coupon "
                              "whose record moment falls in the window is still theirs.",
                   "value": f"{kept_coupon} of {len(tendered_by)} kept coupon {window}",
                   "refused": [att[i] for i in ("sell-tendered", "over-maximum", "sell-others-bonds")]},
         "verdict": f"Same end for bonds bought back: retired against cash. {sum(accepted_by.values())} of {total_tendered} "
                    f"tendered accepted (maximum {tender['maxQuantity']}), {retired['buyback']} bought first come (maximum "
                    f"{buyback['maxQuantity']}).",
         "draw": "tender", "data": {"tendered": [tendered_by[h] for h in holders_tender],
                                    "accepted": [accepted_by.get(h, 0) for h in holders_tender],
                                    "max": tender["maxQuantity"], "window": window}},
        {"id": "audit", "title": "Anyone can check every unit",
         "left": {"caption": "Holdings sit in tiers: the CSD knows its participants, and each level informs the next "
                             "until the information reaches the end investor.",
                  "source": "AFME, standard 9; T2S note on the cascade"},
         "right": {"caption": "A ledger-only audit reads the public ledger and recomputes payments from the terms. "
                              "Tamper test, on a separate local run: 0.000001 put into a vault from outside. The audit "
                              f"caught it: {len(probe['after']['failed'])} of its {probe['after']['checks']} checks "
                              "failed. That test is not a devnet exploit.",
                   "value": f"{c5['passed']:,} of {c5['checks']:,} checks",
                   "refused": [], "probes": len(H["attempts"])},
         "verdict": f"Same books, open to anyone: {c5['holdersExact']:,} of {c5['holders']:,} holders' cash equals the "
                    f"terms to the unit.",
         "draw": "checks", "data": {"passed": c5["passed"], "checks": c5["checks"], "probeFailed": len(probe["after"]["failed"]),
                                    "probeChecks": probe["after"]["checks"]}},
    ]
    for stage in stages:
        if stage["id"] == "trade":
            stage["data"]["tradeKeys"] = [f'tx:{e["signature"]}' for e in events if e["kind"] == "trade" and any(r <= e["t"] for r in records)]
        elif stage["id"] == "pay":
            stage["data"]["cells"] = [{"t": e["t"] - t_first, "key": f"paid:{name}"} for e in sorted(payment_events, key=lambda x: x["t"])
                                      for name, m in e["moves"].items() if m["cash"] > 0]
        elif stage["id"] == "offer":
            stage["data"]["holders"] = holders_tender
        elif stage["id"] == "record":
            stage["data"]["saleKey"] = next(key for key, value in evidence.items() if key.startswith("tx:") and value["refs"][0]["href"] == c1["saleLink"])
            stage["data"]["payKey"] = next(key for key, value in evidence.items() if key.startswith("tx:") and value["refs"][0]["href"] == c1["payLink"])
    # Embed only evidence used by this page; the full registry remains available as a linked artifact.
    keys = {"program", "keeper"} | {f"{kind}:{s['id']}" for s in stages for kind in ("stage", "rule")}
    keys |= {f"attack:{a['id']}" for a in H["attempts"]}
    keys |= {key for s in stages for key in s.get("data", {}).get("tradeKeys", [])}
    keys |= {c["key"] for s in stages for c in s.get("data", {}).get("cells", [])}
    keys |= {f"offer:{name}" for name in holders_tender}
    keys |= {f"check:{i}" for i in range(len(D["audit"]["checks"]))}
    keys |= {s["data"][key] for s in stages for key in ("saleKey", "payKey") if key in s.get("data", {})}
    keys |= {evidence[key]["related"] for key in list(keys) if evidence[key].get("related")}
    previews = {key: evidence[key] for key in sorted(keys)}
    # Repeated source/data URLs are stored once, rather than once per holder/assertion.
    references, ref_ids = [], {}
    for key, entry in previews.items():
        entry = dict(entry)
        ids = []
        for r in entry["refs"]:
            identity = (r["label"], r["href"])
            if identity not in ref_ids:
                ref_ids[identity] = len(references)
                references.append(r)
            ids.append(ref_ids[identity])
        entry["refs"] = ids
        previews[key] = entry
    page = {"stages": stages, "programLink": D["programLink"], "generated": D["generated"],
            "terms": {"face": face, "ratePct": T["rateBps"] / 100, "perYear": T["periodsPerYear"], "issue": T["issue"],
                      "maturity": T["maturity"], "payDates": [g["payDate"] for g in D["grid"]]},
            "serviced": len(serviced), "evidence": previews, "references": references}
    template = (ROOT / "tools/demo/stages_template.html").read_text()
    safe_json = json.dumps(page, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = fill(template, {"INDEX": index_html(stages), "SCREENS": screens_html(stages, evidence),
                           "FOOT": foot_html(page), "PAGE": safe_json})
    Path(out_path).write_text("\n".join(line.rstrip() for line in html.splitlines()) + "\n")
    print(f"wrote {out_path}: {len(stages)} stages")


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print("usage: build_stages.py <crowd.json> <keeper.json> <tamper-probe.json> <hostile-attempts.json> <out.html>",
              file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
