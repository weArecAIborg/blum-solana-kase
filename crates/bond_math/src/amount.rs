//! Entitlement amounts in token base units.
//!
//! Rounding follows SCoRE Corporate Actions Standard 4 (ECB AMI-SeCo, 2021): the issue-wide rate is
//! applied to the holder's total position, with no per-bond intermediate rounding, and the cash amount
//! is rounded down.

use crate::date::Date;
use crate::daycount::Thirty360;

pub const BPS_PER_UNIT: u128 = 10_000;

/// How the coupon for one period is computed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AccrualBasis {
    /// Rate / periods per year, the same for every regular period. Kazakh treasury formulas:
    /// MEOKAM `S = N*C*180/360` (2 per year), MEUKAM `S = N*C` (1 per year). FinancePy `CouponType.FIXED`.
    FixedPerPeriod { periods_per_year: u32 },
    /// Rate x 30/360 day count / 360. QuantLib `FixedRateCoupon` with `Thirty360`; FinancePy `CouponType.ACCRUED`.
    DayCount(Thirty360),
}

/// Coupon owed on `position` bonds for the period `[start, end)`. `None` on arithmetic overflow.
pub fn coupon_amount(
    position: u64,
    face_units: u64,
    rate_bps: u32,
    basis: AccrualBasis,
    start: Date,
    end: Date,
) -> Option<u64> {
    let gross = u128::from(position)
        .checked_mul(u128::from(face_units))?
        .checked_mul(u128::from(rate_bps))?;
    let amount = match basis {
        AccrualBasis::FixedPerPeriod { periods_per_year } => {
            if periods_per_year == 0 {
                return None;
            }
            gross / (BPS_PER_UNIT * u128::from(periods_per_year))
        }
        AccrualBasis::DayCount(convention) => {
            let days = u128::try_from(convention.day_count(start, end)).ok()?;
            gross.checked_mul(days)? / (BPS_PER_UNIT * 360)
        }
    };
    u64::try_from(amount).ok()
}

/// Principal owed on `position` bonds at `redemption_price_bps` of face (10_000 = 100%), rounded down.
pub fn principal_amount(position: u64, face_units: u64, redemption_price_bps: u32) -> Option<u64> {
    let amount = u128::from(position)
        .checked_mul(u128::from(face_units))?
        .checked_mul(u128::from(redemption_price_bps))?
        / BPS_PER_UNIT;
    u64::try_from(amount).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const USD: u64 = 1_000_000; // 6 decimals, as the test cash token

    fn d(y: i32, m: u8, day: u8) -> Date {
        Date::new(y, m, day).unwrap()
    }

    #[test]
    fn listing_example() {
        // 10 bonds x 1,000 x 10% / 2 = 500 per coupon; 10,000 principal at maturity.
        let basis = AccrualBasis::FixedPerPeriod { periods_per_year: 2 };
        let coupon = coupon_amount(10, 1_000 * USD, 1_000, basis, d(2026, 3, 6), d(2026, 9, 6));
        assert_eq!(coupon, Some(500 * USD));
        assert_eq!(principal_amount(10, 1_000 * USD, 10_000), Some(10_000 * USD));
    }

    #[test]
    fn rounds_down_on_total_holding() {
        // 1 bond, face 1.000003, 10% semi-annual: 0.05000015 -> 0.050000 (not rounded per bond first).
        let basis = AccrualBasis::FixedPerPeriod { periods_per_year: 2 };
        assert_eq!(coupon_amount(1, 1_000_003, 1_000, basis, d(2026, 3, 6), d(2026, 9, 6)), Some(50_000));
        assert_eq!(coupon_amount(7, 1_000_003, 1_000, basis, d(2026, 3, 6), d(2026, 9, 6)), Some(350_001));
    }

    #[test]
    fn overflow_is_reported() {
        let basis = AccrualBasis::FixedPerPeriod { periods_per_year: 1 };
        assert_eq!(coupon_amount(u64::MAX, u64::MAX, u32::MAX, basis, d(2026, 3, 6), d(2027, 3, 6)), None);
    }
}

/// Fixed-period coupon with no date inputs; exact integer arithmetic on the total holding.
pub fn fixed_coupon_amount(position: u64, face_units: u64, rate_bps: u32, periods_per_year: u32) -> Option<u64> {
    if periods_per_year == 0 { return None; }
    let gross = u128::from(position).checked_mul(u128::from(face_units))?.checked_mul(u128::from(rate_bps))?;
    u64::try_from(gross / (BPS_PER_UNIT * u128::from(periods_per_year))).ok()
}
