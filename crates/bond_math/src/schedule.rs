//! Coupon schedule. Dates are generated backward from maturity, each one as maturity minus k periods
//! (not by chaining from the previous date), as QuantLib `DateGeneration::Backward` does; this keeps
//! month-end dates from drifting (Aug 31 -> Feb 28 -> Aug 31).
//!
//! Accrual dates stay unadjusted; only the payment date is moved to a business day. This matches the
//! Kazakh treasury rules, where the coupon is a fixed amount per period (Decree 473 p.34, p.43) and a
//! payment due on a non-working day moves to the next working day (p.14).

use alloc::vec::Vec;

use crate::calendar::{BusinessDayConvention, Calendar};
use crate::date::Date;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CouponPeriod {
    pub accrual_start: Date,
    pub accrual_end: Date,
    pub payment_date: Date,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScheduleError {
    MaturityNotAfterIssue,
    ZeroTenor,
    /// The periods counted back from maturity do not land on the issue date (a stub period).
    /// Stub periods are not supported yet.
    IrregularFirstPeriod { nearest_regular_date: Date },
}

pub fn regular_schedule(
    issue: Date,
    maturity: Date,
    months_per_period: u32,
    calendar: &Calendar,
    payment_convention: BusinessDayConvention,
) -> Result<Vec<CouponPeriod>, ScheduleError> {
    if maturity <= issue {
        return Err(ScheduleError::MaturityNotAfterIssue);
    }
    if months_per_period == 0 {
        return Err(ScheduleError::ZeroTenor);
    }
    let step = months_per_period as i32;
    let mut dates = Vec::new();
    let mut k: i32 = 0;
    loop {
        let d = maturity.add_months(-k * step);
        if d < issue {
            return Err(ScheduleError::IrregularFirstPeriod {
                nearest_regular_date: *dates.last().unwrap_or(&maturity),
            });
        }
        dates.push(d);
        if d == issue {
            break;
        }
        k += 1;
    }
    dates.reverse();
    Ok(dates
        .windows(2)
        .map(|w| CouponPeriod {
            accrual_start: w[0],
            accrual_end: w[1],
            payment_date: calendar.adjust(w[1], payment_convention),
        })
        .collect())
}
