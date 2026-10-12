"""Generate reference values for bond_math tests from QuantLib, FinancePy and the `holidays` calendar.

The libraries are checkers only; the product does not depend on them.
Run: .venv/bin/python3 tools/reference/generate_fixtures.py
Writes: crates/bond_math/tests/fixtures/reference.json
"""

import datetime as dt
import importlib.metadata as md
import json
from pathlib import Path

import holidays
import QuantLib as ql
from financepy.products.bonds.bond import Bond as FpBond, CouponType
from financepy.utils.date import Date as FpDate
from financepy.utils.day_count import DayCountTypes
from financepy.utils.frequency import FrequencyTypes

OUT = Path(__file__).resolve().parents[2] / "crates/bond_math/tests/fixtures/reference.json"
HOLIDAY_YEARS = range(2024, 2037)

# KASE MUM108_0015 page, "Дата предыдущей купонной выплаты: 06.03.26" and
# "Период ближайшей купонной выплаты: 06.03.27 - 09.03.27" (read 2026-10-07).
KASE_MUM108_0015_PAYMENT_DATES = ["2026-03-06", "2027-03-09"]

CASES = [
    # name, issue, maturity, months per period, face, rate bps, periods per year (fixed basis)
    ("MUM108_0015 (MEUKAM)", "2025-03-06", "2034-03-06", 12, 1000, 1450, 1),
    ("B1 listing example, issued 2026-03-06", "2026-03-06", "2028-03-06", 6, 1000, 1000, 2),
    ("B1 listing example, issued 2026-08-31", "2026-08-31", "2028-08-31", 6, 1000, 1000, 2),
    ("B1 listing example, issued 2027-02-28", "2027-02-28", "2029-02-28", 6, 1000, 1000, 2),
    ("B1 listing example, issued 2028-02-29 (stub)", "2028-02-29", "2030-02-28", 6, 1000, 1000, 2),
]


def iso(d: ql.Date) -> str:
    return dt.date(d.year(), d.month(), d.dayOfMonth()).isoformat()


def qd(s: str) -> ql.Date:
    y, m, d = map(int, s.split("-"))
    return ql.Date(d, m, y)


def fpd(s: str) -> FpDate:
    y, m, d = map(int, s.split("-"))
    return FpDate(d, m, y)


def kz_calendar(kz_holidays):
    cal = ql.BespokeCalendar("KZ")
    cal.addWeekend(ql.Saturday)
    cal.addWeekend(ql.Sunday)
    for d in kz_holidays:
        cal.addHoliday(ql.Date(d.day, d.month, d.year))
    return cal


def case(cal, name, issue, maturity, months, face, rate_bps, per_year):
    schedule = ql.Schedule(
        qd(issue), qd(maturity), ql.Period(months, ql.Months), cal,
        ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False,
    )
    bond_basis = ql.Thirty360(ql.Thirty360.BondBasis)
    bond = ql.FixedRateBond(0, float(face), schedule, [rate_bps / 10_000], bond_basis, ql.Following, 100.0,
                            qd(issue), cal)
    periods = []
    for cf in bond.cashflows():
        c = ql.as_fixed_rate_coupon(cf)
        if c is None:
            continue
        s, e = c.accrualStartDate(), c.accrualEndDate()
        periods.append({
            "accrual_start": iso(s),
            "accrual_end": iso(e),
            "payment_date": iso(c.date()),
            "ql_days_bond_basis": bond_basis.dayCount(s, e),
            "ql_days_european": ql.Thirty360(ql.Thirty360.European).dayCount(s, e),
            "ql_days_usa": ql.Thirty360(ql.Thirty360.USA).dayCount(s, e),
            "ql_amount_per_bond_bond_basis": c.amount(),
        })
    regular = all(schedule.isRegular(i) for i in range(1, len(schedule)))

    freq = {1: FrequencyTypes.ANNUAL, 2: FrequencyTypes.SEMI_ANNUAL}[per_year]
    fp = FpBond(fpd(issue), fpd(maturity), rate_bps / 10_000, freq, DayCountTypes.THIRTY_360_BOND,
                cpn_type=CouponType.FIXED)
    fp_flows = [round(a, 12) for a in fp.flow_amounts[1:]]
    fp_dates = [dt.date(d.y, d.m, d.d).isoformat() for d in fp.cpn_dts[1:]]

    return {
        "name": name,
        "issue": issue,
        "maturity": maturity,
        "months_per_period": months,
        "face": face,
        "rate_bps": rate_bps,
        "periods_per_year": per_year,
        "ql_regular": regular,
        "periods": periods,
        "fp_fixed_flow_fraction_of_face": fp_flows,
        "fp_coupon_dates": fp_dates,
    }


def main():
    kz = holidays.country_holidays("KZ", years=HOLIDAY_YEARS)
    kz_dates = sorted(kz.keys())
    cal = kz_calendar(kz_dates)
    out = {
        "generated_by": {
            "QuantLib": ql.__version__,
            "financepy": md.version("financepy"),
            "holidays": holidays.__version__,
        },
        "kz_holidays": [d.isoformat() for d in kz_dates],
        "kase_mum108_0015_payment_dates": KASE_MUM108_0015_PAYMENT_DATES,
        "cases": [case(cal, *c) for c in CASES],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
