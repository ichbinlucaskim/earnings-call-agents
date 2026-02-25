# earnings-call-agents

A multi-agent pipeline that fetches a live earnings calendar, filters it to
NYSE- and NASDAQ-listed companies, retrieves earnings call transcripts, and
runs LLM-based analysis to produce a structured earnings digest. The pipeline
is intentionally modular: the calendar source, exchange filter, and transcript
provider are independent layers that can each be swapped or extended without
touching the analysis stage.

## Assignment requirements

| Requirement | How this repo satisfies it |
|---|---|
| Real external earnings calendar | FMP `stable/earnings-calendar` — one API call per date window, live data, no hard-coded tickers |
| Data-driven US universe filter | FMP `stable/stock-list` (~50 k symbols with exchange tags) cross-referenced at runtime |
| Transcript retrieval | `NinjasTranscriptProvider` implementing the `TranscriptProvider` interface (API Ninjas) |
| Agentic LLM analysis | Filtered `EarningsEvent[]` + `Transcript.text` strings fed into the LLM agent layer |

Nothing in the pipeline is hard-coded. The set of companies that reach the
analysis stage is determined at runtime by crossing two live external data
sources — the FMP earnings calendar and the FMP symbol/exchange snapshot.

## Setup

```zsh
cd fmp
npm install

export FMP_API_KEY=your_fmp_key
export NINJAS_API_KEY=your_ninjas_key
```

## Run

```zsh
npx tsx src/run.ts --from 2026-02-24 --to 2026-02-28
```

Both flags are optional; the values above are the defaults.

## What to expect

The output below is illustrative — exact symbol counts, transcript lengths,
and cache state will vary with the date range and live API data.

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

On the second run of the week, the exchange map is served from disk:

```
[FMP] Symbol→exchange map: loaded from cache (52,341 symbols, cached 2h ago).
```

## Further reading

- [`docs/workflow.md`](docs/workflow.md) — end-to-end multi-agent workflow and extension guide
- [`fmp/`](fmp/) — TypeScript data layer (`fmp-earnings.ts`, `transcript-provider.ts`, `run.ts`)
