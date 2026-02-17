import re
import time
import requests
import pandas as pd
import xml.etree.ElementTree as ET
from typing import Dict, Any, List, Tuple

BASE_ESTK_RS_JSON_URL = "https://opendart.fss.or.kr/api/estkRs.json"
BASE_ESTK_RS_XML_URL = "https://opendart.fss.or.kr/api/estkRs.xml"

# 날짜/숫자 후보 컬럼
DATE_COLS = {"sbd", "pymd", "sband", "asand", "asstd"}
NUMERIC_HINT_COLS = {
    "exprc", "stkcnt", "fv", "slprc", "slta", "udtcnt", "udtamt", "amt",
    "bfsl_hdstk", "slstk", "atsl_hdstk", "grtcnt"
}
NUMERIC_SUFFIX_PATTERN = re.compile(r"(cnt|amt|prc|ta|fv|exprc|stkcnt|slta|udtamt|grtcnt)$", re.IGNORECASE)


def _norm_yyyymmdd(s: str) -> str:
    s = str(s).replace("-", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"Invalid date format: {s} (YYYYMMDD or YYYY-MM-DD)")
    return s


def _to_num_safe_series(sr: pd.Series) -> pd.Series:
    cleaned = (
        sr.astype(str)
          .str.replace(",", "", regex=False)
          .str.replace("%", "", regex=False)
          .str.replace(r"[^\d\.\-]", "", regex=True)
          .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _format_yyyymmdd_series(sr: pd.Series) -> pd.Series:
    raw = sr.astype(str).str.replace(r"[^\d]", "", regex=True)
    dt = pd.to_datetime(raw, format="%Y%m%d", errors="coerce")
    # 변환 성공한 값만 YYYY-MM-DD로 바꾸고 실패한 값은 원본 유지
    out = dt.dt.strftime("%Y-%m-%d")
    return out.where(dt.notna(), sr.astype(str))


def _infer_group_name(df: pd.DataFrame, fallback: str) -> str:
    cols = set(df.columns)
    if {"sbd", "pymd", "sband", "asand", "asstd"} & cols:
        return "일반사항"
    if {"stksen", "stkcnt", "fv", "slprc", "slta", "slmthn"} & cols:
        return "증권의종류"
    if {"actsen", "actnmn", "udtcnt", "udtamt", "udtprc", "udtmth"} & cols:
        return "인수인정보"
    if {"se", "amt"} & cols:
        return "자금의사용목적"
    if {"hdr", "rl_cmp", "bfsl_hdstk", "slstk", "atsl_hdstk"} & cols:
        return "매출인에관한사항"
    if {"grtrs", "exavivr", "grtcnt", "expd", "exprc"} & cols:
        return "일반청약자환매청구권"
    return fallback


def _request_json_with_retry(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    timeout: int = 30
) -> dict:
    params = {
        "crtfc_key": api_key,
        "corp_code": str(corp_code).zfill(8),
        "bgn_de": _norm_yyyymmdd(bgn_de),
        "end_de": _norm_yyyymmdd(end_de),
    }

    js = None
    for attempt in range(max_retries):
        r = requests.get(BASE_ESTK_RS_JSON_URL, params=params, timeout=timeout)
        r.raise_for_status()
        js = r.json()

        st = js.get("status", "")
        if st in ("000", "013"):
            return js
        if st == "020":
            time.sleep(base_sleep * (2 ** attempt))
            continue
        raise RuntimeError(f"estkRs.json error: status={st}, message={js.get('message')}")

    raise RuntimeError("estkRs.json retry exhausted (020)")


def _request_xml_with_retry(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    timeout: int = 30
) -> bytes:
    params = {
        "crtfc_key": api_key,
        "corp_code": str(corp_code).zfill(8),
        "bgn_de": _norm_yyyymmdd(bgn_de),
        "end_de": _norm_yyyymmdd(end_de),
    }

    for attempt in range(max_retries):
        r = requests.get(BASE_ESTK_RS_XML_URL, params=params, timeout=timeout)
        r.raise_for_status()
        raw = r.content

        # XML에서 status 확인
        try:
            root = ET.fromstring(raw)
            st = (root.findtext(".//status") or "").strip()
        except Exception:
            st = ""

        if st in ("000", "013"):
            return raw
        if st == "020":
            time.sleep(base_sleep * (2 ** attempt))
            continue
        # status 파싱이 안 되면 일단 반환(후단 파싱에서 처리)
        if st == "":
            return raw
        raise RuntimeError(f"estkRs.xml error: status={st}")

    raise RuntimeError("estkRs.xml retry exhausted (020)")


def _extract_tables_from_json(js: dict) -> Tuple[Dict[str, pd.DataFrame], dict]:
    """
    JSON 구조가 바뀌어도 동작하게 재귀적으로 list/list1... 수집.
    """
    tables_raw: Dict[str, List[dict]] = {}
    debug = {"paths": [], "top_keys": list(js.keys())}

    def add_rows(group_name: str, rows: list, path: str):
        if not isinstance(rows, list):
            return
        # dict row만 수집
        rows2 = [x for x in rows if isinstance(x, dict)]
        if not rows2:
            return
        g = group_name or "UNKNOWN_GROUP"
        tables_raw.setdefault(g, []).extend(rows2)
        debug["paths"].append({"group": g, "path": path, "rows": len(rows2)})

    def walk(node, current_title: str = "", path: str = "root"):
        if isinstance(node, dict):
            title = node.get("title") if isinstance(node.get("title"), str) else current_title

            for k, v in node.items():
                # list/list1/list2...
                if re.fullmatch(r"list\d*", str(k)) and isinstance(v, list):
                    gname = title or str(k)
                    add_rows(gname, v, f"{path}.{k}")
                else:
                    walk(v, title, f"{path}.{k}")

        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, current_title, f"{path}[{i}]")

    walk(js)

    tables: Dict[str, pd.DataFrame] = {}
    for gname, rows in tables_raw.items():
        df = pd.DataFrame(rows)
        if df.empty:
            continue
        # group명 보정
        real_gname = _infer_group_name(df, fallback=gname)
        # 같은 이름 그룹 합치기
        if real_gname in tables:
            tables[real_gname] = pd.concat([tables[real_gname], df], ignore_index=True)
        else:
            tables[real_gname] = df

    return tables, debug


def _extract_tables_from_xml(xml_bytes: bytes) -> Tuple[Dict[str, pd.DataFrame], str, str]:
    """
    JSON이 비어있을 때 XML fallback 파서.
    """
    root = ET.fromstring(xml_bytes)
    status = (root.findtext(".//status") or "").strip()
    message = (root.findtext(".//message") or "").strip()

    tables: Dict[str, pd.DataFrame] = {}

    # group 단위 파싱
    groups = root.findall(".//group")
    for gi, g in enumerate(groups):
        title = (g.findtext("title") or f"group_{gi+1}").strip()
        rows = []
        for lst in g.findall("./list"):
            row = {}
            for ch in list(lst):
                tag = ch.tag.split("}")[-1]
                row[tag] = (ch.text or "").strip()
            if row:
                rows.append(row)
        if rows:
            df = pd.DataFrame(rows)
            gname = _infer_group_name(df, fallback=title)
            if gname in tables:
                tables[gname] = pd.concat([tables[gname], df], ignore_index=True)
            else:
                tables[gname] = df

    return tables, status, message


def _postprocess_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "corp_code" in out.columns:
        out["corp_code"] = out["corp_code"].astype(str).str.zfill(8)

    if "rcept_no" in out.columns:
        out["viewer_url"] = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + out["rcept_no"].astype(str)

    # 날짜형 컬럼 보정
    for c in DATE_COLS:
        if c in out.columns:
            out[c] = _format_yyyymmdd_series(out[c])

    # 숫자형 컬럼 보정
    numeric_candidates = [c for c in out.columns if NUMERIC_SUFFIX_PATTERN.search(c)]
    numeric_candidates += [c for c in NUMERIC_HINT_COLS if c in out.columns]
    numeric_candidates = sorted(set(numeric_candidates))

    for c in numeric_candidates:
        conv = _to_num_safe_series(out[c])
        # 전부 NaN이면 원문 유지
        if conv.notna().any():
            out[c] = conv

    return out


def _build_combined(tables: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    chunks = []
    for gname, df in tables.items():
        if df.empty:
            continue
        tmp = df.copy()
        tmp["group_name"] = gname
        chunks.append(tmp)

    if not chunks:
        return pd.DataFrame()

    combined = pd.concat(chunks, ignore_index=True)

    sort_cols = [c for c in ["rcept_no", "group_name"] if c in combined.columns]
    if sort_cols:
        ascending = [False if c == "rcept_no" else True for c in sort_cols]
        combined = combined.sort_values(sort_cols, ascending=ascending).reset_index(drop=True)

    return combined


def fetch_estk_rs_robust(
    api_key: str,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    max_retries: int = 5,
    base_sleep: float = 0.8,
    xml_fallback: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    지분증권(estkRs) robust 조회:
    1) JSON 호출
    2) 재귀 파서로 group/list 전부 수집
    3) 비어있으면 XML fallback 파싱
    """
    js = _request_json_with_retry(
        api_key=api_key,
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        max_retries=max_retries,
        base_sleep=base_sleep,
    )

    status = js.get("status", "")
    message = js.get("message", "")

    # 013: 데이터 없음
    if status == "013":
        return {
            "status": status,
            "message": message,
            "source": "json",
            "tables": {},
            "combined": pd.DataFrame(),
            "debug": {"top_keys": list(js.keys()), "paths": []},
        }

    # 1) JSON 재귀 파싱
    tables, debug = _extract_tables_from_json(js)

    # 2) 후처리
    for g in list(tables.keys()):
        tables[g] = _postprocess_table(tables[g]).drop_duplicates().reset_index(drop=True)

    # 3) JSON에서 못 건진 경우 XML fallback
    source = "json"
    if xml_fallback and status == "000" and sum(len(df) for df in tables.values()) == 0:
        raw_xml = _request_xml_with_retry(
            api_key=api_key,
            corp_code=corp_code,
            bgn_de=bgn_de,
            end_de=end_de,
            max_retries=max_retries,
            base_sleep=base_sleep,
        )
        xtables, xstatus, xmessage = _extract_tables_from_xml(raw_xml)

        if xstatus in ("000", "013"):
            # XML 결과가 있으면 교체
            if sum(len(df) for df in xtables.values()) > 0:
                tables = {k: _postprocess_table(v).drop_duplicates().reset_index(drop=True)
                          for k, v in xtables.items()}
                source = "xml_fallback"

            # 상태/메시지도 XML 기준으로 갱신(대부분 동일)
            status, message = xstatus, xmessage

    combined = _build_combined(tables)

    if verbose:
        print(f"[FINAL] status={status} message={message}")
        print(f"[FINAL] source={source}")
        print(f"[FINAL] groups={list(tables.keys())}")
        print(f"[FINAL] combined_rows={len(combined)}")
        print(f"[DEBUG] top_keys={debug.get('top_keys', [])}")
        print(f"[DEBUG] paths={debug.get('paths', [])[:10]} ...")

    return {
        "status": status,
        "message": message,
        "source": source,
        "tables": tables,
        "combined": combined,
        "debug": debug,
    }


# -------------------------
# 사용 예시
# -------------------------
if __name__ == "__main__":
    import os
    import dotenv

    dotenv.load_dotenv()
    API_KEY = os.getenv("OPENDART_API_KEY", "").strip()

    res = fetch_estk_rs_robust(
        api_key=API_KEY,
        corp_code="00106395",
        bgn_de="20190101",
        end_de="20191231",
        xml_fallback=True,
        verbose=True,
    )

    print("[CHECK] status:", res["status"], "message:", res["message"])
    print("[CHECK] source:", res["source"])
    print("[CHECK] groups:", list(res["tables"].keys()))
    print("[CHECK] combined rows:", len(res["combined"]))

    os.makedirs("data", exist_ok=True)

    # 그룹별 저장
    for g, df in res["tables"].items():
        if df.empty:
            continue
        safe = re.sub(r"[^\w가-힣]+", "_", g).strip("_")
        df.to_csv(f"data/estkRs_{safe}.csv", index=False, encoding="utf-8-sig")

    # 통합 저장
    res["combined"].to_csv("data/estkRs_combined.csv", index=False, encoding="utf-8-sig")

    print(res)