//! Generated tests: random sequences of every operation on a bond with random terms, by every party, with valid and
//! invalid quantities and times. Nobody picks the cases: proptest generates them, checks the invariants after every
//! step and, when one breaks, shrinks the sequence to the shortest one that still breaks it.
//!
//! The invariants are measured from Token-2022 balances and the clock, not taken from the program's bookkeeping:
//!   - positions: a holder's position is its bond balance plus the bonds in its open DvP escrows; the program's
//!     record agrees; the supply is the sum of the positions and never exceeds the issue size; a wallet the
//!     registrar never registered holds nothing;
//!   - record moments: the position the program keeps for every passed record moment is the one measured when the
//!     clock passed it;
//!   - payments: a coupon pays the coupon on the position at its record moment, once; a redemption, a buyback and a
//!     tender pay for exactly the bonds they burn; an event's budget is the amount owed on the supply at its record
//!     moment;
//!   - authority: an operation signed by someone other than the registrar or the holder fails;
//!   - maturity: from the final record moment on, no position grows;
//!   - nothing stays locked: at the end every party acts in good faith (open trades cancelled, tenders resolved and
//!     settled, every event funded); then every coupon is paid, every bond is redeemed, and no escrow or tender
//!     holds anything.
//!
//! Amounts are computed with bond_math, whose formulas the QuantLib/FinancePy check verifies separately.
//! Cases: `PROPTEST_CASES` (default 256). A failing case is saved in `random_operations.proptest-regressions` and replayed
//! first.
//! Each invariant has an id; `KNOWN_BREAKS=id1,id2` sets aside the cases that break those, to look for other breaks.

#![allow(dead_code)]

include!("common/harness.rs");

use bond_math::amount::{fixed_coupon_amount, principal_amount};
use proptest::prelude::*;
use proptest::strategy::Union;

const HOLDERS: [&str; 4] = ["H0", "H1", "H2", "H3"];
/// A wallet with token accounts for both tokens that the registrar never registered.
const OUTSIDER: &str = "X";

// ---------- what is generated ----------

/// Bond terms: any fixed-rate bond within the program's limits.
#[derive(Debug, Clone)]
struct Terms {
    periods_per_year: u32,
    years: i32,
    rate_bps: u32,
    face_units: u64,
    redemption_price_bps: u32,
    record_days_before_payment: i64,
    max_supply: u64,
}

fn terms() -> impl Strategy<Value = Terms> {
    (
        prop::sample::select(vec![1u32, 2, 4, 12]),
        1i32..=10,
        1u32..=3_000,
        prop::sample::select(vec![USD, 1_000 * USD, 100_000 * USD]),
        prop::sample::select(vec![9_950u32, 10_000, 10_100]),
        0i64..=2,
        prop::sample::select(vec![15u64, 1_000, 1_000_000]),
    )
        .prop_filter("the program serves at most 40 coupon periods", |(periods_per_year, years, ..)| periods_per_year * (*years as u32) <= 40)
        .prop_map(|(periods_per_year, years, rate_bps, face_units, redemption_price_bps, record_days_before_payment, max_supply)| Terms {
            periods_per_year,
            years,
            rate_bps,
            face_units,
            redemption_price_bps,
            record_days_before_payment,
            max_supply,
        })
}

/// A quantity relative to what the actor has ("all" = its whole free balance, or the rest of the issue size).
#[derive(Debug, Clone, Copy)]
enum Qty {
    Zero,
    One,
    Some(u64),
    All,
    AllPlusOne,
    Max,
}

impl Qty {
    fn of(self, all: u64) -> u64 {
        match self {
            Qty::Zero => 0,
            Qty::One => 1,
            Qty::Some(n) => n,
            Qty::All => all,
            Qty::AllPlusOne => all.saturating_add(1),
            Qty::Max => u64::MAX,
        }
    }
}

fn qty() -> impl Strategy<Value = Qty> {
    prop_oneof![
        1 => Just(Qty::Zero),
        2 => Just(Qty::One),
        4 => (2u64..=20).prop_map(Qty::Some),
        3 => Just(Qty::All),
        1 => Just(Qty::AllPlusOne),
        1 => Just(Qty::Max),
    ]
}

/// How much cash the bank puts into a vault, against what it must hold.
#[derive(Debug, Clone, Copy)]
enum Funding {
    Exact,
    Short,
    Over,
}

fn funding() -> impl Strategy<Value = Funding> {
    prop_oneof![3 => Just(Funding::Exact), 1 => Just(Funding::Short), 1 => Just(Funding::Over)]
}

fn imposter() -> impl Strategy<Value = bool> {
    prop::bool::weighted(0.1)
}

/// One operation. Holders, trades, offers and events are indices taken modulo what exists when the step runs.
#[derive(Debug, Clone)]
enum Op {
    /// The registrar places bonds (`imposter`: someone else signs as the registrar).
    Place { h: usize, qty: Qty, imposter: bool },
    /// A holder transfers bonds; `to` 4 is the unregistered wallet.
    Transfer { from: usize, to: usize, qty: Qty },
    /// The bank gives a holder cash (to pay for trades).
    GiveCash { h: usize, amount: u64 },
    /// The registrar creates a DvP trade and admits it.
    TradeOpen { seller: usize, buyer: usize, qty: Qty, cash: u64, expiry_days: i64, imposter: bool },
    TradeFundBonds { t: usize, qty: Qty },
    TradeFundCash { t: usize },
    TradeSettle { t: usize },
    TradeCancel { t: usize },
    /// A party rejects the trade (both legs back) or reclaims its own leg (the trade stays open).
    TradeReject { t: usize, by_seller: bool },
    TradeReclaim { t: usize, by_seller: bool },
    /// The clock moves forward to one of the bond's moments (record, payment, offer window, trade expiry) plus
    /// `offset` seconds, or by 30 days when none is ahead.
    Advance { moment: usize, offset: i64 },
    OpenEvent { e: usize },
    FundEvent { e: usize, funding: Funding },
    ConfirmFunding { e: usize },
    PayCoupon { e: usize, h: usize },
    Redeem { h: usize },
    /// The registrar creates a buyback (first come) or a tender offer (pro rata).
    OfferCreate { pro_rata: bool, price: u64, max: Qty, opens_in_days: i64, open_days: i64, imposter: bool },
    OfferFund { o: usize, funding: Funding },
    OfferConfirm { o: usize },
    Sell { o: usize, h: usize, qty: Qty, imposter: bool },
    Tender { o: usize, h: usize, qty: Qty, withdraw: bool, imposter: bool },
    ResolveTender { o: usize, h: usize },
    SettleTender { o: usize, h: usize },
    /// Composite steps that reach deep states quickly: an event opened, funded, confirmed and paid to every holder;
    /// a trade created with both legs funded; an offer created, funded and confirmed.
    CouponRound { e: usize, funding: Funding },
    /// `settle`: the registrar settles it at once.
    TradeFull { seller: usize, buyer: usize, qty: Qty, cash: u64, expiry_days: i64, settle: bool },
    /// `takers`: what each holder sells (buyback) or tenders (tender offer) at once, if the window is open.
    OfferReady { pro_rata: bool, price: u64, max: Qty, opens_in_days: i64, open_days: i64, takers: [Option<Qty>; 4] },
}

fn op() -> impl Strategy<Value = Op> {
    let h = || 0usize..4;
    let i = || 0usize..8;
    let price = || prop::sample::select(vec![1u64, 990 * USD, 1_000 * USD, 1_010 * USD]);
    Union::new_weighted(vec![
        (3, (h(), qty(), imposter()).prop_map(|(h, qty, imposter)| Op::Place { h, qty, imposter }).boxed()),
        (4, (h(), 0usize..5, qty()).prop_map(|(from, to, qty)| Op::Transfer { from, to, qty }).boxed()),
        (2, (h(), prop::sample::select(vec![1u64, 1_000 * USD, 100_000 * USD])).prop_map(|(h, amount)| Op::GiveCash { h, amount }).boxed()),
        (
            3,
            (h(), h(), qty(), price(), prop::sample::select(vec![-1i64, 1, 30, 400, 2_000]), imposter())
                .prop_map(|(seller, buyer, qty, cash, expiry_days, imposter)| Op::TradeOpen { seller, buyer, qty, cash, expiry_days, imposter })
                .boxed(),
        ),
        (3, (i(), qty()).prop_map(|(t, qty)| Op::TradeFundBonds { t, qty }).boxed()),
        (2, i().prop_map(|t| Op::TradeFundCash { t }).boxed()),
        (2, i().prop_map(|t| Op::TradeSettle { t }).boxed()),
        (1, i().prop_map(|t| Op::TradeCancel { t }).boxed()),
        (1, (i(), any::<bool>()).prop_map(|(t, by_seller)| Op::TradeReject { t, by_seller }).boxed()),
        (1, (i(), any::<bool>()).prop_map(|(t, by_seller)| Op::TradeReclaim { t, by_seller }).boxed()),
        (4, (0usize..64, prop::sample::select(vec![-1i64, 0, 1, 60, 8 * 3_600])).prop_map(|(moment, offset)| Op::Advance { moment, offset }).boxed()),
        (2, (0usize..40).prop_map(|e| Op::OpenEvent { e }).boxed()),
        (2, (0usize..40, funding()).prop_map(|(e, funding)| Op::FundEvent { e, funding }).boxed()),
        (2, (0usize..40).prop_map(|e| Op::ConfirmFunding { e }).boxed()),
        (3, (0usize..40, h()).prop_map(|(e, h)| Op::PayCoupon { e, h }).boxed()),
        (2, h().prop_map(|h| Op::Redeem { h }).boxed()),
        (
            1,
            (any::<bool>(), price(), qty(), prop::sample::select(vec![0i64, 1, 10]), prop::sample::select(vec![1i64, 5, 30]), imposter())
                .prop_map(|(pro_rata, price, max, opens_in_days, open_days, imposter)| Op::OfferCreate { pro_rata, price, max, opens_in_days, open_days, imposter })
                .boxed(),
        ),
        (1, (i(), funding()).prop_map(|(o, funding)| Op::OfferFund { o, funding }).boxed()),
        (1, i().prop_map(|o| Op::OfferConfirm { o }).boxed()),
        (1, (i(), h(), qty(), imposter()).prop_map(|(o, h, qty, imposter)| Op::Sell { o, h, qty, imposter }).boxed()),
        (
            2,
            (i(), h(), qty(), any::<bool>(), imposter())
                .prop_map(|(o, h, qty, withdraw, imposter)| Op::Tender { o, h, qty, withdraw, imposter })
                .boxed(),
        ),
        (1, (i(), h()).prop_map(|(o, h)| Op::ResolveTender { o, h }).boxed()),
        (1, (i(), h()).prop_map(|(o, h)| Op::SettleTender { o, h }).boxed()),
        (3, (0usize..40, funding()).prop_map(|(e, funding)| Op::CouponRound { e, funding }).boxed()),
        (
            3,
            (h(), h(), qty(), price(), prop::sample::select(vec![1i64, 30, 364]), any::<bool>())
                .prop_map(|(seller, buyer, qty, cash, expiry_days, settle)| Op::TradeFull { seller, buyer, qty, cash, expiry_days, settle })
                .boxed(),
        ),
        (
            2,
            (
                any::<bool>(),
                price(),
                prop_oneof![3 => Just(Qty::All), 2 => (1u64..=20).prop_map(Qty::Some), 1 => qty()],
                prop::sample::select(vec![0i64, 0, 0, 1]),
                prop::sample::select(vec![1i64, 5, 30]),
                [prop::option::of(qty()), prop::option::of(qty()), prop::option::of(qty()), prop::option::of(qty())],
            )
                .prop_map(|(pro_rata, price, max, opens_in_days, open_days, takers)| Op::OfferReady { pro_rata, price, max, opens_in_days, open_days, takers })
                .boxed(),
        ),
    ])
}

// ---------- the run and its measurements ----------

struct Trade {
    seller: usize,
    buyer: usize,
    swap: Pubkey,
    cash: u64,
    expiry: i64,
}

struct Offer {
    id: u16,
    pro_rata: bool,
    price: u64,
    max: u64,
    open: i64,
    close: i64,
    /// Bonds bought so far, measured as burned.
    bought: u64,
}

struct Run {
    env: Env,
    terms: Terms,
    imposter: Keypair,
    /// Positions measured when the clock passed each record moment (index = coupon period).
    at_record: Vec<Option<[u64; 4]>>,
    coupon_paid: Vec<[bool; 4]>,
    redeemed: [u64; 4],
    trades: Vec<Trade>,
    offers: Vec<Offer>,
    nonce: u64,
    step: String,
    /// Why the step's last refused transaction was refused (for the tally).
    last_refusal: Option<String>,
}

impl Run {
    fn new(terms: Terms) -> Run {
        let spec = terms.spec();
        let mut env = setup_bond(&spec);
        for name in HOLDERS {
            env.register(name);
        }
        let outsider = Keypair::new();
        env.svm.airdrop(&outsider.pubkey(), 1_000_000_000).unwrap();
        let atas = [
            spl_associated_token_account_interface::instruction::create_associated_token_account(&outsider.pubkey(), &outsider.pubkey(), &env.mint, &token_2022()),
            spl_associated_token_account_interface::instruction::create_associated_token_account(&outsider.pubkey(), &outsider.pubkey(), &env.cash_mint, &env.cash_program),
        ];
        env.send(&atas, &[&outsider]).unwrap();
        env.holders.insert(OUTSIDER, outsider);
        let imposter = Keypair::new();
        env.svm.airdrop(&imposter.pubkey(), 10_000_000_000).unwrap();
        let periods = env.periods() as usize;
        Run {
            env,
            terms,
            imposter,
            at_record: vec![None; periods],
            coupon_paid: vec![[false; 4]; periods],
            redeemed: [0; 4],
            trades: vec![],
            offers: vec![],
            nonce: 0,
            step: "setup".into(),
            last_refusal: None,
        }
    }

    /// A broken invariant. `id` names it; a case breaking one listed in `KNOWN_BREAKS` is set aside (`KnownBreak`).
    fn fail(&self, id: &'static str, what: String) -> ! {
        if known_breaks().iter().any(|k| k == id) {
            std::panic::panic_any(KnownBreak(id));
        }
        panic!("invariant {id} broken at {}: {what}", self.step);
    }

    fn periods(&self) -> usize {
        self.env.periods() as usize
    }

    fn final_record_moment(&self) -> i64 {
        self.env.record_moment(self.periods() - 1)
    }

    fn coupon(&self, position: u64) -> u64 {
        fixed_coupon_amount(position, self.terms.face_units, self.terms.rate_bps, self.terms.periods_per_year).unwrap()
    }

    fn principal(&self, position: u64) -> u64 {
        principal_amount(position, self.terms.face_units, self.terms.redemption_price_bps).unwrap()
    }

    fn escrow_bonds(&self, h: usize) -> u64 {
        self.trades.iter().filter(|t| t.seller == h).map(|t| self.env.balance(&ata(&t.swap, &self.env.mint, &token_2022()))).sum()
    }

    fn escrow_cash(&self, t: &Trade) -> u64 {
        self.env.balance(&ata(&t.swap, &self.env.cash_mint, &self.env.cash_program))
    }

    /// Measured position: bond balance plus the bonds in the holder's escrows.
    fn positions(&self) -> [u64; 4] {
        std::array::from_fn(|h| self.env.bonds_of(HOLDERS[h]) + self.escrow_bonds(h))
    }

    /// Event for a generated index: mostly one whose record moment has passed and that is already open (`open`
    /// true) or not yet (`open` false), not the redemption when `coupons_only`; otherwise any index, which the
    /// program must refuse while its record moment is ahead.
    fn event_for(&self, e: usize, coupons_only: bool, open: bool) -> u16 {
        let last = if coupons_only { self.periods() } else { self.periods() + 1 };
        let fits: Vec<usize> = (0..last)
            .filter(|&k| self.env.record_moment(k.min(self.periods() - 1)) <= self.env.now() && self.event_exists(k as u16) == open)
            .collect();
        if fits.is_empty() || e >= 32 {
            (e % last) as u16
        } else {
            fits[e % fits.len()] as u16
        }
    }

    /// Offer for a generated index: mostly one of the kind the step needs.
    fn offer_for(&self, o: usize, pro_rata: bool) -> Option<usize> {
        let fits: Vec<usize> = (0..self.offers.len()).filter(|&k| self.offers[k].pro_rata == pro_rata).collect();
        if fits.is_empty() || o >= 6 {
            self.offer_index(o)
        } else {
            Some(fits[o % fits.len()])
        }
    }

    /// Whether a transaction landed; a refusal is kept for the tally.
    fn ok<T>(&mut self, result: Result<T, String>) -> bool {
        if let Err(err) = &result {
            self.last_refusal = Some(refusal_of(err));
        }
        result.is_ok()
    }

    fn event_exists(&self, e: u16) -> bool {
        self.env.svm.get_account(&self.env.event_pda(e)).is_some()
    }

    /// The bank's cash for one generated step: at most 2^57 units (above the largest budget of a generated bond), so
    /// 80 steps plus the end's budgets never exhaust the cash token's u64 supply.
    fn step_cash(&mut self, to: &Pubkey, amount: u64) -> Option<bool> {
        (amount <= 1 << 57).then(|| self.mint_cash(to, amount).is_ok())
    }

    fn mint_cash(&mut self, to: &Pubkey, amount: u64) -> Result<Vec<String>, String> {
        let ix = spl_token_2022_interface::instruction::mint_to(
            &self.env.cash_program, &self.env.cash_mint, to, &self.env.cash_authority.pubkey(), &[], amount,
        )
        .unwrap();
        let bank = self.env.cash_authority.insecure_clone();
        self.env.send(&[ix], &[&bank])
    }

    /// Moves the clock forward, measuring positions at every record moment it passes.
    fn advance_to(&mut self, to: i64) {
        let now = self.env.now();
        if to <= now {
            return;
        }
        let positions = self.positions();
        for k in 0..self.periods() {
            let moment = self.env.record_moment(k);
            if now < moment && moment <= to {
                self.at_record[k] = Some(positions);
            }
        }
        self.env.set_time(to);
    }

    /// The moments ahead where the rules change: record and payment moments, offer windows, trade expiries.
    fn moments_ahead(&self) -> Vec<i64> {
        let now = self.env.now();
        let mut moments: Vec<i64> = (0..self.periods()).flat_map(|k| [self.env.record_moment(k), self.env.payment_moment(k)]).collect();
        moments.extend(self.offers.iter().flat_map(|o| [o.open, o.close]));
        moments.extend(self.trades.iter().map(|t| t.expiry));
        moments.retain(|m| *m > now);
        moments.sort();
        moments.dedup();
        moments
    }

    // ---------- invariants after every step ----------

    fn check(&self, before: [u64; 4]) {
        let env = &self.env;
        let now = env.now();
        let positions = self.positions();
        if env.bonds_of(OUTSIDER) != 0 {
            self.fail("outsider-holds-bonds", format!("an unregistered wallet holds {} bonds", env.bonds_of(OUTSIDER)));
        }
        for h in 0..4 {
            let r = env.record(HOLDERS[h]);
            if r.position.quantity != positions[h] {
                self.fail("position", format!("H{h}: program position {} vs measured {}", r.position.quantity, positions[h]));
            }
            if r.escrowed != self.escrow_bonds(h) {
                self.fail("escrowed", format!("H{h}: program escrowed {} vs {} in its escrows", r.escrowed, self.escrow_bonds(h)));
            }
            if env.bonds_of(HOLDERS[h]) < r.locked {
                self.fail("locked-left-account", format!("H{h}: {} bonds in the account, {} locked in tenders", env.bonds_of(HOLDERS[h]), r.locked));
            }
            for (k, at) in self.at_record.iter().enumerate() {
                if let Some(at) = at {
                    if r.position.at_moment(k as u16) != at[h] {
                        self.fail("record-moment-position", format!("H{h}: record moment {k}: program {} vs measured {}", r.position.at_moment(k as u16), at[h]));
                    }
                }
            }
            if now >= self.final_record_moment() && positions[h] > before[h] {
                self.fail("position-grew-after-maturity", format!("H{h}: position grew from {} to {} after the final record moment", before[h], positions[h]));
            }
        }
        let supply = env.mint_supply();
        if supply != positions.iter().sum::<u64>() {
            self.fail("supply-vs-positions", format!("supply {supply} vs positions {positions:?}"));
        }
        if supply > self.terms.max_supply {
            self.fail("supply-above-issue-size", format!("supply {supply} above the issue size {}", self.terms.max_supply));
        }
    }

    /// Bonds a holder lost and cash it gained in one step.
    fn burned_and_paid(&self, h: usize, bonds_before: u64, cash_before: u64) -> (u64, u64) {
        let name = HOLDERS[h];
        (bonds_before - self.env.bonds_of(name), self.env.cash_of(name) - cash_before)
    }

    fn must_fail(&self, what: &str, result: Result<Vec<String>, String>) -> Option<bool> {
        if result.is_ok() {
            self.fail("unauthorised", format!("{what} succeeded"));
        }
        Some(false)
    }

    // ---------- steps ----------

    fn apply(&mut self, op: &Op) {
        let before = self.positions();
        let supply_before = self.env.mint_supply();
        // Whether the step's transaction landed (None: nothing to act on, e.g. no trade yet).
        let landed: Option<bool> = match *op {
            Op::Place { h, qty, imposter } => {
                let amount = qty.of(self.terms.max_supply.saturating_sub(supply_before));
                if imposter {
                    let ix = self.place_ix(HOLDERS[h], amount, self.imposter.pubkey());
                    let signer = self.imposter.insecure_clone();
                    let result = self.env.send(&[ix], &[&signer]);
                    self.must_fail("placement signed by a non-registrar", result)
                } else {
                    let ok = { let r = self.env.place(HOLDERS[h], amount); self.ok(r) };
                    if ok && self.env.now() >= self.env.record_moment(0) {
                        self.fail("placement-after-first-record-moment", "placement after the first record moment".into());
                    }
                    Some(ok)
                }
            }
            Op::Transfer { from, to, qty } => {
                let to = if to == 4 { OUTSIDER } else { HOLDERS[to] };
                let amount = qty.of(self.env.bonds_of(HOLDERS[from]));
                Some({ let r = self.env.transfer(HOLDERS[from], to, amount); self.ok(r) })
            }
            Op::GiveCash { h, amount } => {
                let to = ata(&self.env.holder(HOLDERS[h]), &self.env.cash_mint, &self.env.cash_program);
                self.step_cash(&to, amount)
            }
            Op::TradeOpen { seller, buyer, qty, cash, expiry_days, imposter } => {
                let quantity = qty.of(self.env.bonds_of(HOLDERS[seller]));
                let expiry = self.env.now() + expiry_days * DAY;
                self.nonce += 1;
                if imposter {
                    let (a, b) = (self.env.holder(HOLDERS[seller]), self.env.holder(HOLDERS[buyer]));
                    let (_, ixs) = self.env.dvp_create_and_admit_ixs(&a, &b, self.nonce, quantity, cash, expiry, &self.imposter.pubkey());
                    let (registrar, signer) = (self.env.registrar.insecure_clone(), self.imposter.insecure_clone());
                    let result = self.env.send(&ixs, &[&registrar, &signer]);
                    self.must_fail("trade admitted by a non-registrar", result)
                } else {
                    let swap = self.env.dvp_create_and_admit_until(HOLDERS[seller], HOLDERS[buyer], self.nonce, quantity, cash, expiry);
                    if let Ok(swap) = swap {
                        self.trades.push(Trade { seller, buyer, swap, cash, expiry });
                    }
                    Some(self.ok(swap))
                }
            }
            Op::TradeFundBonds { t, qty } => self.trade(t).map(|x| (x.seller, x.swap)).map(|(seller, swap)| {
                let amount = qty.of(self.env.bonds_of(HOLDERS[seller]));
                { let r = self.env.dvp_fund_bonds(HOLDERS[seller], &swap, amount); self.ok(r) }
            }),
            Op::TradeFundCash { t } => self
                .trade(t)
                .map(|x| (x.buyer, x.swap, x.cash))
                .map(|(buyer, swap, cash)| { let r = self.env.dvp_fund_cash(HOLDERS[buyer], &swap, cash); self.ok(r) }),
            Op::TradeSettle { t } => self
                .trade(t)
                .map(|x| (x.seller, x.buyer, x.swap))
                .map(|(seller, buyer, swap)| { let r = self.env.dvp_settle(HOLDERS[seller], HOLDERS[buyer], &swap); self.ok(r) }),
            Op::TradeCancel { t } => self
                .trade(t)
                .map(|x| (x.seller, x.buyer, x.swap))
                .map(|(seller, buyer, swap)| { let r = self.env.dvp_cancel(HOLDERS[seller], HOLDERS[buyer], &swap); self.ok(r) }),
            Op::TradeReject { t, by_seller } => self.trade(t).map(|x| (x.seller, x.buyer, x.swap)).map(|(seller, buyer, swap)| {
                let by = if by_seller { seller } else { buyer };
                let r = self.env.dvp_reject(HOLDERS[by], HOLDERS[seller], HOLDERS[buyer], &swap);
                self.ok(r)
            }),
            Op::TradeReclaim { t, by_seller } => self.trade(t).map(|x| (x.seller, x.buyer, x.swap)).map(|(seller, buyer, swap)| {
                let by = if by_seller { seller } else { buyer };
                let r = self.env.dvp_reclaim(HOLDERS[by], &swap, by_seller);
                self.ok(r)
            }),
            Op::Advance { moment, offset } => {
                let ahead = self.moments_ahead();
                let target = if ahead.is_empty() { self.env.now() + 30 * DAY } else { ahead[moment % ahead.len()] + offset };
                let moved = target > self.env.now();
                self.advance_to(target);
                Some(moved)
            }
            Op::OpenEvent { e } => {
                let e = self.event_for(e, false, false);
                Some(self.open_event(e))
            }
            Op::FundEvent { e, funding } => {
                let e = self.event_for(e, false, true);
                if self.event_exists(e) {
                    let (budget, vault) = (self.env.event(e).budget, self.env.vault(e));
                    let amount = Self::funding_amount(funding, budget, self.env.balance(&vault));
                    self.step_cash(&vault, amount)
                } else {
                    None
                }
            }
            Op::ConfirmFunding { e } => {
                let e = self.event_for(e, false, true);
                Some({ let r = self.env.confirm_funding(e); self.ok(r) })
            }
            Op::PayCoupon { e, h } => {
                let e = self.event_for(e, true, true) as usize;
                Some(self.pay_coupon(e, h))
            }
            Op::CouponRound { e, funding } => {
                let e = self.event_for(e, false, e % 2 == 0);
                if !self.event_exists(e) && !self.open_event(e) {
                    Some(false)
                } else {
                    let (budget, vault) = (self.env.event(e).budget, self.env.vault(e));
                    let amount = Self::funding_amount(funding, budget, self.env.balance(&vault));
                    self.step_cash(&vault, amount);
                    let _ = self.env.confirm_funding(e);
                    let mut paid = false;
                    for h in 0..HOLDERS.len() {
                        paid |= if (e as usize) < self.periods() { self.pay_coupon(e as usize, h) } else { self.redeem(h) };
                    }
                    Some(paid)
                }
            }
            Op::TradeFull { seller, buyer, qty, cash, expiry_days, settle } => {
                let quantity = qty.of(self.env.bonds_of(HOLDERS[seller]));
                let expiry = self.env.now() + expiry_days * DAY;
                self.nonce += 1;
                match self.env.dvp_create_and_admit_until(HOLDERS[seller], HOLDERS[buyer], self.nonce, quantity, cash, expiry) {
                    Ok(swap) => {
                        self.trades.push(Trade { seller, buyer, swap, cash, expiry });
                        let bonds = { let r = self.env.dvp_fund_bonds(HOLDERS[seller], &swap, quantity); self.ok(r) };
                        let to = ata(&self.env.holder(HOLDERS[buyer]), &self.env.cash_mint, &self.env.cash_program);
                        self.step_cash(&to, cash);
                        let cash = { let r = self.env.dvp_fund_cash(HOLDERS[buyer], &swap, cash); self.ok(r) };
                        if settle {
                            let settled = self.env.dvp_settle(HOLDERS[seller], HOLDERS[buyer], &swap);
                            Some(self.ok(settled))
                        } else {
                            Some(bonds && cash)
                        }
                    }
                    Err(err) => Some(self.ok::<()>(Err(err))),
                }
            }
            Op::OfferReady { pro_rata, price, max, opens_in_days, open_days, takers } => {
                let id = self.offers.len() as u16;
                let max = max.of(supply_before);
                let open = self.env.now() + opens_in_days * DAY;
                let close = open + open_days * DAY;
                let created = self.env.create_offer(id, pro_rata, price, max, open, close);
                if self.ok(created) {
                    self.offers.push(Offer { id, pro_rata, price, max, open, close, bought: 0 });
                    let vault = self.env.offer_vault(id);
                    self.step_cash(&vault, price.saturating_mul(max));
                    let confirmed = self.env.confirm_offer_funding(id);
                    let mut landed = self.ok(confirmed);
                    let o = self.offers.len() - 1;
                    for (h, take) in takers.iter().enumerate() {
                        let Some(take) = take else { continue };
                        let quantity = take.of(self.env.record(HOLDERS[h]).unlocked());
                        if pro_rata {
                            let r = self.env.change_tender(id, HOLDERS[h], quantity, false);
                            landed &= self.ok(r);
                        } else {
                            let (bonds, cash, supply) = (self.env.bonds_of(HOLDERS[h]), self.env.cash_of(HOLDERS[h]), self.env.mint_supply());
                            let r = self.env.sell(id, HOLDERS[h], quantity);
                            if self.ok(r) {
                                self.bought(o, h, bonds, cash, supply, "buyback");
                            } else {
                                landed = false;
                            }
                        }
                    }
                    Some(landed)
                } else {
                    Some(false)
                }
            }
            Op::Redeem { h } => Some(self.redeem(h)),
            Op::OfferCreate { pro_rata, price, max, opens_in_days, open_days, imposter } => {
                let id = self.offers.len() as u16;
                let max = max.of(supply_before);
                let open = self.env.now() + opens_in_days * DAY;
                let close = open + open_days * DAY;
                if imposter {
                    let ix = self.create_offer_ix(id, pro_rata, price, max, open, close, self.imposter.pubkey());
                    let signer = self.imposter.insecure_clone();
                    let result = self.env.send(&[ix], &[&signer]);
                    self.must_fail("offer created by a non-registrar", result)
                } else {
                    let ok = { let r = self.env.create_offer(id, pro_rata, price, max, open, close); self.ok(r) };
                    if ok {
                        self.offers.push(Offer { id, pro_rata, price, max, open, close, bought: 0 });
                    }
                    Some(ok)
                }
            }
            Op::OfferFund { o, funding } => match self.offer(o).map(|x| (x.id, x.price, x.max)) {
                Some((id, price, max)) => {
                    let vault = self.env.offer_vault(id);
                    let amount = Self::funding_amount(funding, price.saturating_mul(max), self.env.balance(&vault));
                    self.step_cash(&vault, amount)
                }
                None => None,
            },
            Op::OfferConfirm { o } => self.offer(o).map(|x| x.id).map(|id| { let r = self.env.confirm_offer_funding(id); self.ok(r) }),
            Op::Sell { o, h, qty, imposter } => match self.offer_for(o, false) {
                Some(o) => {
                    let id = self.offers[o].id;
                    let quantity = qty.of(self.env.record(HOLDERS[h]).unlocked());
                    if imposter {
                        let ix = self.sell_ix(id, HOLDERS[h], quantity, self.imposter.pubkey());
                        let signer = self.imposter.insecure_clone();
                        let result = self.env.send(&[ix], &[&signer]);
                        self.must_fail("sale of a holder's bonds signed by someone else", result)
                    } else {
                        let (bonds, cash) = (self.env.bonds_of(HOLDERS[h]), self.env.cash_of(HOLDERS[h]));
                        let ok = { let r = self.env.sell(id, HOLDERS[h], quantity); self.ok(r) };
                        if ok {
                            self.bought(o, h, bonds, cash, supply_before, "buyback");
                        }
                        Some(ok)
                    }
                }
                None => None,
            },
            Op::Tender { o, h, qty, withdraw, imposter } => match self.offer_for(o, true).map(|o| self.offers[o].id) {
                Some(id) => {
                    let r = self.env.record(HOLDERS[h]);
                    let quantity = qty.of(if withdraw { r.locked } else { r.unlocked() });
                    if imposter {
                        let ix = self.change_tender_ix(id, HOLDERS[h], quantity, withdraw, self.imposter.pubkey());
                        let signer = self.imposter.insecure_clone();
                        let result = self.env.send(&[ix], &[&signer]);
                        self.must_fail("tender of a holder's bonds signed by someone else", result)
                    } else {
                        Some({ let r = self.env.change_tender(id, HOLDERS[h], quantity, withdraw); self.ok(r) })
                    }
                }
                None => None,
            },
            Op::ResolveTender { o, h } => self.offer_for(o, true).map(|o| self.offers[o].id).map(|id| { let r = self.env.resolve(id, HOLDERS[h]); self.ok(r) }),
            Op::SettleTender { o, h } => self.offer_for(o, true).map(|o| self.settle_tender(o, h)),
        };
        tally(op, landed, self.last_refusal.take());
        self.check(before);
    }

    fn trade(&self, t: usize) -> Option<&Trade> {
        (!self.trades.is_empty()).then(|| &self.trades[t % self.trades.len()])
    }

    fn offer_index(&self, o: usize) -> Option<usize> {
        (!self.offers.is_empty()).then(|| o % self.offers.len())
    }

    fn offer(&self, o: usize) -> Option<&Offer> {
        self.offer_index(o).map(|o| &self.offers[o])
    }

    fn funding_amount(funding: Funding, owed: u64, held: u64) -> u64 {
        let missing = owed.saturating_sub(held);
        match funding {
            Funding::Exact => missing,
            Funding::Short => missing / 2,
            Funding::Over => missing.saturating_add(1),
        }
    }

    /// Opens an event; once open, its supply and budget must be those measured at its record moment.
    fn open_event(&mut self, e: u16) -> bool {
        let result = self.env.open_event(e);
        if !self.ok(result) {
            return false;
        }
        let period = (e as usize).min(self.periods() - 1);
        let Some(at) = self.at_record[period] else {
            self.fail("event-opened-early", format!("event {e} opened before the clock passed its record moment"));
        };
        let supply: u64 = at.iter().sum();
        let event = self.env.event(e);
        let budget = if e as usize == self.periods() { self.principal(supply) } else { self.coupon(supply) };
        if event.supply_at_record != supply || event.budget != budget {
            self.fail("event-budget", format!("event {e}: supply {} budget {} vs measured supply {supply} budget {budget}", event.supply_at_record, event.budget));
        }
        true
    }

    fn pay_coupon(&mut self, e: usize, h: usize) -> bool {
        let cash = self.env.cash_of(HOLDERS[h]);
        let result = self.env.pay_coupon(e as u16, HOLDERS[h]);
        if !self.ok(result) {
            return false;
        }
        let Some(at) = self.at_record[e] else {
            self.fail("coupon-paid-early", format!("coupon {e} paid to H{h} before its record moment"));
        };
        if self.coupon_paid[e][h] {
            self.fail("coupon-paid-twice", format!("coupon {e} paid to H{h} twice"));
        }
        let paid = self.env.cash_of(HOLDERS[h]) - cash;
        if paid != self.coupon(at[h]) {
            self.fail("coupon-amount", format!("coupon {e} to H{h}: paid {paid}, owed {} on {} bonds", self.coupon(at[h]), at[h]));
        }
        self.coupon_paid[e][h] = true;
        true
    }

    fn redeem(&mut self, h: usize) -> bool {
        let (bonds, cash, supply) = (self.env.bonds_of(HOLDERS[h]), self.env.cash_of(HOLDERS[h]), self.env.mint_supply());
        let result = self.env.redeem(HOLDERS[h]);
        if !self.ok(result) {
            return false;
        }
        let (burned, paid) = self.burned_and_paid(h, bonds, cash);
        let owed = self.principal(self.redeemed[h] + burned) - self.principal(self.redeemed[h]);
        if paid != owed || supply - self.env.mint_supply() != burned {
            self.fail("redemption-amount", format!("redemption of H{h}: {burned} bonds burned, paid {paid}, owed {owed}"));
        }
        self.redeemed[h] += burned;
        true
    }

    fn settle_tender(&mut self, o: usize, h: usize) -> bool {
        let (bonds, cash, supply) = (self.env.bonds_of(HOLDERS[h]), self.env.cash_of(HOLDERS[h]), self.env.mint_supply());
        let result = self.env.settle(self.offers[o].id, HOLDERS[h]);
        if !self.ok(result) {
            return false;
        }
        self.bought(o, h, bonds, cash, supply, "tender");
        true
    }

    /// After a buyback sale or a tender settlement: paid the offer's price for exactly the bonds burned, within the
    /// offer's maximum.
    fn bought(&mut self, o: usize, h: usize, bonds: u64, cash: u64, supply: u64, what: &str) {
        let (burned, paid) = self.burned_and_paid(h, bonds, cash);
        let offer = &self.offers[o];
        if paid != offer.price * burned || supply - self.env.mint_supply() != burned || offer.bought + burned > offer.max {
            self.fail("offer-payment", format!("{what} {} from H{h}: {burned} bonds burned, paid {paid} at {} each, {} bought before, maximum {}", offer.id, offer.price, offer.bought, offer.max));
        }
        self.offers[o].bought += burned;
    }

    // ---------- the end: everyone acts in good faith, and nothing may stay locked ----------

    fn finish(&mut self) {
        self.step = "end".into();
        // Open trades are unwound by whoever can: the registrar cancels; each party rejects; each reclaims its leg.
        for t in 0..self.trades.len() {
            let (seller, buyer, swap) = (self.trades[t].seller, self.trades[t].buyer, self.trades[t].swap);
            let (a, b) = (HOLDERS[seller], HOLDERS[buyer]);
            for attempt in 0..5 {
                let trade = &self.trades[t];
                if self.env.balance(&ata(&swap, &self.env.mint, &token_2022())) == 0 && self.escrow_cash(trade) == 0 {
                    break;
                }
                let before = self.positions();
                let _ = match attempt {
                    0 => self.env.dvp_cancel(a, b, &swap),
                    1 => self.env.dvp_reject(a, a, b, &swap),
                    2 => self.env.dvp_reject(b, a, b, &swap),
                    3 => self.env.dvp_reclaim(a, &swap, true),
                    _ => self.env.dvp_reclaim(b, &swap, false),
                };
                self.check(before);
            }
        }
        // After every offer's window: tenders resolved in tender order, accepted parts bought.
        if let Some(close) = self.offers.iter().map(|o| o.close).max() {
            self.advance_to(close);
        }
        for o in 0..self.offers.len() {
            let id = self.offers[o].id;
            for _ in 0..HOLDERS.len() {
                for name in HOLDERS {
                    let _ = self.env.resolve(id, name);
                }
            }
            for h in 0..HOLDERS.len() {
                let before = self.positions();
                self.settle_tender(o, h);
                self.check(before);
            }
        }
        // Past maturity: every event opened and funded, every coupon and the principal paid.
        let redemption = self.periods() as u16;
        self.advance_to(self.env.payment_moment(self.periods() - 1).max(self.final_record_moment()) + 1);
        for e in 0..=redemption {
            if !self.event_exists(e) && !self.open_event(e) {
                self.fail("end-event-cannot-open", format!("event {e} cannot be opened after maturity"));
            }
            let event = self.env.event(e);
            if !event.funded {
                let vault = self.env.vault(e);
                let missing = event.budget.saturating_sub(self.env.balance(&vault));
                self.mint_cash(&vault, missing).unwrap();
                if let Err(err) = self.env.confirm_funding(e) {
                    self.fail("end-funding-cannot-confirm", format!("event {e} funded with its budget cannot be confirmed: {}", refusal_of(&err)));
                }
            }
        }
        for e in 0..self.periods() {
            for h in 0..HOLDERS.len() {
                let owed = self.at_record[e].map_or(0, |at| self.coupon(at[h]));
                if owed > 0 && !self.coupon_paid[e][h] && !self.pay_coupon(e, h) {
                    let err = self.env.pay_coupon(e as u16, HOLDERS[h]).unwrap_err();
                    self.fail("end-coupon-unpayable", format!("coupon {e} owed to H{h} ({owed}) cannot be paid: {}", refusal_of(&err)));
                }
            }
        }
        for h in 0..HOLDERS.len() {
            if self.env.record(HOLDERS[h]).unlocked() > 0 && !self.redeem(h) {
                let err = self.env.redeem(HOLDERS[h]).unwrap_err();
                self.fail("end-cannot-redeem", format!("H{h} cannot redeem: {}", refusal_of(&err)));
            }
        }
        // Nothing locked.
        let left: Vec<String> = (0..HOLDERS.len())
            .map(|h| (h, self.env.record(HOLDERS[h])))
            .filter(|(_, r)| r.position.quantity > 0 || r.locked > 0 || r.escrowed > 0)
            .map(|(h, r)| format!("H{h}: position {}, locked in tenders {}, in trade escrows {}", r.position.quantity, r.locked, r.escrowed))
            .collect();
        let records: Vec<HolderRecord> = HOLDERS.iter().map(|name| self.env.record(name)).collect();
        let stuck = if records.iter().any(|r| r.escrowed > 0) {
            "end-bonds-locked-in-trade-escrow"
        } else if records.iter().any(|r| r.locked > 0) {
            "end-bonds-locked-in-tender"
        } else {
            "end-bonds-not-redeemed"
        };
        if self.env.mint_supply() > 0 || !left.is_empty() {
            self.fail(stuck, format!("{} bonds never redeemed: {left:?}", self.env.mint_supply()));
        }
        for trade in &self.trades {
            if self.escrow_cash(trade) > 0 {
                self.fail("end-cash-locked-in-trade-escrow", format!("H{}'s cash {} locked in a trade escrow", trade.buyer, self.escrow_cash(trade)));
            }
        }
    }

    // ---------- instructions signed by the wrong party ----------

    fn place_ix(&self, name: &str, amount: u64, registrar: Pubkey) -> Instruction {
        let env = &self.env;
        let owner = env.holder(name);
        Instruction {
            program_id: env.program,
            accounts: bond_servicing::accounts::Place {
                registrar,
                bond: env.bond,
                mint: env.mint,
                holder: env.holder_record(&owner),
                holder_bonds: ata(&owner, &env.mint, &token_2022()),
                token_program: token_2022(),
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::Place { amount }.data(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn create_offer_ix(&self, id: u16, pro_rata: bool, price: u64, max: u64, open: i64, close: i64, registrar: Pubkey) -> Instruction {
        let env = &self.env;
        Instruction {
            program_id: env.program,
            accounts: bond_servicing::accounts::CreateOffer {
                registrar,
                bond: env.bond,
                offer: env.offer_pda(id),
                cash_mint: env.cash_mint,
                vault: env.offer_vault(id),
                cash_token_program: env.cash_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::CreateOffer { id, pro_rata, price_units: price, max_quantity: max, open_time: open, close_time: close }.data(),
        }
    }

    /// A sale of `name`'s bonds with `signer` as the seller.
    fn sell_ix(&self, id: u16, name: &str, quantity: u64, signer: Pubkey) -> Instruction {
        let env = &self.env;
        let owner = env.holder(name);
        Instruction {
            program_id: env.program,
            accounts: bond_servicing::accounts::SellToOffer { retirement: env.retirement_accounts(id, owner, signer), seller: signer, offer: env.offer_pda(id) }
                .to_account_metas(None),
            data: bond_servicing::instruction::SellToOffer { quantity }.data(),
        }
    }

    /// A tender of `name`'s bonds with `signer` as the owner.
    fn change_tender_ix(&self, id: u16, name: &str, quantity: u64, withdraw: bool, signer: Pubkey) -> Instruction {
        let env = &self.env;
        let owner = env.holder(name);
        Instruction {
            program_id: env.program,
            accounts: bond_servicing::accounts::ChangeTender {
                owner: signer,
                bond: env.bond,
                offer: env.offer_pda(id),
                holder: env.holder_record(&owner),
                tender: env.tender_pda(id, &signer),
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::ChangeTender { quantity, withdraw }.data(),
        }
    }
}

/// Operations generated and landed, by kind, over all cases, with the reasons for refusals; printed with
/// `RANDOM_OPERATIONS_TALLY=1` (and the instructions that landed with `COUNT_LANDED_INSTRUCTIONS=1`), so a passing
/// run shows what it actually exercised.
static TALLY: std::sync::Mutex<Vec<(String, u64, u64)>> = std::sync::Mutex::new(Vec::new());

fn tally(op: &Op, landed: Option<bool>, refusal: Option<String>) {
    if std::env::var("RANDOM_OPERATIONS_TALLY").is_err() {
        return;
    }
    let kind = format!("{op:?}").split([' ', '{']).next().unwrap_or("").to_string();
    let imposter = matches!(op, Op::Place { imposter: true, .. } | Op::TradeOpen { imposter: true, .. } | Op::OfferCreate { imposter: true, .. }
        | Op::Sell { imposter: true, .. } | Op::Tender { imposter: true, .. });
    let kind = if imposter { format!("{kind} (wrong signer)") } else { kind };
    let mut tally = TALLY.lock().unwrap();
    let row = match tally.iter().position(|r| r.0 == kind) {
        Some(i) => i,
        None => {
            tally.push((kind, 0, 0));
            tally.len() - 1
        }
    };
    tally[row].1 += 1;
    tally[row].2 += u64::from(landed == Some(true));
    if let (Some(false), Some(refusal)) = (landed, refusal) {
        let key = format!("{} refused: {}", tally[row].0, refusal.trim());
        match tally.iter().position(|r| r.0 == key) {
            Some(i) => tally[i].1 += 1,
            None => tally.push((key, 1, 0)),
        }
    }
}

fn print_tally() {
    if std::env::var("RANDOM_OPERATIONS_TALLY").is_err() {
        return;
    }
    let mut tally = TALLY.lock().unwrap().clone();
    tally.sort();
    eprintln!("operation: generated, landed");
    for (kind, generated, landed) in tally {
        eprintln!("{kind}: {generated}, {landed}");
    }
    let mut landed = LANDED.lock().unwrap().clone();
    landed.sort();
    eprintln!("instructions landed (all steps and the end):");
    for (name, count) in landed {
        eprintln!("  {name}: {count}");
    }
}

/// Breaks already found, as invariant ids (comma-separated in `KNOWN_BREAKS`). A case that hits one is set aside, so
/// the generator goes on looking for others. Without the variable every break fails the test.
fn known_breaks() -> Vec<String> {
    std::env::var("KNOWN_BREAKS").map(|v| v.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()).unwrap_or_default()
}

struct KnownBreak(&'static str);

/// Set-aside cases panic with `KnownBreak`; the default hook prints every other panic.
fn quiet_known_breaks() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        let default = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            if !info.payload().is::<KnownBreak>() {
                default(info)
            }
        }));
    });
}

/// Why a transaction was refused: the Anchor error code, else the token program's log, else the runtime error.
fn refusal_of(err: &str) -> String {
    if let Some(line) = err.lines().find(|l| l.contains("Error Code: ")) {
        return line.split("Error Code: ").nth(1).unwrap_or("").split('.').next().unwrap_or("").to_string();
    }
    if let Some(line) = err.lines().find(|l| l.contains("Program log: Error: ")) {
        return line.split("Program log: Error: ").nth(1).unwrap_or("").to_string();
    }
    err.lines().next().unwrap_or("").to_string()
}

impl Terms {
    fn spec(&self) -> Spec {
        let leak = |s: String| -> &'static str { Box::leak(s.into_boxed_str()) };
        Spec {
            issue: "2026-03-06",
            maturity: leak(format!("{}-03-06", 2026 + self.years)),
            months: 12 / self.periods_per_year,
            face_units: self.face_units,
            rate_bps: self.rate_bps,
            periods_per_year: self.periods_per_year,
            redemption_price_bps: self.redemption_price_bps,
            record_days_before_payment: self.record_days_before_payment,
            start: "2026-03-02",
            max_supply: self.max_supply,
        }
    }
}

proptest! {
    #![proptest_config(ProptestConfig { max_shrink_iters: 4_096, max_global_rejects: 1_000_000, ..ProptestConfig::default() })]

    #[test]
    fn invariants_hold_under_random_operations(
        terms in terms(),
        placements in prop::collection::vec((0usize..4, qty()).prop_map(|(h, qty)| Op::Place { h, qty, imposter: false }), 0..5),
        ops in prop::collection::vec(op(), 1..80),
    ) {
        quiet_known_breaks();
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut run = Run::new(terms);
            for (i, op) in placements.iter().chain(&ops).enumerate() {
                run.step = format!("step {i} ({op:?})");
                run.apply(op);
            }
            run.finish();
        }));
        print_tally();
        if let Err(payload) = outcome {
            match payload.downcast::<KnownBreak>() {
                Ok(known) => return Err(TestCaseError::reject(known.0)),
                Err(payload) => std::panic::resume_unwind(payload),
            }
        }
    }
}
