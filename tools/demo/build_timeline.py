"""Build the timeline page (docs/demo/timeline.html): one bond from issue to redemption, step by step, every figure read
from the run's transactions on devnet.

Measured: every transaction of the run (the measured dataset's servicing list: all transactions touching the bond
token, its mint authority or its vaults) is fetched from public RPC (tools/audit keeps confirmed replies on disk) and
read two ways:
  - token balances before and after: each holder's bonds and cash, and the cash in the bond's vaults (cash accounts
    owned by the bond token's mint authority);
  - the program's events in the logs ("Program data:" lines): budgets, coupon payments with the position each was paid
    on, offers, tenders, buybacks, redemptions, and the positions the transfer hook saved.
The page is not written if the two disagree (a coupon paid on another quantity than the ledger shows at the record
moment, or another amount than the terms give), if the run's audit did not pass, or if the holders' final cash differs
from the measured dataset.
Design: Blum (docs/demo/blum/, from the Claude Design project), timeline kit.

Usage: .venv/bin/python3 tools/demo/build_timeline.py docs/demo/data/devnet-full8.json https://api.devnet.solana.com \\
           docs/demo/timeline.html
"""

import base64
import hashlib
import json
import struct
import sys
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
import audit as A  # noqa: E402

# Program events read here, with their fields in order (programs/bond_servicing/src/events.rs).
EVENTS = {
    "BondsPlaced": [("bond", "pk"), ("owner", "pk"), ("amount", "u64")],
    "PositionsSaved": [("bond", "pk"), ("owner", "pk"), ("from_event", "u16"), ("to_event", "u16"), ("quantity", "u64")],
    "EventOpened": [("bond", "pk"), ("event", "u16"), ("supply_at_record", "u64"), ("budget", "u64"), ("vault", "pk")],
    "CouponPaid": [("bond", "pk"), ("event", "u16"), ("owner", "pk"), ("position", "u64"), ("amount", "u64")],
    "OfferCreated": [("bond", "pk"), ("offer", "u16"), ("pro_rata", "bool"), ("price_units", "u64"),
                     ("max_quantity", "u64"), ("open_time", "i64"), ("close_time", "i64")],
    "TenderChanged": [("bond", "pk"), ("offer", "u16"), ("owner", "pk"), ("tendered", "u64"), ("offer_total", "u64")],
    "TenderResolved": [("bond", "pk"), ("offer", "u16"), ("owner", "pk"), ("accepted", "u64"), ("rejected", "u64")],
    "BondsBoughtBack": [("bond", "pk"), ("offer", "u16"), ("owner", "pk"), ("burned", "u64"), ("paid", "u64")],
    "BondsRedeemed": [("bond", "pk"), ("owner", "pk"), ("burned", "u64"), ("principal", "u64")],
}
SIZE = {"pk": 32, "u16": 2, "u64": 8, "i64": 8, "bool": 1}
UNPACK = {"u16": "<H", "u64": "<Q", "i64": "<q", "bool": "<?"}
DISCRIMINATOR = {hashlib.sha256(f"event:{name}".encode()).digest()[:8]: name for name in EVENTS}


def explorer(kind, key):
    return f"https://explorer.solana.com/{kind}/{key}?cluster=devnet"


def decode_events(logs):
    events = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        raw = base64.b64decode(line[len("Program data: "):])
        name = DISCRIMINATOR.get(raw[:8])
        if not name:
            continue
        at, event = 8, {"name": name}
        for field, kind in EVENTS[name]:
            chunk = raw[at:at + SIZE[kind]]
            at += SIZE[kind]
            event[field] = A.b58encode(chunk) if kind == "pk" else struct.unpack(UNPACK[kind], chunk)[0]
        events.append(event)
    return events


def measure(rpc, entry, bond_mint, authority):
    """One transaction: bond and cash changes per wallet, the change in the bond's vaults, the program's events."""
    tx = rpc.call("getTransaction", [entry["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                           "commitment": "confirmed"}], entry["slot"])
    keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
    balances = {}
    for side in ("pre", "post"):
        for b in tx["meta"][f"{side}TokenBalances"]:
            acct = balances.setdefault(keys[b["accountIndex"]], {"owner": b.get("owner"), "mint": b["mint"], "pre": 0, "post": 0})
            acct[side] = int(b["uiTokenAmount"]["amount"])
    bonds, cash, vault = {}, {}, 0
    for b in balances.values():
        delta = b["post"] - b["pre"]
        if b["mint"] == bond_mint:
            bonds[b["owner"]] = bonds.get(b["owner"], 0) + delta
        elif b["owner"] == authority:
            vault += delta
        else:
            cash[b["owner"]] = cash.get(b["owner"], 0) + delta
    return {**entry, "failed": tx["meta"]["err"] is not None, "bonds": bonds, "cash": cash, "vault": vault,
            "events": decode_events(tx["meta"]["logMessages"])}


def kind_of(tx):
    ins = set(tx["instructions"])
    for kind, names in (("issue", {"CreateBond", "RegisterHolder", "Place"}), ("offers", {"CreateOffer", "ConfirmOfferFunding"}),
                        ("trade", {"TransferHook"}), ("buyback", {"SellToOffer"}), ("tender", {"ChangeTender"}),
                        ("fund", {"OpenEvent", "ConfirmFunding"}), ("pay", {"PayCoupon"}),
                        ("settle", {"SettleTender", "ResolveTender"}), ("redeem", {"RedeemHolder"})):
        if ins & names:
            return kind
    # No program instruction: the bank minting into a vault, or a token account being created. Both belong to the
    # step they are part of.
    return "part"


STAGE = {"issue": "Issue", "offers": "Offer", "record": "Record", "trade": "Trade", "buyback": "Offer", "tender": "Offer",
         "fund": "Fund", "pay": "Pay", "settle": "Offer", "redeem": "Redeem", "audit": "Audit"}


def esc(s):
    return escape(str(s), quote=True)


def chip(label, href):
    return f'<a class="bs-proof" href="{esc(href)}" target="_blank" rel="noopener">{esc(label)}</a>'


def fill(template, parts):
    for key, html in parts.items():
        marker = "/*__DATA__*/null" if key == "DATA" else f"<!--__{key}__-->"
        assert template.count(marker) == 1, f"template must hold {marker} once"
        template = template.replace(marker, html)
    return template


def main(data_path, rpc_url, out_path):
    D = json.loads(Path(data_path).read_text())
    scn = json.loads((ROOT / D["scenarioFile"]).read_text())
    T = scn["terms"]
    unit = 10 ** T["cashDecimals"]
    face_units, per_year = int(T["faceUnits"]), T["periodsPerYear"]
    names = [h["name"] for h in D["holders"]]
    who = {h["address"]: h["name"] for h in D["holders"]}
    money = lambda units: f"{units / unit:,.2f}"  # noqa: E731
    coupon_units = lambda q: q * face_units * T["rateBps"] // (10_000 * per_year)  # noqa: E731
    principal_units = lambda q: q * face_units * T["redemptionPriceBps"] // 10_000  # noqa: E731
    failing = [c["label"] for c in D["audit"]["checks"] if not c["ok"]]
    if not D["audit"]["passed"] or failing:
        raise SystemExit(f"not building: the run's audit did not pass {failing}")

    rpc = A.Rpc(rpc_url)
    txs = [measure(rpc, e, D["bondMint"], D["bondAuthority"]) for e in sorted(D["servicing"], key=lambda e: (e["slot"], e["t"]))]
    if any(t["failed"] for t in txs):
        raise SystemExit("not building: a servicing transaction failed")
    strangers = {o for t in txs for o in list(t["bonds"]) + list(t["cash"]) if o not in who}
    if strangers:
        raise SystemExit(f"not building: bonds or cash moved for wallets outside the run: {strangers}")

    origin = datetime.fromisoformat(scn["bondTimeAtCreation"])
    bond_time = lambda t: origin + timedelta(seconds=(t - D["creationTime"]) * 86_400 / scn["chainSecondsPerBondDay"])  # noqa: E731
    records = D["records"]
    for r in records:
        assert f"{bond_time(r['chainTime']):%Y-%m-%d %H:%M}" == f"{r['payDate']} {T['recordTimeLocal']}", r

    # Group the transactions into steps: consecutive transactions of one kind form one step (each trade is its own),
    # with a record step wherever a record moment falls.
    groups, next_record = [], 0
    for t in txs:
        while next_record < len(records) and t["t"] >= records[next_record]["chainTime"]:
            groups.append({"kind": "record", "k": next_record, "txs": []})
            next_record += 1
        kind = kind_of(t)
        if kind == "part" or (groups and groups[-1]["kind"] == kind and kind != "trade"):
            groups[-1]["txs"].append(t)
        else:
            groups.append({"kind": kind, "txs": [t]})
    if next_record != len(records):
        raise SystemExit("not building: a record moment has no transaction after it")
    groups.append({"kind": "audit", "txs": []})

    # Replay the run, step by step.
    pos, locked, burned, cash = ({n: 0 for n in names} for _ in range(4))
    vault, saved, owed, record_k = 0, None, None, None
    offers, tendered_total, offer_paid = {}, {}, {}
    saved_to = {n: 0 for n in names}  # record moments each holder's position is saved for (program events)
    steps, log = [], []
    final = len(records) - 1
    for g in groups:
        kind, links, line, why = g["kind"], [], "", ""
        before_pos, before_cash = dict(pos), dict(cash)
        evs = [e for t in g["txs"] for e in t["events"]]
        for t in g["txs"]:
            for owner, delta in t["bonds"].items():
                pos[who[owner]] += delta
                if delta < 0 and kind in ("buyback", "settle", "redeem"):
                    burned[who[owner]] -= delta
            for owner, delta in t["cash"].items():
                cash[who[owner]] += delta
            vault += t["vault"]
            for e in t["events"]:
                if e["name"] == "TenderChanged":
                    locked[who[e["owner"]]] = e["tendered"]
                    tendered_total[e["offer"]] = e["offer_total"]
                elif e["name"] == "TenderResolved":
                    locked[who[e["owner"]]] -= e["rejected"]
                elif e["name"] == "BondsBoughtBack" and kind == "settle":
                    locked[who[e["owner"]]] -= e["burned"]
                elif e["name"] == "OfferCreated":
                    offers[e["offer"]] = e
                if e["name"] == "BondsBoughtBack":
                    offer_paid[e["offer"]] = offer_paid.get(e["offer"], 0) + e["paid"]
                if e["name"] == "PositionsSaved":
                    saved_to[who[e["owner"]]] = max(saved_to[who[e["owner"]]], e["to_event"])
        changed = [n for n in names if pos[n] != before_pos[n]]
        paid_now = [n for n in names if cash[n] != before_cash[n]]
        supply = sum(pos.values())
        tx_link = lambda t: explorer("tx", t["signature"])  # noqa: E731

        if kind == "issue":
            placed = [e for e in evs if e["name"] == "BondsPlaced"]
            regs = sum(1 for t in g["txs"] if "RegisterHolder" in t["instructions"])
            line = f"create_bond · register {regs} holders · place {supply} bonds"
            links = [chip("bond token", D["bondMintLink"])] + [chip(who[e["owner"]], tx_link(t)) for t in g["txs"]
                                                               for e in t["events"] if e["name"] == "BondsPlaced"]
            why = f"{supply} bonds placed with {len(placed)} holders, only until the first record moment."
            label = "Issue"
        elif kind == "offers":
            made = [(t, e) for t in g["txs"] for e in t["events"] if e["name"] == "OfferCreated"]
            desc = [f"{'tender' if e['pro_rata'] else 'buyback'}: up to {e['max_quantity']} at {money(e['price_units'])}"
                    for _, e in made]
            line = " · ".join(desc) + " · each funded in its own vault"
            links = [chip(f"{'tender' if e['pro_rata'] else 'buyback'} opened", tx_link(t)) for t, e in made]
            links += [chip("funded", tx_link(t)) for t in g["txs"] if t["vault"] > 0]
            why = ". ".join(f"{'Tender' if e['pro_rata'] else 'Buyback'}: {e['max_quantity']} at {money(e['price_units'])}" for _, e in made) + ". Both funded in full."
            label = "Offers"
        elif kind == "record":
            record_k = g["k"]
            saved = dict(pos)
            owed = {n: coupon_units(saved[n]) for n in names}
            g["t"] = records[record_k]["chainTime"]
            line = f"record moment {record_k}" + (" (final): transfers close" if record_k == final else "")
            why = ("Final record moment: trading closes, the last list is fixed." if record_k == final else
                   f"Record moment {record_k}: the 09:00 list is fixed, trading stays open.")
            label = f"Record {record_k}"
        elif kind == "trade":
            t = g["txs"][0]
            seller = next(who[o] for o, d in t["bonds"].items() if d < 0)
            buyer = next(who[o] for o, d in t["bonds"].items() if d > 0)
            q = t["bonds"][next(o for o in t["bonds"] if who[o] == buyer)]
            # Each side's position for this record moment is saved by its first change after it: in this transfer, or
            # in an earlier one of the same holder.
            if saved_to[seller] < record_k + 1 or saved_to[buyer] < record_k + 1:
                raise SystemExit(f"not building: after trade {t['signature']} a position for record {record_k} is not saved")
            line = f"transfer {q}: {seller} → {buyer} · both positions for record {record_k} saved first"
            links = [chip("transaction", tx_link(t))]
            why = f"{seller} sells {q} to {buyer}; their 09:00 positions are saved first."
            label = f"Trade after record {record_k}"
        elif kind == "buyback":
            sold = [(t, e) for t in g["txs"] for e in t["events"] if e["name"] == "BondsBoughtBack"]
            price = money(sold[0][1]["paid"] // sold[0][1]["burned"])
            line = "buyback: " + " · ".join(f"{who[e['owner']]} sells {e['burned']}" for _, e in sold) + f" at {price}"
            links = [chip(who[e["owner"]], tx_link(t)) for t, e in sold]
            why = "Buyback: " + ", ".join(f"{who[e['owner']]} sells {e['burned']}" for _, e in sold) + f" at {price}, burned as paid."
            label = "Buyback"
        elif kind == "tender":
            changes, last = [], {}
            for t in g["txs"]:
                for e in t["events"]:
                    if e["name"] == "TenderChanged":
                        n = who[e["owner"]]
                        step = e["tendered"] - last.get(n, 0)
                        last[n] = e["tendered"]
                        changes.append((t, n, step))
            offer = offers[next(e["offer"] for e in evs if e["name"] == "TenderChanged")]
            line = "tender: " + " · ".join(f"{n} {'+' if s > 0 else '−'}{abs(s)}" for _, n, s in changes) + \
                f" · {tendered_total[offer['offer']]} tendered for up to {offer['max_quantity']}"
            links = [chip(f"{n} {'+' if s > 0 else '−'}{abs(s)}", tx_link(t)) for t, n, s in changes]
            why = f"{tendered_total[offer['offer']]} bonds tendered for {offer['max_quantity']}; they stay locked and keep their coupon."
            label = "Tender"
        elif kind == "fund":
            opened = next(e for e in evs if e["name"] == "EventOpened")
            idx = opened["event"]
            is_redemption = idx == len(records)
            expect = principal_units(opened["supply_at_record"]) if is_redemption else coupon_units(opened["supply_at_record"])
            if opened["budget"] != expect or opened["supply_at_record"] != sum(saved.values()):
                raise SystemExit(f"not building: event {idx}'s budget or supply disagrees with the ledger")
            what = "the redemption" if is_redemption else f"coupon {idx}"
            per = money(face_units * T["redemptionPriceBps"] // 10_000) if is_redemption else money(coupon_units(1))
            line = f"{what}: budget {opened['supply_at_record']} × {per} = {money(opened['budget'])} · funded, confirmed"
            links = [chip("vault", explorer("address", opened["vault"]))] + \
                [chip(lbl, tx_link(t)) for t in g["txs"] for lbl in
                 (["opened"] if "OpenEvent" in t["instructions"] else ["funded"] if t["vault"] > 0 else
                  ["confirmed"] if "ConfirmFunding" in t["instructions"] else [])]
            why = (f"Redemption budget: {opened['supply_at_record']} bonds × {per} = {money(opened['budget'])}, funded." if is_redemption
                   else f"Coupon {idx} budget: {opened['supply_at_record']} bonds at 09:00 × {per} = {money(opened['budget'])}, funded.")
            label = "Fund redemption" if is_redemption else f"Fund {idx}"
        elif kind == "pay":
            paid = [(t, e) for t in g["txs"] for e in t["events"] if e["name"] == "CouponPaid"]
            idx = paid[0][1]["event"]
            for t, e in paid:
                n = who[e["owner"]]
                if e["position"] != saved[n] or e["amount"] != owed[n] or t["cash"].get(e["owner"]) != e["amount"]:
                    raise SystemExit(f"not building: {n}'s coupon {idx} disagrees with the ledger's record-moment position")
            total = sum(e["amount"] for _, e in paid)
            line = f"pay_coupon {idx}: {money(total)} to {len(paid)} holders on their 09:00 positions"
            links = [chip(who[e["owner"]], tx_link(t)) for t, e in paid]
            late = [n for n in names if saved[n] == 0 and pos[n] > 0]
            why = f"Coupon {idx} paid on the 09:00 list: {money(total)} to {len(paid)} holders."
            label = f"Coupon {idx} paid"
        elif kind == "settle":
            res = {who[e["owner"]]: e for e in evs if e["name"] == "TenderResolved"}
            bought = [(t, e) for t in g["txs"] for e in t["events"] if e["name"] == "BondsBoughtBack"]
            offer = offers[bought[0][1]["offer"]]
            line = "tender settled pro rata, in tender order: " + " · ".join(
                f"{who[e['owner']]} {e['burned']} of {e['burned'] + res[who[e['owner']]]['rejected']}" for _, e in bought) \
                + f" · {money(offer['price_units'])} each"
            links = [chip(who[e["owner"]], tx_link(t)) for t, e in bought]
            why = (f"Tender bought {sum(e['burned'] for _, e in bought)} (max {offer['max_quantity']}) in tender order: "
                   + ", ".join(f"{who[e['owner']]} {e['burned']} of {e['burned'] + res[who[e['owner']]]['rejected']}" for _, e in bought) + ".")
            label = "Tender settled"
        elif kind == "redeem":
            red = [(t, e) for t in g["txs"] for e in t["events"] if e["name"] == "BondsRedeemed"]
            total = sum(e["principal"] for _, e in red)
            line = f"redeem_holder: {len(red)} holders · {sum(e['burned'] for _, e in red)} bonds burned · {money(total)} principal"
            links = [chip(who[e["owner"]], tx_link(t)) for t, e in red]
            why = f"Redemption: {sum(e['burned'] for _, e in red)} bonds burned, {money(total)} principal paid; supply {supply}."
            saved = owed = None
            label = "Redeemed"
        else:  # audit
            checks = len(D["audit"]["checks"])
            measured = {n: round(cash[n] / unit, 2) for n in names}
            if measured != {n: D["finalCash"]["measured"][n] for n in names} or supply:
                raise SystemExit("not building: the replayed cash or supply differs from the measured dataset")
            line = f"audit of the public ledger: {checks} of {checks} checks"
            links = [chip("audit result", f"data/{Path(data_path).name}")]
            why = f"Audit of the public ledger: {checks} of {checks} checks pass."
            label = "Audit"

        if kind == "record":
            when = f"{records[g['k']]['payDate']} {T['recordTimeLocal']}"
        elif g["txs"]:
            # One chain second is a whole bond day here (sim note), so a transaction's bond time is shown as its date.
            when = f"{bond_time(g['txs'][0]['t']):%Y-%m-%d}"
        else:
            when = "after redemption"
        steps.append({
            "when": when, "stage": STAGE[kind], "label": label, "tick": records[g["k"]]["payDate"][:7] if kind == "record" else None,
            "jump": kind not in ("trade", "fund"),
            "pos": [pos[n] for n in names], "locked": [locked[n] for n in names], "burned": [burned[n] for n in names],
            "saved": [saved[n] for n in names] if saved else None,
            "owed": [owed[n] / unit for n in names] if owed else None,
            "cash": [cash[n] / unit for n in names], "supply": supply, "vault": vault / unit,
            "paid": sum(cash.values()) / unit,
            "chgPos": [names.index(n) for n in changed], "chgCash": [names.index(n) for n in paid_now],
            "chgSaved": kind == "record",
        })
        if len(why) > 69:
            raise SystemExit(f"not building: step text over 69 characters (two lines on a phone): {why}")
        log.append((when.split(" ")[0] if kind != "record" else when, line, links, why))

    total_paid = steps[-1]["paid"]
    s0 = steps[0]
    trades = sum(1 for g in groups if g["kind"] == "trade")
    eyebrow = (f"Measured on devnet · {len(names)} holders, {max(s['supply'] for s in steps)} bonds, {len(records)} coupons, "
               f"{trades} trades, buyback, tender offer")
    clock = (f'<div><span class="k">Bond time · UTC{T["utcOffsetHours"]:+d}</span><span class="t" id="clk">{esc(s0["when"])}</span>'
             f'<span class="s" id="clk-s">step 1 of {len(steps)} · {esc(s0["label"])}</span></div>'
             f'<div><span class="k">Supply</span><span class="t" id="sup">{s0["supply"]}</span><span class="s">bonds outstanding</span></div>'
             f'<div><span class="k">Vaults</span><span class="t" id="vlt">{s0["vault"]:,.2f}</span><span class="s">cash waiting to be paid</span></div>'
             f'<div><span class="k">Paid to holders</span><span class="t" id="pd" style="color:var(--kept)">{s0["paid"]:,.2f}</span>'
             f'<span class="s">of {total_paid:,.2f}</span></div>')
    controls = (
        '<button class="bs-btn" id="first" title="Issue">|◀</button><button class="bs-btn" id="prev" title="Back one step">◀</button>'
        '<button class="bs-btn play" id="play">Play</button><button class="bs-btn" id="next" title="Forward one step">▶</button>'
        '<button class="bs-btn" id="last" title="Audit">▶|</button>'
        f'<div class="bs-track"><input type="range" id="slider" min="0" max="{len(steps) - 1}" value="0" step="1" '
        'aria-label="Point in the bond\'s life"><div class="bs-ticks" id="ticks"></div></div>'
        '<button class="bs-btn" id="speed" title="Playback speed">1×</button>')
    jumps = '<span class="bs-label" style="align-self:center;margin-right:.3rem">Jump to</span>' + "".join(
        f'<button class="bs-btn" data-go="{i}">{esc(s["label"])}</button>' for i, s in enumerate(steps) if s["jump"])
    rows = "".join(
        f'<tr><td>{esc(n.replace("BH#", "Bond holder #"))}</td><td class="r">{s0["pos"][h]}</td><td></td><td class="r">–</td><td class="r">–</td>'
        f'<td class="r">{s0["cash"][h]:,.2f}</td></tr>' for h, n in enumerate(names))
    def ledger_row(i, w, line, links, why):
        chips = f'<span class="bs-links">{" ".join(links)}</span>' if links else ""
        return (f'<li data-step="{i}"><span class="w">{esc(w)}</span><span class="d"><span>{esc(line)}</span>{chips}'
                f'<span class="why">{esc(why)}</span></span></li>')
    ledger = "".join(ledger_row(i, *row) for i, row in enumerate(log))
    strip = "".join(f'<span data-s="{s}">{s}</span>' for s in
                    ("Issue", "Trade", "Record", "Calculate", "Fund", "Pay", "Redeem", "Offer", "Audit"))
    minutes = round((txs[-1]["t"] - D["creationTime"]) / 60)
    sim = (f"Devnet time is compressed: 1 bond day = {scn['chainSecondsPerBondDay']} chain second, so this two-year bond ran "
           f"in about {minutes} minutes, and transactions seconds apart are days apart in bond time. The cash is a test "
           "token minted by a test bank key; trades are direct transfers between registered holders.")
    foot = (f'<p>Bond token {chip(D["bondMint"][:6] + "…" + D["bondMint"][-4:], D["bondMintLink"])} · program '
            f'{chip("2UkTKYYz…8sHf", D["programLink"])}. Every figure is read from the run\'s {len(txs)} transactions '
            'on devnet: token balances before and after, and the program\'s events in the logs. Measured dataset: '
            f'{chip(f"data/{Path(data_path).name}", f"data/{Path(data_path).name}")}.</p>')
    data = {"holders": names, "steps": steps}
    template = (ROOT / "tools/demo/timeline_template.html").read_text()
    Path(out_path).write_text(fill(template, {
        "EYEBROW": esc(eyebrow), "CLOCK": clock, "CONTROLS": controls, "JUMPS": jumps,
        "NOW": f'<b id="stg">{esc(s0["stage"])}</b><span id="now">{esc(log[0][3])}</span>',
        "ROWS": rows, "COUNT": f"{len(steps)} steps", "LEDGER": ledger, "STRIP": strip, "SIM": esc(sim), "FOOT": foot,
        "DATA": json.dumps(data, separators=(",", ":")),
    }))
    print(f"wrote {out_path}: {len(steps)} steps from {len(txs)} transactions")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: build_timeline.py <measured-dataset.json> <rpc url> <out.html>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
