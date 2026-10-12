//! Pay one holder one coupon. Anyone may call it, at any time after payment opens, in any order: the
//! position comes from the holder's own history (its quantity at the record moment), the amount from the terms.
//! Writes only the holder's record (the paid bit), the event's vault and the holder's cash account; the bond
//! and the event are read-only, so payments do not conflict with trading.
//! The cash transfer and the paid bit are one atomic transaction. The holder's cash account is created if
//! missing; the holder's bond account is not needed, so a closed bond account cannot block the payment.

use anchor_lang::prelude::*;
use anchor_spl::associated_token::AssociatedToken;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use crate::constants::*;
use crate::error::BondError;
use crate::events::CouponPaid;
use crate::settlement::{pay_cash, Cash};
use crate::state::{Bond, Event, EventKind, HolderRecord};

#[derive(Accounts)]
pub struct PayCoupon<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(has_one = cash_mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(has_one = bond, has_one = vault, seeds = [EVENT_SEED, bond.key().as_ref(), &event.index.to_le_bytes()], bump = event.bump)]
    pub event: Box<Account<'info, Event>>,

    #[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED, bond.mint.as_ref(), owner.key().as_ref()], bump = holder.bump)]
    pub holder: Box<Account<'info, HolderRecord>>,

    /// CHECK: the holder's wallet, checked by `has_one = owner` on the holder record.
    pub owner: UncheckedAccount<'info>,

    #[account(mut)]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,

    pub cash_mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        init_if_needed,
        payer = payer,
        associated_token::mint = cash_mint,
        associated_token::authority = owner,
        associated_token::token_program = cash_token_program,
    )]
    pub holder_cash: Box<InterfaceAccount<'info, TokenAccount>>,

    /// CHECK: SPL Memo program; needed only when the holder's cash account requires memos (checked when used).
    pub memo_program: Option<UncheckedAccount<'info>>,

    pub cash_token_program: Interface<'info, TokenInterface>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

pub fn handle_pay_coupon(ctx: Context<PayCoupon>) -> Result<()> {
    let bond = &ctx.accounts.bond;
    let event = &ctx.accounts.event;
    require!(event.kind == EventKind::Coupon, BondError::WrongEventKind);
    require!(event.funded, BondError::NotFunded);
    require!(bond.now()? >= event.payment_time, BondError::PaymentNotOpen);
    let e = event.index;
    require!(!ctx.accounts.holder.is_paid(e), BondError::AlreadyPaid);
    let position = ctx.accounts.holder.position.at_moment(e);
    let amount = bond.coupon(position, e)?;
    require!(amount > 0, BondError::NothingToPay);

    let mint_key = bond.mint;
    let seeds: &[&[u8]] = &[BOND_SEED, mint_key.as_ref(), &[bond.bump]];
    let memo_info = ctx.accounts.memo_program.as_ref().map(|m| m.to_account_info());
    pay_cash(
        &Cash {
            cash_token_program: &ctx.accounts.cash_token_program.to_account_info(),
            vault: &ctx.accounts.vault.to_account_info(),
            cash_mint: &ctx.accounts.cash_mint.to_account_info(),
            holder_cash: &ctx.accounts.holder_cash.to_account_info(),
            bond: &ctx.accounts.bond.to_account_info(),
            memo_program: memo_info.as_ref(),
        },
        amount,
        ctx.accounts.cash_mint.decimals,
        &format!("bond_servicing: coupon {e}"),
        seeds,
    )?;

    let holder = &mut ctx.accounts.holder;
    holder.paid |= 1u64 << e;
    emit!(CouponPaid { bond: ctx.accounts.bond.key(), event: e, owner: holder.owner, position, amount });
    Ok(())
}
