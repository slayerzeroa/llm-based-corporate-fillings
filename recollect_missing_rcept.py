from __future__ import annotations

import argparse
import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pymysql
from dotenv import load_dotenv

from config import load_settings
from data_injection import (
    _build_row_hash,
    _configure_runtime_warnings,
    _enrich_records_with_family_html,
    _fetch_family_html_payload,
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
TRANSFER_REPORT_RE = re.compile(
    r"타법인\s*주식\s*및\s*출자증권\s*(?:처분결정|양도결정|취득결정|양수결정)"
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


def _pick_target_db(default_db: str) -> str:
    return _safe_ident(default_db)


def _build_missing_seed_df(
    conn: pymysql.connections.Connection,
    source_db: str,
    source_table: str,
    target_db: str,
    target_table: str,
    offset: int = 0,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    src_db = _safe_ident(source_db)
    src_table = _safe_ident(source_table)
    tgt_db = _safe_ident(target_db)
    tgt_table = _safe_ident(target_table)

    sql = f"""
    SELECT
        f.rcept_no,
        MAX(f.viewer_url) AS viewer_url,
        MAX(f.corp_cls) AS corp_cls,
        MAX(f.corp_code) AS corp_code,
        MAX(f.corp_name) AS corp_name,
        MAX(f.report_nm) AS report_nm,
        MAX(f.flr_nm) AS flr_nm,
        MAX(f.pblntf_ty) AS pblntf_ty,
        MAX(f.rcept_dt) AS rcept_dt
    FROM `{src_db}`.`{src_table}` f
    LEFT JOIN (
        SELECT DISTINCT rcept_no
        FROM `{tgt_db}`.`{tgt_table}`
    ) d
      ON d.rcept_no = f.rcept_no
    WHERE d.rcept_no IS NULL
    GROUP BY f.rcept_no
    ORDER BY f.rcept_no DESC
    """

    params: list[Any] = []
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


def _build_seed_df_from_rcepts(
    conn: pymysql.connections.Connection,
    source_db: str,
    source_table: str,
    rcept_nos: list[str],
) -> pd.DataFrame:
    src_db = _safe_ident(source_db)
    src_table = _safe_ident(source_table)
    clean = [x for x in [_norm_rcept_no(v) for v in rcept_nos] if RCEPT_RE.fullmatch(x)]
    if not clean:
        return pd.DataFrame()
    placeholders = ", ".join(["%s"] * len(clean))
    sql = f"""
    SELECT
        rcept_no,
        MAX(viewer_url) AS viewer_url,
        MAX(corp_cls) AS corp_cls,
        MAX(corp_code) AS corp_code,
        MAX(corp_name) AS corp_name,
        MAX(report_nm) AS report_nm,
        MAX(flr_nm) AS flr_nm,
        MAX(pblntf_ty) AS pblntf_ty,
        MAX(rcept_dt) AS rcept_dt
    FROM `{src_db}`.`{src_table}`
    WHERE rcept_no IN ({placeholders})
    GROUP BY rcept_no
    ORDER BY rcept_no DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(clean))
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
            "iscmp_cmpnm": _to_str_or_none(row.get("iscmp_cmpnm")),
            "trfdtl_trfprc": _to_amount_or_none(row.get("trfdtl_trfprc")),
            "trfdtl_stkcnt": _to_stkcnt_or_none(row.get("trfdtl_stkcnt")),
            "trf_pp": _to_str_or_none(row.get("trf_pp")),
            "row_hash": None,
        }

        required = ["rcept_no", "rcept_dt", "corp_code", "corp_name", "report_nm"]
        if any(record[k] is None for k in required):
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

    int_digits = len(int_str.lstrip("0"))
    if int_digits == 0:
        int_digits = 1
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recollect missing rcept_no rows by crawling DART viewer/document and upsert into target table."
        )
    )
    parser.add_argument("--source-db", default="fdm")
    parser.add_argument("--target-db", default=None, help="Defaults to DB_NAME from .env.")
    parser.add_argument("--source-table", default="dart_investment_events_copy")
    parser.add_argument("--target-table", default="dart_investment_events_copy")
    parser.add_argument("--dart-api-key", default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--resume-from-rcept-no", default=None)
    parser.add_argument(
        "--rcept-no",
        default=None,
        help="Process specific rcept_no(s) only. Comma-separated allowed.",
    )
    parser.add_argument(
        "--include-non-transfer",
        action="store_true",
        help="If set, try recollect even when report_nm is not transfer/acquire style.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--base-sleep", type=float, default=0.8)
    parser.add_argument("--query-sleep-sec", type=float, default=0.8)
    parser.add_argument("--per-rcept-sleep-sec", type=float, default=0.0)
    parser.add_argument("--enrich-family-html", action="store_true")
    parser.add_argument("--enrich-all-rows", dest="enrich_all_rows", action="store_true")
    parser.add_argument(
        "--enrich-suspicious-only",
        dest="enrich_all_rows",
        action="store_false",
        help="Only enrich cancel/missing-target rows.",
    )
    parser.set_defaults(enrich_all_rows=True)
    parser.add_argument("--enrich-max-rpm", type=int, default=8)
    parser.add_argument("--enrich-max-retries", type=int, default=3)
    parser.add_argument("--enrich-backoff-sec", type=float, default=3.0)
    parser.add_argument("--enrich-sleep-sec", type=float, default=1.0)
    parser.add_argument(
        "--save-document-text-dir",
        default=None,
        help="If set, save document.xml extracted plain text to this directory.",
    )
    parser.add_argument(
        "--save-viewer-text-dir",
        default=None,
        help="If set, save viewer plain text to this directory.",
    )
    parser.add_argument(
        "--save-family-dir",
        default=None,
        help="If set with --enrich-family-html, save per-rcept family payload json files.",
    )
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
    target_db = _pick_target_db(args.target_db or db_cfg.database)
    source_db = _safe_ident(args.source_db)
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
        if args.rcept_no:
            rcept_list = [x.strip() for x in str(args.rcept_no).split(",") if x.strip()]
            seed_df = _build_seed_df_from_rcepts(
                conn=conn,
                source_db=source_db,
                source_table=source_table,
                rcept_nos=rcept_list,
            )
        else:
            seed_df = _build_missing_seed_df(
                conn=conn,
                source_db=source_db,
                source_table=source_table,
                target_db=target_db,
                target_table=target_table,
                offset=args.offset,
                limit=args.limit,
            )
        if seed_df.empty and args.rcept_no:
            rcept_list = [x.strip() for x in str(args.rcept_no).split(",") if x.strip()]
            clean = [x for x in [_norm_rcept_no(v) for v in rcept_list] if RCEPT_RE.fullmatch(x)]
            if clean:
                seed_df = pd.DataFrame(
                    [
                        {
                            "rcept_no": rc,
                            "viewer_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rc}",
                            "corp_cls": pd.NA,
                            "corp_code": pd.NA,
                            "corp_name": pd.NA,
                            "report_nm": "",
                            "flr_nm": pd.NA,
                            "pblntf_ty": pd.NA,
                            "rcept_dt": pd.NA,
                        }
                        for rc in clean
                    ]
                )

        if seed_df.empty:
            print("[DONE] no missing rcept_no found.")
            return

        seed_df["rcept_no"] = seed_df["rcept_no"].map(_norm_rcept_no)
        seed_df = seed_df[seed_df["rcept_no"].str.match(RCEPT_RE, na=False)].copy()
        if seed_df.empty:
            print("[DONE] missing rows exist but no valid rcept_no.")
            return

        if args.resume_from_rcept_no:
            resume_key = _norm_rcept_no(args.resume_from_rcept_no)
            idxs = seed_df.index[seed_df["rcept_no"] == resume_key].tolist()
            if idxs:
                seed_df = seed_df.loc[idxs[0] :].copy()
                print(f"[RESUME] from rcept_no={resume_key}, remaining={len(seed_df):,}")
            else:
                print(f"[RESUME] rcept_no={resume_key} not found in missing set; run full missing set.")

        save_doc_dir: Optional[Path] = None
        save_viewer_dir: Optional[Path] = None
        save_family_dir: Optional[Path] = None
        if args.save_document_text_dir:
            save_doc_dir = Path(str(args.save_document_text_dir))
            save_doc_dir.mkdir(parents=True, exist_ok=True)
        if args.save_viewer_text_dir:
            save_viewer_dir = Path(str(args.save_viewer_text_dir))
            save_viewer_dir.mkdir(parents=True, exist_ok=True)
        if args.save_family_dir:
            save_family_dir = Path(str(args.save_family_dir))
            save_family_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"[LOAD] missing_rcept={len(seed_df):,}, source={source_db}.{source_table}, "
            f"target={target_db}.{target_table}"
        )

        holdings = CorporateHoldingsModule(
            api_key=api_key,
            timeout=args.timeout,
            max_retries=args.max_retries,
            base_sleep=args.base_sleep,
            request_interval_sec=max(float(args.query_sleep_sec), 0.0),
        )

        all_records: list[dict[str, Any]] = []
        ok = 0
        failed = 0
        skipped_non_transfer = 0
        fallback_source_rows = 0
        fallback_used_rcepts = 0
        parsed_records_total = 0
        relaxed_records_total = 0

        for i, row in enumerate(seed_df.to_dict(orient="records"), start=1):
            rcept_no = str(row.get("rcept_no", "")).strip()
            report_nm = str(row.get("report_nm") or "").strip()
            viewer_url = str(row.get("viewer_url") or "").strip()
            if not viewer_url:
                viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

            if (not args.include_non_transfer) and (not TRANSFER_REPORT_RE.search(report_nm)):
                skipped_non_transfer += 1
                continue

            print(f"[{i}/{len(seed_df)}] rcept_no={rcept_no} report_nm={report_nm}")
            try:
                if save_viewer_dir is not None:
                    viewer_text = holdings._fetch_viewer_plain_text(rcept_no)
                    (save_viewer_dir / f"{rcept_no}.txt").write_text(viewer_text or "", encoding="utf-8")
                if save_doc_dir is not None:
                    doc_text = holdings._fetch_document_plain_text(rcept_no)
                    (save_doc_dir / f"{rcept_no}.txt").write_text(doc_text or "", encoding="utf-8")
                if args.enrich_family_html and save_family_dir is not None:
                    family_payload = _fetch_family_html_payload(holdings.session, rcept_no, timeout=args.timeout)
                    (save_family_dir / f"{rcept_no}.json").write_text(
                        json.dumps(family_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                detail_df = extract_transfer_decision_from_viewer_url(
                    viewer_url=viewer_url,
                    api_key=api_key,
                    timeout=args.timeout,
                    verbose=False,
                    seed_row=row,
                    session=holdings.session,
                    request_interval_sec=max(float(args.query_sleep_sec), 0.0),
                )
                if detail_df.empty:
                    parsed_records = []
                else:
                    parsed_records = _prepare_db_records(detail_df)
                    if not parsed_records:
                        parsed_records = _prepare_db_records_relaxed(detail_df)
                        if parsed_records:
                            relaxed_records_total += len(parsed_records)

                if parsed_records:
                    all_records.extend(parsed_records)
                    parsed_records_total += len(parsed_records)
                    ok += 1
                    print(f"  -> parsed rows={len(detail_df):,}, records={len(parsed_records):,}")
                else:
                    src_df = _load_source_rows_by_rcept(
                        conn=conn,
                        source_db=source_db,
                        source_table=source_table,
                        rcept_no=rcept_no,
                    )
                    src_records = _prepare_db_records(src_df)
                    if not src_records:
                        src_records = _prepare_db_records_relaxed(src_df)
                        if src_records:
                            relaxed_records_total += len(src_records)
                    if src_records:
                        all_records.extend(src_records)
                        fallback_source_rows += len(src_records)
                        fallback_used_rcepts += 1
                        ok += 1
                        print(
                            f"  -> fallback source rows used: rows={len(src_df):,}, records={len(src_records):,}"
                        )
                    else:
                        failed += 1
                        print("  -> parsed/fallback both empty")
            except Exception as exc:
                failed += 1
                print(f"  -> ERROR: {exc}")

            if args.per_rcept_sleep_sec > 0 and i < len(seed_df):
                time.sleep(max(float(args.per_rcept_sleep_sec), 0.0))

        if not all_records:
            print(
                f"[DONE] records=0, ok={ok:,}, failed={failed:,}, "
                f"skipped_non_transfer={skipped_non_transfer:,}"
            )
            return

        dedup_key = set()
        records: list[dict[str, Any]] = []
        for rec in all_records:
            key = (
                str(rec.get("rcept_no") or ""),
                str(rec.get("corp_code") or ""),
                str(rec.get("iscmp_cmpnm") or ""),
            )
            if key in dedup_key:
                continue
            dedup_key.add(key)
            records.append(rec)

        decimal_limits = _load_decimal_limits(
            conn=conn,
            db_name=target_db,
            table_name=target_table,
            columns=["trfdtl_trfprc", "trfdtl_stkcnt"],
        )
        overflow_fixed = _sanitize_decimal_overflow(records, decimal_limits)
        print(
            f"[BUILD] records={len(records):,}, parsed_records={parsed_records_total:,}, "
            f"fallback_records={fallback_source_rows:,}, relaxed_records={relaxed_records_total:,}, "
            f"fallback_rcepts={fallback_used_rcepts:,}, overflow_fixed={overflow_fixed:,}, "
            f"ok={ok:,}, failed={failed:,}, skipped_non_transfer={skipped_non_transfer:,}"
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
            )
            if args.save_document_text_dir:
                meta_path = Path(str(args.save_document_text_dir)) / "_family_meta.json"
                family_meta = []
                for r in records:
                    family_meta.append(
                        {
                            "rcept_no": r.get("rcept_no"),
                            "family_root_rcept_no": r.get("family_root_rcept_no"),
                            "family_rcepts_json": r.get("family_rcepts_json"),
                            "family_member_count": r.get("family_member_count"),
                            "family_alert_base_rcept_no": r.get("family_alert_base_rcept_no"),
                            "family_alert_is_fix": r.get("family_alert_is_fix"),
                        }
                    )
                meta_path.write_text(json.dumps(family_meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.dry_run:
            print("[DRY-RUN] skip DB upsert")
            return

        layout = _resolve_db_layout(conn=conn, db_name=target_db, table_name=target_table)
        affected = _upsert_rows(conn=conn, layout=layout, records=records)
        print(f"[DONE] db_affected={affected:,}, target={target_db}.{target_table}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
