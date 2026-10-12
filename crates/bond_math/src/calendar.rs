//! Business-day calendar and adjustment. Behaviour follows QuantLib `Calendar::adjust`
//! (`ql/time/calendar.cpp`). Holiday data is an input: a program has no calendar of its own.

use crate::date::{Date, Weekday};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BusinessDayConvention {
    Unadjusted,
    Following,
    ModifiedFollowing,
    Preceding,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CalendarError {
    HolidaysNotSortedOrDuplicated,
}

pub struct Calendar<'a> {
    weekend: [bool; 7],
    holidays: &'a [Date],
}

impl<'a> Calendar<'a> {
    /// `holidays` must be sorted ascending without duplicates (checked).
    pub fn new(weekend: &[Weekday], holidays: &'a [Date]) -> Result<Calendar<'a>, CalendarError> {
        if holidays.windows(2).any(|w| w[0] >= w[1]) {
            return Err(CalendarError::HolidaysNotSortedOrDuplicated);
        }
        let mut mask = [false; 7];
        for w in weekend {
            mask[w.index()] = true;
        }
        Ok(Calendar {
            weekend: mask,
            holidays,
        })
    }

    pub fn is_business_day(&self, date: Date) -> bool {
        !self.weekend[date.weekday().index()] && self.holidays.binary_search(&date).is_err()
    }

    pub fn adjust(&self, date: Date, convention: BusinessDayConvention) -> Date {
        match convention {
            BusinessDayConvention::Unadjusted => date,
            BusinessDayConvention::Following => self.roll(date, 1),
            BusinessDayConvention::Preceding => self.roll(date, -1),
            BusinessDayConvention::ModifiedFollowing => {
                let next = self.roll(date, 1);
                if next.month() != date.month() {
                    self.roll(date, -1)
                } else {
                    next
                }
            }
        }
    }

    fn roll(&self, date: Date, step: i64) -> Date {
        let mut d = date;
        while !self.is_business_day(d) {
            d = d.add_days(step);
        }
        d
    }
}
