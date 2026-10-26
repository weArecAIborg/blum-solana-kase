// LiteSVM harness shared by the integration tests (`include!`d, so each test file sees these items as its own):
// the devnet Token-2022 and Solana DvP programs, a bond with sample terms, holders, and one helper per instruction.

use std::collections::HashMap;

use anchor_lang::{
    prelude::{AccountMeta, Clock, Pubkey},
    solana_program::{instruction::Instruction, system_instruction, system_program},
    AccountDeserialize, InstructionData, ToAccountMetas,
};
use bond_math::calendar::{BusinessDayConvention, Calendar};
use bond_math::date::{Date, Weekday};
use bond_math::schedule::regular_schedule;
use bond_math::time::local_time_to_unix;
use bond_servicing::state::{CouponBasis, Event, HolderRecord};
use bond_servicing::{
    BondTerms, BOND_SEED, DVP_PROGRAM_ID, EVENT_SEED, EXTRA_ACCOUNT_METAS_SEED, HOLDER_SEED, OFFER_SEED, TENDER_SEED,
    VAULT_SEED,
};
use litesvm::LiteSVM;
use solana_keypair::Keypair;
use solana_message::{Message, VersionedMessage};
use solana_signer::Signer;
use solana_transaction::versioned::VersionedTransaction;

const USD: u64 = 1_000_000; // cash token: 6 decimals, like USDC
const UTC_OFFSET: i32 = 5 * 3_600;
const RECORD_TIME_LOCAL: i32 = 9 * 3_600;
const SATURDAY_SUNDAY: u8 = 0b110_0000;
const DAY: i64 = 86_400;
const TOKEN_2022_DEVNET: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/spl_token_2022_devnet.so");
/// Solana DvP (solana-foundation/dvp) as deployed on devnet (`solana program dump dvp34bdb...`, slot 479376863).
const DVP_DEVNET: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/dvp_swap_devnet.so");
const MEMO_PROGRAM: Pubkey = bond_servicing::settlement::MEMO_PROGRAM_ID;

fn token_2022() -> Pubkey {
    spl_token_2022_interface::ID
}
fn token_classic() -> Pubkey {
    spl_token_interface::ID
}
fn ata(owner: &Pubkey, mint: &Pubkey, token_program: &Pubkey) -> Pubkey {
    spl_associated_token_account_interface::address::get_associated_token_address_with_program_id(
        owner,
        mint,
        token_program,
    )
}
fn date(s: &str) -> Date {
    let p: Vec<&str> = s.split('-').collect();
    Date::new(p[0].parse().unwrap(), p[1].parse().unwrap(), p[2].parse().unwrap()).unwrap()
}

/// Terms of a test bond. Cash token units: 6 decimals.
struct Spec {
    issue: &'static str,
    maturity: &'static str,
    months: u32,
    face_units: u64,
    rate_bps: u32,
    periods_per_year: u32,
    redemption_price_bps: u32,
    /// Business days between the record day and the payment day (0 = record on payment day).
    record_days_before_payment: i64,
    /// Chain (= bond) time when the bond is created, a few days before issue.
    start: &'static str,
    /// Issue size in bonds: the most that may ever be placed.
    max_supply: u64,
}

/// The listing's example: 1,000 face, 10%, semi-annual, 2 years (issue date chosen for the test).
const B1: Spec = Spec {
    issue: "2026-03-06",
    maturity: "2028-03-06",
    months: 6,
    face_units: 1_000 * USD,
    rate_bps: 1_000,
    periods_per_year: 2,
    redemption_price_bps: 10_000,
    record_days_before_payment: 0,
    start: "2026-03-02",
    max_supply: 1_000_000, // chosen for the test: the listing's example gives no issue size
};

/// KASE MUM108_0015 (MEUKAM): 1,000 KZT face, 14.50%, annual, 06.03.2025 - 06.03.2034 (docs/bond-params.md).
const MUM108_0015: Spec = Spec {
    issue: "2025-03-06",
    maturity: "2034-03-06",
    months: 12,
    face_units: 1_000 * USD,
    rate_bps: 1_450,
    periods_per_year: 1,
    redemption_price_bps: 10_000,
    record_days_before_payment: 0,
    start: "2025-03-03",
    max_supply: 161_893_138, // volume in circulation (docs/bond-params.md)
};

fn sample_calendar_holidays() -> Vec<Date> {
    let fixture: serde_json::Value =
        serde_json::from_str(include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../crates/bond_math/tests/fixtures/reference.json"))).unwrap();
    fixture["kz_holidays"].as_array().unwrap().iter().map(|v| date(v.as_str().unwrap())).collect()
}

/// Payment days and record days from bond_math with the sample holiday list.
fn schedule_days(spec: &Spec) -> (Vec<i32>, Vec<i32>) {
    let holidays = sample_calendar_holidays();
    let cal = Calendar::new(&[Weekday::Saturday, Weekday::Sunday], &holidays).unwrap();
    let periods = regular_schedule(date(spec.issue), date(spec.maturity), spec.months, &cal, BusinessDayConvention::Following)
        .unwrap();
    let payment: Vec<i32> = periods.iter().map(|p| p.payment_date.days() as i32).collect();
    let record = periods
        .iter()
        .map(|p| {
            let mut d = p.payment_date;
            for _ in 0..spec.record_days_before_payment {
                d = cal.adjust(d.add_days(-1), BusinessDayConvention::Preceding);
            }
            d.days() as i32
        })
        .collect();
    (payment, record)
}

struct Env {
    svm: LiteSVM,
    program: Pubkey,
    registrar: Keypair,
    cash_authority: Keypair,
    keeper: Keypair,
    /// Classic SPL Token or Token-2022, the cash token's program.
    cash_program: Pubkey,
    cash_mint: Pubkey,
    /// SPL Memo program passed to payment instructions (None: not passed).
    memo: Option<Pubkey>,
    mint: Pubkey,
    bond: Pubkey,
    holders: HashMap<&'static str, Keypair>,
    payment_days: Vec<i32>,
    record_days: Vec<i32>,
}

impl Env {
    fn send(&mut self, ixs: &[Instruction], signers: &[&Keypair]) -> Result<Vec<String>, String> {
        let payer = signers[0].pubkey();
        let msg = Message::new_with_blockhash(ixs, Some(&payer), &self.svm.latest_blockhash());
        let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), signers).unwrap();
        let result = self.svm.send_transaction(tx);
        self.svm.expire_blockhash();
        match result {
            Ok(meta) => {
                count_landed(&meta.logs);
                Ok(meta.logs)
            }
            Err(e) => Err(format!("{:?}\n{}", e.err, e.meta.logs.join("\n"))),
        }
    }

    fn set_time(&mut self, unix: i64) {
        let mut clock: Clock = self.svm.get_sysvar();
        clock.unix_timestamp = unix;
        self.svm.set_sysvar(&clock);
    }

    fn record_moment(&self, period: usize) -> i64 {
        local_time_to_unix(
            Date::from_days(i64::from(self.record_days[period])),
            i64::from(RECORD_TIME_LOCAL),
            i64::from(UTC_OFFSET),
        )
    }

    fn payment_moment(&self, period: usize) -> i64 {
        local_time_to_unix(
            Date::from_days(i64::from(self.payment_days[period])),
            i64::from(RECORD_TIME_LOCAL),
            i64::from(UTC_OFFSET),
        )
    }

    fn holder(&self, name: &str) -> Pubkey {
        self.holders[name].pubkey()
    }

    fn holder_record(&self, owner: &Pubkey) -> Pubkey {
        Pubkey::find_program_address(&[HOLDER_SEED, self.mint.as_ref(), owner.as_ref()], &self.program).0
    }

    fn event_pda(&self, index: u16) -> Pubkey {
        Pubkey::find_program_address(&[EVENT_SEED, self.bond.as_ref(), &index.to_le_bytes()], &self.program).0
    }

    fn vault(&self, index: u16) -> Pubkey {
        Pubkey::find_program_address(&[VAULT_SEED, self.event_pda(index).as_ref()], &self.program).0
    }

    fn balance(&self, token_account: &Pubkey) -> u64 {
        let data = self.svm.get_account(token_account).map(|a| a.data).unwrap_or_default();
        if data.len() < 72 {
            return 0;
        }
        u64::from_le_bytes(data[64..72].try_into().unwrap())
    }

    fn bonds_of(&self, name: &str) -> u64 {
        self.balance(&ata(&self.holder(name), &self.mint, &token_2022()))
    }

    fn cash_of(&self, name: &str) -> u64 {
        self.balance(&ata(&self.holder(name), &self.cash_mint, &self.cash_program))
    }

    fn event(&self, index: u16) -> Event {
        let data = self.svm.get_account(&self.event_pda(index)).unwrap().data;
        Event::try_deserialize(&mut &data[..]).unwrap()
    }

    fn record(&self, name: &str) -> HolderRecord {
        let data = self.svm.get_account(&self.holder_record(&self.holder(name))).unwrap().data;
        HolderRecord::try_deserialize(&mut &data[..]).unwrap()
    }

    fn mint_supply(&self) -> u64 {
        let data = self.svm.get_account(&self.mint).unwrap().data;
        u64::from_le_bytes(data[36..44].try_into().unwrap())
    }

    fn periods(&self) -> u16 {
        self.payment_days.len() as u16
    }

    // ---------- instructions ----------

    fn register(&mut self, name: &'static str) {
        let owner = Keypair::new();
        self.svm.airdrop(&owner.pubkey(), 1_000_000_000).unwrap();
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::RegisterHolder {
                registrar: self.registrar.pubkey(),
                bond: self.bond,
                holder: self.holder_record(&owner.pubkey()),
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::RegisterHolder { owner: owner.pubkey() }.data(),
        };
        let create_atas = [
            spl_associated_token_account_interface::instruction::create_associated_token_account(
                &owner.pubkey(),
                &owner.pubkey(),
                &self.mint,
                &token_2022(),
            ),
            spl_associated_token_account_interface::instruction::create_associated_token_account(
                &owner.pubkey(),
                &owner.pubkey(),
                &self.cash_mint,
                &self.cash_program,
            ),
        ];
        let registrar = self.registrar.insecure_clone();
        self.send(&[ix], &[&registrar]).unwrap();
        self.send(&create_atas, &[&owner]).unwrap();
        self.holders.insert(name, owner);
    }

    fn place(&mut self, name: &str, amount: u64) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::Place {
                registrar: self.registrar.pubkey(),
                bond: self.bond,
                mint: self.mint,
                holder: self.holder_record(&owner),
                holder_bonds: ata(&owner, &self.mint, &token_2022()),
                token_program: token_2022(),
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::Place { amount }.data(),
        };
        let registrar = self.registrar.insecure_clone();
        self.send(&[ix], &[&registrar])
    }

    /// Token-2022 transfer_checked plus the hook's extra accounts: bond, source holder, destination holder, and the
    /// seller's record when the source is a trade escrow (`escrow_of` of the source record; zero for a holder, which
    /// resolves to a record that does not exist).
    fn transfer_ix(&self, from: &Pubkey, to: &Pubkey, amount: u64) -> Instruction {
        let source = ata(from, &self.mint, &token_2022());
        let destination = ata(to, &self.mint, &token_2022());
        let escrow_of = self
            .svm
            .get_account(&self.holder_record(from))
            .and_then(|a| HolderRecord::try_deserialize(&mut &a.data[..]).ok())
            .map(|r| r.escrow_of)
            .unwrap_or_default();
        let mut ix = spl_token_2022_interface::instruction::transfer_checked(
            &token_2022(),
            &source,
            &self.mint,
            &destination,
            from,
            &[],
            amount,
            0,
        )
        .unwrap();
        let extra_metas =
            Pubkey::find_program_address(&[EXTRA_ACCOUNT_METAS_SEED, self.mint.as_ref()], &self.program).0;
        ix.accounts.extend([
            AccountMeta::new_readonly(self.bond, false),
            AccountMeta::new(self.holder_record(from), false),
            AccountMeta::new(self.holder_record(to), false),
            AccountMeta::new(self.holder_record(&escrow_of), false),
            AccountMeta::new_readonly(self.program, false),
            AccountMeta::new_readonly(extra_metas, false),
        ]);
        ix
    }

    fn transfer(&mut self, from: &str, to: &str, amount: u64) -> Result<Vec<String>, String> {
        let ix = self.transfer_ix(&self.holder(from), &self.holder(to), amount);
        let signer = self.holders[from].insecure_clone();
        self.send(&[ix], &[&signer])
    }

    /// Anyone may open an event once its record moment has passed; the keeper does it here.
    fn open_event(&mut self, index: u16) -> Result<Vec<String>, String> {
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::OpenEvent {
                payer: self.keeper.pubkey(),
                bond: self.bond,
                event: self.event_pda(index),
                cash_mint: self.cash_mint,
                vault: self.vault(index),
                cash_token_program: self.cash_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::OpenEvent { index }.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    /// The issuer's bank (cash authority) credits the event's vault: the simulated fiat leg.
    fn fund(&mut self, index: u16, amount: u64) {
        let ix = spl_token_2022_interface::instruction::mint_to(
            &self.cash_program,
            &self.cash_mint,
            &self.vault(index),
            &self.cash_authority.pubkey(),
            &[],
            amount,
        )
        .unwrap();
        let bank = self.cash_authority.insecure_clone();
        self.send(&[ix], &[&bank]).unwrap();
    }

    fn confirm_funding(&mut self, index: u16) -> Result<Vec<String>, String> {
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::ConfirmFunding { event: self.event_pda(index), vault: self.vault(index) }
                .to_account_metas(None),
            data: bond_servicing::instruction::ConfirmFunding {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    fn pay_coupon(&mut self, index: u16, name: &str) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::PayCoupon {
                payer: self.keeper.pubkey(),
                bond: self.bond,
                event: self.event_pda(index),
                holder: self.holder_record(&owner),
                owner,
                vault: self.vault(index),
                cash_mint: self.cash_mint,
                holder_cash: ata(&owner, &self.cash_mint, &self.cash_program),
                memo_program: self.memo,
                cash_token_program: self.cash_program,
                associated_token_program: spl_associated_token_account_interface::program::ID,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::PayCoupon {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    fn redeem(&mut self, name: &str) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let index = self.periods();
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::RedeemHolder {
                payer: self.keeper.pubkey(),
                bond: self.bond,
                mint: self.mint,
                event: self.event_pda(index),
                holder: self.holder_record(&owner),
                owner,
                holder_bonds: ata(&owner, &self.mint, &token_2022()),
                vault: self.vault(index),
                cash_mint: self.cash_mint,
                holder_cash: ata(&owner, &self.cash_mint, &self.cash_program),
                memo_program: self.memo,
                token_program: token_2022(),
                cash_token_program: self.cash_program,
                associated_token_program: spl_associated_token_account_interface::program::ID,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::RedeemHolder {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    // ---------- offers (buyback, tender) ----------

    fn offer_pda(&self, id: u16) -> Pubkey {
        Pubkey::find_program_address(&[OFFER_SEED, self.bond.as_ref(), &id.to_le_bytes()], &self.program).0
    }

    fn offer_vault(&self, id: u16) -> Pubkey {
        Pubkey::find_program_address(&[VAULT_SEED, self.offer_pda(id).as_ref()], &self.program).0
    }

    fn tender_pda(&self, id: u16, owner: &Pubkey) -> Pubkey {
        Pubkey::find_program_address(&[TENDER_SEED, self.offer_pda(id).as_ref(), owner.as_ref()], &self.program).0
    }

    /// Create an offer and fund it with exactly its budget (price x maximum), then confirm.
    #[allow(clippy::too_many_arguments)]
    fn create_offer(&mut self, id: u16, pro_rata: bool, price: u64, max: u64, open: i64, close: i64) -> Result<Vec<String>, String> {
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::CreateOffer {
                registrar: self.registrar.pubkey(),
                bond: self.bond,
                offer: self.offer_pda(id),
                cash_mint: self.cash_mint,
                vault: self.offer_vault(id),
                cash_token_program: self.cash_program,
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::CreateOffer {
                id,
                pro_rata,
                price_units: price,
                max_quantity: max,
                open_time: open,
                close_time: close,
            }
            .data(),
        };
        let registrar = self.registrar.insecure_clone();
        self.send(&[ix], &[&registrar])
    }

    fn fund_offer(&mut self, id: u16, amount: u64) {
        let ix = spl_token_2022_interface::instruction::mint_to(
            &self.cash_program,
            &self.cash_mint,
            &self.offer_vault(id),
            &self.cash_authority.pubkey(),
            &[],
            amount,
        )
        .unwrap();
        let bank = self.cash_authority.insecure_clone();
        self.send(&[ix], &[&bank]).unwrap();
    }

    fn confirm_offer_funding(&mut self, id: u16) -> Result<Vec<String>, String> {
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::ConfirmOfferFunding { offer: self.offer_pda(id), vault: self.offer_vault(id) }
                .to_account_metas(None),
            data: bond_servicing::instruction::ConfirmOfferFunding {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    fn retirement_accounts(&self, id: u16, owner: Pubkey, payer: Pubkey) -> bond_servicing::accounts::Retirement {
        bond_servicing::accounts::Retirement {
            payer,
            bond: self.bond,
            mint: self.mint,
            holder: self.holder_record(&owner),
            owner,
            holder_bonds: ata(&owner, &self.mint, &token_2022()),
            vault: self.offer_vault(id),
            cash_mint: self.cash_mint,
            holder_cash: ata(&owner, &self.cash_mint, &self.cash_program),
            memo_program: self.memo,
            token_program: token_2022(),
            cash_token_program: self.cash_program,
            associated_token_program: spl_associated_token_account_interface::program::ID,
            system_program: system_program::ID,
        }
    }

    fn sell(&mut self, id: u16, name: &str, quantity: u64) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::SellToOffer {
                retirement: self.retirement_accounts(id, owner, owner),
                seller: owner,
                offer: self.offer_pda(id),
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::SellToOffer { quantity }.data(),
        };
        let signer = self.holders[name].insecure_clone();
        self.send(&[ix], &[&signer])
    }

    fn change_tender(&mut self, id: u16, name: &str, quantity: u64, withdraw: bool) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::ChangeTender {
                owner,
                bond: self.bond,
                offer: self.offer_pda(id),
                holder: self.holder_record(&owner),
                tender: self.tender_pda(id, &owner),
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::ChangeTender { quantity, withdraw }.data(),
        };
        let signer = self.holders[name].insecure_clone();
        self.send(&[ix], &[&signer])
    }

    fn settle(&mut self, id: u16, name: &str) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::SettleTender {
                retirement: self.retirement_accounts(id, owner, self.keeper.pubkey()),
                offer: self.offer_pda(id),
                tender: self.tender_pda(id, &owner),
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::SettleTender {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    fn resolve(&mut self, id: u16, name: &str) -> Result<Vec<String>, String> {
        let owner = self.holder(name);
        let ix = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::ResolveTender {
                bond: self.bond,
                offer: self.offer_pda(id),
                holder: self.holder_record(&owner),
                tender: self.tender_pda(id, &owner),
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::ResolveTender {}.data(),
        };
        let keeper = self.keeper.insecure_clone();
        self.send(&[ix], &[&keeper])
    }

    // ---------- Solana DvP trades (solana-foundation/dvp): raw instructions by discriminator ----------

    /// The trade's `SwapDvp` PDA and its two escrow associated token accounts.
    fn dvp_pda(&self, seller: &Pubkey, buyer: &Pubkey, nonce: u64) -> Pubkey {
        Pubkey::find_program_address(
            &[b"dvp", self.registrar.pubkey().as_ref(), seller.as_ref(), buyer.as_ref(), self.mint.as_ref(), self.cash_mint.as_ref(), &nonce.to_le_bytes()],
            &DVP_PROGRAM_ID,
        )
        .0
    }

    /// `CreateDvp` (discriminator 0) by the registrar as payer and settlement authority: the seller delivers `quantity`
    /// bonds, the buyer `cash`; then `admit_trade`, in the same transaction, so the escrow may hold bonds.
    fn dvp_create_and_admit(&mut self, seller: &str, buyer: &str, nonce: u64, quantity: u64, cash: u64) -> Result<Pubkey, String> {
        let expiry = self.now() + 365 * DAY - 60;
        self.dvp_create_and_admit_until(seller, buyer, nonce, quantity, cash, expiry)
    }

    /// `dvp_create_and_admit` with the trade's expiry (chain time) given.
    fn dvp_create_and_admit_until(&mut self, seller: &str, buyer: &str, nonce: u64, quantity: u64, cash: u64, expiry: i64) -> Result<Pubkey, String> {
        let (a, b) = (self.holder(seller), self.holder(buyer));
        let (swap, ixs) = self.dvp_create_and_admit_ixs(&a, &b, nonce, quantity, cash, expiry, &a);
        let (registrar, seller_kp) = (self.registrar.insecure_clone(), self.holders[seller].insecure_clone());
        self.send(&ixs, &[&registrar, &seller_kp])?;
        Ok(swap)
    }

    /// `CreateDvp` by the registrar (standing in for the venue) and `admit_trade` signed by `admitter` as the seller,
    /// who also pays the escrow record's deposit.
    #[allow(clippy::too_many_arguments)]
    fn dvp_create_and_admit_ixs(&self, a: &Pubkey, b: &Pubkey, nonce: u64, quantity: u64, cash: u64, expiry: i64, admitter: &Pubkey) -> (Pubkey, [Instruction; 2]) {
        let (a, b) = (*a, *b);
        let swap = self.dvp_pda(&a, &b, nonce);
        let tombstone = Pubkey::find_program_address(&[b"nonce", swap.as_ref()], &DVP_PROGRAM_ID).0;
        let mut data = vec![0u8];
        data.extend_from_slice(&quantity.to_le_bytes());
        data.extend_from_slice(&cash.to_le_bytes());
        data.extend_from_slice(&expiry.to_le_bytes());
        data.extend_from_slice(&nonce.to_le_bytes());
        data.extend_from_slice(&[0, 0, 0, 0]); // ref_string, destinations, earliest: all None
        let create = Instruction {
            program_id: DVP_PROGRAM_ID,
            accounts: vec![
                AccountMeta::new(self.registrar.pubkey(), true),
                AccountMeta::new(swap, false),
                AccountMeta::new(tombstone, false),
                AccountMeta::new_readonly(self.registrar.pubkey(), false),
                AccountMeta::new_readonly(a, false),
                AccountMeta::new_readonly(b, false),
                AccountMeta::new_readonly(self.mint, false),
                AccountMeta::new_readonly(self.cash_mint, false),
                AccountMeta::new(ata(&swap, &self.mint, &token_2022()), false),
                AccountMeta::new(ata(&swap, &self.cash_mint, &self.cash_program), false),
                AccountMeta::new_readonly(system_program::ID, false),
                AccountMeta::new_readonly(token_2022(), false),
                AccountMeta::new_readonly(self.cash_program, false),
                AccountMeta::new_readonly(spl_associated_token_account_interface::program::ID, false),
            ],
            data,
        };
        let admit = Instruction {
            program_id: self.program,
            accounts: bond_servicing::accounts::AdmitTrade {
                payer: *admitter,
                seller_wallet: *admitter,
                bond: self.bond,
                swap_dvp: swap,
                seller: self.holder_record(&a),
                buyer: self.holder_record(&b),
                escrow: self.holder_record(&swap),
                system_program: system_program::ID,
            }
            .to_account_metas(None),
            data: bond_servicing::instruction::AdmitTrade {}.data(),
        };
        (swap, [create, admit])
    }

    /// The seller funds the bond leg: a plain hooked transfer to the escrow.
    fn dvp_fund_bonds(&mut self, seller: &str, swap: &Pubkey, quantity: u64) -> Result<Vec<String>, String> {
        let ix = self.transfer_ix(&self.holder(seller), swap, quantity);
        let signer = self.holders[seller].insecure_clone();
        self.send(&[ix], &[&signer])
    }

    /// The buyer funds the cash leg: a plain transfer to the escrow.
    fn dvp_fund_cash(&mut self, buyer: &str, swap: &Pubkey, cash: u64) -> Result<Vec<String>, String> {
        let owner = self.holder(buyer);
        let ix = spl_token_2022_interface::instruction::transfer_checked(
            &self.cash_program, &ata(&owner, &self.cash_mint, &self.cash_program), &self.cash_mint,
            &ata(swap, &self.cash_mint, &self.cash_program), &owner, &[], cash, 6,
        )
        .unwrap();
        let signer = self.holders[buyer].insecure_clone();
        self.send(&[ix], &[&signer])
    }

    /// The hook's extra accounts for a transfer out of the escrow: bond, escrow record, destination record, seller record.
    fn dvp_hook_extras(&self, swap: &Pubkey, to: &Pubkey, seller: &Pubkey) -> Vec<AccountMeta> {
        let extra_metas = Pubkey::find_program_address(&[EXTRA_ACCOUNT_METAS_SEED, self.mint.as_ref()], &self.program).0;
        vec![
            AccountMeta::new_readonly(self.bond, false),
            AccountMeta::new(self.holder_record(swap), false),
            AccountMeta::new(self.holder_record(to), false),
            AccountMeta::new(self.holder_record(seller), false),
            AccountMeta::new_readonly(self.program, false),
            AccountMeta::new_readonly(extra_metas, false),
        ]
    }

    /// `SettleDvp` (discriminator 2) by the registrar: bonds to the buyer and cash to the seller, atomically. The bond
    /// leg (A) carries the hook's extras; the cash leg (B) none.
    fn dvp_settle(&mut self, seller: &str, buyer: &str, swap: &Pubkey) -> Result<Vec<String>, String> {
        let (a, b) = (self.holder(seller), self.holder(buyer));
        let extras = self.dvp_hook_extras(swap, &b, &a);
        let mut accounts = vec![
            AccountMeta::new(self.registrar.pubkey(), true),
            AccountMeta::new(*swap, false),
            AccountMeta::new_readonly(self.mint, false),
            AccountMeta::new_readonly(self.cash_mint, false),
            AccountMeta::new(ata(swap, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(swap, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new(ata(&a, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new(ata(&b, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(&a, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(&b, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new_readonly(token_2022(), false),
            AccountMeta::new_readonly(self.cash_program, false),
            AccountMeta::new_readonly(MEMO_PROGRAM, false),
        ];
        let data = vec![2u8, extras.len() as u8];
        accounts.extend(extras);
        let ix = Instruction { program_id: DVP_PROGRAM_ID, accounts, data };
        let registrar = self.registrar.insecure_clone();
        self.send(&[ix], &[&registrar])
    }

    /// `CancelDvp` (discriminator 3) by the registrar: both legs back to their depositors.
    fn dvp_cancel(&mut self, seller: &str, buyer: &str, swap: &Pubkey) -> Result<Vec<String>, String> {
        let (a, b) = (self.holder(seller), self.holder(buyer));
        let extras = self.dvp_hook_extras(swap, &a, &a);
        let mut accounts = vec![
            AccountMeta::new(self.registrar.pubkey(), true),
            AccountMeta::new(*swap, false),
            AccountMeta::new_readonly(self.mint, false),
            AccountMeta::new_readonly(self.cash_mint, false),
            AccountMeta::new(ata(swap, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(swap, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new(ata(&a, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(&b, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new_readonly(token_2022(), false),
            AccountMeta::new_readonly(self.cash_program, false),
            AccountMeta::new_readonly(MEMO_PROGRAM, false),
        ];
        let data = vec![3u8, extras.len() as u8];
        accounts.extend(extras);
        let ix = Instruction { program_id: DVP_PROGRAM_ID, accounts, data };
        let registrar = self.registrar.insecure_clone();
        self.send(&[ix], &[&registrar])
    }

    /// `RejectDvp` (discriminator 4) by one of the parties: both legs back to their depositors, the trade closed. Same
    /// accounts as `CancelDvp`, the party signing in place of the registrar.
    #[allow(dead_code)] // used by the generated tests
    fn dvp_reject(&mut self, by: &str, seller: &str, buyer: &str, swap: &Pubkey) -> Result<Vec<String>, String> {
        let (a, b, signer) = (self.holder(seller), self.holder(buyer), self.holder(by));
        let extras = self.dvp_hook_extras(swap, &a, &a);
        let mut accounts = vec![
            AccountMeta::new(signer, true),
            AccountMeta::new(*swap, false),
            AccountMeta::new_readonly(self.mint, false),
            AccountMeta::new_readonly(self.cash_mint, false),
            AccountMeta::new(ata(swap, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(swap, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new(ata(&a, &self.mint, &token_2022()), false),
            AccountMeta::new(ata(&b, &self.cash_mint, &self.cash_program), false),
            AccountMeta::new_readonly(token_2022(), false),
            AccountMeta::new_readonly(self.cash_program, false),
            AccountMeta::new_readonly(MEMO_PROGRAM, false),
        ];
        let data = vec![4u8, extras.len() as u8];
        accounts.extend(extras);
        let ix = Instruction { program_id: DVP_PROGRAM_ID, accounts, data };
        let party = self.holders[by].insecure_clone();
        self.send(&[ix], &[&party])
    }

    /// `ReclaimDvp` (discriminator 1) by one party: its own leg back, the trade stays open. The seller's bond leg
    /// carries the hook's extras; the buyer's cash leg none.
    #[allow(dead_code)] // used by the generated tests
    fn dvp_reclaim(&mut self, by: &str, swap: &Pubkey, bonds: bool) -> Result<Vec<String>, String> {
        let signer = self.holder(by);
        let (mint, program) = if bonds { (self.mint, token_2022()) } else { (self.cash_mint, self.cash_program) };
        let mut accounts = vec![
            AccountMeta::new_readonly(signer, true),
            AccountMeta::new_readonly(*swap, false),
            AccountMeta::new_readonly(mint, false),
            AccountMeta::new(ata(swap, &mint, &program), false),
            AccountMeta::new(ata(&signer, &mint, &program), false),
            AccountMeta::new_readonly(program, false),
            AccountMeta::new_readonly(MEMO_PROGRAM, false),
        ];
        if bonds {
            accounts.extend(self.dvp_hook_extras(swap, &signer, &signer));
        }
        let ix = Instruction { program_id: DVP_PROGRAM_ID, accounts, data: vec![1u8] };
        let party = self.holders[by].insecure_clone();
        self.send(&[ix], &[&party])
    }

    /// Give a holder cash from the bank (the cash token's mint authority).
    fn give_cash(&mut self, name: &str, amount: u64) {
        let owner = self.holder(name);
        let ix = spl_token_2022_interface::instruction::mint_to(
            &self.cash_program, &self.cash_mint, &ata(&owner, &self.cash_mint, &self.cash_program), &self.cash_authority.pubkey(), &[], amount,
        )
        .unwrap();
        let bank = self.cash_authority.insecure_clone();
        self.send(&[ix], &[&bank]).unwrap();
    }

    fn now(&self) -> i64 {
        self.svm.get_sysvar::<Clock>().unix_timestamp
    }

    /// Open, fund exactly the budget, confirm.
    fn open_and_fund(&mut self, index: u16) {
        self.open_event(index).unwrap();
        let budget = self.event(index).budget;
        self.fund(index, budget);
        self.confirm_funding(index).unwrap();
    }

    /// Open, fund and pay one coupon to every listed holder with a position at its record moment.
    fn run_coupon(&mut self, index: u16, holders: &[&'static str]) {
        self.open_and_fund(index);
        for h in holders {
            if self.record(h).position.at_moment(index) > 0 {
                self.pay_coupon(index, h).unwrap();
            }
        }
    }

    fn run_redemption(&mut self, holders: &[&'static str]) {
        self.open_and_fund(self.periods());
        for h in holders {
            if self.record(h).position.quantity > 0 {
                self.redeem(h).unwrap();
            }
        }
    }
}

/// Classic SPL Token mint with 6 decimals; the "bank" is mint and freeze authority (mirrors USDC).
fn create_cash_mint(svm: &mut LiteSVM, payer: &Keypair, bank: &Pubkey, program: &Pubkey) -> Pubkey {
    let mint = Keypair::new();
    let rent = svm.minimum_balance_for_rent_exemption(82);
    let ixs = [
        system_instruction::create_account(&payer.pubkey(), &mint.pubkey(), rent, 82, program),
        spl_token_2022_interface::instruction::initialize_mint2(program, &mint.pubkey(), bank, Some(bank), 6).unwrap(),
    ];
    let msg = Message::new_with_blockhash(&ixs, Some(&payer.pubkey()), &svm.latest_blockhash());
    let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), &[payer, &mint]).unwrap();
    svm.send_transaction(tx).unwrap();
    svm.expire_blockhash();
    mint.pubkey()
}

fn new_svm() -> LiteSVM {
    let mut svm = LiteSVM::new();
    svm.add_program_from_file(token_2022(), TOKEN_2022_DEVNET).unwrap();
    svm.add_program_from_file(DVP_PROGRAM_ID, DVP_DEVNET).unwrap();
    let bytes = include_bytes!(concat!(env!("CARGO_TARGET_TMPDIR"), "/../deploy/bond_servicing.so"));
    svm.add_program(bond_servicing::id(), bytes).unwrap();
    svm
}

fn terms_for(spec: &Spec, payment_days: &[i32], record_days: &[i32], start: i64) -> BondTerms {
    BondTerms {
        face_units: spec.face_units,
        rate_bps: spec.rate_bps,
        basis: CouponBasis::FixedPerPeriod { periods_per_year: spec.periods_per_year },
        redemption_price_bps: spec.redemption_price_bps,
        issue_day: date(spec.issue).days() as i32,
        maturity_day: date(spec.maturity).days() as i32,
        months_per_period: spec.months,
        payment_days: payment_days.to_vec(),
        record_days: record_days.to_vec(),
        record_seconds_local: RECORD_TIME_LOCAL,
        payment_seconds_local: RECORD_TIME_LOCAL,
        utc_offset_seconds: UTC_OFFSET,
        weekend_days: SATURDAY_SUNDAY,
        bond_origin: start,
        chain_seconds_per_bond_day: 86_400,
        max_supply: spec.max_supply,
    }
}

fn create_bond_ix(registrar: &Pubkey, mint: &Pubkey, cash_mint: &Pubkey, terms: BondTerms) -> Instruction {
    let program = bond_servicing::id();
    let bond = Pubkey::find_program_address(&[BOND_SEED, mint.as_ref()], &program).0;
    let extra_account_meta_list = Pubkey::find_program_address(&[EXTRA_ACCOUNT_METAS_SEED, mint.as_ref()], &program).0;
    Instruction {
        program_id: program,
        accounts: bond_servicing::accounts::CreateBond {
            registrar: *registrar,
            mint: *mint,
            bond,
            cash_mint: *cash_mint,
            extra_account_meta_list,
            token_program: token_2022(),
            system_program: system_program::ID,
        }
        .to_account_metas(None),
        data: bond_servicing::instruction::CreateBond { terms }.data(),
    }
}

fn setup() -> Env {
    setup_bond(&B1)
}

fn setup_bond(spec: &Spec) -> Env {
    setup_bond_with(spec, token_classic())
}

fn setup_bond_with(spec: &Spec, cash_program: Pubkey) -> Env {
    setup_bond_with_cash(spec, cash_program, create_cash_mint)
}

type CashMint = fn(&mut LiteSVM, &Keypair, &Pubkey, &Pubkey) -> Pubkey;

fn setup_bond_with_cash(spec: &Spec, cash_program: Pubkey, make_cash_mint: CashMint) -> Env {
    let program = bond_servicing::id();
    let mut svm = new_svm();
    let registrar = Keypair::new();
    let cash_authority = Keypair::new();
    let keeper = Keypair::new();
    for k in [&registrar, &cash_authority, &keeper] {
        svm.airdrop(&k.pubkey(), 10_000_000_000).unwrap();
    }
    let (payment_days, record_days) = schedule_days(spec);

    // Real time (86_400 chain seconds per bond day); the bond clock equals the chain clock.
    let start = local_time_to_unix(date(spec.start), 12 * 3_600, i64::from(UTC_OFFSET));
    let mut clock: Clock = svm.get_sysvar();
    clock.unix_timestamp = start;
    svm.set_sysvar(&clock);

    let cash_mint = make_cash_mint(&mut svm, &registrar, &cash_authority.pubkey(), &cash_program);
    let mint = Keypair::new();
    let bond = Pubkey::find_program_address(&[BOND_SEED, mint.pubkey().as_ref()], &program).0;
    let create = create_bond_ix(
        &registrar.pubkey(),
        &mint.pubkey(),
        &cash_mint,
        terms_for(spec, &payment_days, &record_days, start),
    );
    let mut env = Env {
        svm,
        program,
        registrar: registrar.insecure_clone(),
        cash_authority,
        keeper,
        cash_program,
        cash_mint,
        memo: None,
        mint: mint.pubkey(),
        bond,
        holders: HashMap::new(),
        payment_days,
        record_days,
    };
    env.send(&[compute_unit_limit(1_400_000), create], &[&registrar, &mint]).unwrap();
    env
}

/// Instructions that landed, by the name programs log (`Instruction: X`), counted only when
/// `COUNT_LANDED_INSTRUCTIONS` is set: shows which instructions a run of generated tests actually executed.
static LANDED: std::sync::Mutex<Vec<(String, u64)>> = std::sync::Mutex::new(Vec::new());

fn count_landed(logs: &[String]) {
    if std::env::var("COUNT_LANDED_INSTRUCTIONS").is_err() {
        return;
    }
    let mut landed = LANDED.lock().unwrap();
    for name in logs.iter().filter_map(|l| l.strip_prefix("Program log: Instruction: ")) {
        match landed.iter().position(|(n, _)| n == name) {
            Some(i) => landed[i].1 += 1,
            None => landed.push((name.to_string(), 1)),
        }
    }
}

/// ComputeBudget `SetComputeUnitLimit`: a transaction gets 200,000 compute units unless it asks for more (at most
/// 1,400,000). Creating a bond with many coupon periods needs more than the default.
fn compute_unit_limit(units: u32) -> Instruction {
    let mut data = vec![2u8];
    data.extend_from_slice(&units.to_le_bytes());
    Instruction { program_id: Pubkey::from_str_const("ComputeBudget111111111111111111111111111111"), accounts: vec![], data }
}
