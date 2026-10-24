//! Redeem one holder at maturity. Anyone may call it once the redemption is funded and payment has opened.
//! In one transaction the program burns the holder's free bonds (permissioned burn: the Bond PDA signs as burn
//! authority and permanent delegate) and pays their principal from the redemption vault. No bond is burned unpaid,
//! no principal is paid for a live bond.
//!   - Bonds still committed to an offer (accepted, not yet bought) are left to that offer; the rest is redeemed
//!     now, so a blocked offer cannot hold back the holder's redemption.
//!   - Principal is computed on the cumulative quantity redeemed: principal(redeemed + now) - principal(redeemed),
//!     so partial redemptions round exactly like one.
//!   - The holder's history is saved first, so the final coupon stays payable whether it is paid before or after.

use anchor_lang::prelude::*;
use anchor_spl::associated_token::AssociatedToken;
use anchor_spl::token_2022::Token2022;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use crate::constants::*;
use crate::error::BondError;
use crate::events::BondsRedeemed;
use crate::settlement::{burn_bonds, pay_cash, retire_quantity, Cash};
use crate::state::{Bond, Event, EventKind, HolderRecord};

#[derive(Accounts)]
pub struct RedeemHolder<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(mut, has_one = cash_mint, has_one = mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(mut)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(has_one = bond, has_one = vault, seeds = [EVENT_SEED, bond.key().as_ref(), &event.index.to_le_bytes()], bump = event.bump)]
    pub event: Box<Account<'info, Event>>,

    #[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED, mint.key().as_ref(), owner.key().as_ref()], bump = holder.bump)]
    pub holder: Box<Account<'info, HolderRecord>>,

    /// CHECK: the holder's wallet, checked by `has_one = owner` on the holder record.
    pub owner: UncheckedAccount<'info>,

    #[account(
        mut,
        associated_token::mint = mint,
        associated_token::authority = owner,
        associated_token::token_program = token_program,
    )]
    pub holder_bonds: Box<InterfaceAccount<'info, TokenAccount>>,

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

    pub token_program: Program<'info, Token2022>,
    pub cash_token_program: Interface<'info, TokenInterface>,
    pub associated_token_program: Program<'info, AssociatedToken>,
    pub system_program: Program<'info, System>,
}

pub fn handle_redeem_holder(ctx: Context<RedeemHolder>) -> Result<()> {
    let a = &mut *ctx.accounts;
    require!(a.event.kind == EventKind::Redemption, BondError::WrongEventKind);
    require!(a.event.funded, BondError::NotFunded);
    require!(a.bond.now()? >= a.event.payment_time, BondError::PaymentNotOpen);
    require!(!a.holder.is_escrow(), BondError::EscrowRecord);
    let passed = a.bond.periods();
    require!(a.holder_bonds.amount == a.holder.expected_balance(), BondError::PositionMismatch);
    let free = a.holder.unlocked();
    require!(free > 0, BondError::NothingToPay);
    let before = a.holder.redeemed;
    let after = before.checked_add(free).ok_or(BondError::Overflow)?;
    let principal = a.bond.principal(after)? - a.bond.principal(before)?;

    let mint_key = a.bond.mint;
    let bond_key = a.bond.key();
    let bump = a.bond.bump;
    let seeds: &[&[u8]] = &[BOND_SEED, mint_key.as_ref(), &[bump]];
    let bond_info = a.bond.to_account_info();
    burn_bonds(
        &a.token_program.to_account_info(),
        &a.holder_bonds.to_account_info(),
        &a.mint.to_account_info(),
        &bond_info,
        free,
        seeds,
    )?;
    let memo_info = a.memo_program.as_ref().map(|m| m.to_account_info());
    pay_cash(
        &Cash {
            cash_token_program: &a.cash_token_program.to_account_info(),
            vault: &a.vault.to_account_info(),
            cash_mint: &a.cash_mint.to_account_info(),
            holder_cash: &a.holder_cash.to_account_info(),
            bond: &bond_info,
            memo_program: memo_info.as_ref(),
        },
        principal,
        a.cash_mint.decimals,
        "bond_servicing: redemption",
        seeds,
    )?;
    retire_quantity(&mut a.holder, &mut a.bond, bond_key, passed, free)?;
    a.holder.redeemed = after;
    emit!(BondsRedeemed { bond: bond_key, owner: a.holder.owner, burned: free, principal });
    Ok(())
}
