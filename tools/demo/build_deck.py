"""Build the deck (docs/demo/deck.html) from its template and the measured devnet data.

The template carries the slides; this fills the parts that are lists of measured transactions: the attack tiles
(every attempt of the attacker run, grouped by whose loss it is about, each linking to its failed transaction) and
the keeper run's lanes (who sent each coupon and the redemption). It refuses to write the deck if an attempt was not
refused, belongs to no party or to two, or if a step of the keeper run was sent by neither wallet it names.

Usage: .venv/bin/python3 tools/demo/build_deck.py docs/demo/data/devnet-hostile-attempts.json \\
           docs/demo/data/devnet-keeper.json docs/demo/deck.html
"""

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/demo"))
from build_app import PARTY_ATTEMPTS  # noqa: E402

PARTIES = [("holder", "Holder"), ("issuer", "Issuer"), ("depository", "Depository"), ("bank", "Bank"), ("exchange", "Exchange")]
STEPS = {"PayCoupon": "paid", "RedeemHolder": "paid"}
# Guards that are Solana's token programs or the runtime refusing, not the bond's own program (its rules and account
# checks): the deck draws these tiles outlined, the others filled.
TOKEN_GUARDS = {"MissingRequiredSignature", "owner does not match", "Invalid instruction", "InvalidAccountData", "cannot freeze",
                "custom program error: 0x1f", "Incorrect authority", "Account is frozen"}
OWN_GUARDS = {"ConstraintHasOne", "UnsupportedCashToken", "ConstraintTokenOwner", "NothingToPay", "AccountOwnedByWrongProgram",
              "NotAssociatedTokenAccount", "NotTransferring", "ExceedsIssueSize", "PlacementClosed", "ConstraintRaw", "ExceedsOffer",
              "InsufficientFunding", "NotFunded", "Constraint", "AlreadyPaid", "BondsLocked", "TransfersClosedAtMaturity",
              "WrongEventKind"}


def attacks(H):
    by_id = {a["id"]: a for a in H["attempts"]}
    placed = [i for p, _ in PARTIES for i in PARTY_ATTEMPTS[p]]
    if sorted(placed) != sorted(by_id) or len(placed) != len(set(placed)):
        raise SystemExit("not building: the attempts and the party groups do not match one to one")
    if not all(a["refused"] for a in H["attempts"]):
        raise SystemExit("not building: an attempt was not refused")
    groups = []
    for party, label in PARTIES:
        ids = PARTY_ATTEMPTS[party]
        tiles = "".join(f'<a class="{"tok" if by_id[i]["guard"] in TOKEN_GUARDS else "own"}" '
                        f'href="{html.escape(by_id[i]["explorer"])}" title="{html.escape(by_id[i]["tries"])}">'
                        f'{html.escape(i)}</a>' for i in ids)
        groups.append(f'<div class="grp"><b>{label}<small>{len(ids)} refused</small></b><div class="tiles">{tiles}</div></div>')
    title = f"{len(by_id)} attacks on devnet, {sum(a['refused'] for a in H['attempts'])} refused"
    unknown = {a["guard"] for a in H["attempts"]} - TOKEN_GUARDS - OWN_GUARDS
    if unknown:
        raise SystemExit(f"not building: guards neither the bond's program nor a token program: {sorted(unknown)}")
    return title, "".join(groups), by_id


def relay(K, operator):
    keeper = K["keeperAddress"]
    events, current = [], None
    for s in K["servicing"]:
        if "OpenEvent" in s["instructions"]:
            current = {"senders": set(), "final": None}
            events.append(current)
        if current is None:
            continue
        if any(i in ("OpenEvent", "ConfirmFunding") + tuple(STEPS) for i in s["instructions"]):
            current["senders"].add(s["feePayer"])
        for ins, word in STEPS.items():
            if ins in s["instructions"] and current["final"] is None:
                current["final"] = (word, s["signature"], s["feePayer"])
    if len(events) != 5 or any(e["final"] is None or len(e["senders"]) != 1 for e in events):
        raise SystemExit("not building: the keeper run is not 4 coupons and a redemption, each sent by one wallet")
    if any(e["final"][2] not in (operator, keeper) for e in events):
        raise SystemExit("not building: a step of the keeper run was sent by a wallet the deck does not name")
    heads = ["Coupon 0", "Coupon 1", "Coupon 2", "Coupon 3", "Redemp&shy;tion"]
    out = ['<span></span>'] + [f'<span class="hd">{h}</span>' for h in heads]
    for label, wallet, cls in (("Our operator", operator, "op"), ("Outside wallet, no role", keeper, "out")):
        out.append(f'<span class="who">{label}</span>')
        for e in events:
            word, sig, payer = e["final"]
            out.append(f'<a class="cell {cls}" href="https://explorer.solana.com/tx/{sig}?cluster=devnet">{word}</a>'
                       if payer == wallet else '<span class="cell"></span>')
    return "".join(out)


def main(hostile_path, keeper_path, out_path):
    H = json.loads(Path(hostile_path).read_text())
    K = json.loads(Path(keeper_path).read_text())
    operator = K["servicing"][0]["feePayer"]
    title, tiles, by_id = attacks(H)
    page = (ROOT / "tools/demo/deck_template.html").read_text()
    for key, value in (("<!--__ATTACKS__-->", tiles), ("__ATTACKS_TITLE__", title), ("<!--__RELAY__-->", relay(K, operator)),
                       ("__SEND_UNREGISTERED__", by_id["send-unregistered"]["explorer"]),
                       ("__SECOND_ACCOUNT__", by_id["second-account"]["explorer"])):
        if key not in page:
            raise SystemExit(f"not building: the template has no {key}")
        page = page.replace(key, value)
    Path(out_path).write_text(page)
    print(f"wrote {out_path}: {page.count('class=\"dk-slide\"')} slides; {title}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
