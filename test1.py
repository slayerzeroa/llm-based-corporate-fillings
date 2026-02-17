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


def _extract_fields_from_xml_tags(xml_text: str) -> Dict[str, object]:
    """
    XML 태그명이 API 필드명과 유사한 경우 직접 매핑
    """
    wanted = {"iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
              "attrf_owstkcnt", "attrf_eqrt", "trf_pp", "trf_prd", "dlptn_cmpnm", "bddd"}
    out = {k: pd.NA for k in wanted}
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    for el in root.iter():
        tag = el.tag.split("}")[-1].strip().lower()
        txt = (el.text or "").strip()
        if not txt:
            continue
        if tag in out and pd.isna(out[tag]):
            out[tag] = txt
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
            cand = _extract_fields_from_html_text(txt)  # xml도 텍스트로 일단 파싱
            tag_cand = _extract_fields_from_xml_tags(txt)
            for k, v in tag_cand.items():
                if k in cand and pd.isna(cand[k]) and not pd.isna(v):
                    cand[k] = v

        score = sum(1 for k in CORE_FIELDS if not pd.isna(cand.get(k)))
        if score > best_score:
            best_score = score
            best = cand

    return best


def extract_transfer_decision_from_viewer_url(
    viewer_url: str,
    api_key: Optional[str] = None,
    timeout: int = 30,
    verbose: bool = True
) -> pd.DataFrame:
    """
    단일 DART 뷰어 URL에서 타법인주식및출자증권처분결정 핵심 필드 추출
    - 1차: report/viewer.do HTML 파싱
    - 2차: document.xml(zip) 파싱( api_key 있을 때 )
    """
    rcp_no = _extract_rcp_no(viewer_url)
    row = {c: pd.NA for c in OUT_COLS}
    row["rcept_no"] = rcp_no
    row["rcept_dt"] = _norm_date_any(rcp_no[:8])   # rcp_no 앞 8자리 날짜
    row["viewer_url"] = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
    row["report_nm"] = "타법인주식및출자증권처분결정"
    row["source"] = "INIT"

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    # ---- Step 1) viewer html ----
    try:
        if verbose:
            print(f"[1/2] viewer HTML 파싱 시작: rcp_no={rcp_no}")
        viewer_html, src = _fetch_viewer_html_by_rcpno(rcp_no, sess, timeout=timeout)
        if viewer_html:
            parsed = _extract_fields_from_html_text(viewer_html)
            for k, v in parsed.items():
                if k in row and pd.isna(row[k]) and not pd.isna(v):
                    row[k] = v
            row["source"] = src
        if verbose:
            got = sum(1 for k in CORE_FIELDS if not pd.isna(row.get(k)))
            print(f"[1/2] viewer HTML core filled = {got}/{len(CORE_FIELDS)}")
    except Exception as e:
        if verbose:
            print(f"[WARN] viewer HTML 파싱 실패: {e}")

    # ---- Step 2) document.xml fallback ----
    need_doc = any(pd.isna(row.get(k)) for k in CORE_FIELDS)
    if need_doc and api_key:
        try:
            if verbose:
                print("[2/2] document.xml fallback 파싱 시작")
            resp = sess.get(DOCUMENT_URL, params={"crtfc_key": api_key, "rcept_no": rcp_no}, timeout=timeout)
            resp.raise_for_status()
            doc_parsed = _parse_document_zip_best(resp.content)
            for k, v in doc_parsed.items():
                if k in row and pd.isna(row[k]) and not pd.isna(v):
                    row[k] = v

            # source 마크
            row["source"] = (str(row["source"]) + "+DOC").strip("+")
            if verbose:
                got = sum(1 for k in CORE_FIELDS if not pd.isna(row.get(k)))
                print(f"[2/2] document fallback 후 core filled = {got}/{len(CORE_FIELDS)}")
        except Exception as e:
            if verbose:
                print(f"[WARN] document.xml 파싱 실패: {e}")
            row["source"] = (str(row["source"]) + "+DOC_FAIL").strip("+")

    # 후처리
    for c in NUM_COLS:
        row[c] = _num_or_na(row.get(c))
    row["bddd"] = _norm_date_any(row.get("bddd"))

    df = pd.DataFrame([row])
    # 컬럼 보장
    for c in OUT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[OUT_COLS].copy()


# -------------------------
# 사용 예시
# -------------------------
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()
    API_KEY = os.getenv("OPENDART_API_KEY", "").strip() or None

    url = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20230727900857"
    df = extract_transfer_decision_from_viewer_url(
        viewer_url=url,
        api_key=API_KEY,     # 없으면 None 가능
        timeout=30,
        verbose=True
    )
    print(df.to_string(index=False))
    df.to_csv("data/extracted_transfer_from_url.csv", index=False, encoding="utf-8-sig")
