//! Timestamps: local wall-clock moments (record time 09:00 Almaty, payment deadline 17:00) and the
//! mapping between bond time (real calendar) and chain time.
//!
//! The program reads the chain clock and converts it to bond time with one parameter: chain seconds per
//! bond day. 86_400 is real time (local network with clock jumps, production). A smaller value
//! compresses the bond's life so that a devnet demo can reach maturity.

use crate::date::Date;

pub const SECONDS_PER_DAY: i64 = 86_400;

/// Unix timestamp of `seconds_after_midnight` local time on `date`, in a zone `utc_offset_seconds`
/// ahead of UTC (Almaty: +18_000 since 2024-03-01).
pub fn local_time_to_unix(date: Date, seconds_after_midnight: i64, utc_offset_seconds: i64) -> i64 {
    date.days() * SECONDS_PER_DAY + seconds_after_midnight - utc_offset_seconds
}

/// Bond time for a chain timestamp. `None` if `chain_seconds_per_bond_day` is not positive.
pub fn chain_to_bond_time(
    chain_now: i64,
    chain_origin: i64,
    bond_origin: i64,
    chain_seconds_per_bond_day: i64,
) -> Option<i64> {
    if chain_seconds_per_bond_day <= 0 {
        return None;
    }
    let elapsed = i128::from(chain_now) - i128::from(chain_origin);
    let scaled = (elapsed * i128::from(SECONDS_PER_DAY)).div_euclid(i128::from(chain_seconds_per_bond_day));
    i64::try_from(i128::from(bond_origin) + scaled).ok()
}

/// Earliest chain timestamp at which bond time reaches `bond_time`. `None` if the scale is not positive.
pub fn bond_to_chain_time(
    bond_time: i64,
    bond_origin: i64,
    chain_origin: i64,
    chain_seconds_per_bond_day: i64,
) -> Option<i64> {
    if chain_seconds_per_bond_day <= 0 {
        return None;
    }
    let elapsed = i128::from(bond_time) - i128::from(bond_origin);
    let numerator = elapsed * i128::from(chain_seconds_per_bond_day);
    let scaled = -((-numerator).div_euclid(i128::from(SECONDS_PER_DAY))); // ceiling division
    i64::try_from(i128::from(chain_origin) + scaled).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALMATY_OFFSET: i64 = 5 * 3_600;

    #[test]
    fn record_moment_almaty() {
        let maturity = Date::new(2034, 3, 6).unwrap();
        assert_eq!(local_time_to_unix(maturity, 9 * 3_600, ALMATY_OFFSET), 2_025_230_400);
    }

    #[test]
    fn real_time_is_identity() {
        assert_eq!(chain_to_bond_time(1_000_500, 1_000_000, 2_000_000, SECONDS_PER_DAY), Some(2_000_500));
        assert_eq!(bond_to_chain_time(2_000_500, 2_000_000, 1_000_000, SECONDS_PER_DAY), Some(1_000_500));
    }

    #[test]
    fn compressed_time_round_trip() {
        // One bond day per 60 chain seconds: a 2-year bond (730 days) runs in 12h10m.
        let (bond_origin, chain_origin, scale) = (1_772_755_200, 1_791_300_000, 60);
        let bond_target = bond_origin + 730 * SECONDS_PER_DAY;
        let chain_target = bond_to_chain_time(bond_target, bond_origin, chain_origin, scale).unwrap();
        assert_eq!(chain_target - chain_origin, 730 * 60);
        assert_eq!(chain_to_bond_time(chain_target, chain_origin, bond_origin, scale), Some(bond_target));
        assert!(chain_to_bond_time(chain_target - 1, chain_origin, bond_origin, scale).unwrap() < bond_target);
    }

    #[test]
    fn non_positive_scale_rejected() {
        assert_eq!(chain_to_bond_time(1, 0, 0, 0), None);
        assert_eq!(bond_to_chain_time(1, 0, 0, -5), None);
    }
}
