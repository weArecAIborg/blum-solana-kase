"""Tampering probe: change the ledger outside the program by the smallest possible amount, then run the independent
audit and record which checks catch it.

Probe: a holder sends 0.000001 of the cash token straight into one of the bond's vaults (cash entering a vault not
from the issuer's bank). Run only on a local network (it really moves tokens).

Usage: .venv/bin/python3 tools/audit/tamper_probe.py <scenarios/NAME.json> <local rpc url> <out.json>
"""

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
import audit as A  # noqa: E402


def run_audit(scenario, rpc_url):
    A.results.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        code = A.main(scenario, rpc_url, "program-accounts")
    return code, list(A.results)


def main(scenario, rpc_url, out_path):
    if "127.0.0.1" not in rpc_url and "localhost" not in rpc_url:
        sys.exit("the probe moves tokens; run it on a local network only")
    scn = json.loads(Path(scenario).read_text())
    cash_mint = A.pubkey_of(scn["keys"]["cashMint"])
    _, vaults = A.bond_vaults(A.Rpc(rpc_url), A.pubkey_of(scn["keys"]["bondMint"]), cash_mint)
    holder, keyfile = next(iter(scn["wallets"].items()))

    before_code, before = run_audit(scenario, rpc_url)
    env = dict(os.environ, PATH=f"{Path.home()}/.local/share/solana/install/active_release/bin:{os.environ['PATH']}")
    out = subprocess.run(
        # The holder signs as owner; the local wallet that ran the scenario pays the fee (holders hold no SOL).
        ["spl-token", "transfer", cash_mint, "0.000001", vaults[0], "--owner", str(ROOT / keyfile),
         "--fee-payer", str(Path.home() / ".config/solana/id.json"), "--url", rpc_url],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    signature = next(line.split()[-1] for line in out.splitlines() if line.startswith("Signature"))
    after_code, after = run_audit(scenario, rpc_url)

    result = {
        "probe": f"{holder} sends 0.000001 cash straight into a bond vault (not through the program, not from the issuer)",
        "network": rpc_url, "signature": signature, "vault": vaults[0],
        "before": {"passed": before_code == 0, "checks": len(before), "failed": [f"{c} {l}" for c, l, ok in before if not ok]},
        "after": {"passed": after_code == 0, "checks": len(after), "failed": [f"{c} {l}" for c, l, ok in after if not ok]},
    }
    Path(out_path).write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: tamper_probe.py <scenarios/NAME.json> <local rpc url> <out.json>", file=sys.stderr)
        sys.exit(2)
    main(*sys.argv[1:])
