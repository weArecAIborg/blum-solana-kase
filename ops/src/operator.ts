// Operator service behind the operator screen (docs/demo/operator-screen.html, blum.caiborg.ai/operator-screen).
// Anyone creates a bond on devnet with their own terms; the service signs as its registrar and:
//   - registers the generated holders BH#1..n and places the issue among them;
//   - knows every corporate action from the terms (each coupon, the redemption) and runs each when its time comes
//     if its "auto" box is ticked, or step by step when the operator presses the step;
//   - lets the holders trade among themselves through Solana DvP (a simple random generator: each bond day each
//     holder trades with a probability that moves from the start-of-life to the end-of-life propensity, at most T
//     trades per holder), and answer the buybacks and tenders the operator adds;
//   - runs the ledger-only audit (tools/audit/audit_json.py) on the bond's public data.
// 1 bond day = 1 chain second. Market rules are the sample's (KASE, docs/bond-mechanics.md).
// Usage (from ops/): npx tsx src/operator.ts
//   env OPERATOR_RPC (cluster RPC), OPERATOR_PORT (HTTP port, localhost), OPERATOR_DATA (state directory),
//   OPERATOR_REGISTRAR (keypair: registrar and fee payer), OPERATOR_BANK (keypair: the test cash token's authority),
//   OPERATOR_SCHEDULE (bond_math's `schedule` example binary), OPERATOR_PYTHON (python with QuantLib and FinancePy).
// HTTP: /api/... (below); any other GET serves docs/demo, so the page and the API share one origin. Anyone may watch any
// bond; only its creator acts on it: creating a bond returns a secret token, and every action on the bond must carry it
// (header X-Operator-Token; the page keeps it in the creator's browser).

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import crypto from "node:crypto";
import { execFile, execFileSync } from "node:child_process";
import * as anchor from "@anchor-lang/core";
import {
  MINT_SIZE,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createInitializeMint2Instruction,
  createMintToInstruction,
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
  TransactionInstruction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import BN from "bn.js";
import { admitTradeInstruction, cancelDvpInstruction, createDvpInstruction, fundBondLegInstruction, fundCashLegInstruction, randomNonce, settleDvpInstruction } from "./dvp.ts";

const env = (name: string) => {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not set`);
  return v;
};
const ROOT = path.resolve(import.meta.dirname, "../..");
const RPC = env("OPERATOR_RPC");
const PORT = Number(env("OPERATOR_PORT"));
const DATA = env("OPERATOR_DATA");
const SCHEDULE = env("OPERATOR_SCHEDULE");
const PYTHON = env("OPERATOR_PYTHON");
const readKeypair = (file: string) => Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(file, "utf8"))));
const registrar = readKeypair(env("OPERATOR_REGISTRAR"));
const bank = readKeypair(env("OPERATOR_BANK"));
const FAUCET = "https://faucet.solana.com";

const DAY = 86_400;
// Market rules of the sample bond (Kazakh depository): holders as of 09:00 Almaty on the payment day, payments from
// 09:00 to 17:00, Saturday and Sunday off, the KZ holiday calendar.
const RULES = {
  recordTimeLocal: "09:00", paymentOpensLocal: "09:00", paymentDeadlineLocal: "17:00", utcOffsetHours: 5,
  recordBusinessDaysBeforePayment: 0, weekend: ["Saturday", "Sunday"], calendar: "data/calendars/kz-holidays.txt",
};
const CASH_DECIMALS = 6;
const UNIT = 10n ** BigInt(CASH_DECIMALS);
// No trade starts closer than this to a record moment: a DvP trade is five transactions, about 10 chain seconds.
const MARGIN_DAYS = 10;
// A holder's Tender record (paid by the holder): see ops/src/scenario.ts.
const TENDER_ACCOUNT_SIZE = 8 + 32 + 32 + 4 + 8 + 1 + 8 + 1 + 1;
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const secondsOf = (hhmm: string) => { const [h, m] = hhmm.split(":").map(Number); return h * 3_600 + m * 60; };
const dayNumber = (iso: string) => Math.round(Date.parse(`${iso}T00:00:00Z`) / 1000 / DAY);
const localIso = (bondUnix: number) => new Date((bondUnix + RULES.utcOffsetHours * 3_600) * 1000).toISOString().slice(0, 16).replace("T", " ");

// Devnet answers 429 in bursts and drops connections now and then: retry the same request (ops/src/scenario.ts).
const retryingFetch = async (input: any, init?: any) => {
  for (let attempt = 1; ; attempt++) {
    let failure: string;
    try {
      const res = await fetch(input, init);
      if (res.status !== 429) return res;
      failure = "429";
    } catch (e) {
      failure = String((e as any)?.cause?.code ?? e);
    }
    if (attempt > 8) throw new Error(`RPC request failed ${attempt} times (${failure})`);
    await new Promise((r) => setTimeout(r, 1_000 * Math.min(2 ** attempt, 10)));
  }
};
const connection = new Connection(RPC, { commitment: "confirmed", fetch: retryingFetch as any });
const idl = JSON.parse(fs.readFileSync(path.join(ROOT, "ops/idl/bond_servicing.json"), "utf8"));
const program = new anchor.Program(idl, new anchor.AnchorProvider(connection, new anchor.Wallet(registrar), { commitment: "confirmed" }));
const eventParser = new anchor.EventParser(program.programId, new anchor.BorshCoder(idl));
const computeLimit = () => ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });
const pda = (...seeds: (Buffer | Uint8Array)[]) => PublicKey.findProgramAddressSync(seeds, program.programId)[0];
const u16 = (n: number) => Buffer.from(new Uint16Array([n]).buffer);
const chunks = <T,>(items: T[], size: number) => Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));
const chainNow = async () => Number((await connection.getAccountInfo(SYSVAR_CLOCK_PUBKEY))!.data.readBigInt64LE(32));

// ---------------------------------------------------------------- state

type Params = {
  issue: string; years: number; periodsPerYear: number; face: number; ratePct: number; redemptionPct: number;
  holders: number; issueSize: number; maxTradesPerHolder: number; propensityStart: number; propensityEnd: number;
};
type EventState = {
  index: number; kind: "coupon" | "redemption"; auto: boolean; recordTime: number; paymentTime: number;
  open?: string; fund?: string; confirm?: string; budget?: string; supplyAtRecord?: number;
  paid: Record<string, { amount: string; sig: string }>; done: boolean; error?: string;
};
type OfferAction = { phase: number; holder: string; sell?: number; tender?: number; sig: string; paid?: string };
type OfferState = {
  id: number; kind: "buyback" | "tender"; priceUnits: string; maxQuantity: number; open: number; close: number; auto: boolean;
  create?: string; fund?: string; confirm?: string; settled: boolean; done: boolean; error?: string;
  plans: { holder: string; at: number; share: number }[]; actions: OfferAction[];
  settlements: { holder: string; tendered: number; burned: number; paid: string; sig: string }[];
};
type TradeState = { phase: number; from: string; to: string; amount: number; cash: string; settled: boolean; sigs: string[]; at: number };
type LedgerRow = { chain: number; bond: number; text: string; sig?: string; kind: string };
type BondState = {
  id: string; createdAt: string; params: Params; title: string; status: "creating" | "live" | "done" | "error"; error?: string;
  bondMint: string; cashMint: string; bond: string; holders: { name: string; pubkey: string }[]; placements: Record<string, number>;
  bondOrigin: number; chainOrigin?: number; issueTime: number; maturityTime: number;
  schedule: { payment_days: number[]; payment_dates: string[]; record_days: number[]; record_dates: string[] };
  recordTimes: number[]; events: EventState[]; offers: OfferState[]; trades: TradeState[]; ledger: LedgerRow[];
  tradesUsed: Record<string, number>; decidedDay: number; pending: { from: string; to: string }[]; rng: number;
  bankTopUps: Record<string, string>; audit?: any; auditError?: string; auditRunning?: boolean; token: string;
};
type Keys = { bondMint: number[]; cashMint: number[]; holders: Record<string, number[]> };
type Runtime = { b: BondState; keys: { bondMint: Keypair; cashMint: Keypair; holders: Record<string, Keypair> }; busy: boolean;
  queue: { run: () => Promise<unknown>; done: (v: unknown) => void; fail: (e: unknown) => void }[]; view?: any; viewAt: number };

const bonds = new Map<string, Runtime>();
const dirOf = (id: string) => path.join(DATA, id);
const save = (b: BondState) => {
  const file = path.join(dirOf(b.id), "state.json");
  fs.writeFileSync(file + ".tmp", JSON.stringify(b));
  fs.renameSync(file + ".tmp", file);
};
// mulberry32: the holders' choices are reproducible from the bond's seed and survive a restart (state.rng).
const random = (b: BondState) => {
  let t = (b.rng += 0x6d2b79f5);
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};

// ---------------------------------------------------------------- accounts of one bond

const accountsOf = (rt: Runtime) => {
  const { b, keys } = rt;
  const mint = new PublicKey(b.bondMint), cashMint = new PublicKey(b.cashMint), bond = new PublicKey(b.bond);
  const owner = (h: string) => keys.holders[h].publicKey;
  const bondsAta = (h: string) => getAssociatedTokenAddressSync(mint, owner(h), false, TOKEN_2022_PROGRAM_ID);
  const cashAta = (h: string) => getAssociatedTokenAddressSync(cashMint, owner(h), false, TOKEN_PROGRAM_ID);
  const record = (h: string) => pda(Buffer.from("holder"), mint.toBuffer(), owner(h).toBuffer());
  const event = (e: number) => pda(Buffer.from("event"), bond.toBuffer(), u16(e));
  const offer = (id: number) => pda(Buffer.from("offer"), bond.toBuffer(), u16(id));
  const vault = (account: PublicKey) => pda(Buffer.from("vault"), account.toBuffer());
  const tender = (id: number, h: string) => pda(Buffer.from("tender"), offer(id).toBuffer(), owner(h).toBuffer());
  return { mint, cashMint, bond, owner, bondsAta, cashAta, record, event, offer, vault, tender };
};
const tokenBalance = async (account: PublicKey) => {
  const info = await connection.getAccountInfo(account, "confirmed");
  return info && info.data.length >= 72 ? info.data.readBigUInt64LE(64) : 0n;
};
const positionOf = (rec: any, e: number) => Number(e < rec.position.liveFrom ? rec.position.at[e] : rec.position.quantity);
const nameOf = (rt: Runtime, pubkey: string) => rt.b.holders.find((h) => h.pubkey === pubkey)?.name ?? pubkey;

// Bond time from chain time (the program's own mapping, bond_math::time; 1 bond day = 1 chain second).
const bondTimeOf = (b: BondState, chain: number) => b.bondOrigin + (chain - b.chainOrigin!) * DAY;
const phaseAt = (b: BondState, bondTime: number) => b.recordTimes.filter((t) => t <= bondTime).length - 1;

// Send, retrying a transaction public RPC reports as expired although it landed (ops/src/scenario.ts), and write the
// ledger row.
const land = async (sendOnce: () => Promise<string>) => {
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      return await sendOnce();
    } catch (e: any) {
      if (!/block height exceeded|expired/i.test(String(e?.message ?? e))) throw e;
      if (e?.signature) {
        const st = (await connection.getSignatureStatus(e.signature, { searchTransactionHistory: true })).value;
        if (st && !st.err && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) return e.signature as string;
      }
    }
  }
  throw new Error("transaction not landed after 4 attempts");
};
const note = async (b: BondState, kind: string, text: string, sig?: string) => {
  const chain = await chainNow();
  b.ledger.push({ chain, bond: b.chainOrigin === undefined ? b.bondOrigin : bondTimeOf(b, chain), text, sig, kind });
  save(b);
};
const send = async (b: BondState, kind: string, text: string, ixs: TransactionInstruction[], signers: Keypair[]) => {
  const sig = await land(() => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), [registrar, ...signers], { commitment: "confirmed" }));
  await note(b, kind, text, sig);
  return sig;
};
const programEvents = async (sig: string) => {
  const tx = await connection.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 });
  return [...eventParser.parseLogs(tx!.meta!.logMessages!)];
};
// The program's message for a refused transaction, short enough for the screen.
const reason = (e: any) => {
  const text = [String(e?.message ?? e), ...(e?.logs ?? e?.transactionLogs ?? [])].join("\n");
  const found = /Error Message: ([^"\n]*)/.exec(text) ?? /custom program error: (0x[0-9a-f]+)/.exec(text) ?? /Message: ([^\n]*)/.exec(text);
  return (found?.[1] ?? text.split("\n")[0]).trim().slice(0, 300);
};

// ---------------------------------------------------------------- creating a bond

const validate = (p: Params) => {
  const errors: string[] = [];
  const int = (v: number, lo: number, hi: number, what: string) => { if (!Number.isInteger(v) || v < lo || v > hi) errors.push(`${what}: целое от ${lo} до ${hi}`); };
  if (!/^\d{4}-\d{2}-\d{2}$/.test(p.issue) || Number.isNaN(Date.parse(p.issue))) errors.push("дата выпуска: ГГГГ-ММ-ДД");
  int(p.years, 1, 10, "срок, лет");
  if (![1, 2, 4, 12].includes(p.periodsPerYear)) errors.push("купонов в год: 1, 2, 4 или 12");
  else if (p.years * p.periodsPerYear > 40) errors.push(`купонов за срок ${p.years * p.periodsPerYear}: программа принимает не больше 40`);
  int(p.face, 1, 1_000_000_000, "номинал");
  if (!(p.ratePct > 0 && p.ratePct <= 100) || Math.round(p.ratePct * 100) !== p.ratePct * 100) errors.push("ставка: от 0,01 до 100 %, шаг 0,01");
  int(p.redemptionPct, 50, 200, "цена погашения, % номинала");
  int(p.holders, 2, 20, "держателей");
  int(p.issueSize, p.holders, 100_000, "облигаций в выпуске (не меньше числа держателей)");
  int(p.maxTradesPerHolder, 0, 50, "сделок на держателя за срок");
  for (const [v, what] of [[p.propensityStart, "склонность к сделкам в начале"], [p.propensityEnd, "склонность к сделкам в конце"]] as const)
    if (!(v >= 0 && v <= 1)) errors.push(`${what}: от 0 до 1`);
  const maturityYear = Number(p.issue.slice(0, 4)) + p.years;
  if (Number(p.issue.slice(0, 4)) < 2024 || maturityYear > 2040) errors.push("годы выпуска и погашения: 2024-2040 (календарь праздников)");
  return errors;
};
// SOL the registrar spends on one bond, from the 23 Oct devnet run (0.0455 SOL for 3 holders and 5 events) and the
// 1000-holder run (0.0059 SOL per holder): accounts' deposits and fees.
const estimateSol = (p: Params) => 0.02 + 0.006 * p.holders + 0.004 * (p.years * p.periodsPerYear + 1) + 0.004 * p.holders * p.maxTradesPerHolder;

async function createBond(p: Params): Promise<Runtime> {
  const months = 12 / p.periodsPerYear;
  const maturity = `${Number(p.issue.slice(0, 4)) + p.years}${p.issue.slice(4)}`;
  const schedule = JSON.parse(execFileSync(SCHEDULE, [p.issue, maturity, String(months), path.join(ROOT, RULES.calendar), RULES.weekend.join(","), "0"], { encoding: "utf8" }));
  const id = crypto.randomBytes(6).toString("hex");
  const mintKp = Keypair.generate(), cashKp = Keypair.generate();
  const holderKps = Object.fromEntries(Array.from({ length: p.holders }, (_, i) => [`BH#${i + 1}`, Keypair.generate()]));
  const rngSeed = crypto.randomBytes(4).readUInt32LE();
  const b: BondState = {
    id, createdAt: new Date().toISOString(), params: p,
    title: `${p.face} · ${p.ratePct}% · ${p.periodsPerYear} куп./год · ${p.years} г. · ${p.holders} держ.`,
    status: "creating", bondMint: mintKp.publicKey.toBase58(), cashMint: cashKp.publicKey.toBase58(),
    bond: "", holders: Object.entries(holderKps).map(([name, kp]) => ({ name, pubkey: kp.publicKey.toBase58() })), placements: {},
    // Bond time at creation: three bond days before the issue date, 12:00 local (as in the devnet runs).
    bondOrigin: Date.parse(`${p.issue}T12:00:00+05:00`) / 1000 - 3 * DAY,
    issueTime: dayNumber(p.issue) * DAY, maturityTime: dayNumber(maturity) * DAY, schedule,
    recordTimes: schedule.record_days.map((d: number) => d * DAY + secondsOf(RULES.recordTimeLocal) - RULES.utcOffsetHours * 3_600),
    events: [], offers: [], trades: [], ledger: [], tradesUsed: {}, decidedDay: 0, pending: [], rng: rngSeed, bankTopUps: {},
    token: crypto.randomBytes(16).toString("hex"),
  };
  // The issue split among the holders at random weights, at least one bond each.
  const names = b.holders.map((h) => h.name);
  const weights = names.map(() => random(b)), total = weights.reduce((x, y) => x + y, 0);
  let left = p.issueSize - p.holders;
  names.forEach((h, i) => { const q = Math.floor((weights[i] / total) * (p.issueSize - p.holders)); b.placements[h] = 1 + q; left -= q; });
  for (let i = 0; left > 0; i++, left--) b.placements[names[i % names.length]] += 1;
  for (const n of names) b.tradesUsed[n] = 0;
  const periods = schedule.payment_days.length;
  const paymentTime = (k: number) => schedule.payment_days[k] * DAY + secondsOf(RULES.paymentOpensLocal) - RULES.utcOffsetHours * 3_600;
  for (let e = 0; e <= periods; e++) {
    const k = Math.min(e, periods - 1);
    b.events.push({ index: e, kind: e < periods ? "coupon" : "redemption", auto: true, recordTime: b.recordTimes[k], paymentTime: paymentTime(k), paid: {}, done: false });
  }
  b.bond = pda(Buffer.from("bond"), mintKp.publicKey.toBuffer()).toBase58();
  b.decidedDay = Math.floor(b.issueTime / DAY) - 1;
  fs.mkdirSync(dirOf(id), { recursive: true });
  const keys: Keys = { bondMint: [...mintKp.secretKey], cashMint: [...cashKp.secretKey], holders: Object.fromEntries(Object.entries(holderKps).map(([n, kp]) => [n, [...kp.secretKey]])) };
  fs.writeFileSync(path.join(dirOf(id), "keys.json"), JSON.stringify(keys), { mode: 0o600 });
  save(b);
  const rt: Runtime = { b, keys: { bondMint: mintKp, cashMint: cashKp, holders: holderKps }, busy: false, queue: [], viewAt: 0 };
  bonds.set(id, rt);
  rt.busy = true;
  issue(rt).catch(async (e) => { console.error(id, "issue", e); b.status = "error"; b.error = reason(e); await note(b, "error", `выпуск не создан: ${b.error}`); }).finally(() => { rt.busy = false; save(b); });
  return rt;
}

// The bond's own test cash token (an audit of this bond reads only its transactions), create_bond, then holders
// registered and the issue placed, three holders per transaction.
async function issue(rt: Runtime) {
  const { b, keys } = rt;
  const a = accountsOf(rt), p = b.params;
  const rent = await connection.getMinimumBalanceForRentExemption(MINT_SIZE);
  await send(b, "setup", "тестовый cash-токен выпуска (банк: mint и freeze authority)", [
    SystemProgram.createAccount({ fromPubkey: registrar.publicKey, newAccountPubkey: a.cashMint, space: MINT_SIZE, lamports: rent, programId: TOKEN_PROGRAM_ID }),
    createInitializeMint2Instruction(a.cashMint, CASH_DECIMALS, bank.publicKey, bank.publicKey, TOKEN_PROGRAM_ID),
  ], [keys.cashMint]);
  const terms = {
    faceUnits: new BN((BigInt(p.face) * UNIT).toString()), rateBps: Math.round(p.ratePct * 100),
    basis: { fixedPerPeriod: { periodsPerYear: p.periodsPerYear } }, redemptionPriceBps: p.redemptionPct * 100,
    issueDay: dayNumber(p.issue), maturityDay: Math.round(b.maturityTime / DAY), monthsPerPeriod: 12 / p.periodsPerYear,
    paymentDays: b.schedule.payment_days, recordDays: b.schedule.record_days,
    recordSecondsLocal: secondsOf(RULES.recordTimeLocal), paymentSecondsLocal: secondsOf(RULES.paymentOpensLocal),
    utcOffsetSeconds: RULES.utcOffsetHours * 3_600, weekendDays: RULES.weekend.reduce((m, d) => m | (1 << WEEKDAYS.indexOf(d)), 0),
    bondOrigin: new BN(b.bondOrigin), chainSecondsPerBondDay: new BN(1), maxSupply: new BN(p.issueSize),
  };
  const createSig = await land(() => program.methods.createBond(terms)
    .accountsPartial({
      registrar: registrar.publicKey, mint: a.mint, bond: a.bond, cashMint: a.cashMint,
      extraAccountMetaList: pda(Buffer.from("extra-account-metas"), a.mint.toBuffer()),
      tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
    })
    .preInstructions([computeLimit()]).signers([keys.bondMint]).rpc());
  b.chainOrigin = Number((await program.account.bond.fetch(a.bond) as any).chainOrigin);
  await note(b, "setup", `create_bond: ${p.face} номинал, ${p.ratePct}% годовых, ${p.periodsPerYear} купона в год, ${b.schedule.payment_dates[0]} - ${b.schedule.payment_dates[b.schedule.payment_dates.length - 1]}, выпуск до ${p.issueSize}`, createSig);
  for (const batch of chunks(b.holders.map((h) => h.name), 3)) {
    const ixs: TransactionInstruction[] = [computeLimit()];
    for (const h of batch) {
      ixs.push(await program.methods.registerHolder(a.owner(h)).accountsPartial({ registrar: registrar.publicKey, bond: a.bond }).instruction());
      ixs.push(
        createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.bondsAta(h), a.owner(h), a.mint, TOKEN_2022_PROGRAM_ID),
        createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.cashAta(h), a.owner(h), a.cashMint, TOKEN_PROGRAM_ID),
      );
      ixs.push(await program.methods.place(new BN(b.placements[h]))
        .accountsPartial({ registrar: registrar.publicKey, bond: a.bond, mint: a.mint, holder: a.record(h), holderBonds: a.bondsAta(h), tokenProgram: TOKEN_2022_PROGRAM_ID })
        .instruction());
    }
    await send(b, "setup", `register_holder, place: ${batch.map((h) => `${h} ${b.placements[h]}`).join(", ")}`, ixs, []);
  }
  b.status = "live";
  save(b);
}

// ---------------------------------------------------------------- corporate actions

// One step of a coupon or the redemption. Steps in order: open (from the record moment), fund (the issuer's bank puts
// the budget into the event's vault), confirm, pay (from the payment time; the redemption burns and pays).
async function eventStep(rt: Runtime, e: number, step: string) {
  const { b } = rt;
  const a = accountsOf(rt), ev = b.events[e], label = ev.kind === "coupon" ? `купон ${e + 1}` : "погашение";
  const now = bondTimeOf(b, await chainNow());
  const event = a.event(e), vault = a.vault(event);
  if (step === "open") {
    if (ev.open) throw new Error(`${label}: уже открыт`);
    if (now < ev.recordTime) throw new Error(`${label}: момент отсечки ${localIso(ev.recordTime)} ещё не наступил`);
    const sig = await land(() => program.methods.openEvent(e).accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).rpc());
    const opened: any = await program.account.event.fetch(event);
    ev.open = sig; ev.budget = opened.budget.toString(); ev.supplyAtRecord = Number(opened.supplyAtRecord);
    await note(b, "event", `open_event, ${label}: на отсечку ${ev.supplyAtRecord} облигаций, бюджет ${fmtCash(ev.budget)}`, sig);
  } else if (step === "fund") {
    if (!ev.open) throw new Error(`${label}: сначала открыть`);
    if (ev.fund) throw new Error(`${label}: бюджет уже внесён`);
    const need = BigInt(ev.budget!) - (await tokenBalance(vault));
    ev.fund = await send(b, "event", `эмитент вносит бюджет, ${label}: ${fmtCash(need.toString())}`, [createMintToInstruction(a.cashMint, vault, bank.publicKey, need, [], TOKEN_PROGRAM_ID)], [bank]);
  } else if (step === "confirm") {
    if (!ev.fund) throw new Error(`${label}: сначала внести бюджет`);
    if (ev.confirm) throw new Error(`${label}: уже подтверждён`);
    const sig = await land(() => program.methods.confirmFunding().accountsPartial({ event, vault }).rpc());
    ev.confirm = sig;
    await note(b, "event", `confirm_funding, ${label}: бюджет внесён полностью, выплаты открыты`, sig);
  } else if (step === "pay") {
    if (!ev.confirm) throw new Error(`${label}: сначала подтвердить бюджет`);
    if (now < ev.paymentTime) throw new Error(`${label}: выплаты с ${localIso(ev.paymentTime)}`);
    const names = b.holders.map((h) => h.name);
    const recs: any[] = await program.account.holderRecord.fetchMultiple(names.map(a.record));
    if (ev.kind === "coupon") {
      const payable = names.filter((h, i) => recs[i] && positionOf(recs[i], e) > 0 && !ev.paid[h]);
      for (const batch of chunks(payable, 6)) {
        const ixs: TransactionInstruction[] = [computeLimit()];
        for (const h of batch)
          ixs.push(await program.methods.payCoupon().accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, holder: a.record(h), owner: a.owner(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction());
        const sig = await land(() => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), [registrar], { commitment: "confirmed" }));
        const paid = (await programEvents(sig)).filter((x) => x.name === "CouponPaid");
        for (const x of paid) ev.paid[nameOf(rt, x.data.owner.toBase58())] = { amount: x.data.amount.toString(), sig };
        await note(b, "pay", `pay_coupon ${e + 1}: ${paid.map((x) => `${nameOf(rt, x.data.owner.toBase58())} ${Number(x.data.position)} обл. → ${fmtCash(x.data.amount.toString())}`).join(", ")}`, sig);
      }
    } else {
      const redeemable = names.filter((h, i) => recs[i] && Number(recs[i].position.quantity) - Number(recs[i].locked) - Number(recs[i].escrowed) > 0);
      for (const batch of chunks(redeemable, 4)) {
        const ixs: TransactionInstruction[] = [computeLimit()];
        for (const h of batch)
          ixs.push(await program.methods.redeemHolder().accountsPartial({ payer: registrar.publicKey, bond: a.bond, mint: a.mint, event, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction());
        const sig = await land(() => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), [registrar], { commitment: "confirmed" }));
        const done = (await programEvents(sig)).filter((x) => x.name === "BondsRedeemed");
        for (const x of done) {
          const h = nameOf(rt, x.data.owner.toBase58()), prev = ev.paid[h];
          ev.paid[h] = { amount: (BigInt(prev?.amount ?? "0") + BigInt(x.data.principal.toString())).toString(), sig };
        }
        await note(b, "pay", `redeem_holder: ${done.map((x) => `${nameOf(rt, x.data.owner.toBase58())} сожжено ${Number(x.data.burned)} → ${fmtCash(x.data.principal.toString())}`).join(", ")}`, sig);
      }
    }
    ev.done = true;
    await note(b, "event", ev.kind === "coupon" ? `купон ${e + 1} выплачен` : "погашение завершено");
  } else throw new Error(`неизвестный шаг ${step}`);
  ev.error = undefined;
  save(b);
}
const nextEventStep = (ev: EventState) => (!ev.open ? "open" : !ev.fund ? "fund" : !ev.confirm ? "confirm" : !ev.done ? "pay" : null);

// An offer the operator adds (buyback: first come; tender: pro rata), funded with price x maximum.
async function addOffer(rt: Runtime, o: { kind: "buyback" | "tender"; price: number; maxQuantity: number; open: string; close: string }) {
  const { b } = rt;
  const a = accountsOf(rt);
  const open = dayNumber(o.open) * DAY + secondsOf("09:00") - RULES.utcOffsetHours * 3_600;
  const close = dayNumber(o.close) * DAY + secondsOf("17:00") - RULES.utcOffsetHours * 3_600;
  if (!(o.kind === "buyback" || o.kind === "tender")) throw new Error("вид оферты: buyback или tender");
  if (!(Number.isInteger(o.maxQuantity) && o.maxQuantity > 0 && o.maxQuantity <= b.params.issueSize)) throw new Error(`объём: целое от 1 до ${b.params.issueSize} (объём выпуска)`);
  if (!(o.price <= 1e9)) throw new Error("цена за облигацию: не больше 1 000 000 000");
  if (!(o.price > 0) || Math.round(o.price * 1e6) !== o.price * 1e6) throw new Error("цена за облигацию: больше 0, до 6 знаков");
  const finalRecord = b.recordTimes[b.recordTimes.length - 1], now = bondTimeOf(b, await chainNow());
  if (!(open < close)) throw new Error("окно: дата открытия раньше даты закрытия");
  if (close <= now) throw new Error("окно уже закрылось");
  if (close >= finalRecord) throw new Error(`окно должно закрыться до последней отсечки ${localIso(finalRecord)}`);
  // A tender settles right after its window, before the next record moment (the audit replays it in that phase).
  if (o.kind === "tender" && nextRecordAfter(b, close) - close < MARGIN_DAYS * DAY) throw new Error(`окно tender должно закрыться не позже чем за ${MARGIN_DAYS} дней до отсечки`);
  const id = b.offers.length + 1;
  const priceUnits = BigInt(Math.round(o.price * 1e6)).toString();
  const offer = a.offer(id), vault = a.vault(offer);
  const sig = await land(() => program.methods.createOffer(id, o.kind === "tender", new BN(priceUnits), new BN(o.maxQuantity), new BN(open), new BN(close))
    .accountsPartial({ registrar: registrar.publicKey, bond: a.bond, offer, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).rpc());
  // Who answers: each holder with probability 1/2, once, at a random moment in the window's first nine tenths.
  const from = Math.max(open, now);
  const plans = b.holders.map((h) => h.name).filter(() => random(b) < 0.5)
    .map((holder) => ({ holder, at: from + Math.floor(random(b) * 0.9 * (close - from)), share: random(b) }));
  b.offers.push({ id, kind: o.kind, priceUnits, maxQuantity: o.maxQuantity, open, close, auto: true, create: sig, settled: false, done: false, plans, actions: [], settlements: [] });
  await note(b, "offer", `create_offer ${id} (${o.kind === "tender" ? "tender, pro rata" : "buyback, first come"}): ${fmtCash(priceUnits)} за облигацию, до ${o.maxQuantity}, окно ${localIso(open)} - ${localIso(close)}`, sig);
}
async function offerStep(rt: Runtime, id: number, step: string) {
  const { b } = rt;
  const a = accountsOf(rt), o = b.offers[id - 1];
  if (!o) throw new Error(`оферты ${id} нет`);
  const offer = a.offer(id), vault = a.vault(offer);
  if (step === "fund") {
    if (o.fund) throw new Error("бюджет уже внесён");
    const budget = BigInt(o.priceUnits) * BigInt(o.maxQuantity);
    o.fund = await send(b, "offer", `эмитент вносит бюджет оферты ${id}: ${fmtCash(budget.toString())}`, [createMintToInstruction(a.cashMint, vault, bank.publicKey, budget, [], TOKEN_PROGRAM_ID)], [bank]);
  } else if (step === "confirm") {
    if (!o.fund) throw new Error("сначала внести бюджет");
    o.confirm = await land(() => program.methods.confirmOfferFunding().accountsPartial({ offer, vault }).rpc());
    await note(b, "offer", `confirm_offer_funding ${id}: оферта открыта для держателей`, o.confirm);
    if (o.kind === "buyback") o.done = true;
  } else if (step === "settle") {
    if (o.kind !== "tender") throw new Error("завершение только у tender");
    if (bondTimeOf(b, await chainNow()) < o.close) throw new Error(`окно открыто до ${localIso(o.close)}`);
    const tendered = new Map<string, number>();
    for (const act of o.actions) if (act.tender) tendered.set(act.holder, (tendered.get(act.holder) ?? 0) + act.tender);
    for (const h of tendered.keys()) {
      const sig = await land(() => program.methods.resolveTender().accountsPartial({ bond: a.bond, offer, holder: a.record(h), tender: a.tender(id, h) }).rpc());
      await note(b, "offer", `resolve_tender ${id}: ${h}`, sig);
    }
    for (const [h, q] of tendered) {
      const sig = await land(() => program.methods.settleTender().accountsPartial({ retirement: retirement(rt, id, h, registrar.publicKey), offer, tender: a.tender(id, h) }).rpc());
      const sold = (await programEvents(sig)).find((x) => x.name === "BondsBoughtBack");
      o.settlements.push({ holder: h, tendered: q, burned: sold ? Number(sold.data.burned) : 0, paid: sold ? sold.data.paid.toString() : "0", sig });
      await note(b, "offer", `settle_tender ${id}: ${h} заявил ${q}, выкуплено и сожжено ${sold ? Number(sold.data.burned) : 0} → ${fmtCash(sold ? sold.data.paid.toString() : "0")}`, sig);
    }
    o.settled = true; o.done = true;
  } else throw new Error(`неизвестный шаг ${step}`);
  o.error = undefined;
  save(b);
}
const retirement = (rt: Runtime, id: number, h: string, payer: PublicKey) => {
  const a = accountsOf(rt);
  return {
    payer, bond: a.bond, mint: a.mint, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault: a.vault(a.offer(id)),
    cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID,
  };
};
const nextOfferStep = (o: OfferState, now: number) =>
  !o.fund ? "fund" : !o.confirm ? "confirm" : o.kind === "tender" && !o.settled && now >= o.close ? "settle" : null;

// A holder answers an offer: sells into a buyback (up to what is left of it) or tenders, a random share of its free bonds.
async function answerOffer(rt: Runtime, o: OfferState, plan: OfferState["plans"][number]) {
  const { b, keys } = rt;
  const a = accountsOf(rt);
  const rec: any = await program.account.holderRecord.fetch(a.record(plan.holder));
  const free = Number(rec.position.quantity) - Number(rec.locked) - Number(rec.escrowed);
  const sold = o.actions.reduce((s, x) => s + (x.sell ?? 0), 0);
  let q = Math.max(1, Math.round(plan.share * free));
  if (o.kind === "buyback") q = Math.min(q, o.maxQuantity - sold);
  if (free <= 0 || q <= 0) return;
  const owner = a.owner(plan.holder), offer = a.offer(o.id);
  let sig: string;
  if (o.kind === "buyback") {
    sig = await land(() => program.methods.sellToOffer(new BN(q)).accountsPartial({ retirement: retirement(rt, o.id, plan.holder, owner), seller: owner, offer }).signers([keys.holders[plan.holder]]).rpc());
  } else {
    const deposit = await connection.getMinimumBalanceForRentExemption(TENDER_ACCOUNT_SIZE), have = await connection.getBalance(owner);
    const ixs: TransactionInstruction[] = have < deposit ? [SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: owner, lamports: deposit - have })] : [];
    ixs.push(await program.methods.changeTender(new BN(q), false).accountsPartial({ owner, bond: a.bond, offer, holder: a.record(plan.holder), tender: a.tender(o.id, plan.holder) }).instruction());
    sig = await land(() => sendAndConfirmTransaction(connection, new Transaction().add(...ixs), [registrar, keys.holders[plan.holder]], { commitment: "confirmed" }));
  }
  const phase = phaseAt(b, bondTimeOf(b, (await connection.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }))!.blockTime!));
  const paid = (await programEvents(sig)).find((x) => x.name === "BondsBoughtBack");
  o.actions.push({ phase, holder: plan.holder, ...(o.kind === "buyback" ? { sell: q } : { tender: q }), sig, ...(paid ? { paid: paid.data.paid.toString() } : {}) });
  await note(b, "offer", o.kind === "buyback"
    ? `sell_to_offer ${o.id}: ${plan.holder} продаёт ${q}, сожжено, получено ${fmtCash(paid?.data.paid.toString() ?? "0")}`
    : `change_tender ${o.id}: ${plan.holder} заявляет ${q} (облигации заблокированы, купон по ним сохраняется)`, sig);
}

// ---------------------------------------------------------------- holders' trades (Solana DvP)

async function trade(rt: Runtime, from: string, to: string) {
  const { b, keys } = rt;
  const a = accountsOf(rt);
  const rec: any = await program.account.holderRecord.fetch(a.record(from));
  const free = Number(rec.position.quantity) - Number(rec.locked) - Number(rec.escrowed);
  if (free <= 0) return;
  const amount = 1 + Math.floor(random(b) * free);
  // Price per bond within 2% of face, in cash units.
  const price = (BigInt(b.params.face) * UNIT * BigInt(Math.round((0.98 + 0.04 * random(b)) * 10_000))) / 10_000n;
  const cash = price * BigInt(amount);
  const seller = a.owner(from), buyer = a.owner(to);
  const nonce = randomNonce(), expiry = BigInt((await chainNow()) + DAY);
  const { ix: create, swap } = createDvpInstruction(registrar.publicKey, seller, buyer, a.mint, a.cashMint, TOKEN_PROGRAM_ID, BigInt(amount), cash, nonce, expiry);
  const admit = await admitTradeInstruction(program, registrar.publicKey, a.bond, a.mint, swap, seller, buyer);
  const t: TradeState = { phase: -2, from, to, amount, cash: cash.toString(), settled: false, sigs: [], at: 0 };
  t.sigs.push(await send(b, "trade", `DvP: ${from} продаёт ${to} ${amount} обл. за ${fmtCash(cash.toString())} (create + admit_trade)`, [create, admit], []));
  t.sigs.push(await send(b, "trade", `DvP: ${from} кладёт ${amount} обл. в escrow (позиция остаётся у продавца)`, [await fundBondLegInstruction(connection, a.mint, seller, swap, BigInt(amount))], [keys.holders[from]]));
  const have = await tokenBalance(a.cashAta(to));
  if (have < cash) {
    t.sigs.push(await send(b, "trade", `банк даёт ${to} деньги на покупку (имитация фиата): ${fmtCash((cash - have).toString())}`, [createMintToInstruction(a.cashMint, a.cashAta(to), bank.publicKey, cash - have, [], TOKEN_PROGRAM_ID)], [bank]));
    b.bankTopUps[to] = (BigInt(b.bankTopUps[to] ?? "0") + cash - have).toString();
  }
  t.sigs.push(await send(b, "trade", `DvP: ${to} кладёт оплату в escrow`, [fundCashLegInstruction(a.cashMint, TOKEN_PROGRAM_ID, CASH_DECIMALS, buyer, swap, cash)], [keys.holders[to]]));
  const dvp = { swap, nonce, seller, buyer, quantity: BigInt(amount), cash };
  try {
    t.sigs.push(await send(b, "trade", `DvP settle: ${amount} обл. → ${to}, ${fmtCash(cash.toString())} → ${from}, одна транзакция`, [await settleDvpInstruction(connection, program.programId, registrar.publicKey, dvp, a.mint, a.cashMint, TOKEN_PROGRAM_ID)], []));
    t.settled = true;
  } catch (e) {
    // The trade could not settle (the final record moment came first): both legs go back to their owners.
    await note(b, "error", `DvP settle отклонён: ${reason(e)}`);
    t.sigs.push(await send(b, "trade", `DvP cancel: облигации → ${from}, деньги → ${to}`, [await cancelDvpInstruction(connection, program.programId, registrar.publicKey, dvp, a.mint, a.cashMint, TOKEN_PROGRAM_ID)], []));
  }
  // The phase is the one the program saw: the seller's record keeps positions for every record moment passed.
  t.phase = Number((await program.account.holderRecord.fetch(a.record(from)) as any).position.liveFrom) - 1;
  t.at = bondTimeOf(b, await chainNow());
  b.trades.push(t);
  if (t.settled) b.tradesUsed[from] += 1;
  save(b);
}

// ---------------------------------------------------------------- the worker: one action at a time per bond

const nextRecordAfter = (b: BondState, t: number) => b.recordTimes.find((r) => r > t) ?? Infinity;
// Start one action; the next tick starts nothing for this bond until it ends.
function start(rt: Runtime, what: string, run: () => Promise<unknown>, onError?: (msg: string) => void) {
  rt.busy = true;
  run()
    .catch(async (e) => { console.error(rt.b.id, what, e); const msg = reason(e); onError?.(msg); await note(rt.b, "error", `${what}: ${msg}`).catch(() => {}); })
    .finally(() => { rt.busy = false; rt.view = undefined; save(rt.b); });
}
function tick(rt: Runtime, chain: number) {
  const { b } = rt;
  if (rt.busy) return;
  if (rt.queue.length) {
    const job = rt.queue.shift()!;
    rt.busy = true;
    job.run().then(job.done, job.fail).finally(() => { rt.busy = false; rt.view = undefined; save(b); });
    return;
  }
  if (b.status !== "live" || b.chainOrigin === undefined) return;
  const now = bondTimeOf(b, chain);
  const finalRecord = b.recordTimes[b.recordTimes.length - 1];
  // 1. corporate actions whose time has come, in order
  for (const ev of b.events) {
    const step = nextEventStep(ev);
    if (!ev.auto || !step || now < ev.recordTime || (step === "pay" && now < ev.paymentTime)) continue;
    if (ev.error && chain % 10) return; // a failed step is retried every 10 seconds
    return start(rt, `${ev.kind === "coupon" ? `купон ${ev.index + 1}` : "погашение"}, шаг ${step}`, () => eventStep(rt, ev.index, step), (m) => { ev.error = m; });
  }
  // 2. offers: funding, then tender settlement after the window
  for (const o of b.offers) {
    const step = nextOfferStep(o, now);
    if (!o.auto || !step) continue;
    if (o.error && chain % 10) return;
    return start(rt, `оферта ${o.id}, шаг ${step}`, () => offerStep(rt, o.id, step), (m) => { o.error = m; });
  }
  // 3. holders answering open offers
  for (const o of b.offers) {
    if (!o.confirm || now >= o.close) continue;
    const i = o.plans.findIndex((p) => p.at <= now);
    if (i < 0) continue;
    const [plan] = o.plans.splice(i, 1);
    return start(rt, `ответ ${plan.holder} на оферту ${o.id}`, () => answerOffer(rt, o, plan));
  }
  // 4. holders' trades: decide day by day, then trade one at a time, never close to a record moment
  const today = Math.floor(now / DAY), p = b.params;
  const lifeStart = b.issueTime, life = b.maturityTime - b.issueTime;
  for (let d = b.decidedDay + 1; d <= today; d++) {
    b.decidedDay = d;
    if (d * DAY < b.issueTime || d * DAY >= finalRecord - MARGIN_DAYS * DAY) continue;
    const share = Math.min(1, Math.max(0, (d * DAY - lifeStart) / life));
    const chance = p.propensityStart + (p.propensityEnd - p.propensityStart) * share;
    for (const h of b.holders.map((x) => x.name)) {
      const queued = b.pending.filter((x) => x.from === h).length;
      if (b.tradesUsed[h] + queued >= p.maxTradesPerHolder || random(b) >= chance) continue;
      const others = b.holders.map((x) => x.name).filter((x) => x !== h);
      b.pending.push({ from: h, to: others[Math.floor(random(b) * others.length)] });
    }
  }
  if (now >= finalRecord - MARGIN_DAYS * DAY) b.pending = [];
  save(b);
  if (b.pending.length && nextRecordAfter(b, now) - now > MARGIN_DAYS * DAY) {
    const t = b.pending.shift()!;
    return start(rt, `сделка ${t.from} → ${t.to}`, () => trade(rt, t.from, t.to));
  }
  // 5. the end: every event paid, every offer done -> audit
  if (b.events.every((e) => e.done) && b.offers.every((o) => o.done)) {
    b.status = "done";
    start(rt, "конец", async () => { await note(b, "event", "все корпоративные действия выполнены; ончейн аудит"); runAudit(rt); });
  }
}

// ---------------------------------------------------------------- audit

const fmtCash = (units: string) => {
  const v = BigInt(units), whole = v / UNIT, frac = v % UNIT;
  return `${whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ")}${frac ? "," + frac.toString().padStart(CASH_DECIMALS, "0").replace(/0+$/, "") : ""}`;
};
// The bond as the audit reads it: terms, public keys, and what the holders did, phase by phase.
const publicScenario = (b: BondState) => ({
  name: `${b.title} (operator screen, bond ${b.id})`,
  terms: {
    issue: b.params.issue, maturity: new Date(b.maturityTime * 1000).toISOString().slice(0, 10), monthsPerPeriod: 12 / b.params.periodsPerYear,
    periodsPerYear: b.params.periodsPerYear, faceUnits: (BigInt(b.params.face) * UNIT).toString(), cashDecimals: CASH_DECIMALS,
    rateBps: Math.round(b.params.ratePct * 100), redemptionPriceBps: b.params.redemptionPct * 100,
    recordTimeLocal: RULES.recordTimeLocal, paymentDeadlineLocal: RULES.paymentDeadlineLocal, utcOffsetHours: RULES.utcOffsetHours,
    paymentOpensLocal: RULES.paymentOpensLocal, recordBusinessDaysBeforePayment: RULES.recordBusinessDaysBeforePayment, maxSupply: b.params.issueSize,
  },
  calendar: RULES.calendar, weekend: RULES.weekend,
  bondTimeAtCreation: new Date((b.bondOrigin + RULES.utcOffsetHours * 3_600) * 1000).toISOString().slice(0, 19) + "+05:00",
  chainSecondsPerBondDay: 1,
  keys: { bondMint: b.bondMint, cashMint: b.cashMint, cashAuthority: bank.publicKey.toBase58() },
  wallets: Object.fromEntries(b.holders.map((h) => [h.name, h.pubkey])),
  placements: b.placements,
  trades: b.trades.filter((t) => t.settled).map((t) => ({ afterRecordMoment: t.phase, from: t.from, to: t.to, amount: t.amount, cash: t.cash })),
  offers: b.offers.filter((o) => o.confirm).map((o) => ({
    id: o.id, kind: o.kind, priceUnits: o.priceUnits, maxQuantity: o.maxQuantity,
    open: { recordMoment: 0, plusDays: (o.open - b.recordTimes[0]) / DAY }, close: { recordMoment: 0, plusDays: (o.close - b.recordTimes[0]) / DAY },
    actions: o.actions.map((x) => ({ afterRecordMoment: x.phase, holder: x.holder, ...(x.sell ? { sell: x.sell } : { tender: x.tender }) })),
  })),
  handover: null,
});
// What each payment the service sent was for, by signature: a coupon's index, "redemption" or "offer N" (labels only;
// the audit measures the amounts itself).
const paymentsOf = (b: BondState) => Object.fromEntries([
  ...b.events.flatMap((e) => Object.values(e.paid).map((p) => [p.sig, e.kind === "coupon" ? e.index : "redemption"])),
  ...b.offers.flatMap((o) => [...o.actions.filter((x) => x.sell).map((x) => x.sig), ...o.settlements.map((x) => x.sig)].map((sig) => [sig, `offer ${o.id}`])),
]);
function runAudit(rt: Runtime) {
  const { b } = rt;
  if (b.auditRunning) return;
  b.auditRunning = true;
  const dir = dirOf(b.id), scn = path.join(dir, "scenario.public.json"), pays = path.join(dir, "payments.json");
  fs.writeFileSync(scn, JSON.stringify(publicScenario(b), null, 1));
  fs.writeFileSync(pays, JSON.stringify(paymentsOf(b)));
  execFile(PYTHON, [path.join(ROOT, "tools/audit/audit_json.py"), scn, RPC, pays], { cwd: ROOT, maxBuffer: 64 << 20 }, (err, stdout, stderr) => {
    b.auditRunning = false;
    if (err && !stdout) { b.auditError = (stderr || String(err)).slice(-2000); save(b); return; }
    try { b.audit = { ...JSON.parse(stdout), at: new Date().toISOString() }; b.auditError = undefined; } catch { b.auditError = (stderr || stdout).slice(-2000); }
    save(b);
  });
}

// ---------------------------------------------------------------- view for the screen

async function view(rt: Runtime) {
  const { b } = rt;
  if (rt.view && Date.now() - rt.viewAt < 1_500) return rt.view;
  const chain = await chainNow();
  const out: any = {
    id: b.id, title: b.title, status: b.status, error: b.error, params: b.params, bond: b.bond, bondMint: b.bondMint, cashMint: b.cashMint,
    registrar: registrar.publicKey.toBase58(), chainNow: chain, bondNow: b.chainOrigin === undefined ? null : bondTimeOf(b, chain),
    issueTime: b.issueTime, maturityTime: b.maturityTime, recordTimes: b.recordTimes, schedule: b.schedule, utcOffsetHours: RULES.utcOffsetHours,
    events: b.events, offers: b.offers.map(({ plans, ...o }) => ({ ...o, waiting: plans.length })), trades: b.trades, pending: b.pending.length,
    tradesUsed: b.tradesUsed, placements: b.placements, ledger: b.ledger, audit: b.audit, auditError: b.auditError, auditRunning: !!b.auditRunning,
    bankTopUps: b.bankTopUps, holders: [],
  };
  delete out.token;
  if (b.chainOrigin !== undefined) {
    const a = accountsOf(rt), names = b.holders.map((h) => h.name);
    const recs: any[] = await program.account.holderRecord.fetchMultiple(names.map(a.record));
    const passed = b.recordTimes.filter((t) => t <= out.bondNow).length;
    out.holders = b.holders.map((h, i) => {
      const r = recs[i];
      return {
        name: h.name, pubkey: h.pubkey, placed: b.placements[h.name],
        quantity: r ? Number(r.position.quantity) : 0, escrowed: r ? Number(r.escrowed) : 0, locked: r ? Number(r.locked) : 0,
        // position at every record moment passed: kept by the program (PositionHistory), "saved" if a transaction wrote it
        atRecord: Array.from({ length: passed }, (_, e) => (r ? positionOf(r, e) : 0)),
        saved: Array.from({ length: passed }, (_, e) => !!r && e < Number(r.position.liveFrom)),
        received: Object.fromEntries(b.events.filter((e) => e.paid[h.name]).map((e) => [e.index, e.paid[h.name]])),
      };
    });
    out.vaults = await Promise.all(b.events.map(async (e) => (e.open ? (await tokenBalance(a.vault(a.event(e.index)))).toString() : null)));
  }
  rt.view = out;
  rt.viewAt = Date.now();
  return out;
}

// ---------------------------------------------------------------- HTTP

const json = (res: http.ServerResponse, code: number, body: unknown) => {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(body));
};
const body = (req: http.IncomingMessage) => new Promise<any>((resolve, reject) => {
  let s = "";
  req.on("data", (c) => { s += c; if (s.length > 1e5) reject(new Error("тело запроса слишком большое")); });
  req.on("end", () => { try { resolve(s ? JSON.parse(s) : {}); } catch { reject(new Error("тело запроса: не JSON")); } });
});
// A button press runs in the bond's worker, after what it is doing now, and answers when done.
const enqueue = (rt: Runtime, run: () => Promise<unknown>) => new Promise((done, fail) => rt.queue.push({ run, done, fail }));
const TYPES: Record<string, string> = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2" };
const STATIC = path.join(ROOT, "docs/demo");

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url!, "http://x");
  const parts = url.pathname.split("/").filter(Boolean);
  try {
    if (parts[0] !== "api") {
      if (req.method !== "GET") return json(res, 405, { error: "GET only" });
      const rel = url.pathname === "/operator-screen" || url.pathname === "/operator-screen/" ? "operator-screen.html" : decodeURIComponent(url.pathname);
      const file = path.join(STATIC, path.normalize(rel).replace(/^(\.\.[/\\])+/, ""));
      if (!file.startsWith(STATIC) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return json(res, 404, { error: "not found" });
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] ?? "application/octet-stream", "Cache-Control": "no-cache" });
      return void fs.createReadStream(file).pipe(res);
    }
    if (req.method === "GET" && parts[1] === "state") {
      const lamports = await connection.getBalance(registrar.publicKey);
      return json(res, 200, {
        registrar: registrar.publicKey.toBase58(), sol: lamports / LAMPORTS_PER_SOL, faucet: FAUCET, rpc: RPC, programId: program.programId.toBase58(),
        bonds: [...bonds.values()].map(({ b }) => ({ id: b.id, title: b.title, status: b.status, createdAt: b.createdAt })).reverse(),
      });
    }
    if (req.method === "POST" && parts[1] === "estimate") {
      const p = await body(req);
      return json(res, 200, { errors: validate(p), sol: estimateSol(p) });
    }
    if (req.method === "POST" && parts[1] === "bonds" && parts.length === 2) {
      const p: Params = await body(req);
      const errors = validate(p);
      if (errors.length) return json(res, 400, { error: errors.join("; ") });
      const need = estimateSol(p), have = (await connection.getBalance(registrar.publicKey)) / LAMPORTS_PER_SOL;
      if (have < need) return json(res, 400, { error: `на кошельке registrar ${have.toFixed(4)} SOL, на выпуск нужно около ${need.toFixed(3)} SOL: пополните его через ${FAUCET}` });
      const rt = await createBond(p);
      return json(res, 200, { id: rt.b.id, token: rt.b.token });
    }
    const rt = parts[1] === "bonds" ? bonds.get(parts[2]) : undefined;
    if (!rt) return json(res, 404, { error: "выпуска нет" });
    if (req.method === "GET" && parts.length === 3) return json(res, 200, await view(rt));
    if (req.method === "GET" && parts[3] === "scenario.public.json") return json(res, 200, publicScenario(rt.b));
    if (req.method === "GET" && parts[3] === "payments.json") return json(res, 200, paymentsOf(rt.b));
    if (req.method !== "POST") return json(res, 405, { error: "method" });
    const given = String(req.headers["x-operator-token"] ?? "");
    if (given.length !== rt.b.token.length || !crypto.timingSafeEqual(Buffer.from(given), Buffer.from(rt.b.token)))
      return json(res, 403, { error: "управляет выпуском тот, кто его создал (токен хранится в его браузере); остальные смотрят" });
    const input = await body(req);
    rt.view = undefined;
    if (parts[3] === "events" && parts[5] === "auto") {
      rt.b.events[Number(parts[4])].auto = !!input.auto; save(rt.b);
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "events" && parts[5] === "step") {
      const e = Number(parts[4]);
      if (rt.b.events[e]?.auto) return json(res, 400, { error: "включена авто-обработка: снимите галку, чтобы нажимать шаги" });
      await enqueue(rt, () => eventStep(rt, e, input.step));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts.length === 4) {
      if (rt.b.status !== "live") return json(res, 400, { error: "выпуск не в обращении" });
      if (rt.b.offers.length >= 10) return json(res, 400, { error: "не больше 10 оферт на выпуск" });
      await enqueue(rt, () => addOffer(rt, input));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts[5] === "auto") {
      rt.b.offers[Number(parts[4]) - 1].auto = !!input.auto; save(rt.b);
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts[5] === "step") {
      const id = Number(parts[4]);
      if (rt.b.offers[id - 1]?.auto) return json(res, 400, { error: "включена авто-обработка: снимите галку, чтобы нажимать шаги" });
      await enqueue(rt, () => offerStep(rt, id, input.step));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "audit") {
      runAudit(rt);
      return json(res, 200, { ok: true });
    }
    return json(res, 404, { error: "нет такого действия" });
  } catch (e) {
    return json(res, 400, { error: reason(e) });
  }
});

// Bonds survive a restart: state and keys are files.
fs.mkdirSync(DATA, { recursive: true });
for (const id of fs.readdirSync(DATA)) {
  const st = path.join(dirOf(id), "state.json");
  if (!fs.existsSync(st)) continue;
  const b: BondState = JSON.parse(fs.readFileSync(st, "utf8"));
  const k: Keys = JSON.parse(fs.readFileSync(path.join(dirOf(id), "keys.json"), "utf8"));
  const kp = (s: number[]) => Keypair.fromSecretKey(Uint8Array.from(s));
  b.auditRunning = false;
  if (b.status === "creating") { b.status = "error"; b.error = "сервис перезапущен во время выпуска"; }
  bonds.set(id, { b, keys: { bondMint: kp(k.bondMint), cashMint: kp(k.cashMint), holders: Object.fromEntries(Object.entries(k.holders).map(([n, s]) => [n, kp(s)])) }, busy: false, queue: [], viewAt: 0 });
}
let ticking = false;
setInterval(async () => {
  if (ticking) return;
  ticking = true;
  try {
    const chain = await chainNow();
    for (const rt of bonds.values()) {
      try { tick(rt, chain); } catch (e) { console.error(rt.b.id, e); }
    }
  } catch (e) {
    console.error("tick", e);
  } finally {
    ticking = false;
  }
}, 1_000);
server.listen(PORT, "127.0.0.1", () => console.log(`operator service on 127.0.0.1:${PORT}, registrar ${registrar.publicKey.toBase58()}, ${bonds.size} bonds`));
