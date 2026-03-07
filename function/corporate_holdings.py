# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import html
import numbers
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

from function.filling import OpenDartClient


OTR_CPR_INVSTMNT_URL = "https://opendart.fss.or.kr/api/otrCprInvstmntSttus.json"
MAJORSTOCK_URL = "https://opendart.fss.or.kr/api/majorstock.json"
INH_DECSN_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrInhDecsn.json"
TRF_DECSN_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrTrfDecsn.json"
STK_EXTR_DECSN_URL = "https://opendart.fss.or.kr/api/stkExtrDecsn.json"
BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do"
VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"
TRANSFER_TITLE_REGEX = r"타법인\s*주식\s*및\s*출자증권\s*(?:처분결정|양도결정|취득결정|양수결정)"

VIEWDOC_PATTERN = re.compile(
    r"viewDoc\(\s*['\"](?P<rcpNo>\d{14})['\"]\s*,\s*['\"](?P<dcmNo>\d+)['\"]\s*,\s*['\"](?P<eleId>\d+)['\"]\s*,\s*['\"](?P<offset>\d+)['\"]\s*,\s*['\"](?P<length>\d+)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"](?:\s*,\s*['\"][^'\"]*['\"])?\s*\)",
    re.IGNORECASE,
)

GRAPH_REQUIRED_COLS = [
    "corp_name",
    "iscmp_cmpnm",
    "trfdtl_trfprc",
]

METADATA_COLS = [
    "rcept_no",
    "rcept_dt",
    "corp_cls",
    "corp_code",
    "report_nm",
    "flr_nm",
    "pblntf_ty",
    "source",
    "viewer_url",
]

GRAPH_OPTIONAL_COLS = [
    "trfdtl_stkcnt",
    "trf_pp",
]

OUTPUT_COLS = METADATA_COLS + GRAPH_REQUIRED_COLS + GRAPH_OPTIONAL_COLS


def _empty_output_df() -> pd.DataFrame:
    return pd.DataFrame(columns=OUTPUT_COLS)


def _norm_yyyymmdd(s: str) -> str:
    raw = str(s).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {s}")
    return raw


def _date_from_rcept_no(sr: pd.Series) -> pd.Series:
    txt = sr.astype(str).str.extract(r"(\d{8})", expand=False)
    dt = pd.to_datetime(txt, format="%Y%m%d", errors="coerce")
    return dt.dt.strftime("%Y-%m-%d")


def _viewer_url_from_rcept_no(sr: pd.Series) -> pd.Series:
    return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + sr.astype(str)


def _clean_numeric_series(sr: pd.Series) -> pd.Series:
    cleaned = (
        sr.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace(r"[^\d\.\-:/]", "", regex=True)
        .str.strip()
    )
    return cleaned


def _to_numeric_series(sr: pd.Series) -> pd.Series:
    return pd.to_numeric(_clean_numeric_series(sr), errors="coerce")


def _to_numeric_scalar(x: Any) -> Any:
    if x is None or x is pd.NA:
        return pd.NA
    s = re.sub(r"[^\d\.\-]", "", str(x))
    if s in {"", "-", ".", "-."}:
        return pd.NA
    try:
        v = float(s)
    except Exception:
        return pd.NA
    if float(v).is_integer():
        return int(v)
    return v


def _ratio_to_float(x: Any) -> Any:
    if x is None or x is pd.NA:
        return pd.NA
    s = str(x).strip()
    if not s:
        return pd.NA

    for sep in (":", "/"):
        if sep in s:
            parts = [p.strip() for p in s.split(sep, 1)]
            if len(parts) != 2:
                return pd.NA
            a = _to_numeric_scalar(parts[0])
            b = _to_numeric_scalar(parts[1])
            if a is pd.NA or b is pd.NA or b in (0, 0.0):
                return pd.NA
            try:
                return float(a) / float(b)
            except Exception:
                return pd.NA

    return _to_numeric_scalar(s)


def _string_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    s = df[col].astype("string").str.strip()
    return s.mask(s == "", pd.NA).astype("object")


def _first_non_null_series(df: pd.DataFrame, *cols: str) -> pd.Series:
    out = pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    for c in cols:
        out = out.combine_first(_string_col(df, c))
    return out


def _first_non_null_numeric_series(df: pd.DataFrame, *cols: str) -> pd.Series:
    out = pd.Series([float("nan")] * len(df), index=df.index, dtype="float64")
    for c in cols:
        if c not in df.columns:
            continue
        out = out.combine_first(_to_numeric_series(df[c]))
    return out


def _is_summary_counterparty(x: Any) -> bool:
    if x is None or x is pd.NA:
        return False
    s = str(x).strip()
    if not s:
        return False
    s = re.sub(r"\s+", "", s)
    return s in {"합계", "총계", "소계"}


def select_graph_and_metadata_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return _empty_output_df()

    out = df.copy()
    for c in OUTPUT_COLS:
        if c not in out.columns:
            out[c] = pd.NA

    out = out[OUTPUT_COLS].copy()

    for c in ("trfdtl_trfprc", "trfdtl_stkcnt"):
        out[c] = _to_numeric_series(out[c])

    if "rcept_no" in out.columns and "viewer_url" in out.columns:
        miss = out["viewer_url"].isna() | (out["viewer_url"].astype(str).str.strip() == "")
        if miss.any():
            out.loc[miss, "viewer_url"] = _viewer_url_from_rcept_no(out.loc[miss, "rcept_no"])

    if "rcept_dt" in out.columns:
        dt = pd.to_datetime(out["rcept_dt"], errors="coerce")
        out["rcept_dt"] = dt.dt.strftime("%Y-%m-%d").where(dt.notna(), out["rcept_dt"])

    if "iscmp_cmpnm" in out.columns:
        out = out[~out["iscmp_cmpnm"].map(_is_summary_counterparty)].copy()

    return out


LEGACY_OUT_COLS = [
    "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
    "report_nm", "flr_nm", "pblntf_ty", "source",
    "iscmp_cmpnm",
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt",
    "trf_pp", "trf_prd", "dlptn_cmpnm",
    "bddd", "viewer_url",
]

LEGACY_NUM_COLS = [
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt",
]

LEGACY_CORE_FIELDS = [
    "iscmp_cmpnm", "trfdtl_trfprc", "trfdtl_stkcnt",
    "trf_pp", "dlptn_cmpnm", "bddd",
]

LEGACY_LABEL_MAP = {
    "iscmp_cmpnm": ["발행회사(회사명)", "발행회사 회사명", "발행회사", "회사명"],
    "trfdtl_stkcnt": [
        "양도내역(양도주식수(주))", "양도주식수", "양도 주식수",
        "취득내역(취득주식수(주))", "취득주식수", "취득 주식수",
        "양수내역(양수주식수(주))", "양수주식수", "양수 주식수",
    ],
    "trfdtl_trfprc": [
        "양도내역(양도금액(원)(A))", "양도금액", "양도 금액",
        "취득내역(취득금액(원)(A))", "취득금액", "취득 금액",
        "양수내역(양수금액(원)(A))", "양수금액", "양수 금액",
    ],
    "trfdtl_tast": ["양도내역(총자산(원)(B))", "총자산(원)(B)", "총자산"],
    "trfdtl_ecpt": ["양도내역(자기자본(원)(C))", "자기자본(원)(C)", "자기자본"],
    "attrf_owstkcnt": ["양도후 소유주식수 및 지분비율(소유주식수(주))", "양도후 소유주식수", "소유주식수(주)"],
    "attrf_eqrt": ["양도후 소유주식수 및 지분비율(지분비율(%))", "양도후 지분비율", "지분비율(%)"],
    "trf_pp": ["양도목적", "양도 목적", "취득목적", "취득 목적", "양수목적", "양수 목적"],
    "trf_prd": ["양도예정일자", "양도 예정일자", "취득예정일자", "취득 예정일자", "양수예정일자", "양수 예정일자"],
    "dlptn_cmpnm": ["거래상대방(회사명(성명))", "거래상대방 회사명", "거래상대방", "상대방"],
    "bddd": ["이사회결의일(결정일)", "이사회결의일", "결정일"],
    "corp_name": ["공시대상회사명", "회사명"],
    "corp_cls": ["법인구분"],
    "corp_code": ["고유번호"],
    "report_nm": ["보고서명", "공시명"],
}


def _ensure_cols(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out


def _decode_bytes_auto(data: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_rcp_no(url_or_rcp: str) -> str:
    s = str(url_or_rcp).strip()
    m = re.search(r"rcpNo=(\d{14})", s)
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"\d{14}", s)
    if m2:
        return s
    raise ValueError(f"rcept_no(14자리) 또는 rcpNo URL 형식이 아닙니다: {s}")


def _num_or_na(x: Any) -> Any:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return pd.NA
    s = str(x).strip()
    if not s:
        return pd.NA
    s = s.replace(",", "").replace("%", "")
    s = re.sub(r"[^\d\.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return pd.NA
    try:
        v = float(s)
        return int(v) if v.is_integer() else v
    except Exception:
        return pd.NA


def _num_or_na_keep_sign(x: Any) -> Any:
    if x is None or x is pd.NA:
        return pd.NA
    if isinstance(x, numbers.Number):
        return x
    s = str(x).strip()
    if not s or s in {"<NA>", "N/A", "NA"}:
        return pd.NA
    s = s.replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return pd.NA
    tok = m.group(0)
    try:
        if "." in tok:
            return float(tok)
        return int(tok)
    except Exception:
        return pd.NA


def _signed_num(x: Any, sign: int = 1) -> Any:
    v = _num_or_na_keep_sign(x)
    if pd.isna(v):
        return pd.NA
    return abs(v) * sign


def _norm_date_any(x: Any) -> Any:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return pd.NA
    s = re.sub(r"[^\d]", "", str(x))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return pd.NA


def _clean_lines_from_text(text: str) -> List[str]:
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r", "", text)
    lines = []
    for ln in text.split("\n"):
        s = ln.strip(" \t:-")
        if s:
            lines.append(s)
    return lines


def _pick_value_from_lines(lines: List[str], aliases: List[str]) -> Optional[str]:
    for i, ln in enumerate(lines):
        if any(a in ln for a in aliases):
            m = re.split(r"[:：]\s*", ln, maxsplit=1)
            if len(m) == 2 and m[1].strip():
                return m[1].strip()
            for j in range(i + 1, min(i + 8, len(lines))):
                cand = lines[j].strip(" \t:-")
                if not cand:
                    continue
                if any(a in cand for a in aliases):
                    continue
                return cand
    return None


def _extract_kv_from_html(html_text: str) -> Dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    kv: Dict[str, str] = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        for i in range(0, len(cells) - 1, 2):
            k = cells[i].strip()
            v = cells[i + 1].strip()
            if k and v and k not in kv:
                kv[k] = v
    return kv


def _extract_fields_from_html_text(html_text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {c: pd.NA for c in LEGACY_OUT_COLS}
    kv = _extract_kv_from_html(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = _clean_lines_from_text(text)

    for field, aliases in LEGACY_LABEL_MAP.items():
        val = None
        for a in aliases:
            if a in kv:
                val = kv[a]
                break
        if val is None:
            for k, v in kv.items():
                if any(a in k for a in aliases):
                    val = v
                    break
        if val is None:
            val = _pick_value_from_lines(lines, aliases)
        if val is not None and str(val).strip() != "":
            vv = str(val).strip()
            if field == "iscmp_cmpnm" and _is_bad_issuer(vv):
                out[field] = pd.NA
            else:
                out[field] = vv

    for c in LEGACY_NUM_COLS:
        out[c] = _num_or_na(out.get(c))
    out["bddd"] = _norm_date_any(out.get("bddd"))
    return out


def _find_viewdoc_candidates(main_html: str) -> List[Dict[str, str]]:
    cands: List[Dict[str, str]] = []
    for m in VIEWDOC_PATTERN.finditer(main_html):
        c = m.groupdict()
        s, e = m.span()
        ctx = main_html[max(0, s - 200): min(len(main_html), e + 200)]
        c["_ctx"] = ctx
        cands.append(c)
    return cands


def _choose_best_candidate(
    cands: List[Dict[str, str]],
    keyword: str = "타법인주식및출자증권처분결정",
) -> Optional[Dict[str, str]]:
    if not cands:
        return None
    scored = []
    for c in cands:
        score = 0
        ctx = c.get("_ctx", "")
        if keyword in ctx:
            score += 10
        if "dart3.xsd" in c.get("dtd", "").lower():
            score += 1
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _fetch_viewer_html_by_rcpno(
    rcp_no: str,
    session: requests.Session,
    timeout: int = 30,
    request_interval_sec: float = 0.0,
    out_meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], str]:
    main_resp = _throttled_session_get(
        session,
        MAIN_URL,
        params={"rcpNo": rcp_no},
        timeout=timeout,
        min_interval_sec=request_interval_sec,
    )
    main_resp.raise_for_status()
    main_html = main_resp.text
    if out_meta is not None:
        out_meta["main_html"] = main_html

    cands = _find_viewdoc_candidates(main_html)
    best = _choose_best_candidate(cands)
    if not best:
        return None, "MAIN_NO_VIEWDOC"

    params = {
        "rcpNo": best["rcpNo"],
        "dcmNo": best["dcmNo"],
        "eleId": best["eleId"],
        "offset": best["offset"],
        "length": best["length"],
        "dtd": best["dtd"],
    }
    vresp = _throttled_session_get(
        session,
        VIEWER_URL,
        params=params,
        timeout=timeout,
        min_interval_sec=request_interval_sec,
    )
    vresp.raise_for_status()
    return vresp.text, "VIEWER_HTML"


def _norm_label(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = re.sub(r"\s+", "", s)
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"^[\-\u2022]+", "", s)
    s = re.sub(r"^\d+\.", "", s)
    return s


_LABEL_TO_FIELD = {
    "회사명": "iscmp_cmpnm",
    "회사명(국적)": "iscmp_cmpnm",
    "처분주식수(주)": "trfdtl_stkcnt",
    "취득주식수(주)": "trfdtl_stkcnt",
    "양수주식수(주)": "trfdtl_stkcnt",
    "처분금액(원)": "trfdtl_trfprc",
    "취득금액(원)": "trfdtl_trfprc",
    "양수금액(원)": "trfdtl_trfprc",
    "자기자본(원)": "trfdtl_ecpt",
    "자기자본대비(%)": "trfdtl_tast",
    "소유주식수(주)": "attrf_owstkcnt",
    "지분비율(%)": "attrf_eqrt",
    "처분목적": "trf_pp",
    "취득목적": "trf_pp",
    "양수목적": "trf_pp",
    "처분예정일자": "trf_prd",
    "취득예정일자": "trf_prd",
    "양수예정일자": "trf_prd",
    "이사회결의일(결정일)": "bddd",
}
_LABEL_TO_FIELD_N = {_norm_label(k): v for k, v in _LABEL_TO_FIELD.items()}


def _to_field(label: str) -> Optional[str]:
    n = _norm_label(label)
    hit = _LABEL_TO_FIELD_N.get(n)
    if hit:
        return hit

    # fuzzy fallback for variants like "처분내역처분금액(원)(A)" / "처분내역처분주식수(주)"
    if ("주식수" in n) and any(k in n for k in ("처분", "양도", "취득", "양수")):
        return "trfdtl_stkcnt"
    if ("금액" in n) and any(k in n for k in ("처분", "양도", "취득", "양수")):
        return "trfdtl_trfprc"
    if ("목적" in n) and any(k in n for k in ("처분", "양도", "취득", "양수")):
        return "trf_pp"
    if ("예정일자" in n) and any(k in n for k in ("처분", "양도", "취득", "양수")):
        return "trf_prd"
    if "이사회결의일" in n or "결정일" in n:
        return "bddd"
    return None


def _extract_fields_from_xml_tags(xml_text: str) -> Dict[str, Any]:
    wanted = {
        "iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
        "attrf_owstkcnt", "attrf_eqrt", "trf_pp", "trf_prd", "dlptn_cmpnm", "bddd",
    }
    out: Dict[str, Any] = {k: pd.NA for k in wanted}

    try:
        root = ET.fromstring(xml_text)
        for el in root.iter():
            tag = el.tag.split("}")[-1].strip().lower()
            txt = (el.text or "").strip()
            if txt and tag in out and pd.isna(out[tag]):
                out[tag] = txt
    except Exception:
        pass

    soup = BeautifulSoup(xml_text, "html.parser")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells if c and c.strip()]
        if len(cells) < 2:
            continue
        for i in range(len(cells) - 1):
            fld = _to_field(cells[i])
            if not fld:
                continue
            val = cells[i + 1].strip()
            if _to_field(val):
                continue
            if pd.isna(out[fld]) and val not in {"", "-", "<NA>"}:
                out[fld] = val

    for c in [
        "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast",
        "trfdtl_ecpt", "attrf_owstkcnt", "attrf_eqrt",
    ]:
        out[c] = _num_or_na_keep_sign(out.get(c))
    if _is_bad_issuer(out.get("iscmp_cmpnm")):
        out["iscmp_cmpnm"] = pd.NA

    combo_text = soup.get_text("\n", strip=True)
    num_fallback = _extract_amount_qty_from_text(combo_text)
    for k in ("trfdtl_trfprc", "trfdtl_stkcnt"):
        if pd.isna(out.get(k)) and (k in num_fallback):
            out[k] = num_fallback[k]
    out["trf_prd"] = _norm_date_any(out.get("trf_prd"))
    out["bddd"] = _norm_date_any(out.get("bddd"))
    return out


def _parse_document_zip_best(raw_zip_bytes: bytes) -> Dict[str, Any]:
    best: Dict[str, Any] = {c: pd.NA for c in LEGACY_OUT_COLS}
    best_score = -1

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip_bytes))
    except zipfile.BadZipFile:
        return best

    for name in zf.namelist():
        low = name.lower()
        if not low.endswith((".xml", ".html", ".htm", ".xhtml", ".txt")):
            continue
        try:
            raw = zf.read(name)
        except Exception:
            continue

        txt = _decode_bytes_auto(raw)
        if low.endswith((".html", ".htm", ".xhtml")):
            cand = _extract_fields_from_html_text(txt)
        else:
            tag_cand = _extract_fields_from_xml_tags(txt)
            html_cand = _extract_fields_from_html_text(txt)
            cand = html_cand.copy()
            for k, v in tag_cand.items():
                if k in cand and not pd.isna(v):
                    cand[k] = v

        score = sum(1 for k in LEGACY_CORE_FIELDS if not pd.isna(cand.get(k)))
        if score > best_score:
            best_score = score
            best = cand

    return best


def _seed_to_dict(seed_row: Optional[Union[Dict[str, Any], pd.Series]]) -> Dict[str, Any]:
    if seed_row is None:
        return {}
    if isinstance(seed_row, pd.Series):
        return seed_row.to_dict()
    if isinstance(seed_row, dict):
        return seed_row
    return {}


def _is_empty_like(x: Any) -> bool:
    if x is None or x is pd.NA:
        return True
    s = str(x).strip()
    return s == "" or s in {"-", "<NA>", "nan", "NaN"}


def _is_bad_issuer(x: Any) -> bool:
    if _is_empty_like(x):
        return True
    s = str(x).strip()
    if not s:
        return True
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
    bad_tokens = {
        "회사명",
        "회사명(국적)",
        "회사명국적",
        "발행회사",
        "발행주식총수",
        "발행주식총수(주)",
        "발행주식총수주",
        "1.발행회사",
        "대표자",
        "대표이사",
        "대표자명",
        "대표이사명",
        "(대표자)",
        "(대표이사)",
        "국적",
        "(국적)",
        "자본금",
        "자본금(원)",
        "자본금원",
        "금액",
        "금액(원)",
        "금액(백만원)",
        "취득금액",
        "취득금액(원)",
        "처분금액",
        "처분금액(원)",
        "양수금액",
        "양수금액(원)",
        "양도금액",
        "양도금액(원)",
        "주식수",
        "주식수(주)",
        "취득주식수",
        "취득주식수(주)",
        "처분주식수",
        "처분주식수(주)",
        "양수주식수",
        "양수주식수(주)",
        "양도주식수",
        "양도주식수(주)",
        "출자사재무제표",
        "발행회사의요약재무상황",
        "철회사유",
        "제출사유",
        "주요내용",
        "관계",
        "(관계)",
        "회사와관계",
        "(회사와관계)",
        ",",
        ".",
        "및",
        "와",
        "과",
        "-",
    }
    if compact in bad_tokens:
        return True
    if re.fullmatch(r"[-,./|&]+", compact):
        return True
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", compact):
        return True
    if compact_plain in {
        "회사명",
        "회사명국적",
        "발행회사",
        "발행주식총수",
        "발행주식총수주",
        "대표자",
        "대표이사",
        "대표자명",
        "대표이사명",
        "국적",
        "성명",
        "관계",
        "회사와관계",
        "자본금",
        "자본금원",
    }:
        return True
    if compact_plain in {"및", "와", "과"}:
        return True
    if any(
        t in compact_plain
        for t in {
            "사명정정",
            "대표조합원변경",
            "기재누락",
            "기재오류",
            "기재정정",
            "정정사항",
            "정정내용",
            "기타투자판단과관련한중요사항",
            "투자판단과관련한중요사항",
            "기재내용추가",
            "양도예정일자",
            "취득예정일자",
            "변경전",
            "변경후",
        }
    ):
        return True
    if (
        "회사명" in compact_plain
        and any(t in compact_plain for t in {"국적", "대표자", "대표이사", "자본금", "발행주식총수", "주요사업"})
    ):
        return True
    if "철회사유" in compact_plain:
        return True
    if compact_plain in {"제출사유", "주요내용"}:
        return True
    if compact_plain in {"대한민국", "한국", "중국", "미국", "일본", "영국", "독일", "프랑스", "체코", "홍콩", "대만"}:
        return True
    if "단위" in compact_plain:
        return True
    if ("금액" in compact_plain) and ("회사명" not in compact_plain):
        return True
    if "주식수" in compact_plain:
        return True
    if ("억원" in compact_plain) or ("백만원" in compact_plain):
        return True
    if any(
        t in compact_plain
        for t in {
            "상기사항",
            "기준환율",
            "정정전",
            "정정후",
        "출자할예정",
        "취득금액은",
        "처분금액은",
        "발행회사의요약재무상황",
        "출자사재무제표",
        "철회사유",
        }
    ):
        return True
    if re.fullmatch(r"(?:취득|처분|양수|양도)?금액(?:원|백만원)?", compact_plain):
        return True
    if re.fullmatch(r"(?:취득|처분|양수|양도)?주식수(?:주)?", compact_plain):
        return True
    if compact_plain.endswith("재무제표"):
        return True
    amount_hits = re.findall(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", s)
    if amount_hits:
        # Avoid taking note/body fragments or representative+capital tuples as issuer.
        if re.match(r"^\s*[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):
            return True
        if re.search(r"\s[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*$", s):
            return True
        if len(amount_hits) >= 2:
            return True
    if core_plain in {"대표자", "대표이사", "대표자명", "대표이사명", "국적", "관계", "회사와관계"}:
        return True
    if not re.search(r"[A-Za-z가-힣]", compact_plain):
        return True
    if re.fullmatch(r"\((대표자|대표이사|국적|관계|회사와관계)\)", compact):
        return True
    if re.fullmatch(r"(대표자|대표이사)(명)?", compact_plain):
        return True
    # role-tail noise such as: "주식회사 ○○ 대 표 이 사"
    if any(t in compact_plain for t in {"대표이사", "대표자"}) and (len(compact_plain) > 8):
        return True
    return False


def _extract_amount_qty_from_text(raw_text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not raw_text:
        return out

    txt = re.sub(r"\s+", " ", str(raw_text))

    amount_patterns = [
        r"(?:처분|양도|취득|양수)(?:내역)?\s*(?:처분|양도|취득|양수)?금액(?:\s*\(?원\)?(?:\s*\([A-Z]\))?)?\s*[:：]?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        r"거래금액\s*\(?원\)?\s*[:：]?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
    ]
    qty_patterns = [
        r"(?:처분|양도|취득|양수)(?:내역)?\s*(?:처분|양도|취득|양수)?주식수\s*\(?주\)?\s*[:：]?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
    ]

    for pat in amount_patterns:
        m = re.search(pat, txt, flags=re.I)
        if m:
            v = _num_or_na_keep_sign(m.group(1))
            if not pd.isna(v):
                out["trfdtl_trfprc"] = v
                break

    for pat in qty_patterns:
        m = re.search(pat, txt, flags=re.I)
        if m:
            v = _num_or_na_keep_sign(m.group(1))
            if not pd.isna(v):
                out["trfdtl_stkcnt"] = v
                break

    return out


def _extract_submitter_from_title(viewer_html: str) -> Any:
    if not viewer_html:
        return pd.NA
    soup = BeautifulSoup(viewer_html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not title:
        return pd.NA
    tok = re.split(r"[/|]", title, maxsplit=1)[0].strip()
    return tok if tok else pd.NA


def _extract_issuer_from_viewer_table(markup: str) -> Any:
    if not markup:
        return pd.NA
    soup = BeautifulSoup(markup, "html.parser")
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        texts = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in cells]
        for i, t in enumerate(texts[:-1]):
            key = re.sub(r"\s+", "", t)
            if key in {"회사명(국적)", "회사명"}:
                v = texts[i + 1].strip()
                if not _is_bad_issuer(v):
                    return v

    plain = soup.get_text("\n", strip=True)
    patterns = [
        r"회사명\(국적\)\s*([^\n]{2,120}?)\s*(?:대표이사|자본금\(원\)|자본금)",
        r"회사명\s*([^\n]{2,120}?)\s*(?:국적|대표자|대표이사|자본금\(원\)|자본금)",
        r"(?:\*?\s*)?(?:회사명(?:\(국적\))?|발행회사(?:\(회사명\))?)\s*[:：]\s*([^\n]{2,220}?)(?=\s*(?:\*?\s*)?(?:국적|대표자|대표이사|자본금(?:\(원\))?|발행주식총수(?:\(주\))?|주요사업)\s*[:：]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, plain, flags=re.I | re.S)
        if not m:
            continue
        v = _normalize_company_token(m.group(1))
        if not _is_bad_issuer(v):
            return v
    return pd.NA


def _extract_issuer_from_text(raw_text: str) -> Any:
    if not raw_text:
        return pd.NA
    txt = re.sub(r"\s+", " ", str(raw_text))
    patterns = [
        r"회사명\(국적\)\s*([가-힣A-Za-z0-9\(\)\.\-·&\s]{2,120}?)\s*(?:대표이사|대표자|자본금\(원\)|자본금)",
        r"회사명\s*([가-힣A-Za-z0-9\(\)\.\-·&㈜\s]{2,120}?)\s*(?:국적|대표자|대표이사|자본금\(원\)|자본금)",
        r"(?:\*?\s*)?(?:회사명(?:\(국적\))?|발행회사(?:\(회사명\))?)\s*[:：]\s*([가-힣A-Za-z0-9\(\)\.\-·&㈜,\s]{2,220}?)(?=\s*(?:\*?\s*)?(?:국적|대표자|대표이사|자본금(?:\(원\))?|발행주식총수(?:\(주\))?|주요사업)\s*[:：]|$)",
    ]
    for pat in patterns:
        m = re.search(pat, txt, flags=re.I | re.S)
        if not m:
            continue
        v = _normalize_company_token(m.group(1))
        if not _is_bad_issuer(v):
            return v

    # narrative fallback for correction reports:
    # "회사가 소유하고 있는 <회사명> 주식 ..."
    narrative_patterns = [
        r"소유하고\s*있는\s*([A-Za-z0-9가-힣&\.\-,'\(\)\s]{2,180}?)\s*\(이하",
        r"소유하고\s*있는\s*([A-Za-z0-9가-힣&\.\-,'\(\)\s]{2,180}?)\s*주식\s*\d",
        r"(\(주\)\s*[A-Za-z0-9가-힣&\.\-]{2,180}(?:회사|리츠|투자회사|위탁관리부동산투자회사)?)\s*가\s*유상증자로\s*발행할\s*주식",
        r"(주식회사\s*[A-Za-z0-9가-힣&\.\-\s]{2,180})\s*가\s*유상증자로\s*발행할\s*주식",
        r"(\(주\)\s*[A-Za-z0-9가-힣&\.\-]{2,180}(?:회사|리츠|투자회사|위탁관리부동산투자회사)?)\s*에\s*대한\s*주식(?:취득|양수|처분|양도)",
        r"(주식회사\s*[A-Za-z0-9가-힣&\.\-\s]{2,180})\s*에\s*대한\s*주식(?:취득|양수|처분|양도)",
    ]
    for pat in narrative_patterns:
        m = re.search(pat, txt, flags=re.I | re.S)
        if not m:
            continue
        v = re.sub(r"\s+", " ", m.group(1)).strip(" \t:-,")
        if not _is_bad_issuer(v):
            return v
    return pd.NA


def _is_acquire_report_name(report_nm: Any) -> bool:
    s = str(report_nm or "")
    return ("취득결정" in s) or ("양수결정" in s)


def _normalize_company_token(token: Any) -> Optional[str]:
    if token is None:
        return None
    s = html.unescape(str(token))
    s = s.replace("&cr", " ").replace("&#13;", " ").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip(" \t,;:|/-")
    if not s:
        return None
    s = re.sub(r"^[\*\-\u2022·]+\s*", "", s)

    # Handle field blobs such as:
    # "*회사명: 주식회사 XXX *국적: 대한민국 *대표자: ..."
    m_field = re.search(
        r"(?:회사명(?:\(국적\))?|발행회사(?:\(회사명\))?)\s*[:：]\s*(?P<nm>.+?)(?=\s*(?:\*?\s*)?(?:국적|대표자|대표이사|자본금(?:\(원\))?|발행주식총수(?:\(주\))?|주요사업)\s*[:：]|$)",
        s,
        flags=re.I,
    )
    if m_field:
        s = m_field.group("nm").strip()

    s = re.sub(r"^\(?\s*(국적|대표자|대표이사|회사명(?:\(국적\))?)\s*\)?\s*", "", s)
    s = re.sub(r"^\(?\s*(발행회사(?:\(회사명\))?)\s*\)?\s*[:：]?\s*", "", s)
    s = re.sub(
        r"\s*(?:\*?\s*)?(?:국적|대표자|대표이사|자본금(?:\(원\))?|발행주식총수(?:\(주\))?|주요사업)\s*[:：].*$",
        "",
        s,
    )
    s = re.sub(r"\s*(?:대\s*표\s*이\s*사|대\s*표\s*자)\s*$", "", s)
    s = re.sub(r"^\(?\s*가\s*칭\s*\)?\s*", "", s)
    s = re.sub(r"\s*\(?\s*가\s*칭\s*\)?\s*$", "", s)
    s = re.sub(r"^[\(\[]?\d+[\)\.]?\s*", "", s)
    s = re.sub(r"\s*의?\s*총\s*\d+\s*개사.*$", "", s)
    s = re.sub(r"\s*[\(\[\{]+\s*$", "", s)
    if s.count("(") > s.count(")"):
        while s.endswith("("):
            s = s[:-1].rstrip()
    s = re.sub(r"\s+", " ", s).strip(" \t,;:|/-")
    if not s or _is_bad_issuer(s):
        return None
    return s


def _has_company_hint(value: Any) -> bool:
    s = str(value or "")
    return bool(
        re.search(
            r"(주식회사|\(주\)|㈜|유한회사|유한공사|조합|Ltd\.?|Inc\.?|LLC|Corp\.?|Co\.?|S\.r\.o|S\.A\.|B\.V\.|PLC)",
            s,
            flags=re.I,
        )
    )


def _issuer_quality_score(value: Any) -> int:
    if _is_bad_issuer(value):
        return -100
    s = str(value or "").strip()
    plain = re.sub(r"[^0-9A-Za-z가-힣]", "", s)
    score = 0
    if _has_company_hint(s):
        score += 4
    if re.search(r"[A-Za-z가-힣]", plain):
        score += 1
    if len(plain) >= 5:
        score += 1
    if re.search(r"(회사분할|분할등기|정정전|정정후|단위|금액|주식수|재무제표)", s):
        score -= 4
    if re.fullmatch(r"[가-힣]{2,4}", plain):
        score -= 1
    return score


def _maybe_choose_better_issuer(current: Any, candidate: Any) -> Any:
    if _is_bad_issuer(candidate):
        return current
    if _is_bad_issuer(current):
        return candidate
    cur_score = _issuer_quality_score(current)
    cand_score = _issuer_quality_score(candidate)
    if cand_score > (cur_score + 1):
        return candidate
    if _has_company_hint(candidate) and (not _has_company_hint(current)):
        return candidate
    return current


def _issuer_dedup_key(value: Any) -> str:
    nm = _normalize_company_token(value)
    if not nm:
        return ""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", nm).lower()


def _extract_multi_issuer_names(viewer_html: str, doc_raw_text: str) -> List[str]:
    candidates: List[str] = []
    source_texts: List[Tuple[str, str]] = []
    if viewer_html:
        source_texts.append((_markup_to_text(viewer_html), "viewer"))
    if doc_raw_text:
        source_texts.append((str(doc_raw_text), "doc"))

    for txt, src_kind in source_texts:
        if not txt:
            continue
        local_candidates: List[str] = []
        body = ""
        m_sec = re.search(
            r"1\.\s*발행회사(?P<section>.*?)(?:\n\s*2\.\s*(?:처분|양도|취득|양수)내역|$)",
            txt,
            flags=re.S,
        )
        section = m_sec.group("section") if m_sec else txt
        m_body = re.search(
            r"(?:회사명(?:\(국적\))?)\s*(?P<body>.*?)(?:\n\s*(?:-\s*)?(?:\(?국적\)?|\(?대표자\)?|\(?대표이사\)?|\(?자본금(?:\(원\))?\)?|회사와\s*관계|발행주식총수)|$)",
            section,
            flags=re.S,
        )
        if m_body:
            body = m_body.group("body")
        if not body:
            continue

        body = re.sub(r"\s+", " ", body).strip()
        multi_hint = bool(re.search(r"총\s*\d+\s*개사", body))
        body = re.sub(r"\s*의?\s*총\s*\d+\s*개사.*$", "", body)

        for name in re.findall(
            r"(?:주식회사|㈜|\(주\))\s*[0-9A-Za-z가-힣·&\-\.\s]{1,120}(?:\([^)]{1,120}\))?",
            body,
        ):
            if re.search(r"대\s*표\s*이\s*사|대\s*표\s*자", str(name)):
                continue
            nm = _normalize_company_token(name)
            if not nm:
                continue
            if re.search(r"(회사분할|분할등기|취득금액|처분금액|주\d+\))", nm):
                continue
            local_candidates.append(nm)

        if not local_candidates and multi_hint:
            for part in re.split(r"\s*,\s*|\s+및\s+|\s+와\s+|\s+과\s+", body):
                nm = _normalize_company_token(part)
                if nm:
                    local_candidates.append(nm)

        # document raw text는 노이즈가 많아 '총 N개사' 근거가 있을 때만 다중 회사로 채택한다.
        if src_kind == "doc" and not multi_hint:
            continue
        candidates.extend(local_candidates)

    out: List[str] = []
    seen: set[str] = set()
    for raw in candidates:
        nm = _normalize_company_token(raw)
        if not nm:
            continue
        key = re.sub(r"\s+", "", nm)
        if key in seen:
            continue
        seen.add(key)
        out.append(nm)
    return out if len(out) >= 2 else []


def _company_match_keys(name: Any) -> set[str]:
    s = _normalize_company_token(name)
    if not s:
        return set()
    variants: List[str] = [s]
    variants.append(re.sub(r"\([^)]*\)", " ", s))
    variants.append(re.sub(r"^(주식회사|㈜|\(주\))\s*", "", s))
    variants.append(re.sub(r"\s*(주식회사|㈜|\(주\))$", "", s))

    keys: set[str] = set()
    for one in variants:
        txt = re.sub(r"\s+", " ", str(one)).strip()
        if not txt:
            continue
        k = re.sub(r"[^0-9A-Za-z가-힣]", "", txt).lower()
        if k:
            keys.add(k)
    return keys


def _best_match_plan_item_index(
    issuer_name: Any,
    items: List[Dict[str, Any]],
    used_idx: set[int],
) -> Optional[int]:
    issuer_keys = _company_match_keys(issuer_name)
    if not issuer_keys:
        return None

    best_idx: Optional[int] = None
    best_score = -1
    for idx, it in enumerate(items):
        if idx in used_idx:
            continue
        item_keys = _company_match_keys(it.get("name"))
        if not item_keys:
            continue
        score = -1
        if issuer_keys & item_keys:
            score = 100
        else:
            for ik in issuer_keys:
                for jk in item_keys:
                    if len(ik) < 3 or len(jk) < 3:
                        continue
                    if ik in jk or jk in ik:
                        score = max(score, min(len(ik), len(jk)))
        if score > best_score:
            best_score = score
            best_idx = idx
    if best_score < 3:
        return None
    return best_idx


def _apply_plan_items_to_rows(
    *,
    rows: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    sign: int,
) -> set[int]:
    used_idx: set[int] = set()
    if not rows or not items:
        return used_idx

    for row in rows:
        idx = _best_match_plan_item_index(row.get("iscmp_cmpnm"), items, used_idx)
        if idx is None:
            continue
        it = items[idx]
        row["trfdtl_stkcnt"] = _signed_num(it.get("shares", pd.NA), sign=sign)
        row["trfdtl_trfprc"] = _signed_num(it.get("amt", pd.NA), sign=sign)
        row["source"] = (str(row.get("source", "INIT")) + "+NOTE_ITEM_MAP").strip("+")
        used_idx.add(idx)

    # 이름 매칭이 어렵지만 개수는 동일한 다중행 케이스는 순서로 보정한다.
    if len(used_idx) == 0 and len(rows) == len(items) and len(rows) > 1:
        for idx, row in enumerate(rows):
            it = items[idx]
            row["trfdtl_stkcnt"] = _signed_num(it.get("shares", pd.NA), sign=sign)
            row["trfdtl_trfprc"] = _signed_num(it.get("amt", pd.NA), sign=sign)
            row["source"] = (str(row.get("source", "INIT")) + "+NOTE_ITEM_SEQ_MAP").strip("+")
            used_idx.add(idx)

    return used_idx


def _expand_base_rows_for_multi_issuers(
    *,
    base_row: Dict[str, Any],
    viewer_html: str,
    doc_raw_text: str,
) -> List[Dict[str, Any]]:
    names = _extract_multi_issuer_names(viewer_html=viewer_html, doc_raw_text=doc_raw_text)
    if not names:
        return [base_row]

    corp_keys = _company_match_keys(base_row.get("corp_name"))
    base_issuer_keys = _company_match_keys(base_row.get("iscmp_cmpnm"))
    filtered = []
    for nm in names:
        nm_keys = _company_match_keys(nm)
        if corp_keys and (nm_keys & corp_keys):
            continue
        filtered.append(nm)
    if filtered:
        names = filtered

    submitter_keys = _company_match_keys(_extract_submitter_from_title(viewer_html))
    filtered_by_submitter = []
    for nm in names:
        nm_keys = _company_match_keys(nm)
        if submitter_keys and (nm_keys & submitter_keys):
            continue
        filtered_by_submitter.append(nm)
    if filtered_by_submitter:
        names = filtered_by_submitter

    if len(names) < 2:
        # If only one clean issuer is left and current issuer is bad/self-like, override base row.
        if len(names) == 1 and (
            _is_bad_issuer(base_row.get("iscmp_cmpnm"))
            or (corp_keys and base_issuer_keys and (base_issuer_keys & corp_keys))
        ):
            r = base_row.copy()
            r["iscmp_cmpnm"] = names[0]
            r["source"] = (str(base_row.get("source", "INIT")) + "+MULTI_ISSUER_SINGLE").strip("+")
            return [r]
        return [base_row]

    out: List[Dict[str, Any]] = []
    for nm in names:
        r = base_row.copy()
        r["iscmp_cmpnm"] = nm
        # 기본값은 본문의 수치를 유지하고, 이후 NOTE_ITEM 매핑이 있으면 회사별 값으로 덮어쓴다.
        r["source"] = (str(base_row.get("source", "INIT")) + "+MULTI_ISSUER").strip("+")
        out.append(r)
    return out


def _parse_plan_items(note_text: str) -> List[Dict[str, Any]]:
    if not note_text:
        return []
    t = note_text.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()

    patterns = [
        (
            re.compile(
            r"""(?mix)
            ^\s*(\d+)\.\s*
            ([^\n\d]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:(?:취득|양수|양도|처분)(?:예정)?금액|(?:취득|양수|양도|처분)\s*금액|금액)\s*
            (\d[\d,]*)\s*원\s*$
            """
            ),
            "shares_first",
        ),
        (
            re.compile(
            r"""(?ix)
            (\d+)\.\s*
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:(?:취득|양수|양도|처분)(?:예정)?금액|(?:취득|양수|양도|처분)\s*금액|금액)\s*
            (\d[\d,]*)\s*원
            """
            ),
            "shares_first",
        ),
        (
            re.compile(
            r"""(?ix)
            (\d+)\.\s*
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{1,140}?)\s*
            (?:[:\-]\s*|\s+)
            (?:(?:주식수|수량)\s*)?(\d[\d,]*)\s*주(?:식)?\s*
            (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
            (\d[\d,]*)\s*원
            """
            ),
            "shares_first",
        ),
        (
            re.compile(
            r"""(?ix)
            (\d+)\.\s*
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{1,140}?)\s*
            (?:[:\-]\s*|\s+)
            (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
            (\d[\d,]*)\s*원\s*
            (?:(?:주식수|수량)\s*)?(\d[\d,]*)\s*주(?:식)?
            """
            ),
            "amount_first",
        ),
        (
            re.compile(
            r"""(?ix)
            (\d+)\.\s*
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{1,160}?)\s*
            (?:[:\-]\s*|\s+)
            (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
            (\d[\d,]*)\s*원
            """
            ),
            "amount_only",
        ),
    ]

    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, Optional[int], Optional[int]]] = set()
    for p, order in patterns:
        for m in p.findall(t):
            if order == "amount_only":
                seq, name, v3 = m
                sh: Optional[int] = None
                am: Optional[int] = int(str(v3).replace(",", ""))
            else:
                seq, name, v3, v4 = m
                g3 = int(str(v3).replace(",", ""))
                g4 = int(str(v4).replace(",", ""))
                if order == "amount_first":
                    am, sh = g3, g4
                else:
                    sh, am = g3, g4
            nm = re.sub(r"\s+", " ", name).strip(" -:\t\r\n")
            nm = re.sub(r"^[\(\[]?\d+[\)\.]?\s*", "", nm)
            key = (nm, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({"line_no": int(seq), "name": nm, "shares": sh, "amt": am})

    # 번호 없는 문장형 라인 fallback
    line_patterns = [
        (
            re.compile(
                r"""(?ix)
                ^
                ([가-힣A-Za-z0-9&\.\-\(\)·\s]{2,140}?)\s+
                (?:(?:주식수|수량)\s*)?(\d[\d,]*)\s*주(?:식)?\s*
                (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
                (\d[\d,]*)\s*원
                $
                """
            ),
            "shares_first",
        ),
        (
            re.compile(
                r"""(?ix)
                ^
                ([가-힣A-Za-z0-9&\.\-\(\)·\s]{2,140}?)\s+
                (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
                (\d[\d,]*)\s*원\s*
                (?:(?:주식수|수량)\s*)?(\d[\d,]*)\s*주(?:식)?
                $
                """
            ),
            "amount_first",
        ),
        (
            re.compile(
                r"""(?ix)
                ^
                ([가-힣A-Za-z0-9&\.\-\(\)·\s]{2,160}?)\s+
                (?:(?:취득|양수|양도|처분)?(?:예정)?금액|금액)\s*
                (\d[\d,]*)\s*원
                $
                """
            ),
            "amount_only",
        ),
    ]
    next_line_no = (max([x["line_no"] for x in out]) + 1) if out else 1
    for ln in t.splitlines():
        line = re.sub(r"\s+", " ", str(ln)).strip(" \t")
        if not line:
            continue
        for p, order in line_patterns:
            m = p.search(line)
            if not m:
                continue
            name = re.sub(r"\s+", " ", m.group(1)).strip(" -:\t\r\n")
            name = re.sub(r"^[\(\[]?\d+[\)\.]?\s*", "", name)
            g2 = int(str(m.group(2)).replace(",", ""))
            if order == "amount_only":
                am = g2
                sh = None
            elif order == "amount_first":
                g3 = int(str(m.group(3)).replace(",", ""))
                am, sh = g2, g3
            else:
                g3 = int(str(m.group(3)).replace(",", ""))
                sh, am = g2, g3
            key = (name, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({"line_no": next_line_no, "name": name, "shares": sh, "amt": am})
            next_line_no += 1
            break
    out.sort(key=lambda x: x["line_no"])
    return out


def _pick_best_note_from_sources(
    viewer_html: str,
    parsed: Dict[str, Any],
    doc_raw_text: str,
) -> str:
    candidates: List[str] = []
    if viewer_html:
        vtxt = _markup_to_text(viewer_html)
        if vtxt:
            candidates.append(vtxt)

    for k, v in (parsed or {}).items():
        if isinstance(v, str) and v.strip():
            lk = str(k).lower()
            if "note" in lk or "etc" in lk or "참고" in v or "기타 투자판단" in v:
                candidates.append(v.strip())

    if doc_raw_text:
        candidates.append(doc_raw_text)
    if not candidates:
        return ""

    best = ""
    best_score = -1
    for c in candidates:
        sec = _extract_section9_text(c)
        n_items = len(re.findall(r"\d+\.\s*.+?\d[\d,]*\s*주.*?\d[\d,]*\s*원", sec, flags=re.S))
        n_amount_only = len(
            re.findall(
                r"\d+\.\s*[^\n]{1,180}?(?:취득|양수|양도|처분)?(?:예정)?금액\s*\d[\d,]*\s*원",
                sec,
                flags=re.S,
            )
        )
        score = (n_items * 10) + (n_amount_only * 7)
        for kw in ("기타 투자판단", "취득금액", "한도내 신규 투자", "총", "주", "원"):
            if kw in sec:
                score += 2
        score += min(len(sec) // 120, 20)
        if score > best_score:
            best_score = score
            best = sec
    return best


def _decode_bytes_best(data: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _markup_to_text(markup: str) -> str:
    if not markup:
        return ""
    soup = BeautifulSoup(markup, "html.parser")
    for bad in soup(["script", "style"]):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    text = text.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    txt = "\n".join(lines).strip()
    txt = re.sub(r"(?<!\n)\s(?=\d+\.\s)", "\n", txt)
    return re.sub(r"\n{2,}", "\n", txt).strip()


def _extract_texts_from_document_zip(zip_bytes: bytes) -> list[str]:
    texts: list[str] = []
    if not zip_bytes:
        return texts
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = sorted(
                zf.namelist(),
                key=lambda n: (
                    0 if n.lower().endswith(".xml") else
                    1 if n.lower().endswith((".html", ".htm")) else
                    2 if n.lower().endswith(".txt") else
                    9
                ),
            )
            for name in names:
                low = name.lower()
                if not low.endswith((".xml", ".html", ".htm", ".txt")):
                    continue
                try:
                    texts.append(_decode_bytes_best(zf.read(name)))
                except Exception:
                    continue
    except Exception:
        return texts
    return texts


def _extract_section9_text(raw_text: str) -> str:
    if not raw_text:
        return ""

    start_patterns = [
        r"9\.\s*기타\s*투자판단에\s*참고할\s*사항",
        r"9\.\s*기타\s*투자판단\s*참고사항",
        r"기타\s*투자판단에\s*참고할\s*사항",
    ]

    m = None
    for p in start_patterns:
        m = re.search(p, raw_text, flags=re.I | re.S)
        if m:
            break

    body = raw_text[m.end():].strip() if m else raw_text
    m10 = re.search(r"\n\s*10\.\s*", body)
    if m10:
        body = body[: m10.start()].strip()

    body = re.sub(r"(?<!\n)\s(?=\d+\.\s)", "\n", body)
    return re.sub(r"\n{2,}", "\n", body).strip()


def _parse_note_plan_items(note_text: str) -> list[dict[str, Any]]:
    if not note_text:
        return []

    text = note_text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()

    patterns = [
        re.compile(
            r"""(?mix)
            ^\s*(\d+)\.\s*
            ([^\n\d]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:취득(?:예정)?금액|취득\s*금액|금액)\s*
            (\d[\d,]*)\s*원\s*$
            """
        ),
        re.compile(
            r"""(?ix)
            (\d+)\.\s*
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:취득(?:예정)?금액|취득\s*금액|금액)\s*
            (\d[\d,]*)\s*원
            """
        ),
    ]

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str, int, int]] = set()

    for pat in patterns:
        for seq, name, shares, amt in pat.findall(text):
            nm = re.sub(r"\s+", " ", name).strip(" -:\t\r\n")
            sh = int(str(shares).replace(",", ""))
            am = int(str(amt).replace(",", ""))
            key = (int(seq), nm, sh, am)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "line_no": int(seq),
                    "name": nm,
                    "shares": sh,
                    "amount": am,
                }
            )

    rows.sort(key=lambda x: x["line_no"])
    return rows


def _throttled_session_get(
    session: requests.Session,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 30,
    min_interval_sec: float = 0.0,
) -> requests.Response:
    configured = getattr(session, "_dart_request_interval_sec", None)
    interval = configured if configured is not None else min_interval_sec
    try:
        interval_sec = max(float(interval), 0.0)
    except Exception:
        interval_sec = 0.0

    if interval_sec > 0:
        now = time.monotonic()
        last = getattr(session, "_dart_last_request_monotonic", 0.0) or 0.0
        wait_sec = interval_sec - (now - float(last))
        if wait_sec > 0:
            time.sleep(wait_sec)
        setattr(session, "_dart_last_request_monotonic", time.monotonic())

    return session.get(url, params=params, timeout=timeout)


def call_json(
    api_key: str,
    url: str,
    params: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    request_interval_sec: float = 0.0,
) -> dict:
    if params is None:
        params = {}

    payload = {"crtfc_key": api_key, **params}
    sess = session or requests.Session()

    for attempt in range(max_retries):
        r = _throttled_session_get(
            sess,
            url,
            params=payload,
            timeout=timeout,
            min_interval_sec=request_interval_sec,
        )
        r.raise_for_status()
        js = r.json()
        st = js.get("status", "")
        if st in ("000", "013"):
            return js
        if st == "020":
            time.sleep(base_sleep * (2 ** attempt))
            continue
        raise RuntimeError(
            f"DART API error: status={st}, message={js.get('message')}, "
            f"url={url}, params={params}"
        )
    raise RuntimeError(f"Request retries exhausted: {url}")


def list_all_pages(
    api_key: str,
    params: dict,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    request_interval_sec: float = 0.0,
) -> List[dict]:
    sess = session or requests.Session()
    first = call_json(
        api_key=api_key,
        url=BASE_LIST_URL,
        params={**params, "page_no": "1"},
        session=sess,
        timeout=timeout,
        max_retries=max_retries,
        base_sleep=base_sleep,
        request_interval_sec=request_interval_sec,
    )
    if first.get("status") == "013":
        return []

    rows = list(first.get("list", []))
    total_page = int(first.get("total_page", 1) or 1)

    for p in range(2, total_page + 1):
        js = call_json(
            api_key=api_key,
            url=BASE_LIST_URL,
            params={**params, "page_no": str(p)},
            session=sess,
            timeout=timeout,
            max_retries=max_retries,
            base_sleep=base_sleep,
            request_interval_sec=request_interval_sec,
        )
        st = js.get("status", "")
        if st == "000":
            rows.extend(js.get("list", []))
        elif st == "013":
            break
        else:
            break

    return rows


def fetch_transfer_list(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_tys: Tuple[str, ...] = ("B", "I"),
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    request_interval_sec: float = 0.0,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    corp_code = str(corp_code).zfill(8)
    bgn_de = _norm_yyyymmdd(bgn_de)
    end_de = _norm_yyyymmdd(end_de)
    sess = session or requests.Session()
    chunks: List[pd.DataFrame] = []

    for ty in pblntf_tys:
        params = {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": ty,
            "sort": "date",
            "sort_mth": "desc",
            "page_count": "100",
            "last_reprt_at": "N",
        }
        rows = list_all_pages(
            api_key=api_key,
            params=params,
            session=sess,
            timeout=timeout,
            max_retries=max_retries,
            base_sleep=base_sleep,
            request_interval_sec=request_interval_sec,
        )
        if not rows:
            continue

        one = pd.DataFrame(rows)
        if "report_nm" in one.columns:
            one = one[
                one["report_nm"].astype(str).str.contains(
                    TRANSFER_TITLE_REGEX, regex=True, na=False
                )
            ].copy()
        if one.empty:
            continue

        one["pblntf_ty"] = ty
        if "rcept_no" in one.columns:
            one["viewer_url"] = _viewer_url_from_rcept_no(one["rcept_no"])

        one = _ensure_cols(
            one,
            [
                "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
                "report_nm", "flr_nm", "rm", "pblntf_ty", "viewer_url",
            ],
        )
        one["corp_code"] = one["corp_code"].astype(str).str.zfill(8)
        one["source"] = one["pblntf_ty"].astype(str).map(lambda x: f"LIST_{x}")
        chunks.append(one)

    if not chunks:
        return pd.DataFrame(
            columns=[
                "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
                "report_nm", "flr_nm", "rm", "pblntf_ty", "viewer_url", "source",
            ]
        )

    df = pd.concat(chunks, ignore_index=True)
    df = df.drop_duplicates(subset=["rcept_no"], keep="first")
    df = df.sort_values("rcept_no", ascending=False).reset_index(drop=True)
    return df


def fetch_transfer_list_standalone(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_tys: Tuple[str, ...] = ("B", "I"),
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    request_interval_sec: float = 0.0,
) -> pd.DataFrame:
    return fetch_transfer_list(
        api_key=api_key,
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        pblntf_tys=pblntf_tys,
        timeout=timeout,
        max_retries=max_retries,
        base_sleep=base_sleep,
        request_interval_sec=request_interval_sec,
    )


def extract_transfer_decision_from_viewer_url(
    viewer_url: str,
    api_key: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = True,
    seed_row: Optional[Union[Dict[str, Any], pd.Series]] = None,
    session: Optional[requests.Session] = None,
    request_interval_sec: float = 0.0,
    out_meta: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    rcp_no = _extract_rcp_no(viewer_url)
    seed = _seed_to_dict(seed_row)

    row: Dict[str, Any] = {c: pd.NA for c in LEGACY_OUT_COLS}
    row["rcept_no"] = rcp_no
    row["rcept_dt"] = _norm_date_any(rcp_no[:8])
    row["viewer_url"] = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
    row["report_nm"] = "타법인주식및출자증권처분결정"
    row["source"] = "INIT"

    for k in ("corp_cls", "corp_code", "corp_name", "flr_nm", "pblntf_ty", "report_nm"):
        v = seed.get(k)
        if not _is_empty_like(v):
            row[k] = v

    viewer_html = ""
    parsed: Dict[str, Any] = {}
    doc_parsed: Dict[str, Any] = {}
    doc_raw_text = ""
    doc_zip_bytes = b""

    sess = session or requests.Session()
    if session is None:
        sess.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })

    try:
        if verbose:
            print(f"[1] viewer HTML 파싱 시작: rcp_no={rcp_no}")
        viewer_meta: Dict[str, Any] = {}
        viewer_html, src = _fetch_viewer_html_by_rcpno(
            rcp_no,
            sess,
            timeout=timeout,
            request_interval_sec=request_interval_sec,
            out_meta=viewer_meta,
        )
        if out_meta is not None:
            out_meta.update(viewer_meta)
        if viewer_html:
            parsed = _extract_fields_from_html_text(viewer_html) or {}
            skip_identity = {"corp_name", "corp_code", "corp_cls", "flr_nm", "pblntf_ty", "rcept_no", "rcept_dt"}
            for k, v in parsed.items():
                if k not in row or k in skip_identity:
                    continue
                if (v is not None) and (not pd.isna(v)) and pd.isna(row[k]):
                    row[k] = v
            row["source"] = src

            if _is_empty_like(row.get("corp_name")):
                title_submitter = _extract_submitter_from_title(viewer_html)
                if not _is_empty_like(title_submitter):
                    row["corp_name"] = title_submitter

            # Priority 1: issuer adjacent to label in viewer table/text.
            issuer_tbl = _extract_issuer_from_viewer_table(viewer_html)
            if not _is_bad_issuer(issuer_tbl):
                row["iscmp_cmpnm"] = issuer_tbl
            else:
                row["iscmp_cmpnm"] = _maybe_choose_better_issuer(row.get("iscmp_cmpnm"), issuer_tbl)

        if verbose:
            got = sum(1 for k in LEGACY_CORE_FIELDS if not pd.isna(row.get(k)))
            print(f"[1] viewer core filled = {got}/{len(LEGACY_CORE_FIELDS)}")
    except Exception as e:
        if verbose:
            print(f"[WARN] viewer HTML 파싱 실패: {e}")

    if api_key:
        try:
            if verbose:
                print("[2] document.zip 다운로드 시작")
            resp = _throttled_session_get(
                sess,
                DOCUMENT_URL,
                params={"crtfc_key": api_key, "rcept_no": rcp_no},
                timeout=timeout,
                min_interval_sec=request_interval_sec,
            )
            resp.raise_for_status()
            doc_zip_bytes = resp.content
            if verbose:
                print(f"[2] document.zip bytes={len(doc_zip_bytes):,}")
        except Exception as e:
            if verbose:
                print(f"[WARN] document.zip 다운로드 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_FETCH_FAIL").strip("+")

    if doc_zip_bytes:
        try:
            if verbose:
                print("[3] zip 내부 xml/html/txt -> raw text 변환")
            texts = _extract_texts_from_document_zip(doc_zip_bytes)
            texts = sorted(texts, key=len, reverse=True)
            plain_chunks = [_markup_to_text(t) for t in texts[:5] if t and t.strip()]
            doc_raw_text = "\n".join([t for t in plain_chunks if t]).strip()
            if verbose:
                print(f"[3] doc_raw_text len={len(doc_raw_text):,}")
        except Exception as e:
            if verbose:
                print(f"[WARN] raw text 변환 실패: {e}")

    note_text = _pick_best_note_from_sources(
        viewer_html=viewer_html,
        parsed=parsed,
        doc_raw_text=doc_raw_text,
    )
    items = _parse_plan_items(note_text)
    if verbose:
        print(f"[4] note_text len={len(note_text)}, parsed items={len(items)}")

    need_doc = any(pd.isna(row.get(k)) for k in LEGACY_CORE_FIELDS)
    if need_doc and doc_zip_bytes:
        try:
            if verbose:
                print("[5] document 구조화 파싱(마지막 단계)")
            doc_parsed = _parse_document_zip_best(doc_zip_bytes) or {}
            skip_identity = {"corp_name", "corp_code", "corp_cls", "flr_nm", "pblntf_ty", "rcept_no", "rcept_dt"}
            for k, v in doc_parsed.items():
                if k not in row or k in skip_identity:
                    continue
                if (v is not None) and (not pd.isna(v)) and pd.isna(row[k]):
                    row[k] = v
            row["source"] = (str(row["source"]) + "+DOC").strip("+")

            # Keep label-adjacent extraction as primary decision rule.
            issuer = _extract_issuer_from_viewer_table(viewer_html)
            if not _is_bad_issuer(issuer):
                row["iscmp_cmpnm"] = issuer
            elif doc_raw_text:
                issuer = _extract_issuer_from_text(doc_raw_text)
                if not _is_bad_issuer(issuer):
                    row["iscmp_cmpnm"] = issuer
                else:
                    row["iscmp_cmpnm"] = _maybe_choose_better_issuer(row.get("iscmp_cmpnm"), issuer)
        except Exception as e:
            if verbose:
                print(f"[WARN] document 구조화 파싱 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_PARSE_FAIL").strip("+")

    issuer = _extract_issuer_from_text(doc_raw_text)
    row["iscmp_cmpnm"] = _maybe_choose_better_issuer(row.get("iscmp_cmpnm"), issuer)
    combo_text = _markup_to_text(viewer_html)
    if doc_raw_text:
        combo_text = f"{combo_text}\n{doc_raw_text}"
    issuer = _extract_issuer_from_text(combo_text)
    row["iscmp_cmpnm"] = _maybe_choose_better_issuer(row.get("iscmp_cmpnm"), issuer)
    if _is_bad_issuer(row.get("iscmp_cmpnm")):
        row["iscmp_cmpnm"] = pd.NA

    # Fallback: when core numeric fields are missing, recover from flattened text labels.
    combo_text = _markup_to_text(viewer_html)
    if doc_raw_text:
        combo_text = f"{combo_text}\n{doc_raw_text}"
    num_fallback = _extract_amount_qty_from_text(combo_text)
    for k in ("trfdtl_trfprc", "trfdtl_stkcnt"):
        if pd.isna(_num_or_na_keep_sign(row.get(k))) and (k in num_fallback):
            row[k] = num_fallback[k]

    for c in LEGACY_NUM_COLS:
        if c in row:
            row[c] = _num_or_na_keep_sign(row.get(c))
    row["bddd"] = _norm_date_any(row.get("bddd"))
    if "trf_prd" in row:
        row["trf_prd"] = _norm_date_any(row.get("trf_prd"))

    seed_corp_name = seed.get("corp_name")
    if not _is_empty_like(seed_corp_name):
        row["corp_name"] = seed_corp_name
    elif _is_empty_like(row.get("corp_name")):
        title_submitter = _extract_submitter_from_title(viewer_html)
        if not _is_empty_like(title_submitter):
            row["corp_name"] = title_submitter
        elif not _is_empty_like(row.get("flr_nm")):
            row["corp_name"] = row["flr_nm"]

    rows: List[Dict[str, Any]] = []
    base = row.copy()
    is_acquire = _is_acquire_report_name(base.get("report_nm"))
    signed = 1 if is_acquire else -1
    if "trfdtl_stkcnt" in base:
        base["trfdtl_stkcnt"] = _signed_num(base.get("trfdtl_stkcnt"), sign=signed)
    if "trfdtl_trfprc" in base:
        base["trfdtl_trfprc"] = _signed_num(base.get("trfdtl_trfprc"), sign=signed)
    rows.extend(
        _expand_base_rows_for_multi_issuers(
            base_row=base,
            viewer_html=viewer_html,
            doc_raw_text=doc_raw_text,
        )
    )
    used_item_idx = _apply_plan_items_to_rows(rows=rows, items=items, sign=signed)

    for idx, it in enumerate(items):
        if idx in used_item_idx:
            continue
        r: Dict[str, Any] = {c: pd.NA for c in LEGACY_OUT_COLS}
        r["rcept_no"] = base.get("rcept_no")
        r["rcept_dt"] = base.get("rcept_dt")
        r["corp_cls"] = base.get("corp_cls")
        r["corp_code"] = base.get("corp_code")
        r["corp_name"] = base.get("corp_name")
        r["flr_nm"] = base.get("flr_nm")
        r["report_nm"] = base.get("report_nm")
        r["viewer_url"] = base.get("viewer_url")
        r["source"] = (str(base.get("source", "INIT")) + "+NOTE_PLAN").strip("+")
        r["iscmp_cmpnm"] = it["name"]
        r["trfdtl_stkcnt"] = _signed_num(it.get("shares"), sign=signed)
        r["trfdtl_trfprc"] = _signed_num(it.get("amt"), sign=signed)
        r["trf_pp"] = "처분대금 재투자(취득계획)"
        r["bddd"] = base.get("bddd")
        r["trf_prd"] = base.get("trf_prd")
        rows.append(r)

    df = pd.DataFrame(rows)
    if "iscmp_cmpnm" in df.columns:
        df["iscmp_cmpnm"] = df["iscmp_cmpnm"].map(
            lambda x: (_normalize_company_token(x) if not _is_bad_issuer(x) else pd.NA)
        )
        # one-report duplicate guard: same issuer parsed twice by minor text noise (&cr, entity, spacing)
        if len(df) > 1:
            df["_issuer_key"] = df["iscmp_cmpnm"].map(_issuer_dedup_key)
            if "trfdtl_stkcnt" in df.columns:
                df["_score_stk"] = df["trfdtl_stkcnt"].map(lambda x: 0 if pd.isna(_num_or_na_keep_sign(x)) else 1)
            else:
                df["_score_stk"] = 0
            if "trfdtl_trfprc" in df.columns:
                df["_score_amt"] = df["trfdtl_trfprc"].map(lambda x: 0 if pd.isna(_num_or_na_keep_sign(x)) else 1)
            else:
                df["_score_amt"] = 0
            df["_score"] = df["_score_stk"] + df["_score_amt"]
            df = df.sort_values(["_issuer_key", "_score"], ascending=[True, False]).drop_duplicates(
                subset=["rcept_no", "_issuer_key"],
                keep="first",
            )
            df = df.drop(columns=["_issuer_key", "_score_stk", "_score_amt", "_score"], errors="ignore")

    for c in LEGACY_NUM_COLS:
        if c in df.columns:
            df[c] = df[c].map(_num_or_na_keep_sign)
    for c in LEGACY_OUT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    return df[LEGACY_OUT_COLS].copy()


@dataclass
class CorporateHoldingsModule:
    api_key: str
    timeout: int = 30
    max_retries: int = 5
    base_sleep: float = 0.8
    request_interval_sec: float = 0.0
    client: OpenDartClient = field(init=False)
    session: requests.Session = field(init=False)

    def __post_init__(self) -> None:
        self.request_interval_sec = max(float(self.request_interval_sec), 0.0)
        self.client = OpenDartClient(
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
            base_sleep=self.base_sleep,
            request_interval_sec=self.request_interval_sec,
        )
        self.session = self.client.session
        setattr(self.session, "_dart_request_interval_sec", self.request_interval_sec)

    def _session_get(self, url: str, *, params: Optional[dict[str, Any]] = None) -> requests.Response:
        return _throttled_session_get(
            self.session,
            url,
            params=params,
            timeout=self.timeout,
            min_interval_sec=self.request_interval_sec,
        )

    def _call_json(self, url: str, **params: Any) -> dict:
        payload = {"crtfc_key": self.api_key, **params}
        for attempt in range(self.max_retries):
            resp = self._session_get(url, params=payload)
            resp.raise_for_status()
            js = resp.json()
            status = js.get("status", "")
            if status in ("000", "013"):
                return js
            if status == "020":
                time.sleep(self.base_sleep * (2 ** attempt))
                continue
            raise RuntimeError(
                f"DART API error: status={status}, message={js.get('message')}, url={url}"
            )
        raise RuntimeError(f"Request retries exhausted: {url}")

    def resolve_investor(self, investor: str) -> tuple[str, str, str]:
        return self.client.resolve_investor(investor)

    def fetch_other_corp_investment_status_df(
        self,
        corp_code: str,
        bsns_year: int | str,
        reprt_code: str = "11011",
    ) -> pd.DataFrame:
        js = self._call_json(
            OTR_CPR_INVSTMNT_URL,
            corp_code=str(corp_code).zfill(8),
            bsns_year=str(bsns_year),
            reprt_code=str(reprt_code),
        )
        if js.get("status") == "013":
            return _empty_output_df()

        raw = pd.DataFrame(js.get("list", []))
        if raw.empty:
            return _empty_output_df()

        out = pd.DataFrame(index=raw.index)
        out["rcept_no"] = _string_col(raw, "rcept_no")
        out["rcept_dt"] = _first_non_null_series(raw, "stlm_dt")
        missing_rcept_dt = out["rcept_dt"].isna() & out["rcept_no"].notna()
        if missing_rcept_dt.any():
            out.loc[missing_rcept_dt, "rcept_dt"] = _date_from_rcept_no(out.loc[missing_rcept_dt, "rcept_no"])

        out["corp_cls"] = _string_col(raw, "corp_cls")
        out["corp_code"] = _string_col(raw, "corp_code")
        out["report_nm"] = f"otrCprInvstmntSttus_{bsns_year}_{reprt_code}"
        out["flr_nm"] = pd.NA
        out["pblntf_ty"] = "A"
        out["source"] = "OTRCPR_INVSTMNT_STTUS"
        out["viewer_url"] = _viewer_url_from_rcept_no(out["rcept_no"])

        out["corp_name"] = _string_col(raw, "corp_name")
        out["iscmp_cmpnm"] = _string_col(raw, "inv_prm")
        out["trfdtl_stkcnt"] = _first_non_null_numeric_series(raw, "trmend_blce_qy", "bsis_blce_qy", "incrs_dcrs_acqs_dsps_qy")
        out["trfdtl_trfprc"] = _first_non_null_numeric_series(
            raw,
            "trmend_blce_acntbk_amount",
            "frst_acqs_amount",
            "incrs_dcrs_acqs_dsps_amount",
        )
        out["trf_pp"] = _string_col(raw, "invstmnt_purps")

        if "iscmp_cmpnm" in out.columns:
            out = out[~out["iscmp_cmpnm"].map(_is_summary_counterparty)].copy()

        return select_graph_and_metadata_columns(out)

    def fetch_majorstock_status_df(self, corp_code: str) -> pd.DataFrame:
        js = self._call_json(MAJORSTOCK_URL, corp_code=str(corp_code).zfill(8))
        if js.get("status") == "013":
            return _empty_output_df()

        raw = pd.DataFrame(js.get("list", []))
        if raw.empty:
            return _empty_output_df()

        source_holder = _string_col(raw, "repror")
        target_corp = _string_col(raw, "corp_name")

        out = pd.DataFrame(index=raw.index)
        out["rcept_no"] = _string_col(raw, "rcept_no")
        out["rcept_dt"] = _string_col(raw, "rcept_dt")
        out["corp_cls"] = pd.NA
        out["corp_code"] = _string_col(raw, "corp_code")
        out["report_nm"] = _first_non_null_series(raw, "report_tp")
        out["flr_nm"] = source_holder
        out["pblntf_ty"] = pd.NA
        out["source"] = "MAJORSTOCK_STKQY_PROXY"
        out["viewer_url"] = _viewer_url_from_rcept_no(out["rcept_no"])

        out["corp_name"] = source_holder.combine_first(target_corp)
        out["iscmp_cmpnm"] = target_corp
        out["trfdtl_stkcnt"] = _to_numeric_series(raw.get("stkqy", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trfdtl_trfprc"] = out["trfdtl_stkcnt"]
        out["trf_pp"] = _string_col(raw, "report_resn")

        return select_graph_and_metadata_columns(out)

    def fetch_inh_decision_df(self, corp_code: str, bgn_de: str, end_de: str) -> pd.DataFrame:
        js = self._call_json(
            INH_DECSN_URL,
            corp_code=str(corp_code).zfill(8),
            bgn_de=_norm_yyyymmdd(bgn_de),
            end_de=_norm_yyyymmdd(end_de),
        )
        if js.get("status") == "013":
            return _empty_output_df()

        raw = pd.DataFrame(js.get("list", []))
        if raw.empty:
            return _empty_output_df()

        out = pd.DataFrame(index=raw.index)
        out["rcept_no"] = _string_col(raw, "rcept_no")
        out["rcept_dt"] = _date_from_rcept_no(out["rcept_no"])
        out["corp_cls"] = _string_col(raw, "corp_cls")
        out["corp_code"] = _string_col(raw, "corp_code")
        out["report_nm"] = "otcprStkInvscrInhDecsn"
        out["flr_nm"] = pd.NA
        out["pblntf_ty"] = pd.NA
        out["source"] = "OTCPR_STK_INH_DECSN"
        out["viewer_url"] = _viewer_url_from_rcept_no(out["rcept_no"])

        out["corp_name"] = _string_col(raw, "corp_name")
        out["iscmp_cmpnm"] = _string_col(raw, "iscmp_cmpnm")
        out["trfdtl_stkcnt"] = _to_numeric_series(raw.get("inhdtl_stkcnt", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trfdtl_trfprc"] = _to_numeric_series(raw.get("inhdtl_inhprc", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trf_pp"] = _string_col(raw, "inh_pp")

        return select_graph_and_metadata_columns(out)

    def fetch_trf_decision_df(self, corp_code: str, bgn_de: str, end_de: str) -> pd.DataFrame:
        raw = self.client.fetch_transfer_major(
            corp_code=str(corp_code).zfill(8),
            bgn_de=_norm_yyyymmdd(bgn_de),
            end_de=_norm_yyyymmdd(end_de),
        )
        if raw.empty:
            return _empty_output_df()

        out = pd.DataFrame(index=raw.index)
        out["rcept_no"] = _string_col(raw, "rcept_no")
        out["rcept_dt"] = _string_col(raw, "rcept_dt")
        out["corp_cls"] = _string_col(raw, "corp_cls")
        out["corp_code"] = _string_col(raw, "corp_code")
        out["report_nm"] = _first_non_null_series(raw, "report_nm")
        out["flr_nm"] = _string_col(raw, "flr_nm")
        out["pblntf_ty"] = _string_col(raw, "pblntf_ty")
        out["source"] = "OTCPR_STK_TRF_DECSN"
        out["viewer_url"] = _first_non_null_series(raw, "viewer_url")

        out["corp_name"] = _string_col(raw, "corp_name")
        out["iscmp_cmpnm"] = _string_col(raw, "iscmp_cmpnm")
        out["trfdtl_stkcnt"] = _to_numeric_series(raw.get("trfdtl_stkcnt", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trfdtl_trfprc"] = _to_numeric_series(raw.get("trfdtl_trfprc", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trf_pp"] = _string_col(raw, "trf_pp")

        return select_graph_and_metadata_columns(out)

    def fetch_stock_exchange_decision_df(self, corp_code: str, bgn_de: str, end_de: str) -> pd.DataFrame:
        js = self._call_json(
            STK_EXTR_DECSN_URL,
            corp_code=str(corp_code).zfill(8),
            bgn_de=_norm_yyyymmdd(bgn_de),
            end_de=_norm_yyyymmdd(end_de),
        )
        if js.get("status") == "013":
            return _empty_output_df()

        raw = pd.DataFrame(js.get("list", []))
        if raw.empty:
            return _empty_output_df()

        ratio_proxy = raw.get("extr_rt", pd.Series([pd.NA] * len(raw), index=raw.index)).map(_ratio_to_float)
        ratio_proxy = pd.to_numeric(ratio_proxy, errors="coerce").fillna(1.0)

        out = pd.DataFrame(index=raw.index)
        out["rcept_no"] = _string_col(raw, "rcept_no")
        out["rcept_dt"] = _date_from_rcept_no(out["rcept_no"])
        out["corp_cls"] = _string_col(raw, "corp_cls")
        out["corp_code"] = _string_col(raw, "corp_code")
        out["report_nm"] = "stkExtrDecsn"
        out["flr_nm"] = pd.NA
        out["pblntf_ty"] = pd.NA
        out["source"] = "STK_EXTR_DECSN_RATIO_PROXY"
        out["viewer_url"] = _viewer_url_from_rcept_no(out["rcept_no"])

        out["corp_name"] = _string_col(raw, "corp_name")
        out["iscmp_cmpnm"] = _string_col(raw, "extr_tgcmp_cmpnm")
        out["trfdtl_stkcnt"] = _to_numeric_series(raw.get("extr_tgcmp_tisstk_ostk", pd.Series([pd.NA] * len(raw), index=raw.index)))
        out["trfdtl_trfprc"] = ratio_proxy
        out["trf_pp"] = _string_col(raw, "extr_pp")

        return select_graph_and_metadata_columns(out)

    def _fetch_viewer_plain_text(self, rcept_no: str) -> str:
        try:
            resp = self._session_get(MAIN_URL, params={"rcpNo": str(rcept_no)})
            resp.raise_for_status()
            main_html = resp.text
        except Exception:
            return ""

        cands = []
        for m in VIEWDOC_PATTERN.finditer(main_html):
            gd = m.groupdict()
            s, e = m.span()
            ctx = main_html[max(0, s - 200): min(len(main_html), e + 200)]
            gd["_ctx"] = ctx
            cands.append(gd)

        if not cands:
            return ""

        cands.sort(
            key=lambda c: (
                10 if "타법인주식및출자증권처분결정" in c.get("_ctx", "") else 0,
                1 if "dart3.xsd" in c.get("dtd", "").lower() else 0,
            ),
            reverse=True,
        )
        best = cands[0]

        params = {
            "rcpNo": best["rcpNo"],
            "dcmNo": best["dcmNo"],
            "eleId": best["eleId"],
            "offset": best["offset"],
            "length": best["length"],
            "dtd": best["dtd"],
        }
        try:
            viewer = self._session_get(VIEWER_URL, params=params)
            viewer.raise_for_status()
            return _markup_to_text(viewer.text)
        except Exception:
            return ""

    def _fetch_document_plain_text(self, rcept_no: str) -> str:
        try:
            resp = self._session_get(
                DOCUMENT_URL,
                params={"crtfc_key": self.api_key, "rcept_no": str(rcept_no)},
            )
            resp.raise_for_status()
        except Exception:
            return ""

        raw_texts = _extract_texts_from_document_zip(resp.content)
        if not raw_texts:
            return ""
        chunks = [_markup_to_text(txt) for txt in raw_texts[:5] if txt and txt.strip()]
        return "\n".join([c for c in chunks if c]).strip()

    def fetch_transfer_note_plan_df(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        max_reports: Optional[int] = None,
        sleep_sec: float = 0.03,
        include_base_row: bool = True,
    ) -> pd.DataFrame:
        list_df = fetch_transfer_list_standalone(
            api_key=self.api_key,
            corp_code=str(corp_code).zfill(8),
            bgn_de=_norm_yyyymmdd(bgn_de),
            end_de=_norm_yyyymmdd(end_de),
            pblntf_tys=("B", "I"),
            timeout=self.timeout,
            max_retries=self.max_retries,
            base_sleep=self.base_sleep,
            request_interval_sec=self.request_interval_sec,
        )
        if list_df.empty:
            return _empty_output_df()

        seeds = list_df.drop_duplicates(subset=["rcept_no"], keep="first").copy()
        if max_reports is not None and int(max_reports) > 0:
            seeds = seeds.head(int(max_reports)).copy()
        chunks: List[pd.DataFrame] = []

        for _, seed in seeds.iterrows():
            viewer_url = seed.get("viewer_url")
            rcept_no = str(seed.get("rcept_no", "")).strip()
            if (pd.isna(viewer_url) or str(viewer_url).strip() == "") and re.fullmatch(r"\d{14}", rcept_no):
                viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            if pd.isna(viewer_url) or str(viewer_url).strip() == "":
                continue

            detail_df = extract_transfer_decision_from_viewer_url(
                viewer_url=str(viewer_url),
                api_key=self.api_key,
                timeout=self.timeout,
                verbose=False,
                seed_row=seed,
                session=self.session,
                request_interval_sec=self.request_interval_sec,
            )
            if detail_df.empty:
                if sleep_sec > 0:
                    time.sleep(sleep_sec)
                continue

            if not include_base_row:
                detail_df = detail_df[
                    detail_df["source"].astype(str).str.contains("NOTE_PLAN", na=False)
                ].copy()
            if not detail_df.empty:
                chunks.append(detail_df)

            if sleep_sec > 0:
                time.sleep(sleep_sec)

        if not chunks:
            return _empty_output_df()

        out = pd.concat(chunks, ignore_index=True)
        return select_graph_and_metadata_columns(out)

    def fetch_all_holding_dfs(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        reprt_codes: Iterable[str] = ("11011",),
        include_periodic_status: bool = False,
        include_majorstock_status: bool = False,
        include_transfer_note_plan: bool = True,
        max_note_reports: Optional[int] = None,
    ) -> dict[str, pd.DataFrame]:
        corp_code = str(corp_code).zfill(8)
        bgn = _norm_yyyymmdd(bgn_de)
        end = _norm_yyyymmdd(end_de)

        if start_year is None:
            start_year = int(bgn[:4])
        if end_year is None:
            end_year = int(end[:4])

        def _safe_df(name: str, fn) -> pd.DataFrame:
            try:
                return fn()
            except Exception as exc:
                print(f"[WARN] {name} failed corp_code={corp_code}: {exc}")
                return _empty_output_df()

        if include_periodic_status:
            otr_frames: list[pd.DataFrame] = []
            for year in range(int(start_year), int(end_year) + 1):
                for reprt_code in reprt_codes:
                    one = _safe_df(
                        f"other_corp_investment_status[{year}-{reprt_code}]",
                        lambda y=year, rc=reprt_code: self.fetch_other_corp_investment_status_df(
                            corp_code=corp_code,
                            bsns_year=y,
                            reprt_code=str(rc),
                        ),
                    )
                    if not one.empty:
                        otr_frames.append(one)
            df_otr = pd.concat(otr_frames, ignore_index=True) if otr_frames else _empty_output_df()
        else:
            df_otr = _empty_output_df()
        df_major = (
            _safe_df("majorstock_status", lambda: self.fetch_majorstock_status_df(corp_code))
            if include_majorstock_status else _empty_output_df()
        )
        df_inh = _safe_df("inh_decision", lambda: self.fetch_inh_decision_df(corp_code, bgn, end))
        df_trf = _safe_df("trf_decision", lambda: self.fetch_trf_decision_df(corp_code, bgn, end))
        df_extr = _safe_df("stock_exchange_decision", lambda: self.fetch_stock_exchange_decision_df(corp_code, bgn, end))
        df_note = (
            _safe_df(
                "transfer_note_plan",
                lambda: self.fetch_transfer_note_plan_df(corp_code, bgn, end, max_reports=max_note_reports),
            )
            if include_transfer_note_plan else _empty_output_df()
        )

        parts = [df_otr, df_major, df_inh, df_trf, df_extr, df_note]
        non_empty = [x for x in parts if not x.empty]
        if non_empty:
            combined = pd.concat(non_empty, ignore_index=True)
            combined = select_graph_and_metadata_columns(combined)
            combined = combined.dropna(subset=["corp_name", "iscmp_cmpnm"], how="any")
            combined = combined[
                (combined["corp_name"].astype(str).str.strip() != "")
                & (combined["iscmp_cmpnm"].astype(str).str.strip() != "")
            ].copy()
            combined = combined.drop_duplicates(
                subset=["source", "rcept_no", "corp_name", "iscmp_cmpnm", "trfdtl_trfprc", "trfdtl_stkcnt", "trf_pp"],
                keep="first",
            )
            combined = combined.reset_index(drop=True)
        else:
            combined = _empty_output_df()

        return {
            "other_corp_investment_status": select_graph_and_metadata_columns(df_otr),
            "majorstock_status": select_graph_and_metadata_columns(df_major),
            "inh_decision": select_graph_and_metadata_columns(df_inh),
            "trf_decision": select_graph_and_metadata_columns(df_trf),
            "stock_exchange_decision": select_graph_and_metadata_columns(df_extr),
            "transfer_note_plan": select_graph_and_metadata_columns(df_note),
            "combined": select_graph_and_metadata_columns(combined),
        }

    def fetch_all_holding_dfs_by_investor(
        self,
        investor: str,
        bgn_de: str,
        end_de: str,
        start_year: Optional[int] = None,
        end_year: Optional[int] = None,
        reprt_codes: Iterable[str] = ("11011",),
        include_periodic_status: bool = False,
        include_majorstock_status: bool = False,
        include_transfer_note_plan: bool = True,
        max_note_reports: Optional[int] = None,
    ) -> tuple[str, str, str, dict[str, pd.DataFrame]]:
        corp_code, corp_name, stock_code = self.resolve_investor(investor)
        data = self.fetch_all_holding_dfs(
            corp_code=corp_code,
            bgn_de=bgn_de,
            end_de=end_de,
            start_year=start_year,
            end_year=end_year,
            reprt_codes=reprt_codes,
            include_periodic_status=include_periodic_status,
            include_majorstock_status=include_majorstock_status,
            include_transfer_note_plan=include_transfer_note_plan,
            max_note_reports=max_note_reports,
        )
        return corp_code, corp_name, stock_code, data


__all__ = [
    "GRAPH_REQUIRED_COLS",
    "METADATA_COLS",
    "GRAPH_OPTIONAL_COLS",
    "OUTPUT_COLS",
    "call_json",
    "list_all_pages",
    "fetch_transfer_list",
    "fetch_transfer_list_standalone",
    "extract_transfer_decision_from_viewer_url",
    "CorporateHoldingsModule",
    "select_graph_and_metadata_columns",
]
