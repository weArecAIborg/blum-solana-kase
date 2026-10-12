#!/usr/bin/env bash
# Run one bond's full lifecycle on devnet (program already deployed with ./deploy.sh), then check it twice:
# against QuantLib/FinancePy (report) and with the independent ledger audit (measured balances via RPC).
# Usage: scripts/run-devnet.sh <scenario name>   (reads scenarios/devnet-<name>.json)
# Each run needs a fresh bond mint: a mint address can be created only once on a cluster.
set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ] || { [ $# -eq 2 ] && [ "$2" != resume ]; }; then
  echo "usage: $0 <scenario name> [resume]   (scenarios/devnet-<name>.json; resume: continue a run that stopped after creating its bond)" >&2
  exit 2
fi
SCENARIO=$1
RESUME=${2:-}
SCENARIO_FILE="scenarios/devnet-$SCENARIO.json"
RPC=https://api.devnet.solana.com
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
cd "$ROOT"
[ -f "$SCENARIO_FILE" ] || { echo "missing $SCENARIO_FILE" >&2; exit 2; }

BOND_MINT=$(solana-keygen pubkey "$(jq -r .keys.bondMint "$SCENARIO_FILE")")
if [ -z "$RESUME" ] && solana account "$BOND_MINT" --url "$RPC" >/dev/null 2>&1; then
  echo "bond mint $BOND_MINT already exists on devnet; generate a new keypair at $(jq -r .keys.bondMint "$SCENARIO_FILE")" >&2
  exit 1
fi

CASH_MINT_KEYPAIR=$(jq -r .keys.cashMint "$SCENARIO_FILE")
if ! solana account "$(solana-keygen pubkey "$CASH_MINT_KEYPAIR")" --url "$RPC" >/dev/null 2>&1; then
  echo "== create test cash token on devnet"
  scripts/create-cash-token.sh "$RPC" "$HOME/.config/solana/id.json" "$(jq -r .keys.cashAuthority "$SCENARIO_FILE")" \
    "$CASH_MINT_KEYPAIR"
fi

echo "== lifecycle on devnet ($SCENARIO)"
REPORT="ops/out/devnet-$SCENARIO.json"
(cd ops && npx tsx src/scenario.ts "../$SCENARIO_FILE" "../$REPORT" "$RPC" devnet)

echo "== independent check (QuantLib / FinancePy)"
.venv/bin/python3 tools/reference/check_report.py "$REPORT"

echo "== independent ledger audit (devnet RPC)"
.venv/bin/python3 tools/audit/audit.py "$SCENARIO_FILE" "$RPC" history-accounts
