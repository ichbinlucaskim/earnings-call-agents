/**
 * transcript-provider.ts
 *
 * Provider abstraction for earnings-call transcripts.
 *
 * Design goal: the rest of the pipeline never imports from a specific
 * provider.  It only sees `TranscriptProvider` and `Transcript`.  To
 * switch from API Ninjas to FMP (or any other source) you swap exactly
 * one `new XProvider(key)` call at the injection site.
 *
 *  ┌─────────────────────────────────────────┐
 *  │ TranscriptProvider (interface)          │
 *  │   fetchTranscript(symbol, callDate)     │
 *  │         → Promise<Transcript>           │
 *  └──────────┬──────────────────┬───────────┘
 *             │                  │
 *   NinjasTranscriptProvider   FmpTranscriptProvider
 *   (current — free tier)      (future — paid tier)
 */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Normalised transcript returned by every provider.
 *
 * `text`  — full plain-text transcript, ready for LLM ingestion.
 * `raw`   — original parsed JSON response from the provider, preserved for
 *           debugging or richer downstream processing (e.g. speaker diarisation
 *           if the provider supports it).
 */
export type Transcript = {
  symbol: string;
  callDate: string;    // ISO-8601, e.g. "2026-02-25"
  text: string;        // full transcript as a single string
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  raw: any;            // original provider response, unmodified
};

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

/**
 * The contract every transcript source must satisfy.
 *
 * The assignment requires fetching earnings call transcripts; this interface
 * is how the rest of the pipeline stays decoupled from any particular vendor.
 * For this submission, {@link NinjasTranscriptProvider} is wired in.
 * {@link FmpTranscriptProvider} is implemented but requires a paid key and
 * is not active — see the migration note in the design doc.
 */
export interface TranscriptProvider {
  /**
   * Fetch an earnings-call transcript for `symbol` reported on `callDate`.
   *
   * Implementations are responsible for:
   *  - Mapping `callDate` to their own year/quarter parameterisation.
   *  - HTTP retries for transient errors.
   *  - Raising descriptive errors for 404 (no transcript) vs. 5xx (server).
   *
   * @param symbol   - Ticker, e.g. "NVDA".
   * @param callDate - ISO-8601 date of the earnings call, e.g. "2026-02-25".
   * @returns        - Normalised {@link Transcript}.
   * @throws         - {@link TranscriptNotFoundError} when the provider has no
   *                   record for this symbol/quarter; plain Error otherwise.
   */
  fetchTranscript(symbol: string, callDate: string): Promise<Transcript>;
}

/**
 * Thrown when a transcript simply does not exist for the requested
 * symbol/quarter.  Callers should catch this separately from generic
 * network/API errors so they can log-and-skip without alarming on-call.
 */
export class TranscriptNotFoundError extends Error {
  constructor(
    public readonly symbol: string,
    public readonly callDate: string,
    public readonly providerName: string,
  ) {
    super(
      `[${providerName}] No transcript found for ${symbol} (call date ${callDate}).`,
    );
    this.name = "TranscriptNotFoundError";
  }
}

// ---------------------------------------------------------------------------
// Provider A — API Ninjas  (current; wired into the assignment pipeline)
// ---------------------------------------------------------------------------

const NINJAS_URL = "https://api.api-ninjas.com/v1/earningstranscript";

/**
 * Fetches earnings transcripts from API Ninjas.
 *
 * API Ninjas returns one of two shapes depending on the account tier:
 *   Premium: `{ transcript_split: [{ speaker, speaker_type, text }, …], … }`
 *   Free:    `{ transcript: "<full plain text>", … }`
 *
 * Both are normalised into a single `text` string:
 *   - Premium: speaker turns are joined with "\n\n" (preserving structure).
 *   - Free:    the `transcript` field is used directly.
 *
 * The original response is always preserved in `raw`.
 */
export class NinjasTranscriptProvider implements TranscriptProvider {
  private readonly name = "API Ninjas";

  constructor(private readonly apiKey: string) {
    if (!apiKey?.trim()) {
      throw new Error(
        "[API Ninjas] Missing API key. Set NINJAS_API_KEY in your environment.",
      );
    }
  }

  async fetchTranscript(symbol: string, callDate: string): Promise<Transcript> {
    const { year, quarter } = dateToFiscalYearQuarter(callDate);

    const url =
      `${NINJAS_URL}` +
      `?ticker=${encodeURIComponent(symbol.toUpperCase())}` +
      `&year=${year}&quarter=${quarter}`;

    const res = await fetch(url, {
      headers: { "X-Api-Key": this.apiKey },
    });

    if (res.status === 404) {
      throw new TranscriptNotFoundError(symbol, callDate, this.name);
    }
    if (!res.ok) {
      throw new Error(
        `[${this.name}] HTTP ${res.status} for ${symbol} ${year}Q${quarter}.`,
      );
    }

    const data: unknown = await res.json().catch(() => {
      throw new Error(`[${this.name}] Non-JSON response for ${symbol} ${year}Q${quarter}.`);
    });

    // The API may return a list; take the first element.
    const raw = Array.isArray(data) ? data[0] : data;

    if (!raw || typeof raw !== "object") {
      throw new TranscriptNotFoundError(symbol, callDate, this.name);
    }

    const text = extractNinjasText(raw as Record<string, unknown>, symbol, year, quarter);
    return { symbol: symbol.toUpperCase(), callDate, text, raw };
  }
}

/**
 * Extract a plain-text transcript from a Ninjas response object.
 * Handles both the premium `transcript_split` shape and the free
 * plain-`transcript` shape.
 */
function extractNinjasText(
  raw: Record<string, unknown>,
  symbol: string,
  year: number,
  quarter: number,
): string {
  // --- Premium path: structured speaker turns ---
  const split =
    (raw["transcript_split"] as unknown[] | undefined) ??
    (raw["transcript_by_speaker"] as unknown[] | undefined);

  if (Array.isArray(split) && split.length > 0) {
    const lines: string[] = [];
    for (const turn of split as Record<string, unknown>[]) {
      const speaker = String(turn["speaker"] ?? "Unknown").trim();
      const text    = String(turn["text"] ?? "").trim();
      if (text) lines.push(`${speaker}: ${text}`);
    }
    if (lines.length > 0) return lines.join("\n\n");
  }

  // --- Free path: plain text ---
  const text = String(raw["transcript"] ?? "").trim();
  if (text) return text;

  throw new TranscriptNotFoundError(
    symbol,
    `${year}Q${quarter}`,
    "API Ninjas",
  );
}

// ---------------------------------------------------------------------------
// Provider B — FMP paid tier  (future; not active in assignment submission)
// ---------------------------------------------------------------------------

const FMP_TRANSCRIPT_URL =
  "https://financialmodelingprep.com/stable/earning-call-transcript";

/**
 * Fetches earnings transcripts from FMP's paid transcript endpoint.
 *
 * Endpoint: GET /stable/earning-call-transcript
 *             ?symbol=AAPL&year=2025&quarter=1&apikey=…
 *
 * FMP returns an array; the first element has a `content` field with the
 * full plain-text transcript.
 *
 * ### How to migrate
 *
 * 1. Upgrade to a paid FMP plan.
 * 2. At your injection site, replace:
 *      ```ts
 *      const transcripts: TranscriptProvider = new NinjasTranscriptProvider(NINJAS_KEY);
 *      ```
 *    with:
 *      ```ts
 *      const transcripts: TranscriptProvider = new FmpTranscriptProvider(FMP_KEY);
 *      ```
 * 3. Nothing else in the pipeline changes — all call sites use the
 *    `TranscriptProvider` interface and receive the same `Transcript` shape.
 */
export class FmpTranscriptProvider implements TranscriptProvider {
  private readonly name = "FMP";

  constructor(private readonly apiKey: string) {
    if (!apiKey?.trim()) {
      throw new Error(
        "[FMP] Missing API key. Set FMP_API_KEY in your environment.",
      );
    }
  }

  async fetchTranscript(symbol: string, callDate: string): Promise<Transcript> {
    const { year, quarter } = dateToFiscalYearQuarter(callDate);

    const url =
      `${FMP_TRANSCRIPT_URL}` +
      `?symbol=${encodeURIComponent(symbol.toUpperCase())}` +
      `&year=${year}&quarter=${quarter}` +
      `&apikey=${encodeURIComponent(this.apiKey)}`;

    const res = await fetch(url);

    if (res.status === 404) {
      throw new TranscriptNotFoundError(symbol, callDate, this.name);
    }
    if (!res.ok) {
      // FMP occasionally returns 200 + {"Error Message": "..."} for paid
      // endpoints accessed on a free key.  We surface that below.
      throw new Error(
        `[${this.name}] HTTP ${res.status} for ${symbol} ${year}Q${quarter}.`,
      );
    }

    const data: unknown = await res.json().catch(() => {
      throw new Error(`[${this.name}] Non-JSON response for ${symbol} ${year}Q${quarter}.`);
    });

    // Detect FMP's application-level error envelope.
    if (isFmpErrorEnvelope(data)) {
      const msg = (data as Record<string, unknown>)["Error Message"] as string;
      throw new Error(`[${this.name}] API error: ${msg}`);
    }

    const raw = Array.isArray(data) ? data[0] : data;

    if (!raw || typeof raw !== "object") {
      throw new TranscriptNotFoundError(symbol, callDate, this.name);
    }

    const record = raw as Record<string, unknown>;
    const text = String(record["content"] ?? "").trim();

    if (!text) {
      throw new TranscriptNotFoundError(symbol, callDate, this.name);
    }

    return { symbol: symbol.toUpperCase(), callDate, text, raw };
  }
}

// ---------------------------------------------------------------------------
// Shared utilities
// ---------------------------------------------------------------------------

/**
 * Approximate the fiscal reporting year and quarter from an earnings call date.
 *
 * Companies report shortly *after* the fiscal quarter ends, so:
 *   Call in Jan–Mar → reports Q4 of the prior year
 *   Call in Apr–Jun → reports Q1 of the current year
 *   Call in Jul–Sep → reports Q2
 *   Call in Oct–Dec → reports Q3
 *
 * This heuristic matches what both API Ninjas and FMP use as the
 * year/quarter parameters.
 */
function dateToFiscalYearQuarter(
  callDate: string,
): { year: number; quarter: number } {
  const parts = callDate.split("-");
  const year  = parseInt(parts[0]!, 10);
  const month = parseInt(parts[1]!, 10);

  if (isNaN(year) || isNaN(month)) {
    throw new Error(
      `[TranscriptProvider] Invalid callDate "${callDate}" — expected ISO-8601 (YYYY-MM-DD).`,
    );
  }

  if (month <= 3)  return { year: year - 1, quarter: 4 };
  if (month <= 6)  return { year,           quarter: 1 };
  if (month <= 9)  return { year,           quarter: 2 };
  return               { year,           quarter: 3 };
}

function isFmpErrorEnvelope(body: unknown): boolean {
  return (
    body !== null &&
    typeof body === "object" &&
    !Array.isArray(body) &&
    "Error Message" in (body as object)
  );
}
