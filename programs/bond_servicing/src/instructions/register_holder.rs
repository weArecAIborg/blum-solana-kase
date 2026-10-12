//! Register a holder (simulated KYC). The transfer hook requires a holder record on both sides, so only
//! registered wallets can hold bonds. A holder registered after some record moments starts at zero for them.

use anchor_lang::prelude::*;

use crate::constants::*;
use crate::events::HolderRegistered;
use crate::state::{Bond, HolderRecord, PositionHistory};

#[derive(Accounts)]
#[instruction(owner: Pubkey)]
pub struct RegisterHolder<'info> {
    #[account(mut)]
    pub registrar: Signer<'info>,

    #[account(has_one = registrar)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(
        init,
        payer = registrar,
        space = 8 + HolderRecord::INIT_SPACE,
        seeds = [HOLDER_SEED, bond.mint.as_ref(), owner.as_ref()],
        bump,
    )]
    pub holder: Box<Account<'info, HolderRecord>>,

    pub system_program: Program<'info, System>,
}

pub fn handle_register_holder(ctx: Context<RegisterHolder>, owner: Pubkey) -> Result<()> {
    let bond = &ctx.accounts.bond;
    let passed = bond.passed_record_moments(bond.now()?)?;

    let holder = &mut ctx.accounts.holder;
    holder.bond = bond.key();
    holder.owner = owner;
    holder.position = PositionHistory::new(passed);
    holder.paid = 0;
    holder.locked = 0;
    holder.redeemed = 0;
    holder.bump = ctx.bumps.holder;
    emit!(HolderRegistered { bond: bond.key(), owner });
    Ok(())
}
