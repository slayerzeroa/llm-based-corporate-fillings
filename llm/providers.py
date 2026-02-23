from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

from config import load_settings


@dataclass(frozen=True)
class LlmInvesteeNameResult:
    iscmp_cmpnm: Optional[str]
    confidence: float
    reason: str
    raw_text: str


def _clean_env_secret(value: Optional[str]) -> str:
    s = str(value or "").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


class OpenAIInvesteeNameResolver:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = 300,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._temperature = float(temperature)
        self._max_output_tokens = max_output_tokens

    @staticmethod
    def _extract_first_json(text: str) -> Optional[dict]:
        if not text:
            return None
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        chunk = text[start : end + 1]
        try:
            return json.loads(chunk)
        except Exception:
            return None

    @staticmethod
    def _to_result(raw_text: str) -> LlmInvesteeNameResult:
        obj = OpenAIInvesteeNameResolver._extract_first_json(raw_text or "")
        if not isinstance(obj, dict):
            return LlmInvesteeNameResult(
                iscmp_cmpnm=None,
                confidence=0.0,
                reason="LLM output is not valid JSON.",
                raw_text=raw_text or "",
            )

        name = obj.get("iscmp_cmpnm")
        if name is not None:
            name = str(name).strip() or None

        confidence = obj.get("confidence", 0.0)
        try:
            conf = float(confidence)
        except Exception:
            conf = 0.0
        conf = min(max(conf, 0.0), 1.0)

        reason = str(obj.get("reason", "")).strip()
        return LlmInvesteeNameResult(
            iscmp_cmpnm=name,
            confidence=conf,
            reason=reason,
            raw_text=raw_text or "",
        )

    def suggest_investee_name(
        self,
        *,
        corp_name: str,
        report_nm: str,
        raw_iscmp_cmpnm: str,
        parser_candidate: str,
        viewer_focus_text: str,
    ) -> LlmInvesteeNameResult:
        system_prompt = (
            "너는 한국 DART 공시 문서에서 '투자/처분 대상 회사명(iscmp_cmpnm)'만 식별하는 정제기다. "
            "반드시 JSON 객체 한 개만 반환하라. 키는 iscmp_cmpnm, confidence, reason 이다. "
            "회사명이 불명확하면 iscmp_cmpnm은 null로 두고 confidence를 낮춰라. "
            "라벨 텍스트(예: 대표자, 국적, 회사명, 발행회사) 자체를 회사명으로 반환하면 안 된다."
        )

        user_payload = {
            "task": "iscmp_cmpnm 정제",
            "fields": {
                "corp_name": corp_name,
                "report_nm": report_nm,
                "current_iscmp_cmpnm": raw_iscmp_cmpnm,
                "parser_candidate": parser_candidate,
            },
            "viewer_focus_text": viewer_focus_text,
            "output_schema": {
                "iscmp_cmpnm": "string|null",
                "confidence": "0.0~1.0 float",
                "reason": "short Korean explanation",
            },
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))

        raw_text = ""
        try:
            resp = self._client.responses.create(
                model=self._model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self._temperature,
                max_output_tokens=self._max_output_tokens,
            )
            raw_text = getattr(resp, "output_text", "") or ""
        except Exception:
            # Backward compatibility fallback.
            chat = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=self._temperature,
                max_tokens=self._max_output_tokens,
            )
            raw_text = (chat.choices[0].message.content or "").strip()

        return self._to_result(raw_text)


class GeminiInvesteeNameResolver:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemma-3-27b-it",
        temperature: float = 0.0,
        max_output_tokens: Optional[int] = 300,
    ) -> None:
        import google.generativeai as genai

        self._genai = genai
        self._genai.configure(api_key=api_key)
        self._model = str(model or "gemma-3-27b-it").strip()
        self._temperature = float(temperature)
        self._max_output_tokens = max_output_tokens

    @staticmethod
    def _extract_first_json(text: str) -> Optional[dict]:
        if not text:
            return None
        text = text.strip()
        try:
            return json.loads(text)
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        chunk = text[start : end + 1]
        try:
            return json.loads(chunk)
        except Exception:
            return None

    @staticmethod
    def _to_result(raw_text: str) -> LlmInvesteeNameResult:
        obj = GeminiInvesteeNameResolver._extract_first_json(raw_text or "")
        if not isinstance(obj, dict):
            return LlmInvesteeNameResult(
                iscmp_cmpnm=None,
                confidence=0.0,
                reason="LLM output is not valid JSON.",
                raw_text=raw_text or "",
            )

        name = obj.get("iscmp_cmpnm")
        if name is not None:
            name = str(name).strip() or None

        confidence = obj.get("confidence", 0.0)
        try:
            conf = float(confidence)
        except Exception:
            conf = 0.0
        conf = min(max(conf, 0.0), 1.0)

        reason = str(obj.get("reason", "")).strip()
        return LlmInvesteeNameResult(
            iscmp_cmpnm=name,
            confidence=conf,
            reason=reason,
            raw_text=raw_text or "",
        )

    def suggest_investee_name(
        self,
        *,
        corp_name: str,
        report_nm: str,
        raw_iscmp_cmpnm: str,
        parser_candidate: str,
        viewer_focus_text: str,
    ) -> LlmInvesteeNameResult:
        system_prompt = (
            "너는 한국 DART 공시 문서에서 '투자/처분 대상 회사명(iscmp_cmpnm)'만 식별하는 정제기다. "
            "반드시 JSON 객체 한 개만 반환하라. 키는 iscmp_cmpnm, confidence, reason 이다. "
            "회사명이 불명확하면 iscmp_cmpnm은 null로 두고 confidence를 낮춰라. "
            "라벨 텍스트(예: 대표자, 국적, 회사명, 발행회사) 자체를 회사명으로 반환하면 안 된다."
        )

        user_payload = {
            "task": "iscmp_cmpnm 정제",
            "fields": {
                "corp_name": corp_name,
                "report_nm": report_nm,
                "current_iscmp_cmpnm": raw_iscmp_cmpnm,
                "parser_candidate": parser_candidate,
            },
            "viewer_focus_text": viewer_focus_text,
            "output_schema": {
                "iscmp_cmpnm": "string|null",
                "confidence": "0.0~1.0 float",
                "reason": "short Korean explanation",
            },
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        final_prompt = (
            f"{system_prompt}\n\n"
            "아래 JSON 데이터에서 대상회사명을 추론하라.\n"
            "반드시 JSON 객체 하나만 출력하라.\n\n"
            f"{user_prompt}"
        )

        model_candidates = [self._model]
        if not self._model.startswith("models/"):
            model_candidates.append(f"models/{self._model}")

        last_error: Optional[Exception] = None
        for model_name in model_candidates:
            try:
                model = self._genai.GenerativeModel(
                    model_name=model_name,
                    generation_config={
                        "temperature": self._temperature,
                        "max_output_tokens": self._max_output_tokens,
                    },
                )
                resp = model.generate_content(final_prompt)
                raw_text = getattr(resp, "text", "") or ""
                return self._to_result(raw_text)
            except Exception as exc:
                last_error = exc
                continue

        raise RuntimeError(f"Gemini model call failed for all candidates: {last_error}")


def build_name_resolver(provider: str = "auto") -> Optional[object]:
    settings = load_settings()
    p = str(provider or "auto").strip().lower()

    if p in {"none", "off"}:
        return None

    if p in {"gemini", "auto"}:
        key = _clean_env_secret(settings.gemini_api_key)
        if key:
            model = (os.getenv("GEMINI_MODEL") or "").strip() or "gemma-3-27b-it"
            return GeminiInvesteeNameResolver(
                api_key=key,
                model=model,
                temperature=settings.temperature,
                max_output_tokens=settings.max_output_tokens or 300,
            )
        if p == "gemini":
            raise RuntimeError("GEMINI_API_KEY is required for --provider gemini.")

    if p in {"auto", "openai"}:
        key = _clean_env_secret(settings.openai_api_key)
        if not key:
            if p == "openai":
                raise RuntimeError("OPENAI_API_KEY is required for --provider openai.")
            return None
        return OpenAIInvesteeNameResolver(
            api_key=key,
            model=settings.openai_model,
            temperature=settings.temperature,
            max_output_tokens=settings.max_output_tokens or 300,
        )

    raise ValueError(f"Unsupported provider: {provider}")
