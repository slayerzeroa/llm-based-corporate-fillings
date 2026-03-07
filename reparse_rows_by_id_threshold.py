from __future__ import annotations

import argparse
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd
import pymysql
from dotenv import load_dotenv

from config import load_settings
from data_injection import (
    _build_family_payload_from_main_html,
    _build_row_hash,
    _configure_runtime_warnings,
    _enrich_records_with_family_html,
    _load_db_config,
    _prepare_db_records,
    _resolve_db_layout,
    _to_amount_or_none,
    _to_date_or_none,
    _to_stkcnt_or_none,
    _to_str_or_none,
    _upsert_rows,
)
from function import CorporateHoldingsModule, extract_transfer_decision_from_viewer_url


IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
RCEPT_RE = re.compile(r"^\d{14}$")
CANCEL_RE = re.compile(r"취소|철회|중단|해제", re.IGNORECASE)
TARGET_PLACEHOLDER_RE = re.compile(
    r"^(합계|총계|소계|회사명(?:\(국적\))?|기업명|법인명|발행회사|대표자|대표이사|국적|성명|회사와관계|\(회사명\)|\(기업명\)|\(대표자\)|\(국적\))$"
)


def _safe_ident(name: str) -> str:
    raw = str(name).strip()
    if not IDENT_RE.fullmatch(raw):
        raise ValueError(f"invalid identifier: {name}")
    return raw


def _norm_rcept_no(value: Any) -> str:
    s = str(value or "").strip()
    if RCEPT_RE.fullmatch(s):
        return s
    m = re.search(r"rcpNo=(\d{14})", s)
    return m.group(1) if m else ""


def _build_seed_df_by_id_threshold(
    conn: pymysql.connections.Connection,
    source_db: str,
    source_table: str,
    id_threshold: int,
    offset: int = 0,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    src_db = _safe_ident(source_db)
    src_table = _safe_ident(source_table)

    sql = f"""
    SELECT
        f.rcept_no,
        MIN(f.id) AS min_id,
        MAX(f.viewer_url) AS viewer_url,
        MAX(f.corp_cls) AS corp_cls,
        MAX(f.corp_code) AS corp_code,
        MAX(f.corp_name) AS corp_name,
        MAX(f.report_nm) AS report_nm,
        MAX(f.flr_nm) AS flr_nm,
        MAX(f.pblntf_ty) AS pblntf_ty,
        MAX(f.rcept_dt) AS rcept_dt
    FROM `{src_db}`.`{src_table}` f
    WHERE f.id > %s
    GROUP BY f.rcept_no
    ORDER BY MIN(f.id) ASC
    """
    params: list[Any] = [int(id_threshold)]

    if limit is not None and int(limit) > 0:
        sql += " LIMIT %s OFFSET %s"
        params.extend([int(limit), max(int(offset), 0)])
    elif int(offset) > 0:
        sql += " LIMIT 18446744073709551615 OFFSET %s"
        params.append(max(int(offset), 0))

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return pd.DataFrame(rows)


def _load_source_rows_by_rcept(
    conn: pymysql.connections.Connection,
    source_db: str,
    source_table: str,
    rcept_no: str,
) -> pd.DataFrame:
    src_db = _safe_ident(source_db)
    src_table = _safe_ident(source_table)
    sql = f"""
    SELECT
        rcept_no,
        rcept_dt,
        corp_cls,
        corp_code,
        corp_name,
        report_nm,
        flr_nm,
        pblntf_ty,
        source,
        viewer_url,
        iscmp_cmpnm,
        trfdtl_trfprc,
        trfdtl_stkcnt,
        trf_pp
    FROM `{src_db}`.`{src_table}`
    WHERE rcept_no = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (rcept_no,))
        rows = cur.fetchall()
    return pd.DataFrame(rows)


def _normalize_target_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    compact = re.sub(r"\s+", "", s)
    compact = (
        compact.replace("（", "(")
        .replace("）", ")")
        .replace("［", "[")
        .replace("］", "]")
        .replace("｛", "{")
        .replace("｝", "}")
    )
    core = re.sub(r"^[\(\[\{<]+|[\)\]\}>]+$", "", compact)
    compact_plain = re.sub(r"[^0-9A-Za-z가-힣]", "", compact)
    core_plain = re.sub(r"[^0-9A-Za-z가-힣]", "", core)
    if TARGET_PLACEHOLDER_RE.fullmatch(compact):
        return None
    if compact_plain in {
        "회사명",
        "회사명국적",
        "기업명",
        "법인명",
        "발행회사",
        "대표자",
        "대표이사",
        "대표자명",
        "대표이사명",
        "국적",
        "성명",
        "회사와관계",
        "금액",
        "금액원",
        "금액백만원",
        "취득금액",
        "취득금액원",
        "처분금액",
        "처분금액원",
        "양수금액",
        "양수금액원",
        "양도금액",
        "양도금액원",
        "주식수",
        "주식수주",
        "취득주식수",
        "취득주식수주",
        "처분주식수",
        "처분주식수주",
        "양수주식수",
        "양수주식수주",
        "양도주식수",
        "양도주식수주",
        "출자사재무제표",
        "발행회사의요약재무상황",
        "합계",
        "총계",
        "소계",
    }:
        return None
    if compact in {",", ".", "-", "/", "&"}:
        return None
    if re.fullmatch(r"[-,./|&]+", compact):
        return None
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", compact):
        return None
    if re.fullmatch(r"(?:취득|처분|양수|양도)?금액(?:원|백만원)?", compact_plain):
        return None
    if re.fullmatch(r"(?:취득|처분|양수|양도)?주식수(?:주)?", compact_plain):
        return None
    if compact_plain.endswith("재무제표"):
        return None
    if not re.search(r"[A-Za-z가-힣]", compact_plain):
        return None
    if core_plain in {"대표자", "대표이사", "대표자명", "대표이사명", "국적", "회사와관계"}:
        return None
    return s


def _is_cancel_like(report_nm: Any, trf_pp: Any, source: Any) -> bool:
    text = " ".join([str(report_nm or ""), str(trf_pp or ""), str(source or "")])
    return bool(CANCEL_RE.search(text))


def _prepare_db_records_relaxed(df: pd.DataFrame) -> list[dict[str, Any]]:
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
    out = out[out["rcept_no"].str.match(r"^\d{14}$", na=False)].copy()
    out = out[out["corp_code"].str.match(r"^\d{8}$", na=False)].copy()

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
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
            "iscmp_cmpnm": _normalize_target_name(_to_str_or_none(row.get("iscmp_cmpnm"))),
            "trfdtl_trfprc": _to_amount_or_none(row.get("trfdtl_trfprc")),
            "trfdtl_stkcnt": _to_stkcnt_or_none(row.get("trfdtl_stkcnt")),
            "trf_pp": _to_str_or_none(row.get("trf_pp")),
            "row_hash": None,
        }

        required = ["rcept_no", "rcept_dt", "corp_code", "corp_name", "report_nm"]
        if any(record[k] is None for k in required):
            continue
        if (
            record.get("iscmp_cmpnm") is None
            and record.get("trfdtl_trfprc") is None
            and record.get("trfdtl_stkcnt") is None
            and not _is_cancel_like(record.get("report_nm"), record.get("trf_pp"), record.get("source"))
        ):
            continue

        dedup_key = (
            str(record.get("rcept_no") or ""),
            str(record.get("corp_code") or ""),
            str(record.get("iscmp_cmpnm") or ""),
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        record["row_hash"] = _build_row_hash(record)
        records.append(record)
    return records


def _load_decimal_limits(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
    columns: list[str],
) -> dict[str, tuple[int, int]]:
    if not columns:
        return {}
    tbl = _safe_ident(table_name)
    dbn = _safe_ident(db_name)
    cols = [c for c in columns if IDENT_RE.fullmatch(str(c))]
    if not cols:
        return {}
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"""
    SELECT column_name, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = %s
      AND table_name = %s
      AND data_type = 'decimal'
      AND column_name IN ({placeholders})
    """
    params: list[Any] = [dbn, tbl, *cols]
    out: dict[str, tuple[int, int]] = {}
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        for r in cur.fetchall():
            col = str(r.get("column_name") or "")
            p = int(r.get("numeric_precision") or 0)
            s = int(r.get("numeric_scale") or 0)
            if col and p > 0 and s >= 0:
                out[col] = (p, s)
    return out


def _decimal_fits(dec: Decimal, precision: int, scale: int) -> bool:
    s = format(dec.copy_abs(), "f")
    if "." in s:
        int_str, frac_str = s.split(".", 1)
        frac_str = frac_str.rstrip("0")
    else:
        int_str, frac_str = s, ""
    int_digits = len(int_str.lstrip("0")) or 1
    frac_digits = len(frac_str)
    if int_digits > (precision - scale):
        return False
    if frac_digits > scale:
        return False
    return True


def _sanitize_decimal_overflow(
    records: list[dict[str, Any]],
    decimal_limits: dict[str, tuple[int, int]],
) -> int:
    changed = 0
    if not records or not decimal_limits:
        return changed
    for rec in records:
        for col, (precision, scale) in decimal_limits.items():
            if col not in rec:
                continue
            value = rec.get(col)
            if value is None:
                continue
            try:
                dec = Decimal(str(value))
            except (InvalidOperation, ValueError):
                rec[col] = None
                changed += 1
                continue
            if not _decimal_fits(dec, precision, scale):
                rec[col] = None
                changed += 1
                continue
            if scale == 0:
                rec[col] = int(dec)
    return changed


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out: list[dict[str, Any]] = []
    for rec in records:
        key = (
            str(rec.get("rcept_no") or ""),
            str(rec.get("corp_code") or ""),
            str(rec.get("iscmp_cmpnm") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reparse rows where source id > threshold and save reparsed results into a separate table.",
    )
    parser.add_argument("--source-db", default="dart")
    parser.add_argument("--target-db", default=None, help="Defaults to DB_NAME from .env.")
    parser.add_argument("--source-table", default="dart_investment_events_copy_copy")
    parser.add_argument("--target-table", default="dart_investment_events_reparsed_gt_18999")
    parser.add_argument("--id-threshold", type=int, default=18999)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--create-target-like-source", action="store_true")
    parser.add_argument("--truncate-target", action="store_true")
    parser.add_argument("--dart-api-key", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--base-sleep", type=float, default=0.8)
    parser.add_argument("--query-sleep-sec", type=float, default=0.8)
    parser.add_argument("--per-rcept-sleep-sec", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--enrich-family-html", dest="enrich_family_html", action="store_true")
    parser.add_argument("--no-enrich-family-html", dest="enrich_family_html", action="store_false")
    parser.set_defaults(enrich_family_html=True)
    parser.add_argument("--enrich-all-rows", dest="enrich_all_rows", action="store_true")
    parser.add_argument("--enrich-suspicious-only", dest="enrich_all_rows", action="store_false")
    parser.set_defaults(enrich_all_rows=True)
    parser.add_argument("--enrich-max-rpm", type=int, default=4)
    parser.add_argument("--enrich-max-retries", type=int, default=3)
    parser.add_argument("--enrich-backoff-sec", type=float, default=5.0)
    parser.add_argument("--enrich-sleep-sec", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    _configure_runtime_warnings()

    settings = load_settings()
    api_key = (args.dart_api_key or settings.dart_api_key or "").strip()
    if not api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY/OPENDART_API_KEY or pass --dart-api-key.")

    db_cfg = _load_db_config()
    source_db = _safe_ident(args.source_db)
    target_db = _safe_ident(args.target_db or db_cfg.database)
    source_table = _safe_ident(args.source_table)
    target_table = _safe_ident(args.target_table)

    conn = pymysql.connect(
        host=db_cfg.host,
        port=db_cfg.port,
        user=db_cfg.user,
        password=db_cfg.password,
        database=target_db,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

    try:
        if args.create_target_like_source:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS `{target_db}`.`{target_table}` LIKE `{source_db}`.`{source_table}`"
                )
            conn.commit()

        if args.truncate_target:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM `{target_db}`.`{target_table}`")
                try:
                    cur.execute(f"ALTER TABLE `{target_db}`.`{target_table}` AUTO_INCREMENT = 1")
                except Exception:
                    pass
            conn.commit()

        seed_df = _build_seed_df_by_id_threshold(
            conn=conn,
            source_db=source_db,
            source_table=source_table,
            id_threshold=args.id_threshold,
            offset=args.offset,
            limit=args.limit,
        )
        if seed_df.empty:
            print("[DONE] no source rows for given id threshold.")
            return

        seed_df["rcept_no"] = seed_df["rcept_no"].map(_norm_rcept_no)
        seed_df = seed_df[seed_df["rcept_no"].str.match(RCEPT_RE, na=False)].copy()
        seed_df = seed_df.drop_duplicates(subset=["rcept_no"], keep="first").reset_index(drop=True)
        if seed_df.empty:
            print("[DONE] rows found but no valid rcept_no.")
            return

        print(
            f"[LOAD] source={source_db}.{source_table}, target={target_db}.{target_table}, "
            f"id_threshold>{args.id_threshold}, rcepts={len(seed_df):,}"
        )

        if not args.dry_run:
            layout = _resolve_db_layout(conn=conn, db_name=target_db, table_name=target_table)
            decimal_limits = _load_decimal_limits(
                conn=conn,
                db_name=target_db,
                table_name=target_table,
                columns=["trfdtl_trfprc", "trfdtl_stkcnt"],
            )
        else:
            layout = None
            decimal_limits = {}

        holdings = CorporateHoldingsModule(
            api_key=api_key,
            timeout=args.timeout,
            max_retries=args.max_retries,
            base_sleep=args.base_sleep,
            request_interval_sec=max(float(args.query_sleep_sec), 0.0),
        )

        batch_size = max(int(args.batch_size), 1)
        buffer: list[dict[str, Any]] = []
        processed = 0
        ok = 0
        failed = 0
        db_affected_total = 0

        def flush_buffer() -> None:
            nonlocal buffer, db_affected_total
            if not buffer:
                return
            records = _dedupe_records(buffer)
            overflow_fixed = _sanitize_decimal_overflow(records, decimal_limits)
            print(
                f"  -> flush prepare: buffered={len(buffer):,}, deduped={len(records):,}, overflow_fixed={overflow_fixed:,}"
            )

            if args.enrich_family_html:
                _enrich_records_with_family_html(
                    records,
                    enable=True,
                    enrich_all_rows=bool(args.enrich_all_rows),
                    timeout=args.timeout,
                    sleep_sec=args.enrich_sleep_sec,
                    max_rpm=args.enrich_max_rpm,
                    max_retries=args.enrich_max_retries,
                    backoff_sec=args.enrich_backoff_sec,
                    api_key=api_key,
                    include_document_xml=True,
                )

            if args.dry_run:
                print(f"  -> flush dry-run: records={len(records):,}")
                buffer = []
                return

            assert layout is not None
            affected = _upsert_rows(conn=conn, layout=layout, records=records)
            db_affected_total += int(affected)
            print(f"  -> flush done: records={len(records):,}, db_affected={affected:,}")
            buffer = []

        for i, row in enumerate(seed_df.to_dict(orient="records"), start=1):
            rcept_no = str(row.get("rcept_no", "")).strip()
            viewer_url = str(row.get("viewer_url") or f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}")
            print(f"[{i}/{len(seed_df)}] rcept_no={rcept_no}")
            processed += 1
            try:
                meta: dict[str, Any] = {}
                detail_df = extract_transfer_decision_from_viewer_url(
                    viewer_url=viewer_url,
                    api_key=api_key,
                    timeout=args.timeout,
                    verbose=False,
                    seed_row=row,
                    session=holdings.session,
                    request_interval_sec=max(float(args.query_sleep_sec), 0.0),
                    out_meta=meta,
                )
                records = _prepare_db_records(detail_df) if not detail_df.empty else []
                if not records:
                    records = _prepare_db_records_relaxed(detail_df)

                if not records:
                    src_df = _load_source_rows_by_rcept(
                        conn=conn,
                        source_db=source_db,
                        source_table=source_table,
                        rcept_no=rcept_no,
                    )
                    records = _prepare_db_records(src_df)
                    if not records:
                        records = _prepare_db_records_relaxed(src_df)

                if records and args.enrich_family_html:
                    main_html = str(meta.get("main_html") or "").strip()
                    if main_html:
                        payload = _build_family_payload_from_main_html(
                            rcept_no=rcept_no,
                            main_html=main_html,
                        )
                        for rec in records:
                            rec.update(payload)

                if records:
                    ok += 1
                    buffer.extend(records)
                    print(f"  -> records={len(records):,}, buffer={len(buffer):,}")
                else:
                    failed += 1
                    print("  -> no records")
            except Exception as exc:
                failed += 1
                print(f"  -> ERROR: {exc}")

            if len(buffer) >= batch_size:
                flush_buffer()

            if args.per_rcept_sleep_sec > 0 and i < len(seed_df):
                time.sleep(max(float(args.per_rcept_sleep_sec), 0.0))

        flush_buffer()
        print(
            f"[DONE] processed={processed:,}, ok={ok:,}, failed={failed:,}, "
            f"db_affected={db_affected_total:,}, dry_run={args.dry_run}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
