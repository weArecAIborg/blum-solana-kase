//! Off-chain setup tool: payment days and record days of a regular bond, for `create_bond`.
//!
//! Usage: cargo run -p bond_math --example schedule -- <issue YYYY-MM-DD> <maturity YYYY-MM-DD> <months>
//!        <holidays file> <weekend, e.g. Saturday,Sunday> <record business days before payment>
//! Output (JSON): {"payment_days":[...],"payment_dates":[...],"record_days":[...],"record_dates":[...]}
//! Payment days follow the Following convention on the bond's calendar; each record day is the given number of
//! business days before its payment day (0 = the payment day itself).
//! The holidays file has one ISO date per line; lines starting with '#' are comments.

use std::env;
use std::fs;
use std::process::exit;

use bond_math::calendar::{BusinessDayConvention, Calendar};
use bond_math::date::{Date, Weekday};
use bond_math::schedule::regular_schedule;

fn fail(msg: String) -> ! {
    eprintln!("{msg}");
    exit(2)
}

fn parse_date(s: &str) -> Date {
    let p: Vec<&str> = s.trim().split('-').collect();
    let parsed = (p.len() == 3)
        .then(|| Some(Date::new(p[0].parse().ok()?, p[1].parse().ok()?, p[2].parse().ok()?)?))
        .flatten();
    parsed.unwrap_or_else(|| fail(format!("invalid date: {s}")))
}

fn parse_weekday(s: &str) -> Weekday {
    match s.trim() {
        "Monday" => Weekday::Monday,
        "Tuesday" => Weekday::Tuesday,
        "Wednesday" => Weekday::Wednesday,
        "Thursday" => Weekday::Thursday,
        "Friday" => Weekday::Friday,
        "Saturday" => Weekday::Saturday,
        "Sunday" => Weekday::Sunday,
        other => fail(format!("invalid weekday: {other}")),
    }
}

fn iso(d: Date) -> String {
    format!("\"{:04}-{:02}-{:02}\"", d.year(), d.month(), d.day())
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.len() != 6 {
        fail("usage: schedule <issue> <maturity> <months> <holidays file> <weekend> <record business days before payment>".into());
    }
    let issue = parse_date(&args[0]);
    let maturity = parse_date(&args[1]);
    let months: u32 = args[2].parse().unwrap_or_else(|_| fail(format!("invalid months: {}", args[2])));
    let text = fs::read_to_string(&args[3]).unwrap_or_else(|e| fail(format!("cannot read {}: {e}", args[3])));
    let weekend: Vec<Weekday> = args[4].split(',').map(parse_weekday).collect();
    let record_lag: u32 = args[5].parse().unwrap_or_else(|_| fail(format!("invalid record lag: {}", args[5])));
    let holidays: Vec<Date> =
        text.lines().filter(|l| !l.trim().is_empty() && !l.starts_with('#')).map(parse_date).collect();
    let calendar = Calendar::new(&weekend, &holidays).unwrap_or_else(|e| fail(format!("bad calendar: {e:?}")));
    let periods = regular_schedule(issue, maturity, months, &calendar, BusinessDayConvention::Following)
        .unwrap_or_else(|e| {
            eprintln!("schedule error: {e:?}");
            exit(1)
        });
    let payment: Vec<Date> = periods.iter().map(|p| p.payment_date).collect();
    let record: Vec<Date> = payment
        .iter()
        .map(|&d| (0..record_lag).fold(d, |d, _| calendar.adjust(d.add_days(-1), BusinessDayConvention::Preceding)))
        .collect();
    let days = |v: &[Date]| v.iter().map(|d| d.days().to_string()).collect::<Vec<_>>().join(",");
    let dates = |v: &[Date]| v.iter().map(|d| iso(*d)).collect::<Vec<_>>().join(",");
    println!(
        "{{\"payment_days\":[{}],\"payment_dates\":[{}],\"record_days\":[{}],\"record_dates\":[{}]}}",
        days(&payment),
        dates(&payment),
        days(&record),
        dates(&record)
    );
}
