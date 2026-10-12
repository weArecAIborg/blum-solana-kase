//! Create the bond: the Token-2022 bond mint, the Bond account with its terms and time table, and the transfer
//! hook's extra-account list (one transaction, so trading can never start without it).
//!
//! The mint is created here, so its configuration is guaranteed rather than checked:
//!   - mint authority, permanent delegate and permissioned-burn authority = Bond PDA;
//!   - transfer hook = this program.
//! With the PermissionedBurn extension every burn needs the Bond PDA's signature, so holders cannot burn bonds
//! outside the program. Every balance change is then a transfer (seen by the hook) or a program mint or burn,
//! which is what lets positions at past record moments be kept exactly (state.rs, PositionHistory).

use anchor_lang::prelude::*;
use anchor_lang::solana_program::instruction::{AccountMeta, Instruction};
use anchor_lang::solana_program::program::invoke;
use anchor_lang::system_program::{self, CreateAccount};
use anchor_spl::token_2022::spl_token_2022::extension::{
    transfer_hook, BaseStateWithExtensions, ExtensionType, StateWithExtensions,
};
use anchor_spl::token_2022::spl_token_2022::{self, state::Mint as MintState};
use anchor_spl::token_2022::Token2022;
use anchor_spl::token_interface::Mint;
use bond_math::calendar::{BusinessDayConvention, Calendar};
use bond_math::date::Date;
use bond_math::schedule::regular_schedule;
use bond_math::time::SECONDS_PER_DAY;

use crate::constants::*;
use crate::error::BondError;
use crate::events::BondCreated;
use crate::instructions::hook_accounts::{extra_account_metas_size, write_extra_account_metas};
use crate::state::{Bond, CouponBasis, PositionHistory};

#[derive(AnchorSerialize, AnchorDeserialize, Clone)]
pub struct BondTerms {
    pub face_units: u64,
    pub rate_bps: u32,
    pub basis: CouponBasis,
    pub redemption_price_bps: u32,
    pub issue_day: i32,
    pub maturity_day: i32,
    pub months_per_period: u32,
    /// Business-day-adjusted payment day per period, from the bond's calendar (off-chain).
    pub payment_days: Vec<i32>,
    /// Record day per period (equal to the payment day in markets that fix holders on payment day).
    pub record_days: Vec<i32>,
    pub record_seconds_local: i32,
    pub payment_seconds_local: i32,
    pub utc_offset_seconds: i32,
    /// Non-business weekdays as a bit mask: bit 0 = Monday ... bit 6 = Sunday.
    pub weekend_days: u8,
    /// Bond time that corresponds to the chain time of this instruction.
    pub bond_origin: i64,
    /// 86_400 = real time; smaller compresses the bond's life (devnet demo).
    pub chain_seconds_per_bond_day: i64,
    /// Issue size in bonds: the most the registrar may ever place.
    pub max_supply: u64,
}

#[derive(Accounts)]
pub struct CreateBond<'info> {
    #[account(mut)]
    pub registrar: Signer<'info>,

    /// The new bond mint: a fresh keypair, created and initialized in this instruction.
    #[account(mut)]
    pub mint: Signer<'info>,

    #[account(
        init,
        payer = registrar,
        space = 8 + Bond::INIT_SPACE,
        seeds = [BOND_SEED, mint.key().as_ref()],
        bump,
    )]
    pub bond: Box<Account<'info, Bond>>,

    pub cash_mint: Box<InterfaceAccount<'info, Mint>>,

    /// CHECK: the transfer hook's extra-account list, created and written here at the interface's seeds.
    #[account(
        init,
        payer = registrar,
        space = extra_account_metas_size(),
        seeds = [EXTRA_ACCOUNT_METAS_SEED, mint.key().as_ref()],
        bump,
    )]
    pub extra_account_meta_list: UncheckedAccount<'info>,

    pub token_program: Program<'info, Token2022>,
    pub system_program: Program<'info, System>,
}

pub fn handle_create_bond(ctx: Context<CreateBond>, terms: BondTerms) -> Result<()> {
    validate_terms(&terms)?;
    check_cash_mint(&ctx.accounts.cash_mint.to_account_info())?;
    create_bond_mint(&ctx)?;
    write_extra_account_metas(&ctx.accounts.extra_account_meta_list.to_account_info())?;

    let bond = &mut ctx.accounts.bond;
    bond.registrar = ctx.accounts.registrar.key();
    bond.mint = ctx.accounts.mint.key();
    bond.cash_mint = ctx.accounts.cash_mint.key();
    bond.face_units = terms.face_units;
    bond.rate_bps = terms.rate_bps;
    bond.basis = terms.basis;
    bond.redemption_price_bps = terms.redemption_price_bps;
    bond.issue_day = terms.issue_day;
    bond.maturity_day = terms.maturity_day;
    bond.months_per_period = terms.months_per_period;
    bond.payment_days = terms.payment_days;
    bond.record_days = terms.record_days;
    bond.record_seconds_local = terms.record_seconds_local;
    bond.payment_seconds_local = terms.payment_seconds_local;
    bond.utc_offset_seconds = terms.utc_offset_seconds;
    bond.chain_origin = Clock::get()?.unix_timestamp;
    bond.bond_origin = terms.bond_origin;
    bond.chain_seconds_per_bond_day = terms.chain_seconds_per_bond_day;
    bond.supply = PositionHistory::new(0);
    bond.max_supply = terms.max_supply;
    bond.bump = ctx.bumps.bond;

    emit!(BondCreated {
        bond: bond.key(),
        mint: bond.mint,
        registrar: bond.registrar,
        periods: bond.periods(),
    });
    Ok(())
}

/// Create the mint account and initialize, in order: permanent delegate, transfer hook, permissioned burn
/// (last, because the bundled interface crate cannot parse extensions after it), then the mint itself.
fn create_bond_mint(ctx: &Context<CreateBond>) -> Result<()> {
    let token_program = ctx.accounts.token_program.to_account_info();
    let mint = ctx.accounts.mint.to_account_info();
    let mint_key = mint.key();
    let bond_key = ctx.accounts.bond.key();
    let space = ExtensionType::try_calculate_account_len::<MintState>(&[
        ExtensionType::PermanentDelegate,
        ExtensionType::TransferHook,
    ])
    .map_err(|_| error!(BondError::InvalidTerms))?
        + PERMISSIONED_BURN_MINT_SPACE;
    system_program::create_account(
        CpiContext::new(
            ctx.accounts.system_program.key(),
            CreateAccount { from: ctx.accounts.registrar.to_account_info(), to: mint.clone() },
        ),
        Rent::get()?.minimum_balance(space),
        space as u64,
        &spl_token_2022::ID,
    )?;

    let mut permissioned_burn = vec![PERMISSIONED_BURN_EXTENSION, PERMISSIONED_BURN_INITIALIZE];
    permissioned_burn.extend_from_slice(bond_key.as_ref());
    let instructions = [
        spl_token_2022::instruction::initialize_permanent_delegate(&spl_token_2022::ID, &mint_key, &bond_key)?,
        transfer_hook::instruction::initialize(&spl_token_2022::ID, &mint_key, Some(bond_key), Some(crate::ID))?,
        Instruction {
            program_id: spl_token_2022::ID,
            accounts: vec![AccountMeta::new(mint_key, false)],
            data: permissioned_burn,
        },
        spl_token_2022::instruction::initialize_mint2(&spl_token_2022::ID, &mint_key, &bond_key, None, 0)?,
    ];
    for ix in instructions.iter() {
        invoke(ix, &[mint.clone(), token_program.clone()])?;
    }
    Ok(())
}

/// The cash token must deliver exactly the amount sent, with no extra accounts, and only the program may move a
/// vault's cash: classic SPL Token, or Token-2022 without a transfer fee, a transfer hook, non-transferability or a
/// permanent delegate (which could move any vault's cash without the program). Unreadable extensions are refused.
/// The token's freeze authority and a pause switch stay with its issuer: they can stop payments, not take the cash.
fn check_cash_mint(info: &AccountInfo) -> Result<()> {
    if *info.owner == anchor_spl::token::ID {
        return Ok(());
    }
    require_keys_eq!(*info.owner, spl_token_2022::ID, BondError::UnsupportedCashToken);
    let data = info.try_borrow_data()?;
    let mint = StateWithExtensions::<MintState>::unpack(&data).map_err(|_| error!(BondError::UnsupportedCashToken))?;
    let types = mint.get_extension_types().map_err(|_| error!(BondError::UnsupportedCashToken))?;
    let refused = [
        ExtensionType::TransferFeeConfig,
        ExtensionType::TransferHook,
        ExtensionType::NonTransferable,
        ExtensionType::PermanentDelegate,
    ];
    require!(!types.iter().any(|t| refused.contains(t)), BondError::UnsupportedCashToken);
    Ok(())
}

fn validate_terms(t: &BondTerms) -> Result<()> {
    let day = 0..SECONDS_PER_DAY as i32;
    require!(
        t.face_units > 0
            && t.max_supply > 0
            && t.rate_bps > 0
            && t.redemption_price_bps > 0
            && t.months_per_period > 0
            && t.chain_seconds_per_bond_day > 0
            && day.contains(&t.record_seconds_local)
            && day.contains(&t.payment_seconds_local)
            && t.weekend_days & 0x7f != 0x7f,
        BondError::InvalidTerms
    );
    match t.basis {
        CouponBasis::FixedPerPeriod { periods_per_year } => require!(
            periods_per_year > 0 && periods_per_year * t.months_per_period == 12,
            BondError::InvalidTerms
        ),
        CouponBasis::Thirty360 { convention } => require!(convention <= 2, BondError::InvalidTerms),
    }

    // Recompute the unadjusted schedule from the terms and check the registrar's days against it.
    let issue = Date::from_days(i64::from(t.issue_day));
    let maturity = Date::from_days(i64::from(t.maturity_day));
    let no_holidays = Calendar::new(&[], &[]).map_err(|_| error!(BondError::InvalidTerms))?;
    let periods = regular_schedule(issue, maturity, t.months_per_period, &no_holidays, BusinessDayConvention::Unadjusted)
        .map_err(|_| error!(BondError::InvalidSchedule))?;
    require!(
        periods.len() == t.payment_days.len() && periods.len() == t.record_days.len() && periods.len() <= MAX_PERIODS,
        BondError::InvalidSchedule
    );
    let business_day = |day: i32| t.weekend_days & (1 << Date::from_days(i64::from(day)).weekday() as u8) == 0;
    let mut previous_record = i64::MIN;
    for ((p, &pay), &rec) in periods.iter().zip(&t.payment_days).zip(&t.record_days) {
        let shift = i64::from(pay) - p.accrual_end.days();
        let record_moment = i64::from(rec) * SECONDS_PER_DAY + i64::from(t.record_seconds_local);
        let payment_moment = i64::from(pay) * SECONDS_PER_DAY + i64::from(t.payment_seconds_local);
        require!(
            (-MAX_PAYMENT_DELAY_DAYS..=MAX_PAYMENT_DELAY_DAYS).contains(&shift)
                && business_day(pay)
                && business_day(rec)
                && record_moment > previous_record
                && record_moment <= payment_moment,
            BondError::InvalidSchedule
        );
        previous_record = record_moment;
    }
    Ok(())
}
