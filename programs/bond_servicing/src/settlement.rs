//! Moving value out of a vault, shared by coupon payment, redemption, buyback and tender settlement.
//!   - `pay_cash`: transfer from a vault (signed by the Bond PDA). If the recipient is a Token-2022 account that
//!     requires incoming memos, a Memo instruction is invoked right before the transfer, as Token-2022 requires.
//!   - `burn_and_pay`: retire bonds against cash in one transaction (Token-2022 permissioned burn, signed by the
//!     Bond PDA as burn authority and permanent delegate, then `pay_cash`).
//!   - `retire_quantity`: save the holder's and the bond's histories, then take the burned quantity off both, so every
//!     right earned at past record moments stays payable.

use anchor_lang::prelude::*;
use anchor_lang::solana_program::instruction::{AccountMeta, Instruction};
use anchor_lang::solana_program::program::{invoke, invoke_signed};
use anchor_spl::token_2022::spl_token_2022::{
    self,
    extension::{memo_transfer::memo_required, StateWithExtensions},
    state::Account as TokenAccountState,
};
use anchor_spl::token_interface::{self, TransferChecked};

use crate::constants::*;
use crate::error::BondError;
use crate::events::PositionsSaved;
use crate::state::{Bond, HolderRecord};

/// SPL Memo program (v3).
pub const MEMO_PROGRAM_ID: Pubkey = pubkey!("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");

pub struct Cash<'a, 'info> {
    pub cash_token_program: &'a AccountInfo<'info>,
    pub vault: &'a AccountInfo<'info>,
    pub cash_mint: &'a AccountInfo<'info>,
    pub holder_cash: &'a AccountInfo<'info>,
    pub bond: &'a AccountInfo<'info>,
    pub memo_program: Option<&'a AccountInfo<'info>>,
}

/// Does the recipient require a memo with every incoming transfer (Token-2022 MemoTransfer extension)?
fn requires_memo(account: &AccountInfo) -> Result<bool> {
    if *account.owner != spl_token_2022::ID {
        return Ok(false);
    }
    let data = account.try_borrow_data()?;
    let state = StateWithExtensions::<TokenAccountState>::unpack(&data).map_err(|_| error!(BondError::UnsupportedCashToken))?;
    Ok(memo_required(&state))
}

/// Pay `amount` from the vault to the holder's cash account, signed by the Bond PDA.
pub fn pay_cash(c: &Cash, amount: u64, decimals: u8, memo: &str, seeds: &[&[u8]]) -> Result<()> {
    if requires_memo(c.holder_cash)? {
        let memo_program = c.memo_program.ok_or(BondError::MemoProgramMissing)?;
        require_keys_eq!(memo_program.key(), MEMO_PROGRAM_ID, BondError::MemoProgramMissing);
        let ix = Instruction { program_id: MEMO_PROGRAM_ID, accounts: vec![], data: memo.as_bytes().to_vec() };
        invoke(&ix, &[memo_program.clone()])?;
    }
    token_interface::transfer_checked(
        CpiContext::new_with_signer(
            c.cash_token_program.key(),
            TransferChecked {
                from: c.vault.clone(),
                mint: c.cash_mint.clone(),
                to: c.holder_cash.clone(),
                authority: c.bond.clone(),
            },
            &[seeds],
        ),
        amount,
        decimals,
    )
}

/// Burn `quantity` bonds (0 decimals) from the holder's bond account, signed by the Bond PDA.
pub fn burn_bonds<'info>(
    token_program: &AccountInfo<'info>,
    holder_bonds: &AccountInfo<'info>,
    mint: &AccountInfo<'info>,
    bond: &AccountInfo<'info>,
    quantity: u64,
    seeds: &[&[u8]],
) -> Result<()> {
    let mut data = vec![PERMISSIONED_BURN_EXTENSION, PERMISSIONED_BURN_BURN_CHECKED];
    data.extend_from_slice(&quantity.to_le_bytes());
    data.push(0);
    let burn = Instruction {
        program_id: token_program.key(),
        accounts: vec![
            AccountMeta::new(holder_bonds.key(), false),
            AccountMeta::new(mint.key(), false),
            AccountMeta::new_readonly(bond.key(), true),
            AccountMeta::new_readonly(bond.key(), true),
        ],
        data,
    };
    invoke_signed(&burn, &[holder_bonds.clone(), mint.clone(), bond.clone(), bond.clone(), token_program.clone()], &[seeds])?;
    Ok(())
}

/// Save the holder's and the bond's histories, then take `quantity` off both.
pub fn retire_quantity(holder: &mut HolderRecord, bond: &mut Bond, bond_key: Pubkey, passed: u16, quantity: u64) -> Result<()> {
    let before = holder.position.quantity;
    if let Some((from_event, to_event)) = holder.position.save(passed) {
        emit!(PositionsSaved { bond: bond_key, owner: holder.owner, from_event, to_event, quantity: before });
    }
    holder.position.quantity = before.checked_sub(quantity).ok_or(BondError::Overflow)?;
    bond.supply.save(passed);
    bond.supply.quantity = bond.supply.quantity.checked_sub(quantity).ok_or(BondError::Overflow)?;
    Ok(())
}
