//! Token-2022 calls this on every bond transfer, after both balances are updated (token-2022 `processor.rs`,
//! `process_transfer`). It
//!   - closes trading from the final record moment (balances are frozen for redemption); only an open trade's bonds
//!     may still go back from its escrow to the seller, which moves no position;
//!   - keeps bonds in associated token accounts only, so one holder record = one position;
//!   - before moving the two holders' quantities, keeps each holder's quantity for every record moment passed
//!     since its last change (PositionHistory::save), so no record-moment position is ever lost;
//!   - routes bonds through a trade escrow (Solana DvP, `admit_trade`): into the escrow they stay the seller's
//!     position (`escrowed`); out of it they settle to the buyer or return to the seller.
//! A holder's position is its token balance plus what its open trade escrows hold. A transfer between holders
//! writes only the two holders' records: transfers between different holders run in parallel. A settlement out of
//! an escrow also writes the seller's record.

use std::cell::RefMut;

use anchor_lang::prelude::*;
use anchor_spl::associated_token::get_associated_token_address_with_program_id;
use anchor_spl::token_2022::spl_token_2022::{
    extension::{transfer_hook::TransferHookAccount, BaseStateWithExtensionsMut, PodStateWithExtensionsMut},
    pod::PodAccount,
};
use anchor_spl::token_interface::{Mint, TokenAccount};

use crate::constants::*;
use crate::error::BondError;
use crate::events::{PositionsSaved, TradeMoved};
use crate::state::{Bond, HolderRecord};

#[derive(Accounts)]
pub struct TransferHook<'info> {
    #[account(token::mint = mint)]
    pub source_token: InterfaceAccount<'info, TokenAccount>,

    pub mint: Box<InterfaceAccount<'info, Mint>>,

    #[account(token::mint = mint)]
    pub destination_token: InterfaceAccount<'info, TokenAccount>,

    /// CHECK: owner or delegate that authorised the transfer; Token-2022 has already checked it.
    pub authority: UncheckedAccount<'info>,

    /// CHECK: ExtraAccountMetaList at the interface seeds.
    #[account(seeds = [EXTRA_ACCOUNT_METAS_SEED, mint.key().as_ref()], bump)]
    pub extra_account_meta_list: UncheckedAccount<'info>,

    #[account(seeds = [BOND_SEED, mint.key().as_ref()], bump = bond.bump)]
    pub bond: Box<Account<'info, Bond>>,

    /// CHECK: holder record of the source owner. Read and written by hand because source and destination
    /// may belong to the same owner.
    #[account(mut, seeds = [HOLDER_SEED, mint.key().as_ref(), source_token.owner.as_ref()], bump)]
    pub source_holder: UncheckedAccount<'info>,

    /// CHECK: holder record of the destination owner (see above).
    #[account(mut, seeds = [HOLDER_SEED, mint.key().as_ref(), destination_token.owner.as_ref()], bump)]
    pub destination_holder: UncheckedAccount<'info>,

    /// CHECK: the seller's record when the source is a trade escrow (seeds from the escrow record's `escrow_of`);
    /// otherwise an address no record exists at, not read. Checked by hand against the escrow record.
    #[account(mut)]
    pub seller_holder: UncheckedAccount<'info>,
}

pub fn handle_transfer_hook(ctx: Context<TransferHook>, amount: u64) -> Result<()> {
    let a = &ctx.accounts;
    check_is_transferring(&a.source_token)?;
    check_associated(&a.source_token, &a.mint)?;
    check_associated(&a.destination_token, &a.mint)?;
    let mut source = read_record(&a.source_holder.to_account_info())?;
    let same = a.source_token.key() == a.destination_token.key();
    let mut destination = if same { None } else { Some(read_record(&a.destination_holder.to_account_info())?) };
    let bond = &a.bond;
    let now = bond.now()?;
    // A trade still open at the final record moment can no longer settle, but its bonds must get back to the seller
    // (cancel, reject, reclaim) to be redeemed: the return keeps every position, so it stays open.
    let returning = source.is_escrow() && destination.as_ref().is_some_and(|d| d.owner == source.escrow_of);
    require!(returning || now < bond.final_record_time()?, BondError::TransfersClosedAtMaturity);
    if same { return Ok(()); }
    let destination = destination.as_mut().unwrap();
    // Balances before this transfer (Token-2022 has already moved `amount`).
    let source_before = a.source_token.amount.checked_add(amount).ok_or(BondError::Overflow)?;
    let destination_before = a.destination_token.amount.checked_sub(amount).ok_or(BondError::Overflow)?;

    if source.is_escrow() {
        // Out of a trade escrow: to the buyer (settle) or back to the seller (cancel, reclaim, surplus refund).
        // The escrow's quantity tracks its own balance; the bonds' position is the seller's.
        require!(!destination.is_escrow(), BondError::EscrowDestination);
        require!(source.position.quantity == source_before, BondError::PositionMismatch);
        source.position.quantity = a.source_token.amount;
        let settled = destination.owner != source.escrow_of;
        let mut passed = None;
        if settled {
            let seller_info = a.seller_holder.to_account_info();
            let expected = Pubkey::find_program_address(&[HOLDER_SEED, a.mint.key().as_ref(), source.escrow_of.as_ref()], &crate::ID).0;
            require_keys_eq!(seller_info.key(), expected, BondError::EscrowDestination);
            let mut seller = read_record(&seller_info)?;
            require_keys_eq!(seller.owner, source.escrow_of, BondError::EscrowDestination);
            let moved = bond.passed_record_moments_from(now, seller.position.live_from.max(destination.position.live_from))?;
            passed = Some(moved);
            // The seller's position loses the bonds; what it holds in escrows shrinks by the same amount.
            seller.escrowed = seller.escrowed.checked_sub(amount).ok_or(BondError::Overflow)?;
            let seller_before = seller.position.quantity;
            let seller_after = seller_before.checked_sub(amount).ok_or(BondError::Overflow)?;
            save_and_set(&mut seller, bond.key(), moved, seller_after)?;
            check_balance(&seller, seller.expected_balance())?; // unchanged balance, consistent position
            let destination_after = destination.position.quantity.checked_add(amount).ok_or(BondError::Overflow)?;
            save_and_set(destination, bond.key(), moved, destination_after)?;
            check_balance(destination, a.destination_token.amount)?;
            seller.try_serialize(&mut &mut seller_info.try_borrow_mut_data()?[..])?;
        } else {
            // Back to the seller: the position never left; only its escrowed part shrinks.
            destination.escrowed = destination.escrowed.checked_sub(amount).ok_or(BondError::Overflow)?;
            check_balance(destination, a.destination_token.amount)?;
        }
        let passed = match passed { Some(p) => p, None => bond.passed_record_moments(now)? };
        emit!(TradeMoved { bond: bond.key(), swap_dvp: source.owner, seller: source.escrow_of, to: destination.owner, quantity: amount, settled, passed });
        source.try_serialize(&mut &mut a.source_holder.try_borrow_mut_data()?[..])?;
        destination.try_serialize(&mut &mut a.destination_holder.try_borrow_mut_data()?[..])?;
        return Ok(());
    }

    if destination.is_escrow() {
        // Into a trade escrow: only the trade's seller may fund it, with bonds free of tenders and other escrows.
        // The bonds stay in the seller's position, so a record moment inside the open trade still pays the seller.
        require_keys_eq!(destination.escrow_of, source.owner, BondError::EscrowDestination);
        require!(source.expected_balance() == source_before, BondError::PositionMismatch);
        require!(amount <= source.unlocked(), BondError::BondsLocked);
        let passed = bond.passed_record_moments_from(now, source.position.live_from)?;
        if let Some((from_event, to_event)) = source.position.save(passed) {
            emit!(PositionsSaved { bond: bond.key(), owner: source.owner, from_event, to_event, quantity: source.position.quantity });
        }
        source.escrowed = source.escrowed.checked_add(amount).ok_or(BondError::Overflow)?;
        require!(destination.position.quantity == destination_before, BondError::PositionMismatch);
        destination.position.quantity = a.destination_token.amount;
        emit!(TradeMoved { bond: bond.key(), swap_dvp: destination.owner, seller: source.owner, to: destination.owner, quantity: amount, settled: false, passed });
        source.try_serialize(&mut &mut a.source_holder.try_borrow_mut_data()?[..])?;
        destination.try_serialize(&mut &mut a.destination_holder.try_borrow_mut_data()?[..])?;
        return Ok(());
    }

    // Between two holders: each position follows its balance; bonds in tenders or escrows cannot move.
    require!(source.expected_balance() == source_before, BondError::PositionMismatch);
    require!(amount <= source.unlocked(), BondError::BondsLocked);
    require!(destination.expected_balance() == destination_before, BondError::PositionMismatch);
    let passed = bond.passed_record_moments_from(now, source.position.live_from.max(destination.position.live_from))?;
    let source_after = source.position.quantity.checked_sub(amount).ok_or(BondError::Overflow)?;
    let destination_after = destination.position.quantity.checked_add(amount).ok_or(BondError::Overflow)?;
    save_and_set(&mut source, bond.key(), passed, source_after)?;
    save_and_set(destination, bond.key(), passed, destination_after)?;
    source.try_serialize(&mut &mut a.source_holder.try_borrow_mut_data()?[..])?;
    destination.try_serialize(&mut &mut a.destination_holder.try_borrow_mut_data()?[..])?;
    Ok(())
}

fn read_record(info: &AccountInfo) -> Result<HolderRecord> {
    require_keys_eq!(*info.owner, crate::ID, ErrorCode::AccountOwnedByWrongProgram);
    HolderRecord::try_deserialize(&mut &info.try_borrow_data()?[..])
}

/// Keep the record's quantity for the record moments passed since its last change, then set the new quantity.
fn save_and_set(record: &mut HolderRecord, bond: Pubkey, passed: u16, after: u64) -> Result<()> {
    let before = record.position.quantity;
    if let Some((from_event, to_event)) = record.position.save(passed) {
        emit!(PositionsSaved { bond, owner: record.owner, from_event, to_event, quantity: before });
    }
    record.position.quantity = after;
    Ok(())
}

/// The record's position must match the account's balance (plus what the holder's escrows hold).
fn check_balance(record: &HolderRecord, balance: u64) -> Result<()> {
    require!(record.expected_balance() == balance, BondError::PositionMismatch);
    Ok(())
}

fn check_associated(token: &InterfaceAccount<TokenAccount>, mint: &InterfaceAccount<Mint>) -> Result<()> {
    let expected =
        get_associated_token_address_with_program_id(&token.owner, &mint.key(), &anchor_spl::token_2022::ID);
    require_keys_eq!(token.key(), expected, BondError::NotAssociatedTokenAccount);
    Ok(())
}

/// From the official example: fail unless Token-2022 marked the account as being transferred.
fn check_is_transferring(source: &InterfaceAccount<TokenAccount>) -> Result<()> {
    let info = source.to_account_info();
    let mut data: RefMut<&mut [u8]> = info.try_borrow_mut_data()?;
    let mut account =
        PodStateWithExtensionsMut::<PodAccount>::unpack(*data).map_err(|_| ProgramError::InvalidAccountData)?;
    let extension =
        account.get_extension_mut::<TransferHookAccount>().map_err(|_| ProgramError::InvalidAccountData)?;
    require!(bool::from(extension.transferring), BondError::NotTransferring);
    Ok(())
}
