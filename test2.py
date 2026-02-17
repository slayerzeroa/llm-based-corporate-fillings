# -*- coding: utf-8 -*-
"""
run_fetch_transfer_list.py

실행 전:
1) pip install requests pandas python-dotenv
2) .env 파일에 OPENDART_API_KEY=발급키 입력

실행:
python run_fetch_transfer_list.py
"""

from __future__ import annotations

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


# -----------------------------
# Client
# -----------------------------
@dataclass
class OpenDartClient:
    api_key: str
    timeout: int = 30
    max_retries: int = 5
    base_sleep: float = 0.8
    session: requests.Session = field(default_factory=requests.Session)

    def _call_json(self, url: str, **params) -> dict:
        payload = {"crtfc_key": self.api_key, **params}

        for attempt in range(self.max_retries):
            r = self.session.get(url, params=payload, timeout=self.timeout)
            r.raise_for_status()
            js = r.json()

            st = js.get("status", "")
            if st in ("000", "013"):  # 000 정상, 013 데이터 없음
                return js
            if st == "020":  # 호출 제한
                time.sleep(self.base_sleep * (2 ** attempt))
                continue

            raise RuntimeError(
                f"DART API error: status={st}, message={js.get('message')}, url={url}, params={params}"
            )

        raise RuntimeError(f"Request retries exhausted: {url}")

    def _list_all_pages(self, **params) -> list[dict]:
        first = self._call_json(BASE_LIST_URL, page_no="1", **params)
        if first.get("status") == "013":
            return []

        rows = list(first.get("list", []))
        total_page = int(first.get("total_page", 1) or 1)

        for p in range(2, total_page + 1):
            js = self._call_json(BASE_LIST_URL, page_no=str(p), **params)
            st = js.get("status", "")
            if st == "000":
                rows.extend(js.get("list", []))
            elif st == "013":
                break
            else:
                break

        return rows

    def fetch_transfer_list(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_tys: tuple[str, ...] = ("B", "I"),
    ) -> pd.DataFrame:
        """
        투자자(corp_code) 기준으로 list API에서
        '타법인주식및출자증권 처분/양도결정' 공시 목록만 가져옴.
        """
        corp_code = str(corp_code).zfill(8)
        bgn_de = _norm_yyyymmdd(bgn_de)
        end_de = _norm_yyyymmdd(end_de)

        chunks = []

        for ty in pblntf_tys:
            rows = self._list_all_pages(
                corp_code=corp_code,
                bgn_de=bgn_de,
                end_de=end_de,
                pblntf_ty=ty,        # B: 주요사항보고, I: 거래소공시 등
                sort="date",
                sort_mth="desc",
                page_count="100",
                last_reprt_at="N",
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


# -----------------------------
# Standalone runner
# -----------------------------
def fetch_transfer_list_standalone(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    pblntf_tys: tuple[str, ...] = ("B", "I"),
) -> pd.DataFrame:
    client = OpenDartClient(api_key=api_key)
    return client.fetch_transfer_list(
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        pblntf_tys=pblntf_tys,
    )


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    API_KEY = os.getenv("OPENDART_API_KEY", "").strip()
    if not API_KEY:
        raise RuntimeError("OPENDART_API_KEY가 비어있습니다. .env 확인하세요.")

    # 예시: 베뉴지(00267906), 기간 지정
    df = fetch_transfer_list_standalone(
        api_key=API_KEY,
        corp_code="00267906",
        bgn_de="20210101",
        end_de="20260213",
    )

    print(f"rows={len(df)}")
    print(df.head(20).to_string(index=False))

    os.makedirs("data", exist_ok=True)
    df.to_csv("data/transfer_list_only.csv", index=False, encoding="utf-8-sig")
    print("saved -> data/transfer_list_only.csv")
