//! Proleptic Gregorian dates with a day serial counted from 1970-01-01.
//!
//! Serial conversion uses Howard Hinnant's `days_from_civil` / `civil_from_days` algorithms (public domain).
//! Month arithmetic clamps the day to the target month's length, as QuantLib's `Date::advance` does.

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Date {
    year: i32,
    month: u8,
    day: u8,
}

/// Day of the week, Monday = 0 ... Sunday = 6.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Weekday {
    Monday,
    Tuesday,
    Wednesday,
    Thursday,
    Friday,
    Saturday,
    Sunday,
}

impl Weekday {
    pub fn index(self) -> usize {
        self as usize
    }
}

pub fn is_leap(year: i32) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

pub fn month_length(year: i32, month: u8) -> u8 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 0,
    }
}

impl Date {
    /// Returns `None` for an invalid calendar date.
    pub fn new(year: i32, month: u8, day: u8) -> Option<Date> {
        if !(1..=12).contains(&month) || day == 0 || day > month_length(year, month) {
            return None;
        }
        Some(Date { year, month, day })
    }

    pub fn year(&self) -> i32 {
        self.year
    }

    pub fn month(&self) -> u8 {
        self.month
    }

    pub fn day(&self) -> u8 {
        self.day
    }

    /// Days since 1970-01-01 (negative before it).
    pub fn days(&self) -> i64 {
        let m = i64::from(self.month);
        let d = i64::from(self.day);
        let y = i64::from(self.year) - i64::from(m <= 2);
        let era = if y >= 0 { y } else { y - 399 } / 400;
        let yoe = y - era * 400;
        let mp = if m > 2 { m - 3 } else { m + 9 };
        let doy = (153 * mp + 2) / 5 + d - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        era * 146_097 + doe - 719_468
    }

    pub fn from_days(days: i64) -> Date {
        let z = days + 719_468;
        let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let day = doy - (153 * mp + 2) / 5 + 1;
        let month = if mp < 10 { mp + 3 } else { mp - 9 };
        let year = yoe + era * 400 + i64::from(month <= 2);
        Date {
            year: year as i32,
            month: month as u8,
            day: day as u8,
        }
    }

    pub fn weekday(&self) -> Weekday {
        // 1970-01-01 was a Thursday (index 3).
        match (self.days() + 3).rem_euclid(7) {
            0 => Weekday::Monday,
            1 => Weekday::Tuesday,
            2 => Weekday::Wednesday,
            3 => Weekday::Thursday,
            4 => Weekday::Friday,
            5 => Weekday::Saturday,
            _ => Weekday::Sunday,
        }
    }

    pub fn add_days(&self, n: i64) -> Date {
        Date::from_days(self.days() + n)
    }

    /// Adds `n` calendar months; a day past the end of the target month is clamped to its last day.
    pub fn add_months(&self, n: i32) -> Date {
        let total = self.year * 12 + i32::from(self.month) - 1 + n;
        let year = total.div_euclid(12);
        let month = (total.rem_euclid(12) + 1) as u8;
        let day = self.day.min(month_length(year, month));
        Date { year, month, day }
    }

    pub fn is_last_day_of_february(&self) -> bool {
        self.month == 2 && self.day == month_length(self.year, 2)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u8, day: u8) -> Date {
        Date::new(y, m, day).unwrap()
    }

    #[test]
    fn serial_known_values() {
        assert_eq!(d(1970, 1, 1).days(), 0);
        assert_eq!(d(2000, 3, 1).days(), 11_017);
        // 2034-03-06 00:00 UTC; the smoke test's time travel target 09:00 Almaty = 2025230400.
        assert_eq!(d(2034, 3, 6).days() * 86_400, 2_025_216_000);
    }

    #[test]
    fn serial_round_trip_1900_2200() {
        let start = d(1900, 1, 1).days();
        let end = d(2200, 12, 31).days();
        let mut n = start;
        while n <= end {
            assert_eq!(Date::from_days(n).days(), n);
            n += 1;
        }
    }

    #[test]
    fn weekdays() {
        assert_eq!(d(1970, 1, 1).weekday(), Weekday::Thursday);
        assert_eq!(d(2027, 3, 6).weekday(), Weekday::Saturday);
        assert_eq!(d(2027, 3, 8).weekday(), Weekday::Monday);
        assert_eq!(d(2033, 3, 6).weekday(), Weekday::Sunday);
    }

    #[test]
    fn month_arithmetic_clamps_like_quantlib() {
        assert_eq!(d(2026, 8, 31).add_months(6), d(2027, 2, 28));
        assert_eq!(d(2028, 8, 31).add_months(-6), d(2028, 2, 29));
        assert_eq!(d(2028, 8, 31).add_months(-12), d(2027, 8, 31));
        assert_eq!(d(2025, 3, 6).add_months(108), d(2034, 3, 6));
        assert_eq!(d(2026, 1, 15).add_months(-1), d(2025, 12, 15));
    }

    #[test]
    fn invalid_dates_rejected() {
        assert!(Date::new(2027, 2, 29).is_none());
        assert!(Date::new(2028, 2, 29).is_some());
        assert!(Date::new(2026, 13, 1).is_none());
        assert!(Date::new(2026, 4, 31).is_none());
    }
}
