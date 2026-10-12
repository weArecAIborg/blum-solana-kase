//! Integer-only bond servicing math, shared by the `bond_servicing` program and off-chain tools.
//!
//! Conventions are modelled on QuantLib 1.43: date arithmetic (`ql/time/date.cpp`), business-day
//! adjustment (`ql/time/calendar.cpp`), backward schedule generation (`ql/time/schedule.cpp`) and 30/360
//! (`ql/time/daycounters/thirty360.cpp`). The code is our own; QuantLib and FinancePy serve only as
//! references that the tests compare against (`tests/reference.rs`).
#![no_std]

extern crate alloc;

pub mod amount;
pub mod calendar;
pub mod date;
pub mod daycount;
pub mod schedule;
pub mod time;
