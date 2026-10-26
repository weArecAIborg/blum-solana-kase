//! Program log events: the audit record that watchers and the explorer read.

use anchor_lang::prelude::*;

#[event]
pub struct BondCreated {
    pub bond: Pubkey,
    pub mint: Pubkey,
    pub registrar: Pubkey,
    pub periods: u16,
}

#[event]
pub struct HolderRegistered {
    pub bond: Pubkey,
    pub owner: Pubkey,
}

#[event]
pub struct BondsPlaced {
    pub bond: Pubkey,
    pub owner: Pubkey,
    pub amount: u64,
}

/// A holder's quantity was kept for record moments [from_event, to_event) before it changed.
#[event]
pub struct PositionsSaved {
    pub bond: Pubkey,
    pub owner: Pubkey,
    pub from_event: u16,
    pub to_event: u16,
    pub quantity: u64,
}

#[event]
pub struct EventOpened {
    pub bond: Pubkey,
    pub event: u16,
    pub supply_at_record: u64,
    pub budget: u64,
    pub vault: Pubkey,
}

#[event]
pub struct EventFunded {
    pub bond: Pubkey,
    pub event: u16,
    pub vault_balance: u64,
    pub budget: u64,
}

#[event]
pub struct CouponPaid {
    pub bond: Pubkey,
    pub event: u16,
    pub owner: Pubkey,
    pub position: u64,
    pub amount: u64,
}

#[event]
pub struct OfferCreated {
    pub bond: Pubkey,
    pub offer: u16,
    pub pro_rata: bool,
    pub price_units: u64,
    pub max_quantity: u64,
    pub open_time: i64,
    pub close_time: i64,
}

#[event]
pub struct OfferFunded {
    pub bond: Pubkey,
    pub offer: u16,
    pub vault_balance: u64,
    pub budget: u64,
}

/// Bonds bought back and burned, price paid (FirstCome sale or ProRata settlement).
#[event]
pub struct BondsBoughtBack {
    pub bond: Pubkey,
    pub offer: u16,
    pub owner: Pubkey,
    pub burned: u64,
    pub paid: u64,
    /// Record moments passed by the program's clock when the bonds were retired: the phase the burn belongs to.
    pub passed: u16,
}

#[event]
pub struct TenderChanged {
    pub bond: Pubkey,
    pub offer: u16,
    pub owner: Pubkey,
    pub tendered: u64,
    pub offer_total: u64,
}

/// After the window: a holder's tender split into accepted (still committed) and rejected (released) bonds.
#[event]
pub struct TenderResolved {
    pub bond: Pubkey,
    pub offer: u16,
    pub owner: Pubkey,
    pub accepted: u64,
    pub rejected: u64,
}

/// A Solana DvP trade admitted: its escrow may hold `quantity` of the seller's bonds, which stay the seller's position
/// (locked) until the trade settles to the buyer or unwinds.
#[event]
pub struct TradeAdmitted {
    pub bond: Pubkey,
    pub swap_dvp: Pubkey,
    pub seller: Pubkey,
    pub buyer: Pubkey,
    pub quantity: u64,
    pub cash: u64,
}

/// Bonds moved through a trade escrow: into it (locked for the seller), to the buyer (settled) or back to the seller.
#[event]
pub struct TradeMoved {
    pub bond: Pubkey,
    pub swap_dvp: Pubkey,
    pub seller: Pubkey,
    pub to: Pubkey,
    pub quantity: u64,
    pub settled: bool,
    /// Record moments passed by the program's clock at this move: on settlement, the phase the trade belongs to.
    pub passed: u16,
}

#[event]
pub struct BondsRedeemed {
    pub bond: Pubkey,
    pub owner: Pubkey,
    pub burned: u64,
    pub principal: u64,
}
