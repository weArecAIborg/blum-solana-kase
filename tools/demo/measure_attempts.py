"""Measure a hostile run from the ledger, independently of the operator's report.

The operator's report (ops/out/devnet-hostile.json, written by ops/src/scenario.ts with ops/src/attacks.ts) says which
attempts were made: who, what, what it would cost, which guard should stop it, and the transaction signature. Here
every signature is fetched again from public RPC:
  - an attempt is refused if its transaction failed; the reason is read from the transaction's own logs;
  - the attacker wallet's successful transactions are listed with every token balance they changed, so the page can
    show whose accounts received cash from them.

Usage: .venv/bin/python3 tools/demo/measure_attempts.py ops/out/devnet-hostile.json https://api.devnet.solana.com \
           docs/demo/data/devnet-hostile-attempts.json
"""

import contextlib
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
import audit as A  # noqa: E402


def reason_of(meta):
    """Why a transaction failed: the Anchor error code, a token program's error text, or the runtime's."""
    logs = meta.get("logMessages") or []
    for line in logs:
        m = re.search(r"Error Code: (\w+)\. Error Number: \d+\. Error Message: (.*)$", line)
        if m:
            return f"{m.group(1)}: {m.group(2)}"
    for line in logs:
        m = re.match(r"^Program log: Error: (.*)$", line)
        if m:
            return m.group(1)
    failed = [m.group(1) for line in logs if (m := re.match(r"^Program \w+ failed: (.*)$", line))]
    return failed[-1] if failed else json.dumps(meta["err"])


def token_changes(tx):
    """Token balance changes of a transaction: (owner, mint, change in base units)."""
    meta = tx["meta"]
    pre = {(b["accountIndex"]): b for b in meta.get("preTokenBalances") or []}
    post = {(b["accountIndex"]): b for b in meta.get("postTokenBalances") or []}
    out = []
    for i in set(pre) | set(post):
        b = post.get(i) or pre[i]
        before = int(pre[i]["uiTokenAmount"]["amount"]) if i in pre else 0
        after = int(post[i]["uiTokenAmount"]["amount"]) if i in post else 0
        if after != before:
            out.append({"owner": b["owner"], "mint": b["mint"], "change": after - before})
    return out


def main(report_path, rpc_url, out_path):
    report = json.loads(Path(report_path).read_text())
    scn = json.loads((ROOT / "scenarios" / f"{report['scenario']}.json").read_text())
    rpc = A.Rpc(rpc_url)
    get = lambda sig: rpc.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                        "commitment": "confirmed"}])
    attacker = A.pubkey_of(scn["attacker"]["wallet"])
    holders = {A.pubkey_of(path): name for name, path in scn["wallets"].items()}

    attempts = []
    for a in report["attempts"]:
        tx = get(a["signature"])
        failed = tx["meta"]["err"] is not None
        reason = reason_of(tx["meta"]) if failed else ""
        attempts.append({k: a.get(k) for k in ("id", "when", "actor", "tries", "wouldCost", "guard", "signature", "explorer")}
                        | {"refused": failed, "reason": reason, "byGuard": failed and a["guard"] in reason,
                           "blockTime": tx["blockTime"]})

    # The attacker wallet's successful transactions: every one the operator logged with the attacker as fee payer.
    serviced = []
    for step in report["transactions"]:
        tx = get(step["signature"])
        keys = tx["transaction"]["message"]["accountKeys"]
        if keys[0]["pubkey"] != attacker or tx["meta"]["err"] is not None:
            continue
        changes = token_changes(tx)
        serviced.append({
            "step": step["step"], "signature": step["signature"], "explorer": step.get("explorer"),
            "cashTo": sorted({holders.get(c["owner"], c["owner"]) for c in changes if c["mint"] == report["cashMint"] and c["change"] > 0}),
            "cashToAttacker": sum(c["change"] for c in changes if c["mint"] == report["cashMint"] and c["owner"] == attacker and c["change"] > 0),
            "bondsToAttacker": sum(c["change"] for c in changes if c["mint"] == report["bondMint"] and c["owner"] == attacker and c["change"] > 0),
        })

    # The independent audit of the same run (its own measurement; failed transactions change no balance).
    with contextlib.redirect_stdout(io.StringIO()):
        audit_exit = A.main(str(ROOT / "scenarios" / f"{report['scenario']}.json"), rpc_url, "history-accounts")
    audit = {"passed": audit_exit == 0, "checks": [{"id": cid, "label": label, "ok": ok} for cid, label, ok in A.results],
             "transactions": A.last_audit["transactions"]}

    out = {
        "scenario": report["scenario"], "name": report["name"], "bondMint": report["bondMint"], "cashMint": report["cashMint"],
        "attacker": attacker, "victim": scn["attacker"]["victim"], "frozenCash": scn["attacker"]["frozenCash"],
        "attempts": attempts, "serviced": serviced, "audit": audit,
        "measured": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "rpc": rpc_url,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(out, indent=1) + "\n")
    refused = sum(1 for a in attempts if a["refused"])
    by_guard = sum(1 for a in attempts if a["byGuard"])
    print(f"wrote {out_path}: {refused} of {len(attempts)} attempts refused ({by_guard} by their named guard); "
          f"attacker wallet: {len(serviced)} successful transactions, cash to itself "
          f"{sum(s['cashToAttacker'] for s in serviced)}, bonds to itself {sum(s['bondsToAttacker'] for s in serviced)}; "
          f"audit {sum(1 for c in audit['checks'] if c['ok'])} of {len(audit['checks'])}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: measure_attempts.py <ops/out/devnet-NAME.json> <rpc url> <out.json>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
