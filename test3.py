# -*- coding: utf-8 -*-
"""
run_fetch_transfer_list.py

실행 전:
1) pip install requests pandas python-dotenv
2) .env 파일에 OPENDART_API_KEY=발급키 입력

실행:
python run_fetch_transfer_list.py
"""

import os
import re
import time
import requests
import pandas as pd
from dataclasses import dataclass, field
from typing import Iterable

# -----------------------------
# OpenDART Endpoints
# -----------------------------
BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# "타법인주식및출자증권처분결정/양도결정" 제목 필터
TRANSFER_TITLE_REGEX = r"타법인\s*주식\s*및\s*출자증권\s*(처분결정|양도결정)"


# -----------------------------
# Helpers
# -----------------------------
def _norm_yyyymmdd(s: str) -> str:
    s = str(s).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"Invalid date format: {s} (YYYYMMDD or YYYY-MM-DD)")
    return s


def _viewer_url_from_rcept(sr: pd.Series) -> pd.Series:
    return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + sr.astype(str)


def _ensure_cols(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out


# -*- coding: utf-8 -*-
"""
run_fetch_transfer_list_no_class.py

실행 전:
1) pip install requests pandas python-dotenv
2) .env 파일에 OPENDART_API_KEY=발급키 입력

실행:
python run_fetch_transfer_list_no_class.py
"""

import os
import re
import time
from typing import Iterable, Optional, Tuple, Dict, List

import requests
import pandas as pd
from dotenv import load_dotenv


# -----------------------------
# Constants
# -----------------------------
BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
TRANSFER_TITLE_REGEX = r"타법인\s*주식\s*및\s*출자증권\s*(처분결정|양도결정)"


# -----------------------------
# Helpers
# -----------------------------
def _norm_yyyymmdd(s: str) -> str:
    s = str(s).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"Invalid date format: {s} (YYYYMMDD or YYYY-MM-DD)")
    return s


def _viewer_url_from_rcept(sr: pd.Series) -> pd.Series:
    return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + sr.astype(str)


def _ensure_cols(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out


# -----------------------------
# Core API functions (No class)
# -----------------------------
def call_json(
    api_key: str,
    url: str,
    params: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    timeout: int = 30,
    max_retries: int = 5,
    base_sleep: float = 0.8,
) -> dict:
    """
    DART 공통 JSON 호출 함수
    - status 000/013 성공 처리
    - status 020 재시도(backoff)
    """
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
        if st == "020":  # 호출 제한
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
    """
    list.json 페이징 전체 수집
    """
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
    """
    투자자(corp_code) 기준으로 list API에서
    '타법인주식및출자증권 처분/양도결정' 공시 목록만 가져옴.
    """
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
            "pblntf_ty": ty,      # B: 주요사항보고, I: 거래소공시 등
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

        # 제목 필터
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
            one["viewer_url"] = _viewer_url_from_rcept(one["rcept_no"])

        one = _ensure_cols(
            one,
            [
                "rcept_no",
                "rcept_dt",
                "corp_cls",
                "corp_code",
                "corp_name",
                "report_nm",
                "flr_nm",
                "rm",
                "pblntf_ty",
                "viewer_url",
            ],
        )

        one["corp_code"] = one["corp_code"].astype(str).str.zfill(8)
        one["source"] = one["pblntf_ty"].astype(str).map(lambda x: f"LIST_{x}")
        chunks.append(one)

    if not chunks:
        return pd.DataFrame(
            columns=[
                "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
                "report_nm", "flr_nm", "rm", "pblntf_ty", "viewer_url", "source"
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
    """
    기존 인터페이스 유지용 래퍼
    """
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



# -*- coding: utf-8 -*-
import re
import io
import zipfile
import html
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET


# =========================
# Config
# =========================
MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do"
VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"

OUT_COLS = [
    "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
    "report_nm", "flr_nm", "pblntf_ty", "source",
    "iscmp_cmpnm",
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt",
    "trf_pp", "trf_prd", "dlptn_cmpnm",
    "bddd", "viewer_url"
]

NUM_COLS = [
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt"
]

# 핵심 필드 우선
CORE_FIELDS = ["iscmp_cmpnm", "trfdtl_trfprc", "trfdtl_stkcnt", "trf_pp", "dlptn_cmpnm", "bddd"]

LABEL_MAP = {
    "iscmp_cmpnm": ["발행회사(회사명)", "발행회사 회사명", "발행회사"],
    "trfdtl_stkcnt": ["양도내역(양도주식수(주))", "양도주식수", "양도 주식수"],
    "trfdtl_trfprc": ["양도내역(양도금액(원)(A))", "양도금액", "양도 금액"],
    "trfdtl_tast": ["양도내역(총자산(원)(B))", "총자산(원)(B)", "총자산"],
    "trfdtl_ecpt": ["양도내역(자기자본(원)(C))", "자기자본(원)(C)", "자기자본"],
    "attrf_owstkcnt": ["양도후 소유주식수 및 지분비율(소유주식수(주))", "양도후 소유주식수", "소유주식수(주)"],
    "attrf_eqrt": ["양도후 소유주식수 및 지분비율(지분비율(%))", "양도후 지분비율", "지분비율(%)"],
    "trf_pp": ["양도목적", "양도 목적"],
    "trf_prd": ["양도예정일자", "양도 예정일자"],
    "dlptn_cmpnm": ["거래상대방(회사명(성명))", "거래상대방 회사명", "거래상대방", "상대방"],
    "bddd": ["이사회결의일(결정일)", "이사회결의일", "결정일"],
    # 보조 메타(있으면 채움)
    "corp_name": ["공시대상회사명", "회사명"],
    "corp_cls": ["법인구분"],
    "corp_code": ["고유번호"],
    "report_nm": ["보고서명", "공시명"]
}


def _decode_bytes_auto(b: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _extract_rcp_no(url_or_rcp: str) -> str:
    s = str(url_or_rcp).strip()
    m = re.search(r"rcpNo=(\d{14})", s)
    if m:
        return m.group(1)
    m2 = re.fullmatch(r"\d{14}", s)
    if m2:
        return s
    raise ValueError(f"rcept_no(14자리) 또는 rcpNo URL 형식이 아닙니다: {s}")


def _num_or_na(x):
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


def _norm_date_any(x):
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
    # 1) 같은 줄 "라벨: 값"
    for i, ln in enumerate(lines):
        if any(a in ln for a in aliases):
            m = re.split(r"[:：]\s*", ln, maxsplit=1)
            if len(m) == 2 and m[1].strip():
                return m[1].strip()

            # 2) 다음 줄에서 값 탐색
            for j in range(i + 1, min(i + 8, len(lines))):
                cand = lines[j].strip(" \t:-")
                if not cand:
                    continue
                # 다음 줄이 또 라벨이면 skip
                if any(a in cand for a in aliases):
                    continue
                return cand
    return None


def _extract_kv_from_html(html_text: str) -> Dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")

    for t in soup(["script", "style"]):
        t.decompose()

    kv = {}
    # 표 기반 key-value
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if len(cells) < 2:
            continue
        # (라벨, 값) 페어
        for i in range(0, len(cells) - 1, 2):
            k = cells[i].strip()
            v = cells[i + 1].strip()
            if k and v and k not in kv:
                kv[k] = v
    return kv


def _extract_fields_from_html_text(html_text: str) -> Dict[str, object]:
    out = {c: pd.NA for c in OUT_COLS}

    kv = _extract_kv_from_html(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = _clean_lines_from_text(text)

    # 1) KV 매칭 우선
    for field, aliases in LABEL_MAP.items():
        val = None

        # exact key
        for a in aliases:
            if a in kv:
                val = kv[a]
                break

        # contains key
        if val is None:
            for k, v in kv.items():
                if any(a in k for a in aliases):
                    val = v
                    break

        # lines fallback
        if val is None:
            val = _pick_value_from_lines(lines, aliases)

        if val is not None and str(val).strip() != "":
            out[field] = str(val).strip()

    # 숫자 보정
    for c in NUM_COLS:
        out[c] = _num_or_na(out.get(c))

    # 날짜 보정
    out["bddd"] = _norm_date_any(out.get("bddd"))
    return out


def _find_viewdoc_candidates(main_html: str) -> List[Dict[str, str]]:
    """
    main.do HTML 내 viewDoc(...) 파라미터 후보 추출
    """
    pattern = re.compile(
        r"viewDoc\(\s*['\"](?P<rcpNo>\d{14})['\"]\s*,\s*['\"](?P<dcmNo>\d+)['\"]\s*,\s*['\"](?P<eleId>\d+)['\"]\s*,\s*['\"](?P<offset>\d+)['\"]\s*,\s*['\"](?P<length>\d+)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"]\s*\)",
        re.IGNORECASE
    )

    cands = []
    for m in pattern.finditer(main_html):
        c = m.groupdict()
        # 주변 문맥 (제목 키워드 판단용)
        s, e = m.span()
        ctx = main_html[max(0, s - 200): min(len(main_html), e + 200)]
        c["_ctx"] = ctx
        cands.append(c)

    return cands


def _choose_best_candidate(cands: List[Dict[str, str]], keyword: str = "타법인주식및출자증권처분결정") -> Optional[Dict[str, str]]:
    if not cands:
        return None
    # 제목 키워드가 주변에 있는 노드 우선
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


def _fetch_viewer_html_by_rcpno(rcp_no: str, session: requests.Session, timeout: int = 30) -> Tuple[Optional[str], str]:
    """
    returns: (viewer_html or None, source_tag)
    """
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


import re
import html
import pandas as pd
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from typing import Dict


def _norm_label(s: str) -> str:
    s = html.unescape(str(s or ""))
    s = re.sub(r"\s+", "", s)
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"^[\-\u2022]+", "", s)   # 앞 bullet 제거
    s = re.sub(r"^\d+\.", "", s)         # 앞 번호(예: 4.) 제거
    return s


# 라벨 -> 필드 정확 매핑
_LABEL_TO_FIELD = {
    "회사명(국적)": "iscmp_cmpnm",
    "처분주식수(주)": "trfdtl_stkcnt",
    "처분금액(원)": "trfdtl_trfprc",
    "자기자본(원)": "trfdtl_ecpt",
    "자기자본대비(%)": "trfdtl_tast",
    "소유주식수(주)": "attrf_owstkcnt",
    "지분비율(%)": "attrf_eqrt",
    "처분목적": "trf_pp",
    "처분예정일자": "trf_prd",
    "이사회결의일(결정일)": "bddd",
}
_LABEL_TO_FIELD_N = {_norm_label(k): v for k, v in _LABEL_TO_FIELD.items()}


def _to_field(label: str):
    return _LABEL_TO_FIELD_N.get(_norm_label(label))


def _extract_fields_from_xml_tags(xml_text: str) -> Dict[str, object]:
    """
    XForms(html/xml) 문서의 '표 라벨/값' 구조를 정확 매핑.
    (태그명 추정이 아니라 라벨 기반 분류)
    """
    wanted = {
        "iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
        "attrf_owstkcnt", "attrf_eqrt", "trf_pp", "trf_prd", "dlptn_cmpnm", "bddd"
    }
    out = {k: pd.NA for k in wanted}

    # 1) 실제 태그명이 필드명인 경우(있으면 먼저 반영)
    try:
        root = ET.fromstring(xml_text)
        for el in root.iter():
            tag = el.tag.split("}")[-1].strip().lower()
            txt = (el.text or "").strip()
            if txt and tag in out and pd.isna(out[tag]):
                out[tag] = txt
    except Exception:
        pass  # html 형태면 ET 실패 가능, 아래 표 파서로 처리

    # 2) 핵심: 표의 라벨 셀 -> 값 셀 매핑
    soup = BeautifulSoup(xml_text, "html.parser")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells if c and c.strip()]
        if len(cells) < 2:
            continue

        # 인접한 (label, value) 탐색
        # 예: [1.발행회사, 회사명(국적), 삼성전자주식회사, 대표이사, 한종회]
        # i=1에서 label=회사명(국적), value=삼성전자주식회사 로 정확히 잡힘
        for i in range(len(cells) - 1):
            fld = _to_field(cells[i])
            if not fld:
                continue

            val = cells[i + 1].strip()
            # 다음 셀이 또 라벨이면 값이 아님
            if _to_field(val):
                continue

            if pd.isna(out[fld]) and val not in {"", "-", "<NA>"}:
                out[fld] = val

    # 3) 타입 보정
    num_cols = [
        "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast",
        "trfdtl_ecpt", "attrf_owstkcnt", "attrf_eqrt"
    ]
    for c in num_cols:
        out[c] = _num_or_na_keep_sign(out.get(c))  # 기존 함수 사용

    out["trf_prd"] = _norm_date_any(out.get("trf_prd"))  # 기존 함수 사용
    out["bddd"] = _norm_date_any(out.get("bddd"))        # 기존 함수 사용

    return out


def _parse_document_zip_best(raw_zip_bytes: bytes) -> Dict[str, object]:
    best = {c: pd.NA for c in OUT_COLS}
    best_score = -1

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip_bytes))
    except zipfile.BadZipFile:
        # document.xml 에러 payload 가능성
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

        # html/text 라벨 파싱
        if low.endswith((".html", ".htm", ".xhtml")):
            cand = _extract_fields_from_html_text(txt)
        else:
            # xml은 태그 + text 모두 시도
            tag_cand = _extract_fields_from_xml_tags(txt)
            html_cand = _extract_fields_from_html_text(txt)

            cand = html_cand.copy()
            for k, v in tag_cand.items():
                # tag_cand가 값을 갖고 있으면 우선 반영
                if k in cand and not pd.isna(v):
                    cand[k] = v

        score = sum(1 for k in CORE_FIELDS if not pd.isna(cand.get(k)))
        if score > best_score:
            best_score = score
            best = cand

    return best


### 새로운 주식 취득 시
import re
import html as ihtml
import numbers
from typing import Optional, Dict, Any, List

import pandas as pd
import requests


# =========================
# Robust helpers
# =========================

def _num_or_na_keep_sign(x):
    """부호(+/-) 보존 숫자 파서. 실패 시 pd.NA"""
    if x is None or x is pd.NA:
        return pd.NA
    if isinstance(x, numbers.Number):
        return x
    s = str(x).strip()
    if s == "" or s.upper() == "N/A" or s == "<NA>":
        return pd.NA

    # 쉼표 제거
    s = s.replace(",", "")
    # 숫자 토큰 추출 (+/- 허용)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return pd.NA
    tok = m.group(0)

    if "." in tok:
        try:
            return float(tok)
        except Exception:
            return pd.NA
    try:
        return int(tok)
    except Exception:
        return pd.NA


def _signed_num(x, sign=1):
    v = _num_or_na_keep_sign(x)
    if pd.isna(v):
        return pd.NA
    return abs(v) * sign


def _html_to_text_preserve_lines(viewer_html: str) -> str:
    """HTML -> 텍스트(줄바꿈 최대 보존)"""
    if not viewer_html:
        return ""

    t = viewer_html
    # 줄바꿈 유도 태그
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>|</tr>|</li>|</div>|</table>|</h\d>", "\n", t)
    t = re.sub(r"(?i)<p[^>]*>|<tr[^>]*>|<li[^>]*>|<div[^>]*>|<table[^>]*>|<h\d[^>]*>", "\n", t)

    # 태그 제거
    t = re.sub(r"<[^>]+>", " ", t)
    t = ihtml.unescape(t)
    t = t.replace("\u00a0", " ")

    # 라인별 정리(라인은 유지)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    t = "\n".join(lines)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t


def _extract_section9_text(raw_text: str) -> str:
    """'9. 기타 투자판단에 참고할 사항' 섹션 추출 (없으면 원문 반환)"""
    if not raw_text:
        return ""

    txt = raw_text

    # 섹션 시작 후보들
    starts = [
        r"9\.\s*기타\s*투자판단에\s*참고할\s*사항",
        r"9\.\s*기타\s*투자판단\s*참고사항",
        r"기타\s*투자판단에\s*참고할\s*사항",
    ]

    m = None
    for p in starts:
        m = re.search(p, txt, flags=re.I | re.S)
        if m:
            break

    if not m:
        # 9번 헤더 못 찾으면 그대로 반환
        body = txt
    else:
        body = txt[m.end():].strip()

    # 붙어있는 번호목록 분리: " ... 1. 포스코 ... 2. 포스코 ..."
    body = re.sub(r"(?<!\n)(\s)(?=\d+\.\s)", "\n", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body


def _pick_best_note_text(parsed: Dict[str, Any], doc_parsed: Dict[str, Any], viewer_html: str) -> str:
    """
    note 텍스트 후보를 광범위하게 수집 후 가장 유의미한 텍스트 선택.
    """
    cands: List[str] = []

    # 1) parsed/doc_parsed 문자열 값에서 후보 수집
    for d in (parsed or {}, doc_parsed or {}):
        for k, v in d.items():
            if not isinstance(v, str):
                continue
            vv = v.strip()
            if not vv:
                continue
            lk = str(k).lower()

            # 키 기반/내용 기반 후보
            if (
                "note" in lk or "etc" in lk or "remark" in lk or
                "기타" in lk or "참고" in lk or
                "기타 투자판단" in vv or "취득금액" in vv or "한도내 신규 투자" in vv
            ):
                cands.append(vv)

            # 번호 목록+주식수+원 패턴이 보이면 후보 추가
            if re.search(r"\d+\.\s*.+?\d[\d,]*\s*주.*?\d[\d,]*\s*원", vv, flags=re.S):
                cands.append(vv)

    # 2) viewer_html 전체 텍스트에서 후보
    html_text = _html_to_text_preserve_lines(viewer_html)
    if html_text:
        cands.append(html_text)

    if not cands:
        return ""

    # 섹션9 추출 후 점수 계산
    best = ""
    best_score = -1

    for c in cands:
        sec = _extract_section9_text(c)

        score = 0
        # 핵심 키워드 점수
        for kw in ["기타 투자판단", "취득금액", "한도내 신규 투자", "총", "주", "원"]:
            if kw in sec:
                score += 2

        # 라인아이템 패턴 점수
        n_items = len(re.findall(r"\d+\.\s*.+?\d[\d,]*\s*주.*?\d[\d,]*\s*원", sec, flags=re.S))
        score += n_items * 5

        # 길이 점수(너무 짧은 텍스트 배제)
        score += min(len(sec) // 100, 20)

        if score > best_score:
            best_score = score
            best = sec

    return best


def _parse_plan_items(note_text: str):
    """
    예:
      1. 포스코퓨처엠 13,857주 취득금액 8,217,322,450원
      2. 포스코홀딩스 12,280주 취득금액 8,213,135,150원
    """
    if not note_text:
        return []

    t = note_text.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)

    # 번호 시작 전 줄바꿈 강제
    t = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()

    patterns = [
        # 가장 일반적인 형태 (라인 기준)
        re.compile(
            r"""(?mix)
            ^\s*(\d+)\.\s*
            ([^\n\d]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:취득(?:예정)?금액|취득\s*금액|금액)\s*
            (\d[\d,]*)\s*원
            \s*$
            """
        ),
        # 한 줄로 뭉친 형태
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

    out = []
    seen = set()

    for p in patterns:
        for seq, name, shares, amt in p.findall(t):
            name = re.sub(r"\s+", " ", name).strip(" -:\t\r\n")
            sh = int(shares.replace(",", ""))
            am = int(amt.replace(",", ""))
            key = (int(seq), name, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "line_no": int(seq),
                "name": name,
                "shares": sh,
                "amt": am,
            })

    # fallback: 번호 없이도 패턴 포착
    if not out:
        p2 = re.compile(
            r"""(?ix)
            ([가-힣A-Za-z0-9&\.\-\(\)·\s]{2,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:취득(?:예정)?금액|취득\s*금액|금액)\s*
            (\d[\d,]*)\s*원
            """
        )
        idx = 1
        for name, shares, amt in p2.findall(t):
            name = re.sub(r"\s+", " ", name).strip(" -:\t\r\n")
            sh = int(shares.replace(",", ""))
            am = int(amt.replace(",", ""))
            key = (idx, name, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "line_no": idx,
                "name": name,
                "shares": sh,
                "amt": am,
            })
            idx += 1

    out.sort(key=lambda x: x["line_no"])
    return out


# =========================
# Main extractor
# =========================

import io
import re
import html as ihtml
import zipfile
import numbers
from typing import Optional, Dict, Any, List, Tuple

import pandas as pd
import requests


# =========================================================
# Helpers (부호 보존 숫자/텍스트 추출/9번 섹션 파싱/라인아이템 파싱)
# =========================================================

def _num_or_na_keep_sign(x):
    """부호(+/-) 보존 숫자 변환. 실패 시 pd.NA."""
    if x is None or x is pd.NA:
        return pd.NA
    if isinstance(x, numbers.Number):
        return x

    s = str(x).strip()
    if not s or s in {"<NA>", "N/A", "NA", "-"}:
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


def _signed_num(x, sign=1):
    v = _num_or_na_keep_sign(x)
    if pd.isna(v):
        return pd.NA
    return abs(v) * sign


def _markup_to_text(raw: str) -> str:
    """
    xml/html 문자열 -> 줄바꿈 최대 보존 텍스트
    """
    if not raw:
        return ""

    t = raw

    # 줄바꿈이 필요한 닫힘 태그 먼저
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>|</tr>|</li>|</div>|</table>|</h\d>|</TITLE>|</P>|</TR>|</TD>|</TH>", "\n", t)

    # 나머지 태그 제거
    t = re.sub(r"<[^>]+>", " ", t)
    t = ihtml.unescape(t)
    t = t.replace("\u00a0", " ")

    # 라인 정리
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    t = "\n".join(lines)
    t = re.sub(r"\n{2,}", "\n", t).strip()

    # 번호목록 전 강제 줄바꿈 (한 줄 붙음 방지)
    t = re.sub(r"(?<!\n)(\s)(?=\d+\.\s)", "\n", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t


def _decode_bytes_best(b: bytes) -> str:
    """
    문서 인코딩 추정 디코드
    """
    for enc in ("utf-8", "cp949", "euc-kr", "utf-16", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", errors="ignore")


def _extract_texts_from_document_zip(zip_bytes: bytes) -> List[str]:
    """
    document.zip에서 xml/html/txt 파일들을 raw text 문자열로 추출
    """
    texts: List[str] = []
    if not zip_bytes:
        return texts

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()

            # 우선순위: xml > htm/html > txt
            preferred = sorted(
                names,
                key=lambda n: (
                    0 if n.lower().endswith(".xml") else
                    1 if (n.lower().endswith(".htm") or n.lower().endswith(".html")) else
                    2 if n.lower().endswith(".txt") else
                    9
                )
            )

            for nm in preferred:
                low = nm.lower()
                if not (low.endswith(".xml") or low.endswith(".htm") or low.endswith(".html") or low.endswith(".txt")):
                    continue

                try:
                    b = zf.read(nm)
                    s = _decode_bytes_best(b)
                    if s and s.strip():
                        texts.append(s)
                except Exception:
                    continue
    except Exception:
        return texts

    return texts


def _extract_section9_text(raw_text: str) -> str:
    """
    '9. 기타 투자판단에 참고할 사항' 섹션만 추출.
    못 찾으면 원문 그대로 반환.
    """
    if not raw_text:
        return ""

    txt = raw_text

    # 시작 패턴
    start_patterns = [
        r"9\.\s*기타\s*투자판단에\s*참고할\s*사항",
        r"9\.\s*기타\s*투자판단\s*참고사항",
        r"기타\s*투자판단에\s*참고할\s*사항",
    ]
    m = None
    for p in start_patterns:
        m = re.search(p, txt, flags=re.I | re.S)
        if m:
            break

    body = txt[m.end():].strip() if m else txt

    # 다음 대항목(10.)가 있으면 자르기
    m10 = re.search(r"\n\s*10\.\s*", body)
    if m10:
        body = body[:m10.start()].strip()

    # 번호 목록 형태 줄바꿈 정리
    body = re.sub(r"(?<!\n)\s(?=\d+\.\s)", "\n", body)
    body = re.sub(r"\n{2,}", "\n", body).strip()
    return body


def _parse_plan_items(note_text: str) -> List[Dict[str, Any]]:
    """
    예:
    1. 포스코퓨처엠 13,857주 취득금액 8,217,322,450원
    """
    if not note_text:
        return []

    t = note_text.replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"(?<!\n)(\d+\.\s)", r"\n\1", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()

    patterns = [
        # 정석 패턴
        re.compile(
            r"""(?mix)
            ^\s*(\d+)\.\s*
            ([^\n\d]{1,120}?)\s+
            (\d[\d,]*)\s*주(?:식)?\s*
            (?:취득(?:예정)?금액|취득\s*금액|금액)\s*
            (\d[\d,]*)\s*원\s*$
            """
        ),
        # 한 줄 붙은 경우
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
            sh = int(shares.replace(",", ""))
            am = int(amt.replace(",", ""))
            key = (int(seq), nm, sh, am)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "line_no": int(seq),
                "name": nm,
                "shares": sh,
                "amt": am,
            })

    out.sort(key=lambda x: x["line_no"])
    return out


def _pick_best_note_from_sources(
    viewer_html: str,
    parsed: Dict[str, Any],
    doc_raw_text: str
) -> str:
    """
    note 후보를 모아 가장 정보량 높은 텍스트 선택
    """
    candidates: List[str] = []

    # viewer html -> text
    if viewer_html:
        vtxt = _markup_to_text(viewer_html)
        if vtxt:
            candidates.append(vtxt)

    # parsed dict 내부 문자열 후보
    for k, v in (parsed or {}).items():
        if isinstance(v, str) and v.strip():
            lk = str(k).lower()
            if "note" in lk or "etc" in lk or "참고" in v or "기타 투자판단" in v:
                candidates.append(v.strip())

    # doc raw text
    if doc_raw_text:
        candidates.append(doc_raw_text)

    if not candidates:
        return ""

    best = ""
    best_score = -1
    for c in candidates:
        sec = _extract_section9_text(c)
        # 점수: 라인아이템 개수 + 키워드 + 길이
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


# =========================================================
# Main
# =========================================================

from typing import Optional, Dict, Any, Union
import re
import pandas as pd
import requests
from bs4 import BeautifulSoup


def _seed_to_dict(seed_row: Optional[Union[Dict[str, Any], pd.Series]]) -> Dict[str, Any]:
    if seed_row is None:
        return {}
    if isinstance(seed_row, pd.Series):
        return seed_row.to_dict()
    if isinstance(seed_row, dict):
        return seed_row
    return {}


def _is_empty_like(x) -> bool:
    if x is None or x is pd.NA:
        return True
    s = str(x).strip()
    return s == "" or s in {"-", "<NA>", "nan", "NaN"}


def _is_bad_issuer(x) -> bool:
    if _is_empty_like(x):
        return True
    s = str(x).strip()
    return s in {"회사명(국적)", "발행회사", "1. 발행회사"}


def _extract_submitter_from_title(viewer_html: str):
    """<title>베뉴지/타법인... 에서 베뉴지 추출"""
    if not viewer_html:
        return pd.NA
    soup = BeautifulSoup(viewer_html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if not title:
        return pd.NA
    # 첫 '/' 앞 토큰
    tok = re.split(r"[/|]", title, maxsplit=1)[0].strip()
    return tok if tok else pd.NA


def _extract_issuer_from_viewer_table(markup: str):
    """
    표에서 '회사명(국적)' 라벨의 바로 다음 셀 값을 issuer로 추출
    """
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
            if key == "회사명(국적)":
                v = texts[i + 1].strip()
                if not _is_bad_issuer(v):
                    return v

    # fallback: 전체 텍스트 regex
    plain = soup.get_text("\n", strip=True)
    m = re.search(
        r"회사명\(국적\)\s*([^\n]{2,80}?)\s*(?:대표이사|자본금\(원\)|자본금)",
        plain,
        flags=re.I | re.S
    )
    if m:
        v = re.sub(r"\s+", " ", m.group(1)).strip(" \t:-")
        if not _is_bad_issuer(v):
            return v

    return pd.NA


def extract_transfer_decision_from_viewer_url(
    viewer_url: str,
    api_key: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = True,
    seed_row: Optional[Union[Dict[str, Any], pd.Series]] = None,  # ✅ 추가
) -> pd.DataFrame:
    """
    단일 DART 뷰어 URL에서 타법인주식및출자증권처분결정 핵심 필드 추출

    seed_row:
      fetch_transfer_list_standalone 결과의 1개 row(dict/Series)
      -> corp_name/corp_code/corp_cls/flr_nm/pblntf_ty 등 '주체 회사 정보' 고정용
    """
    rcp_no = _extract_rcp_no(viewer_url)
    seed = _seed_to_dict(seed_row)

    row = {c: pd.NA for c in OUT_COLS}
    row["rcept_no"] = rcp_no
    row["rcept_dt"] = _norm_date_any(rcp_no[:8])
    row["viewer_url"] = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
    row["report_nm"] = "타법인주식및출자증권처분결정"
    row["source"] = "INIT"

    # ✅ seed에서 제출회사 정보 선주입 (corp_name=베뉴지 등)
    for k in ("corp_cls", "corp_code", "corp_name", "flr_nm", "pblntf_ty"):
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

    # ---------------------------
    # Step 1) viewer HTML
    # ---------------------------
    try:
        if verbose:
            print(f"[1] viewer HTML 파싱 시작: rcp_no={rcp_no}")

        viewer_html, src = _fetch_viewer_html_by_rcpno(rcp_no, sess, timeout=timeout)

        if viewer_html:
            parsed = _extract_fields_from_html_text(viewer_html) or {}

            # ❗ identity 필드는 상세 파서로 덮지 않음
            skip_identity = {"corp_name", "corp_code", "corp_cls", "flr_nm", "pblntf_ty", "rcept_no", "rcept_dt"}
            for k, v in parsed.items():
                if k not in row or k in skip_identity:
                    continue
                if (v is not None) and (not pd.isna(v)) and pd.isna(row[k]):
                    row[k] = v

            row["source"] = src

            # 제출회사명 보정 (seed 없을 때 title에서)
            if _is_empty_like(row.get("corp_name")):
                title_submitter = _extract_submitter_from_title(viewer_html)
                if not _is_empty_like(title_submitter):
                    row["corp_name"] = title_submitter

            # 발행회사명 보정 (회사명(국적) 값)
            if _is_bad_issuer(row.get("iscmp_cmpnm")):
                issuer = _extract_issuer_from_viewer_table(viewer_html)
                if not _is_bad_issuer(issuer):
                    row["iscmp_cmpnm"] = issuer

        if verbose:
            got = sum(1 for k in CORE_FIELDS if not pd.isna(row.get(k)))
            print(f"[1] viewer core filled = {got}/{len(CORE_FIELDS)}")

    except Exception as e:
        if verbose:
            print(f"[WARN] viewer HTML 파싱 실패: {e}")

    # ---------------------------
    # Step 2) document.zip fetch
    # ---------------------------
    if api_key:
        try:
            if verbose:
                print("[2] document.zip 다운로드 시작")
            resp = sess.get(
                DOCUMENT_URL,
                params={"crtfc_key": api_key, "rcept_no": rcp_no},
                timeout=timeout
            )
            resp.raise_for_status()
            doc_zip_bytes = resp.content
            if verbose:
                print(f"[2] document.zip bytes={len(doc_zip_bytes):,}")
        except Exception as e:
            if verbose:
                print(f"[WARN] document.zip 다운로드 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_FETCH_FAIL").strip("+")

    # ---------------------------
    # Step 3) zip -> raw text
    # ---------------------------
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

    # ---------------------------
    # Step 4) raw text note parse
    # ---------------------------
    note_text = _pick_best_note_from_sources(
        viewer_html=viewer_html,
        parsed=parsed,
        doc_raw_text=doc_raw_text
    )
    items = _parse_plan_items(note_text)

    if verbose:
        print(f"[4] note_text len={len(note_text)}, parsed items={len(items)}")
        if note_text:
            print("[4] note preview:", note_text[:220].replace("\n", " | "))

    # ---------------------------
    # Step 5) 마지막에 구조화 xml parse
    # ---------------------------
    need_doc = any(pd.isna(row.get(k)) for k in CORE_FIELDS)
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

            # issuer가 라벨로 들어온 경우 보정
            if _is_bad_issuer(row.get("iscmp_cmpnm")):
                issuer = _extract_issuer_from_viewer_table(viewer_html)
                if _is_bad_issuer(issuer) and doc_raw_text:
                    m = re.search(
                        r"회사명\(국적\)\s*([가-힣A-Za-z0-9\(\)\.\-·&\s]{2,80}?)\s*(?:대표이사|자본금\(원\)|자본금)",
                        re.sub(r"\s+", " ", doc_raw_text)
                    )
                    issuer = m.group(1).strip() if m else pd.NA
                if not _is_bad_issuer(issuer):
                    row["iscmp_cmpnm"] = issuer

            if verbose:
                got = sum(1 for k in CORE_FIELDS if not pd.isna(row.get(k)))
                print(f"[5] doc fallback 후 core filled = {got}/{len(CORE_FIELDS)}")
        except Exception as e:
            if verbose:
                print(f"[WARN] document 구조화 파싱 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_PARSE_FAIL").strip("+")

    # ---------------------------
    # Step 6) 후처리 + rows 구성
    # ---------------------------
    for c in NUM_COLS:
        if c in row:
            row[c] = _num_or_na_keep_sign(row.get(c))

    row["bddd"] = _norm_date_any(row.get("bddd"))
    if "trf_prd" in row:
        row["trf_prd"] = _norm_date_any(row.get("trf_prd"))

    # ✅ corp_name 최종 우선순위: seed > title > flr_nm
    seed_corp_name = seed.get("corp_name")
    if not _is_empty_like(seed_corp_name):
        row["corp_name"] = seed_corp_name
    elif _is_empty_like(row.get("corp_name")):
        title_submitter = _extract_submitter_from_title(viewer_html)
        if not _is_empty_like(title_submitter):
            row["corp_name"] = title_submitter
        elif not _is_empty_like(row.get("flr_nm")):
            row["corp_name"] = row["flr_nm"]

    rows = []

    # 처분 본문 row: 음수
    base = row.copy()
    if "trfdtl_stkcnt" in base:
        base["trfdtl_stkcnt"] = _signed_num(base.get("trfdtl_stkcnt"), sign=-1)
    if "trfdtl_trfprc" in base:
        base["trfdtl_trfprc"] = _signed_num(base.get("trfdtl_trfprc"), sign=-1)
    rows.append(base)

    # 기타사항 취득 row: 양수
    for it in items:
        r = {c: pd.NA for c in OUT_COLS}
        r["rcept_no"] = base.get("rcept_no")
        r["rcept_dt"] = base.get("rcept_dt")
        r["corp_cls"] = base.get("corp_cls")
        r["corp_code"] = base.get("corp_code")
        r["corp_name"] = base.get("corp_name")   # 제출회사 유지(베뉴지)
        r["flr_nm"] = base.get("flr_nm")
        r["report_nm"] = base.get("report_nm")
        r["viewer_url"] = base.get("viewer_url")
        r["source"] = (str(base.get("source", "INIT")) + "+NOTE_PLAN").strip("+")

        # 대상/수량/금액
        r["iscmp_cmpnm"] = it["name"]
        r["trfdtl_stkcnt"] = abs(it["shares"])
        r["trfdtl_trfprc"] = abs(it["amt"])

        if "trf_pp" in r:
            r["trf_pp"] = "처분대금 재투자(취득계획)"
        r["bddd"] = base.get("bddd")
        if "trf_prd" in r:
            r["trf_prd"] = base.get("trf_prd")

        rows.append(r)

    df = pd.DataFrame(rows)

    for c in NUM_COLS:
        if c in df.columns:
            df[c] = df[c].map(_num_or_na_keep_sign)

    for c in OUT_COLS:
        if c not in df.columns:
            df[c] = pd.NA

    return df[OUT_COLS].copy()




# -----------------------------
# Main
# -----------------------------
pd.set_option('display.max_columns', None)

load_dotenv()
API_KEY = os.getenv("OPENDART_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("OPENDART_API_KEY가 비어있습니다. .env 확인하세요.")

list_df = fetch_transfer_list_standalone(
    api_key=API_KEY,
    # corp_code="00267906",   # 베뉴지
    corp_code="00904672", # 넷마블
    bgn_de="20210101",
    end_de="20260213",
)



# https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260205800645

url = list_df.loc[0, "viewer_url"]

detail_df = extract_transfer_decision_from_viewer_url(
    viewer_url=url,
    api_key=API_KEY,
    timeout=30,
    verbose=True,
    seed_row=list_df.loc[0],   # ✅ 이전 df row 전달
)
print(detail_df)
# print(detail_df[["corp_name", "iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "source"]])
