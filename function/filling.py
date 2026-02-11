# -*- coding: utf-8 -*-
"""
한국 주식 분기(및 반기) 보고서 수집기 - OpenDART
필수: pip install requests pandas
환경변수: OPENDART_API_KEY=발급받은_키
"""

import os
import io
import time
import zipfile
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
BASE_CORPCODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"


def _norm_yyyymmdd(s: str) -> str:
    s = str(s).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"날짜 형식 오류: {s} (YYYYMMDD 또는 YYYY-MM-DD)")
    return s


def _split_90d_windows(start_date: str, end_date: str, max_days: int = 90) -> List[Tuple[str, str]]:
    """corp_code 없이 조회 시 3개월 제한 대응용 윈도우 분할"""
    s = datetime.strptime(_norm_yyyymmdd(start_date), "%Y%m%d")
    e = datetime.strptime(_norm_yyyymmdd(end_date), "%Y%m%d")
    if s > e:
        raise ValueError("start_date가 end_date보다 늦습니다.")
    windows = []
    cur = s
    while cur <= e:
        nxt = min(cur + timedelta(days=max_days - 1), e)
        windows.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cur = nxt + timedelta(days=1)
    return windows


def get_corp_codes(api_key: str) -> pd.DataFrame:
    """
    corpCode.xml(Zip) 다운로드 후 파싱
    반환 컬럼: corp_code, corp_name, stock_code, modify_date
    """
    params = {"crtfc_key": api_key}
    r = requests.get(BASE_CORPCODE_URL, params=params, timeout=30)
    r.raise_for_status()

    # 응답은 zip 바이너리
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        # 보통 CORPCODE.xml 1개 파일
        xml_name = zf.namelist()[0]
        xml_bytes = zf.read(xml_name)

    root = ET.fromstring(xml_bytes)
    rows = []
    for item in root.findall("list"):
        rows.append({
            "corp_code": (item.findtext("corp_code") or "").strip(),
            "corp_name": (item.findtext("corp_name") or "").strip(),
            "stock_code": (item.findtext("stock_code") or "").strip(),
            "modify_date": (item.findtext("modify_date") or "").strip(),
        })

    df = pd.DataFrame(rows)
    # 상장사만 (stock_code 6자리)
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    df = df[df["stock_code"].str.match(r"^\d{6}$", na=False)].copy()
    return df


def _call_list_api(api_key: str, params: dict, max_retries: int = 5, base_sleep: float = 0.8) -> dict:
    """
    list.json 공통 호출 + 요청제한(020) 재시도
    """
    p = {"crtfc_key": api_key, **params}
    for attempt in range(max_retries):
        r = requests.get(BASE_LIST_URL, params=p, timeout=30)
        r.raise_for_status()
        js = r.json()
        status = js.get("status", "")
        # 000 정상, 013 데이터 없음, 020 요청 제한 초과
        if status == "000":
            return js
        if status == "013":
            return js
        if status == "020":
            sleep_sec = base_sleep * (2 ** attempt)
            time.sleep(sleep_sec)
            continue
        # 그 외 에러
        raise RuntimeError(f"DART API 오류 status={status}, message={js.get('message')}, params={params}")

    raise RuntimeError("요청 제한(020) 재시도 초과")


def fetch_reports_window(
    api_key: str,
    bgn_de: str,
    end_de: str,
    pblntf_detail_ty: str,
    corp_cls: Optional[str] = None,
    page_count: int = 100
) -> List[dict]:
    """
    특정 기간/상세유형/시장에 대해 page 순회
    pblntf_ty='A' : 정기공시
    pblntf_detail_ty='A003' : 분기보고서, 'A002' : 반기보고서
    """
    bgn_de = _norm_yyyymmdd(bgn_de)
    end_de = _norm_yyyymmdd(end_de)

    common = {
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": "A",
        "pblntf_detail_ty": pblntf_detail_ty,
        "sort": "date",
        "sort_mth": "asc",
        "page_count": str(page_count),
    }
    if corp_cls:
        common["corp_cls"] = corp_cls

    # 1페이지 호출해서 total_page 확인
    first = _call_list_api(api_key, {**common, "page_no": "1"})
    if first.get("status") == "013":
        return []

    total_page = int(first.get("total_page", 1) or 1)
    out = list(first.get("list", []))

    for p in range(2, total_page + 1):
        js = _call_list_api(api_key, {**common, "page_no": str(p)})
        if js.get("status") == "000":
            out.extend(js.get("list", []))
        elif js.get("status") == "013":
            break

    return out


def collect_quarterly_reports(
    api_key: str,
    start_date: str,
    end_date: str,
    tickers: Optional[List[str]] = None,
    include_halfyear: bool = True,
    markets: Tuple[str, ...] = ("Y", "K", "N"),   # 유가/코스닥/코넥스
) -> pd.DataFrame:
    """
    분기(필수) + 반기(선택) 보고서 수집
    - tickers=None: 전체 시장 대상
    - tickers=['005930', ...]: 해당 종목만 최종 필터
    """
    detail_types = ["A003"] + (["A002"] if include_halfyear else [])
    windows = _split_90d_windows(start_date, end_date, max_days=90)

    records = []
    for dty in detail_types:
        for bgn_de, end_de in windows:
            for m in markets:
                recs = fetch_reports_window(
                    api_key=api_key,
                    bgn_de=bgn_de,
                    end_de=end_de,
                    pblntf_detail_ty=dty,
                    corp_cls=m,
                    page_count=100
                )
                records.extend(recs)

    if not records:
        return pd.DataFrame(columns=[
            "corp_cls", "corp_name", "corp_code", "stock_code", "report_nm",
            "rcept_no", "flr_nm", "rcept_dt", "rm", "viewer_url"
        ])

    df = pd.DataFrame(records)

    # 컬럼 정리
    keep_cols = [c for c in [
        "corp_cls", "corp_name", "corp_code", "stock_code", "report_nm",
        "rcept_no", "flr_nm", "rcept_dt", "rm"
    ] if c in df.columns]
    df = df[keep_cols].copy()

    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    df["viewer_url"] = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + df["rcept_no"].astype(str)

    # 종목 필터
    if tickers is not None:
        tickers_norm = {str(x).zfill(6) for x in tickers}
        df = df[df["stock_code"].isin(tickers_norm)].copy()

    # 중복 제거/정렬
    if "rcept_no" in df.columns:
        df = df.drop_duplicates(subset=["rcept_no"])
    if "rcept_dt" in df.columns:
        df = df.sort_values(["rcept_dt", "stock_code"], ascending=[True, True])

    return df.reset_index(drop=True)


if __name__ == "__main__":
    # 1) API 키 준비
    API_KEY = os.getenv("OPENDART_API_KEY", "").strip()
    if not API_KEY:
        raise RuntimeError("환경변수 OPENDART_API_KEY를 설정하세요.")

    # 2) 예시 A: 특정 종목(삼성전자/하이닉스) 2025년 분기+반기
    df_sel = collect_quarterly_reports(
        api_key=API_KEY,
        start_date="20250101",
        end_date="20251231",
        tickers=["005930", "000660"],
        include_halfyear=True,   # False면 분기보고서(A003)만
    )
    print("선택 종목 수집 건수:", len(df_sel))
    print(df_sel.head(10))
    df_sel.to_csv("quarter_reports_selected.csv", index=False, encoding="utf-8-sig")

    # 3) 예시 B: 전체 상장사 최근 기간 분기만
    # df_all = collect_quarterly_reports(
    #     api_key=API_KEY,
    #     start_date="20251001",
    #     end_date="20260211",
    #     tickers=None,
    #     include_halfyear=False,
    # )
    # print("전체 시장 수집 건수:", len(df_all))
    # df_all.to_csv("quarter_reports_all.csv", index=False, encoding="utf-8-sig")
