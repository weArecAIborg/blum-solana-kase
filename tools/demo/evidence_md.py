"""Write a devnet evidence page (Markdown) from an operator report: accounts, every action with its explorer link.

The numbers here are the operator's; the independent proof is the audit (tools/audit/audit.py), whose verdict the
caller passes in. Usage: .venv/bin/python3 tools/demo/evidence_md.py <report.json> <audit verdict line> <out.md>
"""

import json
import sys
from decimal import Decimal
from pathlib import Path


def main(report_path, audit_line, out_path):
    r = json.loads(Path(report_path).read_text())
    unit = 10 ** r["terms"]["cashDecimals"]
    ex = lambda kind, v: f"https://explorer.solana.com/{kind}/{v}?cluster=devnet"
    money = lambda units: f"{Decimal(units) / unit:,.2f}"
    txs = {t["signature"]: t for t in r["transactions"]}
    link = lambda sig, text=None: f"[{text or sig[:10] + '...'}]({ex('tx', sig)})"
    L = [f"# Devnet evidence: {r['name']}", "",
         f"Scenario `{r['scenario']}`. All amounts in the test cash token (6 decimals, mirrors USDC).", "",
         "## Accounts", "", "| What | Address |", "|---|---|",
         f"| Program | [{r['programId']}]({ex('address', r['programId'])}) |",
         f"| Bond token | [{r['bondMint']}]({ex('address', r['bondMint'])}) |",
         f"| Bond PDA (mint, delegate and burn authority) | [{r['bond']}]({ex('address', r['bond'])}) |",
         f"| Cash token | [{r['cashMint']}]({ex('address', r['cashMint'])}) |", ""]
    L += ["## Coupons", "", "| Coupon | Record date | Supply at record | Budget | Paid (holder: amount) | Sent and paid for by |",
          "|---|---|---|---|---|---|"]
    for ev in sorted(r["events"], key=lambda e: e["index"]):
        paid = "; ".join(f"{p['holder']}: {link(p['signature'], money(p['amount']))}" for p in ev["payments"])
        by = ev.get("servicedBy")
        who = f"[{by[:4]}...{by[-4:]}]({ex('address', by)})" if by else ""
        L.append(f"| {ev['index']} | {ev['recordDate']} | {ev['supplyAtRecord']} | {money(ev['budget'])} | {paid} | {who} |")
    L += ["", "## Trades after record moments", "", "| After record moment | Trade | Transaction |", "|---|---|---|"]
    for t in r["transactions"]:
        if t["step"].startswith("transfer "):
            L.append(f"| {t['step'].split('after record moment ')[-1].rstrip(')')} | {t['step'].split(' (after')[0]} | {link(t['signature'])} |")
    L += ["", "## Offers (third corporate action)", ""]
    for o in r["offers"]:
        L += [f"**Offer {o['id']} ({o['kind']})**: price {money(o['priceUnits'])} per bond, maximum {o['maxQuantity']}, "
              f"window {o['openTime']} - {o['closeTime']} (bond time), vault left {money(o['vaultLeft'])}.", "",
              "| Holder | Action | Quantity | Bonds bought | Cash paid | Transaction |", "|---|---|---|---|---|---|"]
        for a in o["actions"]:
            L.append(f"| {a['holder']} | {a['action']} | {a['quantity']} | {a.get('burned', '')} | "
                     f"{money(a['paid']) if 'paid' in a else ''} | {link(a['signature'])} |")
        for st in o["settlements"]:
            L.append(f"| {st['holder']} | settle (tendered {st['tendered']}) | | {st['burned']} | {money(st['paid'])} | {link(st['signature'])} |")
        L.append("")
    red = r["redemption"]
    L += ["## Redemption", "", f"Supply at the final record moment {red['supplyAtRecord']}, budget {money(red['budget'])}.", "",
          "| Holder | Bonds burned | Principal | Transaction |", "|---|---|---|---|"]
    for x in red["redemptions"]:
        L.append(f"| {x['holder']} | {x['burned']} | {money(x['principal'])} | {link(x['signature'])} |")
    L += ["", "## Checks", "", f"- Ledger-only audit (public devnet RPC): {audit_line}",
          f"- Final bond supply: {r['final']['supply']}.", ""]
    Path(out_path).write_text("\n".join(L))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: evidence_md.py <report.json> <audit verdict line> <out.md>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
