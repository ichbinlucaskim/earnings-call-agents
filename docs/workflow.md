# Multi-Agent Workflow

This document describes the end-to-end pipeline: what each agent does, what
data it produces, and where external API calls happen versus pure in-memory
work. It is intended for an engineer who wants to understand, extend, or
replace a stage.

---

## Overview

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
┌─────────────────────────────────────┐   0 calls — pure in-memory
│  Exchange Filter                    │
│  filterNyseNasdaq(                  │
│    events, symbolToExchange)        │
│  → EarningsEvent[]  (NYSE + NASDAQ) │
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
│           | FmpTranscriptProvider   │  ← implemented; paid key required
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

## Stage-by-stage breakdown

### Startup — exchange map

Before the pipeline runs, `buildSymbolToExchangeMap(apiKey)` (in
`fmp/src/fmp-earnings.ts`) is called. It fetches FMP's
`stable/stock-list` endpoint once, iterates ~50 000 records, and
builds a `Map<symbol, exchange>` in memory. The result is persisted to
`.fmp-symbol-cache.json` with a seven-day TTL so that subsequent runs
within the same week read from disk rather than the network.

**Output:** `Map<string, string>` — symbol (uppercase) → exchange short name
(e.g. `"NYSE"`, `"NASDAQ"`, `"AMEX"`, `"TSX"`)

**External calls:** 1 (first run of the week) or 0 (cache hit)

---

### Calendar Agent

`fetchFmpEarningsCalendar(fromDate, toDate, apiKey)` calls
`stable/earnings-calendar` and normalises the response into typed
`EarningsEvent` objects. A single HTTP call covers the full date window; no
pagination is needed.

The full FMP array is kept in `EarningsEvent.raw`, so a downstream agent can
access any field FMP adds in the future without a code change.

**Output:** `EarningsEvent[]` — every scheduled earnings event in the window,
across all exchanges globally

**External calls:** 1 per run

```ts
type EarningsEvent = {
  symbol: string;
  reportDate: string;        // ISO-8601
  epsActual: number | null;
  epsEstimated: number | null;
  revenueActual: number | null;
  revenueEstimated: number | null;
  raw: any;
};
```

---

### Exchange Filter

`filterNyseNasdaq(events, symbolToExchange, options?)` is a pure O(n)
function — no I/O, no async. It cross-references each event's symbol against
the in-memory exchange map and keeps only those mapped to `"NYSE"` or
`"NASDAQ"`.

Symbols absent from the map are dropped by default (conservative). Passing
`keepUnknown: true` retains them for debugging. After filtering, a one-line
log summarises the result, e.g.:

```
[FMP] Filter: 42 NYSE/NASDAQ, 317 dropped (of 359 total events).
```

This makes it immediately obvious the filter is doing real work, not passing
everything through.

**Output:** `EarningsEvent[]` — NYSE/NASDAQ subset only

**External calls:** 0

---

### Transcript Agent

The Transcript Agent is expressed as a `TranscriptProvider` interface with a
single method:

```ts
interface TranscriptProvider {
  fetchTranscript(symbol: string, callDate: string): Promise<Transcript>;
}
```

All pipeline call sites hold a reference to this interface, not a concrete
class. Swapping providers is a one-line change at the injection site.

**Active provider — `NinjasTranscriptProvider`**

Calls the API Ninjas `/v1/earningstranscript` endpoint. Handles both response
shapes:

- **Free tier:** a `transcript` string field, used directly as `text`.
- **Premium tier:** a `transcript_split` array of `{ speaker, text }` objects,
  joined with `"\n\n"` to preserve turn structure.

The `callDate` (ISO-8601) is mapped to a `(year, quarter)` pair internally;
the Calendar Agent simply passes `ev.reportDate` without knowing anything
about provider-specific parameterisation.

When no transcript exists for a symbol/quarter, the provider throws
`TranscriptNotFoundError`. The caller catches this specifically and logs a
warning instead of failing the run — missing transcripts are routine in any
weekly batch, not exceptional.

**Future provider — `FmpTranscriptProvider`**

Targets FMP's `stable/earning-call-transcript` endpoint. Fully implemented
and type-correct; requires a paid FMP key to activate. See the
[migration note](#migrating-transcript-providers) below.

**Output:** `Transcript[]`

```ts
type Transcript = {
  symbol: string;
  callDate: string;  // ISO-8601
  text: string;      // full transcript, LLM-ready
  raw: any;          // original provider response, unmodified
};
```

**External calls:** 1 per symbol (sequential to avoid rate-limit bursting)

---

### Analysis Agents

The Analysis Agents receive `Transcript.text` — a flat string ready for LLM
ingestion. Because `text` is provider-agnostic, the analysis stage is
completely decoupled from whether the transcript came from API Ninjas or FMP.

`Transcript.raw` is also available for agents that want speaker-level detail
(e.g. to attribute statements to the CFO specifically), without any interface
change.

**Output:** structured report (format determined by the LLM agent
implementation in `src/`)

**External calls:** varies by LLM provider / model

---

## API call summary

| Stage | Provider | Calls per run | Notes |
|---|---|---|---|
| Exchange map | FMP | 0 or 1 | 1 only on cold cache (first run of the week) |
| Calendar | FMP | 1 | covers full date window in one response |
| Exchange filter | — | 0 | pure in-memory |
| Transcripts | API Ninjas | 1 per symbol | sequential |
| Analysis | LLM provider | varies | depends on model and batching |

Total FMP calls in the worst case (cold cache, seven consecutive daily runs):
7 × calendar + 1 × stock-list = 8 calls/week, well within FMP's free-tier
daily limit (approximately 250 calls/day — verify the current figure in FMP's
pricing documentation).

---

## Migrating transcript providers

To switch from API Ninjas to FMP transcripts (once a paid FMP key is
available), change one line at the injection site in `src/run.ts`:

```ts
// Current (this submission):
const provider: TranscriptProvider = new NinjasTranscriptProvider(
  process.env.NINJAS_API_KEY!,
);

// After upgrading:
const provider: TranscriptProvider = new FmpTranscriptProvider(
  process.env.FMP_API_KEY!,
);
```

No other file changes are required. The Calendar Agent, Exchange Filter,
Analysis Agents, and any storage layer all hold a `TranscriptProvider`
reference and receive the same `Transcript` shape regardless of the concrete
class behind it.

A fallback strategy — try FMP, catch `TranscriptNotFoundError`, retry with
Ninjas — is also straightforward because both classes implement the same
interface.

---

## Extending the pipeline

| What you want to change | Where to make the change |
|---|---|
| Different calendar source | Implement a new function matching `fetchFmpEarningsCalendar`'s signature; replace the call in `fetchUsEarningsCalendar` |
| Different exchange filter (e.g. add AMEX) | Edit the `NYSE_NASDAQ` set constant in `fmp-earnings.ts` |
| Different transcript provider | Implement `TranscriptProvider`; swap at the injection site in `run.ts` |
| Richer analysis | Extend the Analysis Agent layer in `src/`; `Transcript.text` and `Transcript.raw` are already available |
| Persistent storage between runs | Add a storage step between the Transcript Agent and Analysis Agents; the `Transcript` type is the stable contract |
