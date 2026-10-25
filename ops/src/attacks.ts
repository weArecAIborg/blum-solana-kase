// Hostile actor for a bond run (scenario field "attacker"). At each step of the bond's life it tries the actions that
// would take bonds or cash from someone, or let someone act without authority. Every attempt is sent without
// simulation (skipPreflight), so a refusal is a failed transaction on the chain that anyone can open in an explorer.
// An attempt passes only if it is refused by the guard named in `guard`; the run fails otherwise
// (scenario.ts exits with an error after writing the report).
// Also shows the bank's frozen cash account case: one holder cannot be paid, the others are paid, nobody waits.

import * as anchor from "@anchor-lang/core";
import {
  ExtensionType,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createBurnCheckedInstruction,
  createFreezeAccountInstruction,
  createInitializeAccount3Instruction,
  createInitializeMintInstruction,
  createInitializePermanentDelegateInstruction,
  createInitializeTransferFeeConfigInstruction,
  createMintToInstruction,
  createThawAccountInstruction,
  createTransferCheckedInstruction,
  createTransferCheckedWithTransferHookInstruction,
  createTransferInstruction,
  getAssociatedTokenAddressSync,
  getMint,
  getMintLen,
} from "@solana/spl-token";
import {
  ComputeBudgetProgram,
  Connection,
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionInstruction,
} from "@solana/web3.js";
import BN from "bn.js";

const LOADER = new PublicKey("BPFLoaderUpgradeab1e11111111111111111111111");

export type HostileSpec = {
  wallet: string;
  // Whose bonds and coupons are attacked; the accomplice is the registered holder whose position a forgery would raise.
  victim: string;
  accomplice: string;
  // The bank freezes this holder's cash account before this coupon's payments and thaws it after the others are paid.
  frozenCash: { holder: string; coupon: number };
};

export type Attempt = {
  id: string;
  when: string;
  actor: string;
  tries: string;
  wouldCost: string;
  guard: string;
  outcome: "refused" | "accepted";
  reason: string;
  ok: boolean;
  signature: string;
  explorer?: string;
  chainTime: string;
};

export type Ctx = {
  connection: Connection;
  program: anchor.Program;
  registrar: Keypair;
  bank: Keypair;
  attacker: Keypair;
  spec: HostileSpec;
  terms: any;
  mint: PublicKey;
  bond: PublicKey;
  cashMint: PublicKey;
  cashDecimals: number;
  holders: Record<string, Keypair>;
  bondsAta: (name: string) => PublicKey;
  cashAta: (name: string) => PublicKey;
  holderRecord: (name: string) => PublicKey;
  pda: (...seeds: (Buffer | Uint8Array)[]) => PublicKey;
  u16: (n: number) => Buffer;
  offers: { id: number; kind: "buyback" | "tender"; actions: { afterRecordMoment: number; holder: string; sell?: number; tender?: number }[] }[];
  offerPda: (id: number) => PublicKey;
  retirementAccounts: (o: any, name: string, payer: PublicKey) => any;
  send: (step: string, ixs: TransactionInstruction[], signers: Keypair[]) => Promise<string>;
  chainNow: () => Promise<number>;
  explorer: (sig: string) => string | undefined;
};

// Why a transaction failed, from its logs: the Anchor error code, a token program's error text, or the runtime's.
const reasonOf = (meta: any) => {
  const logs: string[] = meta.logMessages ?? [];
  for (const l of logs) {
    const m = /Error Code: (\w+)\. Error Number: \d+\. Error Message: (.*)$/.exec(l);
    if (m) return `${m[1]}: ${m[2]}`;
  }
  for (const l of logs) {
    const m = /^Program log: Error: (.*)$/.exec(l);
    if (m) return m[1];
  }
  const failed = logs.map((l) => /^Program \w+ failed: (.*)$/.exec(l)).filter(Boolean).pop();
  return failed ? failed[1] : JSON.stringify(meta.err);
};

// A signer the instruction names but that nobody can sign for (a program address): keep it in the instruction as a
// non-signer, so the transaction reaches the chain and the program checking the signature refuses it there.
const unsigned = (ix: TransactionInstruction, key: PublicKey) => {
  for (const k of ix.keys) if (k.pubkey.equals(key)) k.isSigner = false;
  return ix;
};

export function hostile(c: Ctx) {
  const attempts: Attempt[] = [];
  const A = c.attacker.publicKey;
  const victim = c.spec.victim;
  const accomplice = c.spec.accomplice;
  const attackerBonds = getAssociatedTokenAddressSync(c.mint, A, false, TOKEN_2022_PROGRAM_ID);
  const attackerCash = getAssociatedTokenAddressSync(c.cashMint, A, false, TOKEN_PROGRAM_ID);
  const victimSecondAccount = Keypair.generate();
  const asAttacker = new anchor.Program(c.program.idl, new anchor.AnchorProvider(c.connection, new anchor.Wallet(c.attacker), { commitment: "confirmed" }));

  // Send without simulation and poll its status until it lands (failed or not); resend with a fresh blockhash if it
  // expired first. (web3.js confirmTransaction throws a landed transaction's error on devnet.)
  const land = async (ixs: TransactionInstruction[], signers: Keypair[]) => {
    for (let tries = 1; tries <= 4; tries++) {
      const { blockhash, lastValidBlockHeight } = await c.connection.getLatestBlockhash("confirmed");
      const tx = new Transaction({ feePayer: signers[0].publicKey, blockhash, lastValidBlockHeight }).add(...ixs);
      tx.sign(...signers);
      const signature = await c.connection.sendRawTransaction(tx.serialize(), { skipPreflight: true, maxRetries: 5 });
      for (;;) {
        const st = (await c.connection.getSignatureStatuses([signature])).value[0];
        if (st?.confirmationStatus === "confirmed" || st?.confirmationStatus === "finalized") return signature;
        if ((await c.connection.getBlockHeight("confirmed")) > lastValidBlockHeight) break;
        await new Promise((r) => setTimeout(r, 1_000));
      }
    }
    throw new Error("attempt did not land after 4 sends");
  };
  const attempt = async (
    a: { id: string; when: string; actor: string; tries: string; wouldCost: string; guard: string },
    ixs: TransactionInstruction[],
    signers: Keypair[],
  ) => {
    const signature = await land(ixs, signers);
    let tx = null;
    for (let i = 0; i < 20 && !tx; i++) {
      tx = await c.connection.getTransaction(signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
      if (!tx) await new Promise((r) => setTimeout(r, 1_000));
    }
    if (!tx) throw new Error(`${a.id}: transaction ${signature} not found`);
    const outcome = tx.meta!.err ? "refused" : "accepted";
    const reason = tx.meta!.err ? reasonOf(tx.meta) : "";
    const ok = outcome === "refused" && reason.includes(a.guard);
    const row: Attempt = { ...a, outcome, reason, ok, signature, explorer: c.explorer(signature), chainTime: new Date((await c.chainNow()) * 1000).toISOString() };
    attempts.push(row);
    console.log(`${ok ? "REFUSED " : "FAILED  "} ${a.id}: ${a.tries} -> ${outcome}${reason ? ` (${reason})` : ""}  ${row.explorer ?? signature}`);
    return row;
  };

  // Accounts the attacks use: the attacker's own bond and cash accounts, and a second bond account owned by the
  // victim but not its associated one. Anyone may open these; the attacker pays.
  const setUp = async () => {
    const have = await c.connection.getBalance(A);
    if (have < 0.1 * LAMPORTS_PER_SOL)
      await c.send("fund attacker fees", [SystemProgram.transfer({ fromPubkey: c.registrar.publicKey, toPubkey: A, lamports: 0.1 * LAMPORTS_PER_SOL - have })], [c.registrar]);
    const size = (await c.connection.getAccountInfo(c.bondsAta(victim)))!.data.length;
    await c.send("attacker opens its own bond and cash accounts, and a second bond account for the victim", [
      createAssociatedTokenAccountIdempotentInstruction(A, attackerBonds, A, c.mint, TOKEN_2022_PROGRAM_ID),
      createAssociatedTokenAccountIdempotentInstruction(A, attackerCash, A, c.cashMint, TOKEN_PROGRAM_ID),
      SystemProgram.createAccount({
        fromPubkey: A, newAccountPubkey: victimSecondAccount.publicKey, space: size,
        lamports: await c.connection.getMinimumBalanceForRentExemption(size), programId: TOKEN_2022_PROGRAM_ID,
      }),
      createInitializeAccount3Instruction(victimSecondAccount.publicKey, c.mint, c.holders[victim].publicKey, TOKEN_2022_PROGRAM_ID),
    ], [c.attacker, victimSecondAccount]);
  };

  // Token-2022 PermissionedBurnExtension (46), BurnChecked (2): account, mint, burn authority, owner or delegate
  // (the layout settlement.rs uses).
  const permissionedBurn = (account: PublicKey, authority: PublicKey, owner: PublicKey) => new TransactionInstruction({
    programId: TOKEN_2022_PROGRAM_ID,
    data: Buffer.concat([Buffer.from([46, 2]), new BN(1).toArrayLike(Buffer, "le", 8), Buffer.from([0])]),
    keys: [
      { pubkey: account, isSigner: false, isWritable: true },
      { pubkey: c.mint, isSigner: false, isWritable: true },
      { pubkey: authority, isSigner: true, isWritable: false },
      { pubkey: owner, isSigner: true, isWritable: false },
    ],
  });

  const hooked = (from: string | PublicKey, fromOwner: Keypair, to: PublicKey, amount: number) =>
    createTransferCheckedWithTransferHookInstruction(
      c.connection, typeof from === "string" ? c.bondsAta(from) : from, c.mint, to, fromOwner.publicKey, BigInt(amount), 0, [], "confirmed", TOKEN_2022_PROGRAM_ID,
    );

  const payCoupon = (sv: anchor.Program, payer: PublicKey, e: number, name: string, over: Record<string, PublicKey> = {}) => {
    const event = c.pda(Buffer.from("event"), c.bond.toBuffer(), c.u16(e));
    return sv.methods.payCoupon().accountsPartial({
      payer, bond: c.bond, event, holder: c.holderRecord(name), owner: c.holders[name].publicKey,
      vault: c.pda(Buffer.from("vault"), event.toBuffer()), cashMint: c.cashMint, holderCash: c.cashAta(name),
      memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID, ...over,
    }).instruction();
  };

  // Before the first record moment: the register, the bond token and the program itself.
  const beforeFirstRecordMoment = async () => {
    await setUp();
    const when = "before the first record moment";
    await attempt({ id: "register-self", when, actor: "attacker", tries: "registers itself as a holder, signing as registrar",
      wouldCost: "an unvetted wallet in the register", guard: "ConstraintHasOne" },
      [await asAttacker.methods.registerHolder(A).accountsPartial({ registrar: A, bond: c.bond }).instruction()], [c.attacker]);
    await attempt({ id: "mint-bonds", when, actor: "attacker", tries: "mints 1,000 bonds to itself",
      wouldCost: "the issuer owes coupons and principal on bonds it never sold", guard: "owner does not match" },
      [createMintToInstruction(c.mint, attackerBonds, A, 1_000, [], TOKEN_2022_PROGRAM_ID)], [c.attacker]);
    await attempt({ id: "mint-as-program", when, actor: "attacker", tries: "mints 1,000 bonds naming the program as mint authority",
      wouldCost: "the issuer owes coupons and principal on bonds it never sold", guard: "MissingRequiredSignature" },
      [unsigned(createMintToInstruction(c.mint, attackerBonds, c.bond, 1_000, [], TOKEN_2022_PROGRAM_ID), c.bond)], [c.attacker]);
    await attempt({ id: "take-bonds", when, actor: "attacker", tries: `moves ${victim}'s bonds to itself`,
      wouldCost: `${victim} loses its bonds`, guard: "owner does not match" },
      [createTransferCheckedInstruction(c.bondsAta(victim), c.mint, attackerBonds, A, 1, 0, [], TOKEN_2022_PROGRAM_ID)], [c.attacker]);
    await attempt({ id: "take-as-delegate", when, actor: "attacker", tries: `moves ${victim}'s bonds naming the program, the permanent delegate`,
      wouldCost: `${victim} loses its bonds`, guard: "MissingRequiredSignature" },
      [unsigned(createTransferCheckedInstruction(c.bondsAta(victim), c.mint, attackerBonds, c.bond, 1, 0, [], TOKEN_2022_PROGRAM_ID), c.bond)], [c.attacker]);
    // Token-2022 refuses every standard burn on a mint with permissioned burn (TokenError::InvalidInstruction).
    await attempt({ id: "burn-bonds", when, actor: "attacker", tries: `burns ${victim}'s bonds`,
      wouldCost: `${victim} loses coupons and principal`, guard: "Invalid instruction" },
      [createBurnCheckedInstruction(c.bondsAta(victim), c.mint, A, 1, 0, [], TOKEN_2022_PROGRAM_ID)], [c.attacker]);
    // InvalidAccountData: Token-2022's check that the approver is the mint's permissioned burn authority.
    await attempt({ id: "burn-permissioned", when, actor: "attacker", tries: `burns ${victim}'s bonds with the permissioned burn, naming itself burn authority`,
      wouldCost: `${victim} loses coupons and principal`, guard: "InvalidAccountData" },
      [permissionedBurn(c.bondsAta(victim), A, A)], [c.attacker]);
    await attempt({ id: "burn-as-program", when, actor: "attacker", tries: `burns ${victim}'s bonds with the permissioned burn, naming the program, the burn authority`,
      wouldCost: `${victim} loses coupons and principal`, guard: "MissingRequiredSignature" },
      [unsigned(permissionedBurn(c.bondsAta(victim), c.bond, c.bond), c.bond)], [c.attacker]);
    await attempt({ id: "freeze-holder", when, actor: "attacker", tries: `freezes ${victim}'s bond account`,
      wouldCost: `${victim} cannot sell`, guard: "cannot freeze" },
      [createFreezeAccountInstruction(c.bondsAta(victim), c.mint, A, [], TOKEN_2022_PROGRAM_ID)], [c.attacker]);
    await attempt({ id: "send-unregistered", when, actor: victim, tries: "sends a bond to an unregistered wallet (the attacker)",
      wouldCost: "bonds held outside the register", guard: "AccountOwnedByWrongProgram" },
      [await hooked(victim, c.holders[victim], attackerBonds, 1)], [c.registrar, c.holders[victim]]);
    await attempt({ id: "second-account", when, actor: victim, tries: "moves a bond to a second account of its own",
      wouldCost: "a position the register does not see", guard: "NotAssociatedTokenAccount" },
      [await hooked(victim, c.holders[victim], victimSecondAccount.publicKey, 1)], [c.registrar, c.holders[victim]]);
    // Token-2022 error 0x1f = TokenError::MintRequiredForTransfer: a hooked mint's accounts move only with transfer_checked.
    await attempt({ id: "skip-hook", when, actor: victim, tries: `sends a bond to ${accomplice} with Token-2022's plain Transfer instruction, which skips the transfer hook`,
      wouldCost: "a bond moves without its record-moment position being saved", guard: "custom program error: 0x1f" },
      [createTransferInstruction(c.bondsAta(victim), c.bondsAta(accomplice), c.holders[victim].publicKey, 1, [], TOKEN_2022_PROGRAM_ID)], [c.registrar, c.holders[victim]]);
    await attempt({ id: "forge-position", when, actor: "attacker", tries: `calls the transfer hook directly to add 5 bonds to ${accomplice}'s record`,
      wouldCost: "coupons paid on bonds nobody holds", guard: "NotTransferring" },
      [await asAttacker.methods.transferHook(new BN(5)).accountsPartial({
        sourceToken: c.bondsAta(victim), mint: c.mint, destinationToken: c.bondsAta(accomplice), authority: A,
        extraAccountMetaList: c.pda(Buffer.from("extra-account-metas"), c.mint.toBuffer()), bond: c.bond,
        sourceHolder: c.holderRecord(victim), destinationHolder: c.holderRecord(accomplice),
      }).instruction()], [c.attacker]);
    const [programData] = PublicKey.findProgramAddressSync([c.program.programId.toBuffer()], LOADER);
    await attempt({ id: "take-program", when, actor: "attacker", tries: "makes itself the program's upgrade authority",
      wouldCost: "a replaced program could empty every vault", guard: "Incorrect authority" },
      [new TransactionInstruction({ programId: LOADER, data: Buffer.from([4, 0, 0, 0]), keys: [
        { pubkey: programData, isSigner: false, isWritable: true },
        { pubkey: A, isSigner: true, isWritable: false },
        { pubkey: A, isSigner: false, isWritable: false },
      ] })], [c.attacker]);

    // A cash token that keeps a fee on every transfer would pay holders less than the terms.
    const feeMint = Keypair.generate();
    const len = getMintLen([ExtensionType.TransferFeeConfig]);
    await c.send("registrar creates a cash token with a 1% transfer fee", [
      SystemProgram.createAccount({ fromPubkey: c.registrar.publicKey, newAccountPubkey: feeMint.publicKey, space: len,
        lamports: await c.connection.getMinimumBalanceForRentExemption(len), programId: TOKEN_2022_PROGRAM_ID }),
      createInitializeTransferFeeConfigInstruction(feeMint.publicKey, c.registrar.publicKey, c.registrar.publicKey, 100, BigInt(1e12), TOKEN_2022_PROGRAM_ID),
      createInitializeMintInstruction(feeMint.publicKey, c.cashDecimals, c.registrar.publicKey, null, TOKEN_2022_PROGRAM_ID),
    ], [c.registrar, feeMint]);
    const newMint = Keypair.generate();
    // Checking the schedule takes over 200,000 CU from 36 periods on; without this the guard would never be reached.
    const createBondCompute = ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });
    await attempt({ id: "fee-cash-token", when, actor: "registrar", tries: "creates a bond paid in a cash token that keeps a 1% fee on every transfer",
      wouldCost: "holders receive 1% less than the terms", guard: "UnsupportedCashToken" },
      [createBondCompute, await c.program.methods.createBond(c.terms).accountsPartial({
        registrar: c.registrar.publicKey, mint: newMint.publicKey, bond: c.pda(Buffer.from("bond"), newMint.publicKey.toBuffer()), cashMint: feeMint.publicKey,
        extraAccountMetaList: c.pda(Buffer.from("extra-account-metas"), newMint.publicKey.toBuffer()),
        tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
      }).instruction()], [c.registrar, newMint]);

    // A cash token with a permanent delegate lets that delegate move cash out of any account, the vaults included.
    const delegateMint = Keypair.generate();
    const delegateLen = getMintLen([ExtensionType.PermanentDelegate]);
    await c.send("registrar creates a cash token whose permanent delegate is the attacker", [
      SystemProgram.createAccount({ fromPubkey: c.registrar.publicKey, newAccountPubkey: delegateMint.publicKey, space: delegateLen,
        lamports: await c.connection.getMinimumBalanceForRentExemption(delegateLen), programId: TOKEN_2022_PROGRAM_ID }),
      createInitializePermanentDelegateInstruction(delegateMint.publicKey, A, TOKEN_2022_PROGRAM_ID),
      createInitializeMintInstruction(delegateMint.publicKey, c.cashDecimals, c.registrar.publicKey, null, TOKEN_2022_PROGRAM_ID),
    ], [c.registrar, delegateMint]);
    const delegateBondMint = Keypair.generate();
    await attempt({ id: "delegate-cash-token", when, actor: "registrar", tries: "creates a bond paid in a cash token whose permanent delegate is the attacker",
      wouldCost: "the delegate empties the vaults before holders are paid", guard: "UnsupportedCashToken" },
      [createBondCompute, await c.program.methods.createBond(c.terms).accountsPartial({
        registrar: c.registrar.publicKey, mint: delegateBondMint.publicKey, bond: c.pda(Buffer.from("bond"), delegateBondMint.publicKey.toBuffer()),
        cashMint: delegateMint.publicKey, extraAccountMetaList: c.pda(Buffer.from("extra-account-metas"), delegateBondMint.publicKey.toBuffer()),
        tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
      }).instruction()], [c.registrar, delegateBondMint]);

    // Placement is still open here, so only the issue size stops it: one bond more than the terms allow.
    const supply = (await getMint(c.connection, c.mint, "confirmed", TOKEN_2022_PROGRAM_ID)).supply;
    const issueSize = BigInt(c.terms.maxSupply.toString()), beyond = issueSize - supply + 1n;
    await attempt({ id: "place-beyond-issue", when, actor: "registrar",
      tries: `places ${beyond} more bond${beyond === 1n ? "" : "s"} with ${accomplice}, past the issue size of ${issueSize}`,
      wouldCost: "bonds beyond the issue the terms announced", guard: "ExceedsIssueSize" },
      [await c.program.methods.place(new BN(beyond.toString())).accountsPartial({
        registrar: c.registrar.publicKey, bond: c.bond, mint: c.mint, holder: c.holderRecord(accomplice), holderBonds: c.bondsAta(accomplice), tokenProgram: TOKEN_2022_PROGRAM_ID,
      }).instruction()], [c.registrar]);
  };

  // After a record moment's trades.
  const afterRecordMoment = async (e: number) => {
    if (e !== 0) return;
    await attempt({ id: "place-late", when: "after the first record moment", actor: "registrar", tries: `places 100 new bonds with ${accomplice}`,
      wouldCost: "bonds issued after the issue closed", guard: "PlacementClosed" },
      [await c.program.methods.place(new BN(100)).accountsPartial({
        registrar: c.registrar.publicKey, bond: c.bond, mint: c.mint, holder: c.holderRecord(accomplice), holderBonds: c.bondsAta(accomplice), tokenProgram: TOKEN_2022_PROGRAM_ID,
      }).instruction()], [c.registrar]);
  };

  // Between opening the first coupon and its funding: the issuer funds it short, and the attacker tries to use that.
  let underfundedShown = false;
  const whileUnfunded = async (e: number, event: PublicKey, vault: PublicKey, budget: bigint) => {
    if (underfundedShown) return;
    underfundedShown = true;
    const when = `coupon ${e}, vault funded short by 0.000001`;
    await c.send(`issuer funds coupon ${e}'s vault 0.000001 short`, [createMintToInstruction(c.cashMint, vault, c.bank.publicKey, budget - 1n, [], TOKEN_PROGRAM_ID)], [c.registrar, c.bank]);
    await attempt({ id: "confirm-short", when, actor: "attacker", tries: "confirms the coupon funded",
      wouldCost: "the last holders find the vault empty", guard: "InsufficientFunding" },
      [await asAttacker.methods.confirmFunding().accountsPartial({ event, vault }).instruction()], [c.attacker]);
    await attempt({ id: "pay-unfunded", when, actor: "attacker", tries: `pays ${victim} from the unconfirmed vault`,
      wouldCost: "the last holders find the vault empty", guard: "NotFunded" },
      [await payCoupon(asAttacker, A, e, victim)], [c.attacker]);
    await attempt({ id: "drain-vault", when, actor: "attacker", tries: "moves the vault's cash to itself",
      wouldCost: "the coupon's cash is gone", guard: "owner does not match" },
      [createTransferCheckedInstruction(vault, c.cashMint, attackerCash, A, 1, c.cashDecimals, [], TOKEN_PROGRAM_ID)], [c.attacker]);
    await attempt({ id: "drain-as-program", when, actor: "attacker", tries: "moves the vault's cash to itself naming the program, the vault's owner",
      wouldCost: "the coupon's cash is gone", guard: "MissingRequiredSignature" },
      [unsigned(createTransferCheckedInstruction(vault, c.cashMint, attackerCash, c.bond, 1, c.cashDecimals, [], TOKEN_PROGRAM_ID), c.bond)], [c.attacker]);
  };

  // Payment is open; before anyone is paid. Returns the holders to pay after afterPayments.
  const beforePayments = async (e: number): Promise<string[]> => {
    const when = `coupon ${e}, payment open`;
    if (e === 0) {
      await attempt({ id: "redirect-coupon", when, actor: "attacker", tries: `pays ${victim}'s coupon into its own cash account`,
        wouldCost: `${victim}'s coupon`, guard: "ConstraintTokenOwner" },
        [await payCoupon(asAttacker, A, e, victim, { holderCash: attackerCash })], [c.attacker]);
      await attempt({ id: "claim-as-owner", when, actor: "attacker", tries: `presents ${victim}'s holder record as its own`,
        wouldCost: `${victim}'s coupon`, guard: "Constraint" },
        [await payCoupon(asAttacker, A, e, victim, { owner: A, holderCash: attackerCash })], [c.attacker]);
    }
    if (e === 1) {
      const prev = c.pda(Buffer.from("event"), c.bond.toBuffer(), c.u16(0));
      await attempt({ id: "other-vault", when, actor: "attacker", tries: `pays ${victim}'s coupon 1 from coupon 0's vault`,
        wouldCost: "one coupon's cash spent on another", guard: "ConstraintHasOne" },
        [await payCoupon(asAttacker, A, e, victim, { vault: c.pda(Buffer.from("vault"), prev.toBuffer()) })], [c.attacker]);
    }
    if (e !== c.spec.frozenCash.coupon) return [];
    const who = c.spec.frozenCash.holder;
    await c.send(`bank freezes ${who}'s cash account`, [createFreezeAccountInstruction(c.cashAta(who), c.cashMint, c.bank.publicKey, [], TOKEN_PROGRAM_ID)], [c.registrar, c.bank]);
    return [who];
  };

  // Everyone else is paid.
  const afterPayments = async (e: number) => {
    const when = `coupon ${e}, paid`;
    if (e === 0) {
      await attempt({ id: "pay-twice", when, actor: "attacker", tries: `pays ${victim}'s coupon again`,
        wouldCost: "the issuer pays a coupon twice; the last holders find the vault empty", guard: "AlreadyPaid" },
        [await payCoupon(asAttacker, A, e, victim)], [c.attacker]);
      const recs: any[] = await c.program.account.holderRecord.fetchMultiple(Object.keys(c.holders).map(c.holderRecord));
      const late = Object.keys(c.holders).find((n, i) => recs[i] && Number(recs[i].position.liveFrom) > e && Number(recs[i].position.at[e]) === 0 && Number(recs[i].position.quantity) > 0);
      if (!late) throw new Error("scenario has no holder that bought after record moment 0 with none before (needed by attack late-buyer)");
      await attempt({ id: "late-buyer", when, actor: late, tries: "bought after the record moment and asks for the coupon",
        wouldCost: "the seller's coupon paid again to the buyer", guard: "NothingToPay" },
        [await payCoupon(c.program, c.registrar.publicKey, e, late)], [c.registrar]);
    }
    if (e !== c.spec.frozenCash.coupon) return;
    const who = c.spec.frozenCash.holder;
    await attempt({ id: "frozen-cash", when, actor: "anyone", tries: `pays ${who}, whose cash account the bank froze`,
      wouldCost: "nothing: the others are already paid, and the right stays open", guard: "Account is frozen" },
      [await payCoupon(c.program, c.registrar.publicKey, e, who)], [c.registrar]);
    await c.send(`bank thaws ${who}'s cash account`, [createThawAccountInstruction(c.cashAta(who), c.cashMint, c.bank.publicKey, [], TOKEN_PROGRAM_ID)], [c.registrar, c.bank]);
  };

  // After a phase's buyback sales and tenders.
  const afterOfferActions = async (e: number) => {
    for (const o of c.offers) {
      if (!o.actions.some((a) => a.afterRecordMoment === e)) continue;
      const when = `${o.kind} offer ${o.id} open`;
      if (o.kind === "buyback") {
        await attempt({ id: "sell-others-bonds", when, actor: "attacker", tries: `sells ${victim}'s bonds to the buyback`,
          wouldCost: `${victim}'s bonds sold without its consent`, guard: "ConstraintRaw" },
          [await asAttacker.methods.sellToOffer(new BN(1)).accountsPartial({ retirement: c.retirementAccounts(o, victim, A), seller: A, offer: c.offerPda(o.id) }).instruction()], [c.attacker]);
        await attempt({ id: "over-maximum", when, actor: victim, tries: "sells 1 bond to the buyback after its maximum is reached",
          wouldCost: "the issuer buys more bonds than it offered", guard: "ExceedsOffer" },
          [await c.program.methods.sellToOffer(new BN(1)).accountsPartial({ retirement: c.retirementAccounts(o, victim, c.holders[victim].publicKey), seller: c.holders[victim].publicKey, offer: c.offerPda(o.id) }).instruction()],
          [c.registrar, c.holders[victim]]);
      } else {
        const tenderer = o.actions.find((a) => a.afterRecordMoment === e && a.tender)!.holder;
        const rec: any = await c.program.account.holderRecord.fetch(c.holderRecord(tenderer));
        const to = tenderer === accomplice ? victim : accomplice;
        await attempt({ id: "sell-tendered", when, actor: tenderer, tries: `transfers all its bonds to ${to}, including those tendered to the issuer`,
          wouldCost: "the same bonds sold twice: to the issuer and to a buyer", guard: "BondsLocked" },
          [await hooked(tenderer, c.holders[tenderer], c.bondsAta(to), Number(rec.position.quantity))], [c.registrar, c.holders[tenderer]]);
      }
    }
  };

  // Trading has closed for redemption.
  const afterFinalRecordMoment = async () => {
    await attempt({ id: "trade-at-maturity", when: "after the final record moment", actor: accomplice, tries: `sells 1 bond to ${victim}`,
      wouldCost: "a bond changes hands while its principal is being paid", guard: "TransfersClosedAtMaturity" },
      [await hooked(accomplice, c.holders[accomplice], c.bondsAta(victim), 1)], [c.registrar, c.holders[accomplice]]);
  };

  const redeemIx = (sv: anchor.Program, payer: PublicKey, event: PublicKey, vault: PublicKey, name: string, over: Record<string, PublicKey> = {}) =>
    sv.methods.redeemHolder().accountsPartial({
      payer, bond: c.bond, mint: c.mint, event, holder: c.holderRecord(name), owner: c.holders[name].publicKey, holderBonds: c.bondsAta(name),
      vault, cashMint: c.cashMint, holderCash: c.cashAta(name), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID, ...over,
    }).instruction();

  const beforeRedemption = async (event: PublicKey, vault: PublicKey) => {
    const when = "redemption, payment open";
    await attempt({ id: "redirect-principal", when, actor: "attacker", tries: `redeems ${victim}'s bonds into its own cash account`,
      wouldCost: `${victim}'s principal`, guard: "ConstraintTokenOwner" },
      [await redeemIx(asAttacker, A, event, vault, victim, { holderCash: attackerCash })], [c.attacker]);
    await attempt({ id: "coupon-from-principal", when, actor: "attacker", tries: `pays ${victim} a coupon from the redemption vault`,
      wouldCost: "principal cash paid out as coupons", guard: "WrongEventKind" },
      [await asAttacker.methods.payCoupon().accountsPartial({
        payer: A, bond: c.bond, event, holder: c.holderRecord(victim), owner: c.holders[victim].publicKey, vault, cashMint: c.cashMint,
        holderCash: c.cashAta(victim), memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID,
      }).instruction()], [c.attacker]);
  };

  const afterRedemption = async (event: PublicKey, vault: PublicKey) => {
    await attempt({ id: "redeem-twice", when: "redemption, paid", actor: "attacker", tries: `redeems ${victim} again`,
      wouldCost: "principal paid twice", guard: "NothingToPay" },
      [await redeemIx(asAttacker, A, event, vault, victim)], [c.attacker]);
  };

  return { attempts, beforeFirstRecordMoment, afterRecordMoment, whileUnfunded, beforePayments, afterPayments, afterOfferActions, afterFinalRecordMoment, beforeRedemption, afterRedemption };
}
