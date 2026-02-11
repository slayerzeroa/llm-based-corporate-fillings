from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, Optional

from config import load_settings
from function.filling import collect_quarterly_reports, download_document_xml
from llm.arranger import arrange_filings
from llm.providers import OpenAIProvider


def build_provider(
    provider_name: str,
    *,
    openai_model: Optional[str] = None,
    gemini_model: Optional[str] = None,
):
    settings = load_settings()
    name = provider_name.lower()

    if name == "auto":
        if settings.openai_api_key:
            name = "openai"
        elif settings.gemini_api_key:
            name = "gemini"
        else:
            raise RuntimeError("No LLM API key found for auto provider.")

    if name == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI provider.")
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=openai_model or settings.openai_model,
            temperature=settings.temperature,
            max_output_tokens=settings.max_output_tokens,
        )

    raise ValueError("Provider must be one of: openai, gemini, auto")


def _load_filings_from_jsonl(path: Path) -> list[dict]:
    filings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        filings.append(json.loads(line))
    return filings


def _fetch_filings_from_dart(
    start_date: str,
    end_date: str,
    tickers: Optional[list[str]],
    include_halfyear: bool,
    markets: tuple[str, ...],
) -> list[dict]:
    settings = load_settings()
    if not settings.dart_api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY or OPENDART_API_KEY.")

    df = collect_quarterly_reports(
        api_key=settings.dart_api_key,
        start_date=start_date,
        end_date=end_date,
        tickers=tickers,
        include_halfyear=include_halfyear,
        markets=markets,
    )
    return df.to_dict(orient="records")


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Arrange corporate filings with LLMs.")
    parser.add_argument("--provider", default="auto", choices=["openai", "gemini", "auto"])
    parser.add_argument("--start", help="Start date YYYYMMDD")
    parser.add_argument("--end", help="End date YYYYMMDD")
    parser.add_argument("--tickers", help="Comma-separated stock codes")
    parser.add_argument("--include-halfyear", action="store_true", help="Include half-year reports")
    parser.add_argument("--markets", default="Y,K,N", help="Comma-separated corp_cls codes")
    parser.add_argument("--limit", type=int, help="Max number of filings to process")
    parser.add_argument("--out", default="arranged_filings.jsonl", help="Output JSONL file")
    parser.add_argument("--openai-model", help="Override OpenAI model")
    parser.add_argument("--gemini-model", help="Override Gemini model")
    parser.add_argument("--input-jsonl", help="Input JSONL file with filing metadata")
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        help="Fetch and save raw filing metadata only (skip LLM).",
    )
    parser.add_argument("--download-xml", action="store_true", help="Download DART filing XML by receipt no.")
    parser.add_argument("--rcept-no", help="Receipt number for DART filing XML download")
    parser.add_argument("--out-dir", default="dart_documents", help="Output directory for DART XML download")

    args = parser.parse_args(argv)

    if args.download_xml and not args.rcept_no:
        parser.error("--rcept-no is required with --download-xml.")

    if args.download_xml:
        settings = load_settings()
        if not settings.dart_api_key:
            raise RuntimeError("DART API key not found. Set DART_API_KEY or OPENDART_API_KEY.")
        paths = download_document_xml(
            api_key=settings.dart_api_key,
            rcept_no=args.rcept_no,
            out_dir=args.out_dir,
            extract=True,
        )
        print(f"Downloaded {len(paths)} XML file(s) to {args.out_dir}")
        return 0

    if not args.input_jsonl and (not args.start or not args.end):
        parser.error("--start and --end are required unless --input-jsonl is provided.")

    if args.input_jsonl:
        filings = _load_filings_from_jsonl(Path(args.input_jsonl))
    else:
        tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
        markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())
        filings = _fetch_filings_from_dart(
            start_date=args.start,
            end_date=args.end,
            tickers=tickers,
            include_halfyear=args.include_halfyear,
            markets=markets,
        )

    if args.dump_raw:
        _write_jsonl(Path(args.out), filings)
        print(f"Saved {len(filings)} raw filings to {args.out}")
        return 0

    provider = build_provider(
        args.provider,
        openai_model=args.openai_model,
        gemini_model=args.gemini_model,
    )

    arranged = arrange_filings(filings, provider, max_items=args.limit)
    _write_jsonl(Path(args.out), arranged)

    print(f"Saved {len(arranged)} arranged filings to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
