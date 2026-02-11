from __future__ import annotations

import json
import re
from typing import Iterable, Optional

from .providers import BaseProvider


DEFAULT_SCHEMA_KEYS = [
    "company_name",
    "stock_code",
    "report_name",
    "report_type",
    "fiscal_period",
    "filing_date",
    "is_revision",
    "key_topics",
    "summary",
]


SYSTEM_PROMPT = (
    "You are an expert financial analyst. "
    "Return only strict JSON that matches the requested schema. "
    "Do not include markdown, explanations, or extra keys."
)


def _build_user_prompt(filing: dict, filing_text: Optional[str]) -> str:
    input_payload = {
        "filing_metadata": filing,
        "filing_text": filing_text,
        "schema_keys": DEFAULT_SCHEMA_KEYS,
        "rules": {
            "unknown_values": "Use null when a value is unknown or not present.",
            "report_type_values": ["quarterly", "half-year", "annual", "other"],
            "key_topics": "List 3-8 short topical tags, or [] if unknown.",
            "summary": "1-3 sentences, concise and factual.",
        },
    }

    return (
        "Extract structured data from the filing information below. "
        "Return a single JSON object only.\n\n"
        f"Input: {json.dumps(input_payload, ensure_ascii=True)}"
    )


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in LLM response.")

    return json.loads(match.group(0))


def arrange_filings(
    filings: Iterable[dict],
    provider: BaseProvider,
    *,
    filing_texts: Optional[dict[str, str]] = None,
    max_items: Optional[int] = None,
) -> list[dict]:
    results: list[dict] = []
    count = 0

    for filing in filings:
        if max_items is not None and count >= max_items:
            break

        filing_text = None
        if filing_texts:
            key = filing.get("rcept_no") or filing.get("receipt_no")
            if key and key in filing_texts:
                filing_text = filing_texts[key]
        if filing_text is None:
            filing_text = filing.get("filing_text") or filing.get("text")

        prompt = _build_user_prompt(filing, filing_text)
        generation = provider.generate(SYSTEM_PROMPT, prompt)

    structured = _extract_json(generation.text)
    if not isinstance(structured, dict):
        raise ValueError("LLM response must be a JSON object.")
        structured["_llm_model"] = generation.model
        structured["_llm_provider"] = provider.__class__.__name__

        results.append(structured)
        count += 1

    return results
