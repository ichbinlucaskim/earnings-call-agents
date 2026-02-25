# Data Layer — Architecture & Design

## Assignment fit

The assignment requires four things: a real earnings calendar from an external
API; a data-driven filter to the NYSE/NASDAQ universe; transcript retrieval
for filtered symbols; and an agentic LLM analysis pipeline over those
transcripts. Every design decision below maps directly to one of those
requirements.

| Assignment requirement | How it is satisfied |
|---|---|
| Real external earnings calendar | FMP `stable/earnings-calendar` — one API call, live data, any date window |
| Data-driven US universe filter | FMP `stable/stock-list` snapshot (~50 k symbols with exchange tags) — not a hand-picked ticker list |
| Transcript retrieval | `TranscriptProvider` interface, wired to API Ninjas for this submission |
| Agentic analysis | Filtered events + `Transcript.text` strings flow into the LLM agent layer |

Nothing in the pipeline is hard-coded. The set of companies that reach the
analysis stage is determined at runtime by crossing two live data sources —
the calendar and the exchange snapshot.

---

## Endpoint selection

**`GET /stable/earnings-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD&apikey=`**

This is the only calendar source. A single HTTP call returns every scheduled
earnings event in the requested window as a flat JSON array. Each record
carries `symbol`, `date`, `epsActual/Estimated`, and `revenueActual/Estimated`.
There is no `exchange` or `country` field — that gap is addressed by the
exchange oracle below. No pagination is required; FMP returns the full window
in one response.

**`GET /stable/stock-list?apikey=`**

This is the exchange oracle — the mechanism that makes the US-universe filter
data-driven rather than a hard-coded list. A single call returns roughly
50 000 records, one per symbol FMP tracks globally. Each record includes a
short exchange identifier alongside the ticker.

A note on the field name: the canonical key in the stable API is
`exchangeShortName` (camelCase). However, FMP has shipped schema variations
over time, and `exchange_short_name` (snake_case) and `exchange` appear in
older response shapes and some documentation. `buildSymbolToExchangeMap`
checks all three in priority order — `exchangeShortName` → `exchange_short_name`
→ `exchange` — so a naming drift in a future FMP release won't silently empty
the map. Both formats produce uppercase values like `"NYSE"`, `"NASDAQ"`,
`"AMEX"`, `"TSX"`, `"EURONEXT"`.

Because the symbol list changes slowly (new IPOs, delistings), the result is
persisted to `.fmp-symbol-cache.json` with a seven-day TTL. On a warm cache
the exchange-filter step costs zero API calls.

---

## Call budget

```
First run of the week (cold cache):
  1 × stable/earnings-calendar   —  calendar for the requested window
  1 × stable/stock-list          —  builds and persists the exchange map
  ─────────────────────────────────
  2 total calls

Every subsequent run the same week (warm cache):
  1 × stable/earnings-calendar
  0 × stock-list                 —  served from .fmp-symbol-cache.json
  ─────────────────────────────────
  1 total call

Worst case — cold cache, one run per day, seven days:
  7 × earnings-calendar  +  1 × stock-list  =  8 calls/week
  ─────────────────────────────────────────────────────────
  Well within FMP's free-tier daily call limit
  (approximately 250 calls/day — verify the current figure
  in FMP's pricing documentation).
```

This budget leaves headroom for ad-hoc debugging, re-runs, and the eventual
addition of company-profile or financial-statement calls without approaching
the daily ceiling.

---

## Multi-agent flow

```
┌──────────────────────────────────────────────────────────┐
│ Startup: buildSymbolToExchangeMap(FMP_API_KEY)           │
│   → disk cache hit (0 calls) or stock-list fetch (1 call)│
│   → Map<symbol, exchange>  held in memory for the run    │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────┐   1 FMP call / run
│  Calendar Agent                     │
│  fetchFmpEarningsCalendar(          │
│    from, to, FMP_API_KEY)           │
│  → EarningsEvent[]                  │
│    (all events in window,           │
│     all exchanges globally)         │
└──────────────┬──────────────────────┘
               │ EarningsEvent[]
               ▼
┌─────────────────────────────────────┐   0 calls (pure in-memory)
│  Exchange Filter                    │
│  filterNyseNasdaq(                  │
│    events, symbolToExchange)        │
│  → EarningsEvent[]                  │
│    (NYSE + NASDAQ only)             │
│  logs: "42 NYSE/NASDAQ, 317 dropped"│
└──────────────┬──────────────────────┘
               │ filtered EarningsEvent[]
               ▼
┌─────────────────────────────────────┐   1 call / symbol (API Ninjas)
│  Transcript Agent                   │
│  provider.fetchTranscript(          │
│    symbol, reportDate)              │
│  → Transcript { symbol, callDate,   │
│                 text, raw }         │
│  provider = NinjasTranscriptProvider│  ← active for this submission
│           | FmpTranscriptProvider   │  ← implemented, paid key required
└──────────────┬──────────────────────┘
               │ Transcript[]
               ▼
┌─────────────────────────────────────┐
│  Analysis Agents                    │
│  LLM(transcript.text)               │
│  → structured report                │
└─────────────────────────────────────┘
```

---

## Code walkthrough

### `fmp/src/fmp-earnings.ts`

#### `fetchFmpEarningsCalendar(fromDate, toDate, apiKey)`

Calls `stable/earnings-calendar` and normalises the array response into typed
`EarningsEvent` objects. The FMP `date` field becomes `reportDate`; numeric
fields are coerced to `number | null` via a `toNumberOrNull` helper that
guards against FMP returning empty strings or non-finite values. Records
missing a `symbol` or `date` are warned and skipped rather than passed
silently downstream. The original FMP record is preserved in `raw` for any
downstream agent that wants the unprocessed payload.

The internal `fetchJson` helper wraps every FMP call with a three-attempt
retry policy (initial + two retries). Retries apply only to `429` and `503` —
transient, provider-side errors. The wait uses exponential back-off starting
at 500 ms with ±20% jitter to avoid thundering-herd retries. All other non-2xx
statuses throw immediately. A separate check detects FMP's application-level
error envelope: the API occasionally returns `HTTP 200` with body
`{ "Error Message": "Invalid API KEY." }` for bad credentials or locked
endpoints — without this check that silently becomes an empty event list.
API keys are redacted from all error messages before they propagate.

#### `buildSymbolToExchangeMap(apiKey)`

Calls `stable/stock-list` once and builds a `Map<string, string>` where both
key (symbol) and value (exchange) are uppercased. This map is the mechanism
that makes the assignment's "NYSE/NASDAQ universe" requirement data-driven: it
covers ~50 000 symbols from every exchange FMP tracks globally, so the filter
step is cross-referencing live reference data, not a curated list.

Field-name handling: the function checks `exchangeShortName` first, falls back
to `exchange_short_name`, then `exchange`. If the built map is empty after
iterating the entire response, it throws with a diagnostic message suggesting
the response shape may have changed — preferable to silently allowing every
event through.

Cache behaviour on startup:

- File absent → fetch from API, write cache.
- File present, age < 7 days → return immediately (no API call).
- File present, age ≥ 7 days → log "stale", fetch from API, overwrite.
- File present but corrupt JSON → log "corrupt", fetch from API, overwrite.

Cache write failures are logged as warnings but are non-fatal: the map is
returned in memory and the next run simply rebuilds it.

#### `filterNyseNasdaq(events, symbolToExchange, { keepUnknown? })`

A pure O(n) function — no I/O, no async. For each event it looks up the
uppercased symbol in the exchange map. If the exchange is `"NYSE"` or
`"NASDAQ"` the event is kept. Symbols absent from the map are dropped by
default; they are most likely OTC instruments, foreign-primary listings, or
very recent IPOs whose entries haven't propagated to the stock-list snapshot
yet. `keepUnknown: true` retains them for manual inspection without changing
any other behaviour.

After filtering, the function logs a one-line summary — e.g.
`[FMP] Filter: 42 NYSE/NASDAQ, 317 dropped (of 359 total events)` — which
makes it immediately obvious in run logs that the filter is doing real work
and is not trivially passing everything through.

#### `fetchUsEarningsCalendar(fromDate, toDate, apiKey, options?)`

The convenience entry point for the Calendar Agent. It runs
`fetchFmpEarningsCalendar` and `buildSymbolToExchangeMap` in parallel via
`Promise.all` — on a warm cache the map loads from disk in milliseconds while
the calendar call is in flight. The return value is
`{ events, symbolToExchange }`: the filtered event list for the Transcript
Agent, plus the map itself for any downstream agent that wants to log or audit
exchange metadata.

Together, these four functions give the Calendar Agent a single call that
covers the complete path from raw FMP data to a clean NYSE/NASDAQ event list
— with no hard-coded tickers anywhere in the chain.

---

### `fmp/src/transcript-provider.ts`

#### `Transcript`

```ts
type Transcript = {
  symbol: string;
  callDate: string;  // ISO-8601
  text: string;      // full transcript, LLM-ready
  raw: any;          // original provider response
};
```

`text` is a flat string because that is what LLM calls consume. Speaker-level
diarisation, if needed by an analysis agent, is available through `raw`
without any interface change.

#### `TranscriptProvider` interface

The interface exposes a single method:
`fetchTranscript(symbol, callDate) → Promise<Transcript>`. All call sites in
the pipeline hold a reference of this type, not a concrete class. The
`callDate` is always an ISO-8601 string; the provider is responsible for
mapping it to its own year/quarter parameterisation internally. This means
the Calendar Agent can pass `ev.reportDate` directly without knowing anything
about provider-specific API shapes.

#### `TranscriptNotFoundError`

A dedicated error subclass for the common case where a transcript simply does
not exist for a given symbol/quarter. Callers can catch this specifically and
log-and-skip without treating it as a pipeline failure, while unexpected
network or parsing errors propagate normally. This distinction matters at
scale: in a weekly run across 40–80 symbols, a handful of missing transcripts
is routine, not alarming.

#### `NinjasTranscriptProvider` — active for this submission

Handles both response shapes the API can return depending on account tier:

- **Free:** a `transcript` string field — used directly as `text`.
- **Premium:** a `transcript_split` array of `{ speaker, text }` objects —
  joined with `"\n\n"` into a single string, preserving speaker-turn structure
  that the LLM can read naturally.

The original parsed response is always preserved in `raw`. The constructor
rejects a missing key immediately at construction time (not at first call), so
misconfiguration surfaces before any network traffic is attempted.

#### `FmpTranscriptProvider` — implemented, not active

Targets FMP's
`GET /stable/earning-call-transcript?symbol=&year=&quarter=&apikey=`, which
returns an array whose first element has a `content` field. It is fully
implemented and follows the same error-handling conventions as
`fmp-earnings.ts` — including the `{"Error Message": "..."}` envelope check
— but requires a paid FMP key. It is not wired into the pipeline for this
submission.

---

## Migrating from API Ninjas to FMP transcripts

When a paid FMP key is available, the migration is a one-line change at the
injection site:

```ts
// Current submission:
const transcripts: TranscriptProvider = new NinjasTranscriptProvider(
  process.env.NINJAS_API_KEY!,
);

// After upgrading:
const transcripts: TranscriptProvider = new FmpTranscriptProvider(
  process.env.FMP_API_KEY!,
);
```

Every other file in the pipeline — the Calendar Agent, the Analysis Agents,
the storage layer — holds a `TranscriptProvider` reference and receives the
same `Transcript` shape regardless of which class backs it. No other changes
are required.

This pattern also makes it straightforward to run a fallback strategy if
desired: catch `TranscriptNotFoundError` from the FMP provider and retry with
the Ninjas provider before giving up. Because both implement the same
interface, the fallback is four lines of code at the call site.

---

## Running the pipeline

**1. Install**

```zsh
cd fmp && npm install
```

**2. Set credentials**

```zsh
export FMP_API_KEY=your_fmp_key
export NINJAS_API_KEY=your_ninjas_key
```

**3. Run**

```zsh
npx tsx src/run.ts --from 2026-02-24 --to 2026-02-28
```

**Example output** (exact counts vary with the date range and live data):

```
╔══════════════════════════════════════╗
║    FMP → Ninjas Earnings Pipeline    ║
╚══════════════════════════════════════╝
  Window : 2026-02-24  →  2026-02-28

[Calendar] Fetching FMP earnings calendar and exchange map…
[FMP] Symbol→exchange map: cache miss — fetching /stable/stock-list…
[FMP] Symbol→exchange map: built and cached (52,341 symbols → .fmp-symbol-cache.json).
[FMP] Filter: 42 NYSE/NASDAQ, 317 dropped (of 359 total events).
[Calendar] Processing 42 event(s).

[Transcript] ✓  NVDA   2026-02-25  length=45,231
[Transcript] ✓  ZM     2026-02-25  length=38,109
[Transcript] -  CPRX   2026-02-25  no transcript available (skipped)
...

┌─────────────────────────────┐
│          Summary            │
├─────────────────────────────┤
│  Events in window  :   42   │
│  Transcripts OK    :   35   │
│  Skipped (no data) :    7   │
│  Errors            :    0   │
└─────────────────────────────┘
```

On the second run of the week the exchange map is served from disk:

```
[FMP] Symbol→exchange map: loaded from cache (52,341 symbols, cached 2h ago).
```

---

## Three things worth knowing

**Imports use `.js` extensions.**
The package is `"type": "module"` with `"moduleResolution": "NodeNext"`, so
TypeScript requires explicit `.js` extensions in import paths. `tsx`
transparently resolves `./fmp-earnings.js` to the actual `fmp-earnings.ts`
source at runtime — no compiled output needed.

**`@types/node` is required.**
The package must be listed in `devDependencies` for TypeScript to resolve Node
built-ins (`fs`, `path`, `process`). If you see `Cannot find name 'process'`
errors, run `npm install --save-dev @types/node`.

**Transcripts run sequentially.**
API Ninjas enforces per-minute rate limits (exact thresholds depend on their
current documentation). Sequential fetching is intentional — it avoids
bursting and keeps logs easy to read during a demo. If you need faster
throughput on a paid key, replace the `for...of` loop with a `Promise.all`
over batches of ~5.
