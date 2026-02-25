#!/usr/bin/env python3
"""Sanity-check for the SQLite storage layer.

Initialises the database, inserts synthetic committee results for a
fake earnings call, then dumps both tables so you can verify the schema
and aggregation logic at a glance.
"""

from __future__ import annotations

import sqlite3

from storage import (
    DB_PATH,
    get_conn,
    init_db,
    insert_company_summary,
    insert_questions,
)

# Synthetic committee output matching the shape produced by
# analyze_questions_with_committee().
_FAKE_RESULTS: list[dict] = [
    {
        "id": "q1",
        "question": "How should we think about iPhone margins?",
        "praise": {"score": 0.65, "rationale": "Positive on execution"},
        "skeptic": {"score": 0.15, "rationale": "Minor cost concern", "risk_vectors": []},
        "neutral": {"score": 0.20, "rationale": "Partly informational"},
        "final": {"label": "PraiseSupport", "tone_score": 0.50, "disagreement": False},
    },
    {
        "id": "q2",
        "question": "Why the deceleration in China?",
        "praise": {"score": 0.10, "rationale": "Minimal praise"},
        "skeptic": {"score": 0.72, "rationale": "Clear concern on China", "risk_vectors": ["china_macro", "competitive_threat"]},
        "neutral": {"score": 0.18, "rationale": "Some factual element"},
        "final": {"label": "SkepticismDisappointment", "tone_score": -0.62, "disagreement": False},
    },
    {
        "id": "q3",
        "question": "Can you quantify the FX headwind?",
        "praise": {"score": 0.05, "rationale": "No praise"},
        "skeptic": {"score": 0.10, "rationale": "No skepticism"},
        "neutral": {"score": 0.85, "rationale": "Purely factual ask"},
        "final": {"label": "Neutral", "tone_score": -0.05, "disagreement": False},
    },
    {
        "id": "q4",
        "question": "Impressive services growth — but is the take rate sustainable?",
        "praise": {"score": 0.55, "rationale": "Acknowledges strong growth"},
        "skeptic": {"score": 0.48, "rationale": "Questions sustainability", "risk_vectors": ["take_rate_pressure"]},
        "neutral": {"score": 0.12, "rationale": "Low neutrality"},
        "final": {"label": "PraiseSupport", "tone_score": 0.07, "disagreement": True},
    },
]

_RUN_ID = "debug-001"
_TICKER = "AAPL"
_CALL_DATE = "2025-02-01"


def _dump_table(table: str, limit: int = 5) -> None:
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table} LIMIT {limit}")  # noqa: S608
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"  (no rows in {table})")
        return

    cols = rows[0].keys()
    header = " | ".join(f"{c:>22}" for c in cols)
    print(f"  {header}")
    print(f"  {'-' * len(header)}")
    for row in rows:
        vals = " | ".join(f"{str(row[c]):>22}" for c in cols)
        print(f"  {vals}")


def main() -> None:
    print(f"Initialising database at {DB_PATH} …")
    init_db()

    print(f"Inserting {len(_FAKE_RESULTS)} question rows …")
    insert_questions(_RUN_ID, _TICKER, _CALL_DATE, _FAKE_RESULTS)

    print("Computing & inserting company summary …\n")
    insert_company_summary(_RUN_ID, _TICKER, _CALL_DATE, _FAKE_RESULTS)

    print("=== company_summary ===")
    _dump_table("company_summary")
    print()
    print("=== questions ===")
    _dump_table("questions")


if __name__ == "__main__":
    main()
