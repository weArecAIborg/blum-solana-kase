"""Build the stakeholder page (docs/demo/index.html) from data measured on devnet.

Each party to a bond expects something from its servicing. For each party the page shows what it expects, kept
(measured on devnet), and what was tried against it and refused (failed transactions on devnet).

Inputs, all measured:
  docs/demo/data/devnet-crowd-1000.json   1000-holder run (tools/demo/build_demo.py)
  docs/demo/data/devnet-keeper.json       operator stops after coupon 0 (tools/demo/build_demo.py)
  docs/demo/data/tamper-probe-local.json  one unit sent into a vault from outside (local network)
  docs/demo/data/devnet-hostile-attempts.json  attacker run (tools/demo/measure_attempts.py)
Every number on the page is computed here from that data.

Usage: .venv/bin/python3 tools/demo/build_app.py docs/demo/data/devnet-crowd-1000.json docs/demo/data/devnet-keeper.json \
           docs/demo/data/tamper-probe-local.json docs/demo/data/devnet-hostile-attempts.json docs/demo/index.html
"""

import json
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/demo"))
from build_psr import card_audit, card_cost, card_market_open, card_nothing_mirrored, card_operator_failure  # noqa: E402

# What stopped each attempt, in plain words (the measured reason from the transaction's logs is shown beside it).
STOPPED_BY = {
    "register-self": "only the registrar registers holders",
    "mint-bonds": "only the program mints bonds",
    "mint-as-program": "only the program signs as the program",
    "take-bonds": "only the owner moves its bonds",
    "take-as-delegate": "only the program acts as permanent delegate",
    "burn-bonds": "standard burns are disabled for this bond",
    "burn-permissioned": "only the program is burn authority",
    "burn-as-program": "only the program signs as burn authority",
    "freeze-holder": "the bond has no freeze authority",
    "send-unregistered": "bonds go only to registered holders",
    "second-account": "one bond account per holder",
    "skip-hook": "every transfer must pass the program",
    "forge-position": "the program's hook runs only inside a real transfer",
    "take-program": "only the upgrade authority changes the program",
    "fee-cash-token": "cash tokens that change amounts are refused",
    "place-late": "the issue closes at the first record moment",
    "delegate-cash-token": "cash tokens with a permanent delegate are refused",
    "place-beyond-issue": "placement stops at the issue size",
    "confirm-short": "funding must cover the whole budget",
    "pay-unfunded": "no payment before full funding",
    "drain-vault": "a vault pays only through the program",
    "drain-as-program": "only the program signs for a vault",
    "redirect-coupon": "cash goes only to the holder's own account",
    "claim-as-owner": "a holder record belongs to one wallet",
    "pay-twice": "one payment per holder per coupon",
    "late-buyer": "the coupon follows the record moment",
    "other-vault": "each payment has its own vault",
    "frozen-cash": "the bank's freeze holds; the others were paid first",
    "sell-others-bonds": "only the holder sells its bonds",
    "over-maximum": "an offer buys up to its maximum",
    "sell-tendered": "tendered bonds are locked",
    "trade-at-maturity": "trading closes at the final record moment",
    "redirect-principal": "principal goes only to the holder's own account",
    "coupon-from-principal": "principal cash pays only principal",
    "redeem-twice": "redeemed bonds are burned",
}

# Whose loss each attempt is about; every attempt belongs to exactly one party.
PARTY_ATTEMPTS = {
    "holder": ["take-bonds", "take-as-delegate", "burn-bonds", "burn-permissioned", "burn-as-program", "freeze-holder",
               "redirect-coupon", "claim-as-owner", "redirect-principal", "sell-others-bonds", "delegate-cash-token"],
    "issuer": ["mint-bonds", "mint-as-program", "place-late", "place-beyond-issue", "pay-twice", "late-buyer", "redeem-twice", "other-vault",
               "coupon-from-principal", "over-maximum", "sell-tendered", "drain-vault", "drain-as-program"],
    "depository": ["register-self", "send-unregistered", "second-account", "skip-hook", "forge-position",
                   "confirm-short", "pay-unfunded", "take-program"],
    "bank": ["fee-cash-token", "frozen-cash"],
    "exchange": ["trade-at-maturity"],
    "auditor": [],
}


def link(label, href):
    return {"label": label, "href": href}


def item(value, detail, links=()):
    return {"value": value, "detail": detail, "links": list(links)}


# ---------- HTML: every text of every view is written into the file; the page's script only shows one view ----------

def esc(s):
    return escape(str(s), quote=True)


def a(l):
    target = "" if l["href"].startswith("#") else ' target="_blank" rel="noopener"'
    return f'<a class="bs-proof" href="{esc(l["href"])}"{target}>{esc(l["label"])}</a>'


def links_html(links):
    return f'<div class="bs-links">{"".join(a(l) for l in links)}</div>' if links else ""


def attempt_rows(rows):
    return "".join(f"""<li><details class="bs-attempt">
  <summary><span class="bs-mark {"refused" if t["refused"] else "accepted"}">{"Refused" if t["refused"] else "Accepted"}</span>
    <span class="what"><b>{esc(t["actor"][0].upper() + t["actor"][1:])}</b> {esc(t["tries"])}</span>
    <span class="stop">{esc(t["stoppedBy"])} · {a(link("transaction", t["explorer"]))}</span></summary>
  <div class="more"><span>Would cost: {esc(t["wouldCost"])}.</span><span>Logged: <code>{esc(t["reason"])}</code></span></div>
</details></li>""" for t in rows)


def items_html(rows):
    return "".join(f"""<li class="bs-kept"><div class="bs-value">{esc(k["value"])}</div><div class="detail">{esc(k["detail"])}</div>
  {links_html(k["links"])}</li>""" for k in rows)


def tabs_html(page):
    tabs = []
    for i, p in enumerate(page["parties"]):
        refused = sum(1 for t in p["attempts"] if t["refused"])
        tries = f' · <span class="x">{refused} refused</span>' if p["attempts"] else ""
        tabs.append(f'<a class="bs-btn tab" role="tab" href="#{p["id"]}" aria-selected="{"true" if i == 0 else "false"}" '
                    f'data-id="{p["id"]}">{esc(p["name"])} <span class="k">{len(p["kept"])} kept</span>{tries}</a>')
    tabs.append(f'<a class="bs-btn tab how" role="tab" href="#how" aria-selected="false" data-id="how">How it works · '
                f'{len(page["how"]["stages"])} processes</a>')
    return "".join(tabs)


def party_view(p, on):
    kept = f"""<div class="bs-col kept">
    <div class="bs-colhead"><span class="tag">Kept</span><span class="sub">measured on devnet</span></div>
    <ul>{"".join(f'''<li class="bs-kept">
      <div class="claim">{esc(k["claim"])}</div>
      <div class="bs-value">{esc(k["value"])}</div>
      <div class="detail">{esc(k["detail"])}</div>
      {links_html(k["links"])}
    </li>''' for k in p["kept"])}</ul></div>"""
    refused = sum(1 for t in p["attempts"] if t["refused"])
    tries = f"""<div class="bs-col refused">
    <div class="bs-colhead"><span class="tag">Refused</span><span class="sub">{refused} of {len(p["attempts"])} attempts · failed transactions</span></div>
    <ol>{attempt_rows(p["attempts"])}</ol></div>""" if p["attempts"] else ""
    sim = f'<p class="bs-sim"><b>Simulated</b><span>{esc(p["sim"])}</span></p>' if p.get("sim") else ""
    return f"""<section class="bs-panel view{" on" if on else ""}" role="tabpanel" data-view="{p["id"]}" id="{p["id"]}">
    <div class="bs-phead"><h2 class="bs-h2">{esc(p["name"])}</h2><span class="role">{esc(p["role"])}</span></div>
    <p class="bs-now"><b>Expects</b><span>{esc(p["expects"])}</span></p>
    <div class="bs-cols{"" if tries else " single"}">{kept}{tries}</div>
    {sim}
    <p class="bs-now ok"><b>How</b><span>{esc(p["how"])}</span></p>
  </section>"""


# How it works: each process, abstract beside real, the difference and why the outcome is the same; then the design.
def how_view(H):
    first = H["stages"][0]["id"]
    strip = f"""<nav class="stages" aria-label="Processes">{"".join(
        f'<button class="stage" aria-selected="{"true" if s["id"] == first else "false"}" data-stage="{s["id"]}">'
        f'<span class="i">{i + 1}</span>{esc(s["name"])}</button>' for i, s in enumerate(H["stages"]))}</nav>"""
    bodies = []
    for s in H["stages"]:
        r, t = s["refuses"], s["trace"]
        stress = f"""<div class="bs-col refused"><div class="bs-colhead"><span class="tag">Under stress</span>
      <span class="sub">{f'{r["refused"]} of {len(r["ids"])} attacks refused' if r["ids"] else "measured"}</span></div>
      {f'<ul>{items_html(s["stress"])}</ul>' if s["stress"] else ""}{f'<ol class="tight">{attempt_rows(r["rows"])}</ol>' if r["ids"] else ""}</div>""" \
            if s["stress"] or r["ids"] else ""
        bodies.append(f"""<div class="stagebody{" on" if s["id"] == first else ""}" data-stage="{s["id"]}">
    <div class="pair">
      <div class="side abstract"><span class="tag">Abstract</span><p>{esc(s["abstract"])}</p></div>
      <div class="side real"><span class="tag">Real</span><p>{esc(s["real"])}</p></div>
    </div>
    <div class="why">
      <div><span class="tag">Real-world constraint</span><p>{esc(s["gap"])}</p></div>
      <div><span class="tag">Why the outcome is the same</span><p>{esc(s["holds"])}</p></div>
    </div>
    <dl class="trace"><div><dt>Sent by</dt><dd>{esc(t["sentBy"])}</dd></div><div><dt>Reads</dt><dd>{esc(t["reads"])}</dd></div>
      <div><dt>Writes</dt><dd>{esc(t["writes"])}</dd></div><div><dt>Moves</dt><dd>{esc(t["moves"])}</dd></div></dl>
    <div class="bs-cols{"" if stress else " single"}">
      <div class="bs-col kept"><div class="bs-colhead"><span class="tag">Same outcome</span><span class="sub">measured on devnet</span></div><ul>{items_html(s["normal"])}</ul></div>
      {stress}
    </div>
    </div>""")
    principles = "".join(f"""<li data-stages="{" ".join(p["stages"])}"{' class="on"' if first in p["stages"] else ""}>
    <b>{esc(p["name"])}</b><span>{esc(p["text"])}</span></li>""" for p in H["principles"])
    m = H["matrix"]
    cell = lambda c: f'<td class="{"" if c == "" else "r" if c == "read" else "w"}">{esc(c)}</td>'  # noqa: E731
    matrix = f"""<div class="scroll"><table class="matrix"><thead><tr><th>Process</th><th>Sent by</th>{"".join(f"<th>{esc(c)}</th>" for c in m["columns"])}</tr></thead>
    <tbody>{"".join(f'<tr><td><code>{esc(row[0])}</code></td><td>{esc(row[1])}</td>{"".join(cell(c) for c in row[2:])}</tr>' for row in m["rows"])}</tbody></table></div>"""
    eq = lambda xs: " ".join(f"<i>{x}</i>" if x in "=+;" else esc(x) for x in xs)  # noqa: E731
    return f"""<section class="bs-panel view" role="tabpanel" data-view="how" id="how">
    <div class="bs-phead"><h2 class="bs-h2">How it works</h2>
      <span class="role">Every bond needs the same processes. The program runs them under real constraints; where it has to differ, the outcome must not.</span></div>
    {strip}
    {"".join(bodies)}
    <section class="design">
      <h3 class="bs-h3">The design that follows</h3>
      <ol class="principles">{principles}</ol>
      {matrix}
    </section>
    <section class="design">
      <h3 class="bs-h3">Outcome of the 1000-holder run</h3>
      <p class="eq"><b>Bonds</b>{eq(H["outcome"]["bonds"])}</p>
      <p class="eq"><b>Cash</b>{eq(H["outcome"]["cash"])}</p>
    </section>
    <section class="design">
      <h3 class="bs-h3">Assumed, not proven here</h3>
      <ul class="assumed">{"".join(f"<li>{esc(x)}</li>" for x in H["assumed"])}</ul>
    </section>
  </section>"""


def foot_html(page):
    return (f'<p>Program {a(link("2UkTKYYz...8sHf", page["programLink"]))} on Solana {esc(page["cluster"])}. '
            f'{page["refused"]} of {page["attempts"]} attacks refused; {page["holders"]:,} holders in the largest run.</p>'
            '<p><a href="timeline.html">Timeline: one bond from issue to redemption →</a></p>'
            f'<p>Built from ledger data only: the 1000-holder run ({esc(page["generated"])}); the attacker run, measured '
            f'{esc(page["hostileMeasured"])}.</p>')


def header_html(page):
    return (f'<p class="bs-eyebrow">Measured on devnet · {page["holders"]:,} holders, {page["transactions"]:,} transactions sent</p>'
            '<h1 class="bs-h1">Who held the bond at 09:00, and was each holder paid exactly once?</h1>'
            '<p class="bs-lede">A coupon belongs to whoever held the bond at the record moment. One Solana program finds '
            'those holders, pays them from the terms and records it. Pick a party: what it expects, what was kept, what '
            'was refused. Or see how it works.</p>')


def summary_html(page):
    cell = lambda k, t, s, cls="": (f'<div><span class="k">{esc(k)}</span><span class="t{cls}">{esc(t)}</span>'  # noqa: E731
                                    f'<span class="s">{s}</span></div>')
    m = page["summary"]
    return (cell("Paid on the 09:00 list", f'{m["exact"]:,} / {m["cells"]:,}', f'{m["naiveWrong"]:,} wrong if paid by balance', " up")
            + cell("Audit from public data", f'{m["passed"]:,} / {m["checks"]:,}', "checks · " + a(link("result", "#auditor")), " up")
            + cell("Attacks", f'{m["refused"]} / {m["attempts"]}', "refused, each a failed transaction", " down")
            + cell("Coupons paid", f'{m["couponCash"]:,.2f}', f'{m["coupons"]} coupons · final supply 0'))


def fill(template, parts):
    for key, html in parts.items():
        marker = f"<!--__{key}__-->"
        assert template.count(marker) == 1, f"template must hold {marker} once"
        template = template.replace(marker, html)
    return template


def how_it_works(D, K, H, probe, attempts, c1, c2, c4, c5):
    """The bond's abstract processes beside what the program really does, where and why they differ, why the
    outcome does not, and the measured evidence (normal and stress). Then the design they imply and the run's outcome."""
    scn = json.loads((ROOT / D["scenarioFile"]).read_text())
    holders = len(D["holders"])
    bonds, cash = {}, {}
    for e in D["events"]:
        for m in e["moves"].values():
            kind = e["kind"]
            if kind == "buyback" and m["bonds"]:  # sales and tender settlements: told apart by the offer's price
                kind = "tender" if round(m["cash"] / -m["bonds"] * 10 ** scn["terms"]["cashDecimals"]) == int(
                    next(o for o in scn["offers"] if o["kind"] == "tender")["priceUnits"]) else "buyback"
            bonds[kind] = bonds.get(kind, 0) + m["bonds"]
            cash[kind] = cash.get(kind, 0) + m["cash"]
    unit = 10 ** scn["terms"]["cashDecimals"]
    offer_budget = {o["kind"]: int(o["priceUnits"]) * o["maxQuantity"] / unit for o in scn["offers"]}
    offer_max = {o["kind"]: o["maxQuantity"] for o in scn["offers"]}
    redeemers = len({n for e in D["events"] if e["kind"] == "redemption" for n in e["moves"]})
    tender_offer = next(o for o in scn["offers"] if o["kind"] == "tender")
    tenderers = {a["holder"] for a in tender_offer["actions"] if a.get("tender")}
    window_coupon = tender_offer["close"]["recordMoment"]
    paid_in_window = sum(1 for h in tenderers if D["grid"][window_coupon]["paid"].get(h, 0) > 0)
    to_holders = sum(cash[k] for k in ("coupon", "buyback", "tender", "redemption"))
    left_in_offers = sum(offer_budget[k] - cash[k] for k in ("buyback", "tender"))
    serviced = [s for s in H["serviced"] if s["cashTo"]]
    hostile_ok = sum(1 for c in H["audit"]["checks"] if c["ok"])
    frozen = H["frozenCash"]["holder"]

    def refused(ids):
        return {"ids": ids, "refused": sum(1 for i in ids if attempts[i]["refused"])}

    stages = [
        {"id": "issue", "name": "Issue",
         "abstract": "The issuer sells bonds on fixed terms. Each buyer holds what it bought, and the supply is fixed.",
         "real": "create_bond writes the terms and creates the bond token with the program as its only mint and burn "
                 "authority and no freeze authority. register_holder admits a wallet; place mints bonds to it, only "
                 "before the first record moment.",
         "gap": "Anyone can open a token account. A bond needs known holders and a supply nobody can inflate.",
         "holds": "Only the program changes the supply, and it records every change, so the supply at any record "
                  "moment is known without counting holders.",
         "trace": {"sentBy": "registrar", "reads": "terms", "writes": "bond, holder records", "moves": "bonds minted to holders"},
         "refuses": refused(["register-self", "mint-bonds", "mint-as-program", "place-late", "place-beyond-issue", "fee-cash-token", "delegate-cash-token"]),
         "normal": [item(f"{bonds['placement']:,} bonds", f"placed with {holders:,} registered holders", [link("bond", D["bondMintLink"])])],
         "stress": []},
        {"id": "trade", "name": "Trade",
         "abstract": "A trade changes ownership at once. The register always says who holds what.",
         "real": "Every transfer runs the program's transfer hook in the same transaction. Both sides must be "
                 "registered; the hook saves their holdings at the record moments passed, then updates both holder records.",
         "gap": "Transfers run in the token program, not in ours, and one holder could open several accounts.",
         "holds": "If the hook fails, the transfer fails. One account per holder, and a transfer is refused if a holder "
                  "record ever disagrees with its balance.",
         "trace": {"sentBy": "holder; the token program calls the hook", "reads": "bond time",
                   "writes": "both holder records", "moves": "bonds, seller to buyer"},
         "refuses": refused(["take-bonds", "take-as-delegate", "freeze-holder", "send-unregistered", "second-account",
                             "skip-hook", "sell-tendered", "trade-at-maturity"]),
         "normal": [item(f"{c1['trades']} trades", f"between {holders:,} holders, every one through the hook", [link("first", c1["saleLink"])])],
         "stress": []},
        {"id": "record", "name": "Record",
         "abstract": "At the record moment the register is fixed: each holder is owed for what it holds at that instant.",
         "real": "Nothing runs at the record moment. The first change to a holding after it saves the holding it "
                 "replaces; a holding that never changed is still the one held then.",
         "gap": "A program sees only the accounts in a transaction and runs only when one is sent: it cannot read every "
                "holder at 09:00, and nothing is sent at 09:00 by itself.",
         "holds": "Between two changes a holding is constant, so what the next change saves is exactly the holding at "
                  "the record moment, however late anyone looks. No freeze, no deadline.",
         "trace": {"sentBy": "nobody: it happens inside the next transfer", "reads": "bond time, record moments",
                   "writes": "holding history, supply history", "moves": "nothing"},
         "refuses": refused(["forge-position"]),
         "normal": [item(f"{c1['exact']:,} of {c1['cells']:,}", "payments on the saved holdings equal the ledger-only audit's register at the record moments")],
         "stress": [item(f"{c1['trades']} trades", f"{c1['lagMin']} s to {round(c1['lagMax'] / 60)} min after record moments; "
                                                    f"paying by holdings at payment time: {c1['naiveWrong']:,} wrong",
                         [link("sale", c1["saleLink"]), link("its payment", c1["payLink"])])]},
        {"id": "calculate", "name": "Calculate",
         "abstract": "Owed = holding at the record moment x face x rate / periods, as the terms say.",
         "real": "Computed inside each payment from the saved holding and the terms, in integers, rounded down once per "
                 "holder. The event's budget is the same formula on the supply at the record moment.",
         "gap": "Token amounts are whole smallest units; there are no fractions on chain.",
         "holds": "Rounding once on each holder's total keeps the sum within the budget; any residue stays visible in "
                  "the event's vault.",
         "trace": {"sentBy": "part of each payment", "reads": "terms, saved holding", "writes": "nothing", "moves": "nothing"},
         "refuses": refused(["late-buyer"]),
         "normal": [item(f"BH#1: {D['grid'][0]['paid']['BH#1']:,.2f}", f"{D['grid'][0]['atRecord']['BH#1']} bonds x 1,000 x 10% / 2, the listing's example"),
                    item("passed", "every amount checked against QuantLib and FinancePy")],
         "stress": []},
        {"id": "fund", "name": "Fund",
         "abstract": "The issuer provides the full amount before payment.",
         "real": "After the record moment anyone opens the event: it gets its own vault and a budget fixed by the supply "
                 "at the record moment. The issuer deposits; anyone confirms, accepted only if the vault covers the budget.",
         "gap": "Cash is a separate token that the issuer moves outside the program, at its own pace.",
         "holds": "No holder is paid from an event that cannot pay all, and one event's cash cannot pay another. The "
                  "issuer's only action is the deposit.",
         "trace": {"sentBy": "anyone (open, confirm); issuer (deposit)", "reads": "supply history, vault balance",
                   "writes": "event, its vault", "moves": "cash, issuer to the event's vault"},
         "refuses": refused(["confirm-short", "pay-unfunded", "drain-vault", "drain-as-program"]),
         "normal": [item(f"{c2['issuerTxs']} of {c2['total']:,}", "transactions were the issuer's deposits")],
         "stress": []},
        {"id": "pay", "name": "Pay",
         "abstract": "On the payment date each entitled holder receives its amount, exactly once.",
         "real": "Any wallet, any time after payment opens, in any order, pays one holder: cash from the event's vault to "
                 "the holder's own cash account and the holder's paid flag, in one transaction.",
         "gap": "Paying 1,000 holders takes many transactions; someone must send them; the operator may stop; a holder's "
                "cash account may be frozen.",
         "holds": "Whoever sends it and whenever, state fixes the amount and the recipient; the paid flag makes it once; "
                  "rights never expire; one holder's failure blocks nobody.",
         "trace": {"sentBy": "anyone", "reads": "bond, event, holder record", "writes": "holder's paid flag",
                   "moves": "cash, event vault to the holder's own account"},
         "refuses": refused(["redirect-coupon", "claim-as-owner", "pay-twice", "other-vault", "coupon-from-principal", "frozen-cash"]),
         "normal": [item(f"{cash['coupon']:,.2f}", f"{len(D['grid'])} coupons to {holders:,} holders, exact")],
         "stress": [item(f"{c4['exact']} of {c4['cells']}", f"operator stopped after coupon {c4['stop']}; a wallet with no role paid the rest",
                         [link("that wallet", c4["keeperLink"])]),
                    item("0 to itself", f"the attacker's wallet paid coupons and principal in {len(serviced)} transactions",
                         [link("one", serviced[0]["explorer"])]),
                    item(f"{frozen} waited", f"its cash account frozen by the bank: the others were paid first, {frozen} after the thaw",
                         [link("refused while frozen", attempts["frozen-cash"]["explorer"])])]},
        {"id": "redeem", "name": "Redeem",
         "abstract": "At maturity holders receive the principal and the bonds cease to exist.",
         "real": "Transfers close at the final record moment. Any wallet redeems a holder: its bonds are burned and the "
                 "principal paid in one transaction.",
         "gap": "Redemption takes one transaction per few holders, and bonds tendered to an open offer are not free.",
         "holds": "Burn and payment are one transaction: no bond is burned unpaid and no principal is paid for a live "
                  "bond. Bonds committed to an offer are left to it.",
         "trace": {"sentBy": "anyone", "reads": "bond, event, holder record", "writes": "holder record, supply",
                   "moves": "bonds burned; cash, vault to holder"},
         "refuses": refused(["redirect-principal", "redeem-twice", "burn-bonds", "burn-permissioned", "burn-as-program"]),
         "normal": [item(f"{-bonds['redemption']:,} bonds", f"redeemed from {redeemers} holders; final supply 0")],
         "stress": []},
        {"id": "offer", "name": "Buy back",
         "abstract": "The issuer buys bonds back at a price: first come up to a maximum, or pro rata among tenders. "
                     "Bought bonds cease to exist.",
         "real": "The registrar opens an offer with its own funded vault. A sale burns and pays at once. A tender locks "
                 "bonds in the holder's own account; after the window anyone resolves the tenders in the order they were "
                 "made, each taking the pro-rata share of the running total, rounded down, minus what the earlier ones "
                 "took, then settles each accepted part.",
         "gap": "The tender window spans a record moment, and bonds come in whole units.",
         "holds": "Tendered bonds never move, so they keep the coupon whose record moment falls in the window. An "
                  "oversubscribed offer buys exactly its maximum; each tender gets at least its own share rounded down and "
                  "never more than it tendered. If fewer are tendered, the unspent cash stays in the offer's vault.",
         "trace": {"sentBy": "registrar (offer); holder (sell, tender); anyone (settle)", "reads": "offer, holder record",
                   "writes": "offer, tender, holder record, supply", "moves": "bonds burned; cash, offer vault to holder"},
         "refuses": refused(["sell-others-bonds", "over-maximum"]),
         "normal": [item(f"{-bonds['buyback']} bonds", f"bought back first come, maximum {offer_max['buyback']}"),
                    item(f"{-bonds['tender']} bonds", f"accepted pro rata from tenders, maximum {offer_max['tender']}")],
         "stress": [item(f"{paid_in_window} of {len(tenderers)}", f"tendering holders paid coupon {window_coupon} on their tendered bonds")]},
        {"id": "audit", "name": "Audit",
         "abstract": "Anyone can reconcile the register and every payment.",
         "real": "Every change is a ledger transaction. A ledger-only audit reads only the public ledger and "
                 "recomputes every amount from the terms.",
         "gap": "A ledger shows transactions, not obligations: the auditor must rebuild the obligations itself.",
         "holds": "The audit needs no operator report and no program accounts, and it fails on one unit out of place.",
         "trace": {"sentBy": "anyone, off chain", "reads": "public ledger, terms", "writes": "nothing", "moves": "nothing"},
         "refuses": refused([]),
         "normal": [item(f"{c5['passed']:,} of {c5['checks']:,}", f"checks over {c5['transactions']:,} ledger transactions read"),
                    item(f"{hostile_ok} of {len(H['audit']['checks'])}", "checks on the attacker run")],
         "stress": [item(f"{len(probe['after']['failed'])} checks fail", "after 0.000001 is sent into a vault from outside (local network)")]},
    ]
    for s in stages:
        s["refuses"]["rows"] = [attempts[i] for i in s["refuses"]["ids"]]

    # Which instruction reads or changes which account, and who may send it (from the program's account constraints).
    matrix = {
        "columns": ["Bond: terms, supply", "Holder record", "Event", "Offer, tender", "Vault (cash)", "Bond token"],
        "rows": [
            ["create_bond", "registrar", "create", "", "", "", "", "create"],
            ["register_holder", "registrar", "read", "create", "", "", "", ""],
            ["place", "registrar", "supply", "write", "", "", "", "mint"],
            ["transfer + hook", "holder", "read", "write both", "", "", "", "move"],
            ["open_event", "anyone", "read", "", "create", "", "create", ""],
            ["deposit", "issuer", "", "", "", "", "cash in", ""],
            ["confirm_funding", "anyone", "", "", "write", "", "read", ""],
            ["pay_coupon", "anyone", "read", "paid flag", "read", "", "cash out", ""],
            ["redeem_holder", "anyone", "supply", "write", "read", "", "cash out", "burn"],
            ["create_offer", "registrar", "read", "", "", "create", "create", ""],
            ["sell_to_offer", "holder", "supply", "write", "", "write", "cash out", "burn"],
            ["change_tender", "holder", "read", "lock", "", "write", "", ""],
            ["settle_tender", "anyone", "supply", "write", "", "read", "cash out", "burn"],
        ],
    }
    principles = [
        {"name": "Record on write, not by snapshot", "stages": ["trade", "record"],
         "text": "Holdings at record moments are saved by the next change, so the market never stops and nothing has a deadline."},
        {"name": "Every balance change passes the program", "stages": ["issue", "trade", "redeem"],
         "text": "Transfer hook, the program as only mint and burn authority, no freeze authority; records checked against balances."},
        {"name": "Outcomes follow from state, so anyone may act", "stages": ["fund", "pay", "redeem", "offer"],
         "text": "Opening, confirming, paying, redeeming and settling need no authority: the sender cannot change who gets what."},
        {"name": "Each obligation isolated", "stages": ["fund", "pay"],
         "text": "One vault per event and offer, one paid flag per holder per coupon; a payment writes only that holder's record and that vault."},
        {"name": "Value moves only with its counterpart", "stages": ["pay", "redeem", "offer"],
         "text": "Cash and paid flag in one transaction; burn and payment in one transaction."},
        {"name": "Checked from outside", "stages": ["audit"],
         "text": "An audit from the public ledger alone, failing on one unit out of place."},
    ]
    # Bonds: every placed bond burned against cash. Cash: what the bank deposited (vault inflow, audit check C4) went to
    # holders or stays visible in the offer vaults.
    burned = -(bonds["buyback"] + bonds["tender"] + bonds["redemption"])
    if burned != bonds["placement"]:
        raise SystemExit(f"bonds placed {bonds['placement']} != burned {burned}")
    outcome = {
        "bonds": [f"{bonds['placement']:,} placed", "=", f"{-bonds['buyback']} bought back", "+", f"{-bonds['tender']} tendered",
                  "+", f"{-bonds['redemption']:,} redeemed", ";", "0 left"],
        "cash": [f"{to_holders + left_in_offers:,.2f} deposited", "=", f"{to_holders:,.2f} to holders", "+",
                 f"{left_in_offers:,.2f} unspent offer cash, still in its vaults", ";", "0.00 left in coupon and redemption vaults"],
    }
    assumed = [
        "Time is the chain's clock; devnet runs compress a bond day to 1-20 seconds.",
        "The registrar registers only vetted wallets (investor onboarding is simulated).",
        "The program's upgrade authority is one wallet: an attacker cannot take it (refused), but its holder could replace the program.",
        "Cash is a test token minted by a test bank; holders hold directly, with no custodians.",
    ]
    return {"stages": stages, "matrix": matrix, "principles": principles, "outcome": outcome, "assumed": assumed,
            "takeProgram": attempts["take-program"]}


def main(crowd_path, keeper_path, probe_path, hostile_path, out_path):
    D = json.loads(Path(crowd_path).read_text())
    K = json.loads(Path(keeper_path).read_text())
    probe = json.loads(Path(probe_path).read_text())
    H = json.loads(Path(hostile_path).read_text())
    c1, c2, c3 = card_market_open(D), card_nothing_mirrored(D), card_cost(D)
    c4, c5 = card_operator_failure(K), card_audit(D, probe)
    checks = D["audit"]["checks"]
    # The page states these as kept; it is not built on a run where they do not hold.
    failing = [f"crowd audit {c['id']}" for c in checks if not c["ok"]] + [f"keeper audit {c['id']}" for c in K["audit"]["checks"] if not c["ok"]]
    failing += [] if c1["exact"] == c1["cells"] else ["crowd payments not all exact"]
    failing += [] if c4["exact"] == c4["cells"] else ["keeper payments not all exact"]
    failing += [f"attempt {x['id']} not refused by its guard" for x in H["attempts"] if not x["byGuard"]]
    failing += [f"attacker run audit {c['id']}" for c in H["audit"]["checks"] if not c["ok"]]
    hostile_ok = sum(1 for c in H["audit"]["checks"] if c["ok"])
    if failing:
        raise SystemExit("not building the page: " + "; ".join(failing))
    passed = lambda prefix: (sum(1 for c in checks if c["id"] == prefix and c["ok"]), sum(1 for c in checks if c["id"] == prefix))
    holders = len(D["holders"])
    coupon_cash = sum(sum(g["paid"].values()) for g in D["grid"])
    bh1 = next(e for e in D["events"] if e["kind"] == "coupon" and e["coupon"] == 0 and "BH#1" in e["moves"])
    bh1_bonds, bh1_paid = D["grid"][0]["atRecord"]["BH#1"], D["grid"][0]["paid"]["BH#1"]

    attempts = {a["id"]: a | {"stoppedBy": STOPPED_BY[a["id"]]} for a in H["attempts"]}
    assigned = [i for ids in PARTY_ATTEMPTS.values() for i in ids]
    if sorted(assigned) != sorted(attempts) or sorted(STOPPED_BY) != sorted(attempts):
        raise SystemExit(f"attempts and the page's party lists differ: {sorted(set(attempts) ^ set(assigned))}")
    refused = sum(1 for a in attempts.values() if a["refused"])
    frozen = H["frozenCash"]
    frozen_try = attempts["frozen-cash"]
    serviced = H["serviced"]
    serviced_pay = [s for s in serviced if s["cashTo"]]
    to_attacker = sum(s["cashToAttacker"] for s in serviced) + sum(s["bondsToAttacker"] for s in serviced)
    c1_count, c1_all = passed("C1")
    c3_count, c3_all = passed("C3")
    m4_count, m4_all = passed("M4")
    probe_failed = len(probe["after"]["failed"])

    parties = [
        {"id": "holder", "name": "Holder", "role": "investor", "expects": "Paid exactly, on time, for what it held",
         "sim": "Holders are generated wallets, labelled BH#1, BH#2, ...",
         "how": "A transfer first saves both holders' positions at every record moment passed. The coupon is computed from "
                "that saved position and paid only to the holder's own cash account.",
         "kept": [
             {"claim": "Paid for the bonds held at the record moment", "value": f"{c1['exact']:,} of {c1['cells']:,}",
              "detail": f"coupon payments exact, {holders:,} holders; paying by holdings at payment time would have got "
                        f"{c1['naiveWrong']:,} wrong",
              "links": [link("audit", "#auditor")]},
             {"claim": "Sells right after the record moment, keeps the coupon",
              "value": f"{c1['seller']}: {c1['saleLag']} s",
              "detail": f"sold {c1['sold']} of {c1['sellerAtRecord']} bonds {c1['saleLag']} s after the record moment; "
                        f"paid on {c1['sellerAtRecord']}: {c1['sellerPaid']:,.2f}",
              "links": [link("sale", c1["saleLink"]), link("payment", c1["payLink"])]},
             {"claim": "The listing's example", "value": f"BH#1: {bh1_paid:,.2f}",
              "detail": f"{bh1_bonds} bonds x 1,000 x 10% / 2", "links": [link("payment", bh1["link"])]},
         ]},
        {"id": "issuer", "name": "Issuer", "role": "borrower", "expects": "Pays what the terms say, not a unit more",
         "how": "Each coupon and the redemption has its own vault, funded with exactly its budget. A payment and the "
                "holder's paid flag are one transaction.",
         "kept": [
             {"claim": "Coupons paid = the terms", "value": f"{coupon_cash:,.2f}",
              "detail": f"{len(D['grid'])} coupons to {holders:,} holders; every vault: inflow = payments + leftover",
              "links": [link("audit", "#auditor")]},
             {"claim": "Bonds retired only against cash", "value": f"{c3_count:,} of {c3_all:,}",
              "detail": "holders' buyback, tender and redemption: bonds burned and cash paid in one transaction",
              "links": [link("audit", "#auditor")]},
             {"claim": "Its only task: deposit the cash", "value": f"{c2['issuerTxs']} of {c2['total']:,}",
              "detail": "transactions in the 1000-holder run were the issuer's", "links": []},
         ]},
        {"id": "depository", "name": "Depository", "role": "registrar and paying agent",
         "expects": "A correct register, and no new problems",
         "sim": "Holder onboarding and KYC is a registrar key.",
         "how": "The program is the register and the paying agent. Opening, funding confirmation, payment and redemption "
                "can be sent by any wallet; none can change who is paid or how much.",
         "kept": [
             {"claim": "The register at every record moment, trading open", "value": f"{holders:,} holders",
              "detail": f"{c1['trades']} trades {c1['lagMin']} s to {round(c1['lagMax'] / 60)} min after record moments; "
                        f"cost per holder paid: {c3['perHolderMin']:,.0f} to {c3['perHolderMax']:,.0f} lamports",
              "links": [link("bond", D["bondMintLink"])]},
             {"claim": "Works when the operator stops", "value": f"{c4['exact']} of {c4['cells']}",
              "detail": f"our operator stopped after coupon {c4['stop']}; a wallet with no role paid the rest and "
                        f"redeemed everyone",
              "links": [link("that wallet", c4["keeperLink"])]},
             {"claim": "Anyone can service; nobody can redirect", "value": f"{to_attacker} to the attacker",
              "detail": f"the attacker's wallet paid coupons and principal in {len(serviced_pay)} transactions: "
                        f"all cash went to holders",
              "links": [link("transaction", s["explorer"]) for s in serviced_pay[:1]]},
         ]},
        {"id": "bank", "name": "Bank", "role": "cash rail", "expects": "Its money and its controls respected",
         "sim": "The cash is a test token (6 decimals) minted by a test bank key, standing for fiat or a stablecoin.",
         "how": "The program moves cash only with the cash token's own transfer: it cannot pass a frozen account. At bond "
                "creation it refuses cash tokens that change amounts or let a third party move a vault's cash.",
         "kept": [
             {"claim": "Cash enters a vault only from the bank", "value": f"{m4_count} of {m4_all}",
              "detail": "vault checks: deposits only from the bank's mints, payments only to holders",
              "links": [link("audit", "#auditor")]},
             {"claim": "Its freeze holds; the bond keeps paying", "value": f"{frozen['holder']} waits",
              "detail": f"the bank froze {frozen['holder']}'s cash account for coupon {frozen['coupon']}: the others "
                        f"were paid, {frozen['holder']} after the thaw",
              "links": [link("refused while frozen", frozen_try["explorer"])]},
         ]},
        {"id": "exchange", "name": "Exchange", "role": "trading venue", "expects": "Trading open every day until maturity",
         "sim": "Trades are direct transfers between registered holders; there is no order book.",
         "how": "No snapshot and no freeze: the hook saves positions as bonds move, so a record moment needs no pause. "
                "Trading stays open for transfers through the bond token's transfer hook (Solana Token-2022), so a venue "
                "has to support it.",
         "kept": [
             {"claim": "Open through every record moment", "value": f"{c1['trades']} trades",
              "detail": f"{c1['lagMin']} s to {round(c1['lagMax'] / 60)} min after record moments, {holders:,} holders",
              "links": [link("first sale", c1["saleLink"])]},
             {"claim": "Nobody can freeze a holder", "value": "No freeze authority",
              "detail": "the bond token was created without one", "links": [link("bond token", D["bondMintLink"])]},
         ]},
        {"id": "auditor", "name": "Auditor", "role": "or regulator", "expects": "Every payment checkable from public data",
         "sim": "The tampering probe ran on a local network, not devnet.",
         "how": "The audit is independent in its input: it reads the public ledger only, not program accounts or our "
                "operator's report. It checks every amount against the bond's terms and the run's scenario.",
         "kept": [
             {"claim": "Every payment recomputed from the ledger", "value": f"{c5['passed']:,} of {c5['checks']:,}",
              "detail": f"checks over {c5['transactions']:,} ledger transactions read (every one touching the bond token, the cash "
                        f"token, the bond's authority or its vaults); {c5['holdersExact']:,} of {c5['holders']:,} "
                        f"holders' cash equals the terms to the unit", "links": []},
             {"claim": "Tampering is caught", "value": f"{probe_failed} checks fail",
              "detail": "after 0.000001 is sent into a vault from outside (local network)", "links": []},
             {"claim": "The attacker run, audited the same way", "value": f"{hostile_ok} of {len(H['audit']['checks'])}",
              "detail": f"checks pass; {refused} of {len(attempts)} attacks refused, each a failed transaction anyone can open",
              "links": []},
         ]},
    ]
    for p in parties:
        p["attempts"] = [attempts[i] for i in PARTY_ATTEMPTS[p["id"]]]

    page = {
        "programLink": D["programLink"], "cluster": D["cluster"], "generated": D["generated"],
        "hostileMeasured": H["measured"], "holders": holders, "attempts": len(attempts), "refused": refused,
        "parties": parties, "how": how_it_works(D, K, H, probe, attempts, c1, c2, c4, c5),
        "transactions": c2["total"],
        "summary": {"exact": c1["exact"], "cells": c1["cells"], "naiveWrong": c1["naiveWrong"], "saleLink": c1["saleLink"],
                    "passed": c5["passed"], "checks": c5["checks"], "refused": refused, "attempts": len(attempts),
                    "couponCash": coupon_cash, "coupons": len(D["grid"])},
    }
    template = (ROOT / "tools/demo/app_template.html").read_text()
    views = "".join(party_view(p, i == 0) for i, p in enumerate(parties)) + how_view(page["how"])
    Path(out_path).write_text(fill(template, {"HEADER": header_html(page), "SUMMARY": summary_html(page), "TABS": tabs_html(page),
                                              "VIEWS": views, "FOOT": foot_html(page)}))
    print(f"wrote {out_path}: {len(parties)} parties, {refused} of {len(attempts)} attempts refused")


if __name__ == "__main__":
    if len(sys.argv) != 6:
        print("usage: build_app.py <crowd.json> <keeper.json> <tamper-probe.json> <hostile-attempts.json> <out.html>",
              file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
