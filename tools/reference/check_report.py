"""Check an operator report (ops/src/scenario.ts) against QuantLib and FinancePy, independently of the program.

For every coupon, every offer (buyback, tender) and the redemption it recomputes:
  - payment dates (QuantLib schedule + the bond's calendar as the report states it);
  - each holder's position at each record moment, from the scenario's placements, trades and offer actions alone
    (tools/audit/audit.py `timeline`, the same order the operator follows);
  - per-bond coupon (FinancePy CouponType.FIXED = Kazakh decree formula; QuantLib 30/360 shown alongside);
  - each entitlement, payment and principal, with SCoRE Standard 4 rounding (round down on the total holding);
  - final balances.
Run: .venv/bin/python3 tools/reference/check_report.py ops/out/<scenario>.json
Exit code 1 on any mismatch.
"""

import json
import sys
from collections import defaultdict
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import QuantLib as ql
from financepy.products.bonds.bond import Bond as FpBond, CouponType
from financepy.utils.date import Date as FpDate
from financepy.utils.day_count import DayCountTypes
from financepy.utils.frequency import FrequencyTypes

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/audit"))
from audit import record_moment_unix, timeline  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def qd(s):
    y, m, d = map(int, s.split("-"))
    return ql.Date(d, m, y)


def iso(d):
    return f"{d.year():04d}-{d.month():02d}-{d.dayOfMonth():02d}"


def bond_calendar(calendar_file, weekend):
    """The bond's calendar as the report states it (holiday file, weekend days)."""
    cal = ql.BespokeCalendar("bond")
    for day in weekend:
        cal.addWeekend(getattr(ql, day))
    for line in (ROOT / calendar_file).read_text().splitlines():
        if line and not line.startswith("#"):
            cal.addHoliday(qd(line))
    return cal


def main(report_path):
    r = json.loads(Path(report_path).read_text())
    t = r["terms"]
    face_units = int(t["faceUnits"])
    unit = 10 ** t["cashDecimals"]
    face = Decimal(face_units) / unit
    rate = Decimal(t["rateBps"]) / 10_000
    per_year = 12 // t["monthsPerPeriod"]
    print(f"{r['name']}\nbond {r['bond']}  program {r['programId']}")

    print("\nSchedule")
    cal = bond_calendar(t["calendar"], t["weekend"])
    sched = ql.Schedule(qd(t["issue"]), qd(t["maturity"]), ql.Period(t["monthsPerPeriod"], ql.Months), cal,
                        ql.Unadjusted, ql.Unadjusted, ql.DateGeneration.Backward, False)
    ql_bond = ql.FixedRateBond(0, float(face), sched, [float(rate)], ql.Thirty360(ql.Thirty360.BondBasis),
                               ql.Following, 100.0, qd(t["issue"]), cal)
    ql_coupons = [c for c in (ql.as_fixed_rate_coupon(cf) for cf in ql_bond.cashflows()) if c is not None]
    ql_dates = [iso(c.date()) for c in ql_coupons]
    check("payment dates = QuantLib (bond calendar, Following)", ql_dates == t["paymentDates"],
          ", ".join(t["paymentDates"]))
    freq = {1: FrequencyTypes.ANNUAL, 2: FrequencyTypes.SEMI_ANNUAL, 4: FrequencyTypes.QUARTERLY, 12: FrequencyTypes.MONTHLY}[per_year]
    y, m, d = map(int, t["issue"].split("-"))
    y2, m2, d2 = map(int, t["maturity"].split("-"))
    fp = FpBond(FpDate(d, m, y), FpDate(d2, m2, y2), float(rate), freq, DayCountTypes.THIRTY_360_BOND,
                cpn_type=CouponType.FIXED)
    fp_flows = fp.flow_amounts[1:]

    # Positions from placements, trades and offer actions only (no program data).
    record_times = [record_moment_unix(d, t["recordTimeLocal"], t["utcOffsetHours"]) for d in t["recordDates"]]
    tl = timeline(r["placements"], r["trades"], r["offerSpecs"], record_times)
    holders = [x["holder"] for x in r["events"][0]["positions"]]
    expected_cash = {h: Decimal(0) for h in holders}
    expected_vault_left = {}

    print("\nCoupons")
    for ev in r["events"]:
        e = ev["index"]
        fp_per_bond = Decimal(str(fp_flows[e])) * face_units  # FinancePy FIXED, base units
        ql_per_bond = Decimal(str(ql_coupons[e].amount())) * unit
        positions = {h: tl["at_record"][e].get(h, 0) for h in holders}
        supply = sum(positions.values())
        print(f" coupon {e}: record {ev['recordDate']} {ev['recordTime']}, paid from {ev['paymentDate']}")
        exp_budget = (Decimal(supply) * fp_per_bond).to_integral_value(rounding=ROUND_FLOOR)
        check(f"coupon {e}: supply at record moment", ev["supplyAtRecord"] == supply, f"program {ev['supplyAtRecord']}, expected {supply}")
        check(f"coupon {e}: budget = whole supply x FinancePy FIXED coupon, rounded down", Decimal(ev["budget"]) == exp_budget,
              f"program {Decimal(ev['budget']) / unit}, expected {exp_budget / unit} (QuantLib 30/360 per bond {ql_per_bond / unit:.6f})")
        paid = {p["holder"]: p for p in ev["payments"]}
        program_pos = {x["holder"]: x for x in ev["positions"]}
        paid_total = Decimal(0)
        for h, exp_pos in positions.items():
            got_pos = program_pos[h]["position"]
            exp_amt = (Decimal(exp_pos) * fp_per_bond).to_integral_value(rounding=ROUND_FLOOR)
            check(f"coupon {e} {h}: position at record moment", got_pos == exp_pos,
                  f"program {got_pos}, expected {exp_pos} (balance now {program_pos[h]['balanceNow']})")
            got = Decimal(paid[h]["amount"]) if h in paid else Decimal(0)
            check(f"coupon {e} {h}: paid on chain", got == exp_amt,
                  f"{got / unit} vs {exp_amt / unit}" + (f" (tx {paid[h]['signature'][:16]}...)" if h in paid else ""))
            expected_cash[h] += exp_amt
            paid_total += exp_amt
        expected_vault_left[ev["vault"]] = exp_budget - paid_total

    print("\nOffers (buyback, tender)")
    measured = {h: [] for h in holders}
    for o in r["offers"]:
        paid, bought = Decimal(0), 0
        for x in [a for a in o["actions"] if a["action"] == "sell"] + o["settlements"]:
            if x.get("burned"):
                measured[x["holder"]].append((x["burned"], int(x["paid"])))
                paid += Decimal(x["paid"])
                bought += x["burned"]
        expected_vault_left[o["vault"]] = Decimal(o["budget"]) - paid
        print(f" offer {o['id']} ({o['kind']}): price {Decimal(o['priceUnits']) / unit}, max {o['maxQuantity']}, "
              f"window {o['openTime']} - {o['closeTime']}, bought {bought}")
    for h in holders:
        exp = sorted(tl["retire"][h])
        got = sorted(measured[h])
        check(f"offers {h}: (bonds bought, cash paid)", got == exp, f"program {got} vs expected {exp}")
        expected_cash[h] += sum(Decimal(c) for _, c in exp)
    positions = {h: tl["final"].get(h, 0) for h in holders}

    print("\nRedemption")
    red = {x["holder"]: x for x in r["redemption"]["redemptions"]}
    price = Decimal(t["redemptionPriceBps"]) / 10_000
    supply = sum(positions.values())
    exp_budget = (Decimal(supply) * face_units * price).to_integral_value(rounding=ROUND_FLOOR)
    check("redemption: budget = whole supply x face x price, rounded down", Decimal(r["redemption"]["budget"]) == exp_budget,
          f"{Decimal(r['redemption']['budget']) / unit} vs {exp_budget / unit}")
    paid_total = Decimal(0)
    for h, pos in positions.items():
        if pos == 0:
            continue
        exp_principal = (Decimal(pos) * face_units * price).to_integral_value(rounding=ROUND_FLOOR)
        got = red.get(h)
        check(f"redemption {h}: burned = position", got is not None and got["burned"] == pos,
              f"{got['burned'] if got else 'none'} vs {pos}")
        check(f"redemption {h}: principal", got is not None and Decimal(got["principal"]) == exp_principal,
              f"{(Decimal(got['principal']) / unit) if got else 'none'} vs {exp_principal / unit}")
        expected_cash[h] += exp_principal
        paid_total += exp_principal
    expected_vault_left[r["redemption"]["vault"]] = exp_budget - paid_total

    print("\nFinal balances")
    check("bond supply = 0", r["final"]["supply"] == "0")
    for v, left in expected_vault_left.items():
        got = Decimal(r["final"]["vaults"][v])
        check(f"vault {v[:8]}...: left = budget - payments (rounding only)", got == left, f"{got / unit} vs {left / unit}")
    # What this bond delivered = final cash - cash before the run (holders may hold the cash token from elsewhere).
    initial = r.get("initialCash")
    check("report carries initialCash", initial is not None, "" if initial is not None else "report predates initialCash; rerun the scenario")
    # Cash moved by secondary trades settled delivery versus payment (a trade's `cash` leg: buyer to seller) and the
    # bank's top-ups that funded buyers are the parties' own business, outside this bond's payments: what this bond
    # delivered = final - initial - received in trades + paid in trades - bank top-ups.
    outside = defaultdict(Decimal)
    for tr in r["trades"]:
        if tr.get("cash") is not None:
            outside[tr["from"]] += Decimal(tr["cash"])
            outside[tr["to"]] -= Decimal(tr["cash"])
    for h, v in (r.get("bankTopUps") or {}).items():
        outside[h] += Decimal(v)
    for h, exp in expected_cash.items():
        if initial is None:
            continue
        got = Decimal(r["final"]["cash"][h]) - Decimal(initial[h]) - outside[h]
        check(f"cash {h}", got == exp, f"{got / unit} vs {exp / unit} (final {Decimal(r['final']['cash'][h]) / unit}, "
                                       f"initial {Decimal(initial[h]) / unit})")

    if r["scenario"] == "b1":
        print("\nListing example")
        first = {x["holder"]: x for x in r["events"][0]["payments"]}
        holder10 = [x["holder"] for x in r["events"][0]["positions"] if x["position"] == 10]
        check("a holder with 10 bonds on the record date gets 500 per coupon",
              bool(holder10) and Decimal(first[holder10[0]]["amount"]) == 500 * unit)

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}"
          f"  ({len(r['transactions'])} transactions)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
