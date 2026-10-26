// Operator service behind the operator screen (docs/demo/operator-screen.html, blum.caiborg.ai/operator-screen).
// Plan: colosseum/plan-refined.md. Anyone creates a bond on devnet with their own terms; then, with the roles the Solana
// DvP documentation lays out (solana.com/docs/defi/dvp-program):
//   - the registrar (transfer agent) registers the generated holders BH#1..n, places the issue and services every
//     corporate action known from the terms (each coupon, the redemption): by itself at its moment when its "auto"
//     box is ticked, or step by step when the operator presses the step; it takes no part in trades;
//   - the holders trade among themselves through Solana DvP: the seller records the trade, admits its escrow and
//     delivers its bonds in one transaction, the buyer pays in its own, the venue (settlement authority) settles;
//     every record moment has trades a day before it, at it and a day after it, two at each point; besides those,
//     each holder trades at random (at most T trades, propensities at the start and at the end of the bond's life);
//   - holders answer the buybacks and tenders the operator adds: some take part, some do not;
//   - the ledger-only audit (tools/audit/audit_json.py) runs on the bond's public data after the redemption.
// Every request to the RPC goes through one budget kept inside the public devnet's limits (solana.com/docs/references/
// clusters); transactions are confirmed by polling signature statuses, journaled before they are sent and resent as
// the same bytes while their blockhash lives; the screen is served from a snapshot and never reads the network.
// 1 bond day = 1 chain second. Market rules are the sample's (KASE, docs/bond-mechanics.md).
// Usage (from ops/): npx tsx src/operator.ts
//   env OPERATOR_RPC (cluster RPC), OPERATOR_PORT (HTTP port, localhost), OPERATOR_DATA (state directory),
//   OPERATOR_REGISTRAR (keypair: registrar; also the service's wallet, topped up from the faucet), OPERATOR_VENUE
//   (keypair: the venue, settlement authority of every trade), OPERATOR_BANK (keypair: the test cash token's authority),
//   OPERATOR_SCHEDULE (bond_math's `schedule` example binary), OPERATOR_PYTHON (python with QuantLib and FinancePy).
// HTTP: /api/... (below); any other GET serves docs/demo, so the page and the API share one origin. Anyone may watch any
// bond; only its creator acts on it: creating a bond returns a secret token, and every action on the bond must carry it
// (header X-Operator-Token; the page keeps it in the creator's browser).

import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import crypto from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { AsyncLocalStorage } from "node:async_hooks";
import { performance } from "node:perf_hooks";
import * as anchor from "@anchor-lang/core";
import {
  ExtensionType,
  MINT_SIZE,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountIdempotentInstruction,
  createInitializeMint2Instruction,
  createMintToInstruction,
  getAccountLen,
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
} from "@solana/web3.js";
import BN from "bn.js";
import {
  admitTradeInstruction, cancelDvpInstruction, createDvpInstruction, fundBondLegInstruction, fundCashLegInstruction, randomNonce,
  settleDvpInstruction, swapDvpAddress, verifyHookAccounts,
} from "./dvp.ts";

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
const venue = readKeypair(env("OPERATOR_VENUE"));
const FAUCET = "https://faucet.solana.com";
// The commit the service runs (the program it serves is built from the same tree): named in each audit's evidence.
const COMMIT = (() => {
  try {
    const head = fs.readFileSync(path.join(ROOT, ".git/HEAD"), "utf8").trim();
    return head.startsWith("ref: ") ? fs.readFileSync(path.join(ROOT, ".git", head.slice(5)), "utf8").trim() : head;
  } catch { return "unknown"; }
})();

const DAY = 86_400;
// Market rules of the sample bond (Kazakh depository): holders as of 09:00 Almaty on the payment day, payments from
// 09:00 to 17:00, Saturday and Sunday off, the KZ holiday calendar.
const RULES = {
  recordTimeLocal: "09:00", paymentOpensLocal: "09:00", paymentDeadlineLocal: "17:00", utcOffsetHours: 5,
  recordBusinessDaysBeforePayment: 0, weekend: ["Saturday", "Sunday"], calendar: "data/calendars/kz-holidays.txt",
};
const CASH_DECIMALS = 6;
const UNIT = 10n ** BigInt(CASH_DECIMALS);
const FEE = 5_000; // lamports per signature (base fee; no priority fee is set)
// Accounts a trade and a tender open (programs/bond_servicing/src/state.rs, admit_trade.rs): the escrow's holder record
// stays on chain; the DvP nonce marker stays too (measured on devnet 26.10.2026: 0 bytes, 650,240 lamports); the trade
// record (458 bytes) and its two escrow accounts go back to the venue at settlement.
const MAX_OFFERS = 10; // offers per bond
const HOLDER_RECORD = 467, SWAP_RECORD = 458, TENDER_RECORD = 8 + 32 + 32 + 4 + 8 + 1 + 8 + 1 + 1;
const POINTS = [-1, 0, 1] as const;
const POINT_NAMES: Record<number, string> = { [-1]: "за день до отсечки", 0: "в момент отсечки", 1: "через день после отсечки" };
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const secondsOf = (hhmm: string) => { const [h, m] = hhmm.split(":").map(Number); return h * 3_600 + m * 60; };
const dayNumber = (iso: string) => Math.round(Date.parse(`${iso}T00:00:00Z`) / 1000 / DAY);
const localIso = (bondUnix: number) => new Date((bondUnix + RULES.utcOffsetHours * 3_600) * 1000).toISOString().slice(0, 16).replace("T", " ");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const chunks = <T,>(items: T[], size: number) => Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));

// ---------------------------------------------------------------- RPC: one budget for every request

// Public devnet, from one IP: 100 requests per 10 s, 40 per method, 40 connections (solana.com/docs/references/
// clusters). The service keeps under them with a margin and counts every physical request by method and operation.
const LIMIT = { windowMs: 10_000, total: 90, perMethod: 35, inFlight: 6 };
class Budget {
  private times: number[] = [];
  private methods = new Map<string, number[]>();
  private inFlight = 0;
  private until = 0;
  reserved = 0; // requests per window lent to the audit process while it runs
  counts = { total: 0, limited: 0, method: {} as Record<string, number>, op: {} as Record<string, number> };
  async take(method: string) {
    for (;;) {
      const now = Date.now(), from = now - LIMIT.windowMs;
      while (this.times.length && this.times[0] <= from) this.times.shift();
      const m = this.methods.get(method) ?? [];
      while (m.length && m[0] <= from) m.shift();
      this.methods.set(method, m);
      const wait = Math.max(this.until - now,
        this.times.length >= LIMIT.total - this.reserved ? this.times[0] + LIMIT.windowMs - now : 0,
        m.length >= LIMIT.perMethod ? m[0] + LIMIT.windowMs - now : 0,
        this.inFlight >= LIMIT.inFlight ? 25 : 0);
      if (wait <= 0) { this.times.push(now); m.push(now); this.inFlight++; return; }
      await sleep(Math.min(wait, 500) + Math.random() * 30);
    }
  }
  done() { this.inFlight--; }
  hold(ms: number) { this.until = Math.max(this.until, Date.now() + ms); }
  count(method: string, op: string) {
    this.counts.total++;
    this.counts.method[method] = (this.counts.method[method] ?? 0) + 1;
    this.counts.op[op] = (this.counts.op[op] ?? 0) + 1;
  }
}
const budget = new Budget();
// The operation a request belongs to, carried through every await of that operation.
const opLabel = new AsyncLocalStorage<string>();
const rpcMethod = (body: unknown) => { try { return JSON.parse(String(body)).method ?? "?"; } catch { return "?"; } };
const budgetedFetch = async (input: any, init?: any) => {
  const method = rpcMethod(init?.body);
  for (let attempt = 1; ; attempt++) {
    await budget.take(method);
    budget.count(method, (opLabel.getStore() ?? "service").split(":")[0]);
    let res: Response;
    try {
      res = await fetch(input, init);
    } catch (e) {
      budget.done();
      if (attempt >= 5) throw e;
      await sleep(1_000 * attempt);
      continue;
    }
    budget.done();
    if (res.status !== 429) return res;
    budget.counts.limited++;
    // The documentation: use Retry-After; without it, the limit's own 10-second window.
    const after = Number(res.headers.get("retry-after"));
    budget.hold(1_000 * (after > 0 ? after : 10) + Math.random() * 1_000);
    if (attempt >= 6) throw new Error(`RPC ${method}: 429 six times in a row`);
  }
};
const connection = new Connection(RPC, { commitment: "confirmed", fetch: budgetedFetch as any, disableRetryOnRateLimit: true });
const idl = JSON.parse(fs.readFileSync(path.join(ROOT, "ops/idl/bond_servicing.json"), "utf8"));
const program = new anchor.Program(idl, new anchor.AnchorProvider(connection, new anchor.Wallet(registrar), { commitment: "confirmed" }));
// The program's own coder: account, event and field names in camelCase, as the code reads them.
const coder = program.coder;
const eventParser = new anchor.EventParser(program.programId, coder);
const computeLimit = () => ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });
const pda = (...seeds: (Buffer | Uint8Array)[]) => PublicKey.findProgramAddressSync(seeds, program.programId)[0];
const u16 = (n: number) => Buffer.from(new Uint16Array([n]).buffer);

// ---------------------------------------------------------------- time, blockhash, block height

// Chain time: the Clock sysvar as the chain pushes it every slot (an account subscription at `confirmed`, the
// commitment every transaction is simulated at before it is sent), read once at start. The program checks time
// against the Clock of the slot that executes a transaction, and that Clock does not follow a server's clock (a local
// validator's stood still for 7 slots and then jumped), so the service takes the time only from the chain: a step due
// at a moment is sent once the chain's confirmed Clock has reached it, never earlier (at `processed` the simulation,
// two slots behind, still refused an event as early: devnet, 2026-10-26).
let clockAt = { chain: 0, mono: 0 };
const takeClock = (data: Buffer) => {
  const chain = Number(data.readBigInt64LE(32));
  if (chain >= clockAt.chain) clockAt = { chain, mono: performance.now() };
};
const readClock = async () => takeClock((await opLabel.run("clock", () => connection.getAccountInfo(SYSVAR_CLOCK_PUBKEY)))!.data);
const chainNow = () => clockAt.chain;
const clockAge = () => Math.round((performance.now() - clockAt.mono) / 1000);

// One blockhash for every transaction, renewed every 20 seconds (it lives about 60 to 90: solana.com/docs/advanced/
// confirmation), fetched at `confirmed`.
let latest: { blockhash: string; lastValidBlockHeight: number; mono: number } | null = null;
let latestFetch: Promise<void> | null = null;
const blockhash = async () => {
  if (!latest || performance.now() - latest.mono > 20_000) {
    latestFetch ??= opLabel.run("blockhash", () => connection.getLatestBlockhash("confirmed"))
      .then((r) => { latest = { ...r, mono: performance.now() }; })
      .finally(() => { latestFetch = null; });
    await latestFetch;
  }
  return latest!;
};
let height = { value: 0, mono: -1e9 };

// ---------------------------------------------------------------- transactions: one path

type Sent = { sig: string; raw: string; lastValid: number; label: string; state: "pending" | "confirmed" | "failed" | "expired"; slot?: number; error?: string; at: number; signatures: number };
class Refused extends Error {} // the program or a token program refused the transaction: nothing changed on chain
const waiting = new Map<string, { s: Sent; b?: BondState; lastSend: number; done: (state: Sent["state"]) => void }>();
const receipts = new Map<string, any>();

// The program's message for a refused transaction, short enough for the screen.
const reason = (e: any) => {
  const text = [String(e?.message ?? e), ...(e?.logs ?? e?.transactionLogs ?? [])].join("\n");
  const found = /Error Message: ([^"\n]*)/.exec(text) ?? /custom program error: (0x[0-9a-f]+)/.exec(text) ?? /Message: ([^\n]*)/.exec(text);
  return (found?.[1] ?? text.split("\n")[0]).trim().slice(0, 300);
};

// Sign, journal, send, wait. The same signed bytes are resent while their blockhash lives; a transaction past its
// blockhash and absent from the history can no longer land, and only then is it built again (amounts recomputed).
async function transact(b: BondState | undefined, label: string, build: () => Promise<{ ixs: TransactionInstruction[]; signers: Keypair[] }>) {
  return opLabel.run(label, async () => {
    for (let attempt = 1; attempt <= 4; attempt++) {
      const { ixs, signers } = await build();
      const { blockhash: hash, lastValidBlockHeight } = await blockhash();
      const tx = new Transaction({ feePayer: signers[0].publicKey, blockhash: hash, lastValidBlockHeight }).add(...ixs);
      tx.sign(...signers);
      const raw = tx.serialize();
      const s: Sent = { sig: anchor.utils.bytes.bs58.encode(tx.signature!), raw: raw.toString("base64"), lastValid: lastValidBlockHeight, label, state: "pending", at: Date.now(), signatures: tx.signatures.length };
      if (b) { b.ops.push(s); save(b); }
      try {
        await connection.sendRawTransaction(raw, { skipPreflight: false, preflightCommitment: "confirmed", maxRetries: 0 });
      } catch (e) {
        const msg = reason(e);
        // A node behind the cluster has not seen the blockhash yet: the same bytes go again from the poller.
        if (!/Blockhash not found/i.test(msg)) {
          s.state = "failed"; s.error = msg; s.raw = "";
          if (b) save(b);
          throw new Refused(msg);
        }
      }
      const state = await new Promise<Sent["state"]>((done) => waiting.set(s.sig, { s, b, lastSend: Date.now(), done }));
      if (state === "confirmed") return s;
      if (state === "failed") throw new Refused(s.error!);
    }
    throw new Error(`${label}: not landed after 4 attempts`);
  });
}
// One status request for all waiting transactions each second.
async function poll() {
  if (!waiting.size) return;
  await opLabel.run("confirm", async () => {
    if (performance.now() - height.mono > 5_000) height = { value: await connection.getBlockHeight("confirmed"), mono: performance.now() };
    for (const part of chunks([...waiting.values()], 256)) {
      const statuses = (await connection.getSignatureStatuses(part.map((w) => w.s.sig))).value;
      for (const [i, w] of part.entries()) {
        let st = statuses[i];
        const finish = async (state: Sent["state"]) => {
          w.s.state = state; w.s.raw = "";
          if (st?.slot) w.s.slot = st.slot;
          if (state === "failed") w.s.error = await failureReason(w.s.sig);
          waiting.delete(w.s.sig);
          if (w.b) save(w.b);
          w.done(state);
        };
        if (st && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) { await finish(st.err ? "failed" : "confirmed"); continue; }
        if (height.value > w.s.lastValid) {
          // Past its blockhash's life: the history decides; lagging nodes get 32 more blocks (about 13 s).
          st = (await connection.getSignatureStatuses([w.s.sig], { searchTransactionHistory: true })).value[0];
          if (st && st.confirmationStatus !== "processed") await finish(st.err ? "failed" : "confirmed");
          else if (height.value > w.s.lastValid + 32) await finish("expired");
          continue;
        }
        // "clients should keep resending a transaction" (solana.com/docs/advanced/confirmation): the same bytes
        if (w.s.raw && Date.now() - w.lastSend > 4_000) {
          w.lastSend = Date.now();
          connection.sendRawTransaction(Buffer.from(w.s.raw, "base64"), { skipPreflight: true, maxRetries: 0 }).catch(() => {});
        }
      }
    }
  });
}
const receipt = async (sig: string) => {
  if (!receipts.has(sig)) {
    const tx = await opLabel.run("receipt", () => connection.getTransaction(sig, { commitment: "confirmed", maxSupportedTransactionVersion: 0 }));
    if (!tx) throw new Error(`receipt of ${sig} not found`);
    receipts.set(sig, tx);
    if (receipts.size > 500) receipts.delete(receipts.keys().next().value!);
  }
  return receipts.get(sig);
};
const failureReason = async (sig: string) => { try { return reason({ message: "", logs: (await receipt(sig)).meta.logMessages }); } catch { return "transaction failed"; } };
const programEvents = async (sig: string) => [...eventParser.parseLogs((await receipt(sig)).meta.logMessages)];
// Bond time of the block that executed a transaction: what the program saw, not when the service learned of it.
const executedAt = async (b: BondState, sig: string) => bondTimeOf(b, Number((await receipt(sig)).blockTime));
// How many of `items` fit one legacy transaction with `head` (1232 bytes: solana.com/docs/core/transactions/
// transaction-structure), at most `max`.
function pack<T>(head: TransactionInstruction[], items: T[], ixOf: (x: T) => TransactionInstruction, signers: number, payer: PublicKey, max: number) {
  let n = Math.min(items.length, max);
  for (; n > 1; n--) {
    const tx = new Transaction({ feePayer: payer, blockhash: PublicKey.default.toBase58(), lastValidBlockHeight: 0 }).add(...head, ...items.slice(0, n).map(ixOf));
    if (tx.serializeMessage().length + 1 + 64 * signers <= 1232) break;
  }
  return Math.max(n, 1);
}

// ---------------------------------------------------------------- state

type Params = {
  issue: string; years: number; periodsPerYear: number; face: number; ratePct: number; redemptionPct: number;
  holders: number; issueSize: number; maxTradesPerHolder: number; propensityStart: number; propensityEnd: number;
};
type EventState = {
  index: number; kind: "coupon" | "redemption"; auto: boolean; recordTime: number; paymentTime: number;
  openSig?: string; fundSig?: string; budget?: string; supplyAtRecord?: number;
  paid: Record<string, { amount: string; sig: string; slot?: number; at: number }>; done: boolean; error?: string; retryAt?: number;
};
type OfferState = {
  id: number; kind: "buyback" | "tender"; priceUnits: string; maxQuantity: number; open: number; close: number; auto: boolean;
  createSig?: string; fundSig?: string; funded: boolean; done: boolean; error?: string; retryAt?: number;
  participants: string[]; others: string[]; plans: { holder: string; at: number; share: number }[];
  actions: { phase: number; holder: string; sell?: number; tender?: number; sig: string; slot?: number; paid?: string }[];
  settlements: { holder: string; number: number; burned: number; paid: string; phase: number; sig: string; slot?: number }[];
};
type TradeState = {
  id: number; kind: "random" | "mandatory"; k?: number; point?: number; from?: string; to?: string; amount?: number; cash?: string;
  nonce?: string; swap?: string; stage: "planned" | "seller" | "buyer" | "settle" | "settled" | "cancelled" | "refused" | "missed";
  sigs: string[]; fundedPassed?: number; settledPassed?: number; fundSlot?: number; slot?: number; startedAt?: number; fundedAt?: number; settledAt?: number;
  note?: string; retryAt?: number;
};
type LedgerRow = { n: number; chain: number; bond: number; text: string; sig?: string; kind: string };
type BondState = {
  id: string; createdAt: string; params: Params; title: string; status: "creating" | "live" | "done" | "error"; error?: string; token: string;
  bondMint: string; cashMint: string; bond: string; holders: { name: string; pubkey: string }[]; placements: Record<string, number>;
  cashTarget: Record<string, string>; solTarget: number;
  bondOrigin: number; chainOrigin?: number; issueTime: number; maturityTime: number; latencyMs?: number;
  schedule: { payment_days: number[]; payment_dates: string[]; record_days: number[]; record_dates: string[] };
  recordTimes: number[]; events: EventState[]; offers: OfferState[]; trades: TradeState[]; ledger: LedgerRow[]; ops: Sent[];
  reserve: Record<string, number>; decidedDay: number; rng: number; hookChecked?: boolean;
  bankTopUps: Record<string, string>; audit?: any; auditError?: string; auditRunning?: boolean; swept?: boolean;
};
type Keys = { bondMint: number[]; cashMint: number[]; holders: Record<string, number[]> };
type Snapshot = {
  at: number; slot: number; records: Record<string, any | null>; bondBal: Record<string, bigint>; cashBal: Record<string, bigint>; sol: Record<string, number>;
  events: (any | null)[]; vaults: (bigint | null)[]; offers: (any | null)[]; offerVaults: (bigint | null)[]; tenders: Record<string, any | null>; lamports: number; supply: bigint;
};
type Runtime = {
  b: BondState; keys: { bondMint: Keypair; cashMint: Keypair; holders: Record<string, Keypair> };
  jobs: Map<string, Promise<void>>; locked: Set<string>; snap?: Snapshot; refreshing?: Promise<void>; lastFull: number; dirty: boolean; issueRetryAt: number;
};

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
const fmtCash = (units: string | bigint) => {
  const v = BigInt(units), whole = v / UNIT, frac = v % UNIT;
  return `${whole.toString().replace(/\B(?=(\d{3})+(?!\d))/g, " ")}${frac ? "," + frac.toString().padStart(CASH_DECIMALS, "0").replace(/0+$/, "") : ""}`;
};
const bondTimeOf = (b: BondState, chain: number) => b.bondOrigin + (chain - b.chainOrigin!) * DAY;
const bondNow = (b: BondState) => (b.chainOrigin === undefined ? b.bondOrigin : bondTimeOf(b, chainNow()));
const note = (b: BondState, kind: string, text: string, sig?: string) => {
  b.ledger.push({ n: b.ledger.length, chain: chainNow(), bond: bondNow(b), text, sig, kind });
  save(b);
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
const names = (b: BondState) => b.holders.map((h) => h.name);
const positionOf = (rec: any, e: number) => Number(e < rec.position.liveFrom ? rec.position.at[e] : rec.position.quantity);
const freeOf = (rec: any) => (rec ? Number(rec.position.quantity) - Number(rec.locked) - Number(rec.escrowed) : 0);
const isPaid = (rec: any, e: number) => (BigInt(rec.paid.toString()) & (1n << BigInt(e))) !== 0n;
const tokenAmount = (data: Buffer | undefined) => (data && data.length >= 72 ? data.readBigUInt64LE(64) : 0n);

// One read of the bond's accounts (portions of 100: solana.com/docs/rpc/http/getmultipleaccounts); the screen, the
// scheduler and every decision use it. After each operation the next tick reads it again.
async function refresh(rt: Runtime) {
  rt.refreshing ??= opLabel.run(`snapshot:${rt.b.id}`, async () => {
    const { b } = rt;
    const a = accountsOf(rt), hs = names(b);
    const keys: PublicKey[] = [a.mint, ...hs.map(a.record), ...hs.map(a.bondsAta), ...hs.map(a.cashAta), ...hs.map(a.owner)];
    const ev = b.events.map((e) => a.event(e.index)), evVaults = ev.map((e) => a.vault(e));
    const of = b.offers.map((o) => a.offer(o.id)), ofVaults = of.map((o) => a.vault(o));
    const tenders = b.offers.filter((o) => o.kind === "tender").flatMap((o) => o.participants.map((h) => [o.id, h, a.tender(o.id, h)] as const));
    keys.push(...ev, ...evVaults, ...of, ...ofVaults, ...tenders.map((t) => t[2]));
    const infos: any[] = [];
    let slot = 0;
    for (const part of chunks(keys, 100)) {
      const r = await connection.getMultipleAccountsInfoAndContext(part, "confirmed");
      infos.push(...r.value);
      slot = Math.max(slot, r.context.slot);
    }
    let i = 1;
    const take = () => infos[i++];
    const decode = (name: string, info: any) => (info ? coder.accounts.decode(name, info.data) : null);
    const snap: Snapshot = { at: chainNow(), slot, records: {}, bondBal: {}, cashBal: {}, sol: {}, events: [], vaults: [], offers: [], offerVaults: [], tenders: {}, lamports: 0, supply: 0n };
    snap.supply = infos[0] ? infos[0].data.readBigUInt64LE(36) : 0n;
    for (const h of hs) snap.records[h] = decode("holderRecord", take());
    for (const h of hs) snap.bondBal[h] = tokenAmount(take()?.data);
    for (const h of hs) snap.cashBal[h] = tokenAmount(take()?.data);
    for (const h of hs) snap.sol[h] = (take()?.lamports ?? 0) / LAMPORTS_PER_SOL;
    for (const _ of ev) snap.events.push(decode("event", take()));
    for (const _ of ev) { const v = take(); snap.vaults.push(v ? tokenAmount(v.data) : null); }
    for (const _ of of) snap.offers.push(decode("offer", take()));
    for (const _ of of) { const v = take(); snap.offerVaults.push(v ? tokenAmount(v.data) : null); }
    for (const [id, h] of tenders) snap.tenders[`${id}:${h}`] = decode("tender", take());
    // deposits the bond holds on chain: every account read above except the holders' wallets
    const wallets = new Set(hs.map((h) => a.owner(h).toBase58()));
    snap.lamports = keys.reduce((s, k, j) => s + (wallets.has(k.toBase58()) ? 0 : infos[j]?.lamports ?? 0), 0);
    rt.snap = snap;
    rt.lastFull = Date.now();
    rt.dirty = false;
  }).finally(() => { rt.refreshing = undefined; });
  return rt.refreshing;
}

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
  // At least 20 holders; more once a measured run shows the RPC budget leaves room (plan-refined.md, 11.1).
  int(p.holders, 20, 20, "держателей");
  int(p.issueSize, p.holders, 100_000, "облигаций в выпуске (не меньше числа держателей)");
  int(p.maxTradesPerHolder, 1, 50, "сделок на держателя за срок");
  for (const [v, what] of [[p.propensityStart, "склонность к сделкам в начале"], [p.propensityEnd, "склонность к сделкам в конце"]] as const)
    if (!(v >= 0 && v <= 1)) errors.push(`${what}: от 0 до 1`);
  const maturityYear = Number(p.issue.slice(0, 4)) + p.years;
  if (Number(p.issue.slice(0, 4)) < 2024 || maturityYear > 2040) errors.push("годы выпуска и погашения: 2024-2040 (календарь праздников)");
  // Every record moment has 6 trades of 2 holders each: 12 participations per record moment within H x T.
  const records = p.years * p.periodsPerYear;
  if (Number.isInteger(p.holders) && Number.isInteger(p.maxTradesPerHolder) && p.holders * p.maxTradesPerHolder < 12 * records)
    errors.push(`сделок на держателя за срок: нужно не меньше ${Math.ceil((12 * records) / p.holders)} (у каждой отсечки 6 обязательных сделок по 2 участника)`);
  return errors;
};
// SOL a bond spends, from the sizes of the accounts it opens and the fees it pays (rent per byte read from the cluster).
let rentPerByte = 0, rentBase = 0;
const rent = (bytes: number) => rentBase + rentPerByte * bytes;
const BOND_ATA = getAccountLen([ExtensionType.TransferHookAccount, ExtensionType.ImmutableOwner]);
const holderSol = (p: Params) => {
  // Each sale the seller pays the trade record and its two escrow accounts (the venue receives them when it settles or
  // cancels), the nonce marker (an empty account) and the escrow's holder record (both stay), and the fee; a purchase
  // only the fee. At most T trades. Each offer's answer: a tender record (it stays) and the fee. The wallet itself keeps
  // the minimum balance of an empty account.
  const sale = rent(SWAP_RECORD) + rent(BOND_ATA) + rent(165) + rent(0) + rent(HOLDER_RECORD) + FEE;
  return (p.maxTradesPerHolder * sale + MAX_OFFERS * (rent(TENDER_RECORD) + FEE) + rent(0)) / LAMPORTS_PER_SOL;
};
const estimateSol = (p: Params) => {
  const periods = p.years * p.periodsPerYear;
  const holder = rent(HOLDER_RECORD) + rent(BOND_ATA) + rent(165);
  const events = (periods + 1) * (rent(200) + rent(165) + 10 * FEE);
  const lamports = rent(MINT_SIZE) + 3 * rent(600) + p.holders * holder + events + 200 * FEE;
  return lamports / LAMPORTS_PER_SOL + p.holders * holderSol(p);
};

async function createBond(p: Params): Promise<Runtime> {
  const months = 12 / p.periodsPerYear;
  const maturity = `${Number(p.issue.slice(0, 4)) + p.years}${p.issue.slice(4)}`;
  const { stdout } = await promisify(execFile)(SCHEDULE, [p.issue, maturity, String(months), path.join(ROOT, RULES.calendar), RULES.weekend.join(","), "0"], { encoding: "utf8" });
  const schedule = JSON.parse(stdout);
  const id = crypto.randomBytes(6).toString("hex");
  const mintKp = Keypair.generate(), cashKp = Keypair.generate();
  const holderKps = Object.fromEntries(Array.from({ length: p.holders }, (_, i) => [`BH#${i + 1}`, Keypair.generate()]));
  const b: BondState = {
    id, createdAt: new Date().toISOString(), params: p, token: crypto.randomBytes(16).toString("hex"),
    title: `${p.face} · ${p.ratePct}% · ${p.periodsPerYear} куп./год · ${p.years} г. · ${p.holders} держ.`,
    status: "creating", bondMint: mintKp.publicKey.toBase58(), cashMint: cashKp.publicKey.toBase58(),
    bond: pda(Buffer.from("bond"), mintKp.publicKey.toBuffer()).toBase58(),
    holders: Object.entries(holderKps).map(([name, kp]) => ({ name, pubkey: kp.publicKey.toBase58() })), placements: {}, cashTarget: {}, solTarget: holderSol(p),
    bondOrigin: 0, issueTime: dayNumber(p.issue) * DAY, maturityTime: dayNumber(maturity) * DAY, schedule,
    recordTimes: schedule.record_days.map((d: number) => d * DAY + secondsOf(RULES.recordTimeLocal) - RULES.utcOffsetHours * 3_600),
    events: [], offers: [], trades: [], ledger: [], ops: [], reserve: {}, decidedDay: 0, rng: crypto.randomBytes(4).readUInt32LE(), bankTopUps: {},
  };
  // The issue split among the holders at random weights, at least one bond each.
  const hs = names(b);
  const weights = hs.map(() => random(b)), total = weights.reduce((x, y) => x + y, 0);
  let left = p.issueSize - p.holders;
  hs.forEach((h, i) => { const q = Math.floor((weights[i] / total) * (p.issueSize - p.holders)); b.placements[h] = 1 + q; left -= q; });
  for (let i = 0; left > 0; i++, left--) b.placements[hs[i % hs.length]] += 1;
  // Cash for the purchases a holder may make (simulated deposit): T purchases of at most an average holding, at face + 2%.
  const holding = BigInt(Math.max(1, Math.floor(p.issueSize / p.holders)));
  for (const h of hs) b.cashTarget[h] = (BigInt(p.face) * UNIT * BigInt(p.maxTradesPerHolder) * holding * 102n / 100n).toString();
  const periods = schedule.payment_days.length;
  const paymentTime = (k: number) => schedule.payment_days[k] * DAY + secondsOf(RULES.paymentOpensLocal) - RULES.utcOffsetHours * 3_600;
  for (let e = 0; e <= periods; e++) {
    const k = Math.min(e, periods - 1);
    b.events.push({ index: e, kind: e < periods ? "coupon" : "redemption", auto: true, recordTime: b.recordTimes[k], paymentTime: paymentTime(k), paid: {}, done: false });
  }
  // Mandatory trades: per participation a holder's quota is reserved, spread evenly (plan-refined.md, 7.3).
  const participations = 12 * periods;
  hs.forEach((h, i) => { b.reserve[h] = Math.floor(participations / hs.length) + (i < participations % hs.length ? 1 : 0); });
  for (let k = 0; k < periods; k++) for (const point of POINTS) for (let j = 0; j < 2; j++)
    b.trades.push({ id: b.trades.length, kind: "mandatory", k, point, stage: "planned", sigs: [] });
  fs.mkdirSync(dirOf(id), { recursive: true });
  const keys: Keys = { bondMint: [...mintKp.secretKey], cashMint: [...cashKp.secretKey], holders: Object.fromEntries(Object.entries(holderKps).map(([n, kp]) => [n, [...kp.secretKey]])) };
  fs.writeFileSync(path.join(dirOf(id), "keys.json"), JSON.stringify(keys), { mode: 0o600 });
  save(b);
  const rt: Runtime = { b, keys: { bondMint: mintKp, cashMint: cashKp, holders: holderKps }, jobs: new Map(), locked: new Set(), lastFull: 0, dirty: true, issueRetryAt: 0 };
  bonds.set(id, rt);
  return rt;
}

// Issuance in stages, each checked on chain first, so a restart continues where it stopped: the bond's own test cash
// token; create_bond (its clock starts here, so the bond's time origin leaves the measured preparation time before the
// first record moment); holders registered and the issue placed, three per transaction; SOL and cash for the holders.
async function issue(rt: Runtime) {
  const { b, keys } = rt;
  const a = accountsOf(rt), p = b.params, hs = names(b);
  if (!(await opLabel.run("issue", () => connection.getAccountInfo(a.cashMint)))) {
    const t0 = performance.now();
    const s = await transact(b, "issue:cash token", async () => ({
      ixs: [
        SystemProgram.createAccount({ fromPubkey: registrar.publicKey, newAccountPubkey: a.cashMint, space: MINT_SIZE, lamports: rent(MINT_SIZE), programId: TOKEN_PROGRAM_ID }),
        createInitializeMint2Instruction(a.cashMint, CASH_DECIMALS, bank.publicKey, bank.publicKey, TOKEN_PROGRAM_ID),
      ],
      signers: [registrar, keys.cashMint],
    }));
    b.latencyMs = performance.now() - t0;
    note(b, "setup", "тестовый cash-токен выпуска (банк: mint и freeze authority)", s.sig);
  }
  if (!(await opLabel.run("issue", () => connection.getAccountInfo(a.bond)))) {
    // Preparation after create_bond, one transaction after another: registration (3 holders each) and funding (8
    // each), twice the measured time of a transaction; then the first record moment's early trades need their lead.
    const latency = (b.latencyMs ?? 4_000) / 1000;
    const prep = Math.ceil(2 * latency * (Math.ceil(p.holders / 3) + Math.ceil(p.holders / 8))) + leadDays(b) + 2;
    b.bondOrigin = Math.min(dayNumber(p.issue) * DAY - 3 * DAY, b.recordTimes[0] - (prep + 1) * DAY) + 12 * 3_600 - RULES.utcOffsetHours * 3_600;
    save(b);
    const terms = {
      faceUnits: new BN((BigInt(p.face) * UNIT).toString()), rateBps: Math.round(p.ratePct * 100),
      basis: { fixedPerPeriod: { periodsPerYear: p.periodsPerYear } }, redemptionPriceBps: p.redemptionPct * 100,
      issueDay: dayNumber(p.issue), maturityDay: Math.round(b.maturityTime / DAY), monthsPerPeriod: 12 / p.periodsPerYear,
      paymentDays: b.schedule.payment_days, recordDays: b.schedule.record_days,
      recordSecondsLocal: secondsOf(RULES.recordTimeLocal), paymentSecondsLocal: secondsOf(RULES.paymentOpensLocal),
      utcOffsetSeconds: RULES.utcOffsetHours * 3_600, weekendDays: RULES.weekend.reduce((m, d) => m | (1 << WEEKDAYS.indexOf(d)), 0),
      bondOrigin: new BN(b.bondOrigin), chainSecondsPerBondDay: new BN(1), maxSupply: new BN(p.issueSize),
    };
    const s = await transact(b, "issue:create_bond", async () => ({
      ixs: [computeLimit(), await program.methods.createBond(terms).accountsPartial({
        registrar: registrar.publicKey, mint: a.mint, bond: a.bond, cashMint: a.cashMint,
        extraAccountMetaList: pda(Buffer.from("extra-account-metas"), a.mint.toBuffer()),
        tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
      }).instruction()],
      signers: [registrar, keys.bondMint],
    }));
    note(b, "setup", `create_bond: ${p.face} номинал, ${p.ratePct}% годовых, ${p.periodsPerYear} купона в год, ${b.schedule.payment_dates[0]} - ${b.schedule.payment_dates[b.schedule.payment_dates.length - 1]}, выпуск до ${p.issueSize}`, s.sig);
  }
  if (b.chainOrigin === undefined) {
    const bondAccount = await opLabel.run("issue", () => connection.getAccountInfo(a.bond));
    b.chainOrigin = Number((coder.accounts.decode("bond", bondAccount!.data) as any).chainOrigin);
    save(b);
  }
  await refresh(rt);
  for (const batch of chunks(hs.filter((h) => !rt.snap!.records[h]), 3)) {
    // The program places bonds only before the first record moment: past it the issue cannot be completed.
    if (bondNow(b) >= b.recordTimes[0]) {
      b.status = "error";
      b.error = `размещение не закончилось до первой отсечки ${localIso(b.recordTimes[0])}: программа после неё не размещает; не размещены ${hs.filter((h) => !rt.snap!.records[h]).join(", ")}`;
      note(b, "error", b.error);
      return;
    }
    const s = await transact(b, "issue:register", async () => {
      const ixs: TransactionInstruction[] = [computeLimit()];
      for (const h of batch) {
        ixs.push(await program.methods.registerHolder(a.owner(h)).accountsPartial({ registrar: registrar.publicKey, bond: a.bond }).instruction());
        ixs.push(
          createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.bondsAta(h), a.owner(h), a.mint, TOKEN_2022_PROGRAM_ID),
          createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.cashAta(h), a.owner(h), a.cashMint, TOKEN_PROGRAM_ID),
          await program.methods.place(new BN(b.placements[h])).accountsPartial({ registrar: registrar.publicKey, bond: a.bond, mint: a.mint, holder: a.record(h), holderBonds: a.bondsAta(h), tokenProgram: TOKEN_2022_PROGRAM_ID }).instruction(),
        );
      }
      return { ixs, signers: [registrar] };
    });
    note(b, "setup", `register_holder, place: ${batch.map((h) => `${h} ${b.placements[h]}`).join(", ")}`, s.sig);
  }
  await refresh(rt);
  // Investors with wallets of their own: SOL for their fees and deposits, cash for their purchases (simulated deposit).
  const short = hs.filter((h) => rt.snap!.sol[h] < b.solTarget * 0.999 || rt.snap!.cashBal[h] < BigInt(b.cashTarget[h]));
  for (const batch of chunks(short, 8)) {
    const s = await transact(b, "issue:fund holders", async () => {
      await refresh(rt);
      const ixs: TransactionInstruction[] = [];
      for (const h of batch) {
        const sol = Math.round((b.solTarget - rt.snap!.sol[h]) * LAMPORTS_PER_SOL);
        if (sol > 0) ixs.push(SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: a.owner(h), lamports: sol }));
        const cash = BigInt(b.cashTarget[h]) - rt.snap!.cashBal[h];
        if (cash > 0n) { ixs.push(createMintToInstruction(a.cashMint, a.cashAta(h), bank.publicKey, cash, [], TOKEN_PROGRAM_ID)); b.bankTopUps[h] = (BigInt(b.bankTopUps[h] ?? "0") + cash).toString(); }
      }
      return { ixs, signers: [registrar, bank] };
    });
    note(b, "setup", `симуляция: у держателей свои кошельки: SOL на комиссии и депозиты, деньги на покупки (${batch[0]}..${batch[batch.length - 1]})`, s.sig);
  }
  b.status = "live";
  b.error = undefined;
  note(b, "setup", `выпуск в обращении: ${p.holders} держателей, ${b.trades.length} обязательных сделок у ${b.recordTimes.length} отсечек`);
}

// ---------------------------------------------------------------- corporate actions

// One step of a coupon or the redemption, each event on its own: open (from the record moment); fund and confirm in
// one transaction; pay in packed transactions. A refused payment in a batch is retried alone, so one holder does not
// hold back the others. The redemption goes on until no holder has bonds left (bonds in an escrow or a tender wait).
async function eventStep(rt: Runtime, e: number, step: "open" | "fund" | "pay") {
  const { b } = rt;
  const a = accountsOf(rt), ev = b.events[e], label = ev.kind === "coupon" ? `купон ${e + 1}` : "погашение";
  const now = bondNow(b), event = a.event(e), vault = a.vault(event);
  if (step === "open") {
    if (now < ev.recordTime) throw new Refused(`${label}: момент отсечки ${localIso(ev.recordTime)} ещё не наступил`);
    const s = await transact(b, `event:${label} open`, async () => ({ ixs: [await program.methods.openEvent(e).accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()], signers: [registrar] }));
    const opened: any = coder.accounts.decode("event", (await opLabel.run("event", () => connection.getAccountInfo(event)))!.data);
    ev.openSig = s.sig; ev.budget = opened.budget.toString(); ev.supplyAtRecord = Number(opened.supplyAtRecord);
    note(b, "event", `open_event, ${label}: на отсечку ${ev.supplyAtRecord} облигаций, бюджет ${fmtCash(ev.budget!)}`, s.sig);
    return;
  }
  // The holders due a payment now, with their instructions: the coupon to every holder with a position at the record
  // moment and not paid yet; the redemption to every holder with free bonds (bonds in an escrow or a tender wait).
  const dueNow = async () => {
    await refresh(rt);
    const recs = rt.snap!.records;
    const due = ev.kind === "coupon"
      ? names(b).filter((h) => recs[h] && positionOf(recs[h], e) > 0 && !isPaid(recs[h], e))
      : names(b).filter((h) => freeOf(recs[h]) > 0 && !rt.locked.has(h));
    const built = new Map<string, TransactionInstruction>();
    for (const h of due) built.set(h, await (ev.kind === "coupon"
      ? program.methods.payCoupon().accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, holder: a.record(h), owner: a.owner(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()
      : program.methods.redeemHolder().accountsPartial({ payer: registrar.publicKey, bond: a.bond, mint: a.mint, event, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()));
    return { due, built };
  };
  // Every payment a confirmed transaction made, from the program's events.
  const recordPaid = async (s: Sent) => {
    const evs = (await programEvents(s.sig)).filter((y) => y.name === "couponPaid" || y.name === "bondsRedeemed");
    const lines: string[] = [];
    for (const x of evs) {
      const h = b.holders.find((y) => y.pubkey === x.data.owner.toBase58())!.name;
      const amount = (x.name === "couponPaid" ? x.data.amount : x.data.principal).toString();
      ev.paid[h] = { amount: (BigInt(ev.paid[h]?.amount ?? "0") + BigInt(amount)).toString(), sig: s.sig, slot: s.slot, at: await executedAt(b, s.sig) };
      lines.push(x.name === "couponPaid" ? `${h} ${Number(x.data.position)} обл. → ${fmtCash(amount)}` : `${h} сожжено ${Number(x.data.burned)} → ${fmtCash(amount)}`);
    }
    if (lines.length) note(b, "pay", `${ev.kind === "coupon" ? `pay_coupon ${e + 1}` : "redeem_holder"}: ${lines.join(", ")}`, s.sig);
  };
  // measured compute: 6 coupon payments or 4 redemptions fit 1,400,000 CU (the batches of ops/src/scenario.ts)
  const max = ev.kind === "coupon" ? 6 : 4;
  if (step === "fund") {
    if (!ev.budget) throw new Refused(`${label}: сначала открыть`);
    // The budget and confirm_funding in one transaction; once the payment time has come, the first batch of payments
    // goes in it too.
    let first: string[] = [];
    const s = await transact(b, `event:${label} fund`, async () => {
      await refresh(rt);
      const need = BigInt(ev.budget!) - (rt.snap!.vaults[e] ?? 0n);
      const ixs: TransactionInstruction[] = [computeLimit()];
      if (need > 0n) ixs.push(createMintToInstruction(a.cashMint, vault, bank.publicKey, need, [], TOKEN_PROGRAM_ID));
      ixs.push(await program.methods.confirmFunding().accountsPartial({ event, vault }).instruction());
      first = [];
      if (bondNow(b) >= ev.paymentTime) {
        const { due, built } = await dueNow();
        first = due.slice(0, due.length ? pack(ixs, due, (h) => built.get(h)!, 2, registrar.publicKey, max) : 0);
        ixs.push(...first.map((h) => built.get(h)!));
      }
      return { ixs, signers: [registrar, bank] };
    });
    ev.fundSig = s.sig;
    note(b, "event", `эмитент вносит бюджет и confirm_funding, ${label}: ${fmtCash(ev.budget!)}, выплаты открыты`, s.sig);
    if (first.length) await recordPaid(s);
    return;
  }
  if (!ev.fundSig) throw new Refused(`${label}: сначала внести бюджет`);
  if (now < ev.paymentTime) throw new Refused(`${label}: выплаты с ${localIso(ev.paymentTime)}`);
  const { due, built } = await dueNow();
  const send = async (batch: string[]) => recordPaid(await transact(b, `event:${label} pay`, async () => ({ ixs: [computeLimit(), ...batch.map((h) => built.get(h)!)], signers: [registrar] })));
  // The batches of different holders go at once; a refused batch is retried one holder at a time.
  const batches: string[][] = [];
  for (let rest = due.slice(); rest.length; ) batches.push(rest.splice(0, pack([computeLimit()], rest, (h) => built.get(h)!, 1, registrar.publicKey, max)));
  await Promise.all(batches.map(async (batch) => {
    if (ev.kind === "redemption") for (const h of batch) rt.locked.add(h);
    try {
      await send(batch);
    } catch (err) {
      if (!(err instanceof Refused) || batch.length === 1) throw err;
      for (const h of batch) { try { await send([h]); } catch (one) { note(b, "error", `${label}, ${h}: ${reason(one)}`); } }
    } finally {
      if (ev.kind === "redemption") for (const h of batch) rt.locked.delete(h);
    }
  }));
  await refresh(rt);
  const left = names(b).filter((h) => {
    const r = rt.snap!.records[h];
    return r && (ev.kind === "coupon" ? positionOf(r, e) > 0 && !isPaid(r, e) : Number(r.position.quantity) > 0);
  });
  if (!left.length) { ev.done = true; note(b, "event", ev.kind === "coupon" ? `купон ${e + 1} выплачен` : "погашение завершено"); }
}
const nextEventStep = (ev: EventState) => (!ev.openSig ? "open" : !ev.fundSig ? "fund" : !ev.done ? "pay" : null);

// ---------------------------------------------------------------- offers

async function addOffer(rt: Runtime, o: { kind: "buyback" | "tender"; price: number; maxQuantity: number; open: string; close: string }) {
  const { b } = rt;
  const a = accountsOf(rt);
  const open = dayNumber(o.open) * DAY + secondsOf("09:00") - RULES.utcOffsetHours * 3_600;
  const close = dayNumber(o.close) * DAY + secondsOf("17:00") - RULES.utcOffsetHours * 3_600;
  if (!(o.kind === "buyback" || o.kind === "tender")) throw new Refused("вид оферты: buyback или tender");
  if (!(Number.isInteger(o.maxQuantity) && o.maxQuantity > 0 && o.maxQuantity <= b.params.issueSize)) throw new Refused(`объём: целое от 1 до ${b.params.issueSize}`);
  if (!(o.price > 0 && o.price <= 1e9) || Math.round(o.price * 1e6) !== o.price * 1e6) throw new Refused("цена за облигацию: больше 0, до 6 знаков");
  const finalRecord = b.recordTimes[b.recordTimes.length - 1], now = bondNow(b);
  if (!(open < close)) throw new Refused("окно: дата открытия раньше даты закрытия");
  if (close <= now) throw new Refused("окно уже закрылось");
  if (close >= finalRecord) throw new Refused(`окно должно закрыться до последней отсечки ${localIso(finalRecord)}`);
  await refresh(rt);
  // Some holders take part, some do not: both groups non-empty among the holders with free bonds.
  const withBonds = names(b).filter((h) => freeOf(rt.snap!.records[h]) > 0);
  if (withBonds.length < 2) throw new Refused("свободные облигации есть меньше чем у двух держателей");
  for (let i = withBonds.length - 1; i > 0; i--) { const j = Math.floor(random(b) * (i + 1)); [withBonds[i], withBonds[j]] = [withBonds[j], withBonds[i]]; }
  const count = 1 + Math.floor(random(b) * (withBonds.length - 1));
  const id = b.offers.length + 1, priceUnits = BigInt(Math.round(o.price * 1e6)).toString();
  const offer = a.offer(id), vault = a.vault(offer);
  const s = await transact(b, `offer:create ${id}`, async () => ({ ixs: [await program.methods.createOffer(id, o.kind === "tender", new BN(priceUnits), new BN(o.maxQuantity), new BN(open), new BN(close)).accountsPartial({ registrar: registrar.publicKey, bond: a.bond, offer, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()], signers: [registrar] }));
  const from = Math.max(open, now);
  const participants = withBonds.slice(0, count);
  b.offers.push({
    id, kind: o.kind, priceUnits, maxQuantity: o.maxQuantity, open, close, auto: true, createSig: s.sig, funded: false, done: false,
    participants, others: withBonds.slice(count), plans: participants.map((holder) => ({ holder, at: from + Math.floor(random(b) * 0.9 * (close - from)), share: random(b) })),
    actions: [], settlements: [],
  });
  note(b, "offer", `create_offer ${id} (${o.kind === "tender" ? "tender, pro rata" : "buyback, first come"}): ${fmtCash(priceUnits)} за облигацию, до ${o.maxQuantity}, окно ${localIso(open)} - ${localIso(close)}; участвуют ${count} из ${withBonds.length} держателей со свободными облигациями`, s.sig);
}
async function offerStep(rt: Runtime, id: number, step: "fund" | "settle") {
  const { b } = rt;
  const a = accountsOf(rt), o = b.offers[id - 1];
  if (!o) throw new Refused(`оферты ${id} нет`);
  const offer = a.offer(id), vault = a.vault(offer);
  if (step === "fund") {
    const s = await transact(b, `offer:fund ${id}`, async () => {
      await refresh(rt);
      const need = BigInt(o.priceUnits) * BigInt(o.maxQuantity) - (rt.snap!.offerVaults[id - 1] ?? 0n);
      const ixs: TransactionInstruction[] = need > 0n ? [createMintToInstruction(a.cashMint, vault, bank.publicKey, need, [], TOKEN_PROGRAM_ID)] : [];
      ixs.push(await program.methods.confirmOfferFunding().accountsPartial({ offer, vault }).instruction());
      return { ixs, signers: [registrar, bank] };
    });
    o.fundSig = s.sig; o.funded = true;
    if (o.kind === "buyback") o.done = true;
    note(b, "offer", `эмитент вносит бюджет оферты ${id} и confirm_offer_funding: ${fmtCash((BigInt(o.priceUnits) * BigInt(o.maxQuantity)).toString())}`, s.sig);
    return;
  }
  if (o.kind !== "tender") throw new Refused("завершение только у tender");
  if (bondNow(b) < o.close) throw new Refused(`окно открыто до ${localIso(o.close)}`);
  await refresh(rt);
  // In the order the tenders were made (Tender.number); settle_tender resolves a tender itself (offer.rs).
  const open = o.participants.map((h) => ({ h, t: rt.snap!.tenders[`${id}:${h}`] })).filter((x) => x.t && !x.t.settled).sort((x, y) => x.t.number - y.t.number);
  const built = new Map<string, TransactionInstruction>();
  for (const x of open) built.set(x.h, await program.methods.settleTender().accountsPartial({ retirement: retirement(rt, id, x.h, registrar.publicKey), offer, tender: a.tender(id, x.h) }).instruction());
  for (let rest = open.map((x) => x.h); rest.length; ) {
    const batch = rest.splice(0, pack([computeLimit()], rest, (h) => built.get(h)!, 1, registrar.publicKey, 3));
    for (const h of batch) rt.locked.add(h);
    try {
      const s = await transact(b, `offer:settle ${id}`, async () => ({ ixs: [computeLimit(), ...batch.map((h) => built.get(h)!)], signers: [registrar] }));
      const sold = (await programEvents(s.sig)).filter((x) => x.name === "bondsBoughtBack");
      const passedNow = b.recordTimes.filter((t) => t <= bondNow(b)).length;
      for (const h of batch) {
        const x = sold.find((y) => y.data.owner.toBase58() === b.holders.find((z) => z.name === h)!.pubkey);
        o.settlements.push({ holder: h, number: open.find((y) => y.h === h)!.t.number, burned: x ? Number(x.data.burned) : 0, paid: x ? x.data.paid.toString() : "0", phase: (x ? Number(x.data.passed) : passedNow) - 1, sig: s.sig, slot: s.slot });
      }
      note(b, "offer", `settle_tender ${id}: ${batch.map((h) => { const st = o.settlements.find((y) => y.holder === h)!; return `${h} выкуплено и сожжено ${st.burned} → ${fmtCash(st.paid)}`; }).join(", ")}`, s.sig);
    } finally { for (const h of batch) rt.locked.delete(h); }
  }
  o.done = true;
}
const retirement = (rt: Runtime, id: number, h: string, payer: PublicKey) => {
  const a = accountsOf(rt);
  return {
    payer, bond: a.bond, mint: a.mint, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault: a.vault(a.offer(id)),
    cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID,
  };
};
const nextOfferStep = (o: OfferState, now: number) => (!o.funded ? "fund" : o.kind === "tender" && !o.done && now >= o.close ? "settle" : null);

// A holder answers an offer in its own transaction (it signs and pays its fee and its tender record's deposit): sells
// into a buyback (up to what is left of it) or tenders, a random share of its free bonds.
async function answerOffer(rt: Runtime, o: OfferState, plan: OfferState["plans"][number]) {
  const { b, keys } = rt;
  const a = accountsOf(rt);
  await refresh(rt);
  const free = freeOf(rt.snap!.records[plan.holder]);
  const sold = o.actions.reduce((s, x) => s + (x.sell ?? 0), 0);
  let q = Math.max(1, Math.round(plan.share * free));
  if (o.kind === "buyback") q = Math.min(q, o.maxQuantity - sold);
  if (free <= 0 || q <= 0) { note(b, "offer", `оферта ${o.id}: у ${plan.holder} нет свободных облигаций или объём исчерпан`); return; }
  const holder = keys.holders[plan.holder], owner = holder.publicKey, offer = a.offer(o.id);
  const s = await transact(b, `offer:answer ${o.id}`, async () => ({
    ixs: [o.kind === "buyback"
      ? await program.methods.sellToOffer(new BN(q)).accountsPartial({ retirement: retirement(rt, o.id, plan.holder, owner), seller: owner, offer }).instruction()
      : await program.methods.changeTender(new BN(q), false).accountsPartial({ owner, bond: a.bond, offer, holder: a.record(plan.holder), tender: a.tender(o.id, plan.holder) }).instruction()],
    signers: [holder],
  }));
  const paid = (await programEvents(s.sig)).find((x) => x.name === "bondsBoughtBack");
  const phase = paid ? Number(paid.data.passed) - 1 : b.recordTimes.filter((t) => t <= bondNow(b)).length - 1;
  o.actions.push({ phase, holder: plan.holder, ...(o.kind === "buyback" ? { sell: q } : { tender: q }), sig: s.sig, slot: s.slot, ...(paid ? { paid: paid.data.paid.toString() } : {}) });
  note(b, "offer", o.kind === "buyback"
    ? `sell_to_offer ${o.id}: ${plan.holder} продаёт ${q}, сожжено, получено ${fmtCash(paid?.data.paid.toString() ?? "0")}`
    : `change_tender ${o.id}: ${plan.holder} заявляет ${q} (облигации заблокированы, купон по ним сохраняется)`, s.sig);
}

// ---------------------------------------------------------------- trades (Solana DvP, 3 transactions)

// Participations: trades started and not refused count against a holder's limit T; mandatory trades draw on the
// holder's reserve, random trades on what is left beyond it.
const finished = (t: TradeState) => ["settled", "cancelled", "refused", "missed"].includes(t.stage);
const used = (b: BondState, h: string) => b.trades.filter((t) => (t.from === h || t.to === h) && !["planned", "refused", "missed"].includes(t.stage)).length;
const randomRoom = (b: BondState, h: string) => {
  const queued = b.trades.filter((t) => t.kind === "random" && t.stage === "planned" && (t.from === h || t.to === h)).length;
  return b.params.maxTradesPerHolder - used(b, h) - queued - (b.reserve[h] ?? 0) > 0;
};
const tradeTag = (t: TradeState) => (t.kind === "mandatory" ? ` [отсечка ${t.k! + 1}, ${POINT_NAMES[t.point!]}]` : "");

// The seller records the trade (naming the venue settlement authority), admits its escrow and delivers its bonds: one
// transaction it signs and pays; the buyer pays into the escrow in its own (the bank adds missing cash in it); the
// venue settles at `settleAt`, or cancels when the final record moment came first. A refused admission rolls the
// whole seller's transaction back, leaving nothing on chain.
async function tradeStep(rt: Runtime, t: TradeState, settleAt: number, held: string[]) {
  const { b, keys } = rt;
  const a = accountsOf(rt), tag = tradeTag(t);
  if (t.stage === "planned") {
    await refresh(rt);
    const recs = rt.snap!.records, hs = names(b);
    let from = t.from, to = t.to;
    if (t.kind === "mandatory") {
      // At the final record moment and after it the program refuses the admission whatever the seller holds by then
      // (the redemption burns bonds from that moment): the seller is a holder with a position at that record moment.
      // Such a trade moves no bonds, so it neither waits for its parties nor holds them.
      const closed = t.k === b.recordTimes.length - 1 && t.point! >= 0;
      const idle = (h: string) => closed || !rt.locked.has(h);
      const reserved = (h: string) => (b.reserve[h] ?? 0) > 0;
      const has = (h: string) => (closed ? !!recs[h] && positionOf(recs[h], t.k!) > 0 : freeOf(recs[h]) > 0);
      const sellers = [...hs.filter((h) => idle(h) && has(h) && reserved(h)), ...hs.filter((h) => idle(h) && has(h) && !reserved(h) && randomRoom(b, h))];
      from = sellers[Math.floor(random(b) * Math.min(sellers.length, 3))];
      const buyers = [...hs.filter((h) => h !== from && idle(h) && reserved(h)), ...hs.filter((h) => h !== from && idle(h) && !reserved(h) && randomRoom(b, h))];
      to = buyers[Math.floor(random(b) * Math.min(buyers.length, 3))];
      if (!from || !to) { t.stage = "missed"; t.note = "нет свободного держателя с облигациями и лимитом сделок"; note(b, "trade", `сделка пропущена${tag}: ${t.note}`); return; }
      // held by this job, released when it ends (run)
      for (const h of [from, to]) { if (!closed) { rt.locked.add(h); held.push(h); } if (reserved(h)) b.reserve[h] -= 1; }
    }
    const free = t.kind === "mandatory" && t.k === b.recordTimes.length - 1 && t.point! >= 0 ? positionOf(recs[from!], t.k!) : freeOf(recs[from!]);
    if (free <= 0) { t.stage = "missed"; t.note = `у ${from} нет свободных облигаций`; return; }
    t.from = from; t.to = to;
    t.amount = 1 + Math.floor(random(b) * Math.min(free, Math.max(1, Math.floor(b.params.issueSize / b.params.holders))));
    // price per bond within 2% of face
    t.cash = ((BigInt(b.params.face) * UNIT * BigInt(Math.round((0.98 + 0.04 * random(b)) * 10_000)) / 10_000n) * BigInt(t.amount)).toString();
    t.nonce = randomNonce().toString();
    t.swap = swapDvpAddress(venue.publicKey, a.owner(from!), a.owner(to!), a.mint, a.cashMint, BigInt(t.nonce)).toBase58();
    t.stage = "seller";
    t.startedAt = bondNow(b);
    save(b);
  }
  const seller = keys.holders[t.from!], buyer = keys.holders[t.to!];
  const dvp = { swap: new PublicKey(t.swap!), nonce: BigInt(t.nonce!), seller: seller.publicKey, buyer: buyer.publicKey, quantity: BigInt(t.amount!), cash: BigInt(t.cash!) };
  if (t.stage === "seller") {
    try {
      const s = await transact(b, `trade:seller ${t.id}`, async () => {
        const { ix: create } = createDvpInstruction(seller.publicKey, venue.publicKey, seller.publicKey, buyer.publicKey, a.mint, a.cashMint, TOKEN_PROGRAM_ID, dvp.quantity, dvp.cash, dvp.nonce, BigInt(chainNow() + DAY));
        const admit = await admitTradeInstruction(program, seller.publicKey, a.bond, a.mint, dvp.swap, seller.publicKey, buyer.publicKey);
        return { ixs: [create, admit, fundBondLegInstruction(program.programId, a.mint, seller.publicKey, dvp.swap, dvp.quantity)], signers: [seller] };
      });
      t.sigs.push(s.sig);
      const moved = (await programEvents(s.sig)).find((x) => x.name === "tradeMoved");
      t.fundedPassed = moved ? Number(moved.data.passed) : undefined;
      t.fundedAt = await executedAt(b, s.sig);
      t.fundSlot = s.slot;
      t.stage = "buyer";
      note(b, "trade", `${t.from} записывает сделку DvP, допускает escrow и кладёт ${t.amount} обл. (продаёт ${t.to} за ${fmtCash(t.cash!)}; позиция остаётся у продавца)${tag}`, s.sig);
    } catch (e) {
      if (!(e instanceof Refused)) throw e;
      t.stage = "refused"; t.note = reason(e);
      note(b, "trade", `${t.from}: escrow не допущен, сделка не состоялась: ${t.note}${tag}`);
      return;
    }
  }
  if (t.stage === "buyer") {
    const s = await transact(b, `trade:buyer ${t.id}`, async () => {
      await refresh(rt);
      const have = rt.snap!.cashBal[t.to!], ixs: TransactionInstruction[] = [], signers = [buyer];
      if (have < dvp.cash) { ixs.push(createMintToInstruction(a.cashMint, a.cashAta(t.to!), bank.publicKey, dvp.cash - have, [], TOKEN_PROGRAM_ID)); signers.push(bank); b.bankTopUps[t.to!] = (BigInt(b.bankTopUps[t.to!] ?? "0") + dvp.cash - have).toString(); }
      ixs.push(fundCashLegInstruction(a.cashMint, TOKEN_PROGRAM_ID, CASH_DECIMALS, buyer.publicKey, dvp.swap, dvp.cash));
      return { ixs, signers };
    });
    t.sigs.push(s.sig);
    t.stage = "settle";
    note(b, "trade", `${t.to} кладёт оплату в escrow${tag}`, s.sig);
  }
  if (t.stage === "settle") {
    if (bondNow(b) < settleAt) return; // the next tick settles it at its point
    try {
      const s = await transact(b, `trade:settle ${t.id}`, async () => ({ ixs: [settleDvpInstruction(program.programId, venue.publicKey, dvp, a.mint, a.cashMint, TOKEN_PROGRAM_ID)], signers: [venue] }));
      t.sigs.push(s.sig);
      const moved = (await programEvents(s.sig)).find((x) => x.name === "tradeMoved" && x.data.settled);
      t.settledPassed = moved ? Number(moved.data.passed) : undefined;
      t.slot = s.slot;
      t.settledAt = await executedAt(b, s.sig);
      t.stage = "settled";
      note(b, "trade", `площадка рассчитывает сделку: ${t.amount} обл. → ${t.to}, ${fmtCash(t.cash!)} → ${t.from}, одна транзакция${tag}`, s.sig);
    } catch (e) {
      if (!(e instanceof Refused)) throw e;
      // The final record moment came while the trade was open: both legs go back (the program lets an escrow return
      // bonds to its seller at any time).
      t.note = reason(e);
      note(b, "trade", `расчёт отклонён: ${t.note}${tag}`);
      const s = await transact(b, `trade:cancel ${t.id}`, async () => ({ ixs: [cancelDvpInstruction(program.programId, venue.publicKey, dvp, a.mint, a.cashMint, TOKEN_PROGRAM_ID)], signers: [venue] }));
      t.sigs.push(s.sig);
      t.stage = "cancelled";
      t.slot = s.slot;
      t.settledAt = await executedAt(b, s.sig);
      note(b, "trade", `площадка отменяет сделку: облигации → ${t.from}, деньги → ${t.to}${tag}`, s.sig);
    }
  }
}
// The outcome of a mandatory trade against its point (plan-refined.md, definitions).
function outcome(b: BondState, t: TradeState) {
  const final = t.k === b.recordTimes.length - 1;
  if (t.stage === "missed") return { ok: false, text: `пропущена: ${t.note}` };
  if (t.stage === "refused") return { ok: final, text: `escrow не допущен: ${t.note}` };
  if (t.stage === "cancelled") return { ok: final, text: "расчёт отклонён, облигации вернулись продавцу" };
  if (t.stage !== "settled") return { ok: false, text: "идёт" };
  const R = b.recordTimes[t.k!], after = t.k! + 1;
  if (t.point === -1) return { ok: t.fundedPassed === t.k && t.settledPassed === after, text: `escrow ${t.fundedPassed === t.k ? "до" : "после"} отсечки, расчёт ${t.settledPassed === after ? "после" : "до"} неё` };
  const day = Math.floor((t.settledAt! - R) / DAY);
  return { ok: t.settledPassed === after && day === t.point, text: `расчёт на ${day + 1}-й день после отсечки` };
}

// ---------------------------------------------------------------- the scheduler: every operation of a bond

// Seller's and buyer's transactions, measured (the issuance's first transaction), plus 2 seconds: how long before a
// record moment a mandatory trade starts so that its legs are in escrow before it.
const leadDays = (b: BondState) => Math.ceil((2 * (b.latencyMs ?? 4_000)) / 1000) + 2;
const MAX_JOBS = 6; // operations of one bond at once (each one transaction or a short chain of them)

function run(rt: Runtime, key: string, holders: string[], work: () => Promise<void>, onError?: (msg: string) => void) {
  for (const h of holders) rt.locked.add(h);
  const p = work()
    .catch((e) => { console.error(rt.b.id, key, e); const msg = reason(e); onError?.(msg); note(rt.b, "error", `${key}: ${msg}`); })
    .finally(() => { for (const h of holders) rt.locked.delete(h); rt.jobs.delete(key); rt.dirty = true; save(rt.b); });
  rt.jobs.set(key, p);
}
function tick(rt: Runtime) {
  const { b } = rt;
  if (b.status === "creating") {
    if (!rt.jobs.has("issue") && Date.now() >= rt.issueRetryAt) run(rt, "issue", [], () => issue(rt), (m) => { b.error = m; rt.issueRetryAt = Date.now() + 15_000; });
    return;
  }
  if (b.status !== "live" || b.chainOrigin === undefined) return;
  if ((rt.dirty && Date.now() - rt.lastFull > 2_000) || Date.now() - rt.lastFull > 15_000) refresh(rt).catch((e) => console.error(b.id, "refresh", e));
  if (!rt.snap) return;
  const now = bondNow(b), hs = names(b);
  const room = () => rt.jobs.size < MAX_JOBS;
  if (!b.hookChecked && !rt.jobs.has("hook")) run(rt, "hook", [], async () => {
    // the locally built hook accounts against the list on chain, once per bond (a transfer between two holders)
    await verifyHookAccounts(connection, program.programId, new PublicKey(b.bondMint), new PublicKey(b.holders[0].pubkey), new PublicKey(b.holders[1].pubkey));
    b.hookChecked = true;
  });
  // 1. corporate actions, each on its own
  for (const ev of b.events) {
    const step = nextEventStep(ev), key = `event ${ev.index}`;
    if (!ev.auto || !step || rt.jobs.has(key) || now < ev.recordTime || (step === "pay" && now < ev.paymentTime) || (ev.retryAt ?? 0) > Date.now() || !room()) continue;
    // with the box ticked the steps due run one after another (open, fund, pay), as their buttons would
    run(rt, key, [], async () => {
      for (let st: ReturnType<typeof nextEventStep> = step; st; st = nextEventStep(ev)) {
        if (st === "pay" && bondNow(b) < ev.paymentTime) break;
        await eventStep(rt, ev.index, st);
        if (st === "pay") break;
      }
    }, (m) => { ev.error = m; ev.retryAt = Date.now() + 10_000; });
  }
  // 2. offers: funding, then tender settlement after the window
  for (const o of b.offers) {
    const step = nextOfferStep(o, now), key = `offer ${o.id}`;
    if (!o.auto || !step || rt.jobs.has(key) || (o.retryAt ?? 0) > Date.now() || !room()) continue;
    run(rt, key, [], () => offerStep(rt, o.id, step), (m) => { o.error = m; o.retryAt = Date.now() + 10_000; });
  }
  // 3. holders answering open offers
  for (const o of b.offers) {
    if (!o.funded || now >= o.close) continue;
    for (const plan of o.plans.filter((x) => x.at <= now && !rt.locked.has(x.holder))) {
      if (!room()) break;
      o.plans.splice(o.plans.indexOf(plan), 1);
      run(rt, `answer ${o.id} ${plan.holder}`, [plan.holder], () => answerOffer(rt, o, plan));
    }
  }
  // 4. mandatory trades: legs in escrow before the record moment, settled at their point; at the final record moment
  //    the trades at it and after it start then (the program refuses them)
  const lead = leadDays(b), finalK = b.recordTimes.length - 1;
  for (const t of b.trades) {
    if (t.kind !== "mandatory" || finished(t) || rt.jobs.has(`trade ${t.id}`) || (t.retryAt ?? 0) > Date.now() || !room()) continue;
    const R = b.recordTimes[t.k!];
    const start = t.k === finalK && t.point! >= 0 ? R + t.point! * DAY : R - lead * DAY - (t.point === -1 ? DAY : 0);
    const settleAt = t.point === 1 ? R + DAY : R;
    if ((t.stage === "planned" && now < start) || (t.stage === "settle" && now < settleAt)) continue;
    const closed = t.k === finalK && t.point! >= 0, parties = closed ? [] : [t.from, t.to].filter(Boolean) as string[];
    if (parties.some((h) => rt.locked.has(h))) continue;
    run(rt, `trade ${t.id}`, parties, () => tradeStep(rt, t, settleAt, parties), () => { t.retryAt = Date.now() + 5_000; });
  }
  // 5. random trades: each bond day each holder trades with a probability between the two propensities
  const today = Math.floor(now / DAY), p = b.params, life = b.maturityTime - b.issueTime;
  for (let d = b.decidedDay + 1; d <= today; d++) {
    b.decidedDay = d;
    // trading closes at the final record moment (the program refuses every transfer from then on)
    if (d * DAY < b.issueTime || d * DAY >= b.recordTimes[finalK]) continue;
    const share = Math.min(1, Math.max(0, (d * DAY - b.issueTime) / life));
    const chance = p.propensityStart + (p.propensityEnd - p.propensityStart) * share;
    for (const h of hs) {
      if (!randomRoom(b, h) || random(b) >= chance) continue;
      const others = hs.filter((x) => x !== h && randomRoom(b, x));
      if (others.length) b.trades.push({ id: b.trades.length, kind: "random", from: h, to: others[Math.floor(random(b) * others.length)], stage: "planned", sigs: [] });
    }
  }
  for (const t of b.trades) {
    if (t.kind !== "random" || finished(t) || rt.jobs.has(`trade ${t.id}`) || (t.retryAt ?? 0) > Date.now() || !room()) continue;
    if (rt.locked.has(t.from!) || rt.locked.has(t.to!)) continue;
    if (t.stage === "planned" && now >= b.recordTimes[finalK]) { t.stage = "missed"; t.note = "торги закрыты с финальной отсечки"; continue; }
    const parties = [t.from!, t.to!];
    run(rt, `trade ${t.id}`, parties, () => tradeStep(rt, t, 0, parties), () => { t.retryAt = Date.now() + 5_000; });
  }
  // 6. the end: every event paid, every offer done, no trade in flight -> holders' leftover SOL back, then the audit
  if (b.events.every((e) => e.done) && b.offers.every((o) => o.done) && b.trades.every(finished) && rt.jobs.size === 0) {
    b.status = "done";
    note(b, "event", "все корпоративные действия выполнены; ончейн аудит");
    run(rt, "end", [], async () => { await sweep(rt); runAudit(rt); });
  }
}
// Simulation clean-up: the generated holders' leftover SOL goes back to the service's wallet.
async function sweep(rt: Runtime) {
  const { b, keys } = rt;
  await refresh(rt);
  const hs = names(b).filter((h) => rt.snap!.sol[h] * LAMPORTS_PER_SOL > 10 * FEE);
  for (const batch of chunks(hs, 8)) {
    const s = await transact(b, "end:sweep", async () => ({
      ixs: batch.map((h) => SystemProgram.transfer({ fromPubkey: keys.holders[h].publicKey, toPubkey: registrar.publicKey, lamports: Math.round(rt.snap!.sol[h] * LAMPORTS_PER_SOL) })),
      signers: [registrar, ...batch.map((h) => keys.holders[h])],
    }));
    note(b, "setup", `симуляция: остаток SOL держателей возвращается в кошелёк сервиса (${batch[0]}..${batch[batch.length - 1]})`, s.sig);
  }
  b.swept = true;
}
// The venue gets SOL for the records it settles and gives back what it collects beyond that.
async function balanceVenue() {
  const have = await opLabel.run("venue", () => connection.getBalance(venue.publicKey));
  if (have < 0.05 * LAMPORTS_PER_SOL)
    await transact(undefined, "venue", async () => ({ ixs: [SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: venue.publicKey, lamports: 0.1 * LAMPORTS_PER_SOL })], signers: [registrar] }));
  else if (have > 0.5 * LAMPORTS_PER_SOL)
    await transact(undefined, "venue", async () => ({ ixs: [SystemProgram.transfer({ fromPubkey: venue.publicKey, toPubkey: registrar.publicKey, lamports: have - 0.2 * LAMPORTS_PER_SOL })], signers: [venue] }));
}

// ---------------------------------------------------------------- audit

// The bond as the audit reads it: terms, public keys, and what the holders did, each in the phase the program saw.
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
  // settled trades, and cancelled ones whose seller had funded the escrow (its bonds went out and came back)
  trades: b.trades.filter((t) => t.stage === "settled" || (t.stage === "cancelled" && t.fundSlot !== undefined)).map((t) => ({
    afterRecordMoment: t.stage === "settled" ? t.settledPassed! - 1 : null, from: t.from, to: t.to, amount: t.amount, cash: t.cash,
    fundSlot: t.fundSlot, slot: t.slot, ...(t.stage === "cancelled" ? { cancelled: true } : {}),
  })),
  offers: b.offers.filter((o) => o.funded).map((o) => ({
    id: o.id, kind: o.kind, priceUnits: o.priceUnits, maxQuantity: o.maxQuantity,
    open: { recordMoment: 0, plusDays: (o.open - b.recordTimes[0]) / DAY }, close: { recordMoment: 0, plusDays: (o.close - b.recordTimes[0]) / DAY },
    actions: o.actions.map((x) => ({ afterRecordMoment: x.phase, holder: x.holder, ...(x.sell ? { sell: x.sell } : { tender: x.tender }), slot: x.slot })),
    ...(o.kind === "tender" && o.done ? { settlements: o.settlements.map((x) => ({ holder: x.holder, number: x.number, afterRecordMoment: x.phase, slot: x.slot })) } : {}),
  })),
  handover: null,
});
// What each payment the service sent was for, by signature: a coupon's index, "redemption" or "offer N" (labels only;
// the audit measures the amounts itself).
const paymentsOf = (b: BondState) => Object.fromEntries([
  ...b.events.flatMap((e) => Object.values(e.paid).map((p) => [p.sig, e.kind === "coupon" ? e.index : "redemption"])),
  ...b.offers.flatMap((o) => [...o.actions.filter((x) => x.sell).map((x) => x.sig), ...o.settlements.map((x) => x.sig)].map((sig) => [sig, `offer ${o.id}`])),
]);
// The audit is a separate process with its own RPC client: the service lends it a share of the budget while it runs.
const AUDIT_SHARE = 30;
function runAudit(rt: Runtime) {
  const { b } = rt;
  if (b.auditRunning) return;
  b.auditRunning = true;
  budget.reserved += AUDIT_SHARE;
  const dir = dirOf(b.id), scn = path.join(dir, "scenario.public.json"), pays = path.join(dir, "payments.json");
  const scenario = JSON.stringify(publicScenario(b), null, 1);
  fs.writeFileSync(scn, scenario);
  fs.writeFileSync(pays, JSON.stringify(paymentsOf(b)));
  const evidence = {
    bond: b.bond, bondMint: b.bondMint, cashMint: b.cashMint, programId: program.programId.toBase58(), commit: COMMIT, commitment: "confirmed",
    scenarioSha256: crypto.createHash("sha256").update(scenario).digest("hex"),
  };
  execFile(PYTHON, [path.join(ROOT, "tools/audit/audit_json.py"), scn, RPC, pays], { cwd: ROOT, maxBuffer: 64 << 20, env: { ...process.env, AUDIT_MAX_REQUESTS_PER_10S: String(AUDIT_SHARE) } }, (err, stdout, stderr) => {
    b.auditRunning = false;
    budget.reserved -= AUDIT_SHARE;
    if (err && !stdout) { b.auditError = (stderr || String(err)).slice(-2000); save(b); return; }
    try { b.audit = { ...JSON.parse(stdout), evidence, at: new Date().toISOString() }; b.auditError = undefined; } catch { b.auditError = (stderr || stdout).slice(-2000); }
    save(b);
  });
}

// ---------------------------------------------------------------- view for the screen (from the snapshot, no RPC)

function view(rt: Runtime, since: number) {
  const { b, snap } = rt;
  const now = b.chainOrigin === undefined ? null : bondNow(b);
  const passed = now === null ? 0 : b.recordTimes.filter((t) => t <= now).length;
  const done = b.ops.filter((o) => o.state === "confirmed" || o.state === "failed");
  const sigs = done.reduce((s, o) => s + o.signatures, 0);
  return {
    id: b.id, title: b.title, status: b.status, error: b.error, params: b.params, bond: b.bond, bondMint: b.bondMint, cashMint: b.cashMint,
    registrar: registrar.publicKey.toBase58(), venue: venue.publicKey.toBase58(), bondNow: now, clockAge: clockAge(), snapshotAge: snap ? chainNow() - snap.at : null,
    issueTime: b.issueTime, maturityTime: b.maturityTime, recordTimes: b.recordTimes, schedule: b.schedule, utcOffsetHours: RULES.utcOffsetHours,
    events: b.events.map((e, i) => ({
      ...e, vault: snap?.vaults[i]?.toString() ?? null,
      lateDays: Object.values(e.paid).length ? Math.max(...Object.values(e.paid).map((x) => Math.max(0, Math.floor((x.at - e.paymentTime) / DAY)))) : null,
      waiting: e.kind === "redemption" && snap ? names(b).filter((h) => snap.records[h] && (Number(snap.records[h].escrowed) > 0 || Number(snap.records[h].locked) > 0)) : [],
    })),
    offers: b.offers.map(({ plans, ...o }) => ({ ...o, waiting: plans.length })),
    trades: b.trades.filter((t) => t.stage !== "planned" || t.kind === "mandatory").map((t) => ({ ...t, ...(t.kind === "mandatory" ? { outcome: outcome(b, t), target: b.recordTimes[t.k!] + t.point! * DAY } : {}) })),
    reserve: b.reserve, used: Object.fromEntries(names(b).map((h) => [h, used(b, h)])),
    holders: b.holders.map((h) => {
      const r = snap?.records[h.name];
      return {
        name: h.name, pubkey: h.pubkey, placed: b.placements[h.name], sol: snap?.sol[h.name] ?? 0,
        quantity: r ? Number(r.position.quantity) : 0, escrowed: r ? Number(r.escrowed) : 0, locked: r ? Number(r.locked) : 0,
        atRecord: Array.from({ length: passed }, (_, e) => (r ? positionOf(r, e) : 0)),
        saved: Array.from({ length: passed }, (_, e) => !!r && e < Number(r.position.liveFrom)),
        received: Object.fromEntries(b.events.filter((e) => e.paid[h.name]).map((e) => [e.index, e.paid[h.name]])),
      };
    }),
    ledger: b.ledger.slice(since), ledgerSize: b.ledger.length,
    audit: b.audit, auditError: b.auditError, auditRunning: !!b.auditRunning, bankTopUps: b.bankTopUps,
    cost: {
      transactions: done.length, signatures: sigs, feesSol: (sigs * FEE) / LAMPORTS_PER_SOL,
      depositsSol: snap ? snap.lamports / LAMPORTS_PER_SOL : null, pending: b.ops.filter((o) => o.state === "pending").length,
    },
  };
}

// ---------------------------------------------------------------- HTTP

const json = (res: http.ServerResponse, code: number, body: unknown) => {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(body, (_, v) => (typeof v === "bigint" ? v.toString() : v)));
};
const body = (req: http.IncomingMessage) => new Promise<any>((resolve, reject) => {
  let s = "";
  req.on("data", (c) => { s += c; if (s.length > 1e5) reject(new Error("тело запроса слишком большое")); });
  req.on("end", () => { try { resolve(s ? JSON.parse(s) : {}); } catch { reject(new Error("тело запроса: не JSON")); } });
});
const TYPES: Record<string, string> = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2" };
const STATIC = path.join(ROOT, "docs/demo");
// Wallet balances for the screen, read in the background with everything else.
const wallets = { registrar: 0, venue: 0 };
async function readWallets() {
  const r = await opLabel.run("wallets", () => connection.getMultipleAccountsInfo([registrar.publicKey, venue.publicKey]));
  wallets.registrar = (r[0]?.lamports ?? 0) / LAMPORTS_PER_SOL;
  wallets.venue = (r[1]?.lamports ?? 0) / LAMPORTS_PER_SOL;
}
// A button press: a step of an event or an offer, run as an operation of the bond, answered when it ends.
const press = (rt: Runtime, key: string, work: () => Promise<void>) => new Promise<void>((resolve, reject) => {
  if (rt.jobs.has(key)) return reject(new Refused("этот шаг уже выполняется"));
  run(rt, key, [], async () => { try { await work(); resolve(); } catch (e) { reject(e); throw e; } });
});

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
      return json(res, 200, {
        registrar: registrar.publicKey.toBase58(), sol: wallets.registrar, venue: venue.publicKey.toBase58(), venueSol: wallets.venue, faucet: FAUCET, rpc: RPC,
        programId: program.programId.toBase58(), rpcCounts: budget.counts, rpcLimit: LIMIT,
        bonds: [...bonds.values()].map(({ b }) => ({ id: b.id, title: b.title, status: b.status, createdAt: b.createdAt })).reverse(),
      });
    }
    if (req.method === "POST" && parts[1] === "estimate") {
      const p = await body(req), errors = validate(p);
      return json(res, 200, { errors, sol: errors.length ? null : estimateSol(p) });
    }
    if (req.method === "POST" && parts[1] === "bonds" && parts.length === 2) {
      const p: Params = await body(req);
      const errors = validate(p);
      if (errors.length) return json(res, 400, { error: errors.join("; ") });
      // SOL reserved for every bond still being issued
      const need = estimateSol(p) + [...bonds.values()].filter((x) => x.b.status === "creating").reduce((s, x) => s + estimateSol(x.b.params), 0);
      if (wallets.registrar < need) return json(res, 400, { error: `на кошельке registrar ${wallets.registrar.toFixed(4)} SOL, на выпуск нужно около ${need.toFixed(3)} SOL: пополните его через ${FAUCET}` });
      const rt = await createBond(p);
      return json(res, 200, { id: rt.b.id, token: rt.b.token });
    }
    const rt = parts[1] === "bonds" ? bonds.get(parts[2]) : undefined;
    if (!rt) return json(res, 404, { error: "выпуска нет" });
    if (req.method === "GET" && parts.length === 3) return json(res, 200, view(rt, Number(url.searchParams.get("since") ?? 0)));
    if (req.method === "GET" && parts[3] === "scenario.public.json") return json(res, 200, publicScenario(rt.b));
    if (req.method === "GET" && parts[3] === "payments.json") return json(res, 200, paymentsOf(rt.b));
    if (req.method !== "POST") return json(res, 405, { error: "method" });
    const given = String(req.headers["x-operator-token"] ?? "");
    if (given.length !== rt.b.token.length || !crypto.timingSafeEqual(Buffer.from(given), Buffer.from(rt.b.token)))
      return json(res, 403, { error: "управляет выпуском тот, кто его создал (токен хранится в его браузере); остальные смотрят" });
    const input = await body(req);
    if (parts[3] === "events" && parts[5] === "auto") { rt.b.events[Number(parts[4])].auto = !!input.auto; save(rt.b); return json(res, 200, { ok: true }); }
    if (parts[3] === "events" && parts[5] === "step") {
      const e = Number(parts[4]), ev = rt.b.events[e];
      if (!ev) return json(res, 404, { error: "события нет" });
      if (ev.auto) return json(res, 400, { error: "включена авто-обработка: снимите галку, чтобы нажимать шаги" });
      if (!["open", "fund", "pay"].includes(input.step)) return json(res, 400, { error: "шаг: open, fund или pay" });
      await press(rt, `event ${e}`, () => eventStep(rt, e, input.step));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts.length === 4) {
      if (rt.b.status !== "live") return json(res, 400, { error: "выпуск не в обращении" });
      if (rt.b.offers.length >= MAX_OFFERS) return json(res, 400, { error: `не больше ${MAX_OFFERS} оферт на выпуск` });
      await press(rt, "offer new", () => addOffer(rt, input));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts[5] === "auto") { rt.b.offers[Number(parts[4]) - 1].auto = !!input.auto; save(rt.b); return json(res, 200, { ok: true }); }
    if (parts[3] === "offers" && parts[5] === "step") {
      const id = Number(parts[4]), o = rt.b.offers[id - 1];
      if (!o) return json(res, 404, { error: "оферты нет" });
      if (o.auto) return json(res, 400, { error: "включена авто-обработка: снимите галку, чтобы нажимать шаги" });
      if (!["fund", "settle"].includes(input.step)) return json(res, 400, { error: "шаг: fund или settle" });
      await press(rt, `offer ${id}`, () => offerStep(rt, id, input.step));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "audit") { runAudit(rt); return json(res, 200, { ok: true }); }
    return json(res, 404, { error: "нет такого действия" });
  } catch (e) {
    return json(res, 400, { error: reason(e) });
  }
});

// ---------------------------------------------------------------- start

fs.mkdirSync(DATA, { recursive: true });
await readClock();
rentBase = await connection.getMinimumBalanceForRentExemption(0);
rentPerByte = ((await connection.getMinimumBalanceForRentExemption(1_000)) - rentBase) / 1_000;
await readWallets();
// Bonds survive a restart: state and keys are files; an interrupted issuance continues; transactions that were
// waiting go back to the poller, and each operation continues from what the chain shows.
for (const id of fs.readdirSync(DATA)) {
  const st = path.join(dirOf(id), "state.json");
  if (!fs.existsSync(st)) continue;
  const b: BondState = JSON.parse(fs.readFileSync(st, "utf8"));
  if (!b.ops) continue; // a bond of the service before plan-refined.md: kept on disk, not loaded
  const k: Keys = JSON.parse(fs.readFileSync(path.join(dirOf(id), "keys.json"), "utf8"));
  const kp = (s: number[]) => Keypair.fromSecretKey(Uint8Array.from(s));
  b.auditRunning = false;
  const rt: Runtime = { b, keys: { bondMint: kp(k.bondMint), cashMint: kp(k.cashMint), holders: Object.fromEntries(Object.entries(k.holders).map(([n, s]) => [n, kp(s)])) }, jobs: new Map(), locked: new Set(), lastFull: 0, dirty: true, issueRetryAt: 0 };
  bonds.set(id, rt);
  for (const s of b.ops.filter((o) => o.state === "pending" && o.raw)) waiting.set(s.sig, { s, b, lastSend: 0, done: () => {} });
}
connection.onAccountChange(SYSVAR_CLOCK_PUBKEY, (info) => takeClock(info.data), { commitment: "confirmed" });
setInterval(() => readWallets().catch((e) => console.error("wallets", e)), 10_000);
setInterval(() => balanceVenue().catch((e) => console.error("venue", e)), 60_000);
balanceVenue().catch((e) => console.error("venue", e));
let polling = false;
setInterval(() => {
  if (polling) return;
  polling = true;
  poll().catch((e) => console.error("poll", e)).finally(() => { polling = false; });
}, 1_000);
// The scheduler looks once a slot (the chain's step, about 400 ms): a bond day lasts one second.
setInterval(() => { for (const rt of bonds.values()) { try { tick(rt); } catch (e) { console.error(rt.b.id, "tick", e); } } }, 400);
server.listen(PORT, "127.0.0.1", () => console.log(`operator service on 127.0.0.1:${PORT}, registrar ${registrar.publicKey.toBase58()}, venue ${venue.publicKey.toBase58()}, ${bonds.size} bonds`));
