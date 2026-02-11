from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    dart_api_key: Optional[str]
    openai_api_key: Optional[str]
    gemini_api_key: Optional[str]
    openai_model: str
    gemini_model: str
    temperature: float
    max_output_tokens: Optional[int]


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Invalid integer for env var: {value}")


def load_settings() -> Settings:
    return Settings(
        dart_api_key=_first_env("DART_API_KEY", "OPENDART_API_KEY", "OPEN_DART_API"),
        openai_api_key=_first_env("OPENAI_API_KEY"),
        gemini_api_key=_first_env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        max_output_tokens=_int_or_none(os.getenv("LLM_MAX_OUTPUT_TOKENS")),
    )
