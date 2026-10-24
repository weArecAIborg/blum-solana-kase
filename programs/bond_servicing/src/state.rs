use anchor_lang::prelude::*;
use bond_math::amount::{coupon_amount, fixed_coupon_amount, principal_amount, AccrualBasis};
use bond_math::date::Date;
use bond_math::daycount::Thirty360;
use bond_math::time::{chain_to_bond_time, SECONDS_PER_DAY};

use crate::constants::MAX_PERIODS;
use crate::error::BondError;

/// How a coupon is computed (see `bond_math::amount::AccrualBasis`).
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub enum CouponBasis {
    /// Rate / periods per year for every regular period.
    FixedPerPeriod { periods_per_year: u32 },
    /// 30/360 accrual: 0 = BondBasis, 1 = European, 2 = USA.
    Thirty360 { convention: u8 },
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub enum EventKind {
    Coupon,
    Redemption,
}

/// A quantity over the bond's record moments. Record moments are numbered 0..periods in time order.
///
/// `quantity` is the quantity now. It has not changed since the last change, which happened when `live_from`
/// record moments had passed. So the quantity at record moment `e` is:
///   - `at[e]` if `e < live_from` (saved by the first change after moment `e`);
///   - `quantity` otherwise (no change since moment `e`).
/// `save` must run before every change; then no record moment's quantity is ever lost or overwritten, however
/// late anyone looks.
#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub struct PositionHistory {
    pub quantity: u64,
    pub live_from: u16,
    pub at: [u64; MAX_PERIODS],
}

impl PositionHistory {
    /// A new position created when `passed` record moments have passed: zero at all of them.
    pub fn new(passed: u16) -> Self {
        Self { quantity: 0, live_from: passed, at: [0; MAX_PERIODS] }
    }

    /// Keep the current quantity for every record moment passed since the last change. Returns the saved
    /// range of record moments, if any.
    pub fn save(&mut self, passed: u16) -> Option<(u16, u16)> {
        if passed <= self.live_from {
            return None;
        }
        let from = self.live_from;
        for e in from..passed {
            self.at[e as usize] = self.quantity;
        }
        self.live_from = passed;
        Some((from, passed))
    }

    /// Quantity at record moment `e`; valid once moment `e` has passed.
    pub fn at_moment(&self, e: u16) -> u64 {
        if e < self.live_from {
            self.at[e as usize]
        } else {
            self.quantity
        }
    }
}

#[account]
#[derive(InitSpace)]
pub struct Bond {
    pub registrar: Pubkey,
    pub mint: Pubkey,
    pub cash_mint: Pubkey,
    /// Face value of one bond in cash-token base units.
    pub face_units: u64,
    pub rate_bps: u32,
    pub basis: CouponBasis,
    /// Redemption price in basis points of face (10_000 = par).
    pub redemption_price_bps: u32,
    /// Day serials (days since 1970-01-01).
    pub issue_day: i32,
    pub maturity_day: i32,
    pub months_per_period: u32,
    /// Business-day-adjusted payment day of each coupon period (computed off-chain with the bond's calendar).
    #[max_len(MAX_PERIODS)]
    pub payment_days: Vec<i32>,
    /// Record day of each coupon period; the record moment is `record_seconds_local` on that day.
    #[max_len(MAX_PERIODS)]
    pub record_days: Vec<i32>,
    pub record_seconds_local: i32,
    /// Payment opens at this local time on the payment day (never before the record moment).
    pub payment_seconds_local: i32,
    pub utc_offset_seconds: i32,
    /// Time model: bond time = bond_origin + (chain time - chain_origin) * 86_400 / chain_seconds_per_bond_day.
    pub chain_origin: i64,
    pub bond_origin: i64,
    pub chain_seconds_per_bond_day: i64,
    /// Bond supply over record moments. Only the program changes supply (placement, redemption), and it saves
    /// the history first, so the supply at any record moment is known without counting holders.
    pub supply: PositionHistory,
    /// Issue size in bonds, fixed by the terms: placement never takes the supply above it.
    pub max_supply: u64,
    pub bump: u8,
}

impl Bond {
    pub fn periods(&self) -> u16 {
        self.payment_days.len() as u16
    }

    /// Index of the redemption event; coupon events are 0..periods.
    pub fn redemption_event(&self) -> u16 {
        self.periods()
    }

    /// The coupon period an event belongs to: the redemption shares the final period's dates.
    pub fn period_of(&self, event: u16) -> Result<u16> {
        require!(event <= self.redemption_event(), BondError::EventOutOfRange);
        Ok(if event == self.redemption_event() { event - 1 } else { event })
    }

    pub fn now(&self) -> Result<i64> {
        let chain_now = Clock::get()?.unix_timestamp;
        chain_to_bond_time(chain_now, self.chain_origin, self.bond_origin, self.chain_seconds_per_bond_day)
            .ok_or_else(|| error!(BondError::Overflow))
    }

    fn local_moment(&self, day: i32, seconds_local: i32) -> i64 {
        i64::from(day) * SECONDS_PER_DAY + i64::from(seconds_local) - i64::from(self.utc_offset_seconds)
    }

    /// Record moment (bond time) of an event.
    pub fn record_time(&self, event: u16) -> Result<i64> {
        let period = self.period_of(event)?;
        Ok(self.local_moment(self.record_days[period as usize], self.record_seconds_local))
    }

    /// Moment (bond time) from which an event may be paid.
    pub fn payment_time(&self, event: u16) -> Result<i64> {
        let period = self.period_of(event)?;
        Ok(self.local_moment(self.payment_days[period as usize], self.payment_seconds_local))
    }

    /// Trading closes at the final record moment: balances are frozen for redemption.
    pub fn final_record_time(&self) -> Result<i64> {
        self.record_time(self.periods() - 1)
    }

    /// Number of record moments at or before `bond_now`.
    pub fn passed_record_moments(&self, bond_now: i64) -> Result<u16> {
        self.passed_record_moments_from(bond_now, 0)
    }

    /// Holder-local cursors provide a period hint; no shared mutable epoch cache.
    pub fn passed_record_moments_from(&self, bond_now: i64, hint: u16) -> Result<u16> {
        let mut lo = usize::from(hint).min(self.record_days.len());
        // Preserve exact lookup even if a supplied hint is ahead of the queried time.
        if lo > 0 && self.local_moment(self.record_days[lo - 1], self.record_seconds_local) > bond_now { lo = 0; }
        if lo == self.record_days.len() || self.local_moment(self.record_days[lo], self.record_seconds_local) > bond_now {
            return Ok(lo as u16);
        }
        let passed = self.record_days[lo..].partition_point(|day| self.local_moment(*day, self.record_seconds_local) <= bond_now);
        Ok((lo + passed) as u16)
    }

    /// Unadjusted accrual dates of coupon period `i` (regular schedule, counted back from maturity).
    pub fn accrual_dates(&self, i: u16) -> (Date, Date) {
        let maturity = Date::from_days(i64::from(self.maturity_day));
        let back = i32::from(self.periods() - i);
        let step = self.months_per_period as i32;
        (maturity.add_months(-back * step), maturity.add_months(-(back - 1) * step))
    }

    pub fn accrual_basis(&self) -> Result<AccrualBasis> {
        Ok(match self.basis {
            CouponBasis::FixedPerPeriod { periods_per_year } => AccrualBasis::FixedPerPeriod { periods_per_year },
            CouponBasis::Thirty360 { convention } => AccrualBasis::DayCount(match convention {
                0 => Thirty360::BondBasis,
                1 => Thirty360::European,
                2 => Thirty360::Usa,
                _ => return err!(BondError::InvalidTerms),
            }),
        })
    }

    /// Coupon on a holding of `position` bonds for coupon period `period`, rounded down once.
    pub fn coupon(&self, position: u64, period: u16) -> Result<u64> {
        let amount = match self.basis {
            CouponBasis::FixedPerPeriod { periods_per_year } => fixed_coupon_amount(position, self.face_units, self.rate_bps, periods_per_year),
            _ => { let (start, end) = self.accrual_dates(period);
                coupon_amount(position, self.face_units, self.rate_bps, self.accrual_basis()?, start, end) }
        };
        amount.ok_or_else(|| error!(BondError::Overflow))
    }

    pub fn principal(&self, position: u64) -> Result<u64> {
        principal_amount(position, self.face_units, self.redemption_price_bps).ok_or_else(|| error!(BondError::Overflow))
    }

    /// Every amount the bond can owe on `supply` bonds (each coupon budget and the redemption budget) fits the cash
    /// token's u64 amounts. Checked when supply grows (placement); supply only shrinks afterwards.
    pub fn check_serviceable(&self, supply: u64) -> Result<()> {
        let basis = self.accrual_basis()?;
        match basis {
            AccrualBasis::FixedPerPeriod { periods_per_year } => {
                fixed_coupon_amount(supply, self.face_units, self.rate_bps, periods_per_year).ok_or(BondError::IssueTooLarge)?;
            }
            _ => { for period in 0..self.periods() {
                let (start, end) = self.accrual_dates(period);
                coupon_amount(supply, self.face_units, self.rate_bps, basis, start, end).ok_or(BondError::IssueTooLarge)?;
            } }
        }
        principal_amount(supply, self.face_units, self.redemption_price_bps).ok_or(BondError::IssueTooLarge)?;
        Ok(())
    }
}

/// One registered holder of one bond: its quantity over record moments and the coupons already paid.
#[account]
#[derive(InitSpace)]
pub struct HolderRecord {
    pub bond: Pubkey,
    pub owner: Pubkey,
    /// A trade escrow's record (`admit_trade`): the seller whose bonds it holds. Bonds in a DvP escrow stay in the
    /// seller's position (locked) until the trade settles, so a record moment inside an open trade still pays the
    /// seller. Zero for a holder's own record. Third field, so the transfer hook's extra-account list can read it
    /// at a one-byte offset (`HolderRecord::ESCROW_OF_OFFSET`).
    pub escrow_of: Pubkey,
    pub position: PositionHistory,
    /// Bit `e` set = coupon `e` paid.
    pub paid: u64,
    /// Bonds committed to offers: tendered into open offers, or accepted by a closed offer and not yet bought. They
    /// stay in the holder's account (and keep their coupon rights), but the transfer hook refuses transfers that
    /// would take the balance below this, and redemption leaves them to their offer.
    pub locked: u64,
    /// Bonds redeemed at maturity so far. Principal for each partial redemption is the difference of the principal
    /// on the cumulative quantity, so several partial redemptions round exactly like one.
    pub redeemed: u64,
    pub bump: u8,
    /// Bonds the holder has put into open DvP trade escrows (`admit_trade`). They are part of the holder's position
    /// (quantity = token balance + escrowed) until a trade settles; like locked bonds they cannot be transferred,
    /// tendered or redeemed meanwhile.
    pub escrowed: u64,
}

impl HolderRecord {
    /// Byte offset of `escrow_of` in the account data: discriminator 8, bond 32, owner 32.
    pub const ESCROW_OF_OFFSET: usize = 8 + 32 + 32;

    pub fn is_paid(&self, event: u16) -> bool {
        self.paid & (1u64 << event) != 0
    }

    /// Bonds free to transfer, tender or redeem: the position less what is locked in tenders and in trade escrows.
    pub fn unlocked(&self) -> u64 {
        self.position.quantity.saturating_sub(self.locked).saturating_sub(self.escrowed)
    }

    /// The token balance the position implies: the position less what sits in trade escrows.
    pub fn expected_balance(&self) -> u64 {
        self.position.quantity.saturating_sub(self.escrowed)
    }

    /// A trade escrow's record, not a holder's.
    pub fn is_escrow(&self) -> bool {
        self.escrow_of != Pubkey::default()
    }
}

#[derive(AnchorSerialize, AnchorDeserialize, Clone, Copy, Debug, PartialEq, Eq, InitSpace)]
pub enum OfferKind {
    /// Buyback: holders sell at once, first come first served, until the maximum is bought.
    FirstCome,
    /// Tender offer: holders tender during the window; after it, pro rata if oversubscribed.
    ProRata,
}

/// An issuer's offer to buy back bonds at a price per bond, up to a maximum quantity, during a window.
#[account]
#[derive(InitSpace)]
pub struct Offer {
    pub bond: Pubkey,
    pub id: u16,
    pub kind: OfferKind,
    /// All-in cash price per bond, in cash-token base units.
    pub price_units: u64,
    pub max_quantity: u64,
    /// Window in bond time: open <= t < close.
    pub open_time: i64,
    pub close_time: i64,
    /// Bonds bought (FirstCome) or currently tendered (ProRata).
    pub total: u64,
    /// The offer's own cash account.
    pub vault: Pubkey,
    pub funded: bool,
    /// Tenders made (ProRata): each new tender takes the next number.
    pub tenders: u32,
    /// Tenders resolved so far, in the order they were made.
    pub resolved: u32,
    /// Bonds tendered by the tenders resolved so far.
    pub resolved_quantity: u64,
    pub bump: u8,
}

impl Offer {
    /// Cash that covers the whole offer.
    pub fn budget(&self) -> Option<u64> {
        self.price_units.checked_mul(self.max_quantity)
    }

    /// Bonds accepted from the next tender of `quantity`, once the window has closed. Not oversubscribed: all of it.
    /// Oversubscribed: tenders are taken in the order they were made, and each receives the pro-rata share of all
    /// tenders up to and including it, rounded down, minus that of the tenders before it. The shares telescope, so the
    /// offer buys exactly its maximum; each tender receives at least its own pro-rata share rounded down, never more
    /// than it tendered, and differs from its exact share by less than one bond.
    pub fn accept_next(&mut self, quantity: u64) -> Option<u64> {
        let before = self.resolved_quantity;
        let after = before.checked_add(quantity)?;
        let accepted = if self.total <= self.max_quantity {
            quantity
        } else {
            let share = |q: u64| (u128::from(q) * u128::from(self.max_quantity) / u128::from(self.total)) as u64;
            share(after) - share(before)
        };
        self.resolved_quantity = after;
        self.resolved = self.resolved.checked_add(1)?;
        Some(accepted)
    }
}

/// One holder's tender into one pro-rata offer.
#[account]
#[derive(InitSpace)]
pub struct Tender {
    pub offer: Pubkey,
    pub owner: Pubkey,
    /// Its number in the offer: tenders are resolved in this order.
    pub number: u32,
    pub quantity: u64,
    /// After the window: the accepted part is fixed and the rejected part released (no cash involved).
    pub resolved: bool,
    pub accepted: u64,
    /// The accepted part has been bought (burned and paid).
    pub settled: bool,
    pub bump: u8,
}

/// One coupon or the redemption. Created once its record moment has passed, so its inputs are final.
/// Read-only on the payment path.
#[account]
#[derive(InitSpace)]
pub struct Event {
    pub bond: Pubkey,
    pub index: u16,
    pub kind: EventKind,
    /// Record moment and payment opening, in bond time.
    pub record_time: i64,
    pub payment_time: i64,
    /// Bond supply at the record moment (from the bond's supply history).
    pub supply_at_record: u64,
    /// Cash that covers every holder of this event: the amount for the whole supply, rounded down once. Each
    /// holder's amount is rounded down on its own, so the holders' amounts never add up to more than this.
    pub budget: u64,
    /// This event's own cash account; other events cannot spend it.
    pub vault: Pubkey,
    pub funded: bool,
    pub bump: u8,
}
