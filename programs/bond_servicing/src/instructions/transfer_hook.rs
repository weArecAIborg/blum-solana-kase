//! Token-2022 calls this on every bond transfer, after both balances are updated (token-2022 `processor.rs`,
//! `process_transfer`). It
//!   - closes trading from the final record moment (balances are frozen for redemption);
//!   - keeps bonds in associated token accounts only, so one holder record = one position;
//!   - before moving the two holders' quantities, keeps each holder's quantity for every record moment passed
//!     since its last change (PositionHistory::save), so no record-moment position is ever lost.
//! It writes only the two holders' records: transfers between different holders run in parallel.

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
use crate::events::PositionsSaved;
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
    require!(now < bond.final_record_time()?, BondError::TransfersClosedAtMaturity);
    if same { return Ok(()); }
    let destination = destination.as_mut().unwrap();
    let passed = bond.passed_record_moments_from(now, source.position.live_from.max(destination.position.live_from))?;
    let source_before = a.source_token.amount.checked_add(amount).ok_or(BondError::Overflow)?;
    let destination_before = a.destination_token.amount.checked_sub(amount).ok_or(BondError::Overflow)?;
    update_record(&mut source, bond.key(), passed, source_before, a.source_token.amount)?;
    require!(source.locked <= a.source_token.amount, BondError::BondsLocked);
    update_record(destination, bond.key(), passed, destination_before, a.destination_token.amount)?;
    source.try_serialize(&mut &mut a.source_holder.try_borrow_mut_data()?[..])?;
    destination.try_serialize(&mut &mut a.destination_holder.try_borrow_mut_data()?[..])?;
    Ok(())
}

fn read_record(info: &AccountInfo) -> Result<HolderRecord> {
    require_keys_eq!(*info.owner, crate::ID, ErrorCode::AccountOwnedByWrongProgram);
    HolderRecord::try_deserialize(&mut &info.try_borrow_data()?[..])
}

fn update_record(record: &mut HolderRecord, bond: Pubkey, passed: u16, before: u64, after: u64) -> Result<()> {
    require!(record.position.quantity == before, BondError::PositionMismatch);
    if let Some((from_event, to_event)) = record.position.save(passed) {
        emit!(PositionsSaved { bond, owner: record.owner, from_event, to_event, quantity: before });
    }
    record.position.quantity = after;
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
