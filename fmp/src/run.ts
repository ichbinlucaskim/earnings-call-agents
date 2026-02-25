/**
 * run.ts
 *
 * CLI entry point for the FMP → Ninjas earnings pipeline.
 *
 * Steps:
 *   1. Parse --from / --to date flags (defaults to a recent week).
 *   2. Validate required env vars.
 *   3. Fetch the FMP earnings calendar and filter to NYSE/NASDAQ.
 *   4. For each event, fetch the transcript from API Ninjas.
 *   5. Print a per-symbol result and a final summary.
 *
 * Usage:
 *   npx tsx src/run.ts [--from YYYY-MM-DD] [--to YYYY-MM-DD]
 */

import { fetchUsEarningsCalendar } from "./fmp-earnings.js";
import {
  NinjasTranscriptProvider,
  TranscriptNotFoundError,
} from "./transcript-provider.js";

// ---------------------------------------------------------------------------
// Defaults — override with --from / --to flags
// ---------------------------------------------------------------------------

const DEFAULT_FROM = "2026-02-24";
const DEFAULT_TO   = "2026-02-28";

// ---------------------------------------------------------------------------
// Argument parsing  (no third-party libs — plain process.argv)
// ---------------------------------------------------------------------------

type CliArgs = { from: string; to: string };

function parseArgs(argv: string[]): CliArgs {
  // argv = ["node", "/path/to/run.ts", ...user flags]
  const flags = argv.slice(2);
  let from = DEFAULT_FROM;
  let to   = DEFAULT_TO;

  for (let i = 0; i < flags.length; i++) {
    const flag = flags[i]!;
    const next = flags[i + 1];

    if (flag === "--from" || flag === "-f") {
      if (!next || next.startsWith("-")) {
        throw new Error(`Flag ${flag} requires a YYYY-MM-DD value.`);
      }
      from = next;
      i++;
    } else if (flag === "--to" || flag === "-t") {
      if (!next || next.startsWith("-")) {
        throw new Error(`Flag ${flag} requires a YYYY-MM-DD value.`);
      }
      to = next;
      i++;
    } else {
      throw new Error(
        `Unknown flag: "${flag}"\n` +
        `Usage: npx tsx src/run.ts [--from YYYY-MM-DD] [--to YYYY-MM-DD]`,
      );
    }
  }

  const ISO_RE = /^\d{4}-\d{2}-\d{2}$/;
  if (!ISO_RE.test(from)) {
    throw new Error(`--from "${from}" is not a valid ISO-8601 date (YYYY-MM-DD).`);
  }
  if (!ISO_RE.test(to)) {
    throw new Error(`--to "${to}" is not a valid ISO-8601 date (YYYY-MM-DD).`);
  }
  if (from > to) {
    throw new Error(
      `--from (${from}) must not be later than --to (${to}).`,
    );
  }

  return { from, to };
}

// ---------------------------------------------------------------------------
// Environment validation
// ---------------------------------------------------------------------------

function requireEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(
      `Required environment variable "${name}" is not set.\n` +
      `  Fix: export ${name}=<your-key>`,
    );
  }
  return value;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const { from, to } = parseArgs(process.argv);
  const fmpKey       = requireEnv("FMP_API_KEY");
  const ninjasKey    = requireEnv("NINJAS_API_KEY");

  console.log("╔══════════════════════════════════════╗");
  console.log("║    FMP → Ninjas Earnings Pipeline    ║");
  console.log("╚══════════════════════════════════════╝");
  console.log(`  Window : ${from}  →  ${to}`);
  console.log();

  // ── Step 1: Calendar + NYSE/NASDAQ filter ──────────────────────────────

  console.log("[Calendar] Fetching FMP earnings calendar and exchange map…");

  const { events } = await fetchUsEarningsCalendar(from, to, fmpKey);
  // Note: fetchUsEarningsCalendar already logs a one-line filter summary
  // (e.g. "[FMP] Filter: 42 NYSE/NASDAQ, 317 dropped (of 359 total events)").

  if (events.length === 0) {
    console.log("[Calendar] No NYSE/NASDAQ events in this window — nothing to fetch.");
    return;
  }

  console.log(`[Calendar] Processing ${events.length} event(s).\n`);

  // ── Step 2: Transcripts ────────────────────────────────────────────────

  const provider = new NinjasTranscriptProvider(ninjasKey);

  let fetched = 0;
  let skipped = 0; // TranscriptNotFoundError — normal, not alarming
  let errored = 0; // unexpected network / API failure

  // Sequential: avoids hammering the Ninjas rate limit on the free tier.
  for (const ev of events) {
    const tag = `${ev.symbol.padEnd(6)} ${ev.reportDate}`;

    try {
      const transcript = await provider.fetchTranscript(ev.symbol, ev.reportDate);
      fetched++;
      console.log(
        `[Transcript] ✓  ${tag}  length=${transcript.text.length.toLocaleString()}`,
      );
    } catch (err) {
      if (err instanceof TranscriptNotFoundError) {
        // No transcript exists for this symbol/quarter — expected for some events.
        skipped++;
        console.warn(`[Transcript] -  ${tag}  no transcript available (skipped)`);
      } else {
        // Unexpected failure (network, bad JSON, etc.) — log and continue.
        errored++;
        console.error(
          `[Transcript] ✗  ${tag}  error: ${(err as Error).message}`,
        );
      }
    }
  }

  // ── Summary ────────────────────────────────────────────────────────────

  console.log();
  console.log("┌─────────────────────────────┐");
  console.log("│          Summary            │");
  console.log("├─────────────────────────────┤");
  console.log(`│  Events in window  : ${String(events.length).padStart(4)}   │`);
  console.log(`│  Transcripts OK    : ${String(fetched).padStart(4)}   │`);
  console.log(`│  Skipped (no data) : ${String(skipped).padStart(4)}   │`);
  console.log(`│  Errors            : ${String(errored).padStart(4)}   │`);
  console.log("└─────────────────────────────┘");

  if (errored > 0) {
    // Signal partial failure to any calling process (CI, grader scripts, etc.).
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
// Top-level error handler — prints cleanly and exits 1 on fatal errors
// (missing API key, bad date flags, FMP network failure, etc.)
// ---------------------------------------------------------------------------

main().catch((err: unknown) => {
  const message = err instanceof Error ? err.message : String(err);
  console.error(`\n[Fatal] ${message}`);
  process.exit(1);
});
