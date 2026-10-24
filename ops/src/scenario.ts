// Operator (registrar + keeper) for bond_servicing: runs one bond's whole life, jumping the clock between record
// moments on Surfpool or waiting for the real clock on devnet, and writes a report of every position, payment and
// transaction.
// Usage: npx tsx src/scenario.ts <scenarios/NAME.json> <report.json> <rpc url> <localnet|devnet>
//   localnet (Surfpool): the clock is moved forward with surfnet_timeTravel.
//   devnet: the real chain clock is awaited (bond time is compressed by the scenario's chainSecondsPerBondDay).
// Logic: docs/bond-processing.md; overview: docs/technical-overview.md.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import * as anchor from "@anchor-lang/core";
import {
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createMintToInstruction,
  createTransferCheckedWithTransferHookInstruction,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import {
  ComputeBudgetProgram,
  Connection,
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  SYSVAR_CLOCK_PUBKEY,
  SystemProgram,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";

import BN from "bn.js";
import { type HostileSpec, hostile } from "./attacks.ts";
import { admitTradeInstruction, createDvpInstruction, fundBondLegInstruction, fundCashLegInstruction, randomNonce, settleDvpInstruction } from "./dvp.ts";
const ROOT = path.resolve(import.meta.dirname, "../..");
const DAY = 86_400;
// A holder's Tender record (paid by the holder): discriminator 8 + offer 32 + owner 32 + number 4 + quantity 8 +
// resolved 1 + accepted 8 + settled 1 + bump 1 (programs/bond_servicing/src/state.rs).
const TENDER_ACCOUNT_SIZE = 8 + 32 + 32 + 4 + 8 + 1 + 8 + 1 + 1;
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const secondsOf = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 3_600 + m * 60;
};

// A trade with `cash` (cash-token base units) settles delivery versus payment through Solana DvP (ops/src/dvp.ts):
// the seller's bonds and the buyer's cash wait in the trade's escrows and move in one transaction, settled by the
// registrar. Without `cash` it is a plain hooked transfer (a gift, or a trade settled elsewhere).
type Trade = { afterRecordMoment: number; from: string; to: string; amount: number; cash?: string };
// Third corporate action (docs/buyback-tender.md). Window ends are bond times relative to record moments.
type OfferAction = { afterRecordMoment: number; holder: string; sell?: number; tender?: number; withdraw?: number };
type OfferSpec = {
  id: number;
  kind: "buyback" | "tender";
  priceUnits: string;
  maxQuantity: number;
  open: { recordMoment: number; plusDays: number };
  close: { recordMoment: number; plusDays: number };
  actions: OfferAction[];
};
// Scenario input (scenarios/*.json): the single input the operator acts on and the independent auditor measures
// against (expertise/independent-ledger-audit.md).
type Scenario = {
  name: string;
  terms: {
    issue: string;
    maturity: string;
    monthsPerPeriod: number;
    periodsPerYear: number;
    faceUnits: string;
    cashDecimals: number;
    rateBps: number;
    redemptionPriceBps: number;
    recordTimeLocal: string;
    paymentOpensLocal: string;
    utcOffsetHours: number;
    recordBusinessDaysBeforePayment: number;
    /** Issue size in bonds: the most that may ever be placed. */
    maxSupply: number;
  };
  calendar: string;
  weekend: string[];
  bondTimeAtCreation: string;
  chainSecondsPerBondDay: number;
  keys: { bondMint: string; cashMint: string; cashAuthority: string };
  wallets: Record<string, string>;
  placements: Record<string, number>;
  trades: Trade[];
  offers: OfferSpec[];
  // Operator failure: our operator stops after this coupon; a separate wallet with no role in the bond services
  // the remaining coupons and the redemption after the final record moment. null = no handover.
  handover: null | { operatorStopsAfterCoupon: number; keeper: string };
  // Hostile actor (ops/src/attacks.ts): tries to take bonds or cash at each step; absent = no attacks.
  attacker?: HostileSpec;
};

const readKeypair = (file: string) =>
  Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(file, "utf8"))));
const dayNumber = (iso: string) => Math.round(Date.parse(`${iso}T00:00:00Z`) / 1000 / DAY);
const iso = (unix: number) => new Date(unix * 1000).toISOString();
const dayToUnix = (day: number) => day * DAY;

async function main() {
  const [scenarioFile, out, RPC, cluster] = process.argv.slice(2);
  if (!scenarioFile || !out || !RPC || (cluster !== "localnet" && cluster !== "devnet"))
    throw new Error("usage: scenario.ts <scenarios/NAME.json> <report.json> <rpc url> <localnet|devnet>");
  const explorer = (sig: string) => (cluster === "devnet" ? `https://explorer.solana.com/tx/${sig}?cluster=devnet` : undefined);
  const spec: Scenario = JSON.parse(fs.readFileSync(path.resolve(scenarioFile), "utf8"));
  const key = path.basename(scenarioFile, ".json");
  const fromRoot = (p: string) => path.join(ROOT, p);
  const holderNames = Object.keys(spec.wallets);

  // Public RPC drops connections now and then ("fetch failed"; it stopped the second 1000-holder run): retry the same
  // HTTP request until it gets through, waiting at most one bond day of chain time between tries (10 s for 429). Whether the run is
  // still on schedule after an outage is decided by the phase guard, not here. Rate limits (429) are retried by
  // web3.js itself. Resending a signed transaction is safe: same signature.
  const retryingFetch = async (input: any, init?: any) => {
    for (let attempt = 1; ; attempt++) {
      let failure: string;
      try {
        const res = await fetch(input, init);
        // Devnet answers 429 "Connection rate limits exceeded" in bursts (it stopped the third 1000-holder run after
        // web3.js's own five retries): treat it like a dropped connection.
        if (res.status !== 429) return res;
        failure = "429";
      } catch (e) {
        failure = String((e as any)?.cause?.code ?? e);
      }
      console.log(`RPC request failed (${failure}), retry ${attempt}`);
      // 429: back off up to devnet's 10-second rate-limit window. Retrying every chain second (1 bond day in the hostile
      // run) with a few requests in flight kept this IP locked out for over 5 minutes (2026-10-10); a 10-second pause
      // cleared it.
      const capSeconds = failure === "429" ? Math.max(10, spec.chainSecondsPerBondDay) : spec.chainSecondsPerBondDay;
      await new Promise((r) => setTimeout(r, 1_000 * Math.min(2 ** attempt, capSeconds)));
    }
  };
  const connection = new Connection(RPC, { commitment: "confirmed", fetch: retryingFetch as any });
  const registrar = readKeypair(path.join(os.homedir(), ".config/solana/id.json"));
  const bank = readKeypair(fromRoot(spec.keys.cashAuthority));
  const cashMint = readKeypair(fromRoot(spec.keys.cashMint)).publicKey;
  const provider = new anchor.AnchorProvider(connection, new anchor.Wallet(registrar), { commitment: "confirmed" });
  const idl = JSON.parse(fs.readFileSync(path.join(ROOT, "target/idl/bond_servicing.json"), "utf8"));
  const program = new anchor.Program(idl, provider);
  const eventParser = new anchor.EventParser(program.programId, new anchor.BorshCoder(idl));
  const log: { step: string; signature: string; explorer?: string; chainTime: string; bondTime?: string }[] = [];

  const pda = (...seeds: (Buffer | Uint8Array)[]) => PublicKey.findProgramAddressSync(seeds, program.programId)[0];
  const u16 = (n: number) => Buffer.from(new Uint16Array([n]).buffer);
  const chainNow = async () => {
    const info = await connection.getAccountInfo(SYSVAR_CLOCK_PUBKEY);
    return Number(info!.data.readBigInt64LE(32));
  };
  const record = async (step: string, signature: string, bondTime?: number) => {
    const at = iso(await chainNow());
    log.push({ step, signature, explorer: explorer(signature), chainTime: at, bondTime: bondTime ? iso(bondTime) : undefined });
    console.log(`${at}  ${step}  ${explorer(signature) ?? signature}`);
  };
  // Many holders per transaction need more than the default 200,000 compute units.
  const computeLimit = ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });
  const chunks = <T,>(items: T[], size: number) => Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));
  const span = (names: string[]) => (names.length === 1 ? names[0] : `${names[0]}..${names[names.length - 1]}`);
  // Reach a chain time: jump the clock on Surfpool; wait for the real clock on devnet.
  const reachChainTime = async (chainTarget: number) => {
    if (cluster === "devnet") {
      for (let now = await chainNow(); now < chainTarget; now = await chainNow()) {
        await new Promise((r) => setTimeout(r, Math.min(2_000, (chainTarget - now) * 1_000)));
      }
      return;
    }
    if (chainTarget <= (await chainNow())) return;
    const res = await fetch(RPC, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "surfnet_timeTravel", params: [{ absoluteTimestamp: chainTarget * 1000 }] }),
    });
    const body = await res.json();
    if (body.error) throw new Error(`time travel failed: ${JSON.stringify(body.error)}`);
  };
  // Public RPC can report a transaction as expired although it landed (seen on devnet 2026-10-08). On that error,
  // ask for the signature's status: landed -> use it; not landed -> send again with a fresh blockhash.
  const land = async (what: string, sendOnce: () => Promise<string>) => {
    for (let attempt = 1; attempt <= 4; attempt++) {
      try {
        return await sendOnce();
      } catch (e: any) {
        if (!/block height exceeded|expired/i.test(String(e?.message ?? e))) throw e;
        if (e?.signature) {
          const st = (await connection.getSignatureStatus(e.signature, { searchTransactionHistory: true })).value;
          if (st && !st.err && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) return e.signature as string;
        }
        console.log(`${what}: not landed (attempt ${attempt}), sending again`);
      }
    }
    throw new Error(`${what}: not landed after 4 attempts`);
  };
  const send = async (step: string, ixs: anchor.web3.TransactionInstruction[], signers: Keypair[]) => {
    const signature = await land(step, () => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), signers, {
      commitment: "confirmed",
    }));
    await record(step, signature);
    return signature;
  };

  // Schedule from bond_math with the bond's calendar (holiday file and weekend from the scenario).
  const schedule = JSON.parse(
    execFileSync(
      "cargo",
      [
        "run", "-q", "-p", "bond_math", "--example", "schedule", "--", spec.terms.issue, spec.terms.maturity,
        String(spec.terms.monthsPerPeriod), spec.calendar, spec.weekend.join(","), String(spec.terms.recordBusinessDaysBeforePayment),
      ],
      { cwd: ROOT, encoding: "utf8" },
    ),
  ) as { payment_days: number[]; payment_dates: string[]; record_days: number[]; record_dates: string[] };
  const periods = schedule.payment_days.length;

  // Bond time at creation comes from the scenario; chain time is whatever the network clock says now.
  const bondOrigin = Date.parse(spec.bondTimeAtCreation) / 1000;
  const mint = readKeypair(fromRoot(spec.keys.bondMint));
  const bond = pda(Buffer.from("bond"), mint.publicKey.toBuffer());
  const terms = {
    faceUnits: new BN(spec.terms.faceUnits),
    rateBps: spec.terms.rateBps,
    basis: { fixedPerPeriod: { periodsPerYear: spec.terms.periodsPerYear } },
    redemptionPriceBps: spec.terms.redemptionPriceBps,
    issueDay: dayNumber(spec.terms.issue),
    maturityDay: dayNumber(spec.terms.maturity),
    monthsPerPeriod: spec.terms.monthsPerPeriod,
    paymentDays: schedule.payment_days,
    recordDays: schedule.record_days,
    recordSecondsLocal: secondsOf(spec.terms.recordTimeLocal),
    paymentSecondsLocal: secondsOf(spec.terms.paymentOpensLocal),
    utcOffsetSeconds: spec.terms.utcOffsetHours * 3_600,
    weekendDays: spec.weekend.reduce((mask, d) => mask | (1 << WEEKDAYS.indexOf(d)), 0),
    bondOrigin: new BN(bondOrigin),
    chainSecondsPerBondDay: new BN(spec.chainSecondsPerBondDay),
    maxSupply: new BN(spec.terms.maxSupply),
  };
  // A run that stopped after creating the bond resumes it (run-devnet.sh <name> resume): the bond and every holder
  // already registered are read from the chain, not created again.
  if (await connection.getAccountInfo(bond)) console.log(`resuming bond ${bond.toBase58()} (already on chain)`);
  else await record(
    "create_bond",
    await land("rpc", () => program.methods
      .createBond(terms)
      .accountsPartial({
        registrar: registrar.publicKey, mint: mint.publicKey, bond, cashMint,
        extraAccountMetaList: pda(Buffer.from("extra-account-metas"), mint.publicKey.toBuffer()),
        tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
      })
      .signers([mint])
      .rpc()),
  );
  const bondAccount: any = await program.account.bond.fetch(bond);
  const chainOrigin = Number(bondAccount.chainOrigin);
  // Earliest chain time at which the program's bond clock reaches bondTime (same mapping as bond_math::time).
  const chainTimeFor = (bondTime: number) =>
    chainOrigin + Math.ceil(((bondTime - bondOrigin) * spec.chainSecondsPerBondDay) / DAY);

  // Holders, registry, placement: a few holders per transaction; the registrar pays every fee and deposit, so holders
  // need no SOL of their own (except tenderers, who pay their tender record's deposit).
  const holders: Record<string, Keypair> = {};
  for (const name of holderNames) holders[name] = readKeypair(fromRoot(spec.wallets[name]));
  const nameOf = new Map(holderNames.map((n) => [holders[n].publicKey.toBase58(), n]));
  const bondsAta = (name: string) => getAssociatedTokenAddressSync(mint.publicKey, holders[name].publicKey, false, TOKEN_2022_PROGRAM_ID);
  const cashAta = (name: string) => getAssociatedTokenAddressSync(cashMint, holders[name].publicKey, false, TOKEN_PROGRAM_ID);
  const holderRecord = (name: string) => pda(Buffer.from("holder"), mint.publicKey.toBuffer(), holders[name].publicKey.toBuffer());
  // Token account balances, 100 accounts per request (amount = bytes 64..72; a missing account holds 0).
  const balances = async (accounts: PublicKey[]) => {
    const out: bigint[] = [];
    for (const part of chunks(accounts, 100)) {
      const infos = await connection.getMultipleAccountsInfo(part, "confirmed");
      out.push(...infos.map((i) => (i && i.data.length >= 72 ? i.data.readBigUInt64LE(64) : 0n)));
    }
    return out;
  };
  const balance = async (account: PublicKey) => (await balances([account]))[0];
  // Building a hooked transfer looks up the mint, the hook's account list and both token accounts (for their owners,
  // which seed the holder records). None of those fields change, so each account is looked up once per run, not once
  // per transfer: per-transfer lookups made trades the slowest step on devnet (7.7 s per trade in phase 1 of the third
  // 1000-holder run, against 1.5 s per payment transaction). The builder calls only getAccountInfo.
  const lookedUp = new Map<string, Promise<anchor.web3.AccountInfo<Buffer> | null>>();
  const staticLookups = {
    getAccountInfo: (key: PublicKey, commitment?: anchor.web3.Commitment) => {
      const k = key.toBase58();
      if (!lookedUp.has(k)) lookedUp.set(k, connection.getAccountInfo(key, commitment));
      return lookedUp.get(k)!;
    },
  } as unknown as Connection;
  const records = async (names: string[]) => {
    const out: any[] = [];
    for (const part of chunks(names, 100)) out.push(...(await program.account.holderRecord.fetchMultiple(part.map(holderRecord))));
    return out;
  };
  const registered = await records(holderNames);
  const toSetUp = holderNames.filter((_, i) => !registered[i]);
  if (toSetUp.length < holderNames.length) console.log(`${holderNames.length - toSetUp.length} holders already registered; setting up ${toSetUp.length}`);
  for (const batch of chunks(toSetUp, 3)) {
    const ixs: anchor.web3.TransactionInstruction[] = [computeLimit];
    for (const name of batch) {
      const kp = holders[name];
      ixs.push(await program.methods.registerHolder(kp.publicKey).accountsPartial({ registrar: registrar.publicKey, bond }).instruction());
      ixs.push(
        createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, bondsAta(name), kp.publicKey, mint.publicKey, TOKEN_2022_PROGRAM_ID),
        createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, cashAta(name), kp.publicKey, cashMint, TOKEN_PROGRAM_ID),
      );
      const amount = spec.placements[name];
      if (amount)
        ixs.push(await program.methods.place(new BN(amount))
          .accountsPartial({ registrar: registrar.publicKey, bond, mint: mint.publicKey, holder: holderRecord(name), holderBonds: bondsAta(name), tokenProgram: TOKEN_2022_PROGRAM_ID })
          .instruction());
    }
    await send(`register, open accounts, place: ${batch.map((n) => `${n}${spec.placements[n] ? ` ${spec.placements[n]}` : ""}`).join(", ")}`, ixs, [registrar]);
  }
  // Holders may already hold the cash token (other bonds paid in it, earlier runs on the same cluster), so the report
  // carries each holder's cash before this bond paid anything; what this bond delivered is final - initial.
  const initialCashList = await balances(holderNames.map(cashAta));
  const initialCash = Object.fromEntries(holderNames.map((h, i) => [h, initialCashList[i].toString()]));

  const paidEvents = async (signature: string) => {
    const tx = await connection.getTransaction(signature, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
    return [...eventParser.parseLogs(tx!.meta!.logMessages!)];
  };
  // A holder's quantity at record moment e, from its on-chain history (state.rs, PositionHistory).
  const positionOf = (rec: any, e: number) => Number(e < rec.position.liveFrom ? rec.position.at[e] : rec.position.quantity);
  // Who sends the servicing transactions (open, confirm, pay, redeem): anyone may. By default the operator (the
  // registrar's wallet); after a handover, a separate wallet with no role in the bond pays the fees itself.
  type Servicer = { program: anchor.Program; payer: PublicKey; tag: string };
  const operator: Servicer = { program, payer: registrar.publicKey, tag: "" };
  let outside: Servicer | null = null;
  if (spec.handover) {
    const kp = readKeypair(fromRoot(spec.handover.keeper));
    if ((await connection.getBalance(kp.publicKey)) < 0.05 * LAMPORTS_PER_SOL)
      await send("fund outside keeper fees", [SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: kp.publicKey, lamports: 0.05 * LAMPORTS_PER_SOL })], [registrar]);
    const keeperProvider = new anchor.AnchorProvider(connection, new anchor.Wallet(kp), { commitment: "confirmed" });
    outside = { program: new anchor.Program(idl, keeperProvider), payer: kp.publicKey, tag: " [outside keeper]" };
  }
  // Open an event (anyone, after its record moment), fund its vault with exactly the budget (the issuer's bank:
  // simulated fiat leg; the issuer's only role here), confirm the funding.
  const openAndFund = async (e: number, label: string, sv: Servicer) => {
    const event = pda(Buffer.from("event"), bond.toBuffer(), u16(e));
    const vault = pda(Buffer.from("vault"), event.toBuffer());
    await record(`open_event ${label}${sv.tag}`, await land("rpc", () => sv.program.methods.openEvent(e).accountsPartial({ payer: sv.payer, bond, event, cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).rpc()));
    const opened: any = await program.account.event.fetch(event);
    const budget = BigInt(opened.budget.toString());
    if (h && e < periods) await h.whileUnfunded(e, event, vault, budget);
    await send(`fund vault for ${label} (issuer)`, [createMintToInstruction(cashMint, vault, bank.publicKey, budget - (await balance(vault)), [], TOKEN_PROGRAM_ID)], [registrar, bank]);
    await record(`confirm_funding ${label}${sv.tag}`, await land("rpc", () => sv.program.methods.confirmFunding().accountsPartial({ event, vault }).rpc()));
    return { event, vault, opened };
  };

  const events: any[] = [];
  const vaults: string[] = [];

  // Offers: created and fully funded up front; actions happen in the phase after their record moment.
  const bondTerms: any = await program.account.bond.fetch(bond);
  const recordTimeOf = (k: number) =>
    dayToUnix(Number(bondTerms.recordDays[k])) + Number(bondTerms.recordSecondsLocal) - Number(bondTerms.utcOffsetSeconds);
  const offerPda = (id: number) => pda(Buffer.from("offer"), bond.toBuffer(), u16(id));
  const offerTimes = (o: OfferSpec) => ({
    open: recordTimeOf(o.open.recordMoment) + o.open.plusDays * DAY,
    close: recordTimeOf(o.close.recordMoment) + o.close.plusDays * DAY,
  });
  const offerReports: any[] = [];
  for (const o of spec.offers) {
    const offer = offerPda(o.id);
    const vault = pda(Buffer.from("vault"), offer.toBuffer());
    const { open, close } = offerTimes(o);
    await record(
      `create_offer ${o.id} (${o.kind})`,
      await land("rpc", () => program.methods
        .createOffer(o.id, o.kind === "tender", new BN(o.priceUnits), new BN(o.maxQuantity), new BN(open), new BN(close))
        .accountsPartial({ registrar: registrar.publicKey, bond, offer, cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID })
        .rpc()),
    );
    const budget = BigInt(o.priceUnits) * BigInt(o.maxQuantity);
    await send(`fund vault for offer ${o.id}`, [createMintToInstruction(cashMint, vault, bank.publicKey, budget, [], TOKEN_PROGRAM_ID)], [registrar, bank]);
    await record(`confirm_offer_funding ${o.id}`, await land("rpc", () => program.methods.confirmOfferFunding().accountsPartial({ offer, vault }).rpc()));
    vaults.push(vault.toBase58());
    offerReports.push({
      id: o.id, kind: o.kind, priceUnits: o.priceUnits, maxQuantity: o.maxQuantity, openTime: iso(open), closeTime: iso(close),
      vault: vault.toBase58(), budget: budget.toString(), actions: [] as any[], settlements: [] as any[], settled: false,
    });
  }
  // Accounts to burn a holder's bonds and pay from an offer's vault (the program's `Retirement`).
  const retirementAccounts = (o: OfferSpec, name: string, payer: PublicKey) => {
    const offer = offerPda(o.id);
    return {
      payer, bond, mint: mint.publicKey, holder: holderRecord(name), owner: holders[name].publicKey,
      holderBonds: bondsAta(name), vault: pda(Buffer.from("vault"), offer.toBuffer()), cashMint, holderCash: cashAta(name),
      memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID,
    };
  };
  const tenderDeposit = await connection.getMinimumBalanceForRentExemption(TENDER_ACCOUNT_SIZE);
  const h = spec.attacker
    ? hostile({
      connection, program, registrar, bank, attacker: readKeypair(fromRoot(spec.attacker.wallet)), spec: spec.attacker, terms,
      mint: mint.publicKey, bond, cashMint, cashDecimals: spec.terms.cashDecimals, holders, bondsAta, cashAta, holderRecord, pda, u16,
      offers: spec.offers, offerPda, retirementAccounts, send, chainNow, explorer: (sig) => explorer(sig),
    })
    : null;
  await h?.beforeFirstRecordMoment();
  const offerActions = async (e: number) => {
    for (const [i, o] of spec.offers.entries()) {
      for (const act of o.actions.filter((a) => a.afterRecordMoment === e)) {
        await reachChainTime(chainTimeFor(offerTimes(o).open));
        const owner = holders[act.holder].publicKey;
        let label: string, signature: string, quantity: number;
        if (act.sell) {
          quantity = act.sell;
          label = `sell_to_offer ${o.id}: ${act.holder} sells ${quantity}`;
          signature = await land("rpc", () => program.methods.sellToOffer(new BN(quantity))
            .accountsPartial({ retirement: retirementAccounts(o, act.holder, owner), seller: owner, offer: offerPda(o.id) })
            .signers([holders[act.holder]]).rpc());
        } else {
          const withdraw = !act.tender;
          quantity = (act.tender ?? act.withdraw)!;
          // The registrar tops up the holder's tender record deposit in the same transaction (one transaction per
          // tender, not two).
          const have = await connection.getBalance(owner);
          const ixs: anchor.web3.TransactionInstruction[] = have < tenderDeposit
            ? [SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: owner, lamports: tenderDeposit - have })] : [];
          ixs.push(await program.methods.changeTender(new BN(quantity), withdraw)
            .accountsPartial({ owner, bond, offer: offerPda(o.id), holder: holderRecord(act.holder), tender: pda(Buffer.from("tender"), offerPda(o.id).toBuffer(), owner.toBuffer()) })
            .instruction());
          label = `change_tender ${o.id}: ${act.holder} ${withdraw ? "withdraws" : "tenders"} ${quantity}${ixs.length > 1 ? " (registrar funds the deposit)" : ""}`;
          signature = await land("rpc", () => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), [registrar, holders[act.holder]], { commitment: "confirmed" }));
        }
        await record(label, signature);
        const sold = (await paidEvents(signature)).find((x) => x.name === "BondsBoughtBack");
        offerReports[i].actions.push({
          holder: act.holder, action: act.sell ? "sell" : act.tender ? "tender" : "withdraw", quantity, signature,
          ...(sold ? { burned: Number(sold.data.burned), paid: sold.data.paid.toString() } : {}),
        });
      }
    }
  };
  // Tender offers whose window closes before the next record moment are settled in this phase, by anyone.
  const settleTenders = async (e: number) => {
    const next = e + 1 < periods ? recordTimeOf(e + 1) : Infinity;
    for (const [i, o] of spec.offers.entries()) {
      if (o.kind !== "tender" || offerReports[i].settled || offerTimes(o).close >= next) continue;
      await reachChainTime(chainTimeFor(offerTimes(o).close));
      const tendered = new Map<string, number>();
      for (const a of o.actions) tendered.set(a.holder, (tendered.get(a.holder) ?? 0) + (a.tender ?? 0) - (a.withdraw ?? 0));
      // Tenders are resolved in the order they were made (the order of each holder's first tender, as sent above).
      // Resolving needs no cash, so all are resolved first: a holder whose cash cannot move blocks nobody else.
      const tenderPda = (name: string) => pda(Buffer.from("tender"), offerPda(o.id).toBuffer(), holders[name].publicKey.toBuffer());
      for (const name of tendered.keys()) {
        await record(`resolve_tender ${o.id} ${name}`, await land("rpc", () => program.methods.resolveTender()
          .accountsPartial({ bond, offer: offerPda(o.id), holder: holderRecord(name), tender: tenderPda(name) }).rpc()));
      }
      for (const [name, q] of tendered) {
        if (q <= 0) continue;
        const signature = await land("rpc", () => program.methods.settleTender()
          .accountsPartial({ retirement: retirementAccounts(o, name, registrar.publicKey), offer: offerPda(o.id), tender: pda(Buffer.from("tender"), offerPda(o.id).toBuffer(), holders[name].publicKey.toBuffer()) })
          .rpc());
        await record(`settle_tender ${o.id} ${name}`, signature);
        const sold = (await paidEvents(signature)).find((x) => x.name === "BondsBoughtBack");
        offerReports[i].settlements.push({
          holder: name, tendered: q, burned: sold ? Number(sold.data.burned) : 0, paid: sold ? sold.data.paid.toString() : "0", signature,
        });
      }
      offerReports[i].settled = true;
    }
  };
  // One coupon: open, fund, confirm, then pay every holder with a position at the record moment.
  const serviceCoupon = async (e: number, sv: Servicer) => {
    const bondAccountNow: any = await program.account.bond.fetch(bond);
    const recordTime = dayToUnix(Number(bondAccountNow.recordDays[e])) + Number(bondAccountNow.recordSecondsLocal) - Number(bondAccountNow.utcOffsetSeconds);
    const paymentTime = dayToUnix(Number(bondAccountNow.paymentDays[e])) + Number(bondAccountNow.paymentSecondsLocal) - Number(bondAccountNow.utcOffsetSeconds);
    const { event, vault, opened } = await openAndFund(e, `coupon ${e}`, sv);
    vaults.push(vault.toBase58());
    await reachChainTime(chainTimeFor(paymentTime));
    const positions: any[] = [];
    const payments: any[] = [];
    const recs = await records(holderNames);
    const bondBalances = await balances(holderNames.map(bondsAta));
    holderNames.forEach((name, i) => positions.push({ holder: name, position: positionOf(recs[i], e), balanceNow: Number(bondBalances[i]) }));
    const payable = holderNames.filter((_, i) => positionOf(recs[i], e) > 0);
    const pay = async (names: string[]) => {
      for (const batch of chunks(names, 6)) {
        const ixs: anchor.web3.TransactionInstruction[] = [computeLimit];
        for (const name of batch)
          ixs.push(await sv.program.methods.payCoupon()
            .accountsPartial({ payer: sv.payer, bond, event, holder: holderRecord(name), owner: holders[name].publicKey, vault, cashMint, holderCash: cashAta(name), memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID })
            .instruction());
        const signature = await land("pay_coupon", () => sv.program.provider.sendAndConfirm!(new Transaction().add(...ixs), [], { commitment: "confirmed" }));
        await record(`pay_coupon ${e} ${span(batch)}${sv.tag}`, signature);
        for (const ev of (await paidEvents(signature)).filter((x) => x.name === "CouponPaid"))
          payments.push({ holder: nameOf.get(ev.data.owner.toBase58()), amount: ev.data.amount.toString(), position: Number(ev.data.position), signature, payer: sv.payer.toBase58() });
      }
    };
    // A holder the bank froze is paid after everyone else, once thawed.
    const heldBack = (await h?.beforePayments(e)) ?? [];
    await pay(payable.filter((n) => !heldBack.includes(n)));
    await h?.afterPayments(e);
    await pay(payable.filter((n) => heldBack.includes(n)));
    events.push({
      index: e, kind: "coupon", paymentDate: schedule.payment_dates[e], recordDate: schedule.record_dates[e],
      recordTime: iso(recordTime), supplyAtRecord: Number(opened.supplyAtRecord), budget: opened.budget.toString(),
      vault: vault.toBase58(), vaultLeft: (await balance(vault)).toString(), positions, payments, servicedBy: sv.payer.toBase58(),
    });
  };
  const deferred: number[] = [];
  // Cash the bank gave buyers for DvP trades (simulated fiat): not this bond's payments, netted out by the checks.
  const bankTopUps: Record<string, bigint> = {};

  // Coupons: at each record moment, the scenario's trades first (the market stays open), then the payments.
  for (let e = 0; e < periods; e++) {
    const bondAccountNow: any = await program.account.bond.fetch(bond);
    const recordTime = dayToUnix(Number(bondAccountNow.recordDays[e])) + Number(bondAccountNow.recordSecondsLocal) - Number(bondAccountNow.utcOffsetSeconds);
    await reachChainTime(chainTimeFor(recordTime) + (cluster === "devnet" ? 2 : 60));
    // The scenario's actions for this phase must land before the next record moment, or positions would differ from
    // the scenario's; stop instead of producing a run that does not match its own input.
    const phaseEnd = e + 1 < periods ? chainTimeFor(dayToUnix(Number(bondAccountNow.recordDays[e + 1])) + Number(bondAccountNow.recordSecondsLocal) - Number(bondAccountNow.utcOffsetSeconds)) : Infinity;
    const inPhase = async (what: string) => {
      if ((await chainNow()) >= phaseEnd) throw new Error(`phase ${e} overran the next record moment before: ${what}`);
    };
    for (const t of spec.trades.filter((t) => t.afterRecordMoment === e)) {
      await inPhase(`transfer ${t.from} -> ${t.to}`);
      if (t.cash === undefined) {
        const ix = await createTransferCheckedWithTransferHookInstruction(
          staticLookups, bondsAta(t.from), mint.publicKey, bondsAta(t.to), holders[t.from].publicKey, BigInt(t.amount), 0, [], "confirmed", TOKEN_2022_PROGRAM_ID,
        );
        await send(`transfer ${t.amount} ${t.from} -> ${t.to} (after record moment ${e})`, [ix], [registrar, holders[t.from]]);
        continue;
      }
      // Delivery versus payment through Solana DvP: create the trade and admit it (one transaction), the seller funds
      // the bond leg, the buyer the cash leg (the issuer's bank gives the buyer the cash: simulated fiat), then the
      // registrar settles both legs in one transaction.
      const quantity = BigInt(t.amount);
      const cash = BigInt(t.cash);
      const seller = holders[t.from].publicKey;
      const buyer = holders[t.to].publicKey;
      const nonce = randomNonce();
      const expiry = BigInt((await chainNow()) + 86_400);
      const { ix: create, swap } = createDvpInstruction(registrar.publicKey, seller, buyer, mint.publicKey, cashMint, TOKEN_PROGRAM_ID, quantity, cash, nonce, expiry);
      const admit = await admitTradeInstruction(program, registrar.publicKey, bond, mint.publicKey, swap, seller, buyer);
      await send(`dvp create + admit_trade: ${t.from} sells ${t.amount} to ${t.to} for ${t.cash} (after record moment ${e})`, [create, admit], [registrar]);
      await send(`dvp fund bond leg: ${t.from} -> escrow ${t.amount}`, [await fundBondLegInstruction(connection, mint.publicKey, seller, swap, quantity)], [registrar, holders[t.from]]);
      const have = await balance(cashAta(t.to));
      if (have < cash) {
        await send(`fund ${t.to}'s cash (issuer's bank, simulated fiat)`, [createMintToInstruction(cashMint, cashAta(t.to), bank.publicKey, cash - have, [], TOKEN_PROGRAM_ID)], [registrar, bank]);
        bankTopUps[t.to] = (bankTopUps[t.to] ?? 0n) + (cash - have);
      }
      await send(`dvp fund cash leg: ${t.to} -> escrow ${t.cash}`, [fundCashLegInstruction(cashMint, TOKEN_PROGRAM_ID, spec.terms.cashDecimals, buyer, swap, cash)], [registrar, holders[t.to]]);
      const trade = { swap, nonce, seller, buyer, quantity, cash };
      await send(`dvp settle: ${t.amount} bonds -> ${t.to}, ${t.cash} cash -> ${t.from}, one transaction`, [await settleDvpInstruction(connection, program.programId, registrar.publicKey, trade, mint.publicKey, cashMint, TOKEN_PROGRAM_ID)], [registrar]);
    }
    await h?.afterRecordMoment(e);
    await offerActions(e);
    await h?.afterOfferActions(e);
    await inPhase("offer actions");
    if (!spec.handover || e <= spec.handover.operatorStopsAfterCoupon) await serviceCoupon(e, operator);
    else deferred.push(e);
    await settleTenders(e);
  }

  // After a handover the outside keeper services, late, every coupon the operator left.
  for (const e of deferred) await serviceCoupon(e, outside!);

  // Redemption: trading closed at the final record moment; burn and pay each holder.
  const r = periods;
  const redeemer = spec.handover ? outside! : operator;
  await h?.afterFinalRecordMoment();
  const { event: redemption, vault: redemptionVault, opened: redemptionOpened } = await openAndFund(r, "redemption", redeemer);
  vaults.push(redemptionVault.toBase58());
  await h?.beforeRedemption(redemption, redemptionVault);
  const redemptions: any[] = [];
  const finalRecs = await records(holderNames);
  const redeemable = holderNames.filter((_, i) => Number(finalRecs[i].position.quantity) > Number(finalRecs[i].locked));
  for (const batch of chunks(redeemable, 4)) {
    const ixs: anchor.web3.TransactionInstruction[] = [computeLimit];
    for (const name of batch)
      ixs.push(await redeemer.program.methods.redeemHolder()
        .accountsPartial({ payer: redeemer.payer, bond, mint: mint.publicKey, event: redemption, holder: holderRecord(name), owner: holders[name].publicKey, holderBonds: bondsAta(name), vault: redemptionVault, cashMint, holderCash: cashAta(name), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID })
        .instruction());
    const signature = await land("redeem", () => redeemer.program.provider.sendAndConfirm!(new Transaction().add(...ixs), [], { commitment: "confirmed" }));
    await record(`redeem_holder ${span(batch)}${redeemer.tag}`, signature);
    for (const ev of (await paidEvents(signature)).filter((x) => x.name === "BondsRedeemed"))
      redemptions.push({ holder: nameOf.get(ev.data.owner.toBase58()), burned: Number(ev.data.burned), principal: ev.data.principal.toString(), signature });
  }
  await h?.afterRedemption(redemption, redemptionVault);
  const supply = BigInt((await connection.getTokenSupply(mint.publicKey)).value.amount);

  const report = {
    scenario: key,
    name: spec.name,
    programId: program.programId.toBase58(),
    bond: bond.toBase58(),
    bondMint: mint.publicKey.toBase58(),
    cashMint: cashMint.toBase58(),
    terms: {
      issue: spec.terms.issue, maturity: spec.terms.maturity, monthsPerPeriod: spec.terms.monthsPerPeriod, faceUnits: spec.terms.faceUnits,
      cashDecimals: spec.terms.cashDecimals, rateBps: spec.terms.rateBps, basis: `FixedPerPeriod(${spec.terms.periodsPerYear})`, redemptionPriceBps: spec.terms.redemptionPriceBps,
      paymentDates: schedule.payment_dates, recordDates: schedule.record_dates, recordTimeLocal: spec.terms.recordTimeLocal,
      paymentOpensLocal: spec.terms.paymentOpensLocal, utcOffsetHours: spec.terms.utcOffsetHours,
      calendar: spec.calendar, weekend: spec.weekend,
    },
    placements: spec.placements,
    trades: spec.trades,
    offerSpecs: spec.offers,
    handover: spec.handover,
    events,
    offers: await Promise.all(offerReports.map(async (o) => ({ ...o, vaultLeft: (await balance(new PublicKey(o.vault))).toString() }))),
    redemption: {
      supplyAtRecord: Number(redemptionOpened.supplyAtRecord), budget: redemptionOpened.budget.toString(),
      vault: redemptionVault.toBase58(), vaultLeft: (await balance(redemptionVault)).toString(), redemptions,
    },
    initialCash,
    bankTopUps: Object.fromEntries(Object.entries(bankTopUps).map(([h, v]) => [h, v.toString()])),
    final: {
      supply: supply.toString(),
      vaults: Object.fromEntries(await Promise.all(vaults.map(async (v) => [v, (await balance(new PublicKey(v))).toString()]))),
      cash: Object.fromEntries((await balances(holderNames.map(cashAta))).map((b, i) => [holderNames[i], b.toString()])),
    },
    transactions: log,
    attempts: h?.attempts ?? [],
  };
  fs.mkdirSync(path.dirname(out), { recursive: true });
  fs.writeFileSync(out, JSON.stringify(report, null, 1) + "\n");
  console.log(`report: ${out} (${log.length} transactions)`);
  if (h) {
    const failed = h.attempts.filter((a) => !a.ok);
    console.log(`attacks: ${h.attempts.length - failed.length} of ${h.attempts.length} refused by their guard`);
    if (failed.length) throw new Error(`attacks not refused by their guard: ${failed.map((a) => `${a.id} (${a.outcome}${a.reason ? `: ${a.reason}` : ""})`).join("; ")}`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
