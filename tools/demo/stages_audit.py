"""Auditor references for the stages demo. Only public measurements and an explicit source whitelist are exported.

The source pages are snapshots of the sources the pages were built from. No key files or
operator reports are published. Preview metadata is embedded, so hovering does not contact an RPC or explorer.
"""

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from site_nav import chrome

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_SOURCES = {
    "programs/bond_servicing/src/instructions/place.rs",
    "programs/bond_servicing/src/instructions/transfer_hook.rs",
    "programs/bond_servicing/src/instructions/pay_coupon.rs",
    "programs/bond_servicing/src/instructions/redeem_holder.rs",
    "programs/bond_servicing/src/instructions/offer.rs",
    "programs/bond_servicing/src/instructions/create_bond.rs",
    "programs/bond_servicing/src/instructions/confirm_funding.rs",
    "programs/bond_servicing/src/state.rs", "programs/bond_servicing/src/lib.rs",
    "ops/src/scenario.ts", "ops/src/attacks.ts",
    "tools/demo/measure_attempts.py", "tools/audit/audit.py", "tools/reference/check_report.py",
    "crates/bond_math/src/amount.rs", "scenarios/devnet-crowd-1000.json",
    "docs/tradfi-vs-onchain.md", "docs/bond-mechanics.md",
    "tools/demo/stages_audit.py", "tools/demo/build_stages.py", "tools/demo/stages_template.html",
    "tools/demo/check_stages.cjs",
}
SCORE = "https://www.ecb.europa.eu/press/intro/publications/pdf/amiseco202112_corporateevents.en.pdf"
AFME = "https://www.afme.eu/portals/0/globalassets/downloads/standard-forms-and-documents/afme-market-standards-for-corporate-actions-processing-revised-version-2012.pdf"
CSD = "https://backend.kcsd.kz/storage/documents/files/rules-gov-securities-5-ru-6a8c2c9aa90b4.pdf"
T2S = "https://www.ecb.europa.eu/paym/target/t2s/governance/pdf/casg/ecb.targetseccasg130316_T2SMarketClaimStandards.en.pdf"
CASCADE = "https://www.ecb.europa.eu/paym/target/t2s/governance/pdf/casg/ecb.targetseccasg161130_ExplanatoryNoteOnClaimsAndTransformations.en.pdf"
RULES = {
    "issue": [("SCoRE · CSD glossary", SCORE)],
    "trade": [("AFME · market claims", AFME), ("T2S · market claims", T2S)],
    "record": [("SCoRE · record date", SCORE), ("AFME · record date", AFME), ("Sample: Kazakhstan CSD · article 8", CSD)],
    "calculate": [("SCoRE · Standard 4", SCORE)],
    "fund": [("SCoRE · Standard 8", SCORE), ("Sample: Kazakhstan CSD · article 8", CSD)],
    "pay": [("SCoRE · INTR", SCORE), ("Sample: Kazakhstan CSD · article 8", CSD)],
    "redeem": [("AFME · mandatory reorganisations", AFME), ("SCoRE · Standard 8", SCORE), ("Sample: Kazakhstan CSD · article 10", CSD)],
    "offer": [("SCoRE · BIDS / TEND", SCORE), ("Sample: Kazakhstan government bond rules", "https://adilet.zan.kz/rus/docs/P2500000473")],
    "audit": [("AFME · custody cascade", AFME), ("T2S · explanatory note", CASCADE)],
}

# Type, actual mechanism, and the limit of the observation. A refused scripted input is not an exploit discovery.
ATTACKS = {
    "register-self": ("Program authorization", "Calls register_holder with the attacker signing as registrar.", "Checks this Bond's registrar binding; it does not test a compromised registrar."),
    "mint-bonds": ("Native authority", "Calls Token-2022 mint_to with the attacker's key as mint authority.", "Ordinary token authority rejection, not a bond-math attack."),
    "mint-as-program": ("Native signature", "Names the Bond PDA as mint authority, but removes its signer flag.", "Tests missing PDA authorization; no PDA signature was forged."),
    "take-bonds": ("Native authority", "Calls transfer_checked on the victim account with the attacker's key.", "Stops at token ownership, before the bond hook's business rules."),
    "take-as-delegate": ("Native signature", "Names the permanent-delegate PDA but supplies no PDA signature.", "Tests signature enforcement, not a malicious authorized delegate."),
    "burn-bonds": ("Token configuration", "Calls standard burn_checked on the permissioned-burn mint.", "Shows the standard burn instruction is disabled; this attempt is not signed by the rightful holder."),
    "burn-permissioned": ("Token configuration", "Calls raw permissioned BurnChecked (46,2), naming attacker as both authorities.", "Rejection at the configured burn-authority check; valid owner/delegate authorization is also absent."),
    "burn-as-program": ("Native signature", "Names the PDA in permissioned burn, clearing its signer flag.", "No PDA signature is supplied or broken."),
    "freeze-holder": ("Token configuration", "Calls freeze_account on a mint created with no freeze authority.", "Verifies mint configuration; it does not exercise freezing of the separate cash token."),
    "send-unregistered": ("Program invariant", "The victim signs a hook-aware transfer to the unregistered attacker's ATA.", "A meaningful registry check with valid transfer authority. The holder cooperates in the test."),
    "second-account": ("Program invariant", "The victim signs a transfer to a valid second token account owned by the victim.", "Verifies the one-ATA position model; rejecting non-ATAs is an integration policy, not universal bond law."),
    "skip-hook": ("Token integration", "A holder signs the legacy unchecked Transfer, which omits the mint.", "Token-2022 requires the mint for this account profile; it is not a successful hook bypass."),
    "forge-position": ("Program invariant", "Calls the hook Execute entry point directly with amount 5 and real holder records.", "The transferring flag rejects this call. Does not cover every CPI/reentrancy composition."),
    "take-program": ("Native authority", "Calls the upgradeable loader's SetAuthority with the attacker as current authority.", "Does not test the real upgrade authority being compromised or upgrading maliciously."),
    "fee-cash-token": ("Configuration guard", "The registrar creates a real 1% fee mint, then attempts create_bond using it.", "An authorized bad-configuration test, not an outsider attack. Other cash extensions require their own tests."),
    "delegate-cash-token": ("Configuration guard", "The registrar creates a real mint whose permanent delegate is the attacker, then attempts create_bond using it.", "An authorized bad-configuration test, not an outsider attack."),
    "place-beyond-issue": ("Program invariant", "Before the first record moment the registrar calls place for one bond more than the issue size.", "A supply-cap test with real issuance authority while placement is open."),
    "place-late": ("Program invariant", "The registrar calls place for 100 after the first record boundary.", "A business-time restriction tested with real issuance authority."),
    "sell-others-bonds": ("Program authorization", "Attacker signs sell_to_offer while the retirement accounts identify the victim.", "Tests seller/account binding in the custom buyback path."),
    "over-maximum": ("Program invariant", "A holder signs one more sale after the first-come offer cap is reached.", "A meaningful funded-offer quantity-boundary test."),
    "confirm-short": ("Program invariant", "Bank funds budget minus one base unit; attacker calls confirm_funding.", "Checks full numerical backing before activation; does not prove the cash token remains available forever."),
    "pay-unfunded": ("Program invariant", "Calls pay_coupon before successful funding confirmation.", "Checks the funded gate, separately from the actual cash balance."),
    "drain-vault": ("Native authority", "Calls cash-token transfer_checked from the vault with attacker as owner.", "Standard cash-account authority rejection."),
    "drain-as-program": ("Native signature", "Names the vault's PDA owner but clears its signer flag.", "Standard missing-signature check; no PDA signature forgery."),
    "redirect-coupon": ("Program invariant", "Passes the victim's right with the attacker's cash ATA as destination.", "Tests beneficiary binding in a deliberately permissionless payout."),
    "claim-as-owner": ("Program invariant", "Passes the victim's HolderRecord with attacker as owner and recipient.", "Observed rejection is ConstraintSeeds; the harness only required the broad substring 'Constraint'."),
    "pay-twice": ("Program invariant", "Repeats the same coupon/account payout after the first committed payment.", "Tests the consumed bit for this event/account, not all concurrency or migration cases."),
    "late-buyer": ("Entitlement rule", "Requests coupon 0 for a holder with zero at record time and a positive later balance.", "No entitlement exists. Acting as a permissionless payer is otherwise allowed."),
    "sell-tendered": ("Program invariant", "A tendering holder signs a transfer of its entire position, including locked quantity.", "Checks lock enforcement with genuine transfer authority."),
    "other-vault": ("Program invariant", "Passes coupon 0's vault while paying coupon 1.", "Tests event-vault isolation; a failed transaction proves this supplied substitution was rejected."),
    "frozen-cash": ("Recovery test", "The bank freezes a holder's cash ATA; a normal payer attempts the coupon, then bank thaws it.", "Controlled failure injection by the real cash freeze authority, not an outsider exploit."),
    "trade-at-maturity": ("Program invariant", "A holder signs a hook-aware transfer after the final record boundary.", "Verifies this profile's trading cutoff; other contractual cutoffs are separate profiles."),
    "redirect-principal": ("Program invariant", "Redeems the victim's position but supplies the attacker's cash ATA.", "Tests principal beneficiary binding; rejected before any committed retirement."),
    "coupon-from-principal": ("Program invariant", "Calls pay_coupon with the redemption Event and its real vault.", "Tests event-kind separation with otherwise corresponding accounts."),
    "redeem-twice": ("Program invariant", "Repeats redemption after the victim's live position is retired.", "NothingToPay establishes this position has no remaining redeemable quantity."),
}


def build_evidence(D, K, probe, H, scenario, out_dir):
    """Return preview registry and chart bindings, exporting replayable public data and current source snapshots."""
    evidence = {}
    out_dir = Path(out_dir)
    audit_dir = out_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_files = {}

    def ref(label, href):
        return {"label": label, "href": href}

    def source(file, needle=None):
        if file not in PUBLIC_SOURCES:
            raise ValueError(f"Source export is restricted to the public allowlist: {file}")
        p = ROOT / file
        content = p.read_text()
        line = next((i for i, text in enumerate(content.splitlines(), 1) if needle and needle in text), 1)
        if file not in source_files:
            path = audit_dir / "source" / f"{file}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(content.encode()).hexdigest()
            body = "\n".join(f'<span class="line" id="L{i}"><a href="#L{i}" aria-label="Line {i}">{i}</a><code>{escape(s)}</code></span>'
                             for i, s in enumerate(content.splitlines(), 1))
            back = os.path.relpath(out_dir / "stages.html", path.parent)
            path.write_text(chrome(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(file)} · audit source</title><style>
:root{{color-scheme:light dark}}body{{margin:0;background:light-dark(#f4f5f2,#0f1011);color:light-dark(#0b0b0b,#eee);font:14px/1.5 system-ui}}
header{{padding:20px;position:sticky;top:0;background:light-dark(#fcfcfb,#1a1a19);border-bottom:1px solid #7775}}
a{{color:light-dark(#1c5cab,#6da7ec)}}h1{{font-size:18px;margin:8px 0}}p{{margin:4px 0;overflow-wrap:anywhere}}pre{{margin:0;padding:16px;overflow:auto}}
.line{{display:flex;scroll-margin-top:160px;min-height:21px}}.line:target{{background:#1c5cab22}}.line>a{{width:52px;flex:none;text-align:right;padding-right:16px;text-decoration:none;color:light-dark(#6f6d68,#aaa)}}code{{white-space:pre;font:13px/21px monospace}}
</style><header><a href="{escape(back)}">← Bond stages</a><h1>{escape(file)}</h1>
<p>Source snapshot · revision {revision[:12]}.</p><p>SHA-256 {digest}</p></header><pre>{body}</pre></html>''', "Stages", "Stages", os.path.relpath(out_dir, path.parent) + "/"))
            source_files[file] = {"revision": revision, "sha256": digest, "href": f"audit/source/{file}.html"}
        return ref(f"Source · {file}", f"audit/source/{file}.html#L{line}")

    def add(key, title, scope, summary, facts=(), refs=()):
        evidence[key] = {"title": title, "scope": scope, "summary": summary, "facts": list(facts), "refs": list(refs)}
        return key

    def address(value):
        return f"https://explorer.solana.com/address/{value}?cluster=devnet"

    def tx(value):
        return f"https://explorer.solana.com/tx/{value}?cluster=devnet"

    data = ref("Full measured crowd data", "data/devnet-crowd-1000.json")
    host_data = ref(f"All {len(H['attempts'])} probe measurements", "data/devnet-hostile-attempts.json")
    facts_data = ref("Auditor evidence index", "audit/evidence.json")
    by_sig = {s["signature"]: s for s in D["servicing"]}
    holders = {h["name"]: h for h in D["holders"]}
    code_map = {
        "placement": ("programs/bond_servicing/src/instructions/place.rs", "pub fn handle_place"),
        "trade": ("programs/bond_servicing/src/instructions/transfer_hook.rs", "pub fn handle_transfer_hook"),
        "coupon": ("programs/bond_servicing/src/instructions/pay_coupon.rs", "pub fn handle_pay_coupon"),
        "redemption": ("programs/bond_servicing/src/instructions/redeem_holder.rs", "pub fn handle_redeem_holder"),
        "buyback": ("programs/bond_servicing/src/instructions/offer.rs", "pub struct Retirement"),
        "funding": ("ops/src/scenario.ts", "fund vault for"),
    }
    events = D["events"]
    for e in events:
        key = f'tx:{e["signature"]}'
        s = by_sig.get(e["signature"], {})
        facts = [("Signature", e["signature"]), ("Recorded UTC block time", datetime.fromtimestamp(e["t"], timezone.utc).isoformat())]
        if s:
            facts += [("Slot", s["slot"]), ("Fee payer", s["feePayer"]), ("Fee (lamports)", s["fee"])]
        facts += [(name, f'{m["bonds"]:+} bonds; {m["cash"]:+,.6f} cash') for name, m in e["moves"].items()]
        references = [ref("Open transaction in Solana Explorer", e["link"])]
        references += [ref(f'{name} · wallet', holders[name]["link"]) for name in e["moves"] if name in holders]
        references += [data, source(*code_map[e["kind"]])]
        summary = "Measured bond-token transfer; no cash trade leg is present in this transaction." if e["kind"] == "trade" else "Token balance movements measured from the public ledger. Inspect the transaction and compare the recipient/amount."
        add(key, f'{e["kind"].capitalize()}' + (f' · coupon {e["coupon"]}' if e["coupon"] is not None else ""), "Crowd run · devnet · cached measurements", summary, facts, references)

    ledger_path = out_dir / "data/stages-probe-ledger.json"
    ledger = json.loads(ledger_path.read_text())
    if ledger.get("commitment") != "finalized":
        raise ValueError("The auditor ledger must contain finalized RPC records")
    from measure_attempts import reason_of
    for a in H["attempts"]:
        group, mechanism, limit = ATTACKS[a["id"]]
        raw = ledger["transactions"][a["id"]]
        t = raw["result"]
        if (t["transaction"]["signatures"][0] != a["signature"] or t["meta"]["err"] is None
                or reason_of(t["meta"]) != a["reason"] or a["guard"] not in reason_of(t["meta"])):
            raise ValueError(f"Finalized evidence disagrees with probe {a['id']}")
        facts = [("Actor named by test", a["actor"]), ("When", a["when"]), ("Expected guard token", a["guard"]),
                 ("Observed rejection", a["reason"]), ("Signature", a["signature"])]
        if raw:
            t = raw["result"]
            facts += [("Finalized slot", t["slot"]), ("Fee payer", t["transaction"]["message"]["accountKeys"][0]["pubkey"]),
                      ("Actual signers", "; ".join(k["pubkey"] for k in t["transaction"]["message"]["accountKeys"] if k["signer"])),
                      ("Fee (lamports, charged despite failure)", t["meta"]["fee"]), ("RPC verification (UTC)", ledger["verifiedAt"]),
                      ("Runtime error", json.dumps(t["meta"]["err"])),
                      ("Token balances", "Unchanged" if t["meta"].get("preTokenBalances") == t["meta"].get("postTokenBalances") else "Inspect raw pre/post balances")]
        add(f'attack:{a["id"]}', a["tries"], f'Hostile run · devnet · {group}', f'{mechanism} {limit}', facts,
            [ref("Inspect failed transaction and logs", a["explorer"]), host_data,
             ref("Raw finalized RPC records", "data/stages-probe-ledger.json"),
             source("ops/src/attacks.ts", f'id: "{a["id"]}"'), source("tools/demo/measure_attempts.py", "def reason_of")])

    for name, holder in holders.items():
        facts = [("Wallet", holder["address"])]
        references = [ref(f"{name} wallet", holder["link"]), data]
        for g in D["grid"]:
            facts.append((f'Coupon {g["coupon"]}', f'{g["atRecord"][name]} bonds at record; expected {g["expected"][name]:,.6f}; received {g["paid"][name]:,.6f}'))
        references += [ref(f'Coupon {e["coupon"]} payment', e["link"]) for e in events if name in e["moves"] and e["kind"] == "coupon"]
        add(f"holder:{name}", name, "Crowd run · account-level measurements", "Recorded quantities, expected cash and measured cash for this account.", facts, references)

    for i, c in enumerate(D["audit"]["checks"]):
        match = re.match(r"(BH#\d+):", c["label"])
        name = match[1] if match else None
        refs = [data, source("tools/audit/audit.py")]
        facts = [("Check ID", c["id"]), ("Measured outcome", "Passed" if c["ok"] else "Failed")]
        add(f"check:{i}", c["label"], "Ledger-only scenario audit · crowd run", "A recorded audit assertion, not a proof that every possible attack was tested.", facts, refs)
        if name:
            evidence[f"check:{i}"]["related"] = f"holder:{name}"

    for stage, references in RULES.items():
        add(f"rule:{stage}", "Textbook reference", "Source for the left-hand rule", "The left side describes the cited market convention. The demonstration uses the sample bond's declared terms.", refs=[ref(*r) for r in references] + [source("docs/tradfi-vs-onchain.md"), source("docs/bond-mechanics.md")])

    first_pay = next(e for e in events if e["kind"] == "coupon" and e["coupon"] == 0 and "BH#1" in e["moves"])
    first_trade = next(e for e in events if e["kind"] == "trade" and e["t"] >= D["records"][0]["chainTime"])
    created = next(s for s in D["servicing"] if "CreateBond" in s["instructions"])
    c0 = [e for e in events if e["kind"] == "coupon" and e["coupon"] == 0]
    opened = next(s for s in D["servicing"] if s.get("opened") and s["opened"]["event"] == 0)
    funding = [s for s in D["servicing"] if opened["t"] <= s["t"] <= min(e["t"] for e in c0) and (s["funding"] or "ConfirmFunding" in s["instructions"])]
    common = [data, facts_data]
    add("stage:issue", "Issued quantity and registry", "Crowd run · devnet", "Placed quantity is the sum of measured placement movements; the list contains the 1,000 registered sample accounts.",
        [("Bond mint", D["bondMint"]), ("Bond authority / account", D["bondAuthority"]), ("Holders", len(D["holders"]))],
        [ref("Bond mint", D["bondMintLink"]), ref("Bond account", address(D["bondAuthority"])), ref("Create-bond transaction", tx(created["signature"]))] + common + [source("programs/bond_servicing/src/instructions/create_bond.rs")])
    add("stage:trade", "Transfers after record boundaries", "Crowd run · devnet", "Each point is a measured securities transfer. It does not demonstrate a delivery-versus-payment cash trade leg.",
        [("Time scale", f'1 bond day = {D["scale"]} chain seconds')], common + [source("programs/bond_servicing/src/instructions/transfer_hook.rs")])
    add("stage:record", "Record-time entitlement", "Crowd run · devnet", "Compare the first post-record sale's token balances and the seller's coupon transaction against the record grid.",
        [("Record boundary (chain time)", D["records"][0]["chainTime"])], [ref("Sale after record", first_trade["link"]), ref("BH#1 coupon 0", first_pay["link"])] + common + [source("programs/bond_servicing/src/state.rs", "pub struct PositionHistory")])
    add("stage:calculate", "Coupon arithmetic and measured cash", "Crowd run · devnet", "The expected amounts and measured payments are compared per holder and coupon. Audit results apply to these recorded scenarios.",
        [("Face (base units)", scenario["terms"]["faceUnits"]), ("Rate (bps)", scenario["terms"]["rateBps"]), ("Payment precision (token decimals)", scenario["terms"]["cashDecimals"])],
        [ref("BH#1 coupon 0 payment", first_pay["link"])] + common + [source("crates/bond_math/src/amount.rs", "pub fn coupon_amount"), source("tools/reference/check_report.py")])
    add("stage:fund", "Coupon 0 funding trail", "Crowd run · devnet", "The short-funding refusal is from the separate hostile run. These successful funding references belong to the crowd run.",
        [("Coupon 0 vault", opened["opened"]["vault"])], [ref("Event vault", address(opened["opened"]["vault"])), ref("Open event", tx(opened["signature"]))] + [ref("Deposit" if s["funding"] else "Confirm backing", tx(s["signature"])) for s in funding] + common + [source("programs/bond_servicing/src/instructions/confirm_funding.rs")])
    add("stage:pay", "Coupon payout batches", "Crowd run · devnet", "Each square represents one holder's coupon 0 payment. Hover or focus the square to inspect its batch transaction and record quantity.",
        [("Payment transactions", len(c0)), ("Recipients", sum(len(e["moves"]) for e in c0))], common + [ref("Keeper-run measurements", "data/devnet-keeper.json"), source("programs/bond_servicing/src/instructions/pay_coupon.rs")])
    red = next(e for e in events if e["kind"] == "redemption" and "BH#1" in e["moves"])
    add("stage:redeem", "Principal and securities in one transaction", "Crowd run · devnet", "Inspect both the permissioned burn instruction and the cash movement in the same transaction. Coupons already earned stay payable separately.",
        [("BH#1 bonds retired", -red["moves"]["BH#1"]["bonds"]), ("BH#1 principal", red["moves"]["BH#1"]["cash"])],
        [ref("BH#1 redemption", red["link"]), ref("Bond mint / current supply", D["bondMintLink"])] + common + [source("programs/bond_servicing/src/instructions/redeem_holder.rs")])
    offer_refs = [ref("Measured buyback/tender settlements", "audit/evidence.json"), data, source("programs/bond_servicing/src/instructions/offer.rs")]
    add("stage:offer", "Tender commitments and allocations", "Crowd run · devnet", "Tendered quantities come from the recorded scenario instructions; accepted quantities are measured retirement movements. Tenders are resolved in the order they were made, each taking the pro-rata share of the running total minus what the earlier ones took, so an oversubscribed offer buys exactly its maximum.", refs=offer_refs)
    tender_spec = next(o for o in scenario["offers"] if o["kind"] == "tender")
    commitments = {}
    for action in tender_spec["actions"]:
        name = action["holder"]
        commitments[name] = commitments.get(name, 0) + action.get("tender", 0) - action.get("withdraw", 0)
    settled = {s["signature"] for s in D["servicing"] if "SettleTender" in s["instructions"]}
    for name, quantity in commitments.items():
        matches = [e for e in events if e["signature"] in settled and name in e["moves"] and e["moves"][name]["bonds"] < 0]
        add(f"offer:{name}", f"Tender · {name}", "Scenario commitments + measured devnet retirement", "Light bars are the net election in the scenario input; dark bars are the bonds actually retired by tender settlement.",
            [("Net tendered (scenario)", quantity), ("Accepted (ledger)", sum(-e["moves"][name]["bonds"] for e in matches))],
            [ref("Holder wallet", holders[name]["link"])] + [ref("Tender settlement transaction", e["link"]) for e in matches] + [data, source(D["scenarioFile"]), source("programs/bond_servicing/src/instructions/offer.rs", "pub fn handle_settle_tender")])
    add("stage:audit", "Audit outcomes and a separate local reconciliation probe", "Crowd run + local test · explicitly different networks", "The extra 0.000001 was a real unsolicited deposit on localnet. It detects a discrepancy against the scenario's expected funding, not theft or a devnet exploit.",
        [("Local transaction (no public explorer)", probe["signature"]), ("Local vault", probe["vault"]), ("Local network", probe["network"]), ("Failed checks", "; ".join(probe["after"]["failed"]))],
        [data, ref("Local probe before/after results", "data/tamper-probe-local.json"), host_data, ref("Raw finalized hostile transactions", "data/stages-probe-ledger.json"), source("tools/audit/audit.py")])
    add("keeper", "Outside keeper, no payout redirection", "Keeper run · devnet", "An unrelated wallet pays known beneficiaries. Being able to submit the transaction does not make the payer its beneficiary.",
        [("Keeper wallet", K["keeperAddress"])], [ref("Keeper wallet", address(K["keeperAddress"])), ref("Keeper-run measurements", "data/devnet-keeper.json")])
    keeper_signatures = {s["signature"] for s in K["servicing"] if s["feePayer"] == K["keeperAddress"]}
    evidence["keeper"]["refs"] += [ref(f'Keeper {e["kind"]} · coupon {e["coupon"]}', e["link"]) for e in K["events"] if e["signature"] in keeper_signatures and e["kind"] in ("coupon", "redemption")]
    evidence["keeper"]["refs"] += [ref("Successful hostile-wallet servicing", "audit/review.html#permitted")]
    add("program", "Servicing program", "Solana devnet", "The program deployed on devnet, with its metadata and upgrade authority. Its bytes equal a build of these sources (sha256 327271f3b6f3c5e2…), and every run on this page used it.",
        refs=[ref("Program in Explorer", D["programLink"]), source("programs/bond_servicing/src/lib.rs")])

    for file in ("tools/demo/stages_audit.py", "tools/demo/build_stages.py", "tools/demo/stages_template.html", "tools/demo/check_stages.cjs"):
        source(file)
    evidence["stage:audit"]["refs"] += [ref("Probe mechanisms and assessment", "audit/review.html")]
    rows = []
    for a in H["attempts"]:
        e = evidence[f'attack:{a["id"]}']
        constructor = e["refs"][-2]["href"].removeprefix("audit/")
        group, mechanism, limit = ATTACKS[a["id"]]
        rows.append(f'<tr id="{escape(a["id"])}"><td><b>{escape(a["id"])}</b><br>{escape(group)}</td>'
                    f'<td>{escape(mechanism)}<p>{escape(limit)}</p></td><td>{escape(a["reason"])}<br>'
                    f'<a href="{escape(a["explorer"])}" target="_blank" rel="noopener">Finalized transaction ↗</a><br>'
                    f'<a href="{escape(constructor)}">Instruction constructor ↗</a></td></tr>')
    # The review page states these counts; each one is computed here, and the zero to the attacker is checked.
    if any(s["cashToAttacker"] or s["bondsToAttacker"] for s in H["serviced"]):
        raise ValueError("The hostile wallet received cash or bonds in a recorded operation")
    if any(not re.fullmatch(r"BH#\d+", name) for s in H["serviced"] for name in s["cashTo"]):
        raise ValueError("A recorded operation paid cash to a wallet that is not a bond holder")
    paying = sum(1 for s in H["serviced"] if s["cashTo"])
    serviced_rows = "".join(f'<tr><td>{escape(s["step"])}</td><td>{escape(", ".join(s["cashTo"]) or "No positive cash recipient")}</td>'
                            f'<td>{s["cashToAttacker"]} cash / {s["bondsToAttacker"]} bonds</td>'
                            f'<td><a href="{escape(s["explorer"])}" target="_blank" rel="noopener">Transaction ↗</a></td></tr>' for s in H["serviced"])
    (audit_dir / "review.html").write_text(chrome(f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bond demonstration · auditor review</title><style>
:root{{color-scheme:light dark}}body{{margin:24px auto;padding:0 16px;max-width:1100px;font:15px/1.6 system-ui;background:light-dark(#f4f5f2,#0f1011);color:light-dark(#0b0b0b,#eee)}}
h1{{font-size:28px}}a{{color:light-dark(#1c5cab,#6da7ec)}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #7775;padding:12px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}td:first-child{{min-width:140px}}td p{{color:light-dark(#52514e,#c3c2b7)}}.table{{overflow:auto}}tr:target{{background:#1c5cab22}}
</style><a href="../stages.html">← Bond stages</a><h1>What the {len(H["attempts"])} probes actually demonstrate</h1>
<p>All {len(H["attempts"])} listed transactions were fetched from public devnet RPC at finalized commitment. Each failed, its recorded guard matched the execution logs, and its pre/post token balances were unchanged. Failed transactions still paid fees. Verification: {escape(ledger["verifiedAt"])}.</p>
<p>The harness sends real transactions with <code>skipPreflight: true</code>. It calls a test passed when the transaction failed and its reason contains the expected guard token. Naming a PDA while clearing its signer flag tests missing authorization; it does not forge a signature. Some probes use cooperating authorized holders, the registrar or the bank.</p>
<p>The custom checks are meaningful integration evidence for this bond profile. Standard token and loader checks are authority regression tests. The frozen cash account is controlled recovery testing. The separate localnet deposit probe detects a reconciliation discrepancy. These results cover the supplied transactions and scenarios; they do not establish an exhaustive security proof, million-holder throughput, or atomic securities-plus-cash trade settlement.</p>
<p><a href="../data/stages-probe-ledger.json">Raw finalized RPC responses</a> · <a href="../data/devnet-hostile-attempts.json">All measured hostile-run data</a> · <a href="https://solana.com/docs/core/transactions">Solana transaction semantics</a> · <a href="https://solana.com/docs/core/fees">Solana fees</a></p>
<div class="table"><table><thead><tr><th>Probe and class</th><th>Mechanism and evidential limit</th><th>Observed rejection / inspect</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<h2 id="permitted">Successful hostile-wallet operations: permitted servicing</h2>
<p>The measurement enumerates successful operator-recorded transactions whose fee payer is the hostile wallet. These {len(H["serviced"])} recorded operations include account setup and permissionless event opening, funding confirmation and payments. {paying} have positive cash recipients, all bond holders. The measured positive cash/bond movements to the hostile wallet are zero. This is not a claim about unrecorded wallet history.</p>
<div class="table"><table><thead><tr><th>Operation</th><th>Cash recipients</th><th>Positive movement to hostile wallet (base units)</th><th>Inspect</th></tr></thead><tbody>{serviced_rows}</tbody></table></div>
<p>Source pages are snapshots, with hashes and line links, of the sources the pages were built from; the deployed program is a build of these sources (sha256 327271f3b6f3c5e2…).</p></html>''', "Review", "Review", "../"))

    for i, e in enumerate(c0):
        for name, movement in e["moves"].items():
            key = f"paid:{name}"
            tx_key = f'tx:{e["signature"]}'
            base = evidence[tx_key]
            add(key, f"Coupon 0 · {name}", base["scope"], "The holder is one recipient in this transaction; compare its record quantity with the measured cash delta.",
                [("Record quantity", D["grid"][0]["atRecord"][name]), ("Expected cash", D["grid"][0]["expected"][name]), ("Measured cash", movement["cash"])],
                [base["refs"][0], ref("Holder wallet", holders[name]["link"])])
            evidence[key]["related"] = tx_key

    metadata = {"sourceRevision": revision, "sources": source_files, "evidence": evidence,
                "probes": H["attempts"], "checks": D["audit"]["checks"], "holdings": D["grid"],
                "holders": D["holders"], "scope": "Recorded scenarios and current source snapshots; not a general security audit."}
    (audit_dir / "evidence.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=1) + "\n")
    return evidence, c0, source_files
