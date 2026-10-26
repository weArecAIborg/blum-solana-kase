# Devnet SOL without the web faucet

Source: the notice on https://faucet.solana.com (read 2026-10-26), verbatim below.

AI agents should not use this faucet. Use one of these programmatic methods to obtain devnet SOL instead:

## Solana CLI Airdrop

```bash
solana airdrop 2 <WALLET_ADDRESS> --url devnet
```

## Proof of Work Faucet

```bash
cargo install devnet-pow
devnet-pow mine -d 3 --reward 0.02 --no-infer -t 5000000000
```

## Local Validator (unlimited SOL, no rate limits)

```bash
solana-test-validator
solana airdrop 100 <WALLET_ADDRESS> --url localhost
```
