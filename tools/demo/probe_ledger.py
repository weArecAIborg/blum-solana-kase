"""Finalized RPC records of every attempt in the attacker run, the auditor supplement of the stages page.

Reads the attempts measured by measure_attempts.py and fetches each transaction again from the public RPC at finalized
commitment, unchanged (jsonParsed), so a reader can check every refusal without trusting our measurement.
Python standard library only.

Usage: .venv/bin/python3 tools/demo/probe_ledger.py docs/demo/data/devnet-hostile-attempts.json \
           https://api.devnet.solana.com docs/demo/data/stages-probe-ledger.json
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def call(rpc, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": "probe", "method": method, "params": params}).encode()
    for attempt in range(8):
        req = urllib.request.Request(rpc, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(2 ** attempt)
            continue
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply
    raise RuntimeError(f"{method}: rate limited 8 times")


def main(attempts_path, rpc, out_path):
    H = json.loads(Path(attempts_path).read_text())
    transactions = {}
    for a in H["attempts"]:
        reply = call(rpc, "getTransaction", [a["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                                               "maxSupportedTransactionVersion": 0}])
        if reply["result"] is None:
            raise RuntimeError(f"{a['id']}: {a['signature']} is not finalized")
        transactions[a["id"]] = reply
    Path(out_path).write_text(json.dumps({"rpc": rpc, "commitment": "finalized",
                                          "verifiedAt": datetime.now(timezone.utc).isoformat(),
                                          "transactions": transactions}, indent=1) + "\n")
    print(f"wrote {out_path}: {len(transactions)} finalized transactions")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: probe_ledger.py <devnet-hostile-attempts.json> <rpc url> <out.json>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
