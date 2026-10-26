"""Checks of the audit's replay (`timeline`) that need no RPC. Run: .venv/bin/python3 tools/audit/test_timeline.py

Case from the review of the operator screen's plan (colosseum/operator-screen-plan.md, A.8): two tenders of one
offer settled on both sides of a record moment. Each holder's accepted bonds must leave its position in its own
phase: at record moment 1, A has already sold (settled before it), B still holds (settled after it).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import timeline  # noqa: E402

offer = {
    "id": 1, "kind": "tender", "priceUnits": "1", "maxQuantity": 20,
    "open": {"recordMoment": 0, "plusDays": 0}, "close": {"recordMoment": 0, "plusDays": 0.25},
    "actions": [
        {"afterRecordMoment": 0, "holder": "A", "tender": 10, "slot": 1},
        {"afterRecordMoment": 0, "holder": "B", "tender": 10, "slot": 2},
    ],
    "settlements": [
        {"holder": "A", "number": 0, "afterRecordMoment": 0, "slot": 3},
        {"holder": "B", "number": 1, "afterRecordMoment": 1, "slot": 5},
    ],
}
tl = timeline({"A": 10, "B": 10}, [], [offer], [0, 86_400])
assert tl["at_record"][1] == {"A": 0, "B": 10}, tl["at_record"]
assert tl["final"] == {"A": 0, "B": 0}, tl["final"]
assert tl["moves"]["A"] == [10, -10] and tl["moves"]["B"] == [10, -10], tl["moves"]

# Oversubscribed: 30 tendered for 20, shares in tender-number order (B's tender came first).
offer2 = dict(offer, maxQuantity=20, actions=[
    {"afterRecordMoment": 0, "holder": "A", "tender": 10, "slot": 2},
    {"afterRecordMoment": 0, "holder": "B", "tender": 20, "slot": 1},
], settlements=[
    {"holder": "B", "number": 0, "afterRecordMoment": 0, "slot": 3},
    {"holder": "A", "number": 1, "afterRecordMoment": 0, "slot": 4},
])
tl = timeline({"A": 10, "B": 20}, [], [offer2], [0, 86_400])
assert tl["final"] == {"A": 10 - (30 * 20 // 30 - 20 * 20 // 30), "B": 20 - 20 * 20 // 30}, tl["final"]
print("timeline checks passed")
