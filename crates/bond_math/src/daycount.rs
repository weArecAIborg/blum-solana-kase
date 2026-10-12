//! 30/360 day counts. Formulas follow QuantLib `Thirty360` (`ql/time/daycounters/thirty360.cpp`).

use crate::date::Date;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Thirty360 {
    /// QuantLib `BondBasis` / `ISMA`.
    BondBasis,
    /// QuantLib `European` / `EurobondBasis` (30E/360).
    European,
    /// QuantLib `USA`.
    Usa,
}

impl Thirty360 {
    pub fn day_count(self, d1: Date, d2: Date) -> i64 {
        let (y1, m1, mut dd1) = (i64::from(d1.year()), i64::from(d1.month()), i64::from(d1.day()));
        let (y2, m2, mut dd2) = (i64::from(d2.year()), i64::from(d2.month()), i64::from(d2.day()));
        match self {
            Thirty360::BondBasis => {
                if dd1 == 31 {
                    dd1 = 30;
                }
                if dd2 == 31 && dd1 == 30 {
                    dd2 = 30;
                }
            }
            Thirty360::European => {
                if dd1 == 31 {
                    dd1 = 30;
                }
                if dd2 == 31 {
                    dd2 = 30;
                }
            }
            Thirty360::Usa => {
                if d1.is_last_day_of_february() {
                    if d2.is_last_day_of_february() {
                        dd2 = 30;
                    }
                    dd1 = 30;
                }
                if dd2 == 31 && dd1 >= 30 {
                    dd2 = 30;
                }
                if dd1 == 31 {
                    dd1 = 30;
                }
            }
        }
        360 * (y2 - y1) + 30 * (m2 - m1) + (dd2 - dd1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u8, day: u8) -> Date {
        Date::new(y, m, day).unwrap()
    }

    #[test]
    fn regular_semiannual_period_is_180() {
        for c in [Thirty360::BondBasis, Thirty360::European, Thirty360::Usa] {
            assert_eq!(c.day_count(d(2026, 3, 6), d(2026, 9, 6)), 180);
            assert_eq!(c.day_count(d(2025, 3, 6), d(2026, 3, 6)), 360);
        }
    }

    #[test]
    fn conventions_differ_at_month_end() {
        // February end to August 31: the variants disagree; QuantLib gives the same numbers (fixture test).
        let (a, b) = (d(2027, 2, 28), d(2027, 8, 31));
        assert_eq!(Thirty360::BondBasis.day_count(a, b), 183);
        assert_eq!(Thirty360::European.day_count(a, b), 182);
        assert_eq!(Thirty360::Usa.day_count(a, b), 180);
    }
}
