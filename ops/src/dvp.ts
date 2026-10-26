// Secondary trades settled by Solana DvP (solana-foundation/dvp, program dvp34bdb...): the seller's bonds and the
// buyer's cash wait in the trade's two escrows and move in one transaction, settled by the trade's settlement
// authority (the venue, solana.com/docs/defi/dvp-program). Our program's part is `admit_trade` (a holder record for
// the escrow, naming the seller; signed by the seller) and the transfer hook, which keeps the escrowed bonds in the
// seller's position until settlement (programs/bond_servicing/src/instructions/admit_trade.rs).
// Instruction layouts: dvp `program/src/processor/{create,settle,cancel}_dvp.rs` (discriminators 0, 2, 3).
// The hook's extra accounts are derived here from the list our program writes (instructions/hook_accounts.rs), so
// building a transfer reads nothing from the network; `verifyHookAccounts` checks the derivation against the list on
// chain once per bond.

import crypto from "node:crypto";
import * as anchor from "@anchor-lang/core";
import {
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID,
  createTransferCheckedInstruction,
  createTransferCheckedWithTransferHookInstruction,
  getAssociatedTokenAddressSync,
  getExtraAccountMetaAddress,
} from "@solana/spl-token";
import { Connection, Keypair, PublicKey, SystemProgram, TransactionInstruction, type AccountMeta } from "@solana/web3.js";

export const DVP_PROGRAM_ID = new PublicKey("dvp34bdbcEm4f4FCUjGV4mDAkDshaQR4LkK8fdcsyZq");
export const MEMO_PROGRAM_ID = new PublicKey("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr");

export type DvpTrade = {
  swap: PublicKey;
  nonce: bigint;
  seller: PublicKey;
  buyer: PublicKey;
  quantity: bigint;
  cash: bigint;
};

const u64 = (n: bigint) => { const b = Buffer.alloc(8); b.writeBigUInt64LE(n); return b; };
const i64 = (n: bigint) => { const b = Buffer.alloc(8); b.writeBigInt64LE(n); return b; };

/// The trade's `SwapDvp` PDA: ["dvp", settlement_authority, user_a, user_b, mint_a, mint_b, nonce].
export const swapDvpAddress = (authority: PublicKey, seller: PublicKey, buyer: PublicKey, bondMint: PublicKey, cashMint: PublicKey, nonce: bigint) =>
  PublicKey.findProgramAddressSync(
    [Buffer.from("dvp"), authority.toBuffer(), seller.toBuffer(), buyer.toBuffer(), bondMint.toBuffer(), cashMint.toBuffer(), u64(nonce)],
    DVP_PROGRAM_ID,
  )[0];

/// A cryptographically random nonce, as the DvP README requires (a predictable one lets a third party squat the slot).
export const randomNonce = () => crypto.randomBytes(8).readBigUInt64LE();

/// `CreateDvp`: `payer` pays the trade record's rent (anyone may: "CreateDvp is permissionless"); `authority` is the
/// settlement authority; the seller delivers `quantity` bonds, the buyer `cash` of the cash token. Destinations default
/// to the parties themselves.
export function createDvpInstruction(
  payer: PublicKey, authority: PublicKey, seller: PublicKey, buyer: PublicKey, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey,
  quantity: bigint, cash: bigint, nonce: bigint, expiry: bigint,
): { ix: TransactionInstruction; swap: PublicKey } {
  const swap = swapDvpAddress(authority, seller, buyer, bondMint, cashMint, nonce);
  const tombstone = PublicKey.findProgramAddressSync([Buffer.from("nonce"), swap.toBuffer()], DVP_PROGRAM_ID)[0];
  const data = Buffer.concat([Buffer.from([0]), u64(quantity), u64(cash), i64(expiry), u64(nonce), Buffer.from([0, 0, 0, 0])]);
  const ix = new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: payer, isSigner: true, isWritable: true },
      { pubkey: swap, isSigner: false, isWritable: true },
      { pubkey: tombstone, isSigner: false, isWritable: true },
      { pubkey: authority, isSigner: false, isWritable: false },
      { pubkey: seller, isSigner: false, isWritable: false },
      { pubkey: buyer, isSigner: false, isWritable: false },
      { pubkey: bondMint, isSigner: false, isWritable: false },
      { pubkey: cashMint, isSigner: false, isWritable: false },
      { pubkey: getAssociatedTokenAddressSync(bondMint, swap, true, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, swap, true, cashProgram), isSigner: false, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      { pubkey: TOKEN_2022_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: cashProgram, isSigner: false, isWritable: false },
      { pubkey: ASSOCIATED_TOKEN_PROGRAM_ID, isSigner: false, isWritable: false },
    ],
    data,
  });
  return { ix, swap };
}

/// The hook's extra accounts for a bond transfer, in the order Token-2022 appends them: the list's four extras (Bond
/// PDA; source owner's record; destination owner's record; the record of the seller named by the source record's
/// `escrow_of`, the zero key unless the source is a trade escrow), then the hook program and the list itself.
export function hookAccounts(program: PublicKey, bondMint: PublicKey, sourceOwner: PublicKey, destinationOwner: PublicKey, escrowOf: PublicKey): AccountMeta[] {
  const pda = (...seeds: Buffer[]) => PublicKey.findProgramAddressSync(seeds, program)[0];
  const record = (owner: PublicKey) => pda(Buffer.from("holder"), bondMint.toBuffer(), owner.toBuffer());
  return [
    { pubkey: pda(Buffer.from("bond"), bondMint.toBuffer()), isSigner: false, isWritable: false },
    { pubkey: record(sourceOwner), isSigner: false, isWritable: true },
    { pubkey: record(destinationOwner), isSigner: false, isWritable: true },
    { pubkey: record(escrowOf), isSigner: false, isWritable: true },
    { pubkey: program, isSigner: false, isWritable: false },
    { pubkey: getExtraAccountMetaAddress(bondMint, program), isSigner: false, isWritable: false },
  ];
}

/// Compare `hookAccounts` with what the SPL resolver reads from the list on chain, for a holder's transfer into a
/// trade escrow. Throws on any difference.
export async function verifyHookAccounts(connection: Connection, program: PublicKey, bondMint: PublicKey, seller: PublicKey, swap: PublicKey) {
  const resolved = await createTransferCheckedWithTransferHookInstruction(
    connection, getAssociatedTokenAddressSync(bondMint, seller, false, TOKEN_2022_PROGRAM_ID), bondMint,
    getAssociatedTokenAddressSync(bondMint, swap, true, TOKEN_2022_PROGRAM_ID), seller, 1n, 0, [], "confirmed", TOKEN_2022_PROGRAM_ID,
  );
  const local = fundBondLegInstruction(program, bondMint, seller, swap, 1n);
  const show = (ix: TransactionInstruction) => ix.keys.map((k) => `${k.pubkey.toBase58()}:${k.isWritable ? "w" : "r"}`).join(" ");
  if (show(resolved) !== show(local)) throw new Error(`hook accounts differ from the list on chain:\n${show(resolved)}\n${show(local)}`);
}

/// The bond leg: the seller's hooked transfer into the escrow.
export function fundBondLegInstruction(program: PublicKey, bondMint: PublicKey, seller: PublicKey, swap: PublicKey, quantity: bigint) {
  const ix = createTransferCheckedInstruction(
    getAssociatedTokenAddressSync(bondMint, seller, false, TOKEN_2022_PROGRAM_ID), bondMint,
    getAssociatedTokenAddressSync(bondMint, swap, true, TOKEN_2022_PROGRAM_ID), seller, quantity, 0, [], TOKEN_2022_PROGRAM_ID,
  );
  ix.keys.push(...hookAccounts(program, bondMint, seller, swap, PublicKey.default));
  return ix;
}

/// The cash leg: the buyer's plain transfer into the escrow.
export const fundCashLegInstruction = (cashMint: PublicKey, cashProgram: PublicKey, decimals: number, buyer: PublicKey, swap: PublicKey, cash: bigint) =>
  createTransferCheckedInstruction(
    getAssociatedTokenAddressSync(cashMint, buyer, false, cashProgram), cashMint,
    getAssociatedTokenAddressSync(cashMint, swap, true, cashProgram), buyer, cash, decimals, [], cashProgram,
  );

// The bond leg out of the escrow (to the buyer at settlement, back to the seller at cancel): the source is the escrow,
// whose record names the seller.
const outOfEscrow = (program: PublicKey, bondMint: PublicKey, t: DvpTrade, to: PublicKey) => hookAccounts(program, bondMint, t.swap, to, t.seller);

/// `SettleDvp`: bonds to the buyer, cash to the seller, both escrows closed; the settlement authority signs. The bond
/// leg (A) carries the hook's extras, the cash leg (B) none.
export function settleDvpInstruction(program: PublicKey, authority: PublicKey, t: DvpTrade, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey) {
  const extras = outOfEscrow(program, bondMint, t, t.buyer);
  return new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: authority, isSigner: true, isWritable: true },
      { pubkey: t.swap, isSigner: false, isWritable: true },
      { pubkey: bondMint, isSigner: false, isWritable: false },
      { pubkey: cashMint, isSigner: false, isWritable: false },
      { pubkey: getAssociatedTokenAddressSync(bondMint, t.swap, true, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, t.swap, true, cashProgram), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, t.seller, false, cashProgram), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(bondMint, t.buyer, false, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(bondMint, t.seller, false, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, t.buyer, false, cashProgram), isSigner: false, isWritable: true },
      { pubkey: TOKEN_2022_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: cashProgram, isSigner: false, isWritable: false },
      { pubkey: MEMO_PROGRAM_ID, isSigner: false, isWritable: false },
      ...extras,
    ],
    data: Buffer.from([2, extras.length]),
  });
}

/// `CancelDvp`: both legs back to their depositors, escrows closed; the settlement authority signs.
export function cancelDvpInstruction(program: PublicKey, authority: PublicKey, t: DvpTrade, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey) {
  const extras = outOfEscrow(program, bondMint, t, t.seller);
  return new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: authority, isSigner: true, isWritable: true },
      { pubkey: t.swap, isSigner: false, isWritable: true },
      { pubkey: bondMint, isSigner: false, isWritable: false },
      { pubkey: cashMint, isSigner: false, isWritable: false },
      { pubkey: getAssociatedTokenAddressSync(bondMint, t.swap, true, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, t.swap, true, cashProgram), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(bondMint, t.seller, false, TOKEN_2022_PROGRAM_ID), isSigner: false, isWritable: true },
      { pubkey: getAssociatedTokenAddressSync(cashMint, t.buyer, false, cashProgram), isSigner: false, isWritable: true },
      { pubkey: TOKEN_2022_PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: cashProgram, isSigner: false, isWritable: false },
      { pubkey: MEMO_PROGRAM_ID, isSigner: false, isWritable: false },
      ...extras,
    ],
    data: Buffer.from([3, extras.length]),
  });
}

/// `admit_trade` on our program, signed by the trade's seller; `payer` pays the escrow record's deposit.
export const admitTradeInstruction = (program: anchor.Program, payer: PublicKey, bond: PublicKey, bondMint: PublicKey, swap: PublicKey, seller: PublicKey, buyer: PublicKey) => {
  const holder = (owner: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("holder"), bondMint.toBuffer(), owner.toBuffer()], program.programId)[0];
  return program.methods.admitTrade().accountsPartial({
    payer, sellerWallet: seller, bond, swapDvp: swap, seller: holder(seller), buyer: holder(buyer), escrow: holder(swap), systemProgram: SystemProgram.programId,
  }).instruction();
};

export type { Keypair };
