#!/usr/bin/env python3
"""Quick sanity-check for the transcript extraction pipeline.

Loads ``data/transcripts/sample.json``, extracts Q&A pairs, and prints
them in a human-readable format.  Intended for local debugging only.
"""

from __future__ import annotations

from pathlib import Path

from pipeline import extract_qa_pairs, load_transcript

_SAMPLE_PATH = Path("data/transcripts/sample.json")


def main() -> None:
    print(f"Loading transcript from {_SAMPLE_PATH} …")
    transcript = load_transcript(str(_SAMPLE_PATH))
    print(f"Ticker: {transcript['ticker']}  Date: {transcript['call_date']}")
    print(f"Total segments: {len(transcript['segments'])}\n")

    qa_pairs = extract_qa_pairs(transcript)
    print(f"Extracted {len(qa_pairs)} Q&A pair(s):\n")

    for pair in qa_pairs:
        print(f"[{pair['id'].upper()}]  Analyst: {pair['analyst_name']}")
        print(f"  Q: {pair['question_text']}")
        print(f"  A: {pair['answer_text'] or '(no answer recorded)'}")
        print("-" * 60)


if __name__ == "__main__":
    main()
