from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pymysql
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import load_settings
from function.filling import MAIN_URL, VIEWER_URL, OpenDartClient
from llm.providers import LlmInvesteeNameResult, build_name_resolver


BAD_NAME_TOKENS = {
    "",
    "-",
    "--",
    "대표자",
    "(대표자)",
    "국적",
    "(국적)",
    "회사명",
    "(회사명)",
    "회사명(국적)",
    "발행회사",
    "(발행회사)",
    "법인명",
    "(법인명)",
}
BAD_NAME_NORMALIZED = {re.sub(r"\s+", "", x) for x in BAD_NAME_TOKENS}
BAD_NAME_PHRASES = (
    "타법인",
    "출자증권",
    "취득결정",
    "처분결정",
    "양수결정",
    "양도결정",
    "발행회사",
    "대표이사",
    "대표자",
    "성명",
    "국적",
    "처분내역",
    "취득내역",
    "양도내역",
    "양수내역",
    "공시",
)

INVESTEE_LABEL_KEYS = {
    "회사명(국적)",
    "회사명",
    "발행회사(회사명)",
    "발행회사",
    "발행회사명",
    "대상법인(회사명)",
    "대상법인",
    "대상회사",
    "대상회사명",
    "법인명",
}

BOUNDARY_LABEL_KEYS = INVESTEE_LABEL_KEYS | {
    "국적",
    "(국적)",
    "대표자",
    "대표이사",
    "자본금(원)",
    "자본금",
    "회사와관계",
    "발행주식총수(주)",
    "발행주식총수",
    "주요사업",
    "처분주식수(주)",
    "처분금액(원)",
    "양수주식수(주)",
    "양수금액(원)",
}


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table: str


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        max_calls: int,
        period_sec: float = 60.0,
        logger: Optional[logging.Logger] = None,
        label: str = "api",
    ) -> None:
        self.max_calls = max(int(max_calls), 1)
        self.period_sec = max(float(period_sec), 0.1)
        self.logger = logger
        self.label = label
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()
        while self._timestamps and (now - self._timestamps[0] >= self.period_sec):
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_calls:
            wait_sec = self.period_sec - (now - self._timestamps[0]) + 0.01
            wait_sec = max(wait_sec, 0.0)
            if self.logger and wait_sec > 0:
                self.logger.info(
                    "  -> %s rate-limit wait %.2fs (%s/%s per %.0fs)",
                    self.label,
                    wait_sec,
                    len(self._timestamps),
                    self.max_calls,
                    self.period_sec,
                )
            if wait_sec > 0:
                time.sleep(wait_sec)

            now = time.monotonic()
            while self._timestamps and (now - self._timestamps[0] >= self.period_sec):
                self._timestamps.popleft()

        self._timestamps.append(time.monotonic())


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _clean_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _load_db_config(*, table_override: Optional[str], allow_original_table: bool) -> DbConfig:
    host = _first_env("DB_HOST")
    user = _first_env("DB_USER", "DB_USERNAME")
    password = _first_env("DB_PASSWORD", "DB_PASS") or ""
    database = _first_env("DB_NAME", "DB_DATABASE")
    table = table_override or _first_env("DB_TABLE", "TABLE_NAME") or "dart_investment_events_copy"
    port_raw = _first_env("DB_PORT") or "3306"

    host = _clean_env_value(host)
    user = _clean_env_value(user)
    password = _clean_env_value(password) or ""
    database = _clean_env_value(database)
    table = _clean_env_value(table)
    port_raw = _clean_env_value(port_raw) or "3306"

    missing = []
    if not host:
        missing.append("DB_HOST")
    if not user:
        missing.append("DB_USER")
    if not database:
        missing.append("DB_NAME")
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    if not table:
        raise RuntimeError("DB table name is empty.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise RuntimeError(f"Invalid DB table name: {table}")
    if (table == "dart_investment_events") and (not allow_original_table):
        raise RuntimeError(
            "Safety block: target table is dart_investment_events (original). "
            "Use dart_investment_events_copy or pass --allow-original-table explicitly."
        )

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
        table=table,
    )


def _default_log_file() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("logs") / f"iscmp_clean_{ts}.log")


def _default_changes_out() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(Path("logs") / f"iscmp_clean_changes_{ts}.csv")


def _configure_logger(log_file: str) -> logging.Logger:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("llm_iscmp_clean")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def _collapse_spaces(text: Any) -> str:
    if text is None:
        return ""
    try:
        if pd.isna(text):
            return ""
    except Exception:
        pass
    return re.sub(r"\s+", " ", str(text)).strip()


def _normalize_token(text: Any) -> str:
    s = _collapse_spaces(text).replace(" ", "")
    return s


def _sanitize_company_name(name: Any) -> Optional[str]:
    if name is None:
        return None
    s = _collapse_spaces(name).strip(" \t\r\n:;,-")
    if not s:
        return None
    if s.lower() in {"<na>", "nan", "none", "null"}:
        return None

    # 라벨 접두 제거
    s = re.sub(r"^[\-\u2022]+\s*", "", s).strip()
    s = re.sub(r"^\(?성명\)?\s*", "", s).strip()
    s = re.sub(r"^\(?대표자\)?\s*", "", s).strip()
    s = re.sub(r"^\(?대표이사\)?\s*", "", s).strip()
    s = re.sub(r"^(회사명\(국적\)|회사명|발행회사)\s*[:：]?\s*", "", s).strip()
    s = re.sub(r"\(?대표자\)?$", "", s).strip()
    s = re.sub(r"\(?대표이사\)?$", "", s).strip()
    s = re.sub(r"[\(\[\{]\s*$", "", s).strip()
    if not s:
        return None
    return s


def _normalize_label_key(label: Any) -> str:
    key = _collapse_spaces(label)
    key = re.sub(r"\s+", "", key)
    key = re.sub(r"^[\-\u2022]+", "", key)
    key = re.sub(r"^\d+\.", "", key)
    key = key.strip(" :：-")
    return key


def _is_suspicious_company_name(name: Any) -> bool:
    s = _sanitize_company_name(name)
    if not s:
        return True
    n = _normalize_token(s)
    if n in BAD_NAME_NORMALIZED:
        return True
    if re.fullmatch(r"[\(\[]?(대표자|국적|회사명(\(국적\))?|발행회사|법인명)[\)\]]?", n):
        return True
    if len(n) <= 1:
        return True
    return False


def _is_valid_candidate_name(name: Any) -> bool:
    s = _sanitize_company_name(name)
    if not s:
        return False
    if _is_suspicious_company_name(s):
        return False
    if len(s) > 80:
        return False
    if any(p in s for p in BAD_NAME_PHRASES):
        return False
    if re.search(r"\b\d+\.", s):
        return False
    if s.count(" ") > 8:
        return False
    return True


def _is_same_name(left: Any, right: Any) -> bool:
    return _normalize_token(left).lower() == _normalize_token(right).lower()


def _extract_rcp_no(viewer_url: str) -> str:
    s = str(viewer_url or "").strip()
    match = re.search(r"rcpNo=(\d{14})", s)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{14}", s):
        return s
    raise ValueError(f"Invalid viewer_url/rcpNo: {viewer_url}")


def _pick_best_viewdoc_candidate(main_html: str) -> Optional[dict[str, str]]:
    # 7 args variant (newer/legacy mixed): viewDoc(rcpNo, dcmNo, eleId, offset, length, dtd, tocNo)
    pattern7 = re.compile(
        r"""viewDoc\(
            \s*['"](?P<rcpNo>\d{14})['"]\s*,\s*
            ['"](?P<dcmNo>\d+)['"]\s*,\s*
            ['"](?P<eleId>\d+)['"]\s*,\s*
            ['"](?P<offset>\d+)['"]\s*,\s*
            ['"](?P<length>\d+)['"]\s*,\s*
            ['"](?P<dtd>[^'"]+)['"]\s*,\s*
            ['"](?P<tocNo>[^'"]*)['"]\s*
        \)""",
        flags=re.IGNORECASE | re.VERBOSE,
    )
    # 6 args variant
    pattern6 = re.compile(
        r"""viewDoc\(
            \s*['"](?P<rcpNo>\d{14})['"]\s*,\s*
            ['"](?P<dcmNo>\d+)['"]\s*,\s*
            ['"](?P<eleId>\d+)['"]\s*,\s*
            ['"](?P<offset>\d+)['"]\s*,\s*
            ['"](?P<length>\d+)['"]\s*,\s*
            ['"](?P<dtd>[^'"]+)['"]\s*
        \)""",
        flags=re.IGNORECASE | re.VERBOSE,
    )

    candidates: list[dict[str, str]] = []
    for m in pattern7.finditer(main_html):
        c = m.groupdict()
        st, ed = m.span()
        c["_ctx"] = main_html[max(0, st - 240) : min(len(main_html), ed + 240)]
        candidates.append(c)

    for m in pattern6.finditer(main_html):
        c = m.groupdict()
        c["tocNo"] = ""
        st, ed = m.span()
        c["_ctx"] = main_html[max(0, st - 240) : min(len(main_html), ed + 240)]
        candidates.append(c)

    if not candidates:
        return None

    keywords = [
        "타법인주식및출자증권처분결정",
        "타법인주식및출자증권취득결정",
        "타법인주식및출자증권양도결정",
        "타법인주식및출자증권양수결정",
        "타법인 주식 및 출자증권",
    ]
    scored: list[tuple[int, dict[str, str]]] = []
    for c in candidates:
        score = 0
        ctx = c.get("_ctx", "")
        if any(k in ctx for k in keywords):
            score += 10
        dtd = str(c.get("dtd", "")).lower()
        if "html" in dtd:
            score += 2
        if str(c.get("tocNo", "")).strip():
            score += 1
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _fetch_viewer_html_for_cleanup(
    dart: OpenDartClient,
    viewer_url: str,
    *,
    retries: int,
    sleep_sec: float,
) -> tuple[Optional[str], str]:
    rcp_no = _extract_rcp_no(viewer_url)
    last_error: Optional[Exception] = None

    for attempt in range(max(int(retries), 1)):
        try:
            resp = dart.session.get(MAIN_URL, params={"rcpNo": rcp_no}, timeout=dart.timeout)
            resp.raise_for_status()
            main_html = resp.text
            cand = _pick_best_viewdoc_candidate(main_html)
            if not cand:
                return None, "MAIN_NO_VIEWDOC"

            params = {
                "rcpNo": cand["rcpNo"],
                "dcmNo": cand["dcmNo"],
                "eleId": cand["eleId"],
                "offset": cand["offset"],
                "length": cand["length"],
                "dtd": cand["dtd"],
            }
            toc_no = str(cand.get("tocNo", "")).strip()
            if toc_no:
                params["tocNo"] = toc_no

            v = dart.session.get(VIEWER_URL, params=params, timeout=dart.timeout)
            v.raise_for_status()
            return v.text, "VIEWER_HTML_GENERIC"
        except Exception as exc:
            last_error = exc
            if attempt < max(int(retries), 1) - 1 and sleep_sec > 0:
                time.sleep(sleep_sec * (attempt + 1))

    raise RuntimeError(f"viewer html fetch failed: {last_error}")


def _build_viewer_focus_text(viewer_html: str, max_lines: int = 120) -> str:
    if not viewer_html:
        return ""
    soup = BeautifulSoup(viewer_html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [_collapse_spaces(x) for x in text.splitlines() if _collapse_spaces(x)]
    if not lines:
        return ""

    keywords = ("발행회사", "회사명(국적)", "회사명", "대상법인", "양도내역", "양수내역")
    picked = []
    for idx, line in enumerate(lines):
        if any(k in line for k in keywords):
            st = max(idx - 2, 0)
            ed = min(idx + 4, len(lines))
            picked.extend(lines[st:ed])

    if not picked:
        picked = lines[:max_lines]
    picked = picked[:max_lines]
    return "\n".join(picked)


def _extract_name_from_viewer_html_legacy(viewer_html: str) -> Optional[str]:
    if not viewer_html:
        return None
    soup = BeautifulSoup(viewer_html, "html.parser")

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [_collapse_spaces(c) for c in cells if _collapse_spaces(c)]
        if len(cells) < 2:
            continue

        for i, label in enumerate(cells[:-1]):
            key = re.sub(r"\s+", "", label)
            key = re.sub(r"^[\-\u2022]+", "", key)
            if key in {"회사명(국적)", "회사명", "발행회사(회사명)", "발행회사"}:
                candidate = _sanitize_company_name(cells[i + 1])
                if _is_valid_candidate_name(candidate):
                    return candidate

    plain = _build_viewer_focus_text(viewer_html, max_lines=220)
    flat = re.sub(r"\s+", " ", plain)
    regexes = [
        r"회사명\(국적\)\s*[:：-]?\s*([\s\S]{2,120}?)\s*(?:\(?대표이사\)?|\(?대표자\)?|국적|자본금|회사와관계|발행주식총수)",
        r"회사명\s*[:：-]?\s*([\s\S]{2,120}?)\s*(?:\(?대표이사\)?|\(?대표자\)?|국적|자본금|회사와관계|발행주식총수)",
    ]
    for pat in regexes:
        m = re.search(pat, flat, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = _sanitize_company_name(m.group(1))
        if _is_valid_candidate_name(candidate):
            return candidate
    return None


def _extract_name_from_cells_by_label(cells: list[str]) -> Optional[str]:
    if len(cells) < 2:
        return None
    for idx, label in enumerate(cells[:-1]):
        key = _normalize_label_key(label)
        if key not in INVESTEE_LABEL_KEYS:
            continue
        for j in range(idx + 1, min(len(cells), idx + 5)):
            next_key = _normalize_label_key(cells[j])
            if j > (idx + 1) and next_key in BOUNDARY_LABEL_KEYS:
                break
            candidate = _sanitize_company_name(cells[j])
            if _is_valid_candidate_name(candidate):
                return candidate
    return None


def _extract_name_from_viewer_html_table(viewer_html: str) -> Optional[str]:
    if not viewer_html:
        return None

    soup = BeautifulSoup(viewer_html, "html.parser")

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [_collapse_spaces(c) for c in cells if _collapse_spaces(c)]
        candidate = _extract_name_from_cells_by_label(cells)
        if _is_valid_candidate_name(candidate):
            return candidate

    flat_cells = [
        _collapse_spaces(c.get_text(" ", strip=True))
        for c in soup.find_all(["th", "td"])
    ]
    flat_cells = [c for c in flat_cells if c]
    candidate = _extract_name_from_cells_by_label(flat_cells)
    if _is_valid_candidate_name(candidate):
        return candidate
    return None


def _extract_name_from_viewer_html_regex(viewer_html: str) -> Optional[str]:
    if not viewer_html:
        return None

    focus = _build_viewer_focus_text(viewer_html, max_lines=260)
    if not focus:
        soup = BeautifulSoup(viewer_html, "html.parser")
        focus = _collapse_spaces(soup.get_text(" ", strip=True))
    flat = re.sub(r"\s+", " ", focus)

    regexes = [
        (
            r"(?:발행회사(?:\([^)]+\))?|발행회사명|대상(?:회사|법인)(?:\([^)]+\))?|"
            r"회사명(?:\(국적\))?|법인명)\s*[:：-]?\s*([\s\S]{2,140}?)\s*"
            r"(?:\(?대표이사\)?|\(?대표자\)?|\(?국적\)?|자본금|회사와관계|발행주식총수|주요사업)"
        ),
        (
            r"1\.\s*발행회사[\s\S]{0,260}?회사명(?:\(국적\))?\s*[:：-]?\s*([\s\S]{2,140}?)\s*"
            r"(?:\(?대표이사\)?|\(?대표자\)?|\(?국적\)?|자본금|회사와관계|발행주식총수|주요사업)"
        ),
    ]
    for pat in regexes:
        m = re.search(pat, flat, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = _sanitize_company_name(m.group(1))
        if _is_valid_candidate_name(candidate):
            return candidate
    return None


def _fetch_suspicious_rows(
    conn: pymysql.connections.Connection,
    table: str,
    *,
    limit: int,
    offset: int,
    corp_name: Optional[str],
    id_min: Optional[int],
    id_max: Optional[int],
    only_transfer_reports: bool,
) -> list[dict[str, Any]]:
    bad_tokens = sorted({_normalize_token(x) for x in BAD_NAME_TOKENS})
    placeholders = ",".join(["%s"] * len(bad_tokens))

    conditions = [
        "viewer_url IS NOT NULL",
        "TRIM(viewer_url) <> ''",
        (
            "iscmp_cmpnm IS NULL "
            "OR TRIM(iscmp_cmpnm) = '' "
            f"OR REPLACE(TRIM(iscmp_cmpnm), ' ', '') IN ({placeholders})"
        ),
    ]
    params: list[Any] = list(bad_tokens)

    if only_transfer_reports:
        conditions.append("report_nm LIKE %s")
        params.append("%타법인%")

    if corp_name:
        conditions.append("corp_name = %s")
        params.append(corp_name.strip())
    if id_min is not None:
        conditions.append("id >= %s")
        params.append(int(id_min))
    if id_max is not None:
        conditions.append("id <= %s")
        params.append(int(id_max))

    where_sql = " AND ".join(conditions)
    sql = f"""
    SELECT
        id, rcept_no, rcept_dt, corp_code, corp_name, report_nm, source,
        viewer_url, iscmp_cmpnm, trfdtl_trfprc, trfdtl_stkcnt, trf_pp
    FROM {table}
    WHERE {where_sql}
    ORDER BY id ASC
    LIMIT %s OFFSET %s
    """
    params.extend([int(limit), int(offset)])

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return rows


def _update_iscmp_cmpnm(
    conn: pymysql.connections.Connection,
    table: str,
    *,
    row_id: int,
    old_name: Any,
    new_name: str,
) -> int:
    sql = f"""
    UPDATE {table}
    SET iscmp_cmpnm = %s, updated_at = CURRENT_TIMESTAMP
    WHERE id = %s
      AND COALESCE(iscmp_cmpnm, '') = COALESCE(%s, '')
    """
    old_value: Optional[str]
    try:
        old_value = None if pd.isna(old_name) else str(old_name)
    except Exception:
        old_value = None if old_name is None else str(old_name)
    with conn.cursor() as cur:
        try:
            cur.execute(sql, (new_name, int(row_id), old_value))
            return int(cur.rowcount)
        except pymysql.err.IntegrityError as exc:
            # Unique key collision (rcept_no, corp_code, iscmp_cmpnm) => skip safely.
            if exc.args and int(exc.args[0]) == 1062:
                return -1
            raise


def _safe_extract_by_viewer(
    dart: OpenDartClient,
    viewer_url: str,
    *,
    use_document_fallback: bool,
    retries: int,
    sleep_sec: float,
) -> dict[str, Any]:
    last_exc: Optional[Exception] = None
    for attempt in range(max(int(retries), 1)):
        try:
            return dart.extract_transfer_decision_from_viewer_url(
                viewer_url=viewer_url,
                use_document_fallback=use_document_fallback,
                verbose=False,
            )
        except Exception as exc:
            last_exc = exc
            if attempt < max(int(retries), 1) - 1 and sleep_sec > 0:
                time.sleep(sleep_sec * (attempt + 1))
    raise RuntimeError(f"viewer parse failed after retries: {last_exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fix suspicious iscmp_cmpnm values in DB. "
            "Pipeline: viewer_url deterministic parse -> optional LLM fallback -> safe update."
        ),
    )
    parser.add_argument("--provider", choices=["gemini", "auto", "openai", "none"], default="gemini")
    parser.add_argument("--llm-threshold", type=float, default=0.70, help="Minimum LLM confidence to apply.")
    parser.add_argument("--llm-rpm", type=int, default=20, help="LLM requests per minute limit.")
    parser.add_argument("--llm-required", action="store_true", help="Fail if LLM provider is unavailable.")
    parser.add_argument("--limit", type=int, default=300, help="Max suspicious rows to scan.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--corp-name", default=None)
    parser.add_argument("--id-min", type=int, default=None)
    parser.add_argument("--id-max", type=int, default=None)
    parser.add_argument(
        "--only-transfer-reports",
        action="store_true",
        help="Deprecated option. Transfer-report-only mode is default.",
    )
    parser.add_argument(
        "--include-non-transfer-reports",
        action="store_true",
        help="Disable transfer-only filter and scan all reports with viewer_url.",
    )
    parser.add_argument("--use-document-fallback", action="store_true", default=True)
    parser.add_argument("--no-document-fallback", action="store_true")
    parser.add_argument("--viewer-retries", type=int, default=2)
    parser.add_argument("--dart-rpm", type=int, default=8, help="DART requests per minute limit.")
    parser.add_argument("--max-dart-calls", type=int, default=180, help="Hard cap of DART calls per run.")
    parser.add_argument("--dart-max-retries", type=int, default=2, help="DART client retry count.")
    parser.add_argument("--sleep-sec", type=float, default=0.20)
    parser.add_argument("--commit-every", type=int, default=20)
    parser.add_argument("--trace-skip", action="store_true", help="Log candidate details for skipped rows.")
    parser.add_argument("--max-updates", type=int, default=None, help="Stop after this many applied DB updates.")
    parser.add_argument(
        "--apply-interval-sec",
        type=float,
        default=0.0,
        help="Sleep seconds between successful DB updates.",
    )
    parser.add_argument(
        "--sample-10-mode",
        action="store_true",
        help="Convenience: enable --apply, set --max-updates=10 and --apply-interval-sec=1.",
    )
    parser.add_argument("--db-table", default=None, help="Override DB table name.")
    parser.add_argument("--allow-original-table", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Actually update DB. Without this, dry-run mode.")
    parser.add_argument("--log-file", default=_default_log_file())
    parser.add_argument("--changes-out", default=_default_changes_out())
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sample_10_mode:
        args.apply = True
        if args.max_updates is None:
            args.max_updates = 10
        if float(args.apply_interval_sec) <= 0:
            args.apply_interval_sec = 1.0

    if args.max_updates is not None and int(args.max_updates) <= 0:
        raise ValueError("--max-updates must be a positive integer when provided.")

    root_env = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=root_env)

    logger = _configure_logger(args.log_file)
    logger.info("[LOGFILE] %s", args.log_file)
    logger.info("[MODE] apply=%s provider=%s", args.apply, args.provider)

    db_cfg = _load_db_config(
        table_override=args.db_table,
        allow_original_table=bool(args.allow_original_table),
    )
    settings = load_settings()
    dart_key = (settings.dart_api_key or "").strip()
    if not dart_key:
        raise RuntimeError("DART_API_KEY/OPENDART_API_KEY is required.")

    resolver = build_name_resolver(provider=args.provider)
    if args.llm_required and resolver is None:
        raise RuntimeError("LLM resolver is required but unavailable. Check OPENAI_API_KEY or provider option.")
    if resolver is None:
        logger.info("[LLM] resolver=disabled (deterministic parse only)")
    else:
        logger.info("[LLM] resolver=enabled rpm_limit=%s/min", max(int(args.llm_rpm), 1))

    llm_limiter = (
        SlidingWindowRateLimiter(
            max_calls=max(int(args.llm_rpm), 1),
            period_sec=60.0,
            logger=logger,
            label="llm",
        )
        if resolver is not None
        else None
    )

    only_transfer_reports = not bool(args.include_non_transfer_reports)
    use_document_fallback = bool(args.use_document_fallback) and not bool(args.no_document_fallback)

    conn = pymysql.connect(
        host=db_cfg.host,
        port=db_cfg.port,
        user=db_cfg.user,
        password=db_cfg.password,
        database=db_cfg.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

    try:
        rows = _fetch_suspicious_rows(
            conn=conn,
            table=db_cfg.table,
            limit=args.limit,
            offset=args.offset,
            corp_name=args.corp_name,
            id_min=args.id_min,
            id_max=args.id_max,
            only_transfer_reports=only_transfer_reports,
        )
        if not rows:
            logger.info("[DONE] no suspicious rows found.")
            conn.rollback()
            return

        logger.info("[TARGET] suspicious_rows=%s table=%s", f"{len(rows):,}", db_cfg.table)

        dart = OpenDartClient(
            api_key=dart_key,
            timeout=30,
            max_retries=max(int(args.dart_max_retries), 1),
            base_sleep=0.8,
        )

        dart_limiter = SlidingWindowRateLimiter(
            max_calls=max(int(args.dart_rpm), 1),
            period_sec=60.0,
            logger=logger,
            label="dart",
        )
        max_dart_calls = max(int(args.max_dart_calls), 1)
        dart_calls_used = 0
        viewer_resolution_cache: dict[str, dict[str, Any]] = {}

        def _consume_dart_budget() -> bool:
            nonlocal dart_calls_used
            if dart_calls_used >= max_dart_calls:
                return False
            dart_limiter.acquire()
            dart_calls_used += 1
            return True

        stats = {
            "processed": 0,
            "updated": 0,
            "skip_same": 0,
            "skip_invalid_candidate": 0,
            "duplicate_conflict": 0,
            "parser_selected": 0,
            "table_selected": 0,
            "regex_selected": 0,
            "legacy_selected": 0,
            "llm_selected": 0,
            "dart_cache_hit": 0,
            "dart_budget_skips": 0,
            "errors": 0,
        }
        pending_writes = 0
        applied_updates = 0
        changes: list[dict[str, Any]] = []

        for idx, row in enumerate(rows, start=1):
            row_id = int(row["id"])
            old_name_raw = row.get("iscmp_cmpnm")
            old_name = _sanitize_company_name(old_name_raw)
            viewer_url = str(row.get("viewer_url") or "").strip()
            corp_name = _collapse_spaces(row.get("corp_name"))
            report_nm = _collapse_spaces(row.get("report_nm"))

            logger.info("[%s/%s] id=%s rcp=%s old='%s'", idx, len(rows), row_id, row.get("rcept_no"), old_name_raw)

            stats["processed"] += 1
            selected_name: Optional[str] = None
            selected_method = ""
            selected_conf = 1.0
            llm_note = ""
            parser_candidate = None
            viewer_html_cache: Optional[str] = None
            table_candidate: Optional[str] = None
            regex_candidate: Optional[str] = None
            legacy_candidate: Optional[str] = None
            llm_candidate: Optional[str] = None
            llm_confidence: Optional[float] = None
            cache_hit = False

            cached = viewer_resolution_cache.get(viewer_url)
            if cached is not None:
                stats["dart_cache_hit"] += 1
                cache_hit = True
                parser_candidate = _sanitize_company_name(cached.get("parser_candidate"))
                viewer_html_cache = cached.get("viewer_html")
                table_candidate = _sanitize_company_name(cached.get("table_candidate"))
                regex_candidate = _sanitize_company_name(cached.get("regex_candidate"))
                legacy_candidate = _sanitize_company_name(cached.get("legacy_candidate"))
            else:
                if _consume_dart_budget():
                    try:
                        parsed = _safe_extract_by_viewer(
                            dart=dart,
                            viewer_url=viewer_url,
                            use_document_fallback=use_document_fallback,
                            retries=args.viewer_retries,
                            sleep_sec=args.sleep_sec,
                        )
                        parser_candidate = _sanitize_company_name(parsed.get("iscmp_cmpnm"))
                    except Exception as exc:
                        logger.warning("  -> parser_error: %s", exc)
                else:
                    stats["dart_budget_skips"] += 1
                    logger.warning("  -> skip parser (dart budget exhausted)")

                need_viewer_html = not _is_valid_candidate_name(parser_candidate)

                if need_viewer_html:
                    if _consume_dart_budget():
                        try:
                            viewer_html_cache, html_source = _fetch_viewer_html_for_cleanup(
                                dart=dart,
                                viewer_url=viewer_url,
                                retries=max(args.viewer_retries, 1),
                                sleep_sec=args.sleep_sec,
                            )
                            legacy_candidate = _sanitize_company_name(
                                _extract_name_from_viewer_html_legacy(viewer_html_cache)
                            )
                            table_candidate = _sanitize_company_name(
                                _extract_name_from_viewer_html_table(viewer_html_cache)
                            )
                            regex_candidate = _sanitize_company_name(
                                _extract_name_from_viewer_html_regex(viewer_html_cache)
                            )
                            if html_source != "VIEWER_HTML_GENERIC":
                                logger.info("  -> legacy_html_source=%s", html_source)
                        except Exception as exc:
                            logger.warning("  -> legacy_html_error: %s", exc)
                    else:
                        stats["dart_budget_skips"] += 1
                        logger.warning("  -> skip legacy html (dart budget exhausted)")

                viewer_resolution_cache[viewer_url] = {
                    "parser_candidate": parser_candidate,
                    "viewer_html": viewer_html_cache,
                    "table_candidate": table_candidate,
                    "regex_candidate": regex_candidate,
                    "legacy_candidate": legacy_candidate,
                }

            logger.info(
                "  -> reparse parser='%s' table='%s' regex='%s' legacy='%s' cache=%s",
                parser_candidate,
                table_candidate,
                regex_candidate,
                legacy_candidate,
                "hit" if cache_hit else "miss",
            )

            if _is_valid_candidate_name(parser_candidate):
                selected_name = parser_candidate
                selected_method = "viewer_parser"
                selected_conf = 1.0
                stats["parser_selected"] += 1
            elif _is_valid_candidate_name(table_candidate):
                selected_name = table_candidate
                selected_method = "viewer_table"
                selected_conf = 0.95
                stats["table_selected"] += 1
            elif _is_valid_candidate_name(regex_candidate):
                selected_name = regex_candidate
                selected_method = "viewer_regex"
                selected_conf = 0.92
                stats["regex_selected"] += 1
            elif _is_valid_candidate_name(legacy_candidate):
                selected_name = legacy_candidate
                selected_method = "legacy_html"
                selected_conf = 0.90
                stats["legacy_selected"] += 1

            if selected_name is None and resolver is not None:
                try:
                    if llm_limiter is not None:
                        llm_limiter.acquire()
                    if viewer_html_cache is None:
                        if _consume_dart_budget():
                            viewer_html_cache, _ = _fetch_viewer_html_for_cleanup(
                                dart=dart,
                                viewer_url=viewer_url,
                                retries=max(args.viewer_retries, 1),
                                sleep_sec=args.sleep_sec,
                            )
                            viewer_resolution_cache[viewer_url] = {
                                "parser_candidate": parser_candidate,
                                "viewer_html": viewer_html_cache,
                                "table_candidate": table_candidate,
                                "regex_candidate": regex_candidate,
                                "legacy_candidate": legacy_candidate,
                            }
                        else:
                            stats["dart_budget_skips"] += 1
                            logger.warning("  -> skip llm (dart budget exhausted, no viewer html)")
                            viewer_html_cache = None
                    if not viewer_html_cache:
                        raise RuntimeError("viewer_html unavailable for llm stage")
                    focus_text = _build_viewer_focus_text(viewer_html_cache)
                    llm_result: LlmInvesteeNameResult = resolver.suggest_investee_name(
                        corp_name=corp_name,
                        report_nm=report_nm,
                        raw_iscmp_cmpnm=_collapse_spaces(old_name_raw),
                        parser_candidate=_collapse_spaces(parser_candidate),
                        viewer_focus_text=focus_text[:12000],
                    )
                    llm_name = _sanitize_company_name(llm_result.iscmp_cmpnm)
                    llm_candidate = llm_name
                    llm_confidence = float(llm_result.confidence)
                    llm_note = llm_result.reason
                    if (
                        _is_valid_candidate_name(llm_name)
                        and float(llm_result.confidence) >= float(args.llm_threshold)
                    ):
                        selected_name = llm_name
                        selected_method = "llm"
                        selected_conf = float(llm_result.confidence)
                        stats["llm_selected"] += 1
                except Exception as exc:
                    logger.warning("  -> llm_error: %s", exc)
                else:
                    logger.info(
                        "  -> llm candidate='%s' conf=%s note='%s'",
                        llm_candidate,
                        llm_confidence,
                        llm_note,
                    )

            if not _is_valid_candidate_name(selected_name):
                stats["skip_invalid_candidate"] += 1
                logger.info("  -> skip (no valid candidate)")
                if args.trace_skip:
                    logger.info(
                        "  -> candidates parser='%s' table='%s' regex='%s' legacy='%s' llm='%s' llm_conf=%s llm_note='%s'",
                        parser_candidate,
                        table_candidate,
                        regex_candidate,
                        legacy_candidate,
                        llm_candidate,
                        llm_confidence,
                        llm_note,
                    )
                if args.sleep_sec > 0:
                    time.sleep(args.sleep_sec)
                continue

            if old_name and _is_same_name(old_name, selected_name):
                stats["skip_same"] += 1
                logger.info("  -> skip (same name): %s", selected_name)
                if args.sleep_sec > 0:
                    time.sleep(args.sleep_sec)
                continue

            change = {
                "id": row_id,
                "rcept_no": row.get("rcept_no"),
                "corp_name": corp_name,
                "report_nm": report_nm,
                "source": row.get("source"),
                "old_iscmp_cmpnm": old_name_raw,
                "new_iscmp_cmpnm": selected_name,
                "method": selected_method,
                "confidence": selected_conf,
                "llm_note": llm_note,
                "viewer_url": viewer_url,
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
            changes.append(change)

            if args.apply:
                affected = _update_iscmp_cmpnm(
                    conn=conn,
                    table=db_cfg.table,
                    row_id=row_id,
                    old_name=old_name_raw,
                    new_name=selected_name,
                )
                if affected > 0:
                    stats["updated"] += 1
                    applied_updates += 1
                    pending_writes += 1
                    logger.info(
                        "  -> update applied: '%s' -> '%s' (%s, conf=%.2f)",
                        old_name_raw,
                        selected_name,
                        selected_method,
                        selected_conf,
                    )
                    if float(args.apply_interval_sec) > 0:
                        logger.info("  -> apply interval sleep %.2fs", float(args.apply_interval_sec))
                        time.sleep(float(args.apply_interval_sec))

                    if args.max_updates is not None and applied_updates >= int(args.max_updates):
                        logger.info(
                            "  -> reached max-updates limit (%s). stopping further updates.",
                            int(args.max_updates),
                        )
                        break
                elif affected == -1:
                    stats["duplicate_conflict"] += 1
                    logger.info("  -> skipped (duplicate unique key)")
                else:
                    logger.info("  -> skipped by optimistic lock (row changed externally)")
            else:
                stats["updated"] += 1
                logger.info(
                    "  -> dry-run change: '%s' -> '%s' (%s, conf=%.2f)",
                    old_name_raw,
                    selected_name,
                    selected_method,
                    selected_conf,
                )

            if args.apply and pending_writes >= max(int(args.commit_every), 1):
                conn.commit()
                pending_writes = 0
                logger.info("  -> committed batch")

            if args.sleep_sec > 0:
                time.sleep(args.sleep_sec)

        if args.apply:
            conn.commit()
        else:
            conn.rollback()

        if changes:
            out_path = Path(args.changes_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(changes).to_csv(out_path, index=False, encoding="utf-8-sig")
            logger.info("[OUTPUT] changes saved: %s", out_path)

        logger.info(
            "[DONE] processed=%s updated=%s parser_selected=%s table_selected=%s regex_selected=%s "
            "legacy_selected=%s llm_selected=%s "
            "skip_same=%s skip_invalid_candidate=%s duplicate_conflict=%s "
            "dart_calls_used=%s dart_cache_hit=%s dart_budget_skips=%s errors=%s apply=%s",
            f"{stats['processed']:,}",
            f"{stats['updated']:,}",
            f"{stats['parser_selected']:,}",
            f"{stats['table_selected']:,}",
            f"{stats['regex_selected']:,}",
            f"{stats['legacy_selected']:,}",
            f"{stats['llm_selected']:,}",
            f"{stats['skip_same']:,}",
            f"{stats['skip_invalid_candidate']:,}",
            f"{stats['duplicate_conflict']:,}",
            f"{dart_calls_used:,}",
            f"{stats['dart_cache_hit']:,}",
            f"{stats['dart_budget_skips']:,}",
            f"{stats['errors']:,}",
            args.apply,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
