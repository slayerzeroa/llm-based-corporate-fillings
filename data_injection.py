from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Optional

import pandas as pd
import pymysql
from dotenv import load_dotenv

from config import load_settings
from function import CorporateHoldingsModule, KrxApiClient
from function.filling import OpenDartClient


DB_TABLE = "dart_investment_events"

DB_COLUMNS = [
    "rcept_no",
    "rcept_dt",
    "corp_cls",
    "corp_code",
    "corp_name",
    "report_nm",
    "flr_nm",
    "pblntf_ty",
    "source",
    "viewer_url",
    "iscmp_cmpnm",
    "trfdtl_trfprc",
    "trfdtl_stkcnt",
    "trf_pp",
    "row_hash",
]

UPSERT_SQL = f"""
INSERT INTO {DB_TABLE} (
    {", ".join(DB_COLUMNS)}
)
VALUES (
    {", ".join(["%s"] * len(DB_COLUMNS))}
)
ON DUPLICATE KEY UPDATE
    rcept_dt = VALUES(rcept_dt),
    corp_cls = VALUES(corp_cls),
    corp_name = VALUES(corp_name),
    report_nm = VALUES(report_nm),
    flr_nm = VALUES(flr_nm),
    pblntf_ty = VALUES(pblntf_ty),
    source = VALUES(source),
    viewer_url = VALUES(viewer_url),
    trfdtl_trfprc = VALUES(trfdtl_trfprc),
    trfdtl_stkcnt = VALUES(trfdtl_stkcnt),
    trf_pp = VALUES(trf_pp),
    row_hash = VALUES(row_hash),
    updated_at = CURRENT_TIMESTAMP
"""


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _load_db_config() -> DbConfig:
    host = _first_env("DB_HOST")
    user = _first_env("DB_USER", "DB_USERNAME")
    password = _first_env("DB_PASSWORD", "DB_PASS") or ""
    database = _first_env("DB_NAME", "DB_DATABASE")
    port_raw = _first_env("DB_PORT") or "3306"

    missing = []
    if not host:
        missing.append("DB_HOST")
    if not user:
        missing.append("DB_USER")
    if not database:
        missing.append("DB_NAME")
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid DB_PORT: {port_raw}") from exc

    return DbConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )


def _norm_yyyymmdd(value: str) -> str:
    raw = str(value).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {value} (YYYYMMDD or YYYY-MM-DD)")
    return raw


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _is_null_like(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "<na>"}:
        return True
    return False


def _to_str_or_none(value: Any) -> Optional[str]:
    if _is_null_like(value):
        return None
    text = str(value).strip()
    return text if text else None


def _to_date_or_none(value: Any) -> Optional[str]:
    if _is_null_like(value):
        return None
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.strftime("%Y-%m-%d")


def _to_amount_or_none(value: Any) -> Optional[int]:
    if _is_null_like(value):
        return None
    raw = str(value).replace(",", "").strip()
    if not raw:
        return None
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return None
    return int(dec.to_integral_value(rounding=ROUND_HALF_UP))


def _to_stkcnt_or_none(value: Any) -> Optional[Decimal]:
    if _is_null_like(value):
        return None
    raw = str(value).replace(",", "").strip()
    if not raw:
        return None
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            return dec.quantize(Decimal("0.000001"))
    except InvalidOperation:
        return None


def _build_row_hash(row: dict[str, Any]) -> str:
    material = {k: (None if _is_null_like(v) else str(v)) for k, v in row.items() if k != "row_hash"}
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prepare_db_rows(df: pd.DataFrame) -> list[tuple[Any, ...]]:
    if df.empty:
        return []

    need_cols = [
        "rcept_no",
        "rcept_dt",
        "corp_cls",
        "corp_code",
        "corp_name",
        "report_nm",
        "flr_nm",
        "pblntf_ty",
        "source",
        "viewer_url",
        "iscmp_cmpnm",
        "trfdtl_trfprc",
        "trfdtl_stkcnt",
        "trf_pp",
    ]

    out = df.copy()
    for c in need_cols:
        if c not in out.columns:
            out[c] = pd.NA

    out = out[need_cols].copy()
    out["rcept_no"] = out["rcept_no"].astype("string").str.strip()
    out["corp_code"] = out["corp_code"].astype("string").str.strip().str.zfill(8)
    out["iscmp_cmpnm"] = out["iscmp_cmpnm"].astype("string").str.strip()

    out = out[out["rcept_no"].str.match(r"^\d{14}$", na=False)].copy()
    out = out[out["corp_code"].str.match(r"^\d{8}$", na=False)].copy()
    out = out[~out["iscmp_cmpnm"].str.replace(r"\s+", "", regex=True).isin(["합계", "총계", "소계"])].copy()
    out = out[out["iscmp_cmpnm"].notna() & (out["iscmp_cmpnm"] != "")].copy()
    out = out.drop_duplicates(subset=["rcept_no", "corp_code", "iscmp_cmpnm"], keep="first")

    rows: list[tuple[Any, ...]] = []

    for row in out.to_dict(orient="records"):
        record = {
            "rcept_no": _to_str_or_none(row.get("rcept_no")),
            "rcept_dt": _to_date_or_none(row.get("rcept_dt")),
            "corp_cls": _to_str_or_none(row.get("corp_cls")),
            "corp_code": _to_str_or_none(row.get("corp_code")),
            "corp_name": _to_str_or_none(row.get("corp_name")),
            "report_nm": _to_str_or_none(row.get("report_nm")),
            "flr_nm": _to_str_or_none(row.get("flr_nm")),
            "pblntf_ty": _to_str_or_none(row.get("pblntf_ty")),
            "source": _to_str_or_none(row.get("source")),
            "viewer_url": _to_str_or_none(row.get("viewer_url")),
            "iscmp_cmpnm": _to_str_or_none(row.get("iscmp_cmpnm")),
            "trfdtl_trfprc": _to_amount_or_none(row.get("trfdtl_trfprc")),
            "trfdtl_stkcnt": _to_stkcnt_or_none(row.get("trfdtl_stkcnt")),
            "trf_pp": _to_str_or_none(row.get("trf_pp")),
            "row_hash": None,
        }

        required = ["rcept_no", "rcept_dt", "corp_code", "corp_name", "report_nm", "iscmp_cmpnm"]
        if any(record[k] is None for k in required):
            continue

        record["row_hash"] = _build_row_hash(record)
        rows.append(tuple(record[c] for c in DB_COLUMNS))

    return rows


def _upsert_rows(conn: pymysql.connections.Connection, rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, rows)
        affected = cur.rowcount
    conn.commit()
    return affected


def _build_target_frame(
    dart_api_key: str,
    krx_date: Optional[str],
    timeout: int,
    max_retries: int,
    base_sleep: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    krx = KrxApiClient()
    kospi = krx.get_current_kospi_tickers(as_of=krx_date)
    if kospi.empty:
        raise RuntimeError("No KOSPI tickers fetched from KRX.")

    dart = OpenDartClient(
        api_key=dart_api_key,
        timeout=timeout,
        max_retries=max_retries,
        base_sleep=base_sleep,
    )
    corp_df = dart.get_corp_codes(listed_only=True)
    corp_df = corp_df[["stock_code", "corp_code", "corp_name"]].drop_duplicates(subset=["stock_code"]).copy()
    corp_df = corp_df.rename(columns={"corp_name": "dart_corp_name"})

    merged = kospi.merge(corp_df, on="stock_code", how="left")
    matched = merged[merged["corp_code"].notna()].copy()
    unmatched = merged[merged["corp_code"].isna()].copy()

    matched["corp_code"] = matched["corp_code"].astype(str).str.zfill(8)
    matched = matched.sort_values(["stock_code"]).reset_index(drop=True)
    unmatched = unmatched.sort_values(["stock_code"]).reset_index(drop=True)
    return matched, unmatched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch investment events for all current KOSPI stocks and upsert into dart_investment_events.",
    )
    parser.add_argument("--start-date", default="20150101", help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--end-date", default=_today_yyyymmdd(), help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--krx-date", default=None, help="KRX base date (YYYYMMDD). Omit for latest available.")
    parser.add_argument("--dart-api-key", default=None, help="DART API key override")
    parser.add_argument("--reprt-codes", default="11011", help="Comma-separated reprt_code values")
    parser.add_argument("--include-periodic-status", action="store_true")
    parser.add_argument("--include-majorstock-status", action="store_true")
    parser.add_argument("--exclude-note-plan", action="store_true")
    parser.add_argument("--max-note-reports", type=int, default=200)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep-sec", type=float, default=0.03)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--base-sleep", type=float, default=0.8)
    parser.add_argument("--unmatched-out", default="unmatched_kospi_to_dart.csv")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform only, skip DB insert.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    settings = load_settings()
    dart_api_key = (args.dart_api_key or settings.dart_api_key or "").strip()
    if not dart_api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY/OPENDART_API_KEY or pass --dart-api-key.")

    start_ymd = _norm_yyyymmdd(args.start_date)
    end_ymd = _norm_yyyymmdd(args.end_date)
    if start_ymd > end_ymd:
        raise ValueError(f"start_date > end_date: {start_ymd} > {end_ymd}")

    reprt_codes = tuple(c.strip() for c in str(args.reprt_codes).split(",") if c.strip()) or ("11011",)

    targets, unmatched = _build_target_frame(
        dart_api_key=dart_api_key,
        krx_date=args.krx_date,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
    )

    if args.unmatched_out and not unmatched.empty:
        unmatched.to_csv(args.unmatched_out, index=False, encoding="utf-8-sig")

    total_targets = len(targets)
    if args.offset > 0:
        targets = targets.iloc[args.offset :].copy()
    if args.limit is not None:
        targets = targets.iloc[: args.limit].copy()
    targets = targets.reset_index(drop=True)

    if targets.empty:
        raise RuntimeError("No target KOSPI symbols to process after offset/limit.")

    print(
        f"[MAP] kospi_total={total_targets:,}, mapped={total_targets - len(unmatched):,}, "
        f"unmatched={len(unmatched):,}, run_targets={len(targets):,}"
    )

    holdings = CorporateHoldingsModule(
        api_key=dart_api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
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
        for idx, row in targets.iterrows():
            stock_code = str(row.get("stock_code", "")).zfill(6)
            corp_code = str(row.get("corp_code", "")).zfill(8)
            stock_name = str(row.get("stock_name", "")).strip()
            dart_corp_name = str(row.get("dart_corp_name", "")).strip()
            label = dart_corp_name or stock_name

            print(f"[{idx + 1}/{len(targets)}] {stock_code} {label} ({corp_code})")

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
                    max_note_reports=args.max_note_reports,
                )
                combined = dfs.get("combined", pd.DataFrame())
                rows = _prepare_db_rows(combined)

                total_events += len(rows)

                if args.dry_run:
                    print(f"  -> events={len(rows):,} (dry-run)")
                else:
                    assert conn is not None
                    affected = _upsert_rows(conn, rows)
                    total_db_affected += affected
                    print(f"  -> events={len(rows):,}, db_affected={affected:,}")

            except Exception as exc:
                error_count += 1
                if conn is not None:
                    conn.rollback()
                print(f"  -> ERROR: {exc}")

            processed += 1
            if args.sleep_sec > 0:
                time.sleep(args.sleep_sec)
    finally:
        if conn is not None:
            conn.close()

    print(
        f"[DONE] processed={processed:,}, errors={error_count:,}, "
        f"prepared_events={total_events:,}, db_affected={total_db_affected:,}, "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
