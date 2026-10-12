//! Placement: mints bonds to a registered holder's associated token account, before the first record moment and up to
//! the bond's issue size. Updates the holder's quantity and the bond's supply, which the program alone changes.

use anchor_lang::prelude::*;
use anchor_spl::token_2022::{self, MintTo, Token2022};
use anchor_spl::token_interface::{Mint, TokenAccount};

use crate::constants::*;
use crate::error::BondError;
use crate::events::BondsPlaced;
use crate::state::{Bond, HolderRecord};

#[derive(Accounts)]
pub struct Place<'info> {
    pub registrar: Signer<'info>,

    #[account(mut, has_one = registrar, has_one = mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(mut)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(mut, has_one = bond, seeds = [HOLDER_SEED, mint.key().as_ref(), holder.owner.as_ref()], bump = holder.bump)]
    pub holder: Box<Account<'info, HolderRecord>>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = holder.owner,
        associated_token::token_program = token_program,
    )]
    pub holder_bonds: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Program<'info, Token2022>,
}

pub fn handle_place(ctx: Context<Place>, amount: u64) -> Result<()> {
    let bond = &ctx.accounts.bond;
    require!(bond.now()? < bond.record_time(0)?, BondError::PlacementClosed);
    let new_supply = bond.supply.quantity.checked_add(amount).ok_or(BondError::IssueTooLarge)?;
    require!(new_supply <= bond.max_supply, BondError::ExceedsIssueSize);
    bond.check_serviceable(new_supply)?;
    let mint_key = bond.mint;
    let seeds: &[&[u8]] = &[BOND_SEED, mint_key.as_ref(), &[bond.bump]];
    token_2022::mint_to(
        CpiContext::new_with_signer(
            ctx.accounts.token_program.key(),
            MintTo {
                mint: ctx.accounts.mint.to_account_info(),
                to: ctx.accounts.holder_bonds.to_account_info(),
                authority: ctx.accounts.bond.to_account_info(),
            },
            &[seeds],
        ),
        amount,
    )?;

    // Before the first record moment no history needs saving.
    let holder = &mut ctx.accounts.holder;
    holder.position.quantity = holder.position.quantity.checked_add(amount).ok_or(BondError::Overflow)?;
    let bond = &mut ctx.accounts.bond;
    bond.supply.quantity = bond.supply.quantity.checked_add(amount).ok_or(BondError::Overflow)?;
    emit!(BondsPlaced { bond: bond.key(), owner: holder.owner, amount });
    Ok(())
}
