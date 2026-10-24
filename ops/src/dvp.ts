// Secondary trades settled by Solana DvP (solana-foundation/dvp, program dvp34bdb...): the seller's bonds and the
// buyer's cash wait in the trade's two escrows and move in one transaction, settled by the registrar. Our program's
// part is `admit_trade` (a holder record for the escrow, naming the seller) and the transfer hook, which keeps the
// escrowed bonds in the seller's position until settlement (programs/bond_servicing/src/instructions/admit_trade.rs).
// Instruction layouts: dvp `program/src/processor/{create,settle,cancel}_dvp.rs` (discriminators 0, 2, 3).

import crypto from "node:crypto";
import * as anchor from "@anchor-lang/core";
import {
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID,
  createTransferCheckedInstruction,
  createTransferCheckedWithTransferHookInstruction,
  getAssociatedTokenAddressSync,
  getExtraAccountMetaAddress,
  getExtraAccountMetas,
  resolveExtraAccountMeta,
} from "@solana/spl-token";
import { Connection, Keypair, PublicKey, SystemProgram, TransactionInstruction } from "@solana/web3.js";

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

/// `CreateDvp`: the registrar pays and is the settlement authority; the seller delivers `quantity` bonds, the buyer
/// `cash` of the cash token; expiry one day of chain time ahead. Destinations default to the parties themselves.
export function createDvpInstruction(
  registrar: PublicKey, seller: PublicKey, buyer: PublicKey, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey,
  quantity: bigint, cash: bigint, nonce: bigint, expiry: bigint,
): { ix: TransactionInstruction; swap: PublicKey } {
  const swap = swapDvpAddress(registrar, seller, buyer, bondMint, cashMint, nonce);
  const tombstone = PublicKey.findProgramAddressSync([Buffer.from("nonce"), swap.toBuffer()], DVP_PROGRAM_ID)[0];
  const data = Buffer.concat([Buffer.from([0]), u64(quantity), u64(cash), i64(expiry), u64(nonce), Buffer.from([0, 0, 0, 0])]);
  const ix = new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: registrar, isSigner: true, isWritable: true },
      { pubkey: swap, isSigner: false, isWritable: true },
      { pubkey: tombstone, isSigner: false, isWritable: true },
      { pubkey: registrar, isSigner: false, isWritable: false },
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

/// The bond leg: the seller's hooked transfer into the escrow (the hook's extras resolved from the mint's list).
export const fundBondLegInstruction = (connection: Connection, bondMint: PublicKey, seller: PublicKey, swap: PublicKey, quantity: bigint) =>
  createTransferCheckedWithTransferHookInstruction(
    connection, getAssociatedTokenAddressSync(bondMint, seller, false, TOKEN_2022_PROGRAM_ID), bondMint,
    getAssociatedTokenAddressSync(bondMint, swap, true, TOKEN_2022_PROGRAM_ID), seller, quantity, 0, [], "confirmed", TOKEN_2022_PROGRAM_ID,
  );

/// The cash leg: the buyer's plain transfer into the escrow.
export const fundCashLegInstruction = (cashMint: PublicKey, cashProgram: PublicKey, decimals: number, buyer: PublicKey, swap: PublicKey, cash: bigint) =>
  createTransferCheckedInstruction(
    getAssociatedTokenAddressSync(cashMint, buyer, false, cashProgram), cashMint,
    getAssociatedTokenAddressSync(cashMint, swap, true, cashProgram), buyer, cash, decimals, [], cashProgram,
  );

/// The hook's extra accounts for a transfer out of the escrow to `to`, resolved from the mint's list exactly as a
/// wallet would (the fourth, the seller's record, comes from the escrow record's `escrow_of`).
async function hookExtras(connection: Connection, program: PublicKey, bondMint: PublicKey, swap: PublicKey, to: PublicKey, amount: bigint) {
  const validateState = getExtraAccountMetaAddress(bondMint, program);
  const info = await connection.getAccountInfo(validateState, "confirmed");
  if (!info) throw new Error("hook account list not found");
  const source = getAssociatedTokenAddressSync(bondMint, swap, true, TOKEN_2022_PROGRAM_ID);
  const destination = getAssociatedTokenAddressSync(bondMint, to, false, TOKEN_2022_PROGRAM_ID);
  // The execute instruction the hook sees: source, mint, destination, authority (the SwapDvp PDA), validation PDA.
  const keys = [
    { pubkey: source, isSigner: false, isWritable: true },
    { pubkey: bondMint, isSigner: false, isWritable: false },
    { pubkey: destination, isSigner: false, isWritable: true },
    { pubkey: swap, isSigner: false, isWritable: false },
    { pubkey: validateState, isSigner: false, isWritable: false },
  ];
  const data = Buffer.concat([Buffer.from([105, 37, 101, 197, 75, 251, 102, 26]), u64(amount)]);
  for (const meta of getExtraAccountMetas(info)) keys.push(await resolveExtraAccountMeta(connection, meta, keys, data, program));
  // Token-2022 expects, after the four transfer accounts: the extras, then the hook program, then the validation PDA.
  return [...keys.slice(5), { pubkey: program, isSigner: false, isWritable: false }, { pubkey: validateState, isSigner: false, isWritable: false }];
}

/// `SettleDvp`: bonds to the buyer, cash to the seller, both escrows closed; the registrar signs. The bond leg (A)
/// carries the hook's extras, the cash leg (B) none.
export async function settleDvpInstruction(
  connection: Connection, program: PublicKey, registrar: PublicKey, t: DvpTrade, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey,
) {
  const extras = await hookExtras(connection, program, bondMint, t.swap, t.buyer, t.quantity);
  return new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: registrar, isSigner: true, isWritable: true },
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

/// `CancelDvp`: both legs back to their depositors, escrows closed; the registrar signs.
export async function cancelDvpInstruction(
  connection: Connection, program: PublicKey, registrar: PublicKey, t: DvpTrade, bondMint: PublicKey, cashMint: PublicKey, cashProgram: PublicKey,
) {
  const extras = await hookExtras(connection, program, bondMint, t.swap, t.seller, t.quantity);
  return new TransactionInstruction({
    programId: DVP_PROGRAM_ID,
    keys: [
      { pubkey: registrar, isSigner: true, isWritable: true },
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

/// `admit_trade` on our program: the registrar creates the escrow's holder record.
export const admitTradeInstruction = (program: anchor.Program, registrar: PublicKey, bond: PublicKey, bondMint: PublicKey, swap: PublicKey, seller: PublicKey, buyer: PublicKey) => {
  const holder = (owner: PublicKey) => PublicKey.findProgramAddressSync([Buffer.from("holder"), bondMint.toBuffer(), owner.toBuffer()], program.programId)[0];
  return program.methods.admitTrade().accountsPartial({
    registrar, bond, swapDvp: swap, seller: holder(seller), buyer: holder(buyer), escrow: holder(swap), systemProgram: SystemProgram.programId,
  }).instruction();
};

export type { Keypair };
