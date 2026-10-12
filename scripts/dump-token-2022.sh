#!/usr/bin/env bash
# Refresh the Token-2022 program used by the LiteSVM tests (programs/bond_servicing/tests/fixtures/).
# The Token-2022 bundled with LiteSVM 0.10 predates the PermissionedBurn extension the bond mint uses, so the
# tests load the program deployed on devnet instead. Prints the sha256 of the dumped program.
# Usage: scripts/dump-token-2022.sh
set -euo pipefail
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"
OUT="$(cd "$(dirname "$0")/.." && pwd)/programs/bond_servicing/tests/fixtures/spl_token_2022_devnet.so"
solana program dump -u devnet TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb "$OUT"
solana program show -u devnet TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb | grep "Last Deployed"
sha256sum "$OUT"
