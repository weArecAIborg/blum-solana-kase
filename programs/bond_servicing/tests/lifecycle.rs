//! End-to-end lifecycles on LiteSVM (the Anchor template's harness) with clock jumps between record moments.
//!
//! Sample bonds (the system must serve any bond; these are only samples):
//!   - B1, the KASE listing's example: 1,000 face, 10%, semi-annual, 2 years, issued 2026-03-06;
//!   - MUM108_0015, a real KASE treasury bond: 1,000 face, 14.5%, annual, 9 years.
//! Sample conventions: record moment 09:00 UTC+5 on the payment day, Saturday/Sunday weekend, the Kazakh holiday
//! list for payment days.
//!
//! Token-2022 is the program deployed on devnet (`tests/fixtures/spl_token_2022_devnet.so`): the bond mint uses
//! the PermissionedBurn extension, which the Token-2022 bundled with LiteSVM 0.10 predates.

include!("common/harness.rs");

#[test]
fn b1_full_lifecycle_with_record_moment_logic() {
    let mut env = setup();
    assert_eq!(env.periods(), 4);
    for name in ["BH#1", "BH#2", "BH#3"] {
        env.register(name);
    }
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    assert_eq!(env.mint_supply(), 15);

    // Coupon 0: record moment 2026-09-07 09:00 (2026-09-06 is a Sunday).
    assert_eq!(Date::from_days(i64::from(env.payment_days[0])), date("2026-09-07"));
    env.set_time(env.record_moment(0) + 60);
    // A minute after the record moment BH#1 sells 3 bonds to BH#3. The sale keeps both 09:00 positions.
    env.transfer("BH#1", "BH#3", 3).unwrap();
    assert_eq!(env.bonds_of("BH#1"), 7);
    assert_eq!(env.record("BH#1").position.at_moment(0), 10);
    assert_eq!(env.record("BH#3").position.at_moment(0), 0);

    env.run_coupon(0, &["BH#1", "BH#2", "BH#3"]);
    assert_eq!(env.event(0).supply_at_record, 15);
    assert_eq!(env.event(0).budget, 750 * USD);
    assert_eq!(env.cash_of("BH#1"), 500 * USD, "listing example: 10 bonds at the record moment -> 500");
    assert_eq!(env.cash_of("BH#2"), 250 * USD);
    assert_eq!(env.cash_of("BH#3"), 0, "bought after the record moment: no coupon 0");
    let err = env.pay_coupon(0, "BH#3").unwrap_err();
    assert!(err.contains("NothingToPay"), "{err}");

    // Coupons 1-3 on 7 / 5 / 3 bonds.
    for index in 1..4u16 {
        env.set_time(env.record_moment(index as usize) + 60);
        env.run_coupon(index, &["BH#1", "BH#2", "BH#3"]);
    }
    assert_eq!(env.cash_of("BH#1"), (500 + 3 * 350) * USD);
    assert_eq!(env.cash_of("BH#2"), 4 * 250 * USD);
    assert_eq!(env.cash_of("BH#3"), 3 * 150 * USD);

    env.run_redemption(&["BH#1", "BH#2", "BH#3"]);
    assert_eq!(env.mint_supply(), 0);
    // Listing: "At maturity, the same holder is entitled to $10,000 in principal, plus any final coupon due."
    assert_eq!(env.cash_of("BH#1"), (500 + 3 * 350 + 7_000) * USD);
    assert_eq!(env.cash_of("BH#2"), (4 * 250 + 5_000) * USD);
    assert_eq!(env.cash_of("BH#3"), (3 * 150 + 3_000) * USD);
    for index in 0..=4u16 {
        assert_eq!(env.balance(&env.vault(index)), 0, "event {index}: budget paid out exactly");
    }
}

/// Long tenor: KASE MUM108_0015, 9 annual coupons of 145 per bond (S = N*C), payment days moved by the sample
/// calendar exactly as KASE publishes them (06.03.27 -> 09.03.27). Mid-life BH#2 sells 10 of its 40 bonds
/// to BH#3 after the 2030 record moment.
#[test]
fn mum108_0015_nine_year_lifecycle() {
    const KZT: u64 = USD; // same 6-decimal test cash token, read as tenge
    let mut env = setup_bond(&MUM108_0015);
    let days: Vec<Date> = env.payment_days.iter().map(|d| Date::from_days(i64::from(*d))).collect();
    assert_eq!(days.len(), 9);
    assert_eq!(days[1], date("2027-03-09")); // KASE: "06.03.27 - 09.03.27"
    assert_eq!(days[6], date("2032-03-09"));
    assert_eq!(days[7], date("2033-03-07"));
    assert_eq!(days[8], date("2034-03-06"));

    for name in ["BH#1", "BH#2", "BH#3"] {
        env.register(name);
    }
    env.place("BH#1", 100).unwrap();
    env.place("BH#2", 40).unwrap();
    for index in 0..5u16 {
        env.set_time(env.record_moment(index as usize) + 3_600);
        env.run_coupon(index, &["BH#1", "BH#2", "BH#3"]);
    }
    env.transfer("BH#2", "BH#3", 10).unwrap();
    for index in 5..9u16 {
        env.set_time(env.record_moment(index as usize) + 3_600);
        env.run_coupon(index, &["BH#1", "BH#2", "BH#3"]);
    }
    assert_eq!(env.event(0).budget, 140 * 145 * KZT);
    assert_eq!(env.cash_of("BH#1"), 9 * 100 * 145 * KZT);
    assert_eq!(env.cash_of("BH#2"), (5 * 40 + 4 * 30) * 145 * KZT);
    assert_eq!(env.cash_of("BH#3"), 4 * 10 * 145 * KZT);

    env.run_redemption(&["BH#1", "BH#2", "BH#3"]);
    assert_eq!(env.mint_supply(), 0);
    assert_eq!(env.cash_of("BH#1"), (9 * 100 * 145 + 100_000) * KZT);
    assert_eq!(env.cash_of("BH#3"), (4 * 10 * 145 + 10_000) * KZT);
    assert_eq!(env.balance(&env.vault(9)), 0);
}

// ---------- defects of the previous design (review F1-F8): each probe tries to reproduce one ----------

/// F1/F6: a holder burning bonds outside the program. Standard burns are refused by Token-2022 itself.
#[test]
fn holder_cannot_burn_bonds_outside_the_program() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 5).unwrap();
    let (h, m) = (env.holder("BH#1"), env.mint);
    let burn = spl_token_2022_interface::instruction::burn_checked(&token_2022(), &ata(&h, &m, &token_2022()), &m, &h, &[], 1, 0)
        .unwrap();
    let signer = env.holders["BH#1"].insecure_clone();
    env.set_time(env.record_moment(0) + 60);
    let err = env.send(&[burn.clone()], &[&signer]).unwrap_err();
    assert!(err.contains("Custom(12)"), "TokenError::InvalidInstruction expected: {err}");
    env.set_time(env.record_moment(3) + 60); // redemption: balances frozen
    assert!(env.send(&[burn], &[&signer]).is_err());
    assert_eq!(env.bonds_of("BH#1"), 5);
    assert_eq!(env.mint_supply(), 5);
}

/// F2: a holder sells everything after the record moment and closes the empty bond account. Its coupon on the
/// record-moment position is still paid, and nobody else waits for it.
#[test]
fn closed_bond_account_blocks_nobody() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.transfer("BH#1", "BH#2", 10).unwrap();
    let (h, m) = (env.holder("BH#1"), env.mint);
    let close = spl_token_2022_interface::instruction::close_account(&token_2022(), &ata(&h, &m, &token_2022()), &h, &h, &[])
        .unwrap();
    let signer = env.holders["BH#1"].insecure_clone();
    env.send(&[close], &[&signer]).unwrap();
    assert!(env.svm.get_account(&ata(&h, &m, &token_2022())).map_or(true, |a| a.lamports == 0));

    env.open_and_fund(0);
    env.pay_coupon(0, "BH#2").unwrap();
    env.pay_coupon(0, "BH#1").unwrap();
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
    assert_eq!(env.cash_of("BH#2"), 250 * USD);

    env.set_time(env.record_moment(1) + 60);
    env.run_coupon(1, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#2"), (250 + 750) * USD);
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
}

/// F3: nobody services coupons 0 and 1 in time; trades happen after later record moments. Every coupon is
/// still payable on its own record-moment positions, in any order.
#[test]
fn missed_coupons_stay_payable_after_later_record_moments() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.set_time(env.record_moment(1) + 60);
    env.transfer("BH#1", "BH#2", 4).unwrap(); // first change after moments 0 and 1: both keep 10 / 5
    env.set_time(env.record_moment(2) + 60);
    env.transfer("BH#2", "BH#1", 2).unwrap(); // moment 2: 6 / 9

    for index in [2u16, 0, 1] {
        env.run_coupon(index, &["BH#1", "BH#2"]);
    }
    assert_eq!(env.cash_of("BH#1"), (500 + 500 + 300) * USD);
    assert_eq!(env.cash_of("BH#2"), (250 + 250 + 450) * USD);
}

/// F5: the redemption budget is the principal of the whole supply, rounded down once; each holder is rounded
/// down on its own. The difference stays visibly in that event's vault, and nothing else is "reserved".
#[test]
fn redemption_rounding_leftover_stays_in_its_vault() {
    let spec = Spec { face_units: 10_001, redemption_price_bps: 5_000, ..B1 };
    let mut env = setup_bond(&spec);
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 1).unwrap();
    env.place("BH#2", 1).unwrap();
    env.set_time(env.record_moment(3) + 60);
    env.run_redemption(&["BH#1", "BH#2"]);
    assert_eq!(env.event(4).budget, 10_001);
    assert_eq!(env.cash_of("BH#1"), 5_000);
    assert_eq!(env.cash_of("BH#2"), 5_000);
    assert_eq!(env.balance(&env.vault(4)), 1);
    assert_eq!(env.mint_supply(), 0);
    // The final coupon does not depend on the redemption: still payable afterwards.
    env.run_coupon(3, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), 5_000 + 500);
}

/// F7: a record day before the payment day. A sale between the two leaves the coupon with the seller; payment
/// cannot start before the payment moment.
#[test]
fn record_day_before_payment_day() {
    let spec = Spec { record_days_before_payment: 2, ..B1 };
    let mut env = setup_bond(&spec);
    assert!(env.record_days[0] < env.payment_days[0]);
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.transfer("BH#1", "BH#2", 10).unwrap();
    env.open_and_fund(0);
    let err = env.pay_coupon(0, "BH#1").unwrap_err();
    assert!(err.contains("PaymentNotOpen"), "{err}");
    env.set_time(env.payment_moment(0));
    env.pay_coupon(0, "BH#1").unwrap();
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
    assert!(env.pay_coupon(0, "BH#2").unwrap_err().contains("NothingToPay"));
}

/// F8: a cash token with a transfer fee would deliver less than the amount sent; the bond refuses it.
#[test]
fn cash_token_with_transfer_fee_is_refused() {
    let mut svm = new_svm();
    let registrar = Keypair::new();
    svm.airdrop(&registrar.pubkey(), 10_000_000_000).unwrap();
    let start = local_time_to_unix(date(B1.start), 12 * 3_600, i64::from(UTC_OFFSET));
    let mut clock: Clock = svm.get_sysvar();
    clock.unix_timestamp = start;
    svm.set_sysvar(&clock);

    let cash = Keypair::new();
    let space = spl_token_2022_interface::extension::ExtensionType::try_calculate_account_len::<
        spl_token_2022_interface::state::Mint,
    >(&[spl_token_2022_interface::extension::ExtensionType::TransferFeeConfig])
    .unwrap();
    let rent = svm.minimum_balance_for_rent_exemption(space);
    let r = registrar.pubkey();
    let ixs = [
        system_instruction::create_account(&r, &cash.pubkey(), rent, space as u64, &token_2022()),
        spl_token_2022_interface::extension::transfer_fee::instruction::initialize_transfer_fee_config(
            &token_2022(),
            &cash.pubkey(),
            Some(&r),
            Some(&r),
            10,
            1_000_000,
        )
        .unwrap(),
        spl_token_2022_interface::instruction::initialize_mint2(&token_2022(), &cash.pubkey(), &r, None, 6).unwrap(),
    ];
    let msg = Message::new_with_blockhash(&ixs, Some(&r), &svm.latest_blockhash());
    svm.send_transaction(VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[&registrar, &cash]).unwrap())
        .unwrap();
    svm.expire_blockhash();

    let (payment_days, record_days) = schedule_days(&B1);
    let mint = Keypair::new();
    let create = create_bond_ix(&r, &mint.pubkey(), &cash.pubkey(), terms_for(&B1, &payment_days, &record_days, start));
    let msg = Message::new_with_blockhash(&[create], Some(&r), &svm.latest_blockhash());
    let result =
        svm.send_transaction(VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[&registrar, &mint]).unwrap());
    let logs = result.unwrap_err().meta.logs.join("\n");
    assert!(logs.contains("UnsupportedCashToken"), "{logs}");
}

// ---------- other rules ----------

#[test]
fn unregistered_wallet_cannot_receive_bonds() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    let outsider = Keypair::new();
    env.svm.airdrop(&outsider.pubkey(), 1_000_000_000).unwrap();
    let create = spl_associated_token_account_interface::instruction::create_associated_token_account(
        &outsider.pubkey(),
        &outsider.pubkey(),
        &env.mint,
        &token_2022(),
    );
    env.send(&[create], &[&outsider]).unwrap();
    let ix = env.transfer_ix(&env.holder("BH#1"), &outsider.pubkey(), 1);
    let signer = env.holders["BH#1"].insecure_clone();
    assert!(env.send(&[ix], &[&signer]).is_err());
    assert_eq!(env.bonds_of("BH#1"), 10);
}

#[test]
fn placement_closes_at_first_record_moment() {
    let mut env = setup();
    env.register("BH#1");
    env.set_time(env.record_moment(0));
    let err = env.place("BH#1", 1).unwrap_err();
    assert!(err.contains("PlacementClosed"), "{err}");
}

#[test]
fn open_before_record_moment_and_pay_before_funding_fail() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) - 1);
    let err = env.open_event(0).unwrap_err();
    assert!(err.contains("RecordMomentNotReached"), "{err}");

    env.set_time(env.record_moment(0));
    env.open_event(0).unwrap();
    let err = env.pay_coupon(0, "BH#1").unwrap_err();
    assert!(err.contains("NotFunded"), "{err}");
    env.fund(0, 499 * USD);
    let err = env.confirm_funding(0).unwrap_err();
    assert!(err.contains("InsufficientFunding"), "{err}");
}

#[test]
fn coupon_cannot_be_paid_twice() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0));
    env.run_coupon(0, &["BH#1"]);
    env.fund(0, 500 * USD);
    let err = env.pay_coupon(0, "BH#1").unwrap_err();
    assert!(err.contains("AlreadyPaid"), "{err}");
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
}

#[test]
fn transfers_close_at_final_record_moment() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(3) - 1);
    env.transfer("BH#1", "BH#2", 1).unwrap();
    env.set_time(env.record_moment(3));
    let err = env.transfer("BH#1", "BH#2", 1).unwrap_err();
    assert!(err.contains("TransfersClosedAtMaturity"), "{err}");
}

#[test]
fn non_registrar_cannot_register() {
    let mut env = setup();
    let intruder = Keypair::new();
    env.svm.airdrop(&intruder.pubkey(), 1_000_000_000).unwrap();
    let victim = Keypair::new().pubkey();
    let ix = Instruction {
        program_id: env.program,
        accounts: bond_servicing::accounts::RegisterHolder {
            registrar: intruder.pubkey(),
            bond: env.bond,
            holder: env.holder_record(&victim),
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: bond_servicing::instruction::RegisterHolder { owner: victim }.data(),
    };
    let err = env.send(&[ix], &[&intruder]).unwrap_err();
    assert!(err.contains("ConstraintHasOne") || err.contains("has one"), "{err}");
}

/// A holder registered after a record moment has no position at it, even if it buys right after.
#[test]
fn late_registered_holder_gets_nothing_for_past_record_moments() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.register("BH#2");
    env.transfer("BH#1", "BH#2", 3).unwrap();
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#2"), 0);
    env.set_time(env.record_moment(1) + 60);
    env.run_coupon(1, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#2"), 150 * USD);
    assert_eq!(env.cash_of("BH#1"), (500 + 350) * USD);
}

/// The bank freezes one holder's cash account: that holder cannot be paid, but the others are paid and the
/// next coupon still runs; once unfrozen, the holder collects both coupons.
#[test]
fn unpayable_holder_does_not_block_others_or_next_event() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    let frozen_cash = ata(&env.holder("BH#2"), &env.cash_mint, &env.cash_program);
    let freeze = spl_token_interface::instruction::freeze_account(
        &token_classic(),
        &frozen_cash,
        &env.cash_mint,
        &env.cash_authority.pubkey(),
        &[],
    )
    .unwrap();
    let bank = env.cash_authority.insecure_clone();
    env.send(&[freeze], &[&bank]).unwrap();

    env.set_time(env.record_moment(0) + 60);
    env.open_and_fund(0);
    env.pay_coupon(0, "BH#1").unwrap();
    assert!(env.pay_coupon(0, "BH#2").is_err(), "frozen account cannot receive");

    env.set_time(env.record_moment(1) + 60);
    env.open_and_fund(1);
    env.pay_coupon(1, "BH#1").unwrap();

    let thaw = spl_token_interface::instruction::thaw_account(
        &token_classic(),
        &frozen_cash,
        &env.cash_mint,
        &env.cash_authority.pubkey(),
        &[],
    )
    .unwrap();
    env.send(&[thaw], &[&bank]).unwrap();
    env.pay_coupon(0, "BH#2").unwrap();
    env.pay_coupon(1, "BH#2").unwrap();
    assert_eq!(env.cash_of("BH#2"), 2 * 250 * USD);
    assert_eq!(env.balance(&env.vault(0)), 0);
    assert_eq!(env.balance(&env.vault(1)), 0);
}

// ---------- third corporate action: buyback (first come) and tender offer (pro rata) ----------

/// Buyback: holders sell at once at the issuer's price until the maximum is bought. Bought bonds are burned, the
/// supply shrinks, and the next coupon's budget follows the smaller supply.
#[test]
fn buyback_first_come_until_the_maximum() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    let (open, close) = (env.now(), env.record_moment(0) - DAY);
    env.create_offer(0, false, 990 * USD, 6, open, close).unwrap();
    let err = env.sell(0, "BH#1", 4).unwrap_err();
    assert!(err.contains("OfferNotFunded"), "{err}");
    env.fund_offer(0, 6 * 990 * USD);
    env.confirm_offer_funding(0).unwrap();

    env.sell(0, "BH#1", 4).unwrap();
    assert_eq!(env.bonds_of("BH#1"), 6);
    assert_eq!(env.cash_of("BH#1"), 4 * 990 * USD);
    let err = env.sell(0, "BH#2", 3).unwrap_err();
    assert!(err.contains("ExceedsOffer"), "4 + 3 > 6: {err}");
    env.sell(0, "BH#2", 2).unwrap();
    assert_eq!(env.mint_supply(), 9);
    assert_eq!(env.balance(&env.offer_vault(0)), 0);

    env.set_time(close);
    let err = env.sell(0, "BH#2", 1).unwrap_err();
    assert!(err.contains("OfferNotOpen") || err.contains("ExceedsOffer"), "{err}");

    env.set_time(env.record_moment(0) + 60);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.event(0).supply_at_record, 9);
    assert_eq!(env.cash_of("BH#1"), (4 * 990 + 6 * 50) * USD);
    assert_eq!(env.cash_of("BH#2"), (2 * 990 + 3 * 50) * USD);
}

/// Tender offer across a record moment: tendered bonds stay in the holder's account, locked, so the holder keeps
/// that coupon; after the window, an oversubscribed offer is settled pro rata in whole bonds.
#[test]
fn tender_pro_rata_keeps_coupon_rights_during_the_window() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    let (open, close) = (env.record_moment(0) - DAY, env.record_moment(0) + 10 * DAY);
    env.create_offer(1, true, 1_000 * USD, 6, open, close).unwrap();
    env.fund_offer(1, 6 * 1_000 * USD);
    env.confirm_offer_funding(1).unwrap();
    let err = env.sell(1, "BH#1", 1).unwrap_err();
    assert!(err.contains("WrongOfferKind"), "{err}");

    env.set_time(open);
    env.change_tender(1, "BH#1", 8, false).unwrap();
    env.change_tender(1, "BH#2", 4, false).unwrap();
    env.change_tender(1, "BH#2", 1, true).unwrap(); // withdraws one: total 11 > 6
    let err = env.transfer("BH#1", "BH#2", 3).unwrap_err();
    assert!(err.contains("BondsLocked"), "only 2 of BH#1's 10 are free: {err}");
    let err = env.change_tender(1, "BH#1", 3, false).unwrap_err();
    assert!(err.contains("InsufficientUnlocked"), "{err}");

    // The record moment passes during the window: locked bonds keep their coupon.
    env.set_time(env.record_moment(0) + 60);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
    assert_eq!(env.cash_of("BH#2"), 250 * USD);

    let err = env.settle(1, "BH#1").unwrap_err();
    assert!(err.contains("OfferStillOpen"), "{err}");
    env.set_time(close);
    env.settle(1, "BH#1").unwrap(); // running total 8 of 11: floor(8 x 6 / 11) = 4
    env.settle(1, "BH#2").unwrap(); // running total 11 of 11: floor(11 x 6 / 11) - 4 = 2
    assert!(env.settle(1, "BH#1").unwrap_err().contains("AlreadySettled"));
    assert_eq!(env.bonds_of("BH#1"), 6);
    assert_eq!(env.bonds_of("BH#2"), 3);
    assert_eq!(env.cash_of("BH#1"), (500 + 4 * 1_000) * USD);
    assert_eq!(env.cash_of("BH#2"), (250 + 2 * 1_000) * USD);
    assert_eq!(env.balance(&env.offer_vault(1)), 0, "exactly the maximum bought: no cash left by rounding");
    assert_eq!(env.record("BH#1").locked, 0);
    env.transfer("BH#1", "BH#2", 3).unwrap(); // unlocked again

    env.set_time(env.record_moment(1) + 60);
    env.run_coupon(1, &["BH#1", "BH#2"]);
    assert_eq!(env.event(1).supply_at_record, 9);
    assert_eq!(env.cash_of("BH#1"), (500 + 4_000 + 3 * 50) * USD);
    assert_eq!(env.cash_of("BH#2"), (250 + 2_000 + 6 * 50) * USD);
}

/// An oversubscribed tender buys exactly its maximum. Tenders are resolved in the order they were made, each receiving
/// the pro-rata share of the running total minus what the earlier ones received. Seven 1-bond tenders for at most 5:
/// rounding each tender's own share down would buy none.
#[test]
fn oversubscribed_tender_buys_exactly_its_maximum_in_tender_order() {
    let mut env = setup();
    let names = ["BH#1", "BH#2", "BH#3", "BH#4", "BH#5", "BH#6", "BH#7"];
    for n in names {
        env.register(n);
        env.place(n, 2).unwrap();
    }
    let (open, close) = (env.now(), env.record_moment(0) - DAY);
    env.create_offer(1, true, 1_000 * USD, 5, open, close).unwrap();
    env.fund_offer(1, 5 * 1_000 * USD);
    env.confirm_offer_funding(1).unwrap();
    for n in names {
        env.change_tender(1, n, 1, false).unwrap();
    }
    env.set_time(close);
    let err = env.resolve(1, "BH#2").unwrap_err();
    assert!(err.contains("TenderOutOfOrder"), "the second tender cannot be resolved before the first: {err}");
    for n in names {
        env.settle(1, n).unwrap();
    }
    // Running total k x 5 / 7 rounded down, k = 1..7: 0 1 2 2 3 4 5, so the tenders receive 0 1 1 0 1 1 1.
    let accepted: Vec<u64> = names.iter().map(|n| 2 - env.bonds_of(n)).collect();
    assert_eq!(accepted, vec![0, 1, 1, 0, 1, 1, 1]);
    assert_eq!(accepted.iter().sum::<u64>(), 5, "exactly the maximum");
    assert_eq!(env.balance(&env.offer_vault(1)), 0);
}

#[test]
fn offer_must_close_before_the_final_record_moment() {
    let mut env = setup();
    let (open, close) = (env.now(), env.record_moment(3) + 1);
    let err = env.create_offer(2, true, 1_000 * USD, 5, open, close).unwrap_err();
    assert!(err.contains("InvalidOffer"), "{err}");
}

/// At maturity, bonds still accepted by an unsettled offer are left to that offer; the holder's free bonds are
/// redeemed now. The offer then buys the rest.
#[test]
fn redemption_pays_free_bonds_while_an_offer_is_pending() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    let (open, close) = (env.now(), env.record_moment(3) - DAY);
    env.create_offer(3, true, 1_000 * USD, 2, open, close).unwrap();
    env.fund_offer(3, 2 * 1_000 * USD);
    env.confirm_offer_funding(3).unwrap();
    env.change_tender(3, "BH#1", 2, false).unwrap();
    env.set_time(env.record_moment(3) + 60);
    env.open_and_fund(4);
    env.redeem("BH#1").unwrap(); // 8 free bonds
    assert_eq!(env.bonds_of("BH#1"), 2);
    assert_eq!(env.cash_of("BH#1"), 8 * 1_000 * USD);
    assert!(env.redeem("BH#1").unwrap_err().contains("NothingToPay"), "the 2 accepted bonds belong to the offer");
    env.settle(3, "BH#1").unwrap();
    assert_eq!(env.mint_supply(), 0);
    assert_eq!(env.cash_of("BH#1"), (8 * 1_000 + 2 * 1_000) * USD);
    assert_eq!(env.balance(&env.vault(4)), 2 * 1_000 * USD, "redemption budget for the 2 bought bonds stays visible");
}

/// Review 2, F (tender): the offer's cash is frozen after funding. Settlement cannot pay, but the rejected part is
/// released anyway (no cash involved), so it can be traded and redeemed; the accepted part waits for its cash.
#[test]
fn rejected_tender_bonds_are_released_while_offer_cash_is_frozen() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.register("BH#3");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 10).unwrap();
    let (open, close) = (env.now(), env.record_moment(2));
    env.create_offer(4, true, 1_000 * USD, 2, open, close).unwrap();
    env.fund_offer(4, 2 * 1_000 * USD);
    env.confirm_offer_funding(4).unwrap();
    env.change_tender(4, "BH#1", 10, false).unwrap();
    env.change_tender(4, "BH#2", 10, false).unwrap(); // total 20 > 2: each accepted 1, rejected 9
    let freeze = spl_token_interface::instruction::freeze_account(
        &token_classic(),
        &env.offer_vault(4),
        &env.cash_mint,
        &env.cash_authority.pubkey(),
        &[],
    )
    .unwrap();
    let bank = env.cash_authority.insecure_clone();
    env.send(&[freeze], &[&bank]).unwrap();

    env.set_time(close + 60);
    assert!(env.settle(4, "BH#1").is_err(), "frozen offer cash cannot pay");
    assert_eq!(env.record("BH#1").locked, 10, "a failed settlement changes nothing");
    env.resolve(4, "BH#1").unwrap();
    env.resolve(4, "BH#1").unwrap(); // idempotent
    assert_eq!(env.record("BH#1").locked, 1, "9 rejected bonds released without any cash movement");
    env.transfer("BH#1", "BH#3", 9).unwrap();

    env.set_time(env.record_moment(3) + 60);
    env.open_and_fund(4); // the redemption event (index = periods)
    env.redeem("BH#3").unwrap();
    assert_eq!(env.cash_of("BH#3"), 9 * 1_000 * USD);
    assert!(env.redeem("BH#1").unwrap_err().contains("NothingToPay"), "the accepted bond waits for its offer");

    let thaw = spl_token_interface::instruction::thaw_account(
        &token_classic(),
        &env.offer_vault(4),
        &env.cash_mint,
        &env.cash_authority.pubkey(),
        &[],
    )
    .unwrap();
    env.send(&[thaw], &[&bank]).unwrap();
    env.settle(4, "BH#1").unwrap();
    assert_eq!(env.bonds_of("BH#1"), 0);
    assert_eq!(env.cash_of("BH#1"), 1_000 * USD, "the accepted bond, bought once the cash can move");
}

/// Review 2, F (rounding): partial redemptions are paid on the cumulative quantity, so they round exactly like one.
/// Face 10,001 units at 50%: one bond is 5,000.5. Two bonds redeemed one by one must still pay 10,001 in total.
#[test]
fn partial_redemptions_round_like_one() {
    let spec = Spec { face_units: 10_001, redemption_price_bps: 5_000, ..B1 };
    let mut env = setup_bond(&spec);
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 2).unwrap();
    env.place("BH#2", 2).unwrap();
    let (open, close) = (env.now(), env.record_moment(2));
    env.create_offer(5, true, 1, 1, open, close).unwrap();
    env.fund_offer(5, 1);
    env.confirm_offer_funding(5).unwrap();
    env.change_tender(5, "BH#1", 1, false).unwrap();
    env.change_tender(5, "BH#2", 2, false).unwrap(); // total 3 > 1: running total, BH#1 floor(1/3) = 0, BH#2 1
    env.set_time(env.record_moment(3) + 60);
    env.open_and_fund(4);
    let before = env.cash_of("BH#1");
    env.redeem("BH#1").unwrap(); // the free bond: principal(1) - principal(0) = 5,000
    assert_eq!(env.cash_of("BH#1") - before, 5_000);
    env.resolve(5, "BH#1").unwrap(); // the tendered bond is rejected and released
    env.redeem("BH#1").unwrap(); // principal(2) - principal(1) = 10,001 - 5,000 = 5,001
    assert_eq!(env.cash_of("BH#1") - before, 10_001, "same as redeeming both bonds at once");
}

/// Review 2, F (issue size): placement refuses a supply whose redemption would not fit the cash token's u64 amount.
#[test]
fn issue_too_large_is_refused_at_placement() {
    let spec = Spec { max_supply: 20_000_000_000, ..B1 };
    let mut env = setup_bond(&spec);
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10_000_000_000).unwrap(); // principal 1e19 units: fits
    let err = env.place("BH#2", 10_000_000_000).unwrap_err(); // 2e19 units: does not
    assert!(err.contains("IssueTooLarge"), "{err}");
}

/// Review 2, F (memo): a Token-2022 cash account that requires memos is paid when the Memo program is passed, and
/// refused with a clear error when it is not; the right stays unpaid until then.
#[test]
fn memo_required_cash_account_is_paid() {
    let mut env = setup_bond_with(&B1, token_2022());
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    let owner = env.holders["BH#1"].insecure_clone();
    let cash_account = ata(&owner.pubkey(), &env.cash_mint, &token_2022());
    let ixs = [
        spl_token_2022_interface::instruction::reallocate(
            &token_2022(),
            &cash_account,
            &owner.pubkey(),
            &owner.pubkey(),
            &[],
            &[spl_token_2022_interface::extension::ExtensionType::MemoTransfer],
        )
        .unwrap(),
        spl_token_2022_interface::extension::memo_transfer::instruction::enable_required_transfer_memos(
            &token_2022(),
            &cash_account,
            &owner.pubkey(),
            &[],
        )
        .unwrap(),
    ];
    env.send(&ixs, &[&owner]).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.open_and_fund(0);
    let err = env.pay_coupon(0, "BH#1").unwrap_err();
    assert!(err.contains("MemoProgramMissing"), "{err}");
    env.memo = Some(Pubkey::from_str_const("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"));
    env.pay_coupon(0, "BH#1").unwrap();
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
}

// ---------- hostile attempts beyond the devnet attacker run ----------

/// Token-2022 cash mint whose issuer (the bank) also holds a permanent delegate over every account of the token, as
/// some regulated stablecoins do.
fn create_cash_mint_with_permanent_delegate(svm: &mut LiteSVM, payer: &Keypair, bank: &Pubkey, program: &Pubkey) -> Pubkey {
    use spl_token_2022_interface::extension::ExtensionType;
    let mint = Keypair::new();
    let space = ExtensionType::try_calculate_account_len::<spl_token_2022_interface::state::Mint>(&[ExtensionType::PermanentDelegate])
        .unwrap();
    let rent = svm.minimum_balance_for_rent_exemption(space);
    let ixs = [
        system_instruction::create_account(&payer.pubkey(), &mint.pubkey(), rent, space as u64, program),
        spl_token_2022_interface::instruction::initialize_permanent_delegate(program, &mint.pubkey(), bank).unwrap(),
        spl_token_2022_interface::instruction::initialize_mint2(program, &mint.pubkey(), bank, Some(bank), 6).unwrap(),
    ];
    let msg = Message::new_with_blockhash(&ixs, Some(&payer.pubkey()), &svm.latest_blockhash());
    svm.send_transaction(VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[payer, &mint]).unwrap()).unwrap();
    svm.expire_blockhash();
    mint.pubkey()
}

/// Token-2022 cash mint with a pause switch held by the bank.
fn create_cash_mint_with_pause(svm: &mut LiteSVM, payer: &Keypair, bank: &Pubkey, program: &Pubkey) -> Pubkey {
    use spl_token_2022_interface::extension::ExtensionType;
    let mint = Keypair::new();
    let space =
        ExtensionType::try_calculate_account_len::<spl_token_2022_interface::state::Mint>(&[ExtensionType::Pausable]).unwrap();
    let rent = svm.minimum_balance_for_rent_exemption(space);
    let ixs = [
        system_instruction::create_account(&payer.pubkey(), &mint.pubkey(), rent, space as u64, program),
        spl_token_2022_interface::extension::pausable::instruction::initialize(program, &mint.pubkey(), bank).unwrap(),
        spl_token_2022_interface::instruction::initialize_mint2(program, &mint.pubkey(), bank, Some(bank), 6).unwrap(),
    ];
    let msg = Message::new_with_blockhash(&ixs, Some(&payer.pubkey()), &svm.latest_blockhash());
    svm.send_transaction(VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[payer, &mint]).unwrap()).unwrap();
    svm.expire_blockhash();
    mint.pubkey()
}

/// The reason a refused transaction gives: the program's error or Token-2022's message, else the runtime error.
fn refusal(err: &str) -> String {
    let lines: Vec<&str> = err.lines().collect();
    lines
        .iter()
        .rev()
        .find_map(|l| l.split("Error Message: ").nth(1).or_else(|| l.split("Error: ").nth(1)))
        .map(str::to_string)
        .unwrap_or_else(|| lines[0].to_string())
}

/// An instruction that names `who` as its signing authority without its signature (only the program could sign).
fn unsigned(mut ix: Instruction, who: &Pubkey) -> Instruction {
    for meta in ix.accounts.iter_mut().filter(|m| m.pubkey == *who) {
        meta.is_signer = false;
    }
    ix
}

/// Nobody but the program controls the bond mint or a vault: re-pointing the transfer hook (which would let transfers
/// skip the records), taking the permanent delegate, the mint authority or the hook authority, and taking, approving
/// or closing a vault are all refused, whether the attacker signs as itself or names the Bond PDA without its
/// signature. Afterwards the hook still records transfers and the coupon still pays.
#[test]
fn nobody_takes_over_the_bond_mint_or_a_vault() {
    use spl_token_2022_interface::instruction::{approve, close_account, set_authority, AuthorityType};
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.open_event(0).unwrap(); // the vault is empty until the bank funds it
    let attacker = Keypair::new();
    env.svm.airdrop(&attacker.pubkey(), 1_000_000_000).unwrap();
    let (a, bond, mint, vault, cash) = (attacker.pubkey(), env.bond, env.mint, env.vault(0), env.cash_program);
    let t22 = token_2022();
    let hook = |authority: &Pubkey| {
        spl_token_2022_interface::extension::transfer_hook::instruction::update(&t22, &mint, authority, &[], Some(a)).unwrap()
    };
    let (not_owner, unsigned_pda) = ("owner does not match", "MissingRequiredSignature");
    let attempts: Vec<(&str, Instruction, &str)> = vec![
        ("re-point the transfer hook", hook(&a), not_owner),
        ("re-point the transfer hook naming the Bond PDA", unsigned(hook(&bond), &bond), unsigned_pda),
        ("take the permanent delegate", set_authority(&t22, &mint, Some(&a), AuthorityType::PermanentDelegate, &a, &[]).unwrap(), not_owner),
        (
            "take the permanent delegate naming the Bond PDA",
            unsigned(set_authority(&t22, &mint, Some(&a), AuthorityType::PermanentDelegate, &bond, &[]).unwrap(), &bond),
            unsigned_pda,
        ),
        ("take the mint authority", set_authority(&t22, &mint, Some(&a), AuthorityType::MintTokens, &a, &[]).unwrap(), not_owner),
        ("take the hook authority", set_authority(&t22, &mint, Some(&a), AuthorityType::TransferHookProgramId, &a, &[]).unwrap(), not_owner),
        ("close the empty vault", close_account(&cash, &vault, &a, &a, &[]).unwrap(), not_owner),
        ("close the empty vault naming the Bond PDA", unsigned(close_account(&cash, &vault, &a, &bond, &[]).unwrap(), &bond), unsigned_pda),
        ("approve itself on the vault", approve(&cash, &vault, &a, &a, &[], u64::MAX).unwrap(), not_owner),
        ("approve itself on the vault naming the Bond PDA", unsigned(approve(&cash, &vault, &a, &bond, &[], u64::MAX).unwrap(), &bond), unsigned_pda),
        ("take ownership of the vault", set_authority(&cash, &vault, Some(&a), AuthorityType::AccountOwner, &a, &[]).unwrap(), not_owner),
    ];
    for (what, ix, reason) in attempts {
        let err = env.send(&[ix], &[&attacker]).unwrap_err();
        assert!(refusal(&err).contains(reason), "{what}: {err}");
    }
    let budget = env.event(0).budget;
    env.fund(0, budget);
    env.confirm_funding(0).unwrap();
    env.transfer("BH#1", "BH#2", 4).unwrap();
    assert_eq!(env.record("BH#2").position.quantity, 4, "the hook still records transfers");
    env.pay_coupon(0, "BH#1").unwrap();
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
}

/// Anyone may create a bond, also on the same cash token. An attacker's own bond, with its own holder record, event and
/// vault, cannot be mixed with the first bond's accounts to pay itself from the first bond's vault or to confirm its
/// own event with the first bond's cash.
#[test]
fn a_second_bond_on_the_same_cash_token_cannot_reach_the_first_bonds_vaults() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();

    // The attacker's bond: same cash token, same schedule, 1,000 bonds placed with itself.
    let attacker = Keypair::new();
    env.svm.airdrop(&attacker.pubkey(), 10_000_000_000).unwrap();
    let a = attacker.pubkey();
    let mint2 = Keypair::new();
    let terms = terms_for(&B1, &env.payment_days.clone(), &env.record_days.clone(), env.now());
    let create = create_bond_ix(&a, &mint2.pubkey(), &env.cash_mint, terms);
    env.send(&[create], &[&attacker, &mint2]).unwrap();
    let p = env.program;
    let bond2 = Pubkey::find_program_address(&[BOND_SEED, mint2.pubkey().as_ref()], &p).0;
    let holder2 = Pubkey::find_program_address(&[HOLDER_SEED, mint2.pubkey().as_ref(), a.as_ref()], &p).0;
    let event2 = Pubkey::find_program_address(&[EVENT_SEED, bond2.as_ref(), &0u16.to_le_bytes()], &p).0;
    let vault2 = Pubkey::find_program_address(&[VAULT_SEED, event2.as_ref()], &p).0;
    let register = Instruction {
        program_id: p,
        accounts: bond_servicing::accounts::RegisterHolder { registrar: a, bond: bond2, holder: holder2, system_program: system_program::ID }
            .to_account_metas(None),
        data: bond_servicing::instruction::RegisterHolder { owner: a }.data(),
    };
    let create_atas = [
        spl_associated_token_account_interface::instruction::create_associated_token_account(&a, &a, &mint2.pubkey(), &token_2022()),
        spl_associated_token_account_interface::instruction::create_associated_token_account(&a, &a, &env.cash_mint, &env.cash_program),
    ];
    let place = Instruction {
        program_id: p,
        accounts: bond_servicing::accounts::Place {
            registrar: a,
            bond: bond2,
            mint: mint2.pubkey(),
            holder: holder2,
            holder_bonds: ata(&a, &mint2.pubkey(), &token_2022()),
            token_program: token_2022(),
        }
        .to_account_metas(None),
        data: bond_servicing::instruction::Place { amount: 1_000 }.data(),
    };
    env.send(&[register], &[&attacker]).unwrap();
    env.send(&create_atas, &[&attacker]).unwrap();
    env.send(&[place], &[&attacker]).unwrap();

    env.set_time(env.record_moment(0) + 60);
    env.open_and_fund(0);
    let open2 = Instruction {
        program_id: p,
        accounts: bond_servicing::accounts::OpenEvent {
            payer: a,
            bond: bond2,
            event: event2,
            cash_mint: env.cash_mint,
            vault: vault2,
            cash_token_program: env.cash_program,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: bond_servicing::instruction::OpenEvent { index: 0 }.data(),
    };
    env.send(&[open2], &[&attacker]).unwrap();

    let pay = |bond: Pubkey, event: Pubkey, vault: Pubkey, holder: Pubkey, env: &Env| Instruction {
        program_id: p,
        accounts: bond_servicing::accounts::PayCoupon {
            payer: a,
            bond,
            event,
            holder,
            owner: a,
            vault,
            cash_mint: env.cash_mint,
            holder_cash: ata(&a, &env.cash_mint, &env.cash_program),
            memo_program: None,
            cash_token_program: env.cash_program,
            associated_token_program: spl_associated_token_account_interface::program::ID,
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: bond_servicing::instruction::PayCoupon {}.data(),
    };
    let confirm = |event: Pubkey, vault: Pubkey| Instruction {
        program_id: p,
        accounts: bond_servicing::accounts::ConfirmFunding { event, vault }.to_account_metas(None),
        data: bond_servicing::instruction::ConfirmFunding {}.data(),
    };
    let (seeds, has_one) = ("seeds constraint", "has one constraint");
    let attempts = [
        ("its holder record on the first bond's coupon", pay(env.bond, env.event_pda(0), env.vault(0), holder2, &env), seeds),
        ("its own bond and event, paid from the first bond's vault", pay(bond2, event2, env.vault(0), holder2, &env), has_one),
        ("the first bond with its own event", pay(env.bond, event2, env.vault(0), holder2, &env), seeds),
        ("its own event confirmed with the first bond's vault", confirm(event2, env.vault(0)), has_one),
    ];
    let before = env.balance(&env.vault(0));
    for (what, ix, reason) in attempts {
        let err = env.send(&[ix], &[&attacker]).unwrap_err();
        assert!(refusal(&err).contains(reason), "{what}: {err}");
    }
    assert_eq!(env.balance(&env.vault(0)), before);
    assert_eq!(env.balance(&ata(&a, &env.cash_mint, &env.cash_program)), 0);
    env.pay_coupon(0, "BH#1").unwrap();
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
}

/// A coupon is confirmed funded only from its own vault: another account that holds the budget does not count.
#[test]
fn funding_is_confirmed_only_from_the_events_own_vault() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.open_event(0).unwrap();
    let budget = env.event(0).budget;
    let fuller = ata(&env.holder("BH#1"), &env.cash_mint, &env.cash_program);
    let credit = spl_token_2022_interface::instruction::mint_to(&env.cash_program, &env.cash_mint, &fuller, &env.cash_authority.pubkey(), &[], budget)
        .unwrap();
    let bank = env.cash_authority.insecure_clone();
    env.send(&[credit], &[&bank]).unwrap();
    let ix = Instruction {
        program_id: env.program,
        accounts: bond_servicing::accounts::ConfirmFunding { event: env.event_pda(0), vault: fuller }.to_account_metas(None),
        data: bond_servicing::instruction::ConfirmFunding {}.data(),
    };
    let keeper = env.keeper.insecure_clone();
    let err = env.send(&[ix], &[&keeper]).unwrap_err();
    assert!(err.contains("ConstraintHasOne"), "{err}");
    assert!(!env.event(0).funded);
    assert!(env.pay_coupon(0, "BH#1").unwrap_err().contains("NotFunded"));
}

/// The same bonds cannot be committed twice: bonds tendered into one offer cannot be sold to a buyback, tendered into
/// a second offer, transferred, or withdrawn beyond what was tendered.
#[test]
fn the_same_bonds_cannot_be_committed_twice() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    let (open, close) = (env.now(), env.record_moment(0) - DAY);
    env.create_offer(0, false, 990 * USD, 10, open, close).unwrap();
    env.fund_offer(0, 10 * 990 * USD);
    env.confirm_offer_funding(0).unwrap();
    for id in [1, 2] {
        env.create_offer(id, true, 1_000 * USD, 10, open, close).unwrap();
        env.fund_offer(id, 10 * 1_000 * USD);
        env.confirm_offer_funding(id).unwrap();
    }
    env.change_tender(1, "BH#1", 10, false).unwrap();
    assert!(env.sell(0, "BH#1", 1).unwrap_err().contains("InsufficientUnlocked"), "tendered, then sold to the buyback");
    assert!(env.change_tender(2, "BH#1", 1, false).unwrap_err().contains("InsufficientUnlocked"), "tendered into a second offer");
    assert!(env.transfer("BH#1", "BH#2", 1).unwrap_err().contains("BondsLocked"), "tendered, then transferred");
    assert!(env.change_tender(1, "BH#1", 11, true).unwrap_err().contains("ExceedsOffer"), "withdraws more than tendered");
    env.set_time(close);
    env.settle(1, "BH#1").unwrap();
    assert!(env.settle(1, "BH#1").unwrap_err().contains("AlreadySettled"));
    assert_eq!(env.bonds_of("BH#1"), 0);
    assert_eq!(env.cash_of("BH#1"), 10 * 1_000 * USD, "paid once, by the one offer the bonds were tendered into");
}

/// The record moment is one second: a transfer one second before it moves the coupon with the bonds; a transfer in
/// that second is already after it, so the seller keeps the coupon on the bonds it sold.
#[test]
fn a_transfer_in_the_record_second_counts_as_after_the_record() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) - 1);
    env.transfer("BH#1", "BH#2", 2).unwrap();
    env.set_time(env.record_moment(0));
    env.transfer("BH#1", "BH#2", 3).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), 8 * 50 * USD, "10 - 2 bonds at 09:00:00");
    assert_eq!(env.cash_of("BH#2"), 2 * 50 * USD);
}

/// Transfers of zero bonds and transfers to oneself change no position and no entitlement, before and after a record
/// moment.
#[test]
fn zero_and_self_transfers_change_no_entitlement() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    for t in [env.record_moment(0) - 60, env.record_moment(0) + 60] {
        env.set_time(t);
        env.transfer("BH#1", "BH#1", 3).unwrap();
        env.transfer("BH#1", "BH#2", 0).unwrap();
        env.transfer("BH#2", "BH#1", 0).unwrap();
    }
    assert_eq!(env.record("BH#1").position.quantity, 10);
    assert_eq!(env.record("BH#2").position.quantity, 5);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), 500 * USD);
    assert_eq!(env.cash_of("BH#2"), 250 * USD);
}

/// A holder turns on Token-2022's CPI Guard on its bond account (it stops programs from using the owner's authority
/// over the account). The program burns with the Bond PDA's authority, so the guard must not block selling to a
/// buyback, settling a tender, or redemption.
#[test]
fn cpi_guard_on_a_bond_account_does_not_block_retirement() {
    use spl_token_2022_interface::extension::ExtensionType;
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    let (open, close) = (env.now(), env.record_moment(0) - DAY);
    env.create_offer(0, false, 990 * USD, 1, open, close).unwrap();
    env.fund_offer(0, 990 * USD);
    env.confirm_offer_funding(0).unwrap();
    env.create_offer(1, true, 1_000 * USD, 2, open, close).unwrap();
    env.fund_offer(1, 2 * 1_000 * USD);
    env.confirm_offer_funding(1).unwrap();

    let owner = env.holders["BH#1"].insecure_clone();
    let bonds = ata(&owner.pubkey(), &env.mint, &token_2022());
    let guard = [
        spl_token_2022_interface::instruction::reallocate(&token_2022(), &bonds, &owner.pubkey(), &owner.pubkey(), &[], &[ExtensionType::CpiGuard])
            .unwrap(),
        spl_token_2022_interface::extension::cpi_guard::instruction::enable_cpi_guard(&token_2022(), &bonds, &owner.pubkey(), &[]).unwrap(),
    ];
    env.send(&guard, &[&owner]).unwrap();

    env.sell(0, "BH#1", 1).unwrap();
    env.change_tender(1, "BH#1", 2, false).unwrap();
    env.set_time(close);
    env.settle(1, "BH#1").unwrap();
    assert_eq!(env.bonds_of("BH#1"), 7);
    env.set_time(env.record_moment(3) + 60);
    env.open_and_fund(4);
    env.redeem("BH#1").unwrap();
    assert_eq!(env.bonds_of("BH#1"), 0);
    assert_eq!(env.cash_of("BH#1"), (990 + 2 * 1_000 + 7 * 1_000) * USD);
}

/// A cash token with a permanent delegate (as some regulated stablecoins have) is refused when the bond is created: its
/// delegate could move any vault's cash without the program.
#[test]
fn cash_token_with_permanent_delegate_is_refused() {
    let mut svm = new_svm();
    let registrar = Keypair::new();
    svm.airdrop(&registrar.pubkey(), 10_000_000_000).unwrap();
    let start = local_time_to_unix(date(B1.start), 12 * 3_600, i64::from(UTC_OFFSET));
    let mut clock: Clock = svm.get_sysvar();
    clock.unix_timestamp = start;
    svm.set_sysvar(&clock);
    let delegate = Keypair::new();
    let cash = create_cash_mint_with_permanent_delegate(&mut svm, &registrar, &delegate.pubkey(), &token_2022());
    let (payment_days, record_days) = schedule_days(&B1);
    let mint = Keypair::new();
    let r = registrar.pubkey();
    let create = create_bond_ix(&r, &mint.pubkey(), &cash, terms_for(&B1, &payment_days, &record_days, start));
    let msg = Message::new_with_blockhash(&[create], Some(&r), &svm.latest_blockhash());
    let result =
        svm.send_transaction(VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[&registrar, &mint]).unwrap());
    let logs = result.unwrap_err().meta.logs.join("\n");
    assert!(logs.contains("UnsupportedCashToken"), "{logs}");
}

/// Trusted, not enforced: the cash token's issuer can freeze a vault (USDC-like tokens have a freeze authority). The
/// event stays funded and nobody is paid from it until the issuer thaws it; then everyone is paid, nothing is lost.
#[test]
fn a_frozen_vault_pays_nobody_until_thawed() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    env.open_and_fund(0);
    let bank = env.cash_authority.insecure_clone();
    let (cash, vault, mint, authority) = (env.cash_program, env.vault(0), env.cash_mint, bank.pubkey());
    let freeze = spl_token_2022_interface::instruction::freeze_account(&cash, &vault, &mint, &authority, &[]).unwrap();
    env.send(&[freeze], &[&bank]).unwrap();
    assert!(env.pay_coupon(0, "BH#1").is_err(), "a frozen vault pays nobody");
    assert!(!env.record("BH#1").is_paid(0), "the claim stays open");
    let thaw = spl_token_2022_interface::instruction::thaw_account(&cash, &vault, &mint, &authority, &[]).unwrap();
    env.send(&[thaw], &[&bank]).unwrap();
    env.pay_coupon(0, "BH#1").unwrap();
    env.pay_coupon(0, "BH#2").unwrap();
    assert_eq!(env.cash_of("BH#1") + env.cash_of("BH#2"), 1_000 * USD);
}

/// The registrar places bonds only up to the bond's issue size, fixed in its terms at creation.
#[test]
fn placement_beyond_the_issue_size_is_refused() {
    let spec = Spec { max_supply: 15, ..B1 };
    let mut env = setup_bond(&spec);
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    let err = env.place("BH#2", 6).unwrap_err();
    assert!(err.contains("ExceedsIssueSize"), "{err}");
    env.place("BH#2", 5).unwrap();
    assert_eq!(env.mint_supply(), 15);
    assert!(env.place("BH#1", 1).unwrap_err().contains("ExceedsIssueSize"));
}

/// Trusted, not enforced: a cash token with a pause switch. Records whether such a token is accepted and what a pause
/// does to payments.
#[test]
fn cash_issuer_pause() {
    let mut env = setup_bond_with_cash(&B1, token_2022(), create_cash_mint_with_pause);
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    env.set_time(env.record_moment(0) + 60);
    let opened = env.open_event(0);
    println!("event vault opened on a pausable cash token: {}", if opened.is_ok() { "yes" } else { "no" });
    if opened.is_err() {
        println!("{}", opened.unwrap_err());
        return;
    }
    let budget = env.event(0).budget;
    env.fund(0, budget);
    env.confirm_funding(0).unwrap();
    let bank = env.cash_authority.insecure_clone();
    let pause = spl_token_2022_interface::extension::pausable::instruction::pause(&token_2022(), &env.cash_mint, &bank.pubkey(), &[]).unwrap();
    env.send(&[pause], &[&bank]).unwrap();
    println!("coupon paid while the cash token is paused: {}", if env.pay_coupon(0, "BH#1").is_ok() { "yes" } else { "no" });
}

// ---------- secondary trades settled by Solana DvP ----------

/// A trade settles delivery versus payment through the Solana DvP program: the buyer's cash and the seller's bonds
/// move in one transaction, the hook runs inside it, and the positions follow.
#[test]
fn dvp_trade_settles_bonds_against_cash_in_one_transaction() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.give_cash("BH#2", 3_000 * USD);

    let swap = env.dvp_create_and_admit("BH#1", "BH#2", 1, 3, 3_000 * USD).unwrap();
    env.dvp_fund_bonds("BH#1", &swap, 3).unwrap();
    assert_eq!(env.bonds_of("BH#1"), 7);
    let seller = env.record("BH#1");
    assert_eq!((seller.position.quantity, seller.escrowed, seller.locked), (10, 3, 0), "escrowed bonds stay the seller's position");
    env.transfer("BH#1", "BH#2", 7).unwrap(); // the 7 free bonds may move
    env.transfer("BH#2", "BH#1", 7).unwrap();
    let err = env.transfer("BH#1", "BH#2", 8).unwrap_err();
    assert!(err.contains("insufficient funds"), "only 7 are in the account: {err}");
    env.dvp_fund_cash("BH#2", &swap, 3_000 * USD).unwrap();

    let logs = env.dvp_settle("BH#1", "BH#2", &swap).unwrap();
    assert!(logs.iter().any(|l| l.contains("Instruction: TransferHook")), "the hook ran inside the DvP settlement: {logs:?}");
    assert_eq!(env.bonds_of("BH#1"), 7);
    assert_eq!(env.bonds_of("BH#2"), 8);
    assert_eq!(env.cash_of("BH#1"), 3_000 * USD);
    assert_eq!(env.cash_of("BH#2"), 0);
    let seller = env.record("BH#1");
    assert_eq!((seller.position.quantity, seller.escrowed), (7, 0));
    assert_eq!(env.record("BH#2").position.quantity, 8);
    assert!(env.svm.get_account(&ata(&swap, &env.mint, &token_2022())).is_none(), "escrow closed");

    env.set_time(env.record_moment(0) + 60);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), (3_000 + 7 * 50) * USD);
    assert_eq!(env.cash_of("BH#2"), 8 * 50 * USD);
}

/// A record moment inside an open trade pays the seller: the bonds in the escrow are still the seller's position.
/// Settled after it, the buyer's position starts from the settlement, and the seller's history keeps the record.
#[test]
fn dvp_record_moment_inside_an_open_trade_pays_the_seller() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.give_cash("BH#2", 4_000 * USD);

    let swap = env.dvp_create_and_admit("BH#1", "BH#2", 7, 4, 4_000 * USD).unwrap();
    env.dvp_fund_bonds("BH#1", &swap, 4).unwrap();

    env.set_time(env.record_moment(0) + 60);
    env.run_coupon(0, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), 10 * 50 * USD, "the seller is paid on all 10, 4 of them in the escrow");
    assert_eq!(env.cash_of("BH#2"), (4_000 + 5 * 50) * USD);
    let err = env.pay_coupon(0, "BH#1").unwrap_err();
    assert!(err.contains("AlreadyPaid"), "{err}");

    env.dvp_fund_cash("BH#2", &swap, 4_000 * USD).unwrap();
    env.dvp_settle("BH#1", "BH#2", &swap).unwrap();
    assert_eq!(env.record("BH#1").position.at_moment(0), 10, "the seller's record-moment position is kept");
    assert_eq!(env.record("BH#2").position.at_moment(0), 5);
    assert_eq!((env.record("BH#1").position.quantity, env.record("BH#2").position.quantity), (6, 9));

    env.set_time(env.record_moment(1) + 60);
    env.run_coupon(1, &["BH#1", "BH#2"]);
    assert_eq!(env.cash_of("BH#1"), (500 + 4_000 + 6 * 50) * USD);
    assert_eq!(env.cash_of("BH#2"), (250 + 9 * 50) * USD);
}

/// A cancelled trade returns the bonds to the seller: the lock is released, the position never moved.
#[test]
fn dvp_cancel_returns_the_bonds_and_releases_the_lock() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.give_cash("BH#2", 2_000 * USD);

    let swap = env.dvp_create_and_admit("BH#1", "BH#2", 3, 2, 2_000 * USD).unwrap();
    env.dvp_fund_bonds("BH#1", &swap, 2).unwrap();
    env.dvp_fund_cash("BH#2", &swap, 2_000 * USD).unwrap();
    env.dvp_cancel("BH#1", "BH#2", &swap).unwrap();
    assert_eq!(env.bonds_of("BH#1"), 10);
    assert_eq!(env.cash_of("BH#2"), 2_000 * USD);
    let seller = env.record("BH#1");
    assert_eq!((seller.position.quantity, seller.escrowed), (10, 0));
    env.transfer("BH#1", "BH#2", 10).unwrap();
}

/// A trade still open at the final record moment can no longer settle, and its bonds must not stay in the escrow:
/// cancel, reject, and reclaim after the trade's expiry return them to the seller, who is redeemed on all of them.
#[test]
fn dvp_trade_open_at_the_final_record_moment_returns_the_bonds_for_redemption() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.place("BH#1", 10).unwrap();
    env.give_cash("BH#2", 3_000 * USD);
    env.set_time(env.record_moment(3) - 10 * DAY);
    let (open, expired) = (env.record_moment(3) + 30 * DAY, env.record_moment(3) - 60);
    let cancelled = env.dvp_create_and_admit_until("BH#1", "BH#2", 1, 1, 1_000 * USD, open).unwrap();
    let rejected = env.dvp_create_and_admit_until("BH#1", "BH#2", 2, 2, 1_000 * USD, open).unwrap();
    let reclaimed = env.dvp_create_and_admit_until("BH#1", "BH#2", 3, 3, 1_000 * USD, expired).unwrap();
    for (swap, quantity) in [(&cancelled, 1), (&rejected, 2), (&reclaimed, 3)] {
        env.dvp_fund_bonds("BH#1", swap, quantity).unwrap();
        env.dvp_fund_cash("BH#2", swap, 1_000 * USD).unwrap();
    }
    assert_eq!(env.record("BH#1").escrowed, 6);

    env.set_time(env.record_moment(3));
    let err = env.dvp_settle("BH#1", "BH#2", &cancelled).unwrap_err();
    assert!(err.contains("TransfersClosedAtMaturity"), "{err}");
    env.dvp_cancel("BH#1", "BH#2", &cancelled).unwrap();
    env.dvp_reject("BH#2", "BH#1", "BH#2", &rejected).unwrap();
    env.dvp_reclaim("BH#1", &reclaimed, true).unwrap();
    env.dvp_reclaim("BH#2", &reclaimed, false).unwrap();
    assert_eq!(env.bonds_of("BH#1"), 10);
    assert_eq!(env.cash_of("BH#2"), 3_000 * USD);
    let seller = env.record("BH#1");
    assert_eq!((seller.position.quantity, seller.escrowed), (10, 0));
    let err = env.transfer("BH#1", "BH#2", 1).unwrap_err();
    assert!(err.contains("TransfersClosedAtMaturity"), "only the return to the seller is open: {err}");

    env.set_time(env.record_moment(3) + 60);
    env.run_redemption(&["BH#1"]);
    assert_eq!(env.cash_of("BH#1"), 10 * 1_000 * USD);
    assert_eq!(env.mint_supply(), 0);
}

/// The escrow's record is not a holder: it is paid nothing, and only the trade's seller can put bonds into it.
#[test]
fn dvp_escrow_is_not_a_holder_and_takes_bonds_only_from_its_seller() {
    let mut env = setup();
    env.register("BH#1");
    env.register("BH#2");
    env.register("BH#3");
    env.place("BH#1", 10).unwrap();
    env.place("BH#2", 5).unwrap();
    env.place("BH#3", 5).unwrap();
    env.give_cash("BH#2", 1_000 * USD);

    let swap = env.dvp_create_and_admit("BH#1", "BH#2", 11, 1, 1_000 * USD).unwrap();
    let ix = env.transfer_ix(&env.holder("BH#3"), &swap, 1);
    let bh3 = env.holders["BH#3"].insecure_clone();
    let err = env.send(&[ix], &[&bh3]).unwrap_err();
    assert!(err.contains("EscrowDestination"), "{err}");
    env.dvp_fund_bonds("BH#1", &swap, 1).unwrap();

    env.set_time(env.record_moment(0) + 60);
    env.open_and_fund(0);
    assert_eq!(env.event(0).supply_at_record, 20);
    env.pay_coupon(0, "BH#1").unwrap();
    env.pay_coupon(0, "BH#2").unwrap();
    env.pay_coupon(0, "BH#3").unwrap();
    assert_eq!(env.balance(&env.vault(0)), 0, "the escrow is owed nothing: the seller was paid on all 10");
}

/// A trade is admitted only for this bond's tokens against its cash, between registered holders, delivered to
/// themselves.
#[test]
fn dvp_trade_between_unregistered_parties_is_not_admitted() {
    let mut env = setup();
    env.register("BH#1");
    env.place("BH#1", 10).unwrap();
    let outsider = Keypair::new();
    env.svm.airdrop(&outsider.pubkey(), 1_000_000_000).unwrap();
    env.holders.insert("outsider", outsider);
    let err = env.dvp_create_and_admit("BH#1", "outsider", 5, 1, 1_000 * USD).unwrap_err();
    assert!(err.contains("AccountNotInitialized") || err.contains("AccountOwnedByWrongProgram"), "no holder record for the buyer: {err}");
}
