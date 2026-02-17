from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from io import StringIO
from typing import Optional

import pandas as pd
import requests

try:
    from pykrx import stock
except Exception:  # pragma: no cover - import error branch
    stock = None


def _norm_yyyymmdd(value: str) -> str:
    raw = str(value).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {value} (YYYYMMDD or YYYY-MM-DD)")
    return raw


@dataclass
class KrxApiClient:
    lookback_days: int = 14
    kind_corp_list_url: str = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"

    def _candidate_dates(self, as_of: Optional[str]) -> list[str]:
        if as_of:
            base = datetime.strptime(_norm_yyyymmdd(as_of), "%Y%m%d")
        else:
            base = datetime.now()
        return [(base - timedelta(days=i)).strftime("%Y%m%d") for i in range(self.lookback_days + 1)]

    def _fetch_kospi_from_pykrx(self, as_of: Optional[str] = None) -> pd.DataFrame:
        if stock is None:
            return pd.DataFrame(columns=["stock_code", "stock_name", "market", "as_of_date"])

        tickers: list[str] = []
        used_date: Optional[str] = None

        for ymd in self._candidate_dates(as_of):
            try:
                one = stock.get_market_ticker_list(date=ymd, market="KOSPI")
            except Exception:
                one = []
            if one:
                tickers = sorted({str(x).zfill(6) for x in one})
                used_date = ymd
                break

        if not tickers or not used_date:
            return pd.DataFrame(columns=["stock_code", "stock_name", "market", "as_of_date"])

        rows = []
        for ticker in tickers:
            try:
                name = stock.get_market_ticker_name(ticker)
            except Exception:
                name = None
            rows.append(
                {
                    "stock_code": ticker,
                    "stock_name": name if name else pd.NA,
                    "market": "KOSPI",
                    "as_of_date": f"{used_date[:4]}-{used_date[4:6]}-{used_date[6:8]}",
                }
            )

        return pd.DataFrame(rows, columns=["stock_code", "stock_name", "market", "as_of_date"])

    def _fetch_kospi_from_kind(self) -> pd.DataFrame:
        resp = requests.get(
            self.kind_corp_list_url,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )
        resp.raise_for_status()

        html_text = ""
        for enc in ("euc-kr", "cp949", "utf-8"):
            try:
                html_text = resp.content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not html_text:
            html_text = resp.text

        tables = pd.read_html(StringIO(html_text))
        if not tables:
            return pd.DataFrame(columns=["stock_code", "stock_name", "market", "as_of_date"])

        raw = tables[0].copy()
        if raw.shape[1] < 3:
            return pd.DataFrame(columns=["stock_code", "stock_name", "market", "as_of_date"])

        corp_name_col = raw.columns[0]
        market_col = raw.columns[1]
        stock_col = raw.columns[2]

        out = pd.DataFrame()
        out["stock_name"] = raw[corp_name_col].astype("string").str.strip()
        out["market_raw"] = raw[market_col].astype("string").str.strip()
        out["stock_code"] = raw[stock_col].astype("string").str.replace(r"[^\d]", "", regex=True).str.zfill(6)
        out = out[out["stock_code"].str.match(r"^\d{6}$", na=False)].copy()

        out = out[out["market_raw"].isin(["유가", "KOSPI", "코스피"])].copy()
        out["market"] = "KOSPI"
        out["as_of_date"] = datetime.now().strftime("%Y-%m-%d")

        out = out[["stock_code", "stock_name", "market", "as_of_date"]].drop_duplicates(subset=["stock_code"])
        out = out.sort_values("stock_code").reset_index(drop=True)
        return out

    def get_current_kospi_tickers(self, as_of: Optional[str] = None) -> pd.DataFrame:
        pykrx_df = self._fetch_kospi_from_pykrx(as_of=as_of)
        if not pykrx_df.empty:
            return pykrx_df
        return self._fetch_kospi_from_kind()
