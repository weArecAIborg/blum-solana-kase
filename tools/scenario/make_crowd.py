"""Generate a many-holder scenario (scenarios/<name>.json) and its holder keypairs, reproducibly from a seed.

The bond: B1 terms (1,000 face, 10%, semi-annual, 2 years). What happens, all drawn from the seed:
  - placement: every holder gets 1-20 bonds before the first record moment;
  - after each of the first three record moments: random trades between holders;
  - after record moment 0: an issuer buyback (first come) at 990, up to ~4% of the supply;
  - after record moment 1: a tender offer at 1,000, up to ~4% of the supply, open across record moment 2 (closes 10
    bond days after it), with one withdrawal;
  - redemption at maturity.
The generator replays positions in the operator's order (trades, then offer actions; tenders lock bonds until their
settlement after the window) so every action it writes is valid: nobody sells or tenders more than their free bonds.

Usage: .venv/bin/python3 tools/scenario/make_crowd.py <holders> <seed> <seconds per bond day> <name>
  e.g. ... 1000 20261008 20 devnet-crowd-1000   ->  scenarios/devnet-crowd-1000.json, keys/devnet/crowd/bh*.json
"""

import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOLANA = Path.home() / ".local/share/solana/install/active_release/bin/solana-keygen"


def keypair(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(SOLANA), "new", "--no-bip39-passphrase", "--silent", "-o", str(path)], check=True)
    return str(path.relative_to(ROOT))


def main(n, seed, seconds_per_day, name):
    rng = random.Random(seed)
    names = [f"BH#{i}" for i in range(1, n + 1)]
    wallets = {h: keypair(ROOT / f"keys/devnet/crowd/bh{i}.json") for i, h in enumerate(names, 1)}
    placements = {h: rng.randint(1, 20) for h in names}
    pos = dict(placements)
    locked = {h: 0 for h in names}
    free = lambda h: pos[h] - locked[h]
    supply = sum(placements.values())
    trades, offers = [], []
    trades_per_phase = max(3, n // 5)

    def trade_phase(k):
        for _ in range(trades_per_phase):
            sellers = [h for h in names if free(h) > 0]
            seller = rng.choice(sellers)
            buyer = rng.choice([h for h in names if h != seller])
            amount = rng.randint(1, min(5, free(seller)))
            pos[seller] -= amount
            pos[buyer] += amount
            trades.append({"afterRecordMoment": k, "from": seller, "to": buyer, "amount": amount})

    # Phase 0: trades, then the buyback (first come, first served, until the maximum).
    trade_phase(0)
    buyback_max = max(2, supply * 4 // 100)
    sold, actions = 0, []
    for h in rng.sample(names, min(len(names), max(12, n // 10))):
        q = min(free(h), rng.randint(1, 3), buyback_max - sold)
        if q <= 0:
            continue
        actions.append({"afterRecordMoment": 0, "holder": h, "sell": q})
        pos[h] -= q
        sold += q
    offers.append({"id": 0, "kind": "buyback", "priceUnits": "990000000", "maxQuantity": buyback_max,
                   "open": {"recordMoment": 0, "plusDays": 2}, "close": {"recordMoment": 1, "plusDays": -2},
                   "actions": actions})

    # Phase 1: trades, then tenders (locked until settled after the window, which spans record moment 2).
    trade_phase(1)
    tender_max = max(2, supply * 4 // 100)
    tendered, actions = {}, []
    for h in rng.sample(names, min(len(names), max(15, n // 7))):
        q = min(free(h), rng.randint(1, 6))
        if q <= 0:
            continue
        actions.append({"afterRecordMoment": 1, "holder": h, "tender": q})
        locked[h] += q
        tendered[h] = q
    withdrawer = next(h for h in tendered if tendered[h] > 1)
    actions.append({"afterRecordMoment": 1, "holder": withdrawer, "withdraw": 1})
    locked[withdrawer] -= 1
    tendered[withdrawer] -= 1
    offers.append({"id": 1, "kind": "tender", "priceUnits": "1000000000", "maxQuantity": tender_max,
                   "open": {"recordMoment": 1, "plusDays": 2}, "close": {"recordMoment": 2, "plusDays": 10},
                   "actions": actions})

    # Phase 2: trades while the tender is still open (locked bonds stay put), then the pro-rata settlement.
    trade_phase(2)
    total = sum(tendered.values())
    for h, q in tendered.items():
        accepted = q if total <= tender_max else q * tender_max // total
        pos[h] -= accepted
        locked[h] -= q

    base = json.loads((ROOT / "scenarios/devnet-full.json").read_text())
    scenario = {
        "name": f"DEVNET CROWD: B1 terms, {n} holders, {len(trades)} trades at record moments, issuer buyback, "
                f"tender offer across a record moment, redemption (1 bond day = {seconds_per_day} chain seconds; "
                f"generated with seed {seed})",
        "terms": dict(base["terms"], maxSupply=sum(placements.values())),  # the whole issue is placed
        "calendar": base["calendar"],
        "weekend": base["weekend"],
        "bondTimeAtCreation": base["bondTimeAtCreation"],
        "chainSecondsPerBondDay": seconds_per_day,
        "keys": dict(base["keys"], bondMint=keypair(ROOT / f"keys/devnet/bond-mint-{name}.json")),
        "wallets": wallets,
        "placements": placements,
        "trades": trades,
        "offers": offers,
        "handover": None,
    }
    (ROOT / f"scenarios/{name}.json").write_text(json.dumps(scenario, indent=2) + "\n")
    print(f"scenarios/{name}.json: {n} holders, supply {supply}, {len(trades)} trades, "
          f"buyback {sold}/{buyback_max}, tender {total} tendered for {tender_max}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("usage: make_crowd.py <holders> <seed> <seconds per bond day> <name>", file=sys.stderr)
        sys.exit(2)
    main(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
