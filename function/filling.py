# -*- coding: utf-8 -*-
"""
opendart_helpers_refactored.py

핵심 개선사항
- transfer(viewer_url 추출) 로직을 기준 코드(사용자 1번 코드)와 동일한 흐름으로 클래스에 내장
- fetch_transfer_by_investor_full에서 list_df를 받아 viewer/document 추출 결과를 병합
- estkRs 함수는 class session/retry 사용하도록 정리
"""

from __future__ import annotations

import io
import os
import re
import time
import html
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple, Iterable

import requests
import pandas as pd
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# ------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------
BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
BASE_CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
BASE_DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"

BASE_ELESTOCK_URL = "https://opendart.fss.or.kr/api/elestock.json"
BASE_MAJORSTOCK_URL = "https://opendart.fss.or.kr/api/majorstock.json"
BASE_OTRCPR_INVST_URL = "https://opendart.fss.or.kr/api/otrCprInvstmntSttus.json"
BASE_INH_DECISION_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrInhDecsn.json"
BASE_TRF_DECISION_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrTrfDecsn.json"
BASE_ESTK_RS_URL = "https://opendart.fss.or.kr/api/estkRs.json"

MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do"
VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"

REPORT_CODES = {"Q1": "11013", "H1": "11012", "Q3": "11014", "Y": "11011"}

# ------------------------------------------------------------
# Transfer constants
# ------------------------------------------------------------
TRANSFER_TITLE_REGEX = r"타법인\s*주식\s*및\s*출자증권\s*(처분결정|양도결정)"

TRF_DETAIL_COLS = [
    "iscmp_cmpnm", "iscmp_nt", "iscmp_rp", "iscmp_cpt", "iscmp_rl_cmpn", "iscmp_tisstk", "iscmp_mbsn",
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_tast_vs", "trfdtl_ecpt", "trfdtl_ecpt_vs",
    "attrf_owstkcnt", "attrf_eqrt",
    "trf_pp", "trf_prd",
    "dlptn_cmpnm", "dlptn_cpt", "dlptn_mbsn", "dlptn_hoadd", "dlptn_rl_cmpn",
    "dl_pym",
    "exevl_atn", "exevl_bs_rs", "exevl_intn", "exevl_pd", "exevl_op",
    "bddd", "od_a_at_t", "od_a_at_b", "adt_a_atn", "ftc_stt_atn", "popt_ctr_atn", "popt_ctr_cn",
]

TRF_NUM_COLS = [
    "iscmp_cpt", "iscmp_tisstk",
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_tast_vs",
    "trfdtl_ecpt", "trfdtl_ecpt_vs",
    "attrf_owstkcnt", "attrf_eqrt",
    "dlptn_cpt", "od_a_at_t", "od_a_at_b"
]

TRF_OUTPUT_COLS = [
    "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
    "report_nm", "flr_nm", "pblntf_ty", "source",
    "iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt",
    "trf_pp", "trf_prd", "dlptn_cmpnm",
    "bddd", "viewer_url",
    "investor_corp_code", "investor_corp_name", "investor_stock_code",
]

# 사용자 기준 코드의 핵심 필드
TRF_VIEW_CORE_FIELDS = ["iscmp_cmpnm", "trfdtl_trfprc", "trfdtl_stkcnt", "trf_pp", "dlptn_cmpnm", "bddd"]

# 사용자 기준 코드의 출력 축소 컬럼
TRF_VIEW_OUT_COLS = [
    "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
    "report_nm", "flr_nm", "pblntf_ty", "source",
    "iscmp_cmpnm",
    "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
    "attrf_owstkcnt", "attrf_eqrt",
    "trf_pp", "trf_prd", "dlptn_cmpnm",
    "bddd", "viewer_url"
]

TRF_PLACEHOLDER_VALUES = {
    "회사명(국적)", "회사명", "발행회사",
    "양도주식수(주)", "양도금액(원)(A)", "총자산(원)(B)", "자기자본(원)(C)",
    "소유주식수(주)", "지분비율(%)",
    "양도목적", "양도예정일자",
    "거래상대방", "거래상대방(회사명(성명))", "거래상대방 회사명",
    "이사회결의일(결정일)", "이사회결의일", "결정일"
}

TRF_LABEL_MAP = {
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
_ALL_LABELS = [a for v in TRF_LABEL_MAP.values() for a in v]


# estkRs 그룹 식별용
ESTK_NUM_CANDIDATES_EXPLICIT = [
    "exprc", "stkcnt", "fv", "slprc", "slta", "udtcnt", "udtamt", "amt",
    "bfsl_hdstk", "slstk", "atsl_hdstk", "grtcnt"
]


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _norm_yyyymmdd(s: str) -> str:
    s = str(s).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"Invalid date format: {s} (YYYYMMDD or YYYY-MM-DD)")
    return s


def _norm_date_any(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return pd.NA
    s = re.sub(r"[^\d]", "", str(x))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return pd.NA


def _split_windows(start_date: str, end_date: str, max_days: int = 90) -> list[tuple[str, str]]:
    s = datetime.strptime(_norm_yyyymmdd(start_date), "%Y%m%d")
    e = datetime.strptime(_norm_yyyymmdd(end_date), "%Y%m%d")
    if s > e:
        raise ValueError("start_date is later than end_date.")
    out = []
    cur = s
    while cur <= e:
        nxt = min(cur + timedelta(days=max_days - 1), e)
        out.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cur = nxt + timedelta(days=1)
    return out


def _ensure_cols(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out


def _to_num_series(s: pd.Series) -> pd.Series:
    cleaned = (
        s.astype(str)
         .str.replace(",", "", regex=False)
         .str.replace("%", "", regex=False)
         .str.replace(r"[^\d\.\-]", "", regex=True)
         .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _to_num_scalar(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return pd.NA
    t = str(x).strip().replace(",", "").replace("%", "")
    t = re.sub(r"[^\d\.\-]", "", t)
    if t in ("", "-", ".", "-."):
        return pd.NA
    try:
        v = float(t)
        return int(v) if float(v).is_integer() else v
    except ValueError:
        return pd.NA


def _viewer_url_from_rcept(sr: pd.Series) -> pd.Series:
    return "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + sr.astype(str)


def _format_yyyymmdd_series(sr: pd.Series) -> pd.Series:
    raw = sr.astype(str).str.replace(r"[^\d]", "", regex=True)
    dt = pd.to_datetime(raw, format="%Y%m%d", errors="coerce")
    return dt.dt.strftime("%Y-%m-%d").where(dt.notna(), sr)


def _infer_estk_group_name(df: pd.DataFrame, fallback: str) -> str:
    cols = set(df.columns)

    if {"sbd", "pymd", "sband", "asand", "asstd"} & cols:
        return "일반사항"
    if {"stksen", "stkcnt", "fv", "slprc", "slta", "slmthn"} & cols:
        return "증권의종류"
    if {"actsen", "actnmn", "udtcnt", "udtamt", "udtprc", "udtmth"} & cols:
        return "인수인정보"
    if {"se", "amt"} <= cols or ({"se", "amt"} & cols):
        return "자금의사용목적"
    if {"hdr", "rl_cmp", "bfsl_hdstk", "slstk", "atsl_hdstk"} & cols:
        return "매출인에관한사항"
    if {"grtrs", "exavivr", "grtcnt", "expd", "exprc"} & cols:
        return "일반청약자환매청구권"

    return fallback


# ------------------------------------------------------------
# Client
# ------------------------------------------------------------
@dataclass
class OpenDartClient:
    api_key: str
    timeout: int = 30
    max_retries: int = 5
    base_sleep: float = 0.8
    session: requests.Session = field(default_factory=requests.Session)
    _corp_cache: Optional[pd.DataFrame] = None

    def __post_init__(self):
        if not self.api_key:
            raise ValueError("api_key is required.")
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })

    # ---------- low-level ----------
    def _call_json(self, url: str, **params) -> dict:
        payload = {"crtfc_key": self.api_key, **params}
        for attempt in range(self.max_retries):
            r = self.session.get(url, params=payload, timeout=self.timeout)
            r.raise_for_status()
            js = r.json()
            status = js.get("status", "")
            if status in ("000", "013"):
                return js
            if status == "020":
                time.sleep(self.base_sleep * (2 ** attempt))
                continue
            raise RuntimeError(
                f"DART API error: status={status}, message={js.get('message')}, url={url}, params={params}"
            )
        raise RuntimeError(f"Request limit retries exhausted: {url}")

    def _call_list(self, **params) -> dict:
        return self._call_json(BASE_LIST_URL, **params)

    def _list_all_pages(self, **params) -> list[dict]:
        first = self._call_list(page_no="1", **params)
        if first.get("status") == "013":
            return []

        rows = list(first.get("list", []))
        total_page = int(first.get("total_page", 1) or 1)

        for p in range(2, total_page + 1):
            js = self._call_list(page_no=str(p), **params)
            st = js.get("status")
            if st == "000":
                rows.extend(js.get("list", []))
            elif st == "013":
                break
            else:
                break
        return rows
    
    def _is_trf_placeholder(self, v, field: Optional[str] = None) -> bool:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return True
        s = re.sub(r"\s+", " ", str(v)).strip()
        if s == "":
            return True

        if s in TRF_PLACEHOLDER_VALUES:
            return True

        # 헤더성 텍스트 패턴
        if re.fullmatch(
            r"(회사명(\(국적\))?|발행회사|거래상대방.*|양도(주식수|금액).*|총자산.*|자기자본.*|소유주식수.*|지분비율.*|결정일)",
            s
        ):
            return True

        # 필드별 엄격 처리
        if field == "iscmp_cmpnm" and s in {"회사명(국적)", "회사명"}:
            return True

        return False


    def _sanitize_transfer_detail_df(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = df.copy()
        for c in TRF_DETAIL_COLS:
            if c in out.columns:
                out[c] = out[c].apply(lambda x, cc=c: pd.NA if self._is_trf_placeholder(x, cc) else x)
        return out


    def _infer_iscmp_cmpnm_from_text(self, text: str, exclude: Optional[List[str]] = None):
        if not text:
            return pd.NA
        t = html.unescape(text)
        cands = re.findall(r"([가-힣A-Za-z0-9\.\-\(\) ]{2,80}?주식회사)", t)
        cleaned = []
        exclude = [e for e in (exclude or []) if e]

        for c in cands:
            s = re.sub(r"\s+", " ", c).strip(" \t:-")
            if not s or self._is_trf_placeholder(s, "iscmp_cmpnm"):
                continue
            if any(ex in s for ex in exclude):
                continue
            cleaned.append(s)

        if not cleaned:
            return pd.NA

        # 너무 일반적인 토큰보다 구체명 우선
        cleaned = sorted(set(cleaned), key=lambda x: (-len(x), x))
        return cleaned[0]

    # ---------- corp codes ----------
    def get_corp_codes(self, listed_only: bool = True, force_refresh: bool = False) -> pd.DataFrame:
        if self._corp_cache is not None and not force_refresh:
            return self._corp_cache.copy()

        r = self.session.get(BASE_CORPCODE_URL, params={"crtfc_key": self.api_key}, timeout=self.timeout)
        r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])

        root = ET.fromstring(xml_bytes)
        rows = []
        for item in root.findall("list"):
            rows.append(
                {
                    "corp_code": (item.findtext("corp_code") or "").strip(),
                    "corp_name": (item.findtext("corp_name") or "").strip(),
                    "stock_code": (item.findtext("stock_code") or "").strip(),
                    "modify_date": (item.findtext("modify_date") or "").strip(),
                }
            )

        df = pd.DataFrame(rows)
        if df.empty:
            self._corp_cache = df
            return df

        df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
        df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)

        if listed_only:
            df = df[df["stock_code"].str.match(r"^\d{6}$", na=False)].copy()

        self._corp_cache = df.reset_index(drop=True)
        return self._corp_cache.copy()

    def resolve_investor(self, investor: str) -> tuple[str, str, str]:
        corp_df = self.get_corp_codes(listed_only=True)
        q = str(investor).strip()

        if q.isdigit() and len(q) == 8:
            hit = corp_df[corp_df["corp_code"] == q]
        elif q.isdigit() and len(q) == 6:
            hit = corp_df[corp_df["stock_code"] == q]
        else:
            hit = corp_df[corp_df["corp_name"] == q]
            if hit.empty:
                hit = corp_df[corp_df["corp_name"].str.contains(q, na=False)]

        if hit.empty:
            raise ValueError(f"investor '{investor}' could not be mapped.")

        row = hit.iloc[0]
        return row["corp_code"], row["corp_name"], row["stock_code"]

    # ---------- periodic reports ----------
    def collect_periodic_reports(
        self,
        start_date: str,
        end_date: str,
        tickers: Optional[list[str]] = None,
        include_halfyear: bool = True,
        markets: tuple[str, ...] = ("Y", "K", "N"),
    ) -> pd.DataFrame:
        detail_types = ["A003"] + (["A002"] if include_halfyear else [])
        windows = _split_windows(start_date, end_date, max_days=90)

        rows = []
        for dty in detail_types:
            for bgn_de, end_de in windows:
                for m in markets:
                    rows.extend(
                        self._list_all_pages(
                            bgn_de=bgn_de, end_de=end_de,
                            pblntf_ty="A",
                            pblntf_detail_ty=dty,
                            corp_cls=m,
                            sort="date", sort_mth="asc",
                            page_count="100"
                        )
                    )

        if not rows:
            return pd.DataFrame(columns=[
                "corp_cls", "corp_name", "corp_code", "stock_code", "report_nm",
                "rcept_no", "flr_nm", "rcept_dt", "rm", "viewer_url"
            ])

        df = pd.DataFrame(rows)
        keep = [c for c in [
            "corp_cls", "corp_name", "corp_code", "stock_code", "report_nm",
            "rcept_no", "flr_nm", "rcept_dt", "rm"
        ] if c in df.columns]
        df = df[keep].copy()

        if "stock_code" in df.columns:
            df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
        if "rcept_no" in df.columns:
            df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])
            df = df.drop_duplicates(subset=["rcept_no"])

        if tickers is not None and "stock_code" in df.columns:
            tset = {str(t).zfill(6) for t in tickers}
            df = df[df["stock_code"].isin(tset)].copy()

        if "rcept_dt" in df.columns:
            df = df.sort_values(["rcept_dt", "stock_code"], ascending=[True, True])

        return df.reset_index(drop=True)

    # ---------- generic corp table ----------
    def _fetch_single_corp_table(
        self,
        url: str,
        corp_code: str,
        numeric_cols: Optional[list[str]] = None,
        date_col: str = "rcept_dt",
    ) -> pd.DataFrame:
        js = self._call_json(url, corp_code=str(corp_code).zfill(8))
        if js.get("status") == "013":
            return pd.DataFrame()

        df = pd.DataFrame(js.get("list", []))
        if df.empty:
            return df

        if "corp_code" in df.columns:
            df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        if "rcept_no" in df.columns:
            df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])

        if numeric_cols:
            for c in numeric_cols:
                if c in df.columns:
                    df[c] = _to_num_series(df[c])

        sort_cols = [c for c in [date_col, "rcept_no"] if c in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols, ascending=[False] * len(sort_cols))

        return df.reset_index(drop=True)

    def fetch_elestock_by_corp_code(self, corp_code: str) -> pd.DataFrame:
        return self._fetch_single_corp_table(
            BASE_ELESTOCK_URL, corp_code,
            numeric_cols=["sp_stock_lmp_cnt", "sp_stock_lmp_irds_cnt", "sp_stock_lmp_rate", "sp_stock_lmp_irds_rate"]
        )

    def fetch_majorstock_by_corp_code(self, corp_code: str) -> pd.DataFrame:
        return self._fetch_single_corp_table(
            BASE_MAJORSTOCK_URL, corp_code,
            numeric_cols=["stkqy", "stkqy_irds", "stkrt", "stkrt_irds", "ctr_stkqy", "ctr_stkrt"]
        )

    def fetch_by_tickers(
        self,
        tickers: list[str],
        kind: str,  # "elestock" | "majorstock"
        sleep_sec: float = 0.05
    ) -> tuple[pd.DataFrame, list[str]]:
        tickers_norm = sorted({str(t).zfill(6) for t in tickers})
        if not tickers_norm:
            return pd.DataFrame(), []

        corp_map = self.get_corp_codes(listed_only=True)
        m = corp_map[["corp_code", "corp_name", "stock_code"]].copy()
        m = m[m["stock_code"].isin(tickers_norm)].drop_duplicates(subset=["stock_code"])
        mapped = set(m["stock_code"].tolist())
        missing = [t for t in tickers_norm if t not in mapped]

        out = []
        for _, row in m.iterrows():
            if kind == "elestock":
                one = self.fetch_elestock_by_corp_code(row["corp_code"])
            elif kind == "majorstock":
                one = self.fetch_majorstock_by_corp_code(row["corp_code"])
            else:
                raise ValueError("kind must be 'elestock' or 'majorstock'")

            if one.empty:
                one = pd.DataFrame([{
                    "corp_code": row["corp_code"],
                    "corp_name": row["corp_name"],
                    "stock_code": row["stock_code"]
                }])
            else:
                one["stock_code"] = row["stock_code"]
                if "corp_name" not in one.columns:
                    one["corp_name"] = row["corp_name"]

            out.append(one)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        return (pd.concat(out, ignore_index=True) if out else pd.DataFrame()), missing

    @staticmethod
    def merge_latest_to_reports(
        reports_df: pd.DataFrame,
        right_df: pd.DataFrame,
        prefix: str,
        keep_cols: list[str],
    ) -> pd.DataFrame:
        if reports_df.empty or right_df.empty:
            return reports_df.copy()

        left = reports_df.copy()
        right = right_df.copy()

        left["corp_code"] = left["corp_code"].astype(str).str.zfill(8)
        right["corp_code"] = right["corp_code"].astype(str).str.zfill(8)

        if "rcept_dt" in right.columns:
            right["rcept_dt"] = pd.to_datetime(right["rcept_dt"], errors="coerce")
            right = right.sort_values(["corp_code", "rcept_dt", "rcept_no"], ascending=[True, False, False])

        right = right.drop_duplicates(subset=["corp_code"], keep="first")

        use = [c for c in ["corp_code"] + keep_cols if c in right.columns]
        right = right[use].copy()
        right = right.rename(columns={c: f"{prefix}_{c}" for c in use if c != "corp_code"})

        return left.merge(right, on="corp_code", how="left")

    # ---------- other-corp investment ----------
    def fetch_other_corp_investment_status(
        self,
        corp_code: str,
        bsns_year: int | str,
        reprt_code: str = REPORT_CODES["Y"],
    ) -> pd.DataFrame:
        js = self._call_json(
            BASE_OTRCPR_INVST_URL,
            corp_code=str(corp_code).zfill(8),
            bsns_year=str(bsns_year),
            reprt_code=str(reprt_code),
        )
        if js.get("status") == "013":
            return pd.DataFrame()

        df = pd.DataFrame(js.get("list", []))
        if df.empty:
            return df

        for c in [
            "frst_acqs_amount", "frst_acqs_qy",
            "incrs_dcrs_acqs_amount", "incrs_dcrs_acqs_qy",
            "trmend_blce_acntbk_amount", "trmend_blce_qy",
            "recent_bsns_year_fnnr_sttus_tot_assets",
        ]:
            if c in df.columns:
                df[c] = _to_num_series(df[c])

        if "rcept_no" in df.columns:
            df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])

        return df

    def collect_investor_all_investments(
        self,
        investor: str,
        start_year: int,
        end_year: int,
        report_keys: tuple[str, ...] = ("Q1", "H1", "Q3", "Y"),
    ) -> pd.DataFrame:
        corp_code, corp_name, stock_code = self.resolve_investor(investor)

        frames = []
        for y in range(start_year, end_year + 1):
            for rk in report_keys:
                df = self.fetch_other_corp_investment_status(corp_code, y, REPORT_CODES[rk])
                if df.empty:
                    continue
                df = df.copy()
                df["bsns_year"] = y
                df["report_key"] = rk
                df["reprt_code"] = REPORT_CODES[rk]
                df["investor_corp_code"] = corp_code
                df["investor_corp_name"] = corp_name
                df["investor_stock_code"] = stock_code
                frames.append(df)

        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        order_map = {"Q1": 1, "H1": 2, "Q3": 3, "Y": 4}
        out["report_order"] = out["report_key"].map(order_map).fillna(99)

        return out.sort_values(["bsns_year", "report_order"], ascending=[False, False]).reset_index(drop=True)

    # ---------- stock decision API wrappers ----------
    def fetch_other_corp_stock_decisions(self, corp_code: str, bgn_de: str, end_de: str) -> pd.DataFrame:
        params = {
            "corp_code": str(corp_code).zfill(8),
            "bgn_de": _norm_yyyymmdd(bgn_de),
            "end_de": _norm_yyyymmdd(end_de),
        }
        js_inh = self._call_json(BASE_INH_DECISION_URL, **params)
        js_trf = self._call_json(BASE_TRF_DECISION_URL, **params)

        rows = []
        if js_inh.get("status") == "000":
            rows += [{**x, "_event_type": "INH"} for x in js_inh.get("list", [])]
        if js_trf.get("status") == "000":
            rows += [{**x, "_event_type": "TRF"} for x in js_trf.get("list", [])]

        df = pd.DataFrame(rows)
        if not df.empty and "rcept_no" in df.columns:
            df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])
        return df

    # ---------- transfer: major/list ----------
    def fetch_transfer_major(self, corp_code: str, bgn_de: str, end_de: str) -> pd.DataFrame:
        js = self._call_json(
            BASE_TRF_DECISION_URL,
            corp_code=str(corp_code).zfill(8),
            bgn_de=_norm_yyyymmdd(bgn_de),
            end_de=_norm_yyyymmdd(end_de),
        )
        if js.get("status") == "013":
            return pd.DataFrame()

        df = pd.DataFrame(js.get("list", []))
        if df.empty:
            return df

        for c in TRF_NUM_COLS:
            if c in df.columns:
                df[c] = _to_num_series(df[c])

        if "rcept_no" in df.columns:
            df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])

        df = _ensure_cols(
            df,
            ["rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name", "report_nm", "flr_nm", "rm"]
            + TRF_DETAIL_COLS + ["viewer_url"]
        )
        df["source"] = "MAJOR_API"
        return df.sort_values("rcept_no", ascending=False).reset_index(drop=True)

    def fetch_transfer_list(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_tys: tuple[str, ...] = ("B", "I"),
    ) -> pd.DataFrame:
        corp_code = str(corp_code).zfill(8)
        chunks = []

        for ty in pblntf_tys:
            rows = self._list_all_pages(
                corp_code=corp_code,
                bgn_de=_norm_yyyymmdd(bgn_de),
                end_de=_norm_yyyymmdd(end_de),
                pblntf_ty=ty,
                sort="date",
                sort_mth="desc",
                page_count="100",
                last_reprt_at="N",
            )
            if not rows:
                continue

            one = pd.DataFrame(rows)
            if "report_nm" in one.columns:
                one = one[one["report_nm"].astype(str).str.contains(TRANSFER_TITLE_REGEX, regex=True, na=False)].copy()
            if one.empty:
                continue

            one["pblntf_ty"] = ty
            if "rcept_no" in one.columns:
                one["viewer_url"] = _viewer_url_from_rcept(one["rcept_no"])

            one = _ensure_cols(
                one,
                ["rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name", "report_nm", "flr_nm", "rm", "pblntf_ty", "viewer_url"]
            )
            one["corp_code"] = one["corp_code"].astype(str).str.zfill(8)
            one["source"] = one["pblntf_ty"].astype(str).map(lambda x: f"LIST_{x}")
            chunks.append(one)

        if not chunks:
            return pd.DataFrame()

        df = pd.concat(chunks, ignore_index=True)
        df = df.drop_duplicates(subset=["rcept_no"], keep="first")
        return df.sort_values("rcept_no", ascending=False).reset_index(drop=True)

    # ============================================================
    # ======= viewer/document extraction (1번 코드 로직) ==========
    # ============================================================
    @staticmethod
    def _decode_bytes_auto(b: bytes) -> str:
        for enc in ("utf-8", "cp949", "euc-kr"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")

    @staticmethod
    def _extract_rcp_no(url_or_rcp: str) -> str:
        s = str(url_or_rcp).strip()
        m = re.search(r"rcpNo=(\d{14})", s)
        if m:
            return m.group(1)
        m2 = re.fullmatch(r"\d{14}", s)
        if m2:
            return s
        raise ValueError(f"rcept_no(14자리) 또는 rcpNo URL 형식이 아닙니다: {s}")

    @staticmethod
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

    @staticmethod
    def _pick_value_from_lines(lines: List[str], aliases: List[str]) -> Optional[str]:
        for i, ln in enumerate(lines):
            if any(a in ln for a in aliases):
                m = re.split(r"[:：]\s*", ln, maxsplit=1)
                if len(m) == 2 and m[1].strip():
                    rhs = m[1].strip()
                    if rhs not in TRF_PLACEHOLDER_VALUES:
                        return rhs

                for j in range(i + 1, min(i + 8, len(lines))):
                    cand = lines[j].strip(" \t:-")
                    if not cand:
                        continue
                    if any(a in cand for a in aliases):
                        continue
                    if cand in TRF_PLACEHOLDER_VALUES:
                        continue
                    return cand
        return None


    @staticmethod
    def _extract_kv_from_html(html_text: str) -> Dict[str, str]:
        soup = BeautifulSoup(html_text, "html.parser")
        for t in soup(["script", "style"]):
            t.decompose()

        kv = {}
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

    def _extract_fields_from_html_text(self, html_text: str) -> Dict[str, object]:
        out = {c: pd.NA for c in TRF_VIEW_OUT_COLS}

        kv = self._extract_kv_from_html(html_text)
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text("\n", strip=True)
        lines = self._clean_lines_from_text(text)

        for field, aliases in TRF_LABEL_MAP.items():
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
                val = self._pick_value_from_lines(lines, aliases)

            if val is not None and str(val).strip() != "":
                out[field] = str(val).strip()

        # 숫자 보정
        for c in ["trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt", "attrf_owstkcnt", "attrf_eqrt"]:
            out[c] = _to_num_scalar(out.get(c))

        # 날짜 보정
        out["bddd"] = _norm_date_any(out.get("bddd"))
        return out

    @staticmethod
    def _find_viewdoc_candidates(main_html: str) -> List[Dict[str, str]]:
        pattern = re.compile(
            r"viewDoc\(\s*['\"](?P<rcpNo>\d{14})['\"]\s*,\s*['\"](?P<dcmNo>\d+)['\"]\s*,\s*['\"](?P<eleId>\d+)['\"]\s*,\s*['\"](?P<offset>\d+)['\"]\s*,\s*['\"](?P<length>\d+)['\"]\s*,\s*['\"](?P<dtd>[^'\"]+)['\"]\s*\)",
            re.IGNORECASE
        )

        cands: List[Dict[str, str]] = []
        for m in pattern.finditer(main_html):
            c = m.groupdict()
            s, e = m.span()
            ctx = main_html[max(0, s - 200): min(len(main_html), e + 200)]
            c["_ctx"] = ctx
            cands.append(c)
        return cands

    @staticmethod
    def _choose_best_candidate(
        cands: List[Dict[str, str]],
        keyword: str = "타법인주식및출자증권처분결정"
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

    def _fetch_viewer_html_by_rcpno(self, rcp_no: str) -> Tuple[Optional[str], str]:
        main_resp = self.session.get(MAIN_URL, params={"rcpNo": rcp_no}, timeout=self.timeout)
        main_resp.raise_for_status()
        main_html = main_resp.text

        cands = self._find_viewdoc_candidates(main_html)
        best = self._choose_best_candidate(cands)

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
        vresp = self.session.get(VIEWER_URL, params=params, timeout=self.timeout)
        vresp.raise_for_status()
        return vresp.text, "VIEWER_HTML"

    @staticmethod
    def _extract_fields_from_xml_tags(xml_text: str) -> Dict[str, object]:
        wanted = {
            "iscmp_cmpnm", "trfdtl_stkcnt", "trfdtl_trfprc", "trfdtl_tast", "trfdtl_ecpt",
            "attrf_owstkcnt", "attrf_eqrt", "trf_pp", "trf_prd", "dlptn_cmpnm", "bddd"
        }
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

    def _parse_document_zip_best(self, raw_zip_bytes: bytes) -> Dict[str, object]:
        best = {c: pd.NA for c in TRF_VIEW_OUT_COLS}
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

            txt = self._decode_bytes_auto(raw)

            if low.endswith((".html", ".htm", ".xhtml")):
                cand = self._extract_fields_from_html_text(txt)
            else:
                cand = self._extract_fields_from_html_text(txt)
                tag_cand = self._extract_fields_from_xml_tags(txt)
                for k, v in tag_cand.items():
                    if k in cand and pd.isna(cand[k]) and not pd.isna(v):
                        cand[k] = v

            score = sum(1 for k in TRF_VIEW_CORE_FIELDS if not pd.isna(cand.get(k)))
            if score > best_score:
                best_score = score
                best = cand

        return best

    def extract_transfer_decision_from_viewer_url(
        self,
        viewer_url: str,
        use_document_fallback: bool = True,
        verbose: bool = False
    ) -> Dict[str, object]:
        rcp_no = self._extract_rcp_no(viewer_url)

        base_cols = [
            "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
            "report_nm", "flr_nm", "pblntf_ty", "source", "viewer_url"
        ] + TRF_DETAIL_COLS

        row = {c: pd.NA for c in base_cols}
        row["rcept_no"] = rcp_no
        row["rcept_dt"] = _norm_date_any(rcp_no[:8])
        row["viewer_url"] = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
        row["report_nm"] = "타법인주식및출자증권처분결정"
        row["source"] = "INIT"

        viewer_html_cache = None

        # Step 1) viewer html
        try:
            if verbose:
                print(f"[1/2] viewer HTML 파싱 시작: rcp_no={rcp_no}")
            viewer_html, src = self._fetch_viewer_html_by_rcpno(rcp_no)
            viewer_html_cache = viewer_html

            if viewer_html:
                parsed = self._extract_fields_from_html_text(viewer_html)
                for k, v in parsed.items():
                    if k in row and pd.isna(row[k]) and not pd.isna(v):
                        row[k] = v
                row["source"] = src

            if verbose:
                got = sum(1 for k in TRF_VIEW_CORE_FIELDS if not pd.isna(row.get(k)))
                print(f"[1/2] viewer HTML core filled = {got}/{len(TRF_VIEW_CORE_FIELDS)}")
        except Exception as e:
            if verbose:
                print(f"[WARN] viewer HTML 파싱 실패: {e}")

        # Step 2) document.xml fallback
        need_doc = any(pd.isna(row.get(k)) for k in TRF_VIEW_CORE_FIELDS)
        if need_doc and use_document_fallback:
            try:
                if verbose:
                    print("[2/2] document.xml fallback 파싱 시작")
                resp = self.session.get(
                    BASE_DOCUMENT_URL,
                    params={"crtfc_key": self.api_key, "rcept_no": rcp_no},
                    timeout=self.timeout
                )
                resp.raise_for_status()
                parsed_doc = self._parse_document_zip_best(resp.content)
                for k, v in parsed_doc.items():
                    if k in row and pd.isna(row[k]) and not pd.isna(v):
                        row[k] = v
                row["source"] = (str(row["source"]) + "+DOC").strip("+")
            except Exception as e:
                if verbose:
                    print(f"[WARN] document.xml 파싱 실패: {e}")
                row["source"] = (str(row["source"]) + "+DOC_FAIL").strip("+")

        # 헤더값 정리
        for c in TRF_DETAIL_COLS:
            if c in row and self._is_trf_placeholder(row[c], c):
                row[c] = pd.NA

        # iscmp_cmpnm 보조 추론
        if pd.isna(row.get("iscmp_cmpnm")) and viewer_html_cache:
            inferred = self._infer_iscmp_cmpnm_from_text(
                viewer_html_cache,
                exclude=[str(row.get("corp_name", ""))]
            )
            if not pd.isna(inferred):
                row["iscmp_cmpnm"] = inferred

        # 후처리
        for c in TRF_NUM_COLS:
            if c in row:
                row[c] = _to_num_scalar(row.get(c))
        row["bddd"] = _norm_date_any(row.get("bddd"))

        return row


    def enrich_transfer_list_with_viewer_extract(
        self,
        list_df: pd.DataFrame,
        use_document_fallback: bool = True,
        max_parse: int = 300,
        sleep_sec: float = 0.03,
        verbose: bool = False,
    ) -> pd.DataFrame:
        if list_df.empty or "rcept_no" not in list_df.columns:
            return list_df

        out = _ensure_cols(list_df.copy(), ["viewer_url", "source"] + TRF_DETAIL_COLS)
        if out["viewer_url"].isna().all():
            out["viewer_url"] = _viewer_url_from_rcept(out["rcept_no"])

        # list_df는 상세값이 거의 없으므로 핵심값 누락건 우선 파싱
        if all(c in out.columns for c in TRF_VIEW_CORE_FIELDS):
            need_mask = out[TRF_VIEW_CORE_FIELDS].isna().any(axis=1)
        else:
            need_mask = pd.Series([True] * len(out), index=out.index)

        targets = out.loc[need_mask, "rcept_no"].dropna().astype(str).unique().tolist()[:max_parse]
        if not targets:
            return out

        parsed_rows = []
        for rcp in targets:
            vu = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
            parsed = self.extract_transfer_decision_from_viewer_url(
                viewer_url=vu,
                use_document_fallback=use_document_fallback,
                verbose=verbose
            )
            parsed_rows.append(parsed)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        ext = pd.DataFrame(parsed_rows).drop_duplicates(subset=["rcept_no"])
        if ext.empty:
            return out

        out = out.merge(ext, on="rcept_no", how="left", suffixes=("", "_ext"))

        merge_cols = ["rcept_dt", "corp_cls", "corp_code", "corp_name", "report_nm", "flr_nm", "viewer_url"] + TRF_DETAIL_COLS
        for c in merge_cols:
            ce = f"{c}_ext"
            if ce in out.columns:
                if c in out.columns:
                    out[c] = out[c].combine_first(out[ce])
                else:
                    out[c] = out[ce]
                out.drop(columns=[ce], inplace=True)

        # source 정리
        if "source_ext" in out.columns:
            base_src = out["source"].fillna("LIST").astype(str)
            ext_src = out["source_ext"].fillna("").astype(str)
            has_ext = ext_src.str.len() > 0
            out.loc[has_ext, "source"] = base_src.loc[has_ext] + "+" + ext_src.loc[has_ext]
            out["source"] = out["source"].str.replace(r"\++", "+", regex=True).str.strip("+")
            out.drop(columns=["source_ext"], inplace=True)

        for c in TRF_NUM_COLS:
            if c in out.columns:
                out[c] = _to_num_series(out[c])

        return out

    # ---------- transfer: final API ----------
    def fetch_transfer_by_investor_full(
        self,
        investor: str,  # corp_code(8) | stock_code(6) | corp_name
        bgn_de: str,
        end_de: str,
        investee_keyword: Optional[str] = None,
        include_exchange_fallback: bool = True,
        parse_document_fallback: bool = True,
        max_doc_parse: int = 300,
        compact: bool = True,
        verbose: bool = False,
    ) -> pd.DataFrame:
        """
        투자자 기준 타법인주식/출자증권 처분결정 데이터를 통합 조회
        1) major API
        2) list API(B/I) + viewer/document 추출 보강
        3) major/list 병합
        4) (옵션) 문서 fallback 추가 보강
        """

        # ------------------------------------------------------------
        # 0) investor resolve
        # ------------------------------------------------------------
        corp_code, investor_name, investor_stock = self.resolve_investor(investor)
        corp_code = str(corp_code).zfill(8)

        # ------------------------------------------------------------
        # 1) fetch major + list
        # ------------------------------------------------------------
        major = self.fetch_transfer_major(corp_code, bgn_de, end_de)

        if include_exchange_fallback:
            list_df = self.fetch_transfer_list(
                corp_code, bgn_de, end_de, pblntf_tys=("B", "I")
            )
        else:
            list_df = pd.DataFrame()

        # list는 상세필드가 거의 비어있으므로 viewer/document로 먼저 보강
        if not list_df.empty:
            list_df = self.enrich_transfer_list_with_viewer_extract(
                list_df=list_df,
                use_document_fallback=parse_document_fallback,
                max_parse=max_doc_parse,
                sleep_sec=0.03,
                verbose=verbose,
            )
            # "회사명(국적)" 같은 헤더성 값 -> NA 처리
            list_df = self._sanitize_transfer_detail_df(list_df)

        # early return
        if list_df.empty and major.empty:
            if compact:
                return pd.DataFrame(columns=TRF_OUTPUT_COLS)
            return pd.DataFrame()

        # ------------------------------------------------------------
        # 2) base build (list 우선, major 보강)
        # ------------------------------------------------------------
        if list_df.empty:
            out = major.copy()
            if "pblntf_ty" not in out.columns:
                out["pblntf_ty"] = pd.NA
            if "source" not in out.columns:
                out["source"] = "MAJOR_API"
        else:
            out = list_df.copy()

            if not major.empty:
                # core 메타 보강
                core_cols = [
                    "rcept_no", "rcept_dt", "corp_cls", "corp_code", "corp_name",
                    "report_nm", "flr_nm", "rm", "viewer_url"
                ]
                m_core = major[[c for c in core_cols if c in major.columns]] \
                    .drop_duplicates(subset=["rcept_no"]) \
                    .copy()

                out = out.merge(m_core, on="rcept_no", how="outer", suffixes=("", "_api"))
                for c in core_cols:
                    ca = f"{c}_api"
                    if ca in out.columns:
                        if c in out.columns:
                            out[c] = out[c].combine_first(out[ca])
                        else:
                            out[c] = out[ca]
                        out.drop(columns=[ca], inplace=True)

                # detail 보강 (list값이 NA일 때 major로 채움)
                m_detail_cols = ["rcept_no"] + [c for c in TRF_DETAIL_COLS if c in major.columns]
                m_detail = major[m_detail_cols].drop_duplicates(subset=["rcept_no"]).copy()

                out = out.merge(m_detail, on="rcept_no", how="left", suffixes=("", "_m"))
                for c in TRF_DETAIL_COLS:
                    cm = f"{c}_m"
                    if cm in out.columns:
                        if c in out.columns:
                            out[c] = out[c].combine_first(out[cm])
                        else:
                            out[c] = out[cm]
                        out.drop(columns=[cm], inplace=True)

                if "source" in out.columns:
                    out["source"] = out["source"].fillna("MAJOR_API")
                else:
                    out["source"] = "MAJOR_API"

        # 병합 후에도 placeholder 정리 (combine_first 방해값 제거)
        out = self._sanitize_transfer_detail_df(out)

        # ------------------------------------------------------------
        # 3) investee keyword filter
        # ------------------------------------------------------------
        if investee_keyword and "iscmp_cmpnm" in out.columns:
            out = out[
                out["iscmp_cmpnm"].astype(str).str.contains(investee_keyword, case=False, na=False)
            ].copy()

        # # ------------------------------------------------------------
        # # 4) document fallback (최종 누락건 추가 보강)
        # # ------------------------------------------------------------
        # if parse_document_fallback:
        #     out = self.enrich_transfer_with_documents(
        #         out,
        #         max_docs=max_doc_parse,
        #         sleep_sec=0.03
        #     )
        #     out = self._sanitize_transfer_detail_df(out)

        # ------------------------------------------------------------
        # 5) normalize / typing / meta
        # ------------------------------------------------------------
        if "corp_code" in out.columns:
            out["corp_code"] = out["corp_code"].astype(str).str.zfill(8)

        if "rcept_dt" in out.columns:
            dt = pd.to_datetime(out["rcept_dt"], errors="coerce")
            out["rcept_dt"] = dt.dt.strftime("%Y-%m-%d").where(dt.notna(), out["rcept_dt"].astype(str))

        if "viewer_url" not in out.columns:
            out["viewer_url"] = _viewer_url_from_rcept(out["rcept_no"])
        else:
            miss_vu = out["viewer_url"].isna() | (out["viewer_url"].astype(str).str.strip() == "")
            out.loc[miss_vu, "viewer_url"] = _viewer_url_from_rcept(out.loc[miss_vu, "rcept_no"])

        for c in TRF_NUM_COLS:
            if c in out.columns:
                out[c] = _to_num_series(out[c])

        # investor meta
        out["investor_corp_code"] = corp_code
        out["investor_corp_name"] = investor_name
        out["investor_stock_code"] = investor_stock

        if verbose:
            print("corp code:", corp_code)
            print("corp name:", investor_name)
            print("stock code:", investor_stock)

        # 정렬/중복제거
        if "rcept_no" in out.columns:
            out = out.sort_values("rcept_no", ascending=False) \
                    .drop_duplicates(subset=["rcept_no"], keep="first")

        # compact output
        if compact:
            out = _ensure_cols(out, TRF_OUTPUT_COLS)[TRF_OUTPUT_COLS].copy()

        return out.reset_index(drop=True)


    # ---------- estkRs ----------
    def fetch_estk_rs(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
    ) -> Dict[str, Any]:
        corp_code = str(corp_code).zfill(8)
        bgn_de = _norm_yyyymmdd(bgn_de)
        end_de = _norm_yyyymmdd(end_de)

        js = self._call_json(
            BASE_ESTK_RS_URL,
            corp_code=corp_code,
            bgn_de=bgn_de,
            end_de=end_de,
        )

        status = js.get("status", "")
        message = js.get("message", "")

        if status == "013":
            return {
                "status": status,
                "message": message,
                "tables": {},
                "combined": pd.DataFrame(),
            }

        # list / list1 / list2 ... 구조 수집
        list_keys = [k for k, v in js.items() if k.startswith("list") and isinstance(v, list)]

        tables: Dict[str, pd.DataFrame] = {}
        combined_chunks = []

        for lk in list_keys:
            rows = js.get(lk, [])
            if not rows:
                continue

            df = pd.DataFrame(rows)
            if df.empty:
                continue

            if "corp_code" in df.columns:
                df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
            if "rcept_no" in df.columns:
                df["viewer_url"] = _viewer_url_from_rcept(df["rcept_no"])

            for dc in ["sbd", "pymd", "sband", "asand", "asstd"]:
                if dc in df.columns:
                    df[dc] = _format_yyyymmdd_series(df[dc])

            numeric_candidates = [
                c for c in df.columns
                if re.search(r"(cnt|amt|prc|ta|fv|exprc|stkcnt|slta|udtamt|grtcnt)$", c)
            ]
            numeric_candidates += [c for c in ESTK_NUM_CANDIDATES_EXPLICIT if c in df.columns]
            numeric_candidates = sorted(set(numeric_candidates))
            for c in numeric_candidates:
                conv = _to_num_series(df[c])
                if conv.notna().any():
                    df[c] = conv

            gname = _infer_estk_group_name(df, fallback=lk)
            tables[gname] = df

            dfx = df.copy()
            dfx["group_name"] = gname
            combined_chunks.append(dfx)

        combined = pd.concat(combined_chunks, ignore_index=True) if combined_chunks else pd.DataFrame()

        if not combined.empty:
            sort_cols = [c for c in ["rcept_no", "group_name"] if c in combined.columns]
            if sort_cols:
                ascending = [False if c == "rcept_no" else True for c in sort_cols]
                combined = combined.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)

        return {
            "status": status,
            "message": message,
            "tables": tables,
            "combined": combined,
        }


# ------------------------------------------------------------
# Example
# ------------------------------------------------------------
if __name__ == "__main__":
    import dotenv

    dotenv.load_dotenv()
    API_KEY = os.getenv("OPENDART_API_KEY", "").strip()
    if not API_KEY:
        raise RuntimeError("Set OPENDART_API_KEY in environment variable.")

    dart = OpenDartClient(api_key=API_KEY)

    # transfer
    df_trf = dart.fetch_transfer_by_investor_full(
        investor="베뉴지",
        bgn_de="20210101",
        end_de="20260213",
        include_exchange_fallback=True,
        parse_document_fallback=True,
        max_doc_parse=200,
        compact=True,
        verbose=True,
    )
    print(df_trf.head(20))

    os.makedirs("data", exist_ok=True)
    df_trf.to_csv("data/total_investing.csv", index=False, encoding="utf-8-sig")
