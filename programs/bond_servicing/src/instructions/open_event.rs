//! Open a coupon (index < periods) or the redemption (index = periods) once its record moment has passed.
//! Anyone may call it: everything it writes follows from the terms and the bond's supply history.
//!   - supply at the record moment: from the supply history, no holder count needed;
//!   - budget: the event's amount for the whole supply, rounded down once (each holder's amount is rounded
//!     down on its own, so the holders' amounts never exceed it);
//!   - vault: a cash account of this event alone, owned by the Bond PDA, so events cannot spend each other's
//!     cash.

use anchor_lang::prelude::*;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use crate::constants::*;
use crate::error::BondError;
use crate::events::EventOpened;
use crate::state::{Bond, Event, EventKind};

#[derive(Accounts)]
#[instruction(index: u16)]
pub struct OpenEvent<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(has_one = cash_mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(
        init,
        payer = payer,
        space = 8 + Event::INIT_SPACE,
        seeds = [EVENT_SEED, bond.key().as_ref(), &index.to_le_bytes()],
        bump,
    )]
    pub event: Box<Account<'info, Event>>,

    pub cash_mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        init,
        payer = payer,
        seeds = [VAULT_SEED, event.key().as_ref()],
        bump,
        token::mint = cash_mint,
        token::authority = bond,
        token::token_program = cash_token_program,
    )]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,

    pub cash_token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

pub fn handle_open_event(ctx: Context<OpenEvent>, index: u16) -> Result<()> {
    let bond = &ctx.accounts.bond;
    let period = bond.period_of(index)?;
    let record_time = bond.record_time(index)?;
    require!(bond.now()? >= record_time, BondError::RecordMomentNotReached);

    let is_redemption = index == bond.redemption_event();
    let supply = bond.supply.at_moment(period);
    let budget = if is_redemption { bond.principal(supply)? } else { bond.coupon(supply, period)? };

    let event = &mut ctx.accounts.event;
    event.bond = bond.key();
    event.index = index;
    event.kind = if is_redemption { EventKind::Redemption } else { EventKind::Coupon };
    event.record_time = record_time;
    event.payment_time = bond.payment_time(index)?;
    event.supply_at_record = supply;
    event.budget = budget;
    event.vault = ctx.accounts.vault.key();
    event.funded = false;
    event.bump = ctx.bumps.event;
    emit!(EventOpened { bond: bond.key(), event: index, supply_at_record: supply, budget, vault: event.vault });
    Ok(())
}
