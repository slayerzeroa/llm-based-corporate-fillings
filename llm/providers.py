from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from openai import OpenAI


class LLMProviderError(RuntimeError):
    pass


@dataclass
class LLMGeneration:
    text: str
    model: str


class BaseProvider:
    def generate(self, system: str, user: str) -> LLMGeneration:
        raise NotImplementedError


class OpenAIProvider(BaseProvider):
    def __init__(
        self,
        api_key: Optional[str],
        model: str,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = None,
    ) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    def generate(self, system: str, user: str) -> LLMGeneration:
        try:
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
            )
        except Exception as exc:
            raise LLMProviderError(f"OpenAI request failed: {exc}") from exc

        text = getattr(response, "output_text", None) or ""
        return LLMGeneration(text=text, model=self.model)