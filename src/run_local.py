"""Local CLI entry-point for the Earnings-Call Tone Committee pipeline.

Walks all transcript JSON files under ``data/transcripts/``, runs the
full pipeline (load → extract Q&A → tone committee → SQLite), and
populates ``db.sqlite3`` with question-level and company-level results.

Usage::

    python -m src.run_local
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterable

from .agents import analyze_questions_with_committee
from .pipeline import extract_qa_pairs, load_transcript
from .storage import init_db, insert_company_summary, insert_questions


def iter_transcript_paths(root: str = "data/transcripts") -> Iterable[Path]:
    """Yield all ``*.json`` files under *root* in sorted order.

    Parameters
    ----------
    root : str
        Top-level directory to search (recursively).

    Yields
    ------
    Path
        Absolute paths to transcript JSON files, sorted lexicographically
        for deterministic processing order.
    """
    return sorted(Path(root).rglob("*.json"))


def process_transcript(path: Path) -> None:
    """Run the end-to-end pipeline for a single transcript file.

    Steps:

    1. Load and validate the transcript JSON.
    2. Extract analyst Q&A pairs.
    3. Score each question via the LLM tone committee.
    4. Persist question-level rows and a company summary to SQLite.

    Parameters
    ----------
    path : Path
        Path to a transcript JSON file.
    """
    run_id = str(uuid.uuid4())

    transcript = load_transcript(str(path))
    ticker = transcript["ticker"]
    call_date = transcript["call_date"]

    qa_pairs = extract_qa_pairs(transcript)
    if not qa_pairs:
        print(f"  WARN  {ticker} {call_date}: no Q&A pairs found — skipping.")
        return

    print(f"  {ticker} {call_date}: {len(qa_pairs)} questions extracted, calling committee …")
    results = analyze_questions_with_committee(qa_pairs)

    insert_questions(run_id, ticker, call_date, results)
    insert_company_summary(run_id, ticker, call_date, results)

    print(f"  {ticker} {call_date}: {len(results)} questions processed  (run_id={run_id[:8]}…)")


def main() -> None:
    """Initialise the database and process every transcript under data/transcripts/."""
    init_db()

    any_files = False
    for path in iter_transcript_paths():
        any_files = True
        process_transcript(path)

    if not any_files:
        print("No transcript files found under data/transcripts/.")


if __name__ == "__main__":
    main()
