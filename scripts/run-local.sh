#!/usr/bin/env bash
# Run one bond's full lifecycle on a fresh local Surfpool network and check every amount against QuantLib/FinancePy.
# Usage: scripts/run-local.sh <scenario name>   (reads scenarios/<name>.json)
# Leaves Surfpool running so the transactions can be inspected in Surfpool Studio (http://127.0.0.1:18488).
# A fresh network per run is required: Surfpool's clock only moves forward.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <scenario name>   (scenarios/<name>.json)" >&2
  exit 2
fi
SCENARIO=$1
SCENARIO_FILE="scenarios/$SCENARIO.json"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RPC=http://127.0.0.1:8899
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd "$ROOT"
[ -f "$SCENARIO_FILE" ] || { echo "missing $SCENARIO_FILE" >&2; exit 2; }

echo "== build program"
anchor build >/dev/null

echo "== fresh Surfpool (offline)"
pkill -f 'surfpool start' 2>/dev/null && sleep 2
mkdir -p .surfpool
nohup surfpool start --no-tui --offline --no-deploy > .surfpool/surfpool.log 2>&1 &
for _ in $(seq 1 60); do
  solana cluster-version --url "$RPC" >/dev/null 2>&1 && break
  sleep 1
done
solana cluster-version --url "$RPC" >/dev/null

echo "== deploy program"
solana program deploy target/deploy/bond_servicing.so --program-id target/deploy/bond_servicing-keypair.json \
  --url "$RPC" | tail -1

echo "== test cash token"
scripts/create-cash-token.sh "$RPC" "$HOME/.config/solana/id.json" "$(jq -r .keys.cashAuthority "$SCENARIO_FILE")" \
  "$(jq -r .keys.cashMint "$SCENARIO_FILE")" | grep -E "^Address|^Signature" | head -2

echo "== lifecycle ($SCENARIO)"
REPORT="ops/out/local-$SCENARIO.json"  # not ops/out/devnet-*.json: a devnet run's report must survive a local rehearsal
(cd ops && npx tsx src/scenario.ts "../$SCENARIO_FILE" "../$REPORT" "$RPC" localnet)

echo "== independent check (QuantLib / FinancePy)"
.venv/bin/python3 tools/reference/check_report.py "$REPORT"

echo "== independent ledger audit (measured balances vs scenario terms; expertise/independent-ledger-audit.md)"
.venv/bin/python3 tools/audit/audit.py "$SCENARIO_FILE" "$RPC" program-accounts
