//! Probe: can a holder burn bonds outside the program once the mint has Token-2022's permissioned burn?
//!
//! Runs the Token-2022 program dumped from devnet (`tests/fixtures/spl_token_2022_devnet.so`, slot 503153936,
//! sha256 b781b71d...), because the Token-2022 bundled with LiteSVM 0.10 predates the PermissionedBurn extension.
//! The mint carries the bond's extension set: permanent delegate, transfer hook and permissioned burn.
//! Instruction layouts are from solana-program/token-2022 (interface 3.1.2, program 11.1.0):
//!   TokenInstruction::PermissionedBurnExtension = 46; sub-instruction Initialize = 0, BurnChecked = 2;
//!   ExtensionType::PermissionedBurn config = 32-byte authority; AuthorityType::PermissionedBurn = 17.

use anchor_lang::prelude::{AccountMeta, Pubkey};
use anchor_lang::solana_program::{instruction::Instruction, system_instruction};
use litesvm::LiteSVM;
use solana_keypair::Keypair;
use solana_message::{Message, VersionedMessage};
use solana_signer::Signer;
use solana_transaction::versioned::VersionedTransaction;
use spl_token_2022_interface::extension::ExtensionType;
use spl_token_2022_interface::state::Mint;

const TOKEN_2022_DEVNET: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/spl_token_2022_devnet.so");
const PERMISSIONED_BURN_EXTENSION: u8 = 46;
const PERMISSIONED_BURN_INITIALIZE: u8 = 0;
const PERMISSIONED_BURN_BURN_CHECKED: u8 = 2;
const SET_AUTHORITY: u8 = 6;
const AUTHORITY_TYPE_PERMISSIONED_BURN: u8 = 17;
/// TokenError::InvalidInstruction: what Token-2022 returns for a standard burn on a permissioned-burn mint.
const TOKEN_ERROR_INVALID_INSTRUCTION: u32 = 12;

fn token_2022() -> Pubkey {
    spl_token_2022_interface::ID
}

fn ata(owner: &Pubkey, mint: &Pubkey) -> Pubkey {
    spl_associated_token_account_interface::address::get_associated_token_address_with_program_id(
        owner,
        mint,
        &token_2022(),
    )
}

fn permissioned_burn_initialize(mint: &Pubkey, authority: &Pubkey) -> Instruction {
    let mut data = vec![PERMISSIONED_BURN_EXTENSION, PERMISSIONED_BURN_INITIALIZE];
    data.extend_from_slice(authority.as_ref());
    Instruction { program_id: token_2022(), accounts: vec![AccountMeta::new(*mint, false)], data }
}

fn permissioned_burn_checked(account: &Pubkey, mint: &Pubkey, burn_authority: &Pubkey, owner: &Pubkey, amount: u64) -> Instruction {
    let mut data = vec![PERMISSIONED_BURN_EXTENSION, PERMISSIONED_BURN_BURN_CHECKED];
    data.extend_from_slice(&amount.to_le_bytes());
    data.push(0); // decimals
    Instruction {
        program_id: token_2022(),
        accounts: vec![
            AccountMeta::new(*account, false),
            AccountMeta::new(*mint, false),
            AccountMeta::new_readonly(*burn_authority, true),
            AccountMeta::new_readonly(*owner, true),
        ],
        data,
    }
}

/// SetAuthority(PermissionedBurn -> None), signed by `current`.
fn clear_burn_authority(mint: &Pubkey, current: &Pubkey) -> Instruction {
    Instruction {
        program_id: token_2022(),
        accounts: vec![AccountMeta::new(*mint, false), AccountMeta::new_readonly(*current, true)],
        data: vec![SET_AUTHORITY, AUTHORITY_TYPE_PERMISSIONED_BURN, 0],
    }
}

struct Probe {
    svm: LiteSVM,
    payer: Keypair,
    /// Stands in for the Bond PDA: mint authority, permanent delegate and permissioned burn authority.
    bond: Keypair,
    holder: Keypair,
    mint: Pubkey,
}

impl Probe {
    fn send(&mut self, ixs: &[Instruction], signers: &[&Keypair]) -> Result<(), String> {
        let msg = Message::new_with_blockhash(ixs, Some(&signers[0].pubkey()), &self.svm.latest_blockhash());
        let tx = VersionedTransaction::try_new(VersionedMessage::Legacy(msg), signers).unwrap();
        let result = self.svm.send_transaction(tx);
        self.svm.expire_blockhash();
        result.map(|_| ()).map_err(|e| format!("{:?}\n{}", e.err, e.meta.logs.join("\n")))
    }

    fn balance(&self) -> u64 {
        let data = self.svm.get_account(&ata(&self.holder.pubkey(), &self.mint)).unwrap().data;
        u64::from_le_bytes(data[64..72].try_into().unwrap())
    }

    fn setup() -> Probe {
        let mut svm = LiteSVM::new();
        svm.add_program_from_file(token_2022(), TOKEN_2022_DEVNET).unwrap();
        let payer = Keypair::new();
        let bond = Keypair::new();
        let holder = Keypair::new();
        let mint = Keypair::new();
        for k in [&payer, &holder] {
            svm.airdrop(&k.pubkey(), 10_000_000_000).unwrap();
        }
        let space = ExtensionType::try_calculate_account_len::<Mint>(&[
            ExtensionType::PermanentDelegate,
            ExtensionType::TransferHook,
        ])
        .unwrap()
            + 4 // TLV header of the PermissionedBurn extension
            + 32; // its config: the burn authority
        let rent = svm.minimum_balance_for_rent_exemption(space);
        let hook_program = Pubkey::new_unique();
        let mut p = Probe { svm, payer, bond, holder, mint: mint.pubkey() };
        let (payer, bond, holder, m) = (p.payer.pubkey(), p.bond.pubkey(), p.holder.pubkey(), p.mint);
        let create = vec![
            system_instruction::create_account(&payer, &m, rent, space as u64, &token_2022()),
            spl_token_2022_interface::instruction::initialize_permanent_delegate(&token_2022(), &m, &bond).unwrap(),
            spl_token_2022_interface::extension::transfer_hook::instruction::initialize(
                &token_2022(),
                &m,
                Some(bond),
                Some(hook_program),
            )
            .unwrap(),
            permissioned_burn_initialize(&m, &bond),
            spl_token_2022_interface::instruction::initialize_mint2(&token_2022(), &m, &bond, None, 0).unwrap(),
            spl_associated_token_account_interface::instruction::create_associated_token_account(
                &payer,
                &holder,
                &m,
                &token_2022(),
            ),
            spl_token_2022_interface::instruction::mint_to(&token_2022(), &m, &ata(&holder, &m), &bond, &[], 5).unwrap(),
        ];
        let (pk, bk) = (p.payer.insecure_clone(), p.bond.insecure_clone());
        p.send(&create, &[&pk, &mint, &bk]).expect("mint with permanent delegate, transfer hook and permissioned burn");
        assert_eq!(p.balance(), 5);
        p
    }
}

fn custom_error(err: &str, code: u32) -> bool {
    err.contains(&format!("Custom({code})"))
}

#[test]
fn holder_cannot_burn_with_standard_burn() {
    let mut p = Probe::setup();
    let (h, m) = (p.holder.pubkey(), p.mint);
    let burn = spl_token_2022_interface::instruction::burn_checked(&token_2022(), &ata(&h, &m), &m, &h, &[], 1, 0).unwrap();
    let holder = p.holder.insecure_clone();
    let err = p.send(&[burn], &[&holder]).unwrap_err();
    assert!(custom_error(&err, TOKEN_ERROR_INVALID_INSTRUCTION), "{err}");
    assert_eq!(p.balance(), 5);
}

#[test]
fn permanent_delegate_cannot_use_standard_burn_either() {
    let mut p = Probe::setup();
    let (h, m, b) = (p.holder.pubkey(), p.mint, p.bond.pubkey());
    let burn = spl_token_2022_interface::instruction::burn_checked(&token_2022(), &ata(&h, &m), &m, &b, &[], 1, 0).unwrap();
    let (payer, bond) = (p.payer.insecure_clone(), p.bond.insecure_clone());
    let err = p.send(&[burn], &[&payer, &bond]).unwrap_err();
    assert!(custom_error(&err, TOKEN_ERROR_INVALID_INSTRUCTION), "{err}");
    assert_eq!(p.balance(), 5);
}

#[test]
fn holder_cannot_permissioned_burn_without_the_burn_authority() {
    let mut p = Probe::setup();
    let (h, m) = (p.holder.pubkey(), p.mint);
    // The holder names itself as the burn authority.
    let burn = permissioned_burn_checked(&ata(&h, &m), &m, &h, &h, 1);
    let holder = p.holder.insecure_clone();
    assert!(p.send(&[burn], &[&holder]).is_err());
    assert_eq!(p.balance(), 5);
}

#[test]
fn holder_cannot_remove_the_burn_authority() {
    let mut p = Probe::setup();
    let (h, m) = (p.holder.pubkey(), p.mint);
    let holder = p.holder.insecure_clone();
    assert!(p.send(&[clear_burn_authority(&m, &h)], &[&holder]).is_err());
    let burn = spl_token_2022_interface::instruction::burn_checked(&token_2022(), &ata(&h, &m), &m, &h, &[], 1, 0).unwrap();
    assert!(p.send(&[burn], &[&holder]).is_err(), "standard burn must still be refused");
}

/// The redemption path: the bond authority is permanent delegate and burn authority at once, so it alone
/// can burn a holder's bonds without the holder signing.
#[test]
fn only_the_bond_authority_burns_without_the_holder() {
    let mut p = Probe::setup();
    let (h, m, b) = (p.holder.pubkey(), p.mint, p.bond.pubkey());
    let burn = permissioned_burn_checked(&ata(&h, &m), &m, &b, &b, 2);
    let (payer, bond) = (p.payer.insecure_clone(), p.bond.insecure_clone());
    p.send(&[burn], &[&payer, &bond]).expect("burn authority acting as permanent delegate");
    assert_eq!(p.balance(), 3);
}
