// Measure seconds per bond trade on a cluster, sent the way the operator sends them (hooked transfer built with
// once-per-run account lookups, sendAndConfirmTransaction at "confirmed", a clock read per record). The measured
// rate sizes a scenario's chainSecondsPerBondDay: every phase between record moments must fit its transactions.
// Each pair of holders moves one bond there and back, so positions end where they started.
// Usage: npx tsx src/throughput.ts <scenarios/NAME.json> <rpc url> <pairs> [lookups-each]
//   lookups-each: look the accounts up for every transfer, as the operator did before 2026-10-09 (for comparison).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Connection, Keypair, PublicKey, SYSVAR_CLOCK_PUBKEY, Transaction, sendAndConfirmTransaction } from "@solana/web3.js";
import { TOKEN_2022_PROGRAM_ID, createTransferCheckedWithTransferHookInstruction, getAssociatedTokenAddressSync } from "@solana/spl-token";

const ROOT = path.resolve(import.meta.dirname, "../..");
const readKeypair = (p: string) => Keypair.fromSecretKey(Uint8Array.from(JSON.parse(fs.readFileSync(p, "utf8"))));

async function main() {
  const [scenarioFile, rpc, pairsArg, mode] = process.argv.slice(2);
  const lookupsEach = mode === "lookups-each";
  if (!scenarioFile || !rpc || !pairsArg) throw new Error("usage: throughput.ts <scenarios/NAME.json> <rpc url> <pairs>");
  const spec = JSON.parse(fs.readFileSync(path.resolve(scenarioFile), "utf8"));
  let retries = 0;
  const retryingFetch = async (input: any, init?: any) => {
    for (let attempt = 1; ; attempt++) {
      try {
        const res = await fetch(input, init);
        if (res.status !== 429) return res;
      } catch {}
      retries++;
      await new Promise((r) => setTimeout(r, 1_000 * Math.min(2 ** attempt, spec.chainSecondsPerBondDay)));
    }
  };
  const connection = new Connection(rpc, { commitment: "confirmed", fetch: retryingFetch as any });
  const registrar = readKeypair(path.join(os.homedir(), ".config/solana/id.json"));
  const mint = readKeypair(path.join(ROOT, spec.keys.bondMint)).publicKey;
  const lookedUp = new Map<string, Promise<any>>();
  const staticLookups = {
    getAccountInfo: (key: PublicKey, commitment?: any) => {
      const k = key.toBase58();
      if (!lookedUp.has(k)) lookedUp.set(k, connection.getAccountInfo(key, commitment));
      return lookedUp.get(k)!;
    },
  } as unknown as Connection;
  const tenderers = new Set(spec.offers.flatMap((o: any) => o.actions.filter((a: any) => a.tender).map((a: any) => a.holder)));
  const names = Object.keys(spec.wallets).filter((n) => !tenderers.has(n));
  const owner = (n: string) => readKeypair(path.join(ROOT, spec.wallets[n]));
  const ata = (n: string) => getAssociatedTokenAddressSync(mint, owner(n).publicKey, false, TOKEN_2022_PROGRAM_ID);
  const infos = await connection.getMultipleAccountsInfo(names.slice(0, 100).map(ata));
  const funded = names.slice(0, 100).filter((_, i) => infos[i] && infos[i]!.data.readBigUInt64LE(64) > 0n);
  const pairs = Math.min(Number(pairsArg), Math.floor(funded.length / 2));
  const times: number[] = [];
  const transfer = async (from: string, to: string) => {
    const started = Date.now();
    const ix = await createTransferCheckedWithTransferHookInstruction(
      lookupsEach ? connection : staticLookups, ata(from), mint, ata(to), owner(from).publicKey, 1n, 0, [], "confirmed", TOKEN_2022_PROGRAM_ID,
    );
    await sendAndConfirmTransaction(connection, new Transaction().add(ix), [registrar, owner(from)], { commitment: "confirmed" });
    await connection.getAccountInfo(SYSVAR_CLOCK_PUBKEY);
    times.push((Date.now() - started) / 1000);
  };
  for (let i = 0; i < pairs; i++) {
    const [a, b] = [funded[2 * i], funded[2 * i + 1]];
    await transfer(a, b);
    await transfer(b, a);
  }
  const mean = times.reduce((s, t) => s + t, 0) / times.length;
  console.log(`${times.length} trades: mean ${mean.toFixed(2)} s, max ${Math.max(...times).toFixed(2)} s, ` +
    `rate-limit or connection retries ${retries}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
