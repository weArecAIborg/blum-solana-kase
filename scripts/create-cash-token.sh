#!/usr/bin/env bash
# Create the test cash token (simulated stablecoin) that pays coupons and principal.
# Mirrors USDC on Solana mainnet (EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v): classic SPL Token program,
# 6 decimals, freeze authority set. The mint keypair fixes the address, so re-running on a fresh local
# network recreates the same mint.
set -euo pipefail

if [ $# -ne 4 ]; then
  echo "usage: $0 <rpc-url> <fee-payer-keypair> <cash-authority-keypair> <cash-mint-keypair>" >&2
  exit 2
fi
RPC=$1
PAYER=$2
AUTHORITY=$3
MINT=$4

for f in "$PAYER" "$AUTHORITY" "$MINT"; do
  [ -f "$f" ] || { echo "keypair not found: $f" >&2; exit 1; }
done

SPL_TOKEN_PROGRAM=TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
DECIMALS=6
AUTHORITY_PUBKEY=$(solana-keygen pubkey "$AUTHORITY")

# The cash authority is both mint and freeze authority: it plays the bank / cash rail.
spl-token create-token \
  --url "$RPC" \
  --fee-payer "$PAYER" \
  --program-id "$SPL_TOKEN_PROGRAM" \
  --decimals "$DECIMALS" \
  --mint-authority "$AUTHORITY_PUBKEY" \
  --enable-freeze \
  "$MINT"

spl-token display --url "$RPC" "$(solana-keygen pubkey "$MINT")"
