from __future__ import annotations

import argparse
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pymysql
from dotenv import load_dotenv

from config import load_settings
from data_injection import (
    _build_target_frame,
    _load_db_config,
    _norm_yyyymmdd,
    _prepare_db_rows,
    _upsert_rows,
)
from function import CorporateHoldingsModule


SYMBOL_LINE_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(\d{6})\s+(.+?)\s+\((\d{8})\)\s*$")
ERROR_LINE_RE = re.compile(r"^\s*->\s*ERROR:")


@dataclass(frozen=True)
class FailedSymbol:
    run_index: int
    run_total: int
    stock_code: str
    stock_name: str
    corp_code: str


@dataclass(frozen=True)
class RunSymbol:
    run_index: int
    run_total: int
    stock_code: str
    stock_name: str
    corp_code: str


def _target_cache_path(market_name: str) -> Path:
    return Path("data/cache") / f"retry_targets_{market_name.lower()}.csv"


def _save_target_cache(targets: pd.DataFrame, market_name: str) -> Path:
    path = _target_cache_path(market_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = targets.copy()
    for col in ("stock_code", "stock_name", "corp_code", "dart_corp_name"):
        if col not in out.columns:
            out[col] = ""
    out["stock_code"] = out["stock_code"].astype(str).str.zfill(6)
    out["corp_code"] = out["corp_code"].astype(str).str.zfill(8)
    out = out[["stock_code", "stock_name", "corp_code", "dart_corp_name"]].copy()
    out = out.drop_duplicates(subset=["corp_code"], keep="first").reset_index(drop=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _load_target_cache(market_name: str) -> Optional[pd.DataFrame]:
    path = _target_cache_path(market_name)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return None
    need = {"stock_code", "stock_name", "corp_code", "dart_corp_name"}
    if not need.issubset(df.columns):
        return None
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
    df = df[df["corp_code"].str.match(r"^\d{8}$", na=False)].copy()
    if df.empty:
        return None
    return df.reset_index(drop=True)


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _default_log_file() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("logs") / f"retry_failed_injection_{ts}.log")


def _configure_logger(log_file: str) -> logging.Logger:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("retry_failed_injection")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def _parse_failed_symbols(log_path: str) -> list[FailedSymbol]:
    failed: list[FailedSymbol] = []
    current: Optional[FailedSymbol] = None

    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip("\n")
            match = SYMBOL_LINE_RE.match(line.strip())
            if match:
                current = FailedSymbol(
                    run_index=int(match.group(1)),
                    run_total=int(match.group(2)),
                    stock_code=match.group(3),
                    stock_name=match.group(4).strip(),
                    corp_code=match.group(5),
                )
                continue

            if current is not None and ERROR_LINE_RE.match(line):
                failed.append(current)
                current = None

    # Same symbol may fail multiple times in one log; keep first appearance order.
    out: list[FailedSymbol] = []
    seen: set[str] = set()
    for item in failed:
        if item.corp_code in seen:
            continue
        seen.add(item.corp_code)
        out.append(item)
    return out


def _parse_run_symbols(log_path: str) -> list[RunSymbol]:
    symbols: list[RunSymbol] = []
    seen: set[str] = set()
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip("\n").strip()
            match = SYMBOL_LINE_RE.match(line)
            if not match:
                continue
            corp_code = match.group(5)
            if corp_code in seen:
                continue
            seen.add(corp_code)
            symbols.append(
                RunSymbol(
                    run_index=int(match.group(1)),
                    run_total=int(match.group(2)),
                    stock_code=match.group(3),
                    stock_name=match.group(4).strip(),
                    corp_code=corp_code,
                )
            )
    return symbols


def _slice_targets_for_retry(
    targets: pd.DataFrame,
    failed: list[FailedSymbol],
    mode: str,
) -> pd.DataFrame:
    if not failed:
        return targets.iloc[0:0].copy()

    if mode == "from-first-error":
        first_idx = min(item.run_index for item in failed)
        start_pos = max(first_idx - 1, 0)
        return targets.iloc[start_pos:].copy().reset_index(drop=True)

    if mode == "failed-only":
        failed_codes = {item.corp_code for item in failed}
        corp_series = targets["corp_code"].astype(str).str.zfill(8)
        return targets[corp_series.isin(failed_codes)].copy().reset_index(drop=True)

    raise ValueError(f"Unsupported resume mode: {mode}")


def _slice_targets_from_start_symbol(
    targets: pd.DataFrame,
    start_corp_code: Optional[str],
    start_stock_code: Optional[str],
    start_stock_name: Optional[str],
) -> pd.DataFrame:
    out = targets.copy()
    out["corp_code"] = out["corp_code"].astype(str).str.zfill(8)
    out["stock_code"] = out["stock_code"].astype(str).str.zfill(6)
    out["stock_name"] = out["stock_name"].astype(str)
    if "dart_corp_name" in out.columns:
        out["dart_corp_name"] = out["dart_corp_name"].astype(str)
    else:
        out["dart_corp_name"] = ""

    start_idx: Optional[int] = None

    if start_corp_code:
        corp_code = str(start_corp_code).strip().zfill(8)
        matched = out.index[out["corp_code"] == corp_code].tolist()
        if not matched:
            raise RuntimeError(f"start_corp_code not found in targets: {corp_code}")
        start_idx = matched[0]
    elif start_stock_code:
        stock_code = str(start_stock_code).strip().zfill(6)
        matched = out.index[out["stock_code"] == stock_code].tolist()
        if not matched:
            raise RuntimeError(f"start_stock_code not found in targets: {stock_code}")
        start_idx = matched[0]
    elif start_stock_name:
        key = str(start_stock_name).strip()
        if not key:
            raise RuntimeError("start_stock_name is empty.")
        name_mask = out["stock_name"].str.contains(key, case=False, regex=False, na=False)
        corp_mask = out["dart_corp_name"].str.contains(key, case=False, regex=False, na=False)
        matched = out.index[name_mask | corp_mask].tolist()
        if not matched:
            raise RuntimeError(f"start_stock_name not found in targets: {key}")
        start_idx = matched[0]

    if start_idx is None:
        raise RuntimeError("No start symbol argument provided.")

    return out.iloc[start_idx:].copy().reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retry DART data injection using a previous run log. "
            "You can resume from first error or retry only failed symbols."
        ),
    )
    parser.add_argument("--log-path", default=None, help="Path to previous run log text file.")
    parser.add_argument(
        "--prefer-log-targets",
        action="store_true",
        help="Use symbol order from --log-path instead of rebuilding KRX+DART target map.",
    )
    parser.add_argument("--resume-mode", choices=["from-first-error", "failed-only"], default="from-first-error")
    parser.add_argument("--start-corp-code", default=None, help="Resume from this corp_code (8 digits).")
    parser.add_argument("--start-stock-code", default=None, help="Resume from this stock_code (6 digits).")
    parser.add_argument("--start-stock-name", default=None, help="Resume from first matching stock name.")
    parser.add_argument("--market", choices=["KOSPI", "KOSDAQ", "KONEX"], default="KOSDAQ")
    parser.add_argument("--start-date", default="20150101", help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--end-date", default=_today_yyyymmdd(), help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--krx-date", default=None, help="KRX base date (YYYYMMDD). Omit for latest available.")
    parser.add_argument("--dart-api-key", default=None, help="DART API key override")
    parser.add_argument("--reprt-codes", default="11011", help="Comma-separated reprt_code values")
    parser.add_argument("--include-periodic-status", action="store_true")
    parser.add_argument("--include-majorstock-status", action="store_true")
    parser.add_argument("--exclude-note-plan", action="store_true")
    parser.add_argument("--max-note-reports", type=int, default=0, help="0 means no limit (all list B/I reports).")
    parser.add_argument("--base-offset", type=int, default=0, help="Original run offset if used.")
    parser.add_argument("--base-limit", type=int, default=None, help="Original run limit if used.")
    parser.add_argument(
        "--sleep-base-sec",
        type=float,
        default=10.0,
        help="Per-stock base sleep seconds (default: 10).",
    )
    parser.add_argument(
        "--sleep-jitter-min",
        type=int,
        default=1,
        help="Per-stock random jitter min seconds (default: 1).",
    )
    parser.add_argument(
        "--sleep-jitter-max",
        type=int,
        default=10,
        help="Per-stock random jitter max seconds (default: 10).",
    )
    parser.add_argument(
        "--sleep-sec",
        type=float,
        default=None,
        help="Deprecated fixed sleep override. If set, random sleep is disabled.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--base-sleep", type=float, default=0.8)
    parser.add_argument(
        "--query-sleep-sec",
        type=float,
        default=0.5,
        help="Sleep interval between each DART query request (default: 0.5).",
    )
    parser.add_argument(
        "--allow-single-fallback",
        action="store_true",
        help="If full target map cannot be built, allow fallback to single start-corp-code target.",
    )
    parser.add_argument("--log-file", default=_default_log_file(), help="Output log file path.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform only, skip DB insert.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    logger = _configure_logger(args.log_file)
    logger.info("[LOGFILE] %s", args.log_file)

    settings = load_settings()
    dart_api_key = (args.dart_api_key or settings.dart_api_key or "").strip()
    if not dart_api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY/OPENDART_API_KEY or pass --dart-api-key.")

    start_ymd = _norm_yyyymmdd(args.start_date)
    end_ymd = _norm_yyyymmdd(args.end_date)
    if start_ymd > end_ymd:
        raise ValueError(f"start_date > end_date: {start_ymd} > {end_ymd}")

    reprt_codes = tuple(c.strip() for c in str(args.reprt_codes).split(",") if c.strip()) or ("11011",)

    market_name = str(args.market).strip().upper()

    targets: pd.DataFrame
    unmatched: pd.DataFrame
    total_targets = 0

    if args.prefer_log_targets:
        if not args.log_path:
            raise RuntimeError("--prefer-log-targets requires --log-path.")
        run_symbols = _parse_run_symbols(args.log_path)
        if not run_symbols:
            raise RuntimeError(f"No symbol lines found in log: {args.log_path}")
        targets = pd.DataFrame(
            [
                {
                    "stock_code": s.stock_code,
                    "stock_name": s.stock_name,
                    "corp_code": s.corp_code,
                    "dart_corp_name": s.stock_name,
                }
                for s in run_symbols
            ]
        )
        unmatched = pd.DataFrame(columns=["stock_code", "stock_name"])
        total_targets = len(targets)
        logger.info("[TARGETS] source=log, symbols=%s", f"{total_targets:,}")
    else:
        try:
            targets, unmatched = _build_target_frame(
                dart_api_key=dart_api_key,
                market=market_name,
                krx_date=args.krx_date,
                timeout=args.timeout,
                max_retries=args.max_retries,
                base_sleep=args.base_sleep,
                request_sleep_sec=args.query_sleep_sec,
            )
            total_targets = len(targets)
            cache_path = _save_target_cache(targets=targets, market_name=market_name)
            logger.info("[TARGETS] source=live, cached=%s, symbols=%s", str(cache_path), f"{total_targets:,}")
        except Exception as exc:
            cached_targets = _load_target_cache(market_name=market_name)
            if cached_targets is not None:
                targets = cached_targets
                unmatched = pd.DataFrame(columns=["stock_code", "stock_name"])
                total_targets = len(targets)
                logger.warning(
                    "[WARN] target map build failed (%s). fallback=target cache (%s symbols)",
                    exc,
                    f"{total_targets:,}",
                )
            elif args.log_path:
                run_symbols = _parse_run_symbols(args.log_path)
                if not run_symbols:
                    raise
                targets = pd.DataFrame(
                    [
                        {
                            "stock_code": s.stock_code,
                            "stock_name": s.stock_name,
                            "corp_code": s.corp_code,
                            "dart_corp_name": s.stock_name,
                        }
                        for s in run_symbols
                    ]
                )
                unmatched = pd.DataFrame(columns=["stock_code", "stock_name"])
                total_targets = len(targets)
                logger.warning(
                    "[WARN] target map build failed (%s). fallback=log symbols (%s)",
                    exc,
                    f"{total_targets:,}",
                )
            elif args.start_corp_code and args.allow_single_fallback:
                targets = pd.DataFrame(
                    [
                        {
                            "stock_code": str(args.start_stock_code or "").zfill(6),
                            "stock_name": str(args.start_stock_name or "").strip(),
                            "corp_code": str(args.start_corp_code).strip().zfill(8),
                            "dart_corp_name": str(args.start_stock_name or "").strip(),
                        }
                    ]
                )
                unmatched = pd.DataFrame(columns=["stock_code", "stock_name"])
                total_targets = 1
                logger.warning(
                    "[WARN] target map build failed (%s). fallback=single corp_code=%s",
                    exc,
                    str(args.start_corp_code).strip().zfill(8),
                )
            else:
                raise RuntimeError(
                    "Failed to build full target universe and no fallback source is available. "
                    "Provide --log-path (or use --prefer-log-targets), or run once when API limit resets "
                    "to build target cache. Use --allow-single-fallback only if you intentionally want one symbol."
                ) from exc
    if args.base_offset > 0:
        targets = targets.iloc[args.base_offset :].copy()
    if args.base_limit is not None:
        targets = targets.iloc[: args.base_limit].copy()
    targets = targets.reset_index(drop=True)

    has_start_anchor = any([args.start_corp_code, args.start_stock_code, args.start_stock_name])
    if has_start_anchor:
        retry_targets = _slice_targets_from_start_symbol(
            targets=targets,
            start_corp_code=args.start_corp_code,
            start_stock_code=args.start_stock_code,
            start_stock_name=args.start_stock_name,
        )
        logger.info(
            "[RESUME] mode=from-symbol, "
            f"start_corp_code={args.start_corp_code}, "
            f"start_stock_code={args.start_stock_code}, "
            f"start_stock_name={args.start_stock_name}"
        )
    else:
        if not args.log_path:
            raise RuntimeError(
                "Provide --log-path for log-based retry, or use one of "
                "--start-corp-code / --start-stock-code / --start-stock-name."
            )
        failed_symbols = _parse_failed_symbols(args.log_path)
        if not failed_symbols:
            raise RuntimeError("No failed symbols found in the log.")

        logger.info(
            f"[LOG] failed_symbols={len(failed_symbols):,}, "
            f"first_error_index={min(x.run_index for x in failed_symbols):,}, "
            f"resume_mode={args.resume_mode}"
        )
        retry_targets = _slice_targets_for_retry(
            targets=targets,
            failed=failed_symbols,
            mode=args.resume_mode,
        )

    if retry_targets.empty:
        raise RuntimeError("No retry targets after applying resume filters.")

    logger.info(
        f"[MAP] market={market_name}, total={total_targets:,}, "
        f"mapped={total_targets - len(unmatched):,}, unmatched={len(unmatched):,}, "
        f"base_targets={len(targets):,}, retry_targets={len(retry_targets):,}"
    )
    logger.info("[QUERY] request_sleep_sec=%.3f", max(float(args.query_sleep_sec), 0.0))

    holdings = CorporateHoldingsModule(
        api_key=dart_api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
        request_interval_sec=args.query_sleep_sec,
    )

    conn: Optional[pymysql.connections.Connection] = None
    if not args.dry_run:
        db_cfg = _load_db_config()
        conn = pymysql.connect(
            host=db_cfg.host,
            port=db_cfg.port,
            user=db_cfg.user,
            password=db_cfg.password,
            database=db_cfg.database,
            charset="utf8mb4",
            autocommit=False,
        )

    processed = 0
    error_count = 0
    total_events = 0
    total_db_affected = 0

    try:
        for idx, row in retry_targets.iterrows():
            stock_code = str(row.get("stock_code", "")).zfill(6)
            corp_code = str(row.get("corp_code", "")).zfill(8)
            stock_name = str(row.get("stock_name", "")).strip()
            dart_corp_name = str(row.get("dart_corp_name", "")).strip()
            label = dart_corp_name or stock_name

            logger.info("[%s/%s] %s %s (%s)", idx + 1, len(retry_targets), stock_code, label, corp_code)

            try:
                dfs = holdings.fetch_all_holding_dfs(
                    corp_code=corp_code,
                    bgn_de=start_ymd,
                    end_de=end_ymd,
                    start_year=int(start_ymd[:4]),
                    end_year=int(end_ymd[:4]),
                    reprt_codes=reprt_codes,
                    include_periodic_status=args.include_periodic_status,
                    include_majorstock_status=args.include_majorstock_status,
                    include_transfer_note_plan=not args.exclude_note_plan,
                    max_note_reports=(None if int(args.max_note_reports) <= 0 else int(args.max_note_reports)),
                )
                combined = dfs.get("combined", pd.DataFrame())
                rows = _prepare_db_rows(combined)
                total_events += len(rows)

                if args.dry_run:
                    logger.info("  -> events=%s (dry-run)", f"{len(rows):,}")
                else:
                    assert conn is not None
                    affected = _upsert_rows(conn, rows)
                    total_db_affected += affected
                    logger.info("  -> events=%s, db_affected=%s", f"{len(rows):,}", f"{affected:,}")
            except Exception as exc:
                error_count += 1
                if conn is not None:
                    conn.rollback()
                logger.error("  -> ERROR: %s", exc)

            processed += 1
            if idx < len(retry_targets) - 1:
                if args.sleep_sec is not None:
                    wait_sec = max(float(args.sleep_sec), 0.0)
                else:
                    base_sec = max(float(args.sleep_base_sec), 0.0)
                    jitter_min = max(int(args.sleep_jitter_min), 0)
                    jitter_max = max(int(args.sleep_jitter_max), jitter_min)
                    wait_sec = base_sec + random.randint(jitter_min, jitter_max)
                if wait_sec > 0:
                    logger.info("  -> sleep %.1fs before next symbol", wait_sec)
                    time.sleep(wait_sec)
    finally:
        if conn is not None:
            conn.close()

    logger.info(
        f"[DONE] processed={processed:,}, errors={error_count:,}, "
        f"prepared_events={total_events:,}, db_affected={total_db_affected:,}, "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
