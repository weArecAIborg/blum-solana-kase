//! bond_servicing: registrar and paying agent for a tokenized bond on Solana.
//! Design and reasoning: docs/bond-processing.md, docs/technical-overview.md.

pub mod constants;
pub mod error;
pub mod events;
pub mod instructions;
pub mod settlement;
pub mod state;

use anchor_lang::prelude::*;
use spl_discriminator::SplDiscriminate;
use spl_transfer_hook_interface::instruction::ExecuteInstruction;

pub use constants::*;
pub use instructions::*;
pub use state::*;

declare_id!("2UkTKYYzgZXZsganCqfxyY5Q5EeynUSBbErdKMpy8sHf");

#[program]
pub mod bond_servicing {
    use super::*;

    pub fn create_bond(ctx: Context<CreateBond>, terms: BondTerms) -> Result<()> {
        instructions::create_bond::handle_create_bond(ctx, terms)
    }

    pub fn register_holder(ctx: Context<RegisterHolder>, owner: Pubkey) -> Result<()> {
        instructions::register_holder::handle_register_holder(ctx, owner)
    }

    pub fn place(ctx: Context<Place>, amount: u64) -> Result<()> {
        instructions::place::handle_place(ctx, amount)
    }

    #[instruction(discriminator = ExecuteInstruction::SPL_DISCRIMINATOR_SLICE)]
    pub fn transfer_hook(ctx: Context<TransferHook>, amount: u64) -> Result<()> {
        instructions::transfer_hook::handle_transfer_hook(ctx, amount)
    }

    pub fn open_event(ctx: Context<OpenEvent>, index: u16) -> Result<()> {
        instructions::open_event::handle_open_event(ctx, index)
    }

    pub fn confirm_funding(ctx: Context<ConfirmFunding>) -> Result<()> {
        instructions::confirm_funding::handle_confirm_funding(ctx)
    }

    pub fn pay_coupon(ctx: Context<PayCoupon>) -> Result<()> {
        instructions::pay_coupon::handle_pay_coupon(ctx)
    }

    pub fn redeem_holder(ctx: Context<RedeemHolder>) -> Result<()> {
        instructions::redeem_holder::handle_redeem_holder(ctx)
    }

    pub fn create_offer(
        ctx: Context<CreateOffer>,
        id: u16,
        pro_rata: bool,
        price_units: u64,
        max_quantity: u64,
        open_time: i64,
        close_time: i64,
    ) -> Result<()> {
        instructions::offer::handle_create_offer(ctx, id, pro_rata, price_units, max_quantity, open_time, close_time)
    }

    pub fn confirm_offer_funding(ctx: Context<ConfirmOfferFunding>) -> Result<()> {
        instructions::offer::handle_confirm_offer_funding(ctx)
    }

    pub fn sell_to_offer(ctx: Context<SellToOffer>, quantity: u64) -> Result<()> {
        instructions::offer::handle_sell_to_offer(ctx, quantity)
    }

    pub fn change_tender(ctx: Context<ChangeTender>, quantity: u64, withdraw: bool) -> Result<()> {
        instructions::offer::handle_change_tender(ctx, quantity, withdraw)
    }

    pub fn resolve_tender(ctx: Context<ResolveTender>) -> Result<()> {
        instructions::offer::handle_resolve_tender(ctx)
    }

    pub fn settle_tender(ctx: Context<SettleTender>) -> Result<()> {
        instructions::offer::handle_settle_tender(ctx)
    }
}
