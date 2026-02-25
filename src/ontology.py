"""Tone ontology loader.

Reads the YAML-based tone ontology definition and returns it as a validated
Python dictionary.  The ontology describes the categorical tone labels
(PraiseSupport, SkepticismDisappointment, Neutral) that the analyst-agent
committee uses when scoring Q&A exchanges from earnings calls.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_EXPECTED_CATEGORIES = {"PraiseSupport", "SkepticismDisappointment", "Neutral"}
_REQUIRED_FIELDS = {"description", "lexical_cues", "question_intent_patterns", "ignore_phrases"}


def load_tone_ontology(path: str = "ontology/tone_ontology_v1.yaml") -> dict:
    """Parse *path* and return the tone ontology as a validated dict.

    Parameters
    ----------
    path : str
        Filesystem path to the YAML ontology file.

    Returns
    -------
    dict
        The full ontology structure including ``version`` and ``categories``.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the YAML content is missing required keys, categories, or
        per-category fields.
    """
    ontology_path = Path(path)
    if not ontology_path.exists():
        raise FileNotFoundError(f"Ontology file not found: {ontology_path}")

    with open(ontology_path, "r") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Ontology root must be a mapping, got {type(data).__name__}")

    # --- top-level keys ---
    if "version" not in data:
        raise ValueError("Ontology is missing required key 'version'")
    if "categories" not in data:
        raise ValueError("Ontology is missing required key 'categories'")

    categories = data["categories"]
    if not isinstance(categories, dict):
        raise ValueError("'categories' must be a mapping")

    # --- expected categories ---
    missing = _EXPECTED_CATEGORIES - categories.keys()
    if missing:
        raise ValueError(f"Ontology is missing expected categories: {sorted(missing)}")

    # --- per-category structure ---
    for name, body in categories.items():
        if not isinstance(body, dict):
            raise ValueError(f"Category '{name}' must be a mapping, got {type(body).__name__}")
        missing_fields = _REQUIRED_FIELDS - body.keys()
        if missing_fields:
            raise ValueError(
                f"Category '{name}' is missing required fields: {sorted(missing_fields)}"
            )

    return data
