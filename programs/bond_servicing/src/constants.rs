use anchor_lang::prelude::*;

#[constant]
pub const BOND_SEED: &[u8] = b"bond";

#[constant]
pub const HOLDER_SEED: &[u8] = b"holder";

#[constant]
pub const EVENT_SEED: &[u8] = b"event";

/// Cash account of one event: ["vault", event].
#[constant]
pub const VAULT_SEED: &[u8] = b"vault";

#[constant]
pub const OFFER_SEED: &[u8] = b"offer";

#[constant]
pub const TENDER_SEED: &[u8] = b"tender";

/// Seed fixed by the transfer-hook interface.
pub const EXTRA_ACCOUNT_METAS_SEED: &[u8] = b"extra-account-metas";

/// Solana DvP (solana-foundation/dvp): atomic delivery-versus-payment settlement. A secondary trade's bonds wait in
/// the trade's escrow (the `SwapDvp` PDA's associated token account) until the DvP program settles or unwinds it.
pub const DVP_PROGRAM_ID: Pubkey = pubkey!("dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq");
/// `SwapDvp` account (dvp v1.0.0, `program/src/state/swap_dvp.rs`): fixed size, no discriminator.
pub const SWAP_DVP_LEN: usize = 458;

/// Up to 40 coupon periods (20 years semi-annual, 40 years annual).
pub const MAX_PERIODS: usize = 40;

/// Largest accepted gap, either way, between an unadjusted coupon date and its business-day payment date.
pub const MAX_PAYMENT_DELAY_DAYS: i64 = 14;

/// Basis points in 100%.
pub const BPS_100_PERCENT: u32 = 10_000;

/// Token-2022 PermissionedBurn extension (solana-program/token-2022, interface 3.1.2 / program 11.1.0; the
/// interface crate bundled with anchor-spl 1.1.2 predates it, so the instructions are encoded here).
pub const PERMISSIONED_BURN_EXTENSION: u8 = 46;
pub const PERMISSIONED_BURN_INITIALIZE: u8 = 0;
pub const PERMISSIONED_BURN_BURN_CHECKED: u8 = 2;
/// TLV header (type u16 + length u16) plus the config (the 32-byte burn authority).
pub const PERMISSIONED_BURN_MINT_SPACE: usize = 4 + 32;
