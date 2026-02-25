"""SQLite persistence layer for the Earnings-Call Tone Committee.

Two tables:

* **questions** — one row per analyst question.  Stores the per-agent
  scores (praise, skeptic, neutral), the aggregator's final label and
  composite tone_score, and a disagreement flag.  Keyed by
  (run_id, ticker, call_date, question_id).

* **company_summary** — one row per earnings call.  Stores ratio-based
  aggregates (support_ratio, skeptic_ratio, neutral_ratio), a composite
  tone_index (support_ratio − skeptic_ratio), and the fraction of
  questions where the committee disagreed.  Intended for dashboards and
  cross-company comparison.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List

DB_PATH: Path = Path("db.sqlite3")

_CREATE_QUESTIONS = """\
CREATE TABLE IF NOT EXISTS questions (
    run_id          TEXT,
    ticker          TEXT,
    call_date       TEXT,
    question_id     TEXT,
    question_text   TEXT,
    label           TEXT,
    tone_score      REAL,
    praise_score    REAL,
    skeptic_score   REAL,
    neutrality_score REAL,
    disagreement    INTEGER
);
"""

_CREATE_COMPANY_SUMMARY = """\
CREATE TABLE IF NOT EXISTS company_summary (
    run_id                  TEXT,
    ticker                  TEXT,
    call_date               TEXT,
    support_ratio           REAL,
    skeptic_ratio           REAL,
    neutral_ratio           REAL,
    tone_index              REAL,
    num_questions           INTEGER,
    high_disagreement_ratio REAL
);
"""


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection to *db_path* (default :data:`DB_PATH`).

    Parameters
    ----------
    db_path : Path | str | None
        Override for the database file location.
    """
    path = Path(db_path) if db_path is not None else DB_PATH
    return sqlite3.connect(str(path))


def init_db(db_path: Path | str | None = None) -> None:
    """Create the ``questions`` and ``company_summary`` tables if absent.

    Safe to call repeatedly — uses ``CREATE TABLE IF NOT EXISTS``.

    Parameters
    ----------
    db_path : Path | str | None
        Override for the database file location.
    """
    conn = get_conn(db_path)
    try:
        conn.execute(_CREATE_QUESTIONS)
        conn.execute(_CREATE_COMPANY_SUMMARY)
        conn.commit()
    finally:
        conn.close()


def insert_questions(
    run_id: str,
    ticker: str,
    call_date: str,
    results: List[Dict],
    *,
    db_path: Path | str | None = None,
) -> None:
    """Persist per-question tone scores produced by the agent committee.

    Parameters
    ----------
    run_id : str
        Unique identifier for this pipeline run (e.g. a UUID or timestamp).
    ticker : str
        Company ticker symbol (e.g. ``"AAPL"``).
    call_date : str
        ISO-8601 date of the earnings call (e.g. ``"2025-02-01"``).
    results : list[dict]
        Output of :func:`src.agents.analyze_questions_with_committee`.
        Each dict must contain ``id``, ``praise``, ``skeptic``, ``neutral``,
        and ``final`` sub-dicts.
    db_path : Path | str | None
        Override for the database file location.
    """
    conn = get_conn(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO questions
                (run_id, ticker, call_date, question_id, question_text,
                 label, tone_score, praise_score, skeptic_score,
                 neutrality_score, disagreement)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    ticker,
                    call_date,
                    r["id"],
                    r.get("question", ""),
                    r["final"]["label"],
                    r["final"]["tone_score"],
                    r["praise"]["score"],
                    r["skeptic"]["score"],
                    r["neutral"]["score"],
                    1 if r["final"]["disagreement"] else 0,
                )
                for r in results
            ],
        )
        conn.commit()
    finally:
        conn.close()


def insert_company_summary(
    run_id: str,
    ticker: str,
    call_date: str,
    results: List[Dict],
    *,
    db_path: Path | str | None = None,
) -> None:
    """Compute and persist a company-level tone summary for one earnings call.

    Aggregated metrics:

    * **support_ratio** — fraction of questions labelled *PraiseSupport*.
    * **skeptic_ratio** — fraction labelled *SkepticismDisappointment*.
    * **neutral_ratio** — remaining fraction.
    * **tone_index** — ``support_ratio − skeptic_ratio`` (range −1 to +1).
    * **high_disagreement_ratio** — fraction where committee disagreed.

    Parameters
    ----------
    run_id : str
        Unique identifier for this pipeline run.
    ticker : str
        Company ticker symbol.
    call_date : str
        ISO-8601 date of the earnings call.
    results : list[dict]
        Same list passed to :func:`insert_questions`.
    db_path : Path | str | None
        Override for the database file location.
    """
    n = len(results)
    if n == 0:
        return

    num_praise = sum(1 for r in results if r["final"]["label"] == "PraiseSupport")
    num_skeptic = sum(
        1 for r in results if r["final"]["label"] == "SkepticismDisappointment"
    )
    num_neutral = n - num_praise - num_skeptic

    support_ratio = num_praise / n
    skeptic_ratio = num_skeptic / n
    neutral_ratio = num_neutral / n
    tone_index = support_ratio - skeptic_ratio

    high_disagree = sum(1 for r in results if r["final"]["disagreement"])
    high_disagreement_ratio = high_disagree / n

    conn = get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO company_summary
                (run_id, ticker, call_date, support_ratio, skeptic_ratio,
                 neutral_ratio, tone_index, num_questions,
                 high_disagreement_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ticker,
                call_date,
                support_ratio,
                skeptic_ratio,
                neutral_ratio,
                tone_index,
                n,
                high_disagreement_ratio,
            ),
        )
        conn.commit()
    finally:
        conn.close()
