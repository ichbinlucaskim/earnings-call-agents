"""Earnings discovery and transcript fetching layer.

Pulls the last week's earnings events from the API Ninjas earnings-calendar
endpoint, fetches each company's call transcript from the API Ninjas
earnings-transcript endpoint, adapts the raw response into the local segment
schema, and writes the result to ``data/transcripts/{ticker}/{call_date}.json``
so that the existing ``run_local.py`` pipeline can process it.

Required environment variables::

    NINJAS_API_KEY   — API Ninjas API key

Usage::

    python -m src.discover_earnings
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = Path("data/transcripts")

_NINJAS_CALENDAR_URL = (
    "https://api.api-ninjas.com/v1/earningscalendar"
)
_NINJAS_TRANSCRIPT_URL = (
    "https://api.api-ninjas.com/v1/earningstranscript"
)

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def get_last_week_window() -> Tuple[str, str]:
    """Return ``(from_date, to_date)`` as ISO strings for the last 7 days.

    *to_date* is today; *from_date* is 7 calendar days ago.
    """
    today = date.today()
    from_date = today - timedelta(days=7)
    return from_date.isoformat(), today.isoformat()


def _date_to_year_quarter(call_date: str) -> Tuple[int, int]:
    """Convert an ISO date string to ``(year, quarter)``.

    The *fiscal* quarter is approximated from the calendar month of the
    earnings call.  Because calls happen *after* the quarter ends, a
    call in January–March usually reports on the prior quarter (Q4 of
    the previous year).  We use a simple mapping:

    * Call in Jan–Mar → reports Q4 of (year − 1)
    * Call in Apr–Jun → reports Q1
    * Call in Jul–Sep → reports Q2
    * Call in Oct–Dec → reports Q3
    """
    d = date.fromisoformat(call_date)
    month = d.month
    if month <= 3:
        return d.year - 1, 4
    elif month <= 6:
        return d.year, 1
    elif month <= 9:
        return d.year, 2
    else:
        return d.year, 3


# ---------------------------------------------------------------------------
# API Ninjas: earnings calendar
# ---------------------------------------------------------------------------


def fetch_earnings_calendar(from_date: str, to_date: str) -> List[Dict]:
    """Fetch earnings events between *from_date* and *to_date* (inclusive).

    Uses the API Ninjas ``/earningscalendar`` endpoint, which accepts a
    single ``date`` parameter.  We iterate day-by-day over the range and
    combine the results into one list.

    Parameters
    ----------
    from_date, to_date : str
        ISO-8601 date strings (e.g. ``"2025-02-01"``).

    Returns
    -------
    list[dict]
        Each dict contains at least ``ticker`` and ``date``.

    Raises
    ------
    EnvironmentError
        If ``NINJAS_API_KEY`` is not set.
    requests.HTTPError
        If any daily request fails.
    """
    api_key = os.environ.get("NINJAS_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "NINJAS_API_KEY is not set. Export it or add it to your .env file."
        )

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    events: List[Dict] = []
    cur = start

    while cur <= end:
        resp = requests.get(
            _NINJAS_CALENDAR_URL,
            params={"date": cur.isoformat()},
            headers={"X-Api-Key": api_key},
            timeout=15,
        )
        resp.raise_for_status()

        day_events = resp.json()
        if isinstance(day_events, list):
            events.extend(day_events)

        cur += timedelta(days=1)

    return events


# ---------------------------------------------------------------------------
# API Ninjas: transcript fetch
# ---------------------------------------------------------------------------


def fetch_raw_transcript(ticker: str, call_date: str) -> Dict[str, Any]:
    """Fetch an earnings-call transcript from API Ninjas.

    Parameters
    ----------
    ticker : str
        Company ticker symbol (e.g. ``"AAPL"``).
    call_date : str
        ISO-8601 date of the earnings call, used to derive year/quarter.

    Returns
    -------
    dict
        The parsed JSON response from the transcript API.

    Raises
    ------
    EnvironmentError
        If ``NINJAS_API_KEY`` is not set.
    requests.HTTPError
        If the HTTP request fails.
    """
    api_key = os.environ.get("NINJAS_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "NINJAS_API_KEY is not set. Export it or add it to your .env file."
        )

    year, quarter = _date_to_year_quarter(call_date)

    resp = requests.get(
        _NINJAS_TRANSCRIPT_URL,
        params={"ticker": ticker, "year": year, "quarter": quarter},
        headers={"X-Api-Key": api_key},
        timeout=60,
    )
    resp.raise_for_status()

    data = resp.json()
    if isinstance(data, list):
        # The API may return a list; take the first element.
        if not data:
            raise ValueError(
                f"Empty transcript response for {ticker} {year}Q{quarter}"
            )
        return data[0]
    return data


# ---------------------------------------------------------------------------
# Adapter: raw transcript → local segment schema
# ---------------------------------------------------------------------------

# Heuristic patterns for guessing speaker role from their title / context
# when the structured ``speaker_type`` field is not available.
_MGMT_PATTERNS = re.compile(
    r"\b(ceo|cfo|coo|cto|president|chairman|chief|evp|svp|vp|"
    r"director|head of|treasurer|controller|secretary|ir\b|"
    r"investor relations)\b",
    re.IGNORECASE,
)
_OPERATOR_PATTERNS = re.compile(
    r"\b(operator|moderator|conference|coordinator)\b",
    re.IGNORECASE,
)


def _guess_role(speaker: str, role_or_title: str) -> str:
    """Best-effort mapping of a speaker to management|analyst|operator."""
    combined = f"{speaker} {role_or_title}"
    if _OPERATOR_PATTERNS.search(combined):
        return "operator"
    if _MGMT_PATTERNS.search(combined):
        return "management"
    # Default assumption: if we can't tell, treat as analyst.
    return "analyst"


def adapt_to_segment_schema(
    raw: Dict[str, Any],
    ticker: str,
    call_date: str,
) -> Dict[str, Any]:
    """Convert a provider-specific transcript into the local schema.

    Handles two response shapes from API Ninjas:

    1. **Premium** — ``transcript_split`` is present: a list of speaker
       dicts each containing ``speaker``, ``speaker_type``, ``text``, etc.
    2. **Free tier** — only the ``transcript`` string is available.
       We split it into paragraphs and apply heuristic role guessing.

    Parameters
    ----------
    raw : dict
        Raw JSON response from :func:`fetch_raw_transcript`.
    ticker, call_date : str
        Injected into the top-level output for consistency.

    Returns
    -------
    dict
        ``{"ticker": …, "call_date": …, "segments": […]}``
    """
    segments: List[Dict[str, str]] = []

    # --- Path A: structured transcript_split (premium) ---
    split = raw.get("transcript_split") or raw.get("transcript_by_speaker")
    if isinstance(split, list) and split:
        for entry in split:
            speaker = entry.get("speaker") or entry.get("name") or "Unknown"
            text = (entry.get("text") or "").strip()
            if not text:
                continue

            # ``speaker_type`` is the canonical role from API Ninjas:
            # "management", "investor", or "operator".
            raw_type = (entry.get("speaker_type") or "").lower()
            if raw_type == "investor":
                role = "analyst"
            elif raw_type in ("management", "operator"):
                role = raw_type
            else:
                role = _guess_role(speaker, entry.get("role", ""))

            segments.append({"speaker": speaker, "role": role, "text": text})

        return {"ticker": ticker, "call_date": call_date, "segments": segments}

    # --- Path B: plain-text transcript (free tier) ---
    transcript_text: str = raw.get("transcript", "")
    if not transcript_text:
        raise ValueError(
            f"Transcript response for {ticker} on {call_date} contains "
            "neither 'transcript_split' nor 'transcript'."
        )

    # Many plain-text transcripts use a "Speaker Name:" prefix.
    # We split on that pattern.
    chunk_re = re.compile(r"^([A-Z][A-Za-z .'\-]+):\s*", re.MULTILINE)
    parts = chunk_re.split(transcript_text)

    # parts alternates: [preamble, speaker1, text1, speaker2, text2, …]
    if len(parts) >= 3:
        # Skip the preamble (parts[0]).
        for i in range(1, len(parts) - 1, 2):
            speaker = parts[i].strip()
            text = parts[i + 1].strip()
            if not text:
                continue
            role = _guess_role(speaker, "")
            segments.append({"speaker": speaker, "role": role, "text": text})
    else:
        # Fallback: no speaker markers detected — treat as a single
        # management block so the file at least loads.
        segments.append({
            "speaker": "Unknown",
            "role": "management",
            "text": transcript_text.strip(),
        })

    return {"ticker": ticker, "call_date": call_date, "segments": segments}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_transcript(ticker: str, call_date: str, transcript: dict) -> Path:
    """Write a transcript JSON to ``data/transcripts/{ticker}/{call_date}.json``.

    Creates parent directories as needed.  If the file already exists it
    is **not** overwritten — the existing path is returned immediately.

    Parameters
    ----------
    ticker, call_date : str
        Used to build the directory and filename.
    transcript : dict
        The segment-schema dict to serialise.

    Returns
    -------
    Path
        The (possibly pre-existing) file path.
    """
    out_dir = DATA_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{call_date}.json"

    if out_path.exists():
        return out_path

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(transcript, fh, indent=2, ensure_ascii=False)

    return out_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _normalize_symbol(symbol: str) -> str:
    """Normalise a ticker symbol for filesystem and API use.

    Upper-cases and replaces dots with hyphens (``BRK.B`` → ``BRK-B``)
    so the symbol is safe for directory names.
    """
    return symbol.upper().replace(".", "-")


def main() -> None:
    """Discover last week's earnings and fetch transcripts.

    Steps:

    1. Compute the last-7-day date window.
    2. Fetch the API Ninjas earnings calendar for that window.
    3. De-duplicate by ticker (keep earliest date).
    4. For each event, fetch the transcript, adapt it, and save it.
    5. Print a one-line summary per file.
    """
    from dotenv import load_dotenv

    load_dotenv()  # pick up .env if present

    from_date, to_date = get_last_week_window()
    print(f"Earnings window: {from_date} → {to_date}")

    # -- Step 1: earnings calendar --
    events = fetch_earnings_calendar(from_date, to_date)
    print(f"  API Ninjas returned {len(events)} earnings event(s).")

    if not events:
        print("  Nothing to process.")
        return

    # -- Step 2: de-duplicate by ticker (keep earliest date) --
    seen: dict[str, str] = {}
    for ev in events:
        sym = ev.get("ticker", "")
        dt = ev.get("date", "")
        if sym and (sym not in seen or dt < seen[sym]):
            seen[sym] = dt

    unique_events = [{"ticker": s, "date": d} for s, d in sorted(seen.items())]
    print(f"  Unique tickers to fetch: {len(unique_events)}")

    # -- Step 3: fetch, adapt, save --
    saved = 0
    skipped = 0
    failed = 0

    for ev in unique_events:
        raw_sym = ev["ticker"]
        call_date = ev["date"]
        ticker = _normalize_symbol(raw_sym)

        # Skip if already on disk.
        target = DATA_DIR / ticker / f"{call_date}.json"
        if target.exists():
            skipped += 1
            continue

        try:
            raw = fetch_raw_transcript(raw_sym, call_date)
            transcript = adapt_to_segment_schema(raw, ticker, call_date)
            path = save_transcript(ticker, call_date, transcript)
            n_seg = len(transcript.get("segments", []))
            print(f"  ✓ {ticker:6s} {call_date}  ({n_seg} segments) → {path}")
            saved += 1
        except Exception as exc:
            print(f"  ✗ {ticker:6s} {call_date}  FAILED: {exc}")
            failed += 1

    print(
        f"\nDone. saved={saved}  skipped={skipped}  failed={failed}  "
        f"total={saved + skipped + failed}"
    )


if __name__ == "__main__":
    main()
