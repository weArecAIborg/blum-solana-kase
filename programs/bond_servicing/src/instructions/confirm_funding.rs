//! The issuer has put cash into the event's vault (a plain token transfer). Anyone may confirm that the vault
//! covers the event's budget; payments start only after that, so no holder is paid from an event that cannot
//! pay all of them.

use anchor_lang::prelude::*;
use anchor_spl::token_interface::TokenAccount;

use crate::constants::*;
use crate::error::BondError;
use crate::events::EventFunded;
use crate::state::Event;

#[derive(Accounts)]
pub struct ConfirmFunding<'info> {
    #[account(mut, has_one = vault, seeds = [EVENT_SEED, event.bond.as_ref(), &event.index.to_le_bytes()], bump = event.bump)]
    pub event: Box<Account<'info, Event>>,

    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
}

pub fn handle_confirm_funding(ctx: Context<ConfirmFunding>) -> Result<()> {
    let event = &mut ctx.accounts.event;
    require!(!event.funded, BondError::AlreadyFunded);
    let vault_balance = ctx.accounts.vault.amount;
    require!(vault_balance >= event.budget, BondError::InsufficientFunding);
    event.funded = true;
    emit!(EventFunded { bond: event.bond, event: event.index, vault_balance, budget: event.budget });
    Ok(())
}
