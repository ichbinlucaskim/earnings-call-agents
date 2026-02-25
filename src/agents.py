"""LLM-backed analyst-agent committee.

Constructs a multi-persona system prompt from the tone ontology and
dispatches a batch of analyst Q&A pairs to the LLM for scoring.  Three
virtual agents — PraiseAgent, SkepticAgent, NeutralAgent — each assign a
0-to-1 score, and an Aggregator decides the final label, composite
tone_score, and a disagreement flag.
"""

from __future__ import annotations

import json
import textwrap
from typing import Dict, List

from .llm import call_llm
from .ontology import load_tone_ontology

# Module-level ontology loaded once at import time.
ontology: dict = load_tone_ontology()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _format_category_block(name: str, body: dict) -> str:
    """Render a single ontology category into a readable text block."""
    lines = [f"### {name}", f"Description: {body['description'].strip()}"]
    lines.append("Lexical cues: " + ", ".join(f'"{c}"' for c in body["lexical_cues"]))
    lines.append(
        "Question-intent patterns: "
        + ", ".join(f'"{p}"' for p in body["question_intent_patterns"])
    )
    lines.append(
        "Ignore phrases (ritual politeness — do NOT let these affect scoring): "
        + ", ".join(f'"{p}"' for p in body["ignore_phrases"])
    )
    return "\n".join(lines)


def build_system_prompt(ontology: dict) -> str:
    """Build the full system prompt that drives the tone committee.

    The prompt defines three analyst-agent personas and an aggregator,
    embeds the ontology for grounding, and specifies the exact JSON
    output schema the model must follow.

    Parameters
    ----------
    ontology : dict
        Tone ontology as returned by :func:`src.ontology.load_tone_ontology`.

    Returns
    -------
    str
        A fully-formed system prompt string.
    """
    categories = ontology["categories"]

    category_blocks = "\n\n".join(
        _format_category_block(name, body) for name, body in categories.items()
    )

    output_schema = json.dumps(
        {
            "questions": [
                {
                    "id": "string",
                    "praise": {"score": 0.0, "rationale": "string"},
                    "skeptic": {
                        "score": 0.0,
                        "rationale": "string",
                        "risk_vectors": ["string"],
                    },
                    "neutral": {"score": 0.0, "rationale": "string"},
                    "final": {
                        "label": "PraiseSupport | SkepticismDisappointment | Neutral",
                        "tone_score": 0.0,
                        "disagreement": False,
                    },
                }
            ]
        },
        indent=2,
    )

    prompt = textwrap.dedent("""\
        You are the Earnings-Call Tone Committee, a panel of three specialist
        analyst-agents plus an aggregator.  Your job is to classify the tone
        of analyst questions from an earnings-call Q&A session.

        ═══════════════════════════════════════════════
        TONE ONTOLOGY (version {version})
        ═══════════════════════════════════════════════

        {category_blocks}

        ═══════════════════════════════════════════════
        AGENT ROLES
        ═══════════════════════════════════════════════

        For EACH question you receive, adopt three perspectives sequentially:

        1. **PraiseAgent** — Look for signals of approval, optimism, or
           endorsement toward management's strategy, execution, or results.
           Assign a score from 0.0 (no praise) to 1.0 (strong praise) and
           write a one-sentence rationale.

        2. **SkepticAgent** — Look for signals of doubt, concern, risk
           probing, or disappointment.  Assign a score from 0.0 (no
           skepticism) to 1.0 (strong skepticism), write a one-sentence
           rationale, and list any concrete risk_vectors identified (e.g.
           "margin compression", "customer churn", "guidance credibility").

        3. **NeutralAgent** — Assess how much the question is purely
           information-seeking with no discernible positive or negative
           valence.  Assign a score from 0.0 (clearly opinionated) to
           1.0 (purely factual / procedural) and write a one-sentence
           rationale.

        ═══════════════════════════════════════════════
        CRITICAL RULES
        ═══════════════════════════════════════════════

        • The three scores (praise, skeptic, neutral) are INDEPENDENT
          assessments — they do NOT need to sum to 1.0.
        • IGNORE ritual-politeness phrases listed under "Ignore phrases"
          in the ontology above.  Phrases such as "congrats on the quarter"
          or "thanks for taking my question" are social conventions and
          must NOT inflate the praise score.
        • Focus on SUBSTANTIVE tone: what the analyst is really probing
          about execution, guidance, risk, capital allocation, competitive
          positioning, etc.
        • If a question contains both supportive and skeptical elements,
          reflect that in the individual scores — the aggregator will
          reconcile.

        ═══════════════════════════════════════════════
        AGGREGATOR
        ═══════════════════════════════════════════════

        After scoring from all three perspectives, act as the Aggregator:

        • **final.label** — Choose exactly one of: "PraiseSupport",
          "SkepticismDisappointment", or "Neutral".  Pick the label
          whose agent gave the highest score.  If praise and skeptic
          scores are within 0.15 of each other and both exceed the
          neutral score, prefer the higher one but set disagreement=true.
        • **final.tone_score** — A single composite score in [-1.0, 1.0]
          where -1.0 is maximum skepticism, 0.0 is neutral, and +1.0 is
          maximum praise.  Formula: praise.score − skeptic.score.
        • **final.disagreement** — Set to true when the top two agent
          scores are within 0.15 of each other, indicating the committee
          did not reach clear consensus.

        ═══════════════════════════════════════════════
        OUTPUT FORMAT
        ═══════════════════════════════════════════════

        Respond with ONLY valid JSON matching this schema (no markdown,
        no commentary outside the JSON):

        {output_schema}

        Scores are floats rounded to two decimal places.
        The "id" field must match the input question id exactly.
    """).format(
        version=ontology.get("version", "?"),
        category_blocks=category_blocks,
        output_schema=output_schema,
    )

    return prompt


# ---------------------------------------------------------------------------
# Committee entry-point
# ---------------------------------------------------------------------------

def analyze_questions_with_committee(qa_pairs: List[Dict]) -> List[Dict]:
    """Run the tone committee over a batch of analyst questions.

    Parameters
    ----------
    qa_pairs : list[dict]
        Each dict must contain at least:

        - ``"id"`` — unique question identifier (e.g. ``"q1"``).
        - ``"question_text"`` — the analyst's question string.
        - ``"analyst_name"`` — name of the analyst (optional but expected).

    Returns
    -------
    list[dict]
        One result dict per question, each containing ``praise``,
        ``skeptic``, ``neutral``, and ``final`` sub-dicts as defined
        by the output schema in the system prompt.

    Raises
    ------
    ValueError
        If the model response is missing the ``"questions"`` key or if
        returned question ids do not match the input ids.
    """
    system_prompt = build_system_prompt(ontology)

    payload = {
        "questions": [
            {"id": q["id"], "question": q["question_text"]}
            for q in qa_pairs
        ]
    }

    response = call_llm(system_prompt, payload)

    # --- validate response structure ---
    if "questions" not in response:
        raise ValueError(
            "LLM response missing top-level 'questions' key. "
            f"Keys received: {list(response.keys())}"
        )

    results = response["questions"]

    expected_ids = {q["id"] for q in qa_pairs}
    returned_ids = {r.get("id") for r in results}
    missing = expected_ids - returned_ids
    if missing:
        raise ValueError(
            f"LLM response is missing results for question ids: {sorted(missing)}"
        )

    return results
