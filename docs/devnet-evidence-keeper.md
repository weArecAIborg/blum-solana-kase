# Devnet evidence: DEVNET KEEPER: B1 terms, 3 holders; our operator stops after coupon 0, an outside wallet with no role services coupons 1-3 and the redemption after the final record moment (1 bond day = 1 chain second)

Scenario `devnet-keeper`. All amounts in the test cash token (6 decimals, mirrors USDC).

## Accounts

| What | Address |
|---|---|
| Program | [2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf](https://explorer.solana.com/address/2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf?cluster=devnet) |
| Bond token | [6MC9HhT1wzo6FFxt5fwgtsDaThz3Ts3H1A7C1PiZQdK7](https://explorer.solana.com/address/6MC9HhT1wzo6FFxt5fwgtsDaThz3Ts3H1A7C1PiZQdK7?cluster=devnet) |
| Bond PDA (mint, delegate and burn authority) | [AMEZgVEu9Yi1WXyS6BveuCV5omjEKgjmj6cKeCD8W1D7](https://explorer.solana.com/address/AMEZgVEu9Yi1WXyS6BveuCV5omjEKgjmj6cKeCD8W1D7?cluster=devnet) |
| Cash token | [8BiPAkyEiLRpK3dkJdJzZJd4J6TWddrSfKSCExQT5GX](https://explorer.solana.com/address/8BiPAkyEiLRpK3dkJdJzZJd4J6TWddrSfKSCExQT5GX?cluster=devnet) |

## Coupons

| Coupon | Record date | Supply at record | Budget | Paid (holder: amount) | Sent and paid for by |
|---|---|---|---|---|---|
| 0 | 2026-09-07 | 15 | 750.00 | BH#1: [500.00](https://explorer.solana.com/tx/2gatkyPitinZAka2DJhyxdDHhnVRwxWKjreBxdn3uPvGC6jxb7TDkQfjw1A8fDKYhm7VubtcJhQHoGRzWKXBQRp5?cluster=devnet); BH#2: [250.00](https://explorer.solana.com/tx/2gatkyPitinZAka2DJhyxdDHhnVRwxWKjreBxdn3uPvGC6jxb7TDkQfjw1A8fDKYhm7VubtcJhQHoGRzWKXBQRp5?cluster=devnet) | [81tD...k6qK](https://explorer.solana.com/address/81tD4k2CsL8Dt9P95WcTiVuaRvN9yTu61HYCcG8gk6qK?cluster=devnet) |
| 1 | 2027-03-09 | 15 | 750.00 | BH#1: [350.00](https://explorer.solana.com/tx/ZyhJRmZZhasapRT99tebrdUHq9ovQQSeZXosX6LVEjcJu1dzLccDLrTRbahEMeXLM3RvD2RKzRHMJBdChKpuBSb?cluster=devnet); BH#2: [250.00](https://explorer.solana.com/tx/ZyhJRmZZhasapRT99tebrdUHq9ovQQSeZXosX6LVEjcJu1dzLccDLrTRbahEMeXLM3RvD2RKzRHMJBdChKpuBSb?cluster=devnet); BH#3: [150.00](https://explorer.solana.com/tx/ZyhJRmZZhasapRT99tebrdUHq9ovQQSeZXosX6LVEjcJu1dzLccDLrTRbahEMeXLM3RvD2RKzRHMJBdChKpuBSb?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) |
| 2 | 2027-09-06 | 15 | 750.00 | BH#1: [450.00](https://explorer.solana.com/tx/xXtWHVtLvhU6aejPPvhP8UoQnJmGV4xaJsa4AxUCuL2YegKQGeEbEY77XZC87ri5oQc9mP18pydtqK3WxzAMuYa?cluster=devnet); BH#2: [150.00](https://explorer.solana.com/tx/xXtWHVtLvhU6aejPPvhP8UoQnJmGV4xaJsa4AxUCuL2YegKQGeEbEY77XZC87ri5oQc9mP18pydtqK3WxzAMuYa?cluster=devnet); BH#3: [150.00](https://explorer.solana.com/tx/xXtWHVtLvhU6aejPPvhP8UoQnJmGV4xaJsa4AxUCuL2YegKQGeEbEY77XZC87ri5oQc9mP18pydtqK3WxzAMuYa?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) |
| 3 | 2028-03-06 | 15 | 750.00 | BH#1: [450.00](https://explorer.solana.com/tx/4aBVeYQyXNwcpq8362WbJfeXhtL5vCY2rG3HdPqahh6cCXTyM5to7R5tQaWw3ynQhTBAfFnrDVuUu2fnUDR1sfFv?cluster=devnet); BH#2: [200.00](https://explorer.solana.com/tx/4aBVeYQyXNwcpq8362WbJfeXhtL5vCY2rG3HdPqahh6cCXTyM5to7R5tQaWw3ynQhTBAfFnrDVuUu2fnUDR1sfFv?cluster=devnet); BH#3: [100.00](https://explorer.solana.com/tx/4aBVeYQyXNwcpq8362WbJfeXhtL5vCY2rG3HdPqahh6cCXTyM5to7R5tQaWw3ynQhTBAfFnrDVuUu2fnUDR1sfFv?cluster=devnet) | [9epk...FxGp](https://explorer.solana.com/address/9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp?cluster=devnet) |

## Trades after record moments

| After record moment | Trade | Transaction |
|---|---|---|
| 0 | transfer 3 BH#1 -> BH#3 | [3TfFichRAo...](https://explorer.solana.com/tx/3TfFichRAoRnuWQdaZbmyXHTVHpSgsrKmZbXVNm9s77mojnjk3rXT8KhSKZqHfYerCoQHWwVHg47t9nJqYjeZonA?cluster=devnet) |
| 1 | transfer 2 BH#2 -> BH#1 | [QNdHc8RS4f...](https://explorer.solana.com/tx/QNdHc8RS4fGbMBYycwi5CrH7TN9oFGGi9aL85bYyX4LVAKDiaa1umZ6Z9WBpuGr3aBPZnwV6zt6qu6aDrpv3Fpo?cluster=devnet) |
| 2 | transfer 1 BH#3 -> BH#2 | [sJ8nv7Zpg2...](https://explorer.solana.com/tx/sJ8nv7Zpg2LmcAG4PyNSdJc5BKPNAFAMT7o3fYpK7bodZ6cAH6n8xYUWAos4EGinRcp64Jvuv7u47ijhFyrXodf?cluster=devnet) |

## Redemption

Supply at the final record moment 15, budget 15,000.00.

| Holder | Bonds burned | Principal | Transaction |
|---|---|---|---|
| BH#1 | 9 | 9,000.00 | [5DXodjnM4t...](https://explorer.solana.com/tx/5DXodjnM4tRpD7XBh6nr35mWoepj3LdThtWX3EAwXuLkHzmxVB7MKZz8wcExtti2Hg5NCYxysKGJxQxx2tpyiBYD?cluster=devnet) |
| BH#2 | 4 | 4,000.00 | [5DXodjnM4t...](https://explorer.solana.com/tx/5DXodjnM4tRpD7XBh6nr35mWoepj3LdThtWX3EAwXuLkHzmxVB7MKZz8wcExtti2Hg5NCYxysKGJxQxx2tpyiBYD?cluster=devnet) |
| BH#3 | 2 | 2,000.00 | [5DXodjnM4t...](https://explorer.solana.com/tx/5DXodjnM4tRpD7XBh6nr35mWoepj3LdThtWX3EAwXuLkHzmxVB7MKZz8wcExtti2Hg5NCYxysKGJxQxx2tpyiBYD?cluster=devnet) |

## Checks

- Ledger-only audit (public devnet RPC): AUDIT PASSED, 23 of 23 checks, 26 transactions measured (2026-10-11; the run has its own cash token); report check against QuantLib/FinancePy: ALL CHECKS PASSED, 25 transactions. Our operator stopped after coupon 0; every later open, confirm, pay and redeem transaction was sent and paid for by the outside wallet 9epkkMCfXjFoDKMoBrG4mE1Pw9BRvPvmyhctxbrHFxGp.
- Final bond supply: 0.
