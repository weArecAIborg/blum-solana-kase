// Operator service behind the operator screen (docs/demo/operator-screen.html, blum.caiborg.ai/operator-screen).
// The operator (a person at the bond's servicing agent) presses a few buttons: issue a bond with its terms, add a
// buyback or a tender; each corporate action known from the terms (each coupon, the redemption) runs by itself at its
// moment when its "auto" box is ticked, or step by step by its buttons. Blum, the program, services the bond: it keeps
// every holder's position at each record moment, computes every amount, pays each holder once and refuses what its
// rules forbid. The service does nothing Blum does: it reads no holder, position or balance and repeats no rule. It
// only sends transactions, each at its moment, and the node's answer to the send is all it learns: accepted (the node
// relays it) or refused by the program, with the program's reason (then nothing happened on chain). Whether each
// accepted transaction landed and what it did, the ledger-only audit reads from the ledger after the redemption.
//   - the registrar registers the generated holders BH#1..n, places the issue, pays every coupon and the redemption
//     and adds the offers; it takes no part in trades;
//   - the generated holders (a simulated market, each with its own wallet) trade among themselves through Solana DvP
//     (solana.com/docs/defi/dvp-program): a trade is one transaction the seller, the buyer and the venue (settlement
//     authority) sign; a trade a day before a record moment is two (its escrow before the moment, its settlement after
//     it). Every record moment has trades a day before it, at it and a day after it, two at each point; besides those,
//     each holder trades at random (at most T trades, propensities at the start and at the end of the bond's life).
//     Holders answer the buybacks and tenders the operator adds: some take part, some do not. Each simulated holder
//     knows only what its own wallet did (how many bonds it received and gave), as a real investor would.
// Reads, each set by the program or needed to sign: the chain's Clock as the chain pushes it (a subscription), the
// bond's chain origin and each event's budget (read once each), the registrar's balance before a bond is created, the
// holders' SOL once for its return after the redemption, a recent blockhash every 20 seconds while sending.
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
  settleDvpInstruction, swapDvpAddress,
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
const MAX_OFFERS = 10; // offers per bond
// Accounts a trade and a tender open (programs/bond_servicing/src/state.rs, admit_trade.rs): the escrow's holder record
// and the DvP nonce marker (an empty account) stay on chain; the trade record (458 bytes) and its two escrow accounts
// go to the venue at settlement or cancellation.
const HOLDER_RECORD = 467, SWAP_RECORD = 458, TENDER_RECORD = 8 + 32 + 32 + 4 + 8 + 1 + 8 + 1 + 1;
const POINTS = [-1, 0, 1] as const;
const POINT_NAMES: Record<number, string> = { [-1]: "за день до отсечки", 0: "в момент отсечки", 1: "через день после отсечки" };
// A trade a day before a record moment sends its escrow this many bond days (chain seconds) before the moment, so it
// is confirmed before it: devnet confirmed a transaction in about 2 seconds (26.10.2026, 12 issuance transactions one
// after another in 25 seconds).
const LEAD_DAYS = 2;
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const secondsOf = (hhmm: string) => { const [h, m] = hhmm.split(":").map(Number); return h * 3_600 + m * 60; };
const dayNumber = (iso: string) => Math.round(Date.parse(`${iso}T00:00:00Z`) / 1000 / DAY);
const localIso = (bondUnix: number) => new Date((bondUnix + RULES.utcOffsetHours * 3_600) * 1000).toISOString().slice(0, 16).replace("T", " ");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const chunks = <T,>(items: T[], size: number) => Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));

// ---------------------------------------------------------------- RPC: one budget for every request

// Public devnet, from one IP: 100 requests per 10 s, 40 per method, 40 connections (solana.com/docs/references/
// clusters). The service keeps under them and counts every physical request by method and operation.
const LIMIT = { windowMs: 10_000, total: 90, perMethod: 35, inFlight: 6 };
class Budget {
  private times: number[] = [];
  private methods = new Map<string, number[]>();
  private inFlight = 0;
  private until = 0;
  reserved = 0; // requests per window lent to the audit process while it runs
  counts = { total: 0, limited: 0, limitedBy: {} as Record<string, number>, method: {} as Record<string, number>, op: {} as Record<string, number> };
  async take(method: string) {
    for (;;) {
      const now = Date.now(), from = now - LIMIT.windowMs;
      while (this.times.length && this.times[0] <= from) this.times.shift();
      const m = this.methods.get(method) ?? [];
      while (m.length && m[0] <= from) m.shift();
      this.methods.set(method, m);
      const wait = Math.max(this.until - now,
        this.times.length && this.times.length >= LIMIT.total - this.reserved ? this.times[0] + LIMIT.windowMs - now : 0,
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
      throw e;
    }
    budget.done();
    if (res.status !== 429) return res;
    budget.counts.limited++;
    // which of the cluster's limits ran out (its x-ratelimit-*-remaining headers at 0 or below), for each 429
    const out = [...res.headers].filter(([k, v]) => /^x-ratelimit-.*-remaining$/.test(k) && Number(v) <= 0).map(([k]) => k.slice(12, -10)).join(",") || "none";
    budget.counts.limitedBy[`${method} ${out}`] = (budget.counts.limitedBy[`${method} ${out}`] ?? 0) + 1;
    throw new Error(`RPC ${method}: 429 (лимит ${out}, retry-after ${res.headers.get("retry-after")})`);
  }
};
const connection = new Connection(RPC, { commitment: "confirmed", fetch: budgetedFetch as any, disableRetryOnRateLimit: true });
const idl = JSON.parse(fs.readFileSync(path.join(ROOT, "ops/idl/bond_servicing.json"), "utf8"));
const program = new anchor.Program(idl, new anchor.AnchorProvider(connection, new anchor.Wallet(registrar), { commitment: "confirmed" }));
// The program's own coder: account and field names in camelCase, as the code reads them.
const coder = program.coder;
const computeLimit = () => ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 });
const pda = (...seeds: (Buffer | Uint8Array)[]) => PublicKey.findProgramAddressSync(seeds, program.programId)[0];
const u16 = (n: number) => Buffer.from(new Uint16Array([n]).buffer);

// ---------------------------------------------------------------- time and blockhash

// Chain time: the Clock sysvar as the chain pushes it every slot (an account subscription at `confirmed`, the
// commitment every transaction is simulated at before it is sent), read once at start. The program checks time
// against the Clock of the slot that executes a transaction, and that Clock does not follow a server's clock (a local
// validator's stood still for 7 slots and then jumped), so the service takes the time only from the chain: a step due
// at a moment is sent once the chain's confirmed Clock has reached it, never earlier.
let clockAt = { chain: 0, mono: 0 };
const takeClock = (data: Buffer) => {
  const chain = Number(data.readBigInt64LE(32));
  if (chain >= clockAt.chain) clockAt = { chain, mono: performance.now() };
};
const readClock = async () => takeClock((await opLabel.run("clock", () => connection.getAccountInfo(SYSVAR_CLOCK_PUBKEY)))!.data);
const chainNow = () => clockAt.chain;
const clockAge = () => Math.round((performance.now() - clockAt.mono) / 1000);

// One blockhash for every transaction, renewed every 20 seconds (it lives about 60 to 90: solana.com/docs/advanced/
// confirmation), fetched at `confirmed`; a send the node refuses with "Blockhash not found" renews it.
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

// ---------------------------------------------------------------- sending: the node's answer is all the service learns

type Sent = { sig: string; label: string; chain: number; bond: number | null; signatures: number; confirmed: boolean; slot?: number };
// The program (or a token program) refused the transaction at the node's simulation: nothing changed on chain. `ix` is
// the instruction it refused, `code` the program's error name.
class Refused extends Error { constructor(message: string, readonly ix?: number, readonly code?: string) { super(message); } }
const refusal = (e: any) => {
  const text = [String(e?.message ?? e), ...(e?.logs ?? e?.transactionLogs ?? [])].join("\n");
  const found = /Error Message: ([^"\n]*)/.exec(text) ?? /custom program error: (0x[0-9a-f]+)/.exec(text) ?? /Message: ([^\n]*)/.exec(text);
  const ix = /Error processing Instruction (\d+)/.exec(text), code = /Error Code: (\w+)/.exec(text);
  return { text, message: (found?.[1] ?? text.split("\n")[0]).trim().slice(0, 300), ix: ix ? Number(ix[1]) : undefined, code: code?.[1] };
};
const reason = (e: any) => (e instanceof Refused ? e.message : refusal(e).message);
// The node pushes a signature's status once it is confirmed (signatureSubscribe: solana.com/docs/rpc/websocket), so a
// step that depends on the previous one is sent only after it; a transaction not confirmed while its blockhash lives
// (60 to 90 seconds: solana.com/docs/advanced/confirmation) or confirmed with an error is an error.
const SUBSCRIPTIONS = 8; // waits at once: the cluster allows 10 subscriptions (x-ratelimit-pubsub-limit), one is the Clock
let waitingNow = 0;
// The transaction is sent only once the node has acknowledged the subscription ("subscribed"), so its confirmation
// cannot come before the node listens; after a reconnection the subscription is acknowledged again and the status is
// read once, since a confirmation in between was not pushed (web3.js confirms the same way: connection.ts,
// getTransactionConfirmationPromise, through the same subscription-state hook).
const confirmation = (sig: string) => {
  let id = 0, timer: NodeJS.Timeout, dispose = () => {}, subscribed = false, ready = () => {};
  const listening = new Promise<void>((r) => { ready = r; });
  const done = new Promise<number>((resolve, reject) => {
    timer = setTimeout(() => { dispose(); void connection.removeSignatureListener(id); reject(new Error(`транзакция ${sig} не подтверждена за 90 с`)); }, 90_000);
    const settle = (err: unknown, slot: number) => { clearTimeout(timer); dispose(); err ? reject(new Error(`транзакция ${sig} исполнена с ошибкой ${JSON.stringify(err)}`)) : resolve(slot); };
    id = connection.onSignature(sig, (r, ctx) => settle(r.err, ctx.slot), "confirmed");
    dispose = (connection as any)._onSubscriptionStateChange(id, (state: string) => {
      if (state !== "subscribed") return;
      if (!subscribed) { subscribed = true; ready(); return; }
      opLabel.run("confirm", () => connection.getSignatureStatus(sig)).then(({ value }) => {
        if (value && (value.confirmationStatus === "confirmed" || value.confirmationStatus === "finalized")) { void connection.removeSignatureListener(id); settle(value.err, value.slot); }
      }, (e) => { clearTimeout(timer); dispose(); reject(e); });
    });
  });
  return { listening: Promise.race([listening, done.then(() => {})]), done, cancel: () => { clearTimeout(timer); dispose(); void connection.removeSignatureListener(id); } };
};

async function send(b: BondState | undefined, label: string, build: () => Promise<{ ixs: TransactionInstruction[]; signers: Keypair[] }>): Promise<Sent> {
  return opLabel.run(label, async () => {
    if (b?.status === "error") throw new Error(`выпуск остановлен: ${b.error}`);
    while (waitingNow >= SUBSCRIPTIONS) await sleep(50);
    waitingNow++;
    try {
      const { ixs, signers } = await build();
      const { blockhash: hash, lastValidBlockHeight } = await blockhash();
      const tx = new Transaction({ feePayer: signers[0].publicKey, blockhash: hash, lastValidBlockHeight }).add(...ixs);
      tx.sign(...signers);
      const sig = anchor.utils.bytes.bs58.encode(tx.signature!);
      // subscribed and acknowledged before sending, so the confirmation cannot come first
      const wait = confirmation(sig);
      await wait.listening;
      try {
        // the node simulates it first (preflight at `confirmed`) and, once accepted, relays it
        await connection.sendRawTransaction(tx.serialize(), { skipPreflight: false, preflightCommitment: "confirmed" });
      } catch (e) {
        wait.cancel();
        const r = refusal(e);
        throw new Refused(r.message + (r.code && !r.message.includes(r.code) ? ` (${r.code})` : ""), r.ix, r.code);
      }
      const s: Sent = { sig, label, chain: chainNow(), bond: b && b.chainOrigin !== undefined ? bondNow(b) : null, signatures: tx.signatures.length, confirmed: false };
      if (b) { b.ops.push(s); save(b); }
      s.slot = await wait.done;
      s.confirmed = true;
      if (b) save(b);
      return s;
    } finally { waitingNow--; }
  });
}
// A step sent once (the label names it): after a restart its journal entry stands for it.
const sentOnce = (b: BondState, label: string) => b.ops.find((o) => o.label === label);
// How many of `items` fit one legacy transaction with `head` (1232 bytes: solana.com/docs/core/transactions/
// transaction-structure), at most `max`.
function pack<T>(head: TransactionInstruction[], items: T[], ixOf: (x: T) => TransactionInstruction, signers: number, payer: PublicKey, max: number) {
  let n = Math.min(items.length, max);
  for (; n > 0; n--) if (txSize([...head, ...items.slice(0, n).map(ixOf)], signers, payer) <= 1232) break;
  return n;
}
const txSize = (ixs: TransactionInstruction[], signers: number, payer: PublicKey) =>
  new Transaction({ feePayer: payer, blockhash: PublicKey.default.toBase58(), lastValidBlockHeight: 0 }).add(...ixs).serializeMessage().length + 1 + 64 * signers;

// ---------------------------------------------------------------- state

type Params = {
  issue: string; years: number; periodsPerYear: number; face: number; ratePct: number; redemptionPct: number;
  holders: number; issueSize: number; maxTradesPerHolder: number; propensityStart: number; propensityEnd: number;
};
// Each payment the service sent and the program accepted; `resolved` holders the program answered "nothing to pay" or
// "already paid", `refused` holders it refused for another reason.
type EventState = {
  index: number; kind: "coupon" | "redemption"; auto: boolean; recordTime: number; paymentTime: number;
  openSig?: string; fundSig?: string; budget?: string; supplyAtRecord?: number;
  paid: Record<string, { sig: string; at: number }>; resolved: Record<string, string>; done: boolean; error?: string; retryAt?: number;
};
type OfferState = {
  id: number; kind: "buyback" | "tender"; priceUnits: string; maxQuantity: number; open: number; close: number; auto: boolean;
  createSig?: string; fundSig?: string; funded: boolean; done: boolean; error?: string; retryAt?: number;
  participants: string[]; others: string[]; plans: { holder: string; at: number; share: number }[];
  actions: { holder: string; sell?: number; tender?: number; sig?: string; at: number; refused?: string }[];
  settlements: { holder: string; sig?: string; at: number; refused?: string }[];
};
// A trade as its holders sent it. Stages: planned; legs (a trade a day before a record moment: escrow sent, settlement
// due); done (settled in one transaction, or settled after its legs); cancelled (legs sent, settlement refused, both
// legs returned); refused (the program refused it: nothing happened); missed (no holder could take part).
type TradeState = {
  id: number; kind: "random" | "mandatory"; k?: number; point?: number; at?: number; from?: string; to?: string; amount?: number; cash?: string;
  nonce?: string; swap?: string; stage: "planned" | "legs" | "done" | "cancelled" | "refused" | "missed";
  legsSig?: string; sig?: string; sentAt?: number; settledAt?: number; note?: string; retryAt?: number;
};
type LedgerRow = { n: number; chain: number; bond: number; text: string; sig?: string; kind: string };
type BondState = {
  id: string; createdAt: string; params: Params; title: string; status: "creating" | "live" | "done" | "error"; error?: string; token: string;
  bondMint: string; cashMint: string; bond: string; holders: { name: string; pubkey: string }[]; placements: Record<string, number>;
  cashTarget: Record<string, string>; solTarget: number;
  bondOrigin: number; chainOrigin?: number; issueTime: number; maturityTime: number;
  schedule: { payment_days: number[]; payment_dates: string[]; record_days: number[]; record_dates: string[] };
  recordTimes: number[]; events: EventState[]; offers: OfferState[]; trades: TradeState[]; ledger: LedgerRow[]; ops: Sent[];
  // what each simulated holder's own wallet did: bonds it may still offer (received minus given, tendered ones given)
  wallet: Record<string, number>; cash: Record<string, string>; reserve: Record<string, number>; decidedDay: number; rng: number; endedAt?: number;
  audit?: any; auditError?: string; auditRunning?: boolean; swept?: boolean;
};
type Keys = { bondMint: number[]; cashMint: number[]; holders: Record<string, number[]> };
type Runtime = {
  b: BondState; keys: { bondMint: Keypair; cashMint: Keypair; holders: Record<string, Keypair> };
  jobs: Map<string, Promise<void>>; locked: Set<string>; issueRetryAt: number;
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

// ---------------------------------------------------------------- accounts of one bond (derived, not read)

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
// An account the program created, read once (after the transaction that created it is confirmed) when the service
// needs what the program put in it.
async function readOnce(label: string, address: PublicKey) {
  const info = await opLabel.run(label, () => connection.getAccountInfo(address, "confirmed"));
  if (!info) throw new Error(`${label}: ${address.toBase58()} не найден после подтверждения`);
  return info;
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
  int(p.holders, 20, 20, "держателей");
  int(p.issueSize, p.holders, 100_000, "облигаций в выпуске (не меньше числа держателей)");
  int(p.maxTradesPerHolder, 1, 50, "сделок на держателя за срок");
  for (const [v, what] of [[p.propensityStart, "склонность к сделкам в начале"], [p.propensityEnd, "склонность к сделкам в конце"]] as const)
    if (!(v >= 0 && v <= 1)) errors.push(`${what}: от 0 до 1`);
  const maturityYear = Number(p.issue.slice(0, 4)) + p.years;
  if (Number(p.issue.slice(0, 4)) < 2024 || maturityYear > 2040) errors.push("годы выпуска и погашения: 2024-2040 (календарь праздников)");
  // Every record moment but the final one has 6 trades of 2 holders each: 12 participations each within H x T.
  const records = p.years * p.periodsPerYear - 1;
  if (Number.isInteger(p.holders) && Number.isInteger(p.maxTradesPerHolder) && p.holders * p.maxTradesPerHolder < 12 * records)
    errors.push(`сделок на держателя за срок: нужно не меньше ${Math.ceil((12 * records) / p.holders)} (у каждой отсечки, кроме финальной, 6 обязательных сделок по 2 участника)`);
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
    // The bond's clock starts at 12:00 Almaty three days before the issue date: issuance (one transaction after another,
    // seconds) ends long before the first record moment, a month or more after the issue date.
    bondOrigin: dayNumber(p.issue) * DAY - 3 * DAY + 12 * 3_600 - RULES.utcOffsetHours * 3_600,
    issueTime: dayNumber(p.issue) * DAY, maturityTime: dayNumber(maturity) * DAY, schedule,
    recordTimes: schedule.record_days.map((d: number) => d * DAY + secondsOf(RULES.recordTimeLocal) - RULES.utcOffsetHours * 3_600),
    events: [], offers: [], trades: [], ledger: [], ops: [], wallet: {}, cash: {}, reserve: {}, decidedDay: 0, rng: crypto.randomBytes(4).readUInt32LE(),
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
    b.events.push({ index: e, kind: e < periods ? "coupon" : "redemption", auto: true, recordTime: b.recordTimes[k], paymentTime: paymentTime(k), paid: {}, resolved: {}, done: false });
  }
  // Mandatory trades at every record moment but the final one (the program closes transfers from it): per
  // participation a holder's quota is reserved, spread evenly.
  const participations = 12 * (periods - 1);
  hs.forEach((h, i) => { b.reserve[h] = Math.floor(participations / hs.length) + (i < participations % hs.length ? 1 : 0); });
  for (let k = 0; k < periods - 1; k++) for (const point of POINTS) for (let j = 0; j < 2; j++)
    b.trades.push({ id: b.trades.length, kind: "mandatory", k, point, stage: "planned" });
  fs.mkdirSync(dirOf(id), { recursive: true });
  const keys: Keys = { bondMint: [...mintKp.secretKey], cashMint: [...cashKp.secretKey], holders: Object.fromEntries(Object.entries(holderKps).map(([n, kp]) => [n, [...kp.secretKey]])) };
  fs.writeFileSync(path.join(dirOf(id), "keys.json"), JSON.stringify(keys), { mode: 0o600 });
  save(b);
  const rt: Runtime = { b, keys: { bondMint: mintKp, cashMint: cashKp, holders: holderKps }, jobs: new Map(), locked: new Set(), issueRetryAt: 0 };
  bonds.set(id, rt);
  return rt;
}

// Issuance, one transaction after another, each confirmed before the next: the bond's own test cash token; create_bond;
// holders registered and the issue placed, three per transaction; SOL and cash for the holders' own wallets.
async function issue(rt: Runtime) {
  const { b, keys } = rt;
  const a = accountsOf(rt), p = b.params, hs = names(b);
  const step = async (label: string, build: () => Promise<{ ixs: TransactionInstruction[]; signers: Keypair[] }>, text: string) => {
    const s = await send(b, label, build);
    note(b, "setup", text, s.sig);
  };
  await step("issue:cash token", async () => ({
    ixs: [
      SystemProgram.createAccount({ fromPubkey: registrar.publicKey, newAccountPubkey: a.cashMint, space: MINT_SIZE, lamports: rent(MINT_SIZE), programId: TOKEN_PROGRAM_ID }),
      createInitializeMint2Instruction(a.cashMint, CASH_DECIMALS, bank.publicKey, bank.publicKey, TOKEN_PROGRAM_ID),
    ],
    signers: [registrar, keys.cashMint],
  }), "тестовый cash-токен выпуска (банк: mint и freeze authority)");
  const terms = {
    faceUnits: new BN((BigInt(p.face) * UNIT).toString()), rateBps: Math.round(p.ratePct * 100),
    basis: { fixedPerPeriod: { periodsPerYear: p.periodsPerYear } }, redemptionPriceBps: p.redemptionPct * 100,
    issueDay: dayNumber(p.issue), maturityDay: Math.round(b.maturityTime / DAY), monthsPerPeriod: 12 / p.periodsPerYear,
    paymentDays: b.schedule.payment_days, recordDays: b.schedule.record_days,
    recordSecondsLocal: secondsOf(RULES.recordTimeLocal), paymentSecondsLocal: secondsOf(RULES.paymentOpensLocal),
    utcOffsetSeconds: RULES.utcOffsetHours * 3_600, weekendDays: RULES.weekend.reduce((m, d) => m | (1 << WEEKDAYS.indexOf(d)), 0),
    bondOrigin: new BN(b.bondOrigin), chainSecondsPerBondDay: new BN(1), maxSupply: new BN(p.issueSize),
  };
  await step("issue:create_bond", async () => ({
    ixs: [computeLimit(), await program.methods.createBond(terms).accountsPartial({
      registrar: registrar.publicKey, mint: a.mint, bond: a.bond, cashMint: a.cashMint,
      extraAccountMetaList: pda(Buffer.from("extra-account-metas"), a.mint.toBuffer()),
      tokenProgram: TOKEN_2022_PROGRAM_ID, systemProgram: SystemProgram.programId,
    }).instruction()],
    signers: [registrar, keys.bondMint],
  }), `create_bond: ${p.face} номинал, ${p.ratePct}% годовых, ${p.periodsPerYear} купона в год, ${b.schedule.payment_dates[0]} - ${b.schedule.payment_dates[b.schedule.payment_dates.length - 1]}, выпуск до ${p.issueSize}`);
  // The bond's clock starts at create_bond's own Clock: the program keeps it in the bond (read once).
  if (b.chainOrigin === undefined) {
    b.chainOrigin = Number((coder.accounts.decode("bond", (await readOnce("issue", a.bond)).data) as any).chainOrigin);
    save(b);
  }
  for (const [i, batch] of chunks(hs, 3).entries()) {
    await step(`issue:register ${i}`, async () => {
        const ixs: TransactionInstruction[] = [computeLimit()];
        for (const h of batch) {
          // every account given, so Anchor fetches none to derive it
          ixs.push(await program.methods.registerHolder(a.owner(h)).accountsPartial({ registrar: registrar.publicKey, bond: a.bond, holder: a.record(h), systemProgram: SystemProgram.programId }).instruction());
          ixs.push(
            createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.bondsAta(h), a.owner(h), a.mint, TOKEN_2022_PROGRAM_ID),
            createAssociatedTokenAccountIdempotentInstruction(registrar.publicKey, a.cashAta(h), a.owner(h), a.cashMint, TOKEN_PROGRAM_ID),
            await program.methods.place(new BN(b.placements[h])).accountsPartial({ registrar: registrar.publicKey, bond: a.bond, mint: a.mint, holder: a.record(h), holderBonds: a.bondsAta(h), tokenProgram: TOKEN_2022_PROGRAM_ID }).instruction(),
          );
        }
        return { ixs, signers: [registrar] };
    }, `register_holder, place: ${batch.map((h) => `${h} ${b.placements[h]}`).join(", ")}`);
    for (const h of batch) b.wallet[h] = b.placements[h];
  }
  // Investors with wallets of their own: SOL for their fees and deposits, cash for their purchases (simulated deposit).
  for (const [i, batch] of chunks(hs, 8).entries())
    await step(`issue:fund ${i}`, async () => ({
      ixs: batch.flatMap((h) => [
        SystemProgram.transfer({ fromPubkey: registrar.publicKey, toPubkey: a.owner(h), lamports: Math.round(b.solTarget * LAMPORTS_PER_SOL) }),
        createMintToInstruction(a.cashMint, a.cashAta(h), bank.publicKey, BigInt(b.cashTarget[h]), [], TOKEN_PROGRAM_ID),
      ]),
      signers: [registrar, bank],
    }), `симуляция: у держателей свои кошельки: SOL на комиссии и депозиты, деньги на покупки (${batch[0]}..${batch[batch.length - 1]})`);
  for (const h of hs) b.cash[h] = b.cashTarget[h];
  b.status = "live";
  b.error = undefined;
  note(b, "setup", `выпуск в обращении: ${p.holders} держателей, ${b.trades.length} обязательных сделок у ${b.recordTimes.length} отсечек`);
}

// ---------------------------------------------------------------- corporate actions

// One step of a coupon or the redemption: open (from the record moment); fund (the budget the program computed at
// opening, read once from the event, and confirm_funding, with the first payments once the payment time has come); pay
// every holder in packed transactions. Who is owed what the program decides: it pays each holder its amount once and
// answers "nothing to pay" or "already paid" for the others, which the service then leaves out.
async function eventStep(rt: Runtime, e: number, step: "open" | "fund" | "pay") {
  const { b } = rt;
  const a = accountsOf(rt), ev = b.events[e], label = ev.kind === "coupon" ? `купон ${e + 1}` : "погашение";
  const event = a.event(e), vault = a.vault(event);
  if (step === "open") {
    if (bondNow(b) < ev.recordTime) throw new Refused(`${label}: момент отсечки ${localIso(ev.recordTime)} ещё не наступил`);
    const s = await send(b, `event:${e} open`, async () => ({ ixs: [await program.methods.openEvent(e).accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()], signers: [registrar] }));
    ev.openSig = s.sig;
    note(b, "event", `open_event, ${label}`, ev.openSig);
    return;
  }
  const pay = (h: string) => ev.kind === "coupon"
    ? program.methods.payCoupon().accountsPartial({ payer: registrar.publicKey, bond: a.bond, event, holder: a.record(h), owner: a.owner(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction()
    : program.methods.redeemHolder().accountsPartial({ payer: registrar.publicKey, bond: a.bond, mint: a.mint, event, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault, cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction();
  const max = ev.kind === "coupon" ? 6 : 4; // measured compute: 6 coupon payments or 4 redemptions fit 1,400,000 CU
  // every holder holds at least one bond at every moment (a seller keeps one), so every holder is owed every payment
  const open = () => names(b).filter((h) => !ev.paid[h]);
  // Sends `head` + payments of `who` (the funding with the first payments, or a batch of payments), confirmed.
  const payBatch = async (labelOf: string, head: TransactionInstruction[], signers: Keypair[], who: string[]) => {
    const built = await Promise.all(who.map(pay));
    const s = await send(b, labelOf, async () => ({ ixs: [...head, ...built], signers }));
    for (const h of who) ev.paid[h] = { sig: s.sig, at: bondNow(b) };
    if (who.length) note(b, "pay", `${ev.kind === "coupon" ? `pay_coupon ${e + 1}` : "redeem_holder"}: ${who.join(", ")}`, s.sig);
    return s;
  };
  if (step === "fund") {
    if (!ev.openSig) throw new Refused(`${label}: сначала открыть`);
    if (!ev.budget) {
      const opened: any = coder.accounts.decode("event", (await readOnce("event", event)).data);
      ev.budget = opened.budget.toString(); ev.supplyAtRecord = Number(opened.supplyAtRecord);
    }
    const head = [computeLimit(), createMintToInstruction(a.cashMint, vault, bank.publicKey, BigInt(ev.budget!), [], TOKEN_PROGRAM_ID), await program.methods.confirmFunding().accountsPartial({ event, vault }).instruction()];
    let first: string[] = [];
    if (bondNow(b) >= ev.paymentTime) {
      const who = open(), ixs = await Promise.all(who.map(pay));
      first = who.slice(0, pack(head, ixs, (x) => x, 2, registrar.publicKey, max));
    }
    ev.fundSig = (await payBatch(`event:${e} fund`, head, [registrar, bank], first)).sig;
    note(b, "event", `эмитент вносит бюджет и confirm_funding, ${label}: ${fmtCash(ev.budget!)} (на отсечку ${ev.supplyAtRecord} облигаций)`, ev.fundSig);
    return;
  }
  if (!ev.fundSig) throw new Refused(`${label}: сначала внести бюджет`);
  if (bondNow(b) < ev.paymentTime) throw new Refused(`${label}: выплаты с ${localIso(ev.paymentTime)}`);
  const due = open();
  const batches: string[][] = [];
  for (let rest = due.slice(); rest.length; ) {
    const ixs = await Promise.all(rest.map(pay));
    const n = Math.max(1, pack([computeLimit()], ixs, (x) => x, 1, registrar.publicKey, max));
    batches.push(rest.splice(0, n));
  }
  // the batches of different holders go at once
  await Promise.all(batches.map((who) => payBatch(`event:${e} pay`, [computeLimit()], [registrar], who)));
  ev.done = true;
  note(b, "event", ev.kind === "coupon" ? `купон ${e + 1} выплачен всем держателям` : "погашение завершено");
}
const nextEventStep = (ev: EventState) => (!ev.openSig ? "open" : !ev.fundSig ? "fund" : !ev.done ? "pay" : null);

// ---------------------------------------------------------------- offers

type OfferInput = { kind: "buyback" | "tender"; price: number; maxQuantity: number; open: string; close: string };
const offerWindow = (o: OfferInput) => ({ open: dayNumber(o.open) * DAY + secondsOf("09:00") - RULES.utcOffsetHours * 3_600, close: dayNumber(o.close) * DAY + secondsOf("17:00") - RULES.utcOffsetHours * 3_600 });
// The operator's input, checked before anything is sent (a wrong input is answered, the bond goes on).
const offerErrors = (b: BondState, o: OfferInput) => {
  const { open, close } = offerWindow(o), errors: string[] = [];
  if (!(o.kind === "buyback" || o.kind === "tender")) errors.push("вид оферты: buyback или tender");
  if (!(Number.isInteger(o.maxQuantity) && o.maxQuantity > 0 && o.maxQuantity <= b.params.issueSize)) errors.push(`объём: целое от 1 до ${b.params.issueSize}`);
  if (!(o.price > 0 && o.price <= 1e9) || Math.round(o.price * 1e6) !== o.price * 1e6) errors.push("цена за облигацию: больше 0, до 6 знаков");
  if (!(open < close)) errors.push("окно: дата открытия раньше даты закрытия");
  if (close <= bondNow(b)) errors.push("окно уже закрылось");
  if (close >= b.recordTimes[b.recordTimes.length - 1]) errors.push(`окно должно закрыться до последней отсечки ${localIso(b.recordTimes[b.recordTimes.length - 1])}`);
  if (names(b).filter((h) => (b.wallet[h] ?? 0) > 1).length < 2) errors.push("облигации сверх одной есть меньше чем у двух держателей");
  return errors;
};
async function addOffer(rt: Runtime, o: OfferInput) {
  const { b } = rt;
  const a = accountsOf(rt);
  const { open, close } = offerWindow(o), now = bondNow(b);
  // Some holders take part, some do not: both groups non-empty among the holders whose wallets hold bonds beyond one.
  const withBonds = names(b).filter((h) => (b.wallet[h] ?? 0) > 1);
  for (let i = withBonds.length - 1; i > 0; i--) { const j = Math.floor(random(b) * (i + 1)); [withBonds[i], withBonds[j]] = [withBonds[j], withBonds[i]]; }
  const count = 1 + Math.floor(random(b) * (withBonds.length - 1));
  const id = b.offers.length + 1, priceUnits = BigInt(Math.round(o.price * 1e6)).toString();
  const offer = a.offer(id), vault = a.vault(offer);
  // create_offer, the bank's deposit of its budget and confirm_offer_funding in one transaction (the program refuses
  // an offer closing after the final record moment: InvalidOffer)
  const s = await send(b, `offer:create ${id}`, async () => ({
    ixs: [
      await program.methods.createOffer(id, o.kind === "tender", new BN(priceUnits), new BN(o.maxQuantity), new BN(open), new BN(close)).accountsPartial({ registrar: registrar.publicKey, bond: a.bond, offer, cashMint: a.cashMint, vault, cashTokenProgram: TOKEN_PROGRAM_ID }).instruction(),
      createMintToInstruction(a.cashMint, vault, bank.publicKey, BigInt(priceUnits) * BigInt(o.maxQuantity), [], TOKEN_PROGRAM_ID),
      await program.methods.confirmOfferFunding().accountsPartial({ offer, vault }).instruction(),
    ],
    signers: [registrar, bank],
  }));
  const from = Math.max(open, now);
  const participants = withBonds.slice(0, count);
  b.offers.push({
    id, kind: o.kind, priceUnits, maxQuantity: o.maxQuantity, open, close, auto: true, createSig: s.sig, fundSig: s.sig, funded: true, done: o.kind === "buyback",
    participants, others: withBonds.slice(count), plans: participants.map((holder) => ({ holder, at: from + Math.floor(random(b) * 0.9 * (close - from)), share: random(b) })),
    actions: [], settlements: [],
  });
  note(b, "offer", `create_offer ${id} (${o.kind === "tender" ? "tender, pro rata" : "buyback, first come"}) и бюджет ${fmtCash(BigInt(priceUnits) * BigInt(o.maxQuantity))}: ${fmtCash(priceUnits)} за облигацию, до ${o.maxQuantity}, окно ${localIso(open)} - ${localIso(close)}; участвуют ${count} из ${withBonds.length} держателей`, s.sig);
}
// After a tender's window: settle_tender for each tender the holders sent, three per transaction, in the order the
// tenders were confirmed (they go one at a time), which is the order the program numbered them and resolves them in.
async function settleTender(rt: Runtime, id: number) {
  const { b } = rt;
  const a = accountsOf(rt), o = b.offers[id - 1], offer = a.offer(id);
  if (o.kind !== "tender") throw new Refused("рассчитывается только tender");
  if (bondNow(b) < o.close) throw new Refused(`окно открыто до ${localIso(o.close)}`);
  const settled = new Set(o.settlements.map((x) => x.holder));
  let rest = [...new Set(o.actions.filter((x) => x.tender && x.sig).map((x) => x.holder))].filter((h) => !settled.has(h));
  const ixOf = async (h: string) => program.methods.settleTender().accountsPartial({ retirement: retirement(rt, id, h, registrar.publicKey), offer, tender: a.tender(id, h) }).instruction();
  while (rest.length) {
    const ixs = await Promise.all(rest.map(ixOf));
    const batch = rest.slice(0, Math.max(1, pack([computeLimit()], ixs, (x) => x, 1, registrar.publicKey, 3)));
    const s = await send(b, `offer:settle ${id}`, async () => ({ ixs: [computeLimit(), ...await Promise.all(batch.map(ixOf))], signers: [registrar] }));
    for (const h of batch) o.settlements.push({ holder: h, sig: s.sig, at: bondNow(b) });
    note(b, "offer", `settle_tender ${id}: ${batch.join(", ")}`, s.sig);
    rest = rest.filter((h) => !batch.includes(h));
  }
  o.done = true;
  note(b, "offer", `оферта ${id}: заявки рассчитаны`);
}
const retirement = (rt: Runtime, id: number, h: string, payer: PublicKey) => {
  const a = accountsOf(rt);
  return {
    payer, bond: a.bond, mint: a.mint, holder: a.record(h), owner: a.owner(h), holderBonds: a.bondsAta(h), vault: a.vault(a.offer(id)),
    cashMint: a.cashMint, holderCash: a.cashAta(h), memoProgram: null, tokenProgram: TOKEN_2022_PROGRAM_ID, cashTokenProgram: TOKEN_PROGRAM_ID,
  };
};
// A holder answers an offer in its own transaction (it signs and pays its fee and its tender record's deposit): sells
// into a buyback or tenders, a random share of the bonds its wallet holds. The program refuses more than the holder's
// unlocked bonds or what the buyback still takes.
async function answerOffer(rt: Runtime, o: OfferState, plan: OfferState["plans"][number]) {
  const { b, keys } = rt;
  const a = accountsOf(rt);
  const have = b.wallet[plan.holder] ?? 0;
  // deal_possible: the holder offers only bonds its wallet holds beyond what it has already committed, keeping one; a
  // buyback takes no more than what is left of it. Taken from the wallet before anything is sent.
  const left = o.kind === "buyback" ? o.maxQuantity - o.actions.reduce((s, x) => s + (x.sell ?? 0), 0) : Infinity;
  const q = Math.min(Math.max(1, Math.round(plan.share * (have - 1))), have - 1, left);
  if (q < 1) { note(b, "offer", `оферта ${o.id}: ${plan.holder} не отвечает: ${left < 1 ? "объём выкупа исчерпан" : "нет облигаций сверх одной"}`); return; }
  b.wallet[plan.holder] = have - q;
  const holder = keys.holders[plan.holder], owner = holder.publicKey, offer = a.offer(o.id);
  {
    const s = await send(b, `offer:answer ${o.id}`, async () => ({
      ixs: [o.kind === "buyback"
        ? await program.methods.sellToOffer(new BN(q)).accountsPartial({ retirement: retirement(rt, o.id, plan.holder, owner), seller: owner, offer }).instruction()
        : await program.methods.changeTender(new BN(q), false).accountsPartial({ owner, bond: a.bond, offer, holder: a.record(plan.holder), tender: a.tender(o.id, plan.holder) }).instruction()],
      signers: [holder],
    }));
    o.actions.push({ holder: plan.holder, ...(o.kind === "buyback" ? { sell: q } : { tender: q }), sig: s.sig, at: bondNow(b) });
    note(b, "offer", o.kind === "buyback" ? `sell_to_offer ${o.id}: ${plan.holder} продаёт ${q}` : `change_tender ${o.id}: ${plan.holder} заявляет ${q}`, s.sig);
  }
}

// ---------------------------------------------------------------- trades (Solana DvP)

// Participations: trades sent and not refused count against a holder's limit T; mandatory trades draw on the holder's
// reserve, random trades on what is left beyond it.
const finished = (t: TradeState) => ["done", "cancelled", "refused", "missed"].includes(t.stage);
const used = (b: BondState, h: string) => b.trades.filter((t) => (t.from === h || t.to === h) && !["planned", "refused", "missed"].includes(t.stage)).length;
const randomRoom = (b: BondState, h: string) => {
  const queued = b.trades.filter((t) => t.kind === "random" && t.stage === "planned" && (t.from === h || t.to === h)).length;
  return b.params.maxTradesPerHolder - used(b, h) - queued - (b.reserve[h] ?? 0) > 0;
};
const tradeTag = (t: TradeState) => (t.kind === "mandatory" ? ` [отсечка ${t.k! + 1}, ${POINT_NAMES[t.point!]}]` : "");

// A trade: the seller records it (naming the venue settlement authority), admits its escrow and delivers its bonds; the
// buyer pays into the escrow; the venue settles: one transaction the three sign. A trade a day before a record moment
// sends its legs first and its settlement at the moment (refused at the final record moment, then cancelled).
async function tradeStep(rt: Runtime, t: TradeState) {
  const { b, keys } = rt;
  const a = accountsOf(rt), tag = tradeTag(t), hs = names(b);
  if (t.stage === "planned") {
    // deal_possible, against everything the holder has already committed: a seller sells only bonds its wallet holds
    // beyond those its earlier trades and answers took, and keeps one (so every holder is owed every coupon and the
    // redemption); a buyer pays only cash its wallet holds beyond its earlier purchases. What a trade takes is taken
    // from the wallet here, before anything is sent.
    const sellable = (h: string) => (b.wallet[h] ?? 0) - 1;
    let from = t.from, to = t.to;
    if (t.kind === "mandatory") {
      const idle = (h: string) => !rt.locked.has(h);
      const reserved = (h: string) => (b.reserve[h] ?? 0) > 0;
      const sellers = [...hs.filter((h) => idle(h) && sellable(h) >= 1 && reserved(h)), ...hs.filter((h) => idle(h) && sellable(h) >= 1 && !reserved(h) && randomRoom(b, h))];
      from = sellers[Math.floor(random(b) * Math.min(sellers.length, 3))];
      const buyers = [...hs.filter((h) => h !== from && idle(h) && reserved(h)), ...hs.filter((h) => h !== from && idle(h) && !reserved(h) && randomRoom(b, h))];
      to = buyers[Math.floor(random(b) * Math.min(buyers.length, 3))];
      if (!from || !to) { t.stage = "missed"; t.note = "нет держателя с облигациями и лимитом сделок"; note(b, "trade", `сделка пропущена${tag}: ${t.note}`); return; }
      for (const h of [from, to]) if (reserved(h)) b.reserve[h] -= 1;
    }
    if (sellable(from!) < 1) { t.stage = "missed"; t.note = `у ${from} нет облигаций сверх одной`; return; }
    const amount = 1 + Math.floor(random(b) * Math.min(sellable(from!), Math.max(1, Math.floor(b.params.issueSize / b.params.holders))));
    // price per bond within 2% of face
    const cash = (BigInt(b.params.face) * UNIT * BigInt(Math.round((0.98 + 0.04 * random(b)) * 10_000)) / 10_000n) * BigInt(amount);
    if (BigInt(b.cash[to!] ?? "0") < cash) { t.stage = "missed"; t.note = `у ${to} не хватает денег`; return; }
    t.from = from; t.to = to; t.amount = amount; t.cash = cash.toString();
    b.wallet[from!] -= amount;
    b.cash[to!] = (BigInt(b.cash[to!]) - cash).toString();
    t.nonce = randomNonce().toString();
    t.swap = swapDvpAddress(venue.publicKey, a.owner(from!), a.owner(to!), a.mint, a.cashMint, BigInt(t.nonce)).toBase58();
    save(b);
  }
  const seller = keys.holders[t.from!], buyer = keys.holders[t.to!];
  const dvp = { swap: new PublicKey(t.swap!), nonce: BigInt(t.nonce!), seller: seller.publicKey, buyer: buyer.publicKey, quantity: BigInt(t.amount!), cash: BigInt(t.cash!) };
  const legs = async () => {
    const { ix: create } = createDvpInstruction(seller.publicKey, venue.publicKey, seller.publicKey, buyer.publicKey, a.mint, a.cashMint, TOKEN_PROGRAM_ID, dvp.quantity, dvp.cash, dvp.nonce, BigInt(chainNow() + DAY));
    const admit = await admitTradeInstruction(program, seller.publicKey, a.bond, a.mint, dvp.swap, seller.publicKey, buyer.publicKey);
    return [create, admit, fundBondLegInstruction(program.programId, a.mint, seller.publicKey, dvp.swap, dvp.quantity), fundCashLegInstruction(a.cashMint, TOKEN_PROGRAM_ID, CASH_DECIMALS, buyer.publicKey, dvp.swap, dvp.cash)];
  };
  const settle = () => settleDvpInstruction(program.programId, venue.publicKey, dvp, a.mint, a.cashMint, TOKEN_PROGRAM_ID);
  // each send returns once the transaction is confirmed; the buyer's bonds and the seller's cash count from then on
  const settled = (sig: string) => {
    t.sig = sig; t.stage = "done"; t.settledAt = bondNow(b);
    b.wallet[t.to!] = (b.wallet[t.to!] ?? 0) + t.amount!;
    b.cash[t.from!] = (BigInt(b.cash[t.from!] ?? "0") + BigInt(t.cash!)).toString();
  };
  if (t.stage === "planned") {
    t.sentAt = bondNow(b);
    if (t.point === -1) {
      const s = await send(b, `trade:legs ${t.id}`, async () => ({ ixs: await legs(), signers: [seller, buyer] }));
      t.legsSig = s.sig; t.stage = "legs";
      note(b, "trade", `${t.from} записывает сделку DvP, допускает escrow и кладёт ${t.amount} обл., ${t.to} кладёт ${fmtCash(t.cash!)}; расчёт после отсечки${tag}`, s.sig);
      return;
    }
    const s = await send(b, `trade:one ${t.id}`, async () => ({ ixs: [...await legs(), settle()], signers: [seller, buyer, venue] }));
    settled(s.sig);
    note(b, "trade", `${t.from} продаёт ${t.to} ${t.amount} обл. за ${fmtCash(t.cash!)}: запись, escrow, оплата и расчёт одной транзакцией${tag}`, s.sig);
    return;
  }
  if (t.stage === "legs") {
    const s = await send(b, `trade:settle ${t.id}`, async () => ({ ixs: [settle()], signers: [venue] }));
    settled(s.sig);
    note(b, "trade", `площадка рассчитывает сделку: ${t.amount} обл. → ${t.to}, ${fmtCash(t.cash!)} → ${t.from}${tag}`, s.sig);
  }
}

// ---------------------------------------------------------------- the scheduler: every operation of a bond

const MAX_JOBS = 6; // operations of one bond at once
// Any error stops the bond at once: logged, written to its ledger, and nothing more is sent for it (send refuses).
function halt(rt: Runtime, where: string, e: unknown) {
  const { b } = rt;
  console.error(b.id, where, e);
  if (b.status === "error") return;
  b.status = "error"; b.error = `${where}: ${reason(e)}`; b.endedAt = bondNow(b);
  note(b, "error", `выпуск остановлен: ${b.error}`);
}
function run(rt: Runtime, key: string, holders: string[], work: () => Promise<void>) {
  for (const h of holders) rt.locked.add(h);
  const p = work()
    .catch((e) => halt(rt, key, e))
    .finally(() => { for (const h of holders) rt.locked.delete(h); rt.jobs.delete(key); save(rt.b); });
  rt.jobs.set(key, p);
}
function tick(rt: Runtime) {
  const { b } = rt;
  if (b.status === "creating") {
    if (!rt.jobs.has("issue")) run(rt, "issue", [], () => issue(rt));
    return;
  }
  if (b.status !== "live" || b.chainOrigin === undefined) return;
  const now = bondNow(b), hs = names(b);
  const room = () => rt.jobs.size < MAX_JOBS;
  // 1. corporate actions, each on its own; with the box ticked the steps due run one after another
  for (const ev of b.events) {
    const step = nextEventStep(ev), key = `event ${ev.index}`;
    if (!ev.auto || !step || rt.jobs.has(key) || now < ev.recordTime || (step === "pay" && now < ev.paymentTime) || !room()) continue;
    // the redemption once every offer is settled (tendered bonds are locked until then)
    if (ev.kind === "redemption" && !b.offers.every((o) => o.done)) continue;
    run(rt, key, [], async () => {
      for (let st: ReturnType<typeof nextEventStep> = step; st; st = nextEventStep(ev)) {
        if (st === "pay" && bondNow(b) < ev.paymentTime) break;
        await eventStep(rt, ev.index, st);
        if (st === "pay") break;
      }
    });
  }
  // 2. tenders settled after their window
  for (const o of b.offers) {
    const key = `offer ${o.id}`;
    if (!o.auto || o.done || o.kind !== "tender" || now < o.close || rt.jobs.has(key) || rt.jobs.has(`answer ${o.id}`) || !room()) continue;
    run(rt, key, [], () => settleTender(rt, o.id));
  }
  // 3. holders answering open offers, one at a time per offer (each confirmed before the next: the tenders' order is
  //    the program's, a buyback's remainder is known)
  for (const o of b.offers) {
    if (now < o.open || now >= o.close || rt.jobs.has(`answer ${o.id}`) || !room()) continue;
    const plan = o.plans.find((x) => x.at <= now && !rt.locked.has(x.holder));
    if (!plan) continue;
    o.plans.splice(o.plans.indexOf(plan), 1);
    run(rt, `answer ${o.id}`, [plan.holder], () => answerOffer(rt, o, plan));
  }
  // 4. mandatory trades: the one a day before a record moment sends its legs LEAD_DAYS before it and its settlement at
  //    it; the one at it and the one a day after it, one transaction each at their moment
  const finalK = b.recordTimes.length - 1;
  for (const t of b.trades) {
    if (t.kind !== "mandatory" || finished(t) || rt.jobs.has(`trade ${t.id}`) || !room()) continue;
    const R = b.recordTimes[t.k!];
    const due = t.stage === "legs" ? R : t.point === -1 ? R - LEAD_DAYS * DAY : R + t.point! * DAY;
    if (now < due) continue;
    const parties = [t.from, t.to].filter(Boolean) as string[];
    if (parties.some((h) => rt.locked.has(h))) continue;
    run(rt, `trade ${t.id}`, parties, () => tradeStep(rt, t));
  }
  // 5. random trades: each bond day each holder trades with a probability between the two propensities
  const today = Math.floor(now / DAY), p = b.params, life = b.maturityTime - b.issueTime;
  for (let d = b.decidedDay + 1; d <= today; d++) {
    b.decidedDay = d;
    // trading closes at the final record moment (the program refuses every transfer from then on): a trade is sent no
    // later than LEAD_DAYS before it, so it is confirmed before it
    if (d * DAY < b.issueTime || d * DAY >= b.recordTimes[finalK] - LEAD_DAYS * DAY) continue;
    const share = Math.min(1, Math.max(0, (d * DAY - b.issueTime) / life));
    const chance = p.propensityStart + (p.propensityEnd - p.propensityStart) * share;
    for (const h of hs) {
      if (!randomRoom(b, h) || random(b) >= chance) continue;
      const others = hs.filter((x) => x !== h && randomRoom(b, x));
      if (others.length) b.trades.push({ id: b.trades.length, kind: "random", at: d * DAY, from: h, to: others[Math.floor(random(b) * others.length)], stage: "planned" });
    }
  }
  for (const t of b.trades) {
    if (t.kind !== "random" || finished(t) || rt.jobs.has(`trade ${t.id}`) || !room()) continue;
    if (rt.locked.has(t.from!) || rt.locked.has(t.to!)) continue;
    if (now >= b.recordTimes[finalK] - LEAD_DAYS * DAY) { t.stage = "missed"; t.note = "торги закрываются с финальной отсечки"; continue; }
    const parties = [t.from!, t.to!];
    run(rt, `trade ${t.id}`, parties, () => tradeStep(rt, t));
  }
  // 6. the end: every event paid, every offer done, no trade in flight -> holders' leftover SOL back, then the audit
  if (b.events.every((e) => e.done) && b.offers.every((o) => o.done) && b.trades.every(finished) && rt.jobs.size === 0) {
    b.status = "done"; b.endedAt = bondNow(b);
    note(b, "event", "все корпоративные действия выполнены; ончейн аудит");
    run(rt, "end", [], async () => { await sweep(rt); runAudit(rt); });
  }
}
// Simulation clean-up: the generated holders' leftover SOL goes back to the service's wallet (their balances read once).
async function sweep(rt: Runtime) {
  const { b, keys } = rt;
  if (b.swept) return;
  const hs = names(b), infos = await opLabel.run("end", () => connection.getMultipleAccountsInfo(hs.map((h) => keys.holders[h].publicKey)));
  const sol = Object.fromEntries(hs.map((h, i) => [h, infos[i]?.lamports ?? 0]));
  for (const batch of chunks(hs.filter((h) => sol[h] > 0), 8)) {
    const s = await send(b, "end:sweep", async () => ({
      ixs: batch.map((h) => SystemProgram.transfer({ fromPubkey: keys.holders[h].publicKey, toPubkey: registrar.publicKey, lamports: sol[h] })),
      signers: [registrar, ...batch.map((h) => keys.holders[h])],
    }));
    note(b, "setup", `симуляция: остаток SOL держателей возвращается в кошелёк сервиса (${batch[0]}..${batch[batch.length - 1]})`, s.sig);
  }
  b.swept = true;
}

// ---------------------------------------------------------------- audit

// The bond as the audit reads it: terms, public keys, and the transactions the service sent for the holders, by
// signature. The audit finds each in the ledger and takes its phase (record moments passed) from the block's time.
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
  // trades the program accepted: settled ones (one transaction, or legs then settlement) and cancelled ones
  trades: b.trades.filter((t) => t.stage === "done" || t.stage === "cancelled").map((t) => ({
    from: t.from, to: t.to, amount: t.amount, cash: t.cash, sig: t.sig, ...(t.legsSig ? { fundSig: t.legsSig } : {}),
    ...(t.stage === "cancelled" ? { cancelled: true } : {}),
  })),
  offers: b.offers.filter((o) => o.funded).map((o) => ({
    id: o.id, kind: o.kind, priceUnits: o.priceUnits, maxQuantity: o.maxQuantity,
    open: { recordMoment: 0, plusDays: (o.open - b.recordTimes[0]) / DAY }, close: { recordMoment: 0, plusDays: (o.close - b.recordTimes[0]) / DAY },
    actions: o.actions.filter((x) => x.sig).map((x) => ({ holder: x.holder, ...(x.sell ? { sell: x.sell } : { tender: x.tender }), sig: x.sig })),
    ...(o.kind === "tender" && o.done ? { settlements: o.settlements.filter((x) => x.sig).map((x) => ({ holder: x.holder, sig: x.sig })) } : {}),
  })),
  handover: null,
});
// What each payment the service sent was for, by signature: a coupon's index, "redemption" or "offer N" (labels only;
// the audit measures the amounts itself).
const paymentsOf = (b: BondState) => Object.fromEntries([
  ...b.events.flatMap((e) => Object.values(e.paid).map((p) => [p.sig, e.kind === "coupon" ? e.index : "redemption"])),
  ...b.offers.flatMap((o) => [...o.actions.filter((x) => x.sell && x.sig).map((x) => x.sig), ...o.settlements.filter((x) => x.sig).map((x) => x.sig)].map((sig) => [sig, `offer ${o.id}`])),
]);
// The audit is a separate process with its own RPC client: one at a time (the others wait in turn), each lent a share
// of the budget, so the service keeps the rest.
const AUDIT_SHARE = 30;
const auditQueue: Runtime[] = [];
let auditing = false;
function runAudit(rt: Runtime) {
  if (rt.b.auditRunning) return;
  rt.b.auditRunning = true;
  auditQueue.push(rt);
  if (!auditing) nextAudit();
}
function nextAudit() {
  const rt = auditQueue.shift();
  if (!rt) { auditing = false; return; }
  auditing = true;
  const { b } = rt;
  budget.reserved = AUDIT_SHARE;
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
    budget.reserved = 0;
    if (err && !stdout) b.auditError = (stderr || String(err)).slice(-2000);
    else try { b.audit = { ...JSON.parse(stdout), evidence, at: new Date().toISOString() }; b.auditError = undefined; } catch { b.auditError = (stderr || stdout).slice(-2000); }
    save(b);
    nextAudit();
  });
}

// ---------------------------------------------------------------- view for the screen (no RPC: what the service sent)

function view(rt: Runtime, since: number) {
  const { b } = rt;
  const a = accountsOf(rt);
  const now = b.chainOrigin === undefined ? null : b.endedAt ?? bondNow(b);
  const sigs = b.ops.reduce((s, o) => s + o.signatures, 0);
  return {
    id: b.id, title: b.title, status: b.status, error: b.error, params: b.params, bond: b.bond, bondMint: b.bondMint, cashMint: b.cashMint,
    registrar: registrar.publicKey.toBase58(), venue: venue.publicKey.toBase58(), bondNow: now, clockAge: clockAge(),
    issueTime: b.issueTime, maturityTime: b.maturityTime, recordTimes: b.recordTimes, schedule: b.schedule, utcOffsetHours: RULES.utcOffsetHours,
    terms: { faceUnits: (BigInt(b.params.face) * UNIT).toString(), rateBps: Math.round(b.params.ratePct * 100), periodsPerYear: b.params.periodsPerYear, redemptionPriceBps: b.params.redemptionPct * 100, cashDecimals: CASH_DECIMALS },
    events: b.events.map((e) => ({
      ...e, lateDays: Object.values(e.paid).length ? Math.max(...Object.values(e.paid).map((x) => Math.max(0, Math.floor((x.at - e.paymentTime) / DAY)))) : null,
    })),
    offers: b.offers.map(({ plans, ...o }) => ({ ...o, waiting: plans.length })),
    trades: b.trades.filter((t) => t.stage !== "planned").map((t) => ({ ...t, place: t.kind === "mandatory" ? `отсечка ${t.k! + 1}, ${POINT_NAMES[t.point!]}` : "случайная" })),
    // addresses the page reads itself from the cluster (from the viewer's IP): each holder's record and bond account
    holders: b.holders.map((h) => ({ name: h.name, pubkey: h.pubkey, placed: b.placements[h.name], record: a.record(h.name).toBase58(), bonds: a.bondsAta(h.name).toBase58(), trades: used(b, h.name) })),
    ledger: b.ledger.slice(since), ledgerSize: b.ledger.length,
    audit: b.audit, auditError: b.auditError, auditRunning: !!b.auditRunning,
    cost: { transactions: b.ops.length, signatures: sigs, feesSol: (sigs * FEE) / LAMPORTS_PER_SOL },
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
        registrar: registrar.publicKey.toBase58(), venue: venue.publicKey.toBase58(), faucet: FAUCET, rpc: RPC,
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
      // SOL reserved for every bond still being issued, against the registrar's balance (read once per bond created)
      const need = estimateSol(p) + [...bonds.values()].filter((x) => x.b.status === "creating").reduce((s, x) => s + estimateSol(x.b.params), 0);
      const have = (await opLabel.run("service", () => connection.getBalance(registrar.publicKey))) / LAMPORTS_PER_SOL;
      if (have < need) return json(res, 400, { error: `на кошельке registrar ${have.toFixed(4)} SOL, на выпуск нужно около ${need.toFixed(3)} SOL: пополните его через ${FAUCET}` });
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
      // the operator's press, checked before anything is sent: the step due next, at its moment
      if (input.step !== nextEventStep(ev)) return json(res, 400, { error: `следующий шаг: ${nextEventStep(ev) ?? "нет"}` });
      if (bondNow(rt.b) < (input.step === "pay" ? ev.paymentTime : ev.recordTime)) return json(res, 400, { error: "время шага не наступило" });
      if (ev.kind === "redemption" && !rt.b.offers.every((o) => o.done)) return json(res, 400, { error: "погашение после расчёта всех оферт" });
      await press(rt, `event ${e}`, () => eventStep(rt, e, input.step));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts.length === 4) {
      if (rt.b.status !== "live") return json(res, 400, { error: "выпуск не в обращении" });
      if (rt.b.offers.length >= MAX_OFFERS) return json(res, 400, { error: `не больше ${MAX_OFFERS} оферт на выпуск` });
      const errors = offerErrors(rt.b, input);
      if (errors.length) return json(res, 400, { error: errors.join("; ") });
      await press(rt, "offer new", () => addOffer(rt, input));
      return json(res, 200, { ok: true });
    }
    if (parts[3] === "offers" && parts[5] === "auto") { rt.b.offers[Number(parts[4]) - 1].auto = !!input.auto; save(rt.b); return json(res, 200, { ok: true }); }
    if (parts[3] === "offers" && parts[5] === "step") {
      const id = Number(parts[4]), o = rt.b.offers[id - 1];
      if (!o) return json(res, 404, { error: "оферты нет" });
      if (o.auto) return json(res, 400, { error: "включена авто-обработка: снимите галку, чтобы нажимать шаги" });
      if (input.step !== "settle" || o.kind !== "tender" || o.done) return json(res, 400, { error: "шаг: settle у нерассчитанного tender" });
      if (bondNow(rt.b) < o.close || rt.jobs.has(`answer ${o.id}`)) return json(res, 400, { error: "окно оферты ещё открыто" });
      await press(rt, `offer ${id}`, () => settleTender(rt, id));
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
// Bonds survive a restart: state and keys are files; each step continues from the state, a step in the journal is not
// sent again.
for (const id of fs.readdirSync(DATA)) {
  const st = path.join(dirOf(id), "state.json");
  if (!fs.existsSync(st)) continue;
  const b: BondState = JSON.parse(fs.readFileSync(st, "utf8"));
  if (!b.wallet) continue; // a bond of an earlier version of the service: kept on disk, not loaded
  const k: Keys = JSON.parse(fs.readFileSync(path.join(dirOf(id), "keys.json"), "utf8"));
  const kp = (s: number[]) => Keypair.fromSecretKey(Uint8Array.from(s));
  b.auditRunning = false;
  const unconfirmed = b.ops.filter((o) => !o.confirmed);
  if ((b.status === "live" || b.status === "creating") && unconfirmed.length) {
    b.status = "error"; b.error = `сервис перезапущен, когда ${unconfirmed.length} транзакций ждали подтверждения: ${unconfirmed.map((o) => o.label).join(", ")}`;
  }
  bonds.set(id, { b, keys: { bondMint: kp(k.bondMint), cashMint: kp(k.cashMint), holders: Object.fromEntries(Object.entries(k.holders).map(([n, s]) => [n, kp(s)])) }, jobs: new Map(), locked: new Set(), issueRetryAt: 0 });
}
connection.onAccountChange(SYSVAR_CLOCK_PUBKEY, (info) => takeClock(info.data), { commitment: "confirmed" });
// The scheduler looks once a slot (the chain's step, about 400 ms): a bond day lasts one second.
setInterval(() => { for (const rt of bonds.values()) { try { tick(rt); } catch (e) { console.error(rt.b.id, "tick", e); } } }, 400);
server.listen(PORT, "127.0.0.1", () => console.log(`operator service on 127.0.0.1:${PORT}, registrar ${registrar.publicKey.toBase58()}, venue ${venue.publicKey.toBase58()}, ${bonds.size} bonds`));
