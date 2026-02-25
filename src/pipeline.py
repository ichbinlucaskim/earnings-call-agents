"""Transcript ingestion pipeline.

Loads local earnings-call transcript JSON files and extracts structured
Q&A pairs for downstream tone analysis by the agent committee.  Each
transcript is expected to live under ``data/transcripts/{ticker}/{date}.json``
and follow the segment-based schema described in the project README.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

# Pattern that signals the start of the Q&A portion of the call.
_QA_START_RE = re.compile(
    r"q\s*&\s*a|question[\s-]*and[\s-]*answer",
    re.IGNORECASE,
)

_REQUIRED_TOP_KEYS = {"ticker", "call_date", "segments"}


def load_transcript(path: str) -> dict:
    """Read an earnings-call transcript JSON file and return it as a dict.

    Parameters
    ----------
    path : str
        Filesystem path to the transcript JSON file.

    Returns
    -------
    dict
        The parsed transcript containing ``ticker``, ``call_date``, and
        a ``segments`` list.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the JSON structure is missing required keys or ``segments``
        is not a list.
    """
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"Transcript file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Transcript root must be a JSON object, got {type(data).__name__}")

    missing = _REQUIRED_TOP_KEYS - data.keys()
    if missing:
        raise ValueError(f"Transcript is missing required keys: {sorted(missing)}")

    if not isinstance(data["segments"], list):
        raise ValueError(
            f"'segments' must be a list, got {type(data['segments']).__name__}"
        )

    return data


def extract_qa_pairs(transcript: dict) -> List[Dict]:
    """Extract structured Q&A pairs from a loaded transcript.

    Scans the ``segments`` list sequentially, skipping everything before
    the operator announces Q&A (detected via phrases like *"Q&A"* or
    *"question and answer"*).  Once in Q&A mode, analyst segments open
    new questions and consecutive management segments are concatenated
    into the answer.

    Parameters
    ----------
    transcript : dict
        Structured transcript as returned by :func:`load_transcript`.

    Returns
    -------
    list[dict]
        Each element is a dict with keys:

        - ``id`` — sequential id like ``"q1"``, ``"q2"``, …
        - ``question_text`` — the analyst's question text.
        - ``analyst_name`` — name of the analyst.
        - ``answer_text`` — concatenated management response(s).
    """
    segments: list = transcript["segments"]

    qa_pairs: List[Dict] = []
    in_qa = False
    current: Dict | None = None
    question_index = 0

    for segment in segments:
        role: str = segment.get("role", "")
        text: str = segment.get("text", "")
        speaker: str = segment.get("speaker", "")

        # Detect Q&A start from an operator segment.
        if role == "operator" and not in_qa:
            if _QA_START_RE.search(text):
                in_qa = True
            continue

        if not in_qa:
            continue

        # --- inside Q&A ---

        if role == "analyst":
            # Close any open question before starting a new one.
            if current is not None:
                qa_pairs.append(current)

            question_index += 1
            current = {
                "id": f"q{question_index}",
                "question_text": text,
                "analyst_name": speaker,
                "answer_text": "",
            }

        elif role == "management" and current is not None:
            if current["answer_text"]:
                current["answer_text"] += " " + text
            else:
                current["answer_text"] = text

        # Operator segments during Q&A are ignored (e.g. "next question").

    # Close the last open question if any.
    if current is not None:
        qa_pairs.append(current)

    return qa_pairs
