//! Compares bond_math with QuantLib 1.43 and FinancePy 1.1.2 on the same bonds.
//! Fixture: `tests/fixtures/reference.json`, produced by `tools/reference/generate_fixtures.py`.

use bond_math::amount::{coupon_amount, AccrualBasis};
use bond_math::calendar::{BusinessDayConvention, Calendar};
use bond_math::date::{Date, Weekday};
use bond_math::daycount::Thirty360;
use bond_math::schedule::{regular_schedule, ScheduleError};
use serde::Deserialize;

const UNITS: u64 = 1_000_000; // cash token has 6 decimals

#[derive(Deserialize)]
struct Fixture {
    kz_holidays: Vec<String>,
    kase_mum108_0015_payment_dates: Vec<String>,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    issue: String,
    maturity: String,
    months_per_period: u32,
    face: u64,
    rate_bps: u32,
    periods_per_year: u32,
    ql_regular: bool,
    periods: Vec<Period>,
    fp_fixed_flow_fraction_of_face: Vec<f64>,
    fp_coupon_dates: Vec<String>,
}

#[derive(Deserialize)]
struct Period {
    accrual_start: String,
    accrual_end: String,
    payment_date: String,
    ql_days_bond_basis: i64,
    ql_days_european: i64,
    ql_days_usa: i64,
    ql_amount_per_bond_bond_basis: f64,
}

fn date(s: &str) -> Date {
    let p: Vec<&str> = s.split('-').collect();
    Date::new(p[0].parse().unwrap(), p[1].parse().unwrap(), p[2].parse().unwrap()).unwrap()
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!("fixtures/reference.json")).unwrap()
}

fn kz_holidays(f: &Fixture) -> Vec<Date> {
    f.kz_holidays.iter().map(|s| date(s)).collect()
}

#[test]
fn day_counts_match_quantlib() {
    let f = fixture();
    for c in &f.cases {
        for p in &c.periods {
            let (s, e) = (date(&p.accrual_start), date(&p.accrual_end));
            assert_eq!(Thirty360::BondBasis.day_count(s, e), p.ql_days_bond_basis, "{} {s:?}", c.name);
            assert_eq!(Thirty360::European.day_count(s, e), p.ql_days_european, "{} {s:?}", c.name);
            assert_eq!(Thirty360::Usa.day_count(s, e), p.ql_days_usa, "{} {s:?}", c.name);
        }
    }
}

#[test]
fn schedules_match_quantlib_and_financepy() {
    let f = fixture();
    let holidays = kz_holidays(&f);
    let cal = Calendar::new(&[Weekday::Saturday, Weekday::Sunday], &holidays).unwrap();
    for c in &f.cases {
        let result = regular_schedule(
            date(&c.issue),
            date(&c.maturity),
            c.months_per_period,
            &cal,
            BusinessDayConvention::Following,
        );
        if !c.ql_regular {
            assert!(
                matches!(result, Err(ScheduleError::IrregularFirstPeriod { .. })),
                "{}: QuantLib builds a stub, we must reject it",
                c.name
            );
            continue;
        }
        let ours = result.unwrap();
        assert_eq!(ours.len(), c.periods.len(), "{}", c.name);
        for (o, p) in ours.iter().zip(&c.periods) {
            assert_eq!(o.accrual_start, date(&p.accrual_start), "{}", c.name);
            assert_eq!(o.accrual_end, date(&p.accrual_end), "{}", c.name);
            assert_eq!(o.payment_date, date(&p.payment_date), "{}", c.name);
        }
        // FinancePy switches on the end-of-month roll by itself when maturity is a month end;
        // that case is covered by `financepy_rolls_month_end_maturities_quantlib_does_not`.
        if !is_month_end(date(&c.maturity)) {
            let fp_dates: Vec<Date> = c.fp_coupon_dates.iter().map(|s| date(s)).collect();
            let our_dates: Vec<Date> = ours.iter().map(|o| o.accrual_end).collect();
            assert_eq!(our_dates, fp_dates, "{}: unadjusted coupon dates vs FinancePy", c.name);
        }
    }
}

fn is_month_end(d: Date) -> bool {
    d.add_days(1).month() != d.month()
}

/// Known convention difference, kept visible: for a bond maturing on a month end, FinancePy rolls every
/// coupon date to a month end; QuantLib with `endOfMonth = false` and bond_math do not. Kazakh rules are
/// silent on this; the end-of-month rule is an open question.
#[test]
fn financepy_rolls_month_end_maturities_quantlib_does_not() {
    let f = fixture();
    let c = f.cases.iter().find(|c| c.maturity == "2029-02-28").unwrap();
    let ql: Vec<Date> = c.periods.iter().map(|p| date(&p.accrual_end)).collect();
    let fp: Vec<Date> = c.fp_coupon_dates.iter().map(|s| date(s)).collect();
    assert_eq!(ql[0], date("2027-08-28"));
    assert_eq!(fp[0], date("2027-08-31"));
    assert!(fp.iter().all(|d| is_month_end(*d)));
}

#[test]
fn mum108_0015_payment_dates_match_kase() {
    let f = fixture();
    let holidays = kz_holidays(&f);
    let cal = Calendar::new(&[Weekday::Saturday, Weekday::Sunday], &holidays).unwrap();
    let schedule = regular_schedule(
        date("2025-03-06"),
        date("2034-03-06"),
        12,
        &cal,
        BusinessDayConvention::Following,
    )
    .unwrap();
    let ours: Vec<Date> = schedule.iter().map(|p| p.payment_date).collect();
    for kase in &f.kase_mum108_0015_payment_dates {
        assert!(ours.contains(&date(kase)), "KASE payment date {kase} missing from {ours:?}");
    }
}

#[test]
fn day_count_amounts_match_quantlib() {
    let f = fixture();
    for c in &f.cases {
        let face_units = c.face * UNITS;
        for p in &c.periods {
            let (s, e) = (date(&p.accrual_start), date(&p.accrual_end));
            let ours = coupon_amount(1, face_units, c.rate_bps, AccrualBasis::DayCount(Thirty360::BondBasis), s, e)
                .unwrap() as f64;
            let ql = p.ql_amount_per_bond_bond_basis * UNITS as f64;
            // Ours is rounded down to whole units; QuantLib returns an unrounded float.
            assert!(ours <= ql + 1e-6 && ql - ours < 1.0, "{} {s:?}: ours {ours} vs QuantLib {ql}", c.name);
        }
    }
}

#[test]
fn fixed_per_period_amounts_match_financepy() {
    let f = fixture();
    for c in &f.cases {
        let face_units = c.face * UNITS;
        let basis = AccrualBasis::FixedPerPeriod { periods_per_year: c.periods_per_year };
        for (p, fp) in c.periods.iter().zip(&c.fp_fixed_flow_fraction_of_face) {
            let (s, e) = (date(&p.accrual_start), date(&p.accrual_end));
            let ours = coupon_amount(1, face_units, c.rate_bps, basis, s, e).unwrap();
            let expected = (fp * face_units as f64).round() as u64;
            assert_eq!(ours, expected, "{} {s:?}", c.name);
        }
    }
}
