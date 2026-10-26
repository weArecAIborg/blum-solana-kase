//! Admit a secondary trade settled by Solana DvP (solana-foundation/dvp, `DVP_PROGRAM_ID`). The DvP program escrows
//! both legs in the `SwapDvp` PDA's associated token accounts and settles them in one transaction; it knows nothing
//! about holders or record moments. This instruction, signed by the trade's seller (the registrar takes no part in a
//! trade between holders; the deposit is paid by any payer), creates a record for the trade's bond escrow so the
//! transfer hook lets bonds in: they stay the seller's position, locked, until the DvP program moves
//! them to the buyer (settle) or back to the seller (cancel, reclaim). The record names the seller (`escrow_of`),
//! which the hook reads to update the seller's position at settlement.
//! Checked here, from the `SwapDvp` account itself: this bond's tokens against this bond's cash, between two
//! registered holders, delivered to themselves; the signer is the seller. Who may hold bonds stays the registrar's
//! decision: both parties hold records it created (register_holder).

use anchor_lang::prelude::*;

use crate::constants::*;
use crate::error::BondError;
use crate::events::TradeAdmitted;
use crate::state::{Bond, HolderRecord, PositionHistory};

/// `SwapDvp` fields read here (dvp `program/src/state/swap_dvp.rs`, `SwapDvp::to_bytes`): bump u8; user_a, user_b,
/// mint_a, mint_b, settlement_authority, token_program_a, token_program_b (32 each); amount_a, amount_b, expiry,
/// nonce (8 each); ref_string [64]; user_a_settlement_destination, user_b_settlement_destination (32 each); ...
struct SwapDvpTerms {
    user_a: Pubkey,
    user_b: Pubkey,
    mint_a: Pubkey,
    mint_b: Pubkey,
    amount_a: u64,
    amount_b: u64,
    destination_a: Pubkey,
    destination_b: Pubkey,
}

fn read_swap_dvp(info: &AccountInfo) -> Result<SwapDvpTerms> {
    require_keys_eq!(*info.owner, DVP_PROGRAM_ID, BondError::InvalidTrade);
    let d = info.try_borrow_data()?;
    require!(d.len() == SWAP_DVP_LEN, BondError::InvalidTrade);
    let key = |at: usize| Pubkey::try_from(&d[at..at + 32]).map_err(|_| error!(BondError::InvalidTrade));
    let num = |at: usize| d[at..at + 8].try_into().map(u64::from_le_bytes).map_err(|_| error!(BondError::InvalidTrade));
    Ok(SwapDvpTerms {
        user_a: key(1)?,
        user_b: key(33)?,
        mint_a: key(65)?,
        mint_b: key(97)?,
        amount_a: num(225)?,
        amount_b: num(233)?,
        destination_a: key(321)?,
        destination_b: key(353)?,
    })
}

#[derive(Accounts)]
pub struct AdmitTrade<'info> {
    /// Pays the escrow record's deposit: the seller, the venue, anyone.
    #[account(mut)]
    pub payer: Signer<'info>,

    /// The trade's seller (`user_a`) admits its own trade.
    pub seller_wallet: Signer<'info>,

    pub bond: Box<Account<'info, Bond>>,

    /// CHECK: Solana DvP `SwapDvp` record; owner, size and terms are checked in `read_swap_dvp`.
    pub swap_dvp: UncheckedAccount<'info>,

    /// The seller's record: `user_a` of the trade.
    #[account(
        has_one = bond,
        seeds = [HOLDER_SEED, bond.mint.as_ref(), seller.owner.as_ref()],
        bump = seller.bump,
        constraint = seller.owner == seller_wallet.key() @ BondError::InvalidTrade,
    )]
    pub seller: Box<Account<'info, HolderRecord>>,

    /// The buyer's record: `user_b` of the trade.
    #[account(has_one = bond, seeds = [HOLDER_SEED, bond.mint.as_ref(), buyer.owner.as_ref()], bump = buyer.bump)]
    pub buyer: Box<Account<'info, HolderRecord>>,

    /// The escrow's record: the `SwapDvp` PDA as owner, so the hook finds it at the holder seeds.
    #[account(
        init,
        payer = payer,
        space = 8 + HolderRecord::INIT_SPACE,
        seeds = [HOLDER_SEED, bond.mint.as_ref(), swap_dvp.key().as_ref()],
        bump,
    )]
    pub escrow: Box<Account<'info, HolderRecord>>,

    pub system_program: Program<'info, System>,
}

pub fn handle_admit_trade(ctx: Context<AdmitTrade>) -> Result<()> {
    let bond = &ctx.accounts.bond;
    let t = read_swap_dvp(&ctx.accounts.swap_dvp)?;
    require!(
        t.mint_a == bond.mint
            && t.mint_b == bond.cash_mint
            && t.user_a == ctx.accounts.seller.owner
            && t.user_b == ctx.accounts.buyer.owner
            && t.user_a != t.user_b
            && t.destination_a == t.user_a
            && t.destination_b == t.user_b
            && t.amount_a > 0,
        BondError::InvalidTrade
    );
    require!(!ctx.accounts.seller.is_escrow() && !ctx.accounts.buyer.is_escrow(), BondError::EscrowRecord);
    require!(bond.now()? < bond.final_record_time()?, BondError::TransfersClosedAtMaturity);

    let escrow = &mut ctx.accounts.escrow;
    escrow.bond = bond.key();
    escrow.owner = ctx.accounts.swap_dvp.key();
    escrow.position = PositionHistory::new(bond.passed_record_moments(bond.now()?)?);
    escrow.paid = 0;
    escrow.locked = 0;
    escrow.redeemed = 0;
    escrow.bump = ctx.bumps.escrow;
    escrow.escrow_of = t.user_a;
    escrow.escrowed = 0;
    emit!(TradeAdmitted {
        bond: bond.key(),
        swap_dvp: ctx.accounts.swap_dvp.key(),
        seller: t.user_a,
        buyer: t.user_b,
        quantity: t.amount_a,
        cash: t.amount_b,
    });
    Ok(())
}
