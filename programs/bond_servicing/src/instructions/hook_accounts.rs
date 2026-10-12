//! The transfer hook's extra-account list, written by `create_bond` (so no bond can exist without it). Pattern from
//! solana-foundation/program-examples, tokens/token-2022/transfer-hook/account-data-as-seed (MIT).
//!
//! Extra accounts for every transfer (indices 0..3 are source, mint, destination, authority; 4 is this list):
//!   5: Bond PDA           ["bond", mint]
//!   6: source holder      ["holder", mint, source.owner]       (owner = token account bytes 32..64)
//!   7: destination holder ["holder", mint, destination.owner]

use anchor_lang::prelude::*;
use spl_tlv_account_resolution::{account::ExtraAccountMeta, seeds::Seed, state::ExtraAccountMetaList};
use spl_transfer_hook_interface::instruction::ExecuteInstruction;

use crate::constants::*;

pub const EXTRA_ACCOUNT_COUNT: usize = 3;

pub fn extra_account_metas() -> Result<Vec<ExtraAccountMeta>> {
    let holder = |token_account_index: u8| {
        ExtraAccountMeta::new_with_seeds(
            &[
                Seed::Literal { bytes: HOLDER_SEED.to_vec() },
                Seed::AccountKey { index: 1 },
                Seed::AccountData { account_index: token_account_index, data_index: 32, length: 32 },
            ],
            false,
            true,
        )
    };
    Ok(vec![
        ExtraAccountMeta::new_with_seeds(
            &[Seed::Literal { bytes: BOND_SEED.to_vec() }, Seed::AccountKey { index: 1 }],
            false,
            false,
        )
        .map_err(|_| ProgramError::InvalidArgument)?,
        holder(0).map_err(|_| ProgramError::InvalidArgument)?,
        holder(2).map_err(|_| ProgramError::InvalidArgument)?,
    ])
}

/// Write the list into its (already allocated) account.
pub fn write_extra_account_metas(info: &AccountInfo) -> Result<()> {
    let metas = extra_account_metas()?;
    // spl-tlv-account-resolution uses solana-program-error 2.x, anchor-lang 1.x uses 3.x: map the error type.
    ExtraAccountMetaList::init::<ExecuteInstruction>(&mut info.try_borrow_mut_data()?, &metas)
        .map_err(|_| ProgramError::InvalidAccountData)?;
    Ok(())
}

pub fn extra_account_metas_size() -> usize {
    ExtraAccountMetaList::size_of(EXTRA_ACCOUNT_COUNT).unwrap()
}
