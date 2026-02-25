/**
 * fmp-earnings.ts
 *
 * FMP free-tier data layer for a multi-agent earnings pipeline.
 *
 * Endpoints (both free tier, well within 250 calls/day):
 *   GET /stable/earnings-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&apikey=
 *   GET /stable/stock-list?apikey=
 *
 * Call budget per run:
 *   • earnings-calendar  → 1 call (covers any date window in one response)
 *   • stock-list         → 0 calls on a warm cache, 1 call after 7-day TTL
 *   Total: ≤ 2 calls/run  (≪ 250/day free limit)
 *
 * Design notes:
 *   stock-list is a single ~50 k-record payload that includes
 *   `exchangeShortName` (e.g. "NYSE", "NASDAQ", "AMEX") per symbol.
 *   We cache it for 7 days so the exchange-filter step is always free
 *   in-memory after the first run of the week.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type EarningsEvent = {
  symbol: string;
  reportDate: string;        // ISO-8601, e.g. "2026-02-25"
  epsActual: number | null;
  epsEstimated: number | null;
  revenueActual: number | null;
  revenueEstimated: number | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  raw: any;                  // original FMP record, passed through unchanged
};

export type SymbolExchange = {
  symbol: string;            // uppercase
  exchange: string;          // uppercase, e.g. "NYSE" | "NASDAQ" | "AMEX"
};

export type FilterOptions = {
  /**
   * When true, events whose symbol is absent from the exchange map are
   * retained rather than dropped.  Useful for debugging / manual inspection.
   * Default: false (conservative — unknown symbols are excluded).
   */
  keepUnknown?: boolean;
};

// ---------------------------------------------------------------------------
// Internal constants
// ---------------------------------------------------------------------------

const FMP_BASE = "https://financialmodelingprep.com/stable";

/**
 * The exchange values we treat as "US primary large-cap listing".
 * This set is the data-driven definition of the assignment's
 * "NYSE/NASDAQ universe" requirement — not a hand-picked ticker list.
 */
const NYSE_NASDAQ = new Set(["NYSE", "NASDAQ"]);

/**
 * Cache file path.  Resolves to <project-root>/.fmp-symbol-cache.json
 * (i.e. next to where the caller runs, not buried inside node_modules).
 */
const CACHE_PATH = path.resolve(process.cwd(), ".fmp-symbol-cache.json");

/** Re-fetch stock-list once the cache is older than 7 days. */
const CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1_000;

// ---------------------------------------------------------------------------
// Internal: HTTP helper — 3 total attempts, exponential back-off + jitter
// ---------------------------------------------------------------------------

/**
 * Fetch `url` and return parsed JSON.
 *
 * Retry policy:
 *   - Max 3 total attempts (initial + 2 retries).
 *   - Retries on HTTP 429 (rate-limited) and 503 (transient server error).
 *   - Back-off: 500 ms → 1 000 ms with ±20 % random jitter.
 *   - All other non-2xx statuses throw immediately (no retry).
 *
 * After a successful HTTP response, the body is checked for FMP's
 * application-level error envelope `{ "Error Message": "..." }`, which
 * the API returns with HTTP 200 for invalid keys or locked endpoints.
 */
async function fetchJson(url: string): Promise<unknown> {
  const MAX_ATTEMPTS = 3;
  const BASE_DELAY_MS = 500;

  // Redact the key from any error messages before they propagate.
  const safeUrl = url.replace(/apikey=[^&]+/, "apikey=REDACTED");

  let delayMs = BASE_DELAY_MS;

  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    let res: Response;
    try {
      res = await fetch(url);
    } catch (networkErr) {
      // Network-level failure (DNS, ECONNRESET, etc.).
      if (attempt === MAX_ATTEMPTS) {
        throw new Error(`[FMP] Network error after ${attempt} attempts — ${safeUrl}: ${networkErr}`);
      }
      await sleep(delayMs * jitter());
      delayMs *= 2;
      continue;
    }

    // --- HTTP error handling ---
    if (res.status === 429 || res.status === 503) {
      if (attempt < MAX_ATTEMPTS) {
        const retryAfter = res.headers.get("Retry-After");
        const waitMs = retryAfter ? parseInt(retryAfter, 10) * 1_000 : delayMs * jitter();
        await sleep(waitMs);
        delayMs *= 2;
        continue;
      }
      throw new Error(
        `[FMP] Rate-limited (${res.status}) after ${MAX_ATTEMPTS} attempts — ${safeUrl}`,
      );
    }

    if (!res.ok) {
      throw new Error(
        `[FMP] HTTP ${res.status} ${res.statusText} — ${safeUrl}`,
      );
    }

    // --- Parse JSON ---
    const body: unknown = await res.json().catch(() => {
      throw new Error(`[FMP] Non-JSON response from ${safeUrl}`);
    });

    // FMP returns HTTP 200 with {"Error Message": "..."} for invalid API keys
    // and paid-only endpoints.  Detect and surface this clearly.
    if (isErrorEnvelope(body)) {
      const msg = (body as Record<string, unknown>)["Error Message"] as string;
      throw new Error(`[FMP] API error: ${msg} — ${safeUrl}`);
    }

    return body;
  }

  throw new Error(`[FMP] Exhausted ${MAX_ATTEMPTS} attempts — ${safeUrl}`);
}

function isErrorEnvelope(body: unknown): boolean {
  return (
    body !== null &&
    typeof body === "object" &&
    !Array.isArray(body) &&
    "Error Message" in (body as object)
  );
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Returns a multiplier in [0.8, 1.2] for ±20 % jitter. */
const jitter = () => 0.8 + Math.random() * 0.4;

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function assertApiKey(apiKey: string, context: string): void {
  if (!apiKey || !apiKey.trim()) {
    throw new Error(
      `[FMP] Missing API key in ${context}. ` +
      "Set FMP_API_KEY in your environment or .env file.",
    );
  }
}

// ---------------------------------------------------------------------------
// 1. fetchFmpEarningsCalendar
// ---------------------------------------------------------------------------

/**
 * Fetch the FMP earnings calendar for [fromDate, toDate] (inclusive) and
 * normalise each record into an {@link EarningsEvent}.
 *
 * A single HTTP call covers the full date window — FMP returns all events
 * in one response.  Typical windows (e.g. one week) return O(100–500) events.
 *
 * @param fromDate - ISO-8601 start date, e.g. "2026-02-24"
 * @param toDate   - ISO-8601 end date,   e.g. "2026-02-28"
 * @param apiKey   - FMP API key (free tier is sufficient)
 * @returns        - Normalised events.  reportDate mirrors the "date" field.
 * @throws         - On network errors, HTTP failures, invalid key, or
 *                   unexpected response shape.
 */
export async function fetchFmpEarningsCalendar(
  fromDate: string,
  toDate: string,
  apiKey: string,
): Promise<EarningsEvent[]> {
  assertApiKey(apiKey, "fetchFmpEarningsCalendar");

  const url =
    `${FMP_BASE}/earnings-calendar` +
    `?from=${encodeURIComponent(fromDate)}` +
    `&to=${encodeURIComponent(toDate)}` +
    `&apikey=${encodeURIComponent(apiKey)}`;

  const data = await fetchJson(url);

  if (!Array.isArray(data)) {
    throw new Error(
      `[FMP] earnings-calendar: expected JSON array, got: ` +
      JSON.stringify(data).slice(0, 160),
    );
  }

  const events: EarningsEvent[] = [];

  for (const item of data as Record<string, unknown>[]) {
    const symbol = String(item["symbol"] ?? "").trim();
    const reportDate = String(item["date"] ?? "").trim();

    if (!symbol || !reportDate) {
      // Skip malformed records rather than silently passing bad data downstream.
      console.warn(
        `[FMP] earnings-calendar: skipping record missing symbol or date:`,
        JSON.stringify(item).slice(0, 120),
      );
      continue;
    }

    events.push({
      symbol,
      reportDate,
      epsActual:        toNumberOrNull(item["epsActual"]),
      epsEstimated:     toNumberOrNull(item["epsEstimated"]),
      revenueActual:    toNumberOrNull(item["revenueActual"]),
      revenueEstimated: toNumberOrNull(item["revenueEstimated"]),
      raw: item,
    });
  }

  return events;
}

// ---------------------------------------------------------------------------
// 2. buildSymbolToExchangeMap
// ---------------------------------------------------------------------------

type CachePayload = {
  builtAt: number;                                  // epoch ms
  entries: Array<{ symbol: string; exchange: string }>;
};

/**
 * Build a `symbol → exchangeShortName` map (both uppercase) using the FMP
 * `/stable/stock-list` endpoint.
 *
 * **Why this endpoint:**
 *   - Returns ~50 k records in a single response — no pagination needed.
 *   - Each record includes `exchangeShortName` (e.g. "NYSE", "NASDAQ",
 *     "AMEX", "TSX") which is exactly the filter key we need.
 *   - Free on the FMP basic plan.
 *
 * **Field name resilience:**
 *   The function checks `exchangeShortName`, `exchange_short_name`, and
 *   `exchange` in that priority order, covering any FMP schema drift.
 *
 * **Disk cache:**
 *   The map is written to `.fmp-symbol-cache.json` with a 7-day TTL.
 *   Typical week: 0 API calls for this step (all in-memory from cache).
 *
 * @param apiKey - FMP API key (free tier is sufficient)
 * @returns      - Map<symbol, exchange>, ready for {@link filterNyseNasdaq}.
 * @throws       - On network errors, HTTP failures, invalid key, or
 *                 an unexpectedly empty response.
 */
export async function buildSymbolToExchangeMap(
  apiKey: string,
): Promise<Map<string, string>> {
  assertApiKey(apiKey, "buildSymbolToExchangeMap");

  // --- Fast path: warm disk cache ---
  const cached = await tryLoadCache();
  if (cached !== null) {
    console.log(
      `[FMP] Symbol→exchange map: loaded from cache ` +
      `(${cached.map.size.toLocaleString()} symbols, ` +
      `cached ${formatAge(cached.builtAt)} ago).`,
    );
    return cached.map;
  }

  // --- Slow path: fetch from FMP ---
  console.log("[FMP] Symbol→exchange map: cache miss — fetching /stable/stock-list…");
  const url = `${FMP_BASE}/stock-list?apikey=${encodeURIComponent(apiKey)}`;
  const data = await fetchJson(url);

  if (!Array.isArray(data)) {
    throw new Error(
      `[FMP] stock-list: expected JSON array, got: ` +
      JSON.stringify(data).slice(0, 160),
    );
  }

  if (data.length === 0) {
    throw new Error(
      "[FMP] stock-list: response was an empty array — " +
      "verify API key permissions or try again later.",
    );
  }

  // Build the map, tolerating multiple field-name conventions.
  const map = new Map<string, string>();

  for (const item of data as Record<string, unknown>[]) {
    const sym = String(item["symbol"] ?? "").toUpperCase().trim();

    // Priority: exchangeShortName → exchange_short_name → exchange
    const exchRaw =
      item["exchangeShortName"] ??
      item["exchange_short_name"] ??
      item["exchange"] ??
      "";
    const exch = String(exchRaw).toUpperCase().trim();

    if (sym && exch) map.set(sym, exch);
  }

  if (map.size === 0) {
    throw new Error(
      "[FMP] stock-list: parsed 0 valid symbol→exchange pairs. " +
      "Response shape may have changed — inspect the raw payload.",
    );
  }

  await persistCache(map);
  console.log(
    `[FMP] Symbol→exchange map: built from API and cached ` +
    `(${map.size.toLocaleString()} symbols → ${CACHE_PATH}).`,
  );
  return map;
}

// --- Cache helpers ---

async function tryLoadCache(): Promise<{
  map: Map<string, string>;
  builtAt: number;
} | null> {
  let raw: string;
  try {
    raw = await fs.readFile(CACHE_PATH, "utf8");
  } catch {
    return null; // file doesn't exist yet — first run
  }

  let payload: CachePayload;
  try {
    payload = JSON.parse(raw) as CachePayload;
  } catch {
    console.warn("[FMP] Symbol cache file is corrupt; will rebuild.");
    return null;
  }

  if (
    typeof payload.builtAt !== "number" ||
    !Array.isArray(payload.entries)
  ) {
    console.warn("[FMP] Symbol cache has unexpected structure; will rebuild.");
    return null;
  }

  if (Date.now() - payload.builtAt > CACHE_TTL_MS) {
    console.log("[FMP] Symbol cache is stale (> 7 days); will refresh from API.");
    return null;
  }

  const map = new Map<string, string>(
    payload.entries.map(({ symbol, exchange }) => [symbol, exchange]),
  );
  return { map, builtAt: payload.builtAt };
}

async function persistCache(map: Map<string, string>): Promise<void> {
  const payload: CachePayload = {
    builtAt: Date.now(),
    entries: Array.from(map.entries()).map(([symbol, exchange]) => ({
      symbol,
      exchange,
    })),
  };
  try {
    await fs.writeFile(CACHE_PATH, JSON.stringify(payload), "utf8");
  } catch (err) {
    // Non-fatal: next run will just re-fetch.  Log so it's visible.
    console.warn(`[FMP] Could not write symbol cache to ${CACHE_PATH}: ${err}`);
  }
}

// ---------------------------------------------------------------------------
// 3. filterNyseNasdaq
// ---------------------------------------------------------------------------

/**
 * Filter `events` down to those whose symbol appears in `symbolToExchange`
 * with exchange "NYSE" or "NASDAQ".
 *
 * **Drop policy for unknowns (default):**
 *   Symbols absent from the map are most likely OTC instruments, foreign
 *   primary listings, or very recent IPOs not yet in the stock-list snapshot.
 *   They are dropped to keep the universe clean.  Pass `keepUnknown: true`
 *   to retain them for debugging — they will appear in the output but their
 *   exchange will be unresolved.
 *
 * This function is pure (no I/O) and runs in O(n) time.
 *
 * @param events           - Calendar events from {@link fetchFmpEarningsCalendar}.
 * @param symbolToExchange - Exchange map from {@link buildSymbolToExchangeMap}.
 * @param options          - Optional behaviour flags.
 */
export function filterNyseNasdaq(
  events: EarningsEvent[],
  symbolToExchange: Map<string, string>,
  options: FilterOptions = {},
): EarningsEvent[] {
  const { keepUnknown = false } = options;

  let keptNyseNasdaq = 0;
  let keptUnknown = 0;
  let dropped = 0;

  const result = events.filter((ev) => {
    const sym  = ev.symbol.toUpperCase();
    const exch = symbolToExchange.get(sym);

    if (exch === undefined) {
      if (keepUnknown) { keptUnknown++; return true; }
      dropped++;
      return false;
    }

    if (NYSE_NASDAQ.has(exch)) { keptNyseNasdaq++; return true; }
    dropped++;
    return false;
  });

  console.log(
    `[FMP] Filter: ${keptNyseNasdaq} NYSE/NASDAQ` +
    (keepUnknown ? `, ${keptUnknown} unknown (kept)` : "") +
    `, ${dropped} dropped` +
    ` (of ${events.length} total events).`,
  );

  return result;
}

// ---------------------------------------------------------------------------
// 4. fetchUsEarningsCalendar  —  convenience pipeline entry point
// ---------------------------------------------------------------------------

/**
 * Single-call convenience wrapper for the full calendar → filter pipeline.
 *
 * Runs the calendar fetch and the exchange-map load **in parallel** via
 * `Promise.all`; the exchange-map step hits the network only on the first
 * run of each week (disk-cache hit otherwise).
 *
 * Returns the already-filtered NYSE/NASDAQ event list plus the map itself
 * (useful for downstream agents that want to log or inspect exchange data).
 *
 * @param fromDate - ISO-8601 start date, e.g. "2026-02-24"
 * @param toDate   - ISO-8601 end date,   e.g. "2026-02-28"
 * @param apiKey   - FMP API key
 * @param options  - Passed through to {@link filterNyseNasdaq}.
 */
export async function fetchUsEarningsCalendar(
  fromDate: string,
  toDate: string,
  apiKey: string,
  options: FilterOptions = {},
): Promise<{
  events: EarningsEvent[];
  symbolToExchange: Map<string, string>;
}> {
  const [rawEvents, symbolToExchange] = await Promise.all([
    fetchFmpEarningsCalendar(fromDate, toDate, apiKey),
    buildSymbolToExchangeMap(apiKey),
  ]);

  const events = filterNyseNasdaq(rawEvents, symbolToExchange, options);
  return { events, symbolToExchange };
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function toNumberOrNull(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function formatAge(epochMs: number): string {
  const ageMs = Date.now() - epochMs;
  const hours = Math.floor(ageMs / (60 * 60 * 1_000));
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}
