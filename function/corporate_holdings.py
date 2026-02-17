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
    r"viewDoc\(\s*['\"](?P<rcpNo>\d{14})['\"]\s*,\s*['\"](?P<dcmNo>\d+)['\"]\s*,\s*['\"](?P<eleId>\d+)['\"]\s*,\s*['\"](?P<offset>\d+)['\"]\s*,\s*['\"](?P<length>\d+)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"]\s*\)",
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
            out[field] = str(val).strip()

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
) -> Tuple[Optional[str], str]:
    main_resp = session.get(MAIN_URL, params={"rcpNo": rcp_no}, timeout=timeout)
    main_resp.raise_for_status()
    main_html = main_resp.text

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
    vresp = session.get(VIEWER_URL, params=params, timeout=timeout)
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
    return _LABEL_TO_FIELD_N.get(_norm_label(label))


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
    return s in {"회사명", "회사명(국적)", "발행회사", "1. 발행회사"}


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
    ]
    for pat in patterns:
        m = re.search(pat, plain, flags=re.I | re.S)
        if not m:
            continue
        v = re.sub(r"\s+", " ", m.group(1)).strip(" \t:-")
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
    ]
    for pat in patterns:
        m = re.search(pat, txt, flags=re.I | re.S)
        if not m:
            continue
        v = re.sub(r"\s+", " ", m.group(1)).strip(" \t:-")
        if not _is_bad_issuer(v):
            return v
    return pd.NA


def _is_acquire_report_name(report_nm: Any) -> bool:
    s = str(report_nm or "")
    return ("취득결정" in s) or ("양수결정" in s)


def _parse_plan_items(note_text: str) -> List[Dict[str, Any]]:
    if not note_text:
        return []
    t = note_text.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()

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

    out: List[Dict[str, Any]] = []
    seen = set()
    for p in patterns:
        for seq, name, shares, amt in p.findall(t):
            nm = re.sub(r"\s+", " ", name).strip(" -:\t\r\n")
            sh = int(str(shares).replace(",", ""))
            am = int(str(amt).replace(",", ""))
            key = (int(seq), nm, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({"line_no": int(seq), "name": nm, "shares": sh, "amt": am})
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
        score = n_items * 10
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


def call_json(
    api_key: str,
    url: str,
    params: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
) -> dict:
    if params is None:
        params = {}

    payload = {"crtfc_key": api_key, **params}
    sess = session or requests.Session()

    for attempt in range(max_retries):
        r = sess.get(url, params=payload, timeout=timeout)
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
    )


def extract_transfer_decision_from_viewer_url(
    viewer_url: str,
    api_key: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = True,
    seed_row: Optional[Union[Dict[str, Any], pd.Series]] = None,
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

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    try:
        if verbose:
            print(f"[1] viewer HTML 파싱 시작: rcp_no={rcp_no}")
        viewer_html, src = _fetch_viewer_html_by_rcpno(rcp_no, sess, timeout=timeout)
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

            if _is_bad_issuer(row.get("iscmp_cmpnm")):
                issuer = _extract_issuer_from_viewer_table(viewer_html)
                if not _is_bad_issuer(issuer):
                    row["iscmp_cmpnm"] = issuer

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
            resp = sess.get(
                DOCUMENT_URL,
                params={"crtfc_key": api_key, "rcept_no": rcp_no},
                timeout=timeout,
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

            if _is_bad_issuer(row.get("iscmp_cmpnm")):
                issuer = _extract_issuer_from_viewer_table(viewer_html)
                if _is_bad_issuer(issuer) and doc_raw_text:
                    issuer = _extract_issuer_from_text(doc_raw_text)
                if not _is_bad_issuer(issuer):
                    row["iscmp_cmpnm"] = issuer
        except Exception as e:
            if verbose:
                print(f"[WARN] document 구조화 파싱 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_PARSE_FAIL").strip("+")

    if _is_bad_issuer(row.get("iscmp_cmpnm")):
        issuer = _extract_issuer_from_text(doc_raw_text)
        if not _is_bad_issuer(issuer):
            row["iscmp_cmpnm"] = issuer

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
    rows.append(base)

    for it in items:
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
        r["trfdtl_stkcnt"] = abs(it["shares"])
        r["trfdtl_trfprc"] = abs(it["amt"])
        r["trf_pp"] = "처분대금 재투자(취득계획)"
        r["bddd"] = base.get("bddd")
        r["trf_prd"] = base.get("trf_prd")
        rows.append(r)

    df = pd.DataFrame(rows)
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
    client: OpenDartClient = field(init=False)
    session: requests.Session = field(init=False)

    def __post_init__(self) -> None:
        self.client = OpenDartClient(
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
            base_sleep=self.base_sleep,
        )
        self.session = self.client.session

    def _call_json(self, url: str, **params: Any) -> dict:
        payload = {"crtfc_key": self.api_key, **params}
        for attempt in range(self.max_retries):
            resp = self.session.get(url, params=payload, timeout=self.timeout)
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
            resp = self.session.get(MAIN_URL, params={"rcpNo": str(rcept_no)}, timeout=self.timeout)
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
            viewer = self.session.get(VIEWER_URL, params=params, timeout=self.timeout)
            viewer.raise_for_status()
            return _markup_to_text(viewer.text)
        except Exception:
            return ""

    def _fetch_document_plain_text(self, rcept_no: str) -> str:
        try:
            resp = self.session.get(
                DOCUMENT_URL,
                params={"crtfc_key": self.api_key, "rcept_no": str(rcept_no)},
                timeout=self.timeout,
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
        max_reports: int = 200,
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
        )
        if list_df.empty:
            return _empty_output_df()

        seeds = list_df.drop_duplicates(subset=["rcept_no"], keep="first").head(max_reports).copy()
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
        max_note_reports: int = 200,
    ) -> dict[str, pd.DataFrame]:
        corp_code = str(corp_code).zfill(8)
        bgn = _norm_yyyymmdd(bgn_de)
        end = _norm_yyyymmdd(end_de)

        if start_year is None:
            start_year = int(bgn[:4])
        if end_year is None:
            end_year = int(end[:4])

        if include_periodic_status:
            otr_frames: list[pd.DataFrame] = []
            for year in range(int(start_year), int(end_year) + 1):
                for reprt_code in reprt_codes:
                    otr_frames.append(
                        self.fetch_other_corp_investment_status_df(
                            corp_code=corp_code,
                            bsns_year=year,
                            reprt_code=str(reprt_code),
                        )
                    )
            df_otr = pd.concat(otr_frames, ignore_index=True) if otr_frames else _empty_output_df()
        else:
            df_otr = _empty_output_df()
        if include_majorstock_status:
            df_major = self.fetch_majorstock_status_df(corp_code)
        else:
            df_major = _empty_output_df()
        df_inh = self.fetch_inh_decision_df(corp_code, bgn, end)
        df_trf = self.fetch_trf_decision_df(corp_code, bgn, end)
        df_extr = self.fetch_stock_exchange_decision_df(corp_code, bgn, end)
        df_note = (
            self.fetch_transfer_note_plan_df(corp_code, bgn, end, max_reports=max_note_reports)
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
        max_note_reports: int = 200,
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
