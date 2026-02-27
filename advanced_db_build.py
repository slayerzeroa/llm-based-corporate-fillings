from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd
import pymysql
from dotenv import load_dotenv

DEFAULT_SOURCE_TABLE = "dart_investment_events"
DEFAULT_RAW_DIR = "logs/raw_data_show"
DEFAULT_SUFFIX = ""

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

DATE_ANY_RE = re.compile(r"(20\d{2})[^\d]([01]?\d)[^\d]([0-3]?\d)")
TARGET_PLACEHOLDER_RE = re.compile(
    r"^(합계|총계|소계|회사명(?:\(국적\))?|기업명|법인명|발행회사|대표자|대표이사|국적|성명|회사와관계|\(회사명\)|\(기업명\)|\(대표자\)|\(국적\))$"
)
CANCEL_KEYWORDS = [
    "철회",
    "취소",
    "계약 해제",
    "양수결정 철회",
    "양도결정 철회",
    "취득결정 철회",
    "처분결정 철회",
]
CORRECTION_KEYWORDS = [
    "정정신고",
    "정정대상 공시서류",
    "정정관련 공시서류",
    "정정사항",
    "정정사유",
]


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass(frozen=True)
class TableNames:
    disclosures: str
    lines: str
    families: str
    family_members: str
    investee_dim: str
    edge_events: str
    edge_state_current: str
    edge_state_daily: str


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
    return DbConfig(host=host, port=port, user=user, password=password, database=database)


def _safe_table_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _build_table_names(suffix: str) -> TableNames:
    sfx = suffix.strip() or ""
    if sfx and not sfx.startswith("_"):
        sfx = f"_{sfx}"
    names = TableNames(
        disclosures=f"dart_disclosures_raw{sfx}",
        lines=f"dart_investment_lines_raw{sfx}",
        families=f"dart_disclosure_families{sfx}",
        family_members=f"dart_disclosure_family_members{sfx}",
        investee_dim=f"dart_investee_dim{sfx}",
        edge_events=f"dart_edge_events{sfx}",
        edge_state_current=f"dart_edge_state_current{sfx}",
        edge_state_daily=f"dart_edge_state_daily{sfx}",
    )
    return TableNames(**{k: _safe_table_name(v) for k, v in names.__dict__.items()})


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _norm_rcept_no(value: Any) -> str:
    s = _to_str(value)
    return s if re.fullmatch(r"\d{14}", s) else ""


def _norm_date(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return None
    return str(dt.date())


def _norm_corp_code(value: Any) -> str:
    raw = _to_str(value)
    if not raw:
        raise ValueError("corp_code is empty")
    digits = re.sub(r"[^\d]", "", raw)
    if not re.fullmatch(r"\d{1,8}", digits):
        raise ValueError(f"Invalid corp_code: {value}")
    return digits.zfill(8)


def _to_float_or_none(value: Any) -> Optional[float]:
    s = _to_str(value)
    if not s:
        return None
    s = s.replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _clean_text(value: Any) -> str:
    s = html.unescape(_to_str(value))
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _clean_target_name(value: Any) -> str:
    s = _clean_text(value)
    s = re.sub(r"\(\s*비상장\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -\t")
    return s


def _is_target_placeholder(value: Any) -> bool:
    s = re.sub(r"\s+", "", _clean_text(value))
    if not s:
        return True
    return bool(TARGET_PLACEHOLDER_RE.fullmatch(s))


def _target_key(name: str) -> str:
    s = _clean_target_name(name).lower()
    for token in [
        "주식회사",
        "㈜",
        "(주)",
        "co., ltd.",
        "co.,ltd.",
        "ltd.",
        "inc.",
        "corp.",
        "corporation",
    ]:
        s = s.replace(token, "")
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^0-9a-z가-힣]", "", s)
    return s


def _report_family_key(report_nm: str) -> str:
    s = _clean_text(report_nm)
    s = re.sub(r"^\[[^\]]+\]\s*", "", s)
    compact = re.sub(r"\s+", "", s)
    m = re.search(r"타법인주식및출자증권(취득결정|양수결정|처분결정|양도결정)", compact)
    if m:
        return f"타법인주식및출자증권{m.group(1)}"
    return compact[:90] if compact else "UNKNOWN"


def _pick_longest_name(values: pd.Series) -> Any:
    names = [_clean_text(x) for x in values if _clean_text(x)]
    return max(names, key=len) if names else pd.NA


def _read_text_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _resolve_path(path_text: str, manifest_path: Path) -> Optional[Path]:
    raw = _to_str(path_text)
    if not raw:
        return None
    p = Path(raw)
    cands = [p, manifest_path.parent / p, Path.cwd() / p]
    for c in cands:
        if c.exists():
            return c
    return None


def _extract_family_rcept_nos(main_html: str) -> list[str]:
    out: set[str] = set()
    blocks = FAMILY_SELECT_RE.findall(main_html)
    search_blocks = blocks if blocks else [main_html]
    for block in search_blocks:
        for m in OPTION_VALUE_RE.finditer(block):
            raw_val = html.unescape(_clean_text(m.group("value")))
            mm = RCP_VALUE_RE.search(raw_val)
            if mm:
                out.add(mm.group(1))
    return sorted(out)


def _extract_origin_date(text: str) -> Optional[str]:
    m = DATE_ANY_RE.search(text)
    if not m:
        return None
    try:
        dt = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return dt.isoformat()


def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    hay = _clean_text(text)
    hits = [kw for kw in keywords if kw in hay]
    return sorted(set(hits))


def _load_raw_evidence(raw_dir: Path) -> pd.DataFrame:
    manifest_paths = sorted(raw_dir.rglob("manifest.json"))
    merged: dict[str, dict[str, Any]] = {}

    for manifest_path in manifest_paths:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        crawl_rows = payload.get("crawl", [])
        if not isinstance(crawl_rows, list):
            continue

        for row in crawl_rows:
            rcp = _norm_rcept_no(row.get("rcept_no"))
            if not rcp:
                continue

            ent = merged.setdefault(
                rcp,
                {
                    "rcept_no": rcp,
                    "alert_base": set(),
                    "alert_is_fix": 0,
                    "family_rcepts": set(),
                    "correction_hits": set(),
                    "cancel_hits": set(),
                    "origin_dates": set(),
                    "evidence_files": set(),
                },
            )

            main_path = _resolve_path(_to_str(row.get("main_html_path")), manifest_path)
            viewer_path = _resolve_path(_to_str(row.get("viewer_html_path")), manifest_path)
            doc_preview_path = _resolve_path(_to_str(row.get("document_preview_path")), manifest_path)

            main_html = _read_text_file(main_path) if main_path else ""
            viewer_html = _read_text_file(viewer_path) if viewer_path else ""
            doc_preview = _read_text_file(doc_preview_path) if doc_preview_path else ""

            if main_path:
                ent["evidence_files"].add(str(main_path))
            if viewer_path:
                ent["evidence_files"].add(str(viewer_path))
            if doc_preview_path:
                ent["evidence_files"].add(str(doc_preview_path))

            m = ALERT_INVEST_NOTICE_RE.search(main_html)
            if m:
                base_rcp = _norm_rcept_no(m.group("base"))
                if base_rcp:
                    ent["alert_base"].add(base_rcp)
                try:
                    ent["alert_is_fix"] = max(ent["alert_is_fix"], int(m.group("is_fix")))
                except ValueError:
                    pass

            for fm in _extract_family_rcept_nos(main_html):
                ent["family_rcepts"].add(fm)

            combined = "\n".join([main_html, viewer_html, doc_preview])
            for kw in _keyword_hits(combined, CORRECTION_KEYWORDS):
                ent["correction_hits"].add(kw)
            for kw in _keyword_hits(combined, CANCEL_KEYWORDS):
                ent["cancel_hits"].add(kw)

            if "최초제출일" in combined:
                od = _extract_origin_date(combined)
                if od:
                    ent["origin_dates"].add(od)

    rows = []
    for rcp, ent in merged.items():
        alert_base = sorted(ent["alert_base"])[0] if ent["alert_base"] else None
        family_rcepts = sorted(ent["family_rcepts"])
        correction_hits = sorted(ent["correction_hits"])
        cancel_hits = sorted(ent["cancel_hits"])
        origin_dates = sorted(ent["origin_dates"])
        rows.append(
            {
                "rcept_no": rcp,
                "alert_base_rcept_no": alert_base,
                "alert_is_fix": int(ent["alert_is_fix"]),
                "family_rcepts_json": json.dumps(family_rcepts, ensure_ascii=False),
                "family_member_count": len(family_rcepts),
                "has_correction_text": 1 if correction_hits else 0,
                "has_cancel_text": 1 if cancel_hits else 0,
                "cancel_reason_hint": " | ".join(cancel_hits),
                "correction_origin_date": origin_dates[0] if origin_dates else None,
                "raw_evidence_json": json.dumps(
                    {
                        "alert_base_rcept_candidates": sorted(ent["alert_base"]),
                        "family_rcepts": family_rcepts,
                        "correction_hits": correction_hits,
                        "cancel_hits": cancel_hits,
                        "origin_dates": origin_dates,
                        "evidence_files": sorted(ent["evidence_files"]),
                    },
                    ensure_ascii=False,
                ),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "rcept_no",
                "alert_base_rcept_no",
                "alert_is_fix",
                "family_rcepts_json",
                "family_member_count",
                "has_correction_text",
                "has_cancel_text",
                "cancel_reason_hint",
                "correction_origin_date",
                "raw_evidence_json",
            ]
        )
    return pd.DataFrame(rows)

def _load_source_rows(
    conn: pymysql.connections.Connection,
    source_table: str,
    corp_code_filter: Optional[str] = None,
    sample_limit: Optional[int] = None,
) -> pd.DataFrame:
    table = _safe_table_name(source_table)
    base_cols = [
        "id",
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
    optional_cols = ["report_type", "is_correction"]

    where_parts = [
        "rcept_no REGEXP '^[0-9]{14}'",
        "report_nm LIKE %s",
    ]
    params: list[Any] = ["%타법인%"]
    if corp_code_filter:
        where_parts.append("corp_code = %s")
        params.append(_norm_corp_code(corp_code_filter))

    limit_sql = ""
    if sample_limit is not None and int(sample_limit) > 0:
        limit_sql = "LIMIT %s"
        params.append(int(sample_limit))

    sql = f"""
    SELECT {", ".join(base_cols + optional_cols)}
    FROM {table}
    WHERE {" AND ".join(where_parts)}
    ORDER BY rcept_dt ASC, rcept_no ASC, id ASC
    {limit_sql}
    """

    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        df = pd.DataFrame(rows)
    except Exception:
        fallback_sql = f"""
        SELECT {", ".join(base_cols)}
        FROM {table}
        WHERE {" AND ".join(where_parts)}
        ORDER BY rcept_dt ASC, rcept_no ASC, id ASC
        {limit_sql}
        """
        with conn.cursor() as cur:
            cur.execute(fallback_sql, tuple(params))
            rows = cur.fetchall()
        df = pd.DataFrame(rows)
        df["report_type"] = pd.NA
        df["is_correction"] = 0

    if df.empty:
        return df

    for c in base_cols + optional_cols:
        if c not in df.columns:
            df[c] = pd.NA

    df["rcept_no"] = df["rcept_no"].map(_norm_rcept_no)
    df = df[df["rcept_no"] != ""].copy()
    df["rcept_dt"] = df["rcept_dt"].map(_norm_date)
    df["corp_code"] = df["corp_code"].astype(str).str.strip().str.zfill(8)
    return df.reset_index(drop=True)


def _prepare_disclosures(source_df: pd.DataFrame, evidence_df: pd.DataFrame) -> pd.DataFrame:
    def _first_non_empty(sr: pd.Series) -> Any:
        for v in sr:
            if _to_str(v):
                return v
        return pd.NA

    df = source_df.copy()
    df["report_nm"] = df["report_nm"].map(_clean_text)
    df["trf_pp"] = df["trf_pp"].map(_clean_text)
    df["source"] = df["source"].map(_clean_text)
    df["report_family_key"] = df["report_nm"].map(_report_family_key)
    df["db_is_correction"] = df["is_correction"].fillna(0).astype(int)
    df["text_blob"] = (
        df["report_nm"].fillna("").astype(str)
        + " "
        + df["trf_pp"].fillna("").astype(str)
        + " "
        + df["source"].fillna("").astype(str)
    ).str.strip()

    agg = (
        df.groupby("rcept_no", as_index=False)
        .agg(
            rcept_dt=("rcept_dt", "min"),
            corp_cls=("corp_cls", _first_non_empty),
            corp_code=("corp_code", _first_non_empty),
            corp_name=("corp_name", _first_non_empty),
            report_nm=("report_nm", _first_non_empty),
            flr_nm=("flr_nm", _first_non_empty),
            pblntf_ty=("pblntf_ty", _first_non_empty),
            source=("source", _first_non_empty),
            viewer_url=("viewer_url", _first_non_empty),
            report_type=("report_type", _first_non_empty),
            db_is_correction=("db_is_correction", "max"),
            report_family_key=("report_family_key", _first_non_empty),
            text_blob=("text_blob", lambda s: " | ".join(sorted(set([_clean_text(x) for x in s if _clean_text(x)])))),
            line_count=("id", "count"),
        )
    )

    out = agg.merge(evidence_df, on="rcept_no", how="left")
    for c, default in [
        ("alert_is_fix", 0),
        ("family_member_count", 0),
        ("has_correction_text", 0),
        ("has_cancel_text", 0),
    ]:
        col = out[c]
        col = col.where(~col.isna(), default)
        out[c] = pd.to_numeric(col, errors="coerce").fillna(default).astype(int)

    out["cancel_reason_hint"] = out["cancel_reason_hint"].fillna("")
    out["raw_evidence_json"] = out["raw_evidence_json"].fillna("{}")

    is_corr_by_name = out["report_nm"].astype(str).str.contains(r"^\[[^\]]*정정[^\]]*\]", regex=True, na=False)
    out["is_correction"] = (
        (out["db_is_correction"] > 0)
        | is_corr_by_name
        | (out["alert_is_fix"] > 0)
        | (out["has_correction_text"] > 0)
    ).astype(int)

    out["is_cancel"] = (
        out["report_nm"].astype(str).str.contains("철회|취소|해제", regex=True, na=False)
        | out["text_blob"].astype(str).str.contains("철회|취소|계약 해제|해제 통지", regex=True, na=False)
        | (out["has_cancel_text"] > 0)
    ).astype(int)

    out["family_anchor_rcept_no"] = out["alert_base_rcept_no"].map(_norm_rcept_no)
    out["family_anchor_rcept_no"] = out["family_anchor_rcept_no"].replace("", pd.NA)

    for idx, row in out[out["family_anchor_rcept_no"].isna()].iterrows():
        rcp = row["rcept_no"]
        try:
            members = json.loads(row.get("family_rcepts_json") or "[]")
        except Exception:
            members = []
        members = [m for m in members if re.fullmatch(r"\d{14}", str(m))]
        if rcp not in members:
            members.append(rcp)
        if members:
            out.at[idx, "family_anchor_rcept_no"] = sorted(members)[0]

    out = out.sort_values(["corp_code", "report_family_key", "rcept_dt", "rcept_no"]).reset_index(drop=True)

    for (corp_code, family_key), g in out.groupby(["corp_code", "report_family_key"], sort=False):
        last_anchor = None
        last_date = None
        for idx in g.index.tolist():
            anchor = _norm_rcept_no(out.at[idx, "family_anchor_rcept_no"])
            if not anchor:
                is_corr = int(out.at[idx, "is_correction"]) == 1
                is_cancel = int(out.at[idx, "is_cancel"]) == 1
                if (is_corr or is_cancel) and last_anchor:
                    if last_date and out.at[idx, "rcept_dt"]:
                        d_now = pd.to_datetime(out.at[idx, "rcept_dt"], errors="coerce")
                        d_prev = pd.to_datetime(last_date, errors="coerce")
                        if (not pd.isna(d_now)) and (not pd.isna(d_prev)):
                            day_gap = (d_now - d_prev).days
                            # 정정은 좁게(1년), 취소/철회는 넓게(10년) 허용
                            # 취소 공시는 대상 라인이 비어있는 경우가 잦아 이전 anchor 연결이 중요.
                            max_gap = 365 if is_corr else 3650
                            if day_gap <= max_gap:
                                anchor = last_anchor
                    else:
                        anchor = last_anchor
                if not anchor:
                    anchor = out.at[idx, "rcept_no"]
            out.at[idx, "family_anchor_rcept_no"] = anchor
            last_anchor = anchor
            last_date = out.at[idx, "rcept_dt"]

    # 중요: sample_limit/corp_code 샘플 실행 시 family 옵션에 있는 root가 로드 범위 밖일 수 있음.
    # FK 일관성을 위해 anchor는 "이번 실행에서 로드된 rcept_no 집합" 안으로 강제 정규화한다.
    loaded_rcps = set([_norm_rcept_no(x) for x in out["rcept_no"].tolist() if _norm_rcept_no(x)])
    for idx, row in out.iterrows():
        anchor = _norm_rcept_no(row.get("family_anchor_rcept_no"))
        if anchor and anchor in loaded_rcps:
            continue

        cands: list[str] = []
        try:
            family_rows = json.loads(row.get("family_rcepts_json") or "[]")
            cands = [m for m in family_rows if _norm_rcept_no(m) in loaded_rcps]
        except Exception:
            cands = []

        if cands:
            out.at[idx, "family_anchor_rcept_no"] = sorted(set(cands))[0]
        else:
            # 최후 fallback은 항상 자기 자신(반드시 loaded)
            out.at[idx, "family_anchor_rcept_no"] = row["rcept_no"]

    out["family_group_key"] = out["corp_code"].astype(str) + ":" + out["family_anchor_rcept_no"].astype(str)
    return out

def _prepare_lines(source_df: pd.DataFrame, disclosures_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    lines = source_df.copy()
    lines["target_raw"] = lines["iscmp_cmpnm"].map(_clean_text)
    lines["target_clean"] = lines["target_raw"].map(_clean_target_name)
    lines["target_key"] = lines["target_clean"].map(_target_key)
    lines["is_target_valid"] = (~lines["target_clean"].map(_is_target_placeholder)) & (lines["target_key"] != "")
    lines["amount_value"] = lines["trfdtl_trfprc"].map(_to_float_or_none)
    lines["qty_value"] = lines["trfdtl_stkcnt"].map(_to_float_or_none)
    lines["trf_pp"] = lines["trf_pp"].map(_clean_text)
    lines["source"] = lines["source"].map(_clean_text)
    lines["src_row_id"] = lines["id"]

    keep_cols = [
        "rcept_no",
        "rcept_dt",
        "corp_code",
        "corp_name",
        "report_nm",
        "source",
        "viewer_url",
        "trf_pp",
        "src_row_id",
        "id",
        "target_raw",
        "target_clean",
        "target_key",
        "is_target_valid",
        "amount_value",
        "qty_value",
    ]
    lines = lines[keep_cols].copy()
    lines = lines.merge(
        disclosures_df[
            [
                "rcept_no",
                "family_group_key",
                "family_anchor_rcept_no",
                "is_correction",
                "is_cancel",
                "report_family_key",
            ]
        ],
        on="rcept_no",
        how="left",
    )

    def _sum_nullable(sr: pd.Series) -> Optional[float]:
        vals = [float(v) for v in sr if v is not None and not (isinstance(v, float) and pd.isna(v))]
        if not vals:
            return None
        return float(sum(vals))

    snapshot = (
        lines[lines["is_target_valid"] == True]
        .groupby(["rcept_no", "target_key"], as_index=False)
        .agg(
            target_name=("target_clean", _pick_longest_name),
            snapshot_amount=("amount_value", _sum_nullable),
            snapshot_qty=("qty_value", _sum_nullable),
            trf_pp=("trf_pp", lambda s: " | ".join(sorted(set([_clean_text(x) for x in s if _clean_text(x)])))),
            src_row_ids=("id", lambda s: ",".join([str(int(x)) for x in s if pd.notna(x)])),
        )
    )

    return lines, snapshot


def _build_edge_events(disclosures_df: pd.DataFrame, snapshot_df: pd.DataFrame) -> pd.DataFrame:
    snap_map: dict[str, list[dict[str, Any]]] = {}
    for rcp, g in snapshot_df.groupby("rcept_no", sort=False):
        snap_map[rcp] = g.to_dict(orient="records")

    events: list[dict[str, Any]] = []
    dis = disclosures_df.sort_values(["corp_code", "family_group_key", "rcept_dt", "rcept_no"]).reset_index(drop=True)

    for (corp_code, family_key), g in dis.groupby(["corp_code", "family_group_key"], sort=False):
        prev_snapshot: dict[str, dict[str, Any]] = {}

        for _, row in g.iterrows():
            rcp = row["rcept_no"]
            rows = snap_map.get(rcp, [])
            is_cancel = int(row.get("is_cancel", 0)) == 1
            is_corr = int(row.get("is_correction", 0)) == 1

            if (not rows) and (not is_cancel):
                continue

            current_snapshot: dict[str, dict[str, Any]] = {}
            if not is_cancel:
                for it in rows:
                    key = _to_str(it.get("target_key"))
                    if not key:
                        continue
                    current_snapshot[key] = {
                        "target_name": _clean_text(it.get("target_name")),
                        "amount": float(it.get("snapshot_amount") or 0.0),
                        "qty": float(it.get("snapshot_qty") or 0.0),
                        "trf_pp": _clean_text(it.get("trf_pp")),
                        "src_row_ids": _to_str(it.get("src_row_ids")),
                    }

            all_keys = sorted(set(prev_snapshot.keys()) | set(current_snapshot.keys()))
            confidence = 3 if _norm_rcept_no(row.get("alert_base_rcept_no")) else (2 if int(row.get("family_member_count", 0)) > 0 else 1)
            reason_text = _clean_text(" | ".join([_to_str(row.get("text_blob")), _to_str(row.get("cancel_reason_hint"))]))

            for key in all_keys:
                before = prev_snapshot.get(key, {})
                after = current_snapshot.get(key, {})
                b_amt = float(before.get("amount", 0.0))
                a_amt = float(after.get("amount", 0.0))
                b_qty = float(before.get("qty", 0.0))
                a_qty = float(after.get("qty", 0.0))
                d_amt = a_amt - b_amt
                d_qty = a_qty - b_qty

                if abs(d_amt) < 1e-12 and abs(d_qty) < 1e-12:
                    continue

                if is_cancel:
                    action_type = "CANCEL"
                elif is_corr:
                    action_type = "CORRECTION"
                elif key not in prev_snapshot:
                    action_type = "OPEN"
                else:
                    action_type = "UPDATE"

                target_name = _clean_text(after.get("target_name") or before.get("target_name"))
                src_row_ids = _clean_text(after.get("src_row_ids") or before.get("src_row_ids"))
                event_hash_material = "|".join(
                    [
                        _to_str(corp_code),
                        _to_str(key),
                        _to_str(rcp),
                        _to_str(action_type),
                        f"{d_amt:.6f}",
                        f"{d_qty:.6f}",
                        _to_str(src_row_ids),
                    ]
                )
                event_id = hashlib.sha256(event_hash_material.encode("utf-8")).hexdigest()

                events.append(
                    {
                        "event_id": event_id,
                        "family_group_key": family_key,
                        "family_anchor_rcept_no": row.get("family_anchor_rcept_no"),
                        "rcept_no": rcp,
                        "rcept_dt": row.get("rcept_dt"),
                        "corp_code": corp_code,
                        "corp_name": row.get("corp_name"),
                        "target_key": key,
                        "target_name": target_name,
                        "action_type": action_type,
                        "amount_before": b_amt,
                        "amount_after": a_amt,
                        "amount_delta": d_amt,
                        "qty_before": b_qty,
                        "qty_after": a_qty,
                        "qty_delta": d_qty,
                        "is_correction": 1 if is_corr else 0,
                        "is_cancel": 1 if is_cancel else 0,
                        "confidence": confidence,
                        "reason_text": reason_text or None,
                        "source_row_ids": src_row_ids or None,
                    }
                )

            prev_snapshot = current_snapshot

    if not events:
        return pd.DataFrame(
            columns=[
                "event_id",
                "family_group_key",
                "family_anchor_rcept_no",
                "rcept_no",
                "rcept_dt",
                "corp_code",
                "corp_name",
                "target_key",
                "target_name",
                "action_type",
                "amount_before",
                "amount_after",
                "amount_delta",
                "qty_before",
                "qty_after",
                "qty_delta",
                "is_correction",
                "is_cancel",
                "confidence",
                "reason_text",
                "source_row_ids",
            ]
        )
    return pd.DataFrame(events)


def _build_states(edge_events_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if edge_events_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    ev = edge_events_df.sort_values(["rcept_dt", "rcept_no", "event_id"]).reset_index(drop=True)
    ev["cum_amount"] = ev.groupby(["corp_code", "target_key"], sort=False)["amount_delta"].cumsum()
    ev["cum_qty"] = ev.groupby(["corp_code", "target_key"], sort=False)["qty_delta"].cumsum()

    last = ev.groupby(["corp_code", "target_key"], as_index=False).last()
    sums = ev.groupby(["corp_code", "target_key"], as_index=False).agg(net_amount=("amount_delta", "sum"), net_qty=("qty_delta", "sum"))
    current = sums.merge(
        last[["corp_code", "target_key", "corp_name", "target_name", "event_id", "rcept_no", "rcept_dt", "action_type"]],
        on=["corp_code", "target_key"],
        how="left",
    )
    current["is_active"] = ((current["net_amount"].abs() > 1e-12) | (current["net_qty"].abs() > 1e-12)).astype(int)
    current = current.rename(
        columns={
            "event_id": "last_event_id",
            "rcept_no": "last_rcept_no",
            "rcept_dt": "last_rcept_dt",
            "action_type": "last_action_type",
        }
    )

    daily = (
        ev.groupby(["rcept_dt", "corp_code", "target_key"], as_index=False)
        .agg(
            corp_name=("corp_name", "last"),
            target_name=("target_name", "last"),
            snapshot_amount=("cum_amount", "last"),
            snapshot_qty=("cum_qty", "last"),
            last_rcept_no=("rcept_no", "last"),
            last_action_type=("action_type", "last"),
        )
        .rename(columns={"rcept_dt": "snapshot_date"})
    )
    return current, daily

def _create_tables(conn: pymysql.connections.Connection, t: TableNames) -> None:
    ddls = [
        f"""
        CREATE TABLE IF NOT EXISTS {t.disclosures} (
            rcept_no CHAR(14) PRIMARY KEY,
            rcept_dt DATE NULL,
            corp_cls CHAR(1) NULL,
            corp_code VARCHAR(8) NOT NULL,
            corp_name VARCHAR(200) NULL,
            report_nm VARCHAR(300) NULL,
            flr_nm VARCHAR(200) NULL,
            pblntf_ty VARCHAR(20) NULL,
            source VARCHAR(100) NULL,
            viewer_url VARCHAR(500) NULL,
            report_type VARCHAR(10) NULL,
            report_family_key VARCHAR(120) NULL,
            family_anchor_rcept_no CHAR(14) NULL,
            family_group_key VARCHAR(80) NOT NULL,
            is_correction TINYINT(1) NOT NULL DEFAULT 0,
            is_cancel TINYINT(1) NOT NULL DEFAULT 0,
            line_count INT NOT NULL DEFAULT 0,
            has_correction_text TINYINT(1) NOT NULL DEFAULT 0,
            has_cancel_text TINYINT(1) NOT NULL DEFAULT 0,
            family_member_count INT NOT NULL DEFAULT 0,
            correction_origin_date DATE NULL,
            cancel_reason_hint TEXT NULL,
            text_blob TEXT NULL,
            raw_evidence_json LONGTEXT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_disc_corp_date (corp_code, rcept_dt),
            KEY idx_disc_family (family_group_key),
            KEY idx_disc_anchor (family_anchor_rcept_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.lines} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            src_row_id BIGINT NULL,
            rcept_no CHAR(14) NOT NULL,
            rcept_dt DATE NULL,
            corp_code VARCHAR(8) NOT NULL,
            corp_name VARCHAR(200) NULL,
            report_nm VARCHAR(300) NULL,
            source VARCHAR(100) NULL,
            trf_pp TEXT NULL,
            family_group_key VARCHAR(80) NULL,
            family_anchor_rcept_no CHAR(14) NULL,
            target_raw VARCHAR(400) NULL,
            target_clean VARCHAR(400) NULL,
            target_key VARCHAR(300) NULL,
            is_target_valid TINYINT(1) NOT NULL DEFAULT 0,
            amount_value DECIMAL(30,6) NULL,
            qty_value DECIMAL(30,6) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_lines_rcp (rcept_no),
            KEY idx_lines_target (corp_code, target_key),
            KEY idx_lines_family (family_group_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.families} (
            family_group_key VARCHAR(80) PRIMARY KEY,
            corp_code VARCHAR(8) NOT NULL,
            family_anchor_rcept_no CHAR(14) NOT NULL,
            first_rcept_dt DATE NULL,
            last_rcept_dt DATE NULL,
            family_size INT NOT NULL DEFAULT 0,
            has_correction TINYINT(1) NOT NULL DEFAULT 0,
            has_cancel TINYINT(1) NOT NULL DEFAULT 0,
            member_rcepts_json LONGTEXT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_family_corp (corp_code),
            KEY idx_family_anchor (family_anchor_rcept_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.family_members} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            family_group_key VARCHAR(80) NOT NULL,
            seq_no INT NOT NULL,
            rcept_no CHAR(14) NOT NULL,
            rcept_dt DATE NULL,
            report_nm VARCHAR(300) NULL,
            is_anchor TINYINT(1) NOT NULL DEFAULT 0,
            is_correction TINYINT(1) NOT NULL DEFAULT 0,
            is_cancel TINYINT(1) NOT NULL DEFAULT 0,
            relation_type VARCHAR(20) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_family_member (family_group_key, rcept_no),
            KEY idx_family_member_family (family_group_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.investee_dim} (
            target_key VARCHAR(300) PRIMARY KEY,
            canonical_name VARCHAR(400) NULL,
            alias_count INT NOT NULL DEFAULT 0,
            aliases_json LONGTEXT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.edge_events} (
            event_id CHAR(64) PRIMARY KEY,
            family_group_key VARCHAR(80) NOT NULL,
            family_anchor_rcept_no CHAR(14) NULL,
            rcept_no CHAR(14) NOT NULL,
            rcept_dt DATE NULL,
            corp_code VARCHAR(8) NOT NULL,
            corp_name VARCHAR(200) NULL,
            target_key VARCHAR(300) NOT NULL,
            target_name VARCHAR(400) NULL,
            action_type VARCHAR(20) NOT NULL,
            amount_before DECIMAL(30,6) NULL,
            amount_after DECIMAL(30,6) NULL,
            amount_delta DECIMAL(30,6) NULL,
            qty_before DECIMAL(30,6) NULL,
            qty_after DECIMAL(30,6) NULL,
            qty_delta DECIMAL(30,6) NULL,
            is_correction TINYINT(1) NOT NULL DEFAULT 0,
            is_cancel TINYINT(1) NOT NULL DEFAULT 0,
            confidence TINYINT UNSIGNED NOT NULL DEFAULT 1,
            reason_text TEXT NULL,
            source_row_ids VARCHAR(300) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_events_corp_target_date (corp_code, target_key, rcept_dt),
            KEY idx_events_family (family_group_key),
            KEY idx_events_rcept (rcept_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.edge_state_current} (
            corp_code VARCHAR(8) NOT NULL,
            target_key VARCHAR(300) NOT NULL,
            corp_name VARCHAR(200) NULL,
            target_name VARCHAR(400) NULL,
            net_amount DECIMAL(30,6) NULL,
            net_qty DECIMAL(30,6) NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 0,
            last_event_id CHAR(64) NULL,
            last_rcept_no CHAR(14) NULL,
            last_rcept_dt DATE NULL,
            last_action_type VARCHAR(20) NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (corp_code, target_key),
            KEY idx_state_current_active (is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {t.edge_state_daily} (
            snapshot_date DATE NOT NULL,
            corp_code VARCHAR(8) NOT NULL,
            target_key VARCHAR(300) NOT NULL,
            corp_name VARCHAR(200) NULL,
            target_name VARCHAR(400) NULL,
            snapshot_amount DECIMAL(30,6) NULL,
            snapshot_qty DECIMAL(30,6) NULL,
            last_rcept_no CHAR(14) NULL,
            last_action_type VARCHAR(20) NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (snapshot_date, corp_code, target_key),
            KEY idx_state_daily_target (corp_code, target_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ]

    with conn.cursor() as cur:
        for ddl in ddls:
            cur.execute(ddl)
    conn.commit()


def _truncate_tables(conn: pymysql.connections.Connection, t: TableNames) -> None:
    order = [
        t.edge_state_daily,
        t.edge_state_current,
        t.edge_events,
        t.family_members,
        t.families,
        t.lines,
        t.disclosures,
        t.investee_dim,
    ]
    with conn.cursor() as cur:
        # FK가 걸린 운영 스키마에서는 TRUNCATE가 차단될 수 있어 DELETE 기반으로 비움.
        for name in order:
            cur.execute(f"DELETE FROM {name}")
        # AUTO_INCREMENT 초기화(가능한 테이블만)
        for name in order:
            try:
                cur.execute(f"ALTER TABLE {name} AUTO_INCREMENT = 1")
            except Exception:
                pass
    conn.commit()


def _insert_df(
    conn: pymysql.connections.Connection,
    table: str,
    df: pd.DataFrame,
    cols: list[str],
    batch_size: int = 1000,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> int:
    if df.empty:
        return 0
    table = _safe_table_name(table)

    data = []
    for rec in df[cols].to_dict(orient="records"):
        row = []
        for c in cols:
            v = rec.get(c)
            if isinstance(v, float) and pd.isna(v):
                row.append(None)
            elif isinstance(v, pd.Timestamp):
                row.append(str(v.date()))
            else:
                row.append(v)
        data.append(tuple(row))

    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(data), batch_size):
            chunk = data[i : i + batch_size]
            cur.executemany(sql, chunk)
            inserted += len(chunk)
            if progress_cb is not None:
                progress_cb(inserted, len(data))
    conn.commit()
    return inserted


def _load_table_columns(conn: pymysql.connections.Connection, table: str) -> set[str]:
    table = _safe_table_name(table)
    sql = """
    SELECT COLUMN_NAME
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (table,))
        rows = cur.fetchall()
    return {str(r.get("COLUMN_NAME")) for r in rows}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_etl_run_id() -> str:
    # 26-char compact run id
    return datetime.now().strftime("%Y%m%d%H%M%S%f")[:26]


def _classify_event_hint(report_nm: str, source: str, trf_pp: str, is_cancel: bool, delta_amount: float, delta_qty: float) -> str:
    report_nm = _clean_text(report_nm)
    source = _clean_text(source)
    trf_pp = _clean_text(trf_pp)
    sign = 1 if (delta_amount > 0 or (abs(delta_amount) < 1e-12 and delta_qty > 0)) else (-1 if (delta_amount < 0 or delta_qty < 0) else 0)
    is_plan = ("NOTE_PLAN" in source) or ("계획" in trf_pp)
    if is_cancel:
        return "CANCEL_PLAN" if is_plan else "CANCEL_EXECUTED"
    if is_plan:
        return "PLAN_ACQUIRE" if sign >= 0 else "PLAN_DISPOSE"
    if re.search(r"취득|양수", report_nm):
        return "ACQUIRE"
    if re.search(r"처분|양도", report_nm):
        return "DISPOSE"
    if sign > 0:
        return "ACQUIRE"
    if sign < 0:
        return "DISPOSE"
    return "UNKNOWN"


def _to_legacy_frames(
    disclosures_df: pd.DataFrame,
    lines_df: pd.DataFrame,
    fam_df: pd.DataFrame,
    mem_df: pd.DataFrame,
    events_df: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    # 1) disclosures
    disclosures_legacy = disclosures_df[
        [
            "rcept_no",
            "rcept_dt",
            "corp_cls",
            "corp_code",
            "corp_name",
            "report_nm",
            "flr_nm",
            "pblntf_ty",
            "viewer_url",
        ]
    ].copy()
    disclosures_legacy = disclosures_legacy.drop_duplicates(subset=["rcept_no"], keep="first")

    # 2) investee_dim
    valid_alias = lines_df[(lines_df["is_target_valid"] == True) & (lines_df["target_key"] != "")].copy()
    if valid_alias.empty:
        investee_dim = pd.DataFrame(columns=["name_norm", "name_canonical", "stock_code", "corp_code", "alias_json"])
    else:
        investee_dim = (
            valid_alias.groupby("target_key", as_index=False)
            .agg(
                name_canonical=("target_clean", _pick_longest_name),
                alias_json=("target_clean", lambda s: json.dumps(sorted(set([_clean_text(x) for x in s if _clean_text(x)])), ensure_ascii=False)),
            )
            .rename(columns={"target_key": "name_norm"})
        )
        investee_dim["stock_code"] = pd.NA
        investee_dim["corp_code"] = pd.NA

    # 3) lines_raw
    run_id = _build_etl_run_id()
    lraw = lines_df.copy().sort_values(["rcept_no", "id"]).reset_index(drop=True)
    lraw["line_no"] = lraw.groupby("rcept_no").cumcount() + 1
    lraw["iscmp_cmpnm_raw"] = lraw["target_raw"]
    lraw["iscmp_cmpnm_norm"] = lraw["target_key"]
    lraw["trfdtl_trfprc"] = lraw["amount_value"].map(lambda x: int(round(float(x))) if x is not None and not pd.isna(x) else None)
    lraw["trfdtl_stkcnt"] = lraw["qty_value"].map(lambda x: float(x) if x is not None and not pd.isna(x) else None)
    lraw["note_text"] = pd.NA
    lraw["parse_method"] = "RULE_BASED"
    lraw["parse_confidence"] = 0.85
    lraw["parse_evidence"] = "advanced_db_build parser"
    lraw["parse_version"] = "advanced_db_build_v1"
    lraw["event_action_hint"] = lraw.apply(
        lambda r: _classify_event_hint(
            report_nm=_to_str(r.get("report_nm")),
            source=_to_str(r.get("source")),
            trf_pp=_to_str(r.get("trf_pp")),
            is_cancel=False,
            delta_amount=float(r.get("amount_value") or 0.0),
            delta_qty=float(r.get("qty_value") or 0.0),
        ),
        axis=1,
    )
    lraw["etl_run_id"] = run_id
    lraw["row_hash"] = lraw.apply(
        lambda r: _sha256_text(
            "|".join(
                [
                    _to_str(r.get("rcept_no")),
                    _to_str(r.get("line_no")),
                    _to_str(r.get("source")),
                    _to_str(r.get("iscmp_cmpnm_norm")),
                    _to_str(r.get("trfdtl_trfprc")),
                    _to_str(r.get("trfdtl_stkcnt")),
                ]
            )
        ),
        axis=1,
    )
    lines_legacy = lraw[
        [
            "rcept_no",
            "line_no",
            "corp_code",
            "corp_name",
            "source",
            "viewer_url",
            "iscmp_cmpnm_raw",
            "iscmp_cmpnm_norm",
            "trfdtl_trfprc",
            "trfdtl_stkcnt",
            "trf_pp",
            "note_text",
            "parse_method",
            "parse_confidence",
            "parse_evidence",
            "parse_version",
            "event_action_hint",
            "row_hash",
            "etl_run_id",
        ]
    ].copy()

    # 4) families
    topic_map = disclosures_df.groupby("family_group_key", as_index=False).agg(topic_key=("report_family_key", "first"))
    famx = fam_df.merge(topic_map, on="family_group_key", how="left")
    for c in ["family_size", "has_correction", "has_cancel"]:
        if c not in famx.columns:
            famx[c] = 0
    famx["family_size"] = famx["family_size"].fillna(0).astype(int)
    famx["has_correction"] = famx["has_correction"].fillna(0).astype(int)
    famx["has_cancel"] = famx["has_cancel"].fillna(0).astype(int)

    # "실제 family"만 별도 family 테이블에 적재:
    # - 멤버가 2개 이상이거나
    # - 정정/취소 문맥이 있는 경우
    famx["is_real_family"] = (
        (famx["family_size"] >= 2)
        | (famx["has_correction"] > 0)
        | (famx["has_cancel"] > 0)
    ).astype(int)
    real_family_keys = set(
        famx.loc[famx["is_real_family"] == 1, "family_group_key"].astype(str).tolist()
    )

    # family_id는 사람이 읽을 수 있는 키로 유지한다: corp_code:anchor_rcept_no
    famx["family_id"] = famx["family_group_key"]
    famx["topic_key"] = famx["topic_key"].fillna("UNKNOWN")
    famx["resolve_method"] = "RULE+RAW"
    famx["resolve_conf"] = famx.apply(
        lambda r: 0.95 if int(r.get("has_correction", 0)) == 1 else 0.8,
        axis=1,
    )
    families_legacy = famx[famx["is_real_family"] == 1][
        [
            "family_group_key",
            "family_id",
            "corp_code",
            "topic_key",
            "family_anchor_rcept_no",
            "resolve_method",
            "resolve_conf",
        ]
    ].copy().rename(columns={"family_anchor_rcept_no": "root_rcept_no"})

    # 5) family members
    memx = mem_df.copy()
    memx = memx[memx["family_group_key"].astype(str).isin(real_family_keys)].copy()
    memx = memx.sort_values(["family_group_key", "seq_no"]).reset_index(drop=True)
    memx["family_id"] = memx["family_group_key"]
    memx["parent_rcept_no"] = memx.groupby("family_group_key")["rcept_no"].shift(1)
    memx["version_no"] = memx["seq_no"].astype(int)
    memx["effective_dt"] = memx["rcept_dt"]
    memx["confidence"] = 0.9
    members_legacy = memx[
        [
            "family_id",
            "rcept_no",
            "parent_rcept_no",
            "version_no",
            "relation_type",
            "effective_dt",
            "confidence",
        ]
    ].copy()

    # root FK 안전장치:
    # families.root_rcept_no는 disclosures_raw에 반드시 존재해야 한다.
    valid_rcepts = set([_to_str(x) for x in disclosures_legacy["rcept_no"].tolist() if _to_str(x)])
    first_member_map = (
        memx.sort_values(["family_group_key", "version_no"])
        .groupby("family_group_key", as_index=False)
        .agg(first_member_rcept_no=("rcept_no", "first"))
    )
    first_member_by_family = dict(zip(first_member_map["family_group_key"], first_member_map["first_member_rcept_no"]))
    corp_first_map_df = (
        disclosures_legacy.sort_values(["corp_code", "rcept_dt", "rcept_no"])
        .groupby("corp_code", as_index=False)
        .agg(first_rcept_no=("rcept_no", "first"))
    )
    corp_first_rcept = dict(zip(corp_first_map_df["corp_code"], corp_first_map_df["first_rcept_no"]))

    def _safe_root(row: pd.Series) -> str:
        r = _to_str(row.get("root_rcept_no"))
        if r in valid_rcepts:
            return r
        fk = _to_str(row.get("family_group_key"))
        fm = _to_str(first_member_by_family.get(fk))
        if fm in valid_rcepts:
            return fm
        corp_code = _to_str(row.get("corp_code"))
        c0 = _to_str(corp_first_rcept.get(corp_code))
        if c0 in valid_rcepts:
            return c0
        if valid_rcepts:
            return sorted(valid_rcepts)[0]
        return r

    families_legacy["root_rcept_no"] = families_legacy.apply(_safe_root, axis=1)
    families_legacy = families_legacy.drop(columns=["family_group_key"])

    # 6) edge events (investee_id는 나중에 name_norm으로 조인해서 채움)
    ev = events_df.copy()
    if ev.empty:
        edge_events_legacy = pd.DataFrame(
            columns=[
                "rcept_no",
                "family_id",
                "corp_code",
                "corp_name",
                "target_name_norm",
                "event_action",
                "event_effect_sign",
                "delta_amount",
                "delta_shares",
                "reason_text",
                "rcept_dt",
                "effective_dt",
                "confidence",
                "row_hash",
            ]
        )
    else:
        # 일반(비정정/비취소/단일 멤버) 공시는 family_id를 비워
        # edge 이벤트는 유지하되 family 체인으로는 보지 않음.
        ev["family_id"] = ev["family_group_key"].map(
            lambda x: _to_str(x) if _to_str(x) in real_family_keys else None
        )
        ev["target_name_norm"] = ev["target_key"]
        ev["event_effect_sign"] = ev["amount_delta"].map(lambda x: 1 if float(x) > 0 else (-1 if float(x) < 0 else 0))
        ev.loc[(ev["event_effect_sign"] == 0) & (ev["qty_delta"] > 0), "event_effect_sign"] = 1
        ev.loc[(ev["event_effect_sign"] == 0) & (ev["qty_delta"] < 0), "event_effect_sign"] = -1
        ev["event_action"] = ev.apply(
            lambda r: _classify_event_hint(
                report_nm=_to_str(r.get("reason_text")),
                source=_to_str(r.get("reason_text")),
                trf_pp=_to_str(r.get("reason_text")),
                is_cancel=int(r.get("is_cancel", 0)) == 1,
                delta_amount=float(r.get("amount_delta") or 0.0),
                delta_qty=float(r.get("qty_delta") or 0.0),
            ),
            axis=1,
        )
        ev["delta_amount"] = ev["amount_delta"].map(lambda x: int(round(float(x))) if x is not None and not pd.isna(x) else None)
        ev["delta_shares"] = ev["qty_delta"].map(lambda x: float(x) if x is not None and not pd.isna(x) else None)
        ev["effective_dt"] = ev["rcept_dt"]
        ev["row_hash"] = ev.apply(
            lambda r: _sha256_text(
                "|".join(
                    [
                        _to_str(r.get("rcept_no")),
                        _to_str(r.get("corp_code")),
                        _to_str(r.get("target_name_norm")),
                        _to_str(r.get("event_action")),
                        _to_str(r.get("delta_amount")),
                        _to_str(r.get("delta_shares")),
                    ]
                )
            ),
            axis=1,
        )
        edge_events_legacy = ev[
            [
                "rcept_no",
                "family_id",
                "corp_code",
                "corp_name",
                "target_name_norm",
                "event_action",
                "event_effect_sign",
                "delta_amount",
                "delta_shares",
                "reason_text",
                "rcept_dt",
                "effective_dt",
                "confidence",
                "row_hash",
            ]
        ].copy()

    return {
        "disclosures": disclosures_legacy,
        "investee_dim": investee_dim,
        "lines": lines_legacy,
        "families": families_legacy,
        "members": members_legacy,
        "edge_events": edge_events_legacy,
    }


def _prepare_legacy_states(edge_events_db_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if edge_events_db_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    ev = edge_events_db_df.copy()
    ev["effective_dt"] = ev["effective_dt"].map(_norm_date)
    ev = ev.sort_values(["corp_code", "target_name_norm", "effective_dt", "rcept_no", "edge_event_id"]).reset_index(drop=True)
    ev["delta_amount"] = ev["delta_amount"].fillna(0).astype(float)
    ev["delta_shares"] = ev["delta_shares"].fillna(0).astype(float)

    plan_actions = {"PLAN_ACQUIRE", "PLAN_DISPOSE", "CANCEL_PLAN"}
    ev["is_plan"] = ev["event_action"].isin(plan_actions).astype(int)
    ev["holding_delta_amount"] = ev.apply(lambda r: 0.0 if r["is_plan"] == 1 else float(r["delta_amount"]), axis=1)
    ev["holding_delta_shares"] = ev.apply(lambda r: 0.0 if r["is_plan"] == 1 else float(r["delta_shares"]), axis=1)
    ev["planned_delta_amount"] = ev.apply(lambda r: float(r["delta_amount"]) if r["is_plan"] == 1 else 0.0, axis=1)
    ev["planned_delta_shares"] = ev.apply(lambda r: float(r["delta_shares"]) if r["is_plan"] == 1 else 0.0, axis=1)

    ev["holding_amount"] = ev.groupby(["corp_code", "target_name_norm"], sort=False)["holding_delta_amount"].cumsum()
    ev["holding_shares"] = ev.groupby(["corp_code", "target_name_norm"], sort=False)["holding_delta_shares"].cumsum()
    ev["planned_amount"] = ev.groupby(["corp_code", "target_name_norm"], sort=False)["planned_delta_amount"].cumsum()
    ev["planned_shares"] = ev.groupby(["corp_code", "target_name_norm"], sort=False)["planned_delta_shares"].cumsum()

    last = ev.groupby(["corp_code", "target_name_norm"], as_index=False).last()
    first_dt = ev.groupby(["corp_code", "target_name_norm"], as_index=False).agg(first_connected_dt=("effective_dt", "min"))
    curr = last.merge(first_dt, on=["corp_code", "target_name_norm"], how="left")
    curr["active_flag"] = (
        (curr["holding_amount"].abs() > 1e-12)
        | (curr["holding_shares"].abs() > 1e-12)
        | (curr["planned_amount"].abs() > 1e-12)
        | (curr["planned_shares"].abs() > 1e-12)
    ).astype(int)
    curr["closed_dt"] = curr.apply(lambda r: r["effective_dt"] if int(r["active_flag"]) == 0 else None, axis=1)

    current_df = curr[
        [
            "corp_code",
            "target_name_norm",
            "corp_name",
            "active_flag",
            "first_connected_dt",
            "effective_dt",
            "closed_dt",
            "holding_amount",
            "holding_shares",
            "planned_amount",
            "planned_shares",
            "rcept_no",
        ]
    ].copy().rename(
        columns={
            "effective_dt": "last_changed_dt",
            "rcept_no": "last_rcept_no",
        }
    )
    current_df["investee_id"] = pd.NA
    current_df["last_edge_event_id"] = curr.get("edge_event_id")

    daily = (
        ev.groupby(["effective_dt", "corp_code", "target_name_norm"], as_index=False)
        .agg(
            corp_name=("corp_name", "last"),
            holding_amount=("holding_amount", "last"),
            holding_shares=("holding_shares", "last"),
            planned_amount=("planned_amount", "last"),
            planned_shares=("planned_shares", "last"),
            last_rcept_no=("rcept_no", "last"),
            last_edge_event_id=("edge_event_id", "last"),
        )
        .rename(columns={"effective_dt": "as_of_date"})
    )
    daily["active_flag"] = (
        (daily["holding_amount"].abs() > 1e-12)
        | (daily["holding_shares"].abs() > 1e-12)
        | (daily["planned_amount"].abs() > 1e-12)
        | (daily["planned_shares"].abs() > 1e-12)
    ).astype(int)
    daily["investee_id"] = pd.NA
    daily_df = daily[
        [
            "as_of_date",
            "corp_code",
            "target_name_norm",
            "investee_id",
            "active_flag",
            "holding_amount",
            "holding_shares",
            "planned_amount",
            "planned_shares",
            "last_rcept_no",
            "last_edge_event_id",
        ]
    ].copy()

    return current_df, daily_df

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build advanced investment DB tables with family/correction/cancel semantics from source DB + local raw logs."
    )
    source_default = (
        os.getenv("DB_SOURCE_TABLE")
        or os.getenv("SOURCE_TABLE_NAME")
        or DEFAULT_SOURCE_TABLE
    )
    parser.add_argument("--source-table", default=source_default)
    parser.add_argument("--table-suffix", default=DEFAULT_SUFFIX)
    parser.add_argument("--raw-dir", default=DEFAULT_RAW_DIR)
    parser.add_argument("--corp-code", default=None, help="Optional 8-digit corp_code filter (single company sample run).")
    parser.add_argument("--sample-limit", type=int, default=None, help="Optional max source rows to load after filter.")
    parser.add_argument("--truncate-target", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--progress-file",
        default=".advanced_db_build_progress.json",
        help="Path to JSON progress file written during run.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    db = _load_db_config()
    tables = _build_table_names(args.table_suffix)
    raw_dir = Path(args.raw_dir)
    progress_path = Path(args.progress_file)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    progress: dict[str, Any] = {
        "run_id": run_id,
        "status": "starting",
        "step": "init",
        "source_table": args.source_table,
        "table_suffix": args.table_suffix,
        "raw_dir": str(raw_dir),
        "corp_code_filter": args.corp_code,
        "sample_limit": args.sample_limit,
        "truncate_target": bool(args.truncate_target),
        "dry_run": bool(args.dry_run),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }

    def _mark_step(step: str, status: str = "running", print_line: bool = True, **fields: Any) -> None:
        progress["step"] = step
        progress["status"] = status
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        for k, v in fields.items():
            if isinstance(v, (datetime, date, pd.Timestamp)):
                progress[k] = str(v)
            else:
                progress[k] = v
        _write_json_atomic(progress_path, progress)
        if print_line:
            print(f"[STEP] {step} ({status})")

    def _chunk_cb(step_name: str) -> Callable[[int, int], None]:
        def _cb(done: int, total: int) -> None:
            _mark_step(
                step_name,
                status="running",
                print_line=False,
                **{f"{step_name}_progress": f"{done}/{total}"},
            )
            if done == total or done % 5000 == 0:
                print(f"[STEP] {step_name} progress {done:,}/{total:,}")

        return _cb

    _mark_step("init", "running")

    conn = pymysql.connect(
        host=db.host,
        port=db.port,
        user=db.user,
        password=db.password,
        database=db.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )

    try:
        try:
            _mark_step("load_source", "running")
            corp_filter = _norm_corp_code(args.corp_code) if args.corp_code else None
            source_df = _load_source_rows(
                conn,
                args.source_table,
                corp_code_filter=corp_filter,
                sample_limit=args.sample_limit,
            )
            if source_df.empty:
                raise RuntimeError("No source rows loaded. Check source table and report filter.")
            print(
                f"[LOAD] source_rows={len(source_df):,}"
                f", corp_code_filter={corp_filter or 'ALL'}"
                f", sample_limit={args.sample_limit if args.sample_limit else 'NONE'}"
            )
            _mark_step("load_source", "done", source_rows=int(len(source_df)), corp_code_filter=corp_filter or "ALL")

            _mark_step("load_evidence", "running")
            evidence_df = _load_raw_evidence(raw_dir)
            print(f"[LOAD] raw_evidence_rows={len(evidence_df):,} from {raw_dir}")
            _mark_step("load_evidence", "done", raw_evidence_rows=int(len(evidence_df)))

            _mark_step("build_frames", "running")
            disclosures_df = _prepare_disclosures(source_df, evidence_df)
            lines_df, snapshot_df = _prepare_lines(source_df, disclosures_df)
            events_df = _build_edge_events(disclosures_df, snapshot_df)
            state_current_df, state_daily_df = _build_states(events_df)

            fam_df = (
                disclosures_df.groupby("family_group_key", as_index=False)
                .agg(
                    corp_code=("corp_code", "first"),
                    family_anchor_rcept_no=("family_anchor_rcept_no", "first"),
                    first_rcept_dt=("rcept_dt", "min"),
                    last_rcept_dt=("rcept_dt", "max"),
                    family_size=("rcept_no", "nunique"),
                    has_correction=("is_correction", "max"),
                    has_cancel=("is_cancel", "max"),
                    member_rcepts_json=("rcept_no", lambda s: json.dumps(sorted(set([_to_str(x) for x in s if _to_str(x)])), ensure_ascii=False)),
                )
            )

            mem = disclosures_df.sort_values(["family_group_key", "rcept_dt", "rcept_no"]).copy()
            mem["seq_no"] = mem.groupby("family_group_key").cumcount() + 1
            mem["is_anchor"] = (mem["rcept_no"] == mem["family_anchor_rcept_no"]).astype(int)
            mem["relation_type"] = "UPDATE"
            mem.loc[mem["is_anchor"] == 1, "relation_type"] = "ROOT"
            mem.loc[mem["is_correction"] == 1, "relation_type"] = "CORRECTION"
            mem.loc[mem["is_cancel"] == 1, "relation_type"] = "CANCEL"
            mem_df = mem[
                [
                    "family_group_key",
                    "seq_no",
                    "rcept_no",
                    "rcept_dt",
                    "report_nm",
                    "is_anchor",
                    "is_correction",
                    "is_cancel",
                    "relation_type",
                ]
            ].copy()

            real_family_count = int(
                (
                    (fam_df["family_size"].fillna(0).astype(int) >= 2)
                    | (fam_df["has_correction"].fillna(0).astype(int) > 0)
                    | (fam_df["has_cancel"].fillna(0).astype(int) > 0)
                ).sum()
            )

            valid_alias = lines_df[(lines_df["is_target_valid"] == True) & (lines_df["target_key"] != "")].copy()
            investee_dim_df = (
                valid_alias.groupby("target_key", as_index=False)
                .agg(
                    canonical_name=("target_clean", _pick_longest_name),
                    alias_count=("target_clean", lambda s: len(set([_clean_text(x) for x in s if _clean_text(x)]))),
                    aliases_json=("target_clean", lambda s: json.dumps(sorted(set([_clean_text(x) for x in s if _clean_text(x)])), ensure_ascii=False)),
                )
            )

            print(
                "[BUILD] "
                f"disclosures={len(disclosures_df):,}, "
                f"lines={len(lines_df):,}, families={len(fam_df):,}, "
                f"real_families={real_family_count:,}, "
                f"family_members={len(mem_df):,}, events={len(events_df):,}, "
                f"state_current={len(state_current_df):,}, state_daily={len(state_daily_df):,}"
            )
            _mark_step(
                "build_frames",
                "done",
                disclosures=int(len(disclosures_df)),
                lines=int(len(lines_df)),
                families=int(len(fam_df)),
                real_families=real_family_count,
                family_members=int(len(mem_df)),
                events=int(len(events_df)),
                state_current=int(len(state_current_df)),
                state_daily=int(len(state_daily_df)),
            )

            if args.dry_run:
                print("[DRY-RUN] Table DDL/data insert skipped.")
                _mark_step("done", "completed", dry_run=True)
                return

            _mark_step("create_tables", "running")
            _create_tables(conn, tables)
            _mark_step("create_tables", "done")

            if args.truncate_target:
                _mark_step("truncate_target", "running")
                _truncate_tables(conn, tables)
                _mark_step("truncate_target", "done")

            _mark_step("check_schema", "running")
            edge_event_cols = _load_table_columns(conn, tables.edge_events)
            legacy_mode = "edge_event_id" in edge_event_cols
            if not legacy_mode:
                raise RuntimeError(
                    "Current script supports legacy no-v2 schema in this run. "
                    "Detected edge_events table without edge_event_id."
                )
            _mark_step("check_schema", "done", legacy_mode=bool(legacy_mode))

            _mark_step("to_legacy_frames", "running")
            frames = _to_legacy_frames(disclosures_df, lines_df, fam_df, mem_df, events_df)
            disclosures_legacy = frames["disclosures"]
            investee_dim = frames["investee_dim"]
            lines_legacy = frames["lines"]
            families_legacy = frames["families"]
            members_legacy = frames["members"]
            edge_events_legacy = frames["edge_events"]
            _mark_step(
                "to_legacy_frames",
                "done",
                legacy_disclosures=int(len(disclosures_legacy)),
                legacy_lines=int(len(lines_legacy)),
                legacy_families=int(len(families_legacy)),
                legacy_members=int(len(members_legacy)),
                legacy_events=int(len(edge_events_legacy)),
            )

            _mark_step("insert_disclosures", "running")
            ins_disclosures = _insert_df(
                conn,
                tables.disclosures,
                disclosures_legacy,
                ["rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name", "report_nm", "flr_nm", "pblntf_ty", "viewer_url"],
                progress_cb=_chunk_cb("insert_disclosures"),
            )
            _mark_step("insert_disclosures", "done", inserted_disclosures=int(ins_disclosures))

            _mark_step("insert_investee_dim", "running")
            ins_dim = _insert_df(
                conn,
                tables.investee_dim,
                investee_dim,
                ["name_norm", "name_canonical", "stock_code", "corp_code", "alias_json"],
                progress_cb=_chunk_cb("insert_investee_dim"),
            )
            _mark_step("insert_investee_dim", "done", inserted_investee_dim=int(ins_dim))

            _mark_step("load_investee_map", "running")
            with conn.cursor() as cur:
                cur.execute(f"SELECT investee_id, name_norm FROM {tables.investee_dim}")
                investee_map = {str(r["name_norm"]): int(r["investee_id"]) for r in cur.fetchall()}
            _mark_step("load_investee_map", "done", investee_map_size=int(len(investee_map)))

            _mark_step("insert_lines", "running")
            ins_lines = _insert_df(
                conn,
                tables.lines,
                lines_legacy,
                [
                    "rcept_no",
                    "line_no",
                    "corp_code",
                    "corp_name",
                    "source",
                    "viewer_url",
                    "iscmp_cmpnm_raw",
                    "iscmp_cmpnm_norm",
                    "trfdtl_trfprc",
                    "trfdtl_stkcnt",
                    "trf_pp",
                    "note_text",
                    "parse_method",
                    "parse_confidence",
                    "parse_evidence",
                    "parse_version",
                    "event_action_hint",
                    "row_hash",
                    "etl_run_id",
                ],
                progress_cb=_chunk_cb("insert_lines"),
            )
            _mark_step("insert_lines", "done", inserted_lines=int(ins_lines))

            _mark_step("insert_families", "running")
            ins_families = _insert_df(
                conn,
                tables.families,
                families_legacy,
                ["family_id", "corp_code", "topic_key", "root_rcept_no", "resolve_method", "resolve_conf"],
                progress_cb=_chunk_cb("insert_families"),
            )
            _mark_step("insert_families", "done", inserted_families=int(ins_families))

            _mark_step("insert_family_members", "running")
            ins_members = _insert_df(
                conn,
                tables.family_members,
                members_legacy,
                ["family_id", "rcept_no", "parent_rcept_no", "version_no", "relation_type", "effective_dt", "confidence"],
                progress_cb=_chunk_cb("insert_family_members"),
            )
            _mark_step("insert_family_members", "done", inserted_family_members=int(ins_members))

            _mark_step("insert_edge_events", "running")
            edge_events_legacy = edge_events_legacy.copy()
            edge_events_legacy["investee_id"] = edge_events_legacy["target_name_norm"].map(investee_map)
            edge_events_legacy = edge_events_legacy.drop_duplicates(subset=["row_hash"], keep="first")
            ins_events = _insert_df(
                conn,
                tables.edge_events,
                edge_events_legacy,
                [
                    "rcept_no",
                    "family_id",
                    "corp_code",
                    "corp_name",
                    "investee_id",
                    "target_name_norm",
                    "event_action",
                    "event_effect_sign",
                    "delta_amount",
                    "delta_shares",
                    "reason_text",
                    "rcept_dt",
                    "effective_dt",
                    "confidence",
                    "row_hash",
                ],
                progress_cb=_chunk_cb("insert_edge_events"),
            )
            _mark_step("insert_edge_events", "done", inserted_edge_events=int(ins_events))

            _mark_step("build_state_legacy", "running")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT edge_event_id, rcept_no, corp_code, corp_name, target_name_norm,
                           event_action, delta_amount, delta_shares, effective_dt
                    FROM {tables.edge_events}
                    ORDER BY edge_event_id ASC
                    """
                )
                edge_db_df = pd.DataFrame(cur.fetchall())

            state_current_legacy, state_daily_legacy = _prepare_legacy_states(edge_db_df)
            state_current_legacy["investee_id"] = state_current_legacy["target_name_norm"].map(investee_map)
            state_daily_legacy["investee_id"] = state_daily_legacy["target_name_norm"].map(investee_map)
            _mark_step(
                "build_state_legacy",
                "done",
                state_current_rows=int(len(state_current_legacy)),
                state_daily_rows=int(len(state_daily_legacy)),
            )

            _mark_step("insert_state_current", "running")
            ins_curr = _insert_df(
                conn,
                tables.edge_state_current,
                state_current_legacy,
                [
                    "corp_code",
                    "target_name_norm",
                    "investee_id",
                    "corp_name",
                    "active_flag",
                    "first_connected_dt",
                    "last_changed_dt",
                    "closed_dt",
                    "holding_amount",
                    "holding_shares",
                    "planned_amount",
                    "planned_shares",
                    "last_rcept_no",
                    "last_edge_event_id",
                ],
                progress_cb=_chunk_cb("insert_state_current"),
            )
            _mark_step("insert_state_current", "done", inserted_state_current=int(ins_curr))

            _mark_step("insert_state_daily", "running")
            ins_daily = _insert_df(
                conn,
                tables.edge_state_daily,
                state_daily_legacy,
                [
                    "as_of_date",
                    "corp_code",
                    "target_name_norm",
                    "investee_id",
                    "active_flag",
                    "holding_amount",
                    "holding_shares",
                    "planned_amount",
                    "planned_shares",
                    "last_rcept_no",
                    "last_edge_event_id",
                ],
                progress_cb=_chunk_cb("insert_state_daily"),
            )
            _mark_step("insert_state_daily", "done", inserted_state_daily=int(ins_daily))

            print(
                "[DONE] "
                f"{tables.disclosures}={ins_disclosures:,}, "
                f"{tables.lines}={ins_lines:,}, "
                f"{tables.families}={ins_families:,}, "
                f"{tables.family_members}={ins_members:,}, "
                f"{tables.investee_dim}={ins_dim:,}, "
                f"{tables.edge_events}={ins_events:,}, "
                f"{tables.edge_state_current}={ins_curr:,}, "
                f"{tables.edge_state_daily}={ins_daily:,}"
            )
            _mark_step(
                "done",
                "completed",
                inserted_disclosures=int(ins_disclosures),
                inserted_lines=int(ins_lines),
                inserted_families=int(ins_families),
                inserted_family_members=int(ins_members),
                inserted_investee_dim=int(ins_dim),
                inserted_edge_events=int(ins_events),
                inserted_state_current=int(ins_curr),
                inserted_state_daily=int(ins_daily),
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
        except BaseException as exc:
            _mark_step(
                "failed",
                "failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
