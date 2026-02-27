from __future__ import annotations

import argparse
from collections import deque
import hashlib
import io
import json
import os
import random
import re
import time
import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pymysql
import requests
from dotenv import load_dotenv
from bs4 import XMLParsedAsHTMLWarning

from config import load_settings
from function import CorporateHoldingsModule, KrxApiClient
from function.filling import OpenDartClient


DB_TABLE = "dart_investment_events"

BASE_DB_COLUMNS = [
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

ENRICHMENT_DB_COLUMNS = [
    "family_root_rcept_no",
    "family_rcepts_json",
    "family_member_count",
    "family_alert_base_rcept_no",
    "family_alert_is_fix",
    "main_html_raw",
    "main_html_sha256",
    "main_html_fetched_at",
    "main_html_type",
    "document_html_raw",
    "document_html_sha256",
    "document_html_fetched_at",
    "document_html_format",
    "document_html_entry_name",
    "document_xml_raw",
    "document_xml_sha256",
    "document_xml_fetched_at",
    "document_xml_entry_name",
]

DOCUMENT_XML_URL = "https://opendart.fss.or.kr/api/document.xml"

CANCEL_RE = re.compile(r"취소|철회|해제|중단", re.IGNORECASE)
ALERT_INVEST_NOTICE_RE = re.compile(
    r'alertInvestNotice\(\s*"?(?P<current>\d{14})"?\s*,\s*"?(?P<dcmNo>\d+)"?\s*,\s*"?(?P<base>\d{14})"?\s*,\s*"?(?P<is_fix>[01])"?',
    flags=re.IGNORECASE,
)
FAMILY_SELECT_RE = re.compile(
    r'<select[^>]*id=["\']family["\'][^>]*>(?P<body>.*?)</select>',
    flags=re.IGNORECASE | re.DOTALL,
)
OPTION_VALUE_RE = re.compile(
    r'<option[^>]*value=["\'](?P<value>[^"\']+)["\'][^>]*>(?P<label>.*?)</option>',
    flags=re.IGNORECASE | re.DOTALL,
)
RCP_VALUE_RE = re.compile(r"rcpNo=(\d{14})", flags=re.IGNORECASE)
TARGET_PLACEHOLDER_RE = re.compile(
    r"^(합계|총계|소계|회사명(?:\(국적\))?|기업명|법인명|발행회사|대표자|대표이사|국적|성명|회사와관계|\(회사명\)|\(기업명\)|\(대표자\)|\(국적\))$"
)


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class DbLayout:
    table: str
    columns: tuple[str, ...]
    upsert_sql: str
    available_columns: frozenset[str]


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, period_sec: float = 60.0) -> None:
        self.max_calls = max(int(max_calls), 1)
        self.period_sec = max(float(period_sec), 0.1)
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        now = time.time()
        while self._calls and (now - self._calls[0]) >= self.period_sec:
            self._calls.popleft()

        if len(self._calls) >= self.max_calls:
            wait_sec = self.period_sec - (now - self._calls[0]) + 0.01
            if wait_sec > 0:
                time.sleep(wait_sec)
            now = time.time()
            while self._calls and (now - self._calls[0]) >= self.period_sec:
                self._calls.popleft()

        self._calls.append(now)


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


def _safe_table_name(name: str) -> str:
    table = str(name).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise RuntimeError(f"Invalid table name: {name}")
    return table


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _update_state(path: Path, state: dict[str, Any], **kwargs: Any) -> None:
    for k, v in kwargs.items():
        state[k] = v
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_state(path, state)


def _load_table_columns(conn: pymysql.connections.Connection, table: str, db_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (db_name, table),
        )
        out: set[str] = set()
        for r in cur.fetchall():
            if isinstance(r, dict):
                out.add(str(r.get("column_name")))
            else:
                out.add(str(r[0]))
        return out


def _build_upsert_sql(table: str, columns: list[str]) -> str:
    quoted_cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))

    skip_update = {"id", "created_at"}
    update_cols = [c for c in columns if c not in skip_update]
    update_stmt = ",\n    ".join([f"{c} = VALUES({c})" for c in update_cols])

    if "updated_at" not in columns:
        update_stmt = f"{update_stmt},\n    updated_at = CURRENT_TIMESTAMP"

    return f"""
INSERT INTO {table} (
    {quoted_cols}
)
VALUES (
    {placeholders}
)
ON DUPLICATE KEY UPDATE
    {update_stmt}
"""


def _resolve_db_layout(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
) -> DbLayout:
    table = _safe_table_name(table_name)
    cols = _load_table_columns(conn, table, db_name)
    if not cols:
        raise RuntimeError(f"Table not found or no columns: {table}")

    missing_base = [c for c in BASE_DB_COLUMNS if c not in cols]
    if missing_base:
        raise RuntimeError(f"Table {table} missing required columns: {', '.join(missing_base)}")

    insert_cols = list(BASE_DB_COLUMNS) + [c for c in ENRICHMENT_DB_COLUMNS if c in cols]
    upsert_sql = _build_upsert_sql(table, insert_cols)
    return DbLayout(
        table=table,
        columns=tuple(insert_cols),
        upsert_sql=upsert_sql,
        available_columns=frozenset(cols),
    )


def _norm_yyyymmdd(value: str) -> str:
    raw = str(value).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {value} (YYYYMMDD or YYYY-MM-DD)")
    return raw


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _configure_runtime_warnings() -> None:
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    warnings.filterwarnings(
        "ignore",
        category=FutureWarning,
        message=r"The behavior of DataFrame concatenation with empty or all-NA entries is deprecated\..*",
    )


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


def _text_or_empty(value: Any) -> str:
    if _is_null_like(value):
        return ""
    return str(value).strip()


def _normalize_target_name(value: Any) -> Optional[str]:
    text = _to_str_or_none(value)
    if text is None:
        return None
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    if TARGET_PLACEHOLDER_RE.fullmatch(compact):
        return None
    return text


def _is_cancel_like(report_nm: Any, trf_pp: Any, source: Any) -> bool:
    text = " ".join([_text_or_empty(report_nm), _text_or_empty(trf_pp), _text_or_empty(source)])
    return bool(CANCEL_RE.search(text))


def _extract_rcept_no_from_viewer_url(url: Any) -> str:
    s = _text_or_empty(url)
    m = re.search(r"rcpNo=(\d{14})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{14}", s):
        return s
    return ""


def _extract_family_rcept_nos(main_html: str) -> list[str]:
    out: set[str] = set()
    blocks = FAMILY_SELECT_RE.findall(main_html)
    search_blocks = blocks if blocks else [main_html]
    for block in search_blocks:
        for m in OPTION_VALUE_RE.finditer(block):
            val = str(m.group("value") or "")
            for r in RCP_VALUE_RE.findall(val):
                out.add(r)
        for r in RCP_VALUE_RE.findall(block):
            out.add(r)
    return sorted(out)


def _fetch_family_html_payload(
    session: requests.Session,
    rcept_no: str,
    timeout: int,
) -> dict[str, Any]:
    url = "https://dart.fss.or.kr/dsaf001/main.do"
    resp = session.get(url, params={"rcpNo": rcept_no}, timeout=timeout)
    resp.raise_for_status()
    main_html = resp.text

    members = _extract_family_rcept_nos(main_html)
    if rcept_no not in members:
        members.append(rcept_no)
    members = sorted(set([m for m in members if re.fullmatch(r"\d{14}", str(m))]))

    alert_base = None
    alert_is_fix = 0
    am = ALERT_INVEST_NOTICE_RE.search(main_html)
    if am:
        base = str(am.group("base") or "").strip()
        alert_base = base if re.fullmatch(r"\d{14}", base) else None
        alert_is_fix = int(am.group("is_fix") or "0")

    family_root = alert_base or (members[0] if members else rcept_no)
    html_sha = hashlib.sha256(main_html.encode("utf-8")).hexdigest()
    return {
        "family_root_rcept_no": family_root,
        "family_rcepts_json": json.dumps(members, ensure_ascii=False),
        "family_member_count": len(members),
        "family_alert_base_rcept_no": alert_base,
        "family_alert_is_fix": alert_is_fix,
        "main_html_raw": main_html,
        "main_html_sha256": html_sha,
        "main_html_fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "main_html_type": "HTML_MAIN",
        # mixed slot: if XML is not fetched, document_html_* can still hold HTML payload
        "document_html_raw": main_html,
        "document_html_sha256": html_sha,
        "document_html_fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "document_html_format": "HTML_MAIN",
        "document_html_entry_name": None,
    }


def _decode_text_auto(data: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_xml_from_document_zip(content: bytes) -> tuple[Optional[str], Optional[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            xml_names = [n for n in zf.namelist() if str(n).lower().endswith(".xml")]
            if not xml_names:
                return None, None
            best_name = max(
                xml_names,
                key=lambda n: int(getattr(zf.getinfo(n), "file_size", 0) or 0),
            )
            raw = zf.read(best_name)
            return _decode_text_auto(raw), str(best_name)
    except Exception:
        return None, None


def _fetch_document_xml_payload(
    session: requests.Session,
    api_key: str,
    rcept_no: str,
    timeout: int,
) -> dict[str, Any]:
    if not api_key:
        return {}
    resp = session.get(
        DOCUMENT_XML_URL,
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        timeout=timeout,
    )
    resp.raise_for_status()
    xml_raw, entry_name = _extract_xml_from_document_zip(resp.content)
    if not xml_raw:
        return {}

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    xml_sha = hashlib.sha256(xml_raw.encode("utf-8")).hexdigest()
    return {
        # keep current schema compatibility: main_html_* can store XML payload
        "main_html_raw": xml_raw,
        "main_html_sha256": xml_sha,
        "main_html_fetched_at": fetched_at,
        "main_html_type": "XML",
        # dedicated XML columns
        "document_xml_raw": xml_raw,
        "document_xml_sha256": xml_sha,
        "document_xml_fetched_at": fetched_at,
        "document_xml_entry_name": entry_name,
        # mixed HTML/XML columns (requested)
        "document_html_raw": xml_raw,
        "document_html_sha256": xml_sha,
        "document_html_fetched_at": fetched_at,
        "document_html_format": "XML",
        "document_html_entry_name": entry_name,
    }


def _prepare_db_records(df: pd.DataFrame) -> list[dict[str, Any]]:
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
    out["iscmp_cmpnm"] = out["iscmp_cmpnm"].map(_normalize_target_name)
    out["is_cancel_like"] = out.apply(
        lambda r: _is_cancel_like(r.get("report_nm"), r.get("trf_pp"), r.get("source")),
        axis=1,
    )

    out = out[out["rcept_no"].str.match(r"^\d{14}$", na=False)].copy()
    out = out[out["corp_code"].str.match(r"^\d{8}$", na=False)].copy()
    out = out[
        (out["is_cancel_like"] == True)
        | (out["iscmp_cmpnm"].notna() & (out["iscmp_cmpnm"] != ""))
    ].copy()

    out["_dedup_target"] = out["iscmp_cmpnm"].fillna("").astype(str)
    out = out.drop_duplicates(subset=["rcept_no", "corp_code", "_dedup_target"], keep="first")

    records: list[dict[str, Any]] = []

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

        record["row_hash"] = _build_row_hash(record)
        records.append(record)

    return records


def _enrich_records_with_family_html(
    records: list[dict[str, Any]],
    *,
    enable: bool,
    enrich_all_rows: bool,
    timeout: int,
    sleep_sec: float,
    max_rpm: int,
    max_retries: int,
    backoff_sec: float,
    api_key: str = "",
    include_document_xml: bool = True,
) -> None:
    if not enable or not records:
        return

    # row-level heuristics: 취소/철회 혹은 target 비어있는 공시를 우선 보강
    target_rcepts: set[str] = set()
    for rec in records:
        rcp = _extract_rcept_no_from_viewer_url(rec.get("viewer_url")) or _text_or_empty(rec.get("rcept_no"))
        if not re.fullmatch(r"\d{14}", rcp):
            continue
        if enrich_all_rows:
            target_rcepts.add(rcp)
            continue
        if _is_cancel_like(rec.get("report_nm"), rec.get("trf_pp"), rec.get("source")):
            target_rcepts.add(rcp)
            continue
        if _is_null_like(rec.get("iscmp_cmpnm")):
            target_rcepts.add(rcp)

    if not target_rcepts:
        print("  -> family/html enrich skipped: no target rcept_no selected by enrich policy")
        return

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )
    limiter = SlidingWindowRateLimiter(max_calls=max_rpm, period_sec=60.0)
    payload_by_rcept: dict[str, dict[str, Any]] = {}
    sorted_rcpts = sorted(target_rcepts)
    ok_count = 0
    fail_count = 0
    for idx, rcp in enumerate(sorted_rcpts, start=1):
        attempt = 0
        while True:
            limiter.acquire()
            try:
                payload = _fetch_family_html_payload(sess, rcp, timeout=timeout)
                if include_document_xml and api_key:
                    try:
                        limiter.acquire()
                        payload.update(
                            _fetch_document_xml_payload(
                                session=sess,
                                api_key=api_key,
                                rcept_no=rcp,
                                timeout=timeout,
                            )
                        )
                    except Exception as doc_exc:
                        print(f"  -> WARN document.xml enrich failed rcept_no={rcp}: {doc_exc}")
                payload_by_rcept[rcp] = payload
                ok_count += 1
                break
            except Exception as exc:
                attempt += 1
                if attempt > max_retries:
                    fail_count += 1
                    print(f"  -> WARN family/html enrich failed rcept_no={rcp}: {exc}")
                    break
                wait_retry = max(float(backoff_sec), 0.1) * (2 ** (attempt - 1)) + random.uniform(0.2, 0.8)
                print(
                    f"  -> WARN family/html transient error rcept_no={rcp} "
                    f"(attempt {attempt}/{max_retries}), retry in {wait_retry:.1f}s: {exc}"
                )
                time.sleep(wait_retry)
        if idx < len(sorted_rcpts) and sleep_sec > 0:
            time.sleep(max(float(sleep_sec), 0.0))

    print(
        f"  -> family/html enrich summary: target_rcp={len(sorted_rcpts):,}, "
        f"ok={ok_count:,}, fail={fail_count:,}, rpm={max_rpm}"
    )

    if not payload_by_rcept:
        return

    for rec in records:
        rcp = _extract_rcept_no_from_viewer_url(rec.get("viewer_url")) or _text_or_empty(rec.get("rcept_no"))
        payload = payload_by_rcept.get(rcp)
        if not payload:
            continue
        rec.update(payload)


def _upsert_rows(conn: pymysql.connections.Connection, layout: DbLayout, records: list[dict[str, Any]]) -> int:
    if not records:
        return 0

    rows: list[tuple[Any, ...]] = []
    for rec in records:
        row = []
        for c in layout.columns:
            row.append(rec.get(c))
        rows.append(tuple(row))

    with conn.cursor() as cur:
        cur.executemany(layout.upsert_sql, rows)
        affected = cur.rowcount
    conn.commit()
    return affected


def _build_target_frame(
    dart_api_key: str,
    market: str,
    krx_date: Optional[str],
    timeout: int,
    max_retries: int,
    base_sleep: float,
    request_sleep_sec: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    krx = KrxApiClient()
    market_name = str(market).strip().upper()
    market_df = krx.get_current_market_tickers(market=market_name, as_of=krx_date)
    if market_df.empty:
        raise RuntimeError(f"No {market_name} tickers fetched from KRX.")

    dart = OpenDartClient(
        api_key=dart_api_key,
        timeout=timeout,
        max_retries=max_retries,
        base_sleep=base_sleep,
        request_interval_sec=max(float(request_sleep_sec), 0.0),
    )
    corp_df = dart.get_corp_codes(listed_only=True)
    corp_df = corp_df[["stock_code", "corp_code", "corp_name"]].drop_duplicates(subset=["stock_code"]).copy()
    corp_df = corp_df.rename(columns={"corp_name": "dart_corp_name"})

    merged = market_df.merge(corp_df, on="stock_code", how="left")
    matched = merged[merged["corp_code"].notna()].copy()
    unmatched = merged[merged["corp_code"].isna()].copy()

    matched["corp_code"] = matched["corp_code"].astype(str).str.zfill(8)
    matched = matched.sort_values(["stock_code"]).reset_index(drop=True)
    unmatched = unmatched.sort_values(["stock_code"]).reset_index(drop=True)
    return matched, unmatched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch investment events for current KRX market stocks and upsert into dart_investment_events.",
    )
    parser.add_argument("--db-table", default=None, help="Target DB table name. Defaults to DB_TABLE/TABLE_NAME env.")
    parser.add_argument("--state-file", default=".data_injection_state.json", help="Resume state json path.")
    parser.add_argument("--resume", action="store_true", help="Resume from state file's resume_from_corp_code.")
    parser.add_argument("--resume-from-corp-code", default=None, help="Force resume start from this corp_code.")
    parser.add_argument("--reset-state", action="store_true", help="Delete previous state file before run.")
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
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
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
        "--enrich-family-html",
        action="store_true",
        help="Fetch DART main.do HTML and store family metadata/html when target table has matching columns.",
    )
    parser.add_argument(
        "--enrich-document-xml",
        dest="enrich_document_xml",
        action="store_true",
        help="When enrichment is enabled, also fetch OpenDART document.xml and store xml raw payload columns.",
    )
    parser.add_argument(
        "--no-enrich-document-xml",
        dest="enrich_document_xml",
        action="store_false",
        help="Disable document.xml enrichment.",
    )
    parser.set_defaults(enrich_document_xml=True)
    parser.add_argument(
        "--enrich-all-rows",
        dest="enrich_all_rows",
        action="store_true",
        help="When --enrich-family-html is set, enrich all rows (default: enabled).",
    )
    parser.add_argument(
        "--enrich-suspicious-only",
        dest="enrich_all_rows",
        action="store_false",
        help="When --enrich-family-html is set, enrich only cancel/missing-target rows.",
    )
    parser.set_defaults(enrich_all_rows=True)
    parser.add_argument(
        "--enrich-sleep-sec",
        type=float,
        default=1.0,
        help="Sleep between per-disclosure family/html fetches.",
    )
    parser.add_argument(
        "--enrich-max-rpm",
        type=int,
        default=8,
        help="Max family/html enrichment requests per minute (default: 8).",
    )
    parser.add_argument(
        "--enrich-max-retries",
        type=int,
        default=3,
        help="Max retries per family/html request on transient errors.",
    )
    parser.add_argument(
        "--enrich-backoff-sec",
        type=float,
        default=3.0,
        help="Base backoff seconds for family/html enrichment retries.",
    )
    parser.add_argument("--unmatched-out", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and transform only, skip DB insert.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()
    _configure_runtime_warnings()
    market_name = str(args.market).strip().upper()
    state_path = Path(args.state_file)
    if args.reset_state and state_path.exists():
        state_path.unlink()

    state = _load_state(state_path)
    _update_state(
        state_path,
        state,
        status="starting",
        market=market_name,
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        db_table=(args.db_table or _first_env("DB_TABLE", "TABLE_NAME") or DB_TABLE),
        pid=os.getpid(),
        run_started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

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
        market=market_name,
        krx_date=args.krx_date,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
        request_sleep_sec=args.query_sleep_sec,
    )

    unmatched_out = args.unmatched_out or f"unmatched_{market_name.lower()}_to_dart.csv"
    if unmatched_out and not unmatched.empty:
        unmatched.to_csv(unmatched_out, index=False, encoding="utf-8-sig")

    total_targets = len(targets)
    if args.offset > 0:
        targets = targets.iloc[args.offset :].copy()
    if args.limit is not None:
        targets = targets.iloc[: args.limit].copy()
    targets = targets.reset_index(drop=True)

    resume_corp = None
    if args.resume_from_corp_code:
        resume_corp = str(args.resume_from_corp_code).strip().zfill(8)
    elif args.resume:
        saved = state.get("resume_from_corp_code")
        if saved:
            resume_corp = str(saved).strip().zfill(8)
        else:
            print(f"[RESUME] no resume point in {state_path}")

    if resume_corp:
        idxs = targets.index[targets["corp_code"].astype(str).str.zfill(8) == resume_corp].tolist()
        if idxs:
            start_idx = int(idxs[0])
            targets = targets.iloc[start_idx:].copy().reset_index(drop=True)
            print(f"[RESUME] start from corp_code={resume_corp}, remaining_targets={len(targets):,}")
            _update_state(
                state_path,
                state,
                resume_applied=True,
                resume_from_corp_code=resume_corp,
                resume_remaining_targets=int(len(targets)),
            )
        else:
            print(f"[RESUME] corp_code={resume_corp} not found in current target set, run from beginning.")
            _update_state(
                state_path,
                state,
                resume_applied=False,
                resume_from_corp_code=resume_corp,
                resume_miss=True,
            )

    if targets.empty:
        raise RuntimeError("No target KOSPI symbols to process after offset/limit.")

    print(
        f"[MAP] market={market_name}, total={total_targets:,}, mapped={total_targets - len(unmatched):,}, "
        f"unmatched={len(unmatched):,}, run_targets={len(targets):,}"
    )
    print(f"[QUERY] request_sleep_sec={max(float(args.query_sleep_sec), 0.0):.3f}")

    holdings = CorporateHoldingsModule(
        api_key=dart_api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
        request_interval_sec=args.query_sleep_sec,
    )

    conn: Optional[pymysql.connections.Connection] = None
    layout: Optional[DbLayout] = None
    db_table = args.db_table or _first_env("DB_TABLE", "TABLE_NAME") or DB_TABLE
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
        layout = _resolve_db_layout(conn=conn, db_name=db_cfg.database, table_name=db_table)
        extra_cols = [c for c in ENRICHMENT_DB_COLUMNS if c in layout.available_columns]
        print(f"[DB] table={layout.table}, base_cols={len(BASE_DB_COLUMNS)}, extra_cols={len(extra_cols)}")
        if extra_cols:
            print(f"[DB] enrichment columns available: {', '.join(extra_cols)}")
            if not args.enrich_family_html:
                print("[DB] note: enrichment columns will stay NULL unless --enrich-family-html is set.")
        elif args.enrich_family_html:
            print("[DB] enrichment requested but target table has no enrichment columns; metadata/html will be fetched but not stored.")

    processed = 0
    error_count = 0
    total_events = 0
    total_db_affected = 0
    interrupted = False

    try:
        for idx, row in targets.iterrows():
            stock_code = str(row.get("stock_code", "")).zfill(6)
            corp_code = str(row.get("corp_code", "")).zfill(8)
            stock_name = str(row.get("stock_name", "")).strip()
            dart_corp_name = str(row.get("dart_corp_name", "")).strip()
            label = dart_corp_name or stock_name

            print(f"[{idx + 1}/{len(targets)}] {stock_code} {label} ({corp_code})")
            _update_state(
                state_path,
                state,
                status="running",
                current_index=int(idx),
                current_stock_code=stock_code,
                current_corp_code=corp_code,
                current_label=label,
                resume_from_corp_code=corp_code,
                processed=int(processed),
                errors=int(error_count),
                prepared_events=int(total_events),
                db_affected=int(total_db_affected),
            )

            symbol_error: Optional[str] = None
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
                records = _prepare_db_records(combined)

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
                        api_key=dart_api_key,
                        include_document_xml=bool(args.enrich_document_xml),
                    )

                total_events += len(records)

                if args.dry_run:
                    print(f"  -> events={len(records):,} (dry-run)")
                else:
                    assert conn is not None and layout is not None
                    affected = _upsert_rows(conn, layout, records)
                    total_db_affected += affected
                    print(f"  -> events={len(records):,}, db_affected={affected:,}")

            except Exception as exc:
                error_count += 1
                symbol_error = str(exc)
                if conn is not None:
                    conn.rollback()
                print(f"  -> ERROR: {exc}")

            processed += 1
            next_corp_code = None
            if idx < len(targets) - 1:
                next_corp_code = str(targets.iloc[idx + 1].get("corp_code", "")).zfill(8)

            _update_state(
                state_path,
                state,
                status="running",
                last_completed_index=int(idx),
                last_completed_stock_code=stock_code,
                last_completed_corp_code=corp_code,
                last_completed_label=label,
                last_completed_error=symbol_error,
                resume_from_corp_code=(next_corp_code or ""),
                processed=int(processed),
                errors=int(error_count),
                prepared_events=int(total_events),
                db_affected=int(total_db_affected),
            )

            if idx < len(targets) - 1:
                if args.sleep_sec is not None:
                    wait_sec = max(float(args.sleep_sec), 0.0)
                else:
                    base_sec = max(float(args.sleep_base_sec), 0.0)
                    jitter_min = max(int(args.sleep_jitter_min), 0)
                    jitter_max = max(int(args.sleep_jitter_max), jitter_min)
                    wait_sec = base_sec + random.randint(jitter_min, jitter_max)

                if wait_sec > 0:
                    print(f"  -> sleep {wait_sec:.1f}s before next symbol")
                    time.sleep(wait_sec)
    except KeyboardInterrupt:
        interrupted = True
        _update_state(
            state_path,
            state,
            status="interrupted",
            interrupted_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            processed=int(processed),
            errors=int(error_count),
            prepared_events=int(total_events),
            db_affected=int(total_db_affected),
        )
    finally:
        if conn is not None:
            conn.close()

    if interrupted:
        print(
            f"[INTERRUPTED] processed={processed:,}, errors={error_count:,}, "
            f"resume_from={state.get('resume_from_corp_code') or 'N/A'}"
        )
        print(
            f"[RESUME CMD] python data_injection.py --db-table {db_table} --market {market_name} "
            f"--start-date {args.start_date} --end-date {args.end_date} --resume --state-file {state_path}"
        )
        return

    _update_state(
        state_path,
        state,
        status="completed",
        completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        processed=int(processed),
        errors=int(error_count),
        prepared_events=int(total_events),
        db_affected=int(total_db_affected),
        resume_from_corp_code="",
    )

    print(
        f"[DONE] processed={processed:,}, errors={error_count:,}, "
        f"prepared_events={total_events:,}, db_affected={total_db_affected:,}, "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
