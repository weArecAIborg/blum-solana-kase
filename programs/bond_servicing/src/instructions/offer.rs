//! Third corporate action: issuer buyback (first come, first served) and tender offer (pro rata).
//! Design: docs/buyback-tender.md.
//!   - An offer is fully funded in its own vault before anyone can sell or tender.
//!   - Tendered bonds stay in the holder's account, locked by the transfer hook, so coupon rights at record
//!     moments during the window stay with the holder.
//!   - After the window, each tender has two separate outcomes. The rejected part is released by `resolve_tender`,
//!     which needs no cash and no token accounts, so a blocked cash leg cannot keep it locked. The accepted part
//!     stays committed until `settle_tender` buys it.
//!   - Tenders are numbered as they are made and resolved in that order, so an oversubscribed offer allocates by
//!     running total (`Offer::accept_next`): it buys exactly its maximum, and no tender can gain by being resolved at
//!     a chosen moment.
//!   - Bonds bought are burned and paid in one transaction, after saving the holder's and the bond's histories.

use anchor_lang::prelude::*;
use anchor_spl::associated_token::AssociatedToken;
use anchor_spl::token_2022::Token2022;
use anchor_spl::token_interface::{Mint, TokenAccount, TokenInterface};

use crate::constants::*;
use crate::error::BondError;
use crate::events::{BondsBoughtBack, OfferCreated, OfferFunded, TenderChanged, TenderResolved};
use crate::settlement::{burn_bonds, pay_cash, retire_quantity, Cash};
use crate::state::{Bond, HolderRecord, Offer, OfferKind, Tender};

// ---------------------------------------------------------------- create and fund

#[derive(Accounts)]
#[instruction(id: u16)]
pub struct CreateOffer<'info> {
    #[account(mut)]
    pub registrar: Signer<'info>,

    #[account(has_one = registrar, has_one = cash_mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(
        init,
        payer = registrar,
        space = 8 + Offer::INIT_SPACE,
        seeds = [OFFER_SEED, bond.key().as_ref(), &id.to_le_bytes()],
        bump,
    )]
    pub offer: Box<Account<'info, Offer>>,

    pub cash_mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(
        init,
        payer = registrar,
        seeds = [VAULT_SEED, offer.key().as_ref()],
        bump,
        token::mint = cash_mint,
        token::authority = bond,
        token::token_program = cash_token_program,
    )]
    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,

    pub cash_token_program: Interface<'info, TokenInterface>,
    pub system_program: Program<'info, System>,
}

pub fn handle_create_offer(
    ctx: Context<CreateOffer>,
    id: u16,
    pro_rata: bool,
    price_units: u64,
    max_quantity: u64,
    open_time: i64,
    close_time: i64,
) -> Result<()> {
    let bond = &ctx.accounts.bond;
    require!(
        price_units > 0
            && max_quantity > 0
            && open_time < close_time
            && close_time <= bond.final_record_time()?
            && price_units.checked_mul(max_quantity).is_some(),
        BondError::InvalidOffer
    );
    let offer = &mut ctx.accounts.offer;
    offer.bond = bond.key();
    offer.id = id;
    offer.kind = if pro_rata { OfferKind::ProRata } else { OfferKind::FirstCome };
    offer.price_units = price_units;
    offer.max_quantity = max_quantity;
    offer.open_time = open_time;
    offer.close_time = close_time;
    offer.total = 0;
    offer.vault = ctx.accounts.vault.key();
    offer.funded = false;
    offer.tenders = 0;
    offer.resolved = 0;
    offer.resolved_quantity = 0;
    offer.bump = ctx.bumps.offer;
    emit!(OfferCreated { bond: bond.key(), offer: id, pro_rata, price_units, max_quantity, open_time, close_time });
    Ok(())
}

#[derive(Accounts)]
pub struct ConfirmOfferFunding<'info> {
    #[account(mut, has_one = vault, seeds = [OFFER_SEED, offer.bond.as_ref(), &offer.id.to_le_bytes()], bump = offer.bump)]
    pub offer: Box<Account<'info, Offer>>,

    pub vault: Box<InterfaceAccount<'info, TokenAccount>>,
}

pub fn handle_confirm_offer_funding(ctx: Context<ConfirmOfferFunding>) -> Result<()> {
    let offer = &mut ctx.accounts.offer;
    require!(!offer.funded, BondError::AlreadyFunded);
    let budget = offer.budget().ok_or(BondError::Overflow)?;
    let vault_balance = ctx.accounts.vault.amount;
    require!(vault_balance >= budget, BondError::InsufficientFunding);
    offer.funded = true;
    emit!(OfferFunded { bond: offer.bond, offer: offer.id, vault_balance, budget });
    Ok(())
}

// ---------------------------------------------------------------- shared accounts for retiring bonds against cash

/// Accounts to burn a holder's bonds and pay from a vault. The offer is a separate account in each instruction, so
/// each instruction takes it writable only if it changes it.
#[derive(Accounts)]
pub struct Retirement<'info> {
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(mut, has_one = mint, has_one = cash_mint)]
    pub bond: Box<Account<'info, Bond>>,

    #[account(mut)]
    pub mint: Box<InterfaceAccount<'info, Mint>>,

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

/// Burn `quantity` of the holder's bonds, pay quantity x price from the offer's vault, save histories and update
/// quantities.
fn buy_back(r: &mut Retirement, offer_id: u16, price_units: u64, quantity: u64) -> Result<u64> {
    require!(!r.holder.is_escrow(), BondError::EscrowRecord);
    require!(r.holder_bonds.amount == r.holder.expected_balance(), BondError::PositionMismatch);
    let paid = quantity.checked_mul(price_units).ok_or(BondError::Overflow)?;
    let passed = r.bond.passed_record_moments(r.bond.now()?)?;
    let mint_key = r.bond.mint;
    let bond_key = r.bond.key();
    let bump = r.bond.bump;
    let seeds: &[&[u8]] = &[BOND_SEED, mint_key.as_ref(), &[bump]];
    let bond_info = r.bond.to_account_info();
    burn_bonds(
        &r.token_program.to_account_info(),
        &r.holder_bonds.to_account_info(),
        &r.mint.to_account_info(),
        &bond_info,
        quantity,
        seeds,
    )?;
    let memo_info = r.memo_program.as_ref().map(|m| m.to_account_info());
    pay_cash(
        &Cash {
            cash_token_program: &r.cash_token_program.to_account_info(),
            vault: &r.vault.to_account_info(),
            cash_mint: &r.cash_mint.to_account_info(),
            holder_cash: &r.holder_cash.to_account_info(),
            bond: &bond_info,
            memo_program: memo_info.as_ref(),
        },
        paid,
        r.cash_mint.decimals,
        &format!("bond_servicing: offer {offer_id}"),
        seeds,
    )?;
    retire_quantity(&mut r.holder, &mut r.bond, bond_key, passed, quantity)?;
    emit!(BondsBoughtBack { bond: bond_key, offer: offer_id, owner: r.holder.owner, burned: quantity, paid, passed });
    Ok(paid)
}

// ---------------------------------------------------------------- buyback: sell at once

#[derive(Accounts)]
pub struct SellToOffer<'info> {
    pub retirement: Retirement<'info>,

    #[account(constraint = seller.key() == retirement.owner.key())]
    pub seller: Signer<'info>,

    #[account(
        mut,
        constraint = offer.bond == retirement.bond.key() && offer.vault == retirement.vault.key(),
        seeds = [OFFER_SEED, retirement.bond.key().as_ref(), &offer.id.to_le_bytes()],
        bump = offer.bump,
    )]
    pub offer: Box<Account<'info, Offer>>,
}

pub fn handle_sell_to_offer(ctx: Context<SellToOffer>, quantity: u64) -> Result<()> {
    let a = &mut *ctx.accounts;
    let now = a.retirement.bond.now()?;
    require!(a.offer.kind == OfferKind::FirstCome, BondError::WrongOfferKind);
    require!(a.offer.funded, BondError::OfferNotFunded);
    require!(a.offer.open_time <= now && now < a.offer.close_time, BondError::OfferNotOpen);
    let total = a.offer.total.checked_add(quantity).ok_or(BondError::Overflow)?;
    require!(quantity > 0 && total <= a.offer.max_quantity, BondError::ExceedsOffer);
    require!(quantity <= a.retirement.holder.unlocked(), BondError::InsufficientUnlocked);
    a.offer.total = total;
    buy_back(&mut a.retirement, a.offer.id, a.offer.price_units, quantity)?;
    Ok(())
}

// ---------------------------------------------------------------- tender offer: tender, withdraw, resolve, settle

#[derive(Accounts)]
pub struct ChangeTender<'info> {
    #[account(mut)]
    pub owner: Signer<'info>,

    pub bond: Box<Account<'info, Bond>>,

    #[account(mut, has_one = bond, seeds = [OFFER_SEED, bond.key().as_ref(), &offer.id.to_le_bytes()], bump = offer.bump)]
    pub offer: Box<Account<'info, Offer>>,

    #[account(mut, has_one = bond, has_one = owner, seeds = [HOLDER_SEED, bond.mint.as_ref(), owner.key().as_ref()], bump = holder.bump)]
    pub holder: Box<Account<'info, HolderRecord>>,

    #[account(
        init_if_needed,
        payer = owner,
        space = 8 + Tender::INIT_SPACE,
        seeds = [TENDER_SEED, offer.key().as_ref(), owner.key().as_ref()],
        bump,
    )]
    pub tender: Box<Account<'info, Tender>>,

    pub system_program: Program<'info, System>,
}

/// Tender `quantity` more bonds (`withdraw` = false) or withdraw `quantity` (`withdraw` = true), while the window
/// is open. Tendered bonds are locked in the holder's account.
pub fn handle_change_tender(ctx: Context<ChangeTender>, quantity: u64, withdraw: bool) -> Result<()> {
    let a = &mut *ctx.accounts;
    let now = a.bond.now()?;
    require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);
    require!(!a.holder.is_escrow(), BondError::EscrowRecord);
    require!(a.offer.funded, BondError::OfferNotFunded);
    require!(a.offer.open_time <= now && now < a.offer.close_time, BondError::OfferNotOpen);
    require!(quantity > 0, BondError::ExceedsOffer);
    let tender = &mut a.tender;
    if tender.offer == Pubkey::default() {
        tender.offer = a.offer.key();
        tender.owner = a.owner.key();
        tender.number = a.offer.tenders;
        a.offer.tenders = a.offer.tenders.checked_add(1).ok_or(BondError::Overflow)?;
        tender.quantity = 0;
        tender.resolved = false;
        tender.accepted = 0;
        tender.settled = false;
        tender.bump = ctx.bumps.tender;
    }
    if withdraw {
        require!(quantity <= tender.quantity, BondError::ExceedsOffer);
        tender.quantity -= quantity;
        a.holder.locked -= quantity;
        a.offer.total -= quantity;
    } else {
        require!(quantity <= a.holder.unlocked(), BondError::InsufficientUnlocked);
        tender.quantity = tender.quantity.checked_add(quantity).ok_or(BondError::Overflow)?;
        a.holder.locked = a.holder.locked.checked_add(quantity).ok_or(BondError::Overflow)?;
        a.offer.total = a.offer.total.checked_add(quantity).ok_or(BondError::Overflow)?;
    }
    emit!(TenderChanged {
        bond: a.bond.key(),
        offer: a.offer.id,
        owner: a.owner.key(),
        tendered: tender.quantity,
        offer_total: a.offer.total,
    });
    Ok(())
}

/// After the window: fix the accepted part (everything if the offer is not oversubscribed, else the running-total
/// pro-rata share, `Offer::accept_next`) and release the rejected part. Tenders are resolved in the order they were
/// made. Idempotent; a resolved tender is left as it is.
fn resolve(bond_key: Pubkey, offer: &mut Offer, tender: &mut Tender, holder: &mut HolderRecord) -> Result<()> {
    if tender.resolved {
        return Ok(());
    }
    require!(tender.number == offer.resolved, BondError::TenderOutOfOrder);
    let accepted = offer.accept_next(tender.quantity).ok_or(BondError::Overflow)?;
    let rejected = tender.quantity - accepted;
    holder.locked = holder.locked.checked_sub(rejected).ok_or(BondError::Overflow)?;
    tender.accepted = accepted;
    tender.resolved = true;
    emit!(TenderResolved { bond: bond_key, offer: offer.id, owner: holder.owner, accepted, rejected });
    Ok(())
}

#[derive(Accounts)]
pub struct ResolveTender<'info> {
    pub bond: Box<Account<'info, Bond>>,

    #[account(mut, has_one = bond, seeds = [OFFER_SEED, bond.key().as_ref(), &offer.id.to_le_bytes()], bump = offer.bump)]
    pub offer: Box<Account<'info, Offer>>,

    #[account(mut, has_one = bond, seeds = [HOLDER_SEED, bond.mint.as_ref(), holder.owner.as_ref()], bump = holder.bump)]
    pub holder: Box<Account<'info, HolderRecord>>,

    #[account(mut, seeds = [TENDER_SEED, offer.key().as_ref(), holder.owner.as_ref()], bump = tender.bump)]
    pub tender: Box<Account<'info, Tender>>,
}

/// Anyone, after the window: release the rejected part of one holder's tender. Needs no cash and no token
/// accounts, so it works even when the offer's cash cannot move.
pub fn handle_resolve_tender(ctx: Context<ResolveTender>) -> Result<()> {
    let a = &mut *ctx.accounts;
    require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);
    require!(a.bond.now()? >= a.offer.close_time, BondError::OfferStillOpen);
    resolve(a.bond.key(), &mut a.offer, &mut a.tender, &mut a.holder)
}

#[derive(Accounts)]
pub struct SettleTender<'info> {
    pub retirement: Retirement<'info>,

    /// Writable: settling resolves the tender first if nobody has (the offer counts resolutions).
    #[account(
        mut,
        constraint = offer.bond == retirement.bond.key() && offer.vault == retirement.vault.key(),
        seeds = [OFFER_SEED, retirement.bond.key().as_ref(), &offer.id.to_le_bytes()],
        bump = offer.bump,
    )]
    pub offer: Box<Account<'info, Offer>>,

    #[account(
        mut,
        seeds = [TENDER_SEED, offer.key().as_ref(), retirement.owner.key().as_ref()],
        bump = tender.bump,
    )]
    pub tender: Box<Account<'info, Tender>>,
}

/// Anyone, after the window: buy the accepted part of one holder's tender (resolving it first if nobody has), burn
/// and pay in one transaction, and release it from the lock.
pub fn handle_settle_tender(ctx: Context<SettleTender>) -> Result<()> {
    let a = &mut *ctx.accounts;
    require!(a.offer.kind == OfferKind::ProRata, BondError::WrongOfferKind);
    require!(a.retirement.bond.now()? >= a.offer.close_time, BondError::OfferStillOpen);
    resolve(a.retirement.bond.key(), &mut a.offer, &mut a.tender, &mut a.retirement.holder)?;
    require!(!a.tender.settled, BondError::AlreadySettled);
    let accepted = a.tender.accepted;
    a.retirement.holder.locked = a.retirement.holder.locked.checked_sub(accepted).ok_or(BondError::Overflow)?;
    a.tender.settled = true;
    if accepted > 0 {
        buy_back(&mut a.retirement, a.offer.id, a.offer.price_units, accepted)?;
    }
    Ok(())
}
