"""Thin LLM abstraction over the OpenAI Chat Completions API.

This module provides a single ``call_llm`` function that sends a
system-prompt + user-payload pair and returns a parsed JSON dict.
It is intentionally minimal so it can later be swapped for the Anthropic
SDK, a local model server, or any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import os

from openai import OpenAI


def call_llm(
    system_prompt: str,
    user_payload: dict,
    *,
    model: str = "gpt-4o",
    temperature: float = 0.2,
    max_retries: int = 2,
) -> dict:
    """Send a structured request to the LLM and return parsed JSON.

    Parameters
    ----------
    system_prompt : str
        The system-level instruction for the model.
    user_payload : dict
        Arbitrary data that will be JSON-serialised into the user message.
    model : str
        Model identifier (default ``"gpt-4o"``).
    temperature : float
        Sampling temperature (default ``0.2`` for deterministic-ish output).
    max_retries : int
        Number of times to retry on JSON-parse failure before raising.

    Returns
    -------
    dict
        The model's response parsed from JSON.

    Raises
    ------
    EnvironmentError
        If ``OPENAI_API_KEY`` is not set.
    RuntimeError
        If the model response cannot be parsed as valid JSON after all
        retry attempts.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY is not set. Export it or add it to your .env file."
        )

    client = OpenAI(api_key=api_key)

    user_content = json.dumps(user_payload, ensure_ascii=False)
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )

            raw = response.choices[0].message.content
            return json.loads(raw)

        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < max_retries:
                continue
        except Exception as exc:
            raise RuntimeError(f"LLM call failed: {exc}") from exc

    raise RuntimeError(
        f"Failed to parse JSON from model response after {max_retries} attempts. "
        f"Last error: {last_error}"
    )
