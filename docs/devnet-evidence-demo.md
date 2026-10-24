# Devnet evidence: DEVNET DEMO: 1,000 face, 10%, quarterly, 1 year; 3 holders; two secondary trades settled by Solana DvP; our operator stops after coupon 0, an outside wallet with no role services coupons 1-3 and the redemption (1 bond day = 1 chain second)

Scenario `devnet-demo` (`scenarios/devnet-demo.json`), run 2026-10-23 10:17:44-10:24:30 UTC (6 min 46 s, 32 transactions) on the Demo Day program `tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i` (branch `demo-day-prep`; the submitted program 2UkTK... is untouched). All amounts in the test cash token (6 decimals, mirrors USDC). Secondary trades settle delivery versus payment through the Solana Foundation's DvP program `dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq`: the seller's bonds and the buyer's cash wait in the trade's escrows and move in one transaction (`ops/src/dvp.ts`, `programs/bond_servicing/src/instructions/admit_trade.rs`).

## Accounts

| What | Address |
|---|---|
| Program (Demo Day build) | [tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i](https://explorer.solana.com/address/tkcoqL7G6s7Ry64uu2QvkxgwMFier7oiCBQ9pksYf8i?cluster=devnet) |
| Solana DvP program | [dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq](https://explorer.solana.com/address/dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq?cluster=devnet) |
| Bond token (Token-2022, transfer hook) | [VeF9b4BH8349mXu8h2nwpi7ai4bzbMyyTMsLc9WGSn3](https://explorer.solana.com/address/VeF9b4BH8349mXu8h2nwpi7ai4bzbMyyTMsLc9WGSn3?cluster=devnet) |
| Bond PDA | [H3kkQ3feJGbTvEgadbate9dQDuKAXgT3FhinD8yTQavY](https://explorer.solana.com/address/H3kkQ3feJGbTvEgadbate9dQDuKAXgT3FhinD8yTQavY?cluster=devnet) |
| Cash token | [E4zTmBpHuACmk8qe9q6mnW8wGkZp95nZDFAoxatrbZ1J](https://explorer.solana.com/address/E4zTmBpHuACmk8qe9q6mnW8wGkZp95nZDFAoxatrbZ1J?cluster=devnet) |

## Secondary trades through Solana DvP

Each trade: `CreateDvp` and `admit_trade` in one transaction (the registrar creates the trade and a record for its escrow, naming the seller); the seller's hooked transfer of bonds into the escrow (they stay the seller's position, `escrowed`); the buyer's transfer of cash into the escrow; `SettleDvp` by the registrar: bonds to the buyer and cash to the seller in one transaction, the transfer hook running inside it, both escrows closed.

| After record moment | Trade | create + admit | bond leg | cash leg | settle (one transaction) |
|---|---|---|---|---|---|
| 0 | BH#1 sells 3 to BH#3 for 2,970.00 | [4RcqP1tKZW...](https://explorer.solana.com/tx/4RcqP1tKZWpkfmFp9ArgmrDJhiGHs8dLMhAEZyGkiqoz4M291UFDwcuuYmX1gBjUkwXhtYYBmMnnUVdsK7zEeVZe?cluster=devnet) | [DFpg3H5DUJ...](https://explorer.solana.com/tx/DFpg3H5DUJbjRaVKs5qA2YM9us4UqQBgghjv4MBLFMfuhJJpMNXKvsKearicgjp8h29ty3GkmeuA5jP74PHkjT2?cluster=devnet) | [5jcHFikR5p...](https://explorer.solana.com/tx/5jcHFikR5pcraCEg7cs4XYF2tT3zwPybj2VJTVh49aCYCdNgkRnYhXn21EPwXVDFXf95jnK2s5GeCxDHL53ovxdy?cluster=devnet) | [2bE3cFkqZE...](https://explorer.solana.com/tx/2bE3cFkqZEvawGBvq1t7JEGhe4HJSX4a2pxbvDb18E7m7R7btHkipDAvH4BaTHq5Sb3gVY78fbpifGsQfoyPfw8E?cluster=devnet) |
| 1 | BH#2 sells 2 to BH#1 for 2,000.00 | [3uBSqEtZdi...](https://explorer.solana.com/tx/3uBSqEtZdiGgKWTz6Q63n8fjq4izHqBupET4DF2CfqCH7h5MV5H4xATnArQKEUbouCpMJV7tetRpvocECTB1TWxq?cluster=devnet) | [4kyAzXJDrE...](https://explorer.solana.com/tx/4kyAzXJDrEcEAtQNUpqf1ctxGMcA3Pw2PVkNjRTh6Xi5x6a3nVdM1s8AkNcj1NHxHcrUfcigLLmUEPzERBCKBAWi?cluster=devnet) | [345126xa76...](https://explorer.solana.com/tx/345126xa76EgvbaL5QHm9RjkZYdwPH43bpQ1WBvwfQSmCXseUhxj9KeDJrC2djQTB54VQWKoT2da1edFztSKkYXo?cluster=devnet) | [45567KWoM3...](https://explorer.solana.com/tx/45567KWoM33sZeaVqNnzbHKiDKvyX3P18WA8LHxttXuPaV5XzuTH3QBwktk1oXf3soY8jdsHiVeqwTWM6xwyGGqw?cluster=devnet) |
| 2 | transfer 1 BH#3 -> BH#2 (plain hooked transfer, no cash leg) | | | | [D1u5yPSvux...](https://explorer.solana.com/tx/D1u5yPSvuxUt5p9RUrM8pFhmCr8BYZp71fKupXa23nFmAdXoW59QQb4fiERxCjYMv5E9JRSQRVeTk8CNdXhAGN6?cluster=devnet) |

The first trade was created, funded and settled after record moment 0 and before coupon 0 was paid: the 3 bonds had left BH#1's account, and BH#1 was still paid on 10 (the record-moment position); BH#3 was paid nothing for coupon 0 and 75.00 on 3 for coupon 1.

## Coupons

| Coupon | Record date | Supply at record | Budget | Paid (holder: position, amount) | Sent and paid for by |
|---|---|---|---|---|---|
| 0 | 2026-06-08 | 15 | 375.00 | BH#1: 10 bonds, [250.00](https://explorer.solana.com/tx/623T3tPos11a6rmMZZ55Zvn8Vp9jghcBdUbWum4JNw1qqyDTep5qScBdUeunZYbJNRP3HtfdoYA9rHXRoGT9XACS?cluster=devnet); BH#2: 5 bonds, [125.00](https://explorer.solana.com/tx/623T3tPos11a6rmMZZ55Zvn8Vp9jghcBdUbWum4JNw1qqyDTep5qScBdUeunZYbJNRP3HtfdoYA9rHXRoGT9XACS?cluster=devnet) | [81tD...k6qK](https://explorer.solana.com/address/81tD4k2CsL8Dt9P95WcTiVuaRvN9yTu61HYCcG8gk6qK?cluster=devnet) (operator) |
| 1 | 2026-09-07 | 15 | 375.00 | BH#1: 7 bonds, [175.00](https://explorer.solana.com/tx/4neEZEiDX5bppL3HxRGuH5Gycy7UP3itjc7frm8Na6uuwGGAfGMN1TXBYx4yEBddvHy2FwPjWxw9YBFSDCyFn1VC?cluster=devnet); BH#2: 5 bonds, [125.00](https://explorer.solana.com/tx/4neEZEiDX5bppL3HxRGuH5Gycy7UP3itjc7frm8Na6uuwGGAfGMN1TXBYx4yEBddvHy2FwPjWxw9YBFSDCyFn1VC?cluster=devnet); BH#3: 3 bonds, [75.00](https://explorer.solana.com/tx/4neEZEiDX5bppL3HxRGuH5Gycy7UP3itjc7frm8Na6uuwGGAfGMN1TXBYx4yEBddvHy2FwPjWxw9YBFSDCyFn1VC?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) (outside keeper) |
| 2 | 2026-12-07 | 15 | 375.00 | BH#1: 9 bonds, [225.00](https://explorer.solana.com/tx/xXwFiuK5QowHjagDrQYkVUy9kLoBY4rXWM7RpSMrmPDsbeiNrqBNip2RVE3VF94AyaTXXHEf396Nt5TzajRZCEF?cluster=devnet); BH#2: 3 bonds, [75.00](https://explorer.solana.com/tx/xXwFiuK5QowHjagDrQYkVUy9kLoBY4rXWM7RpSMrmPDsbeiNrqBNip2RVE3VF94AyaTXXHEf396Nt5TzajRZCEF?cluster=devnet); BH#3: 3 bonds, [75.00](https://explorer.solana.com/tx/xXwFiuK5QowHjagDrQYkVUy9kLoBY4rXWM7RpSMrmPDsbeiNrqBNip2RVE3VF94AyaTXXHEf396Nt5TzajRZCEF?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) (outside keeper) |
| 3 | 2027-03-09 | 15 | 375.00 | BH#1: 9 bonds, [225.00](https://explorer.solana.com/tx/3WnbAVBx8eWEBbitRoS9ViCq3F6gLaTTVLNgFNjwgKkgSfmyR3m5LECB6MNPbfMii4iHuKiUHHym4qV3hyyJR9cm?cluster=devnet); BH#2: 4 bonds, [100.00](https://explorer.solana.com/tx/3WnbAVBx8eWEBbitRoS9ViCq3F6gLaTTVLNgFNjwgKkgSfmyR3m5LECB6MNPbfMii4iHuKiUHHym4qV3hyyJR9cm?cluster=devnet); BH#3: 2 bonds, [50.00](https://explorer.solana.com/tx/3WnbAVBx8eWEBbitRoS9ViCq3F6gLaTTVLNgFNjwgKkgSfmyR3m5LECB6MNPbfMii4iHuKiUHHym4qV3hyyJR9cm?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) (outside keeper) |

## Redemption

Supply at the final record moment 15, budget 15,000.00; sent by the outside keeper.

| Holder | Bonds burned | Principal | Transaction |
|---|---|---|---|
| BH#1 | 9 | 9,000.00 | [3AkTpXHoot...](https://explorer.solana.com/tx/3AkTpXHootW47w3wffC1kuZBjo1MoQejssPSQ9C4oDQipFk8sWvLgtJx7Jh9j8bjrTk73rhmmMNSk8pscBck1LN9?cluster=devnet) |
| BH#2 | 4 | 4,000.00 | [3AkTpXHoot...](https://explorer.solana.com/tx/3AkTpXHootW47w3wffC1kuZBjo1MoQejssPSQ9C4oDQipFk8sWvLgtJx7Jh9j8bjrTk73rhmmMNSk8pscBck1LN9?cluster=devnet) |
| BH#3 | 2 | 2,000.00 | [3AkTpXHoot...](https://explorer.solana.com/tx/3AkTpXHootW47w3wffC1kuZBjo1MoQejssPSQ9C4oDQipFk8sWvLgtJx7Jh9j8bjrTk73rhmmMNSk8pscBck1LN9?cluster=devnet) |

Final bond supply: 0.

## Checks

- Report check against QuantLib/FinancePy (`tools/reference/check_report.py`): ALL CHECKS PASSED, 32 transactions. Cash the parties moved in the DvP trades (2,970.00 and 2,000.00) and the bank's top-up that funded BH#3 are netted out: what the bond delivered is the coupons and the principal.
- Ledger-only audit (`tools/audit/audit.py ... history-accounts`, public devnet RPC): AUDIT PASSED, 23 of 23 checks, 33 transactions measured. The two trade escrows are recognised from the ledger alone: every outflow of bonds from them was a Solana DvP instruction, and their bonds went only to scenario wallets.
- Positions at the record moments (from each holder's on-chain history): coupon 0: BH#1 10, BH#2 5, BH#3 0; coupon 1: BH#1 7, BH#2 5, BH#3 3; coupon 2: BH#1 9, BH#2 3, BH#3 3; coupon 3: BH#1 9, BH#2 4, BH#3 2. Final cash: BH#1 10,845.00 (2,970.00 from the sale, 875.00 of coupons, 9,000.00 principal, less 2,000.00 paid in the second trade), BH#2 6,425.00, BH#3 2,200.00.
