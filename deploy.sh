#!/usr/bin/env bash
# Deploy (or upgrade) the bond_servicing program to a Solana cluster. Run from the repo root.
# Usage: ./deploy.sh <cluster rpc url>        e.g. ./deploy.sh https://api.devnet.solana.com
#   There is no server to SSH into: the "server" is the Solana cluster; the program keypair fixes the address
#   (2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf), the default Solana wallet pays and is the upgrade authority.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <cluster rpc url>" >&2
  exit 2
fi
cd "$(dirname "$0")"
RPC=$1
export PATH="$HOME/.local/share/solana/install/active_release/bin:$HOME/.cargo/bin:$PATH"

echo "== build"
anchor build >/dev/null
SO=target/deploy/bond_servicing.so
PROGRAM_KEYPAIR=target/deploy/bond_servicing-keypair.json
PROGRAM_ID=$(solana-keygen pubkey "$PROGRAM_KEYPAIR")

echo "== funds"
NEED=$(solana rent "$(stat -c%s "$SO")" --url "$RPC" | awk '/Rent-exempt minimum/ {print $3}')
HAVE=$(solana balance --url "$RPC" | awk '{print $1}')
echo "wallet $(solana address): $HAVE SOL; program rent $NEED SOL (a deploy also needs a temporary buffer of similar size)"

# An upgrade larger than the deployed program needs a larger program account first; the loader extends it by at least
# 10,240 bytes, a smaller request (what the CLI asks for by itself) is refused.
CURRENT=$(solana program show "$PROGRAM_ID" --url "$RPC" 2>/dev/null | awk '/Data Length:/ {print $3}' || true)
SIZE=$(stat -c%s "$SO")
if [ -n "$CURRENT" ] && [ "$SIZE" -gt "$CURRENT" ]; then
  GROW=$(( SIZE - CURRENT < 10240 ? 10240 : SIZE - CURRENT ))
  echo "== extend the program account by $GROW bytes ($CURRENT -> $SIZE needed)"
  solana program extend "$PROGRAM_ID" "$GROW" --url "$RPC"
fi

echo "== deploy $PROGRAM_ID"
solana program deploy "$SO" --program-id "$PROGRAM_KEYPAIR" --url "$RPC"
solana program show "$PROGRAM_ID" --url "$RPC"
