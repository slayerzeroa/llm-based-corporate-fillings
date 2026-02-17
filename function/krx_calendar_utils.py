# pip install requests beautifulsoup4 pandas

import re
import time
import requests
import pandas as pd
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://kind.krx.co.kr"
IR_URL = f"{BASE}/corpgeneral/irschedule.do"


def build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": f"{BASE}/",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return s


def extract_form_defaults(form: BeautifulSoup) -> dict:
    payload = {}
    # input 기본값
    for inp in form.select("input[name]"):
        t = (inp.get("type") or "").lower()
        if t in {"submit", "button", "image", "file", "reset"}:
            continue
        payload[inp["name"]] = inp.get("value", "")
    # select 기본값
    for sel in form.select("select[name]"):
        opts = sel.select("option")
        if not opts:
            payload[sel["name"]] = ""
            continue
        selected = next((o for o in opts if o.has_attr("selected")), opts[0])
        payload[sel["name"]] = selected.get("value", selected.get_text(strip=True))
    return payload


def detect_year_month_select_names(form: BeautifulSoup):
    year_name, month_name = None, None
    for sel in form.select("select[name]"):
        name = sel["name"]
        nums = []
        for o in sel.select("option"):
            raw = (o.get("value") or o.get_text(strip=True)).strip()
            m = re.search(r"\d+", raw)
            if m:
                nums.append(int(m.group()))
        if not nums:
            continue
        u = sorted(set(nums))
        # 연도 셀렉트 추정
        if year_name is None and len(u) >= 20 and min(u) <= 2005 and max(u) >= 2025:
            year_name = name
        # 월 셀렉트 추정
        if month_name is None and len(u) >= 12 and min(u) <= 1 and max(u) >= 12:
            if set(range(1, 13)).issubset(set(u)):
                month_name = name
    return year_name, month_name


def format_value_for_select(form: BeautifulSoup, select_name: str, target: int) -> str:
    sel = next((x for x in form.select("select[name]") if x.get("name") == select_name), None)
    if sel is None:
        return str(target)
    values = []
    for o in sel.select("option"):
        v = (o.get("value") or o.get_text(strip=True)).strip()
        values.append(v)

    if f"{target:02d}" in values:
        return f"{target:02d}"
    if str(target) in values:
        return str(target)

    # 값 안에 숫자만 있는 경우 대응
    for v in values:
        m = re.search(r"\d+", v)
        if m and int(m.group()) == target:
            return v
    return str(target)


def extract_irseq_ids(html: str) -> list[str]:
    ids = set(re.findall(r"irSeq=(\d+)", html))
    ids.update(re.findall(r"fnPopupIRInfoDetail\(['\"]?(\d+)['\"]?\)", html))
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("[href],[onclick]"):
        txt = f'{tag.get("href","")} {tag.get("onclick","")}'
        ids.update(re.findall(r"irSeq=(\d+)", txt))
        ids.update(re.findall(r"searchIRSchedulePopup[^0-9]*(\d+)", txt))
        ids.update(re.findall(r"fnPopupIRInfoDetail\(['\"]?(\d+)['\"]?\)", txt))

    return sorted(ids, key=int)


def parse_popup(html: str, ir_seq: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = {"irSeq": int(ir_seq)}

    # 표 형태 key-value 파싱
    for tr in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.select("th,td")]
        if len(cells) < 2:
            continue

        i = 0
        while i + 1 < len(cells):
            k, v = cells[i], cells[i + 1]
            if k and k not in {"보기", "상세내역"}:
                out[k] = v
            i += 2

    # 최소 필드 정리
    wanted = ["회사명", "시장구분", "URL", "장소", "일자", "시작시간", "제목", "내용"]
    normalized = {"irSeq": out["irSeq"]}
    for k in wanted:
        if k in out:
            normalized[k] = out[k]

    # 표 파싱 실패 시 텍스트 fallback
    if len(normalized) <= 1:
        text = soup.get_text("\n", strip=True)
        for k in wanted:
            m = re.search(rf"{k}\s*[:：]?\s*(.+)", text)
            if m:
                normalized[k] = m.group(1).split("\n")[0].strip()

    return normalized


def _format_time(value: str) -> str:
    if not value:
        return value
    raw = re.sub(r"\s+", "", str(value))
    m = re.match(r"^(\d{1,2}):?(\d{2})$", raw)
    if not m:
        return value
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh > 23 or mm > 59:
        return value
    return f"{hh:02d}:{mm:02d}"


def arrange_ir_calendar_df(df: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    if df.empty:
        preferred = ["irSeq", "회사명", "시장구분", "일자", "시작시간", "제목", "장소", "URL", "내용"]
        return pd.DataFrame(columns=preferred)

    # Ensure expected columns exist
    preferred = ["irSeq", "회사명", "시장구분", "일자", "시작시간", "제목", "장소", "URL", "내용"]
    for col in preferred:
        if col not in df.columns:
            df[col] = None

    # Normalize date
    dt = pd.to_datetime(df["일자"], errors="coerce")
    df = df[(dt.dt.year == year) & (dt.dt.month == month)].copy()
    df["일자"] = dt.dt.strftime("%Y-%m-%d")

    # Normalize time
    df["시작시간"] = df["시작시간"].apply(_format_time)

    # Drop duplicates
    if "irSeq" in df.columns:
        df = df.drop_duplicates(subset=["irSeq"])

    # Sort
    sort_cols = [c for c in ["일자", "시작시간", "회사명", "irSeq"] if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=True)

    # Reorder columns
    cols = preferred + [c for c in df.columns if c not in preferred]
    return df[cols].reset_index(drop=True)


def fetch_kind_ir_calendar(year: int, month: int, max_items: int | None = None, sleep_sec: float = 0.12) -> pd.DataFrame:
    s = build_session()

    # 1) 달력 페이지 기본 진입
    init_params = {"gubun": "iRScheduleCalendar", "method": "searchIRScheduleMain"}
    r0 = s.get(IR_URL, params=init_params, timeout=25)
    r0.raise_for_status()

    soup0 = BeautifulSoup(r0.text, "html.parser")
    target_form = soup0.find("form", {"name": "searchForm"}) or soup0.find("form", {"id": "searchForm"})

    payload = {}
    submit_method = "post"

    if target_form is not None:
        payload.update(extract_form_defaults(target_form))
        y_name, m_name = detect_year_month_select_names(target_form)

        if y_name:
            payload[y_name] = format_value_for_select(target_form, y_name, year)
        else:
            payload["selYear"] = str(year)

        if m_name:
            payload[m_name] = format_value_for_select(target_form, m_name, month)
        else:
            payload["selMonth"] = f"{month:02d}"

        submit_method = (target_form.get("method") or "post").lower()
    else:
        payload["selYear"] = str(year)
        payload["selMonth"] = f"{month:02d}"

    # Ensure correct method and gubun for calendar AJAX response
    payload["method"] = "searchIRScheduleCalendar"
    payload["gubun"] = "iRScheduleCalendar"

    # 2) 연/월 조건으로 달력 요청 (AJAX endpoint)
    if submit_method == "get":
        r = s.get(IR_URL, params=payload, timeout=25)
    else:
        r = s.post(IR_URL, data=payload, timeout=25)
    r.raise_for_status()

    # 3) 달력 html에서 irSeq 추출
    irseq_ids = extract_irseq_ids(r.text)

    # fallback: 목록 페이지에서도 irSeq 확보 시도
    if not irseq_ids:
        r_list = s.get(IR_URL, params={"gubun": "iRSchedule", "method": "searchIRScheduleMain"}, timeout=25)
        r_list.raise_for_status()
        irseq_ids = extract_irseq_ids(r_list.text)

    if max_items is not None:
        irseq_ids = irseq_ids[:max_items]

    rows = []
    for ir_seq in irseq_ids:
        rp = s.get(
            IR_URL,
            params={"method": "searchIRSchedulePopup", "irSeq": ir_seq},
            timeout=25
        )
        if rp.status_code == 200:
            rows.append(parse_popup(rp.text, ir_seq))
        time.sleep(sleep_sec)  # 서버 부하 방지

    df = pd.DataFrame(rows)
    return arrange_ir_calendar_df(df, year, month)


if __name__ == "__main__":
    # 예시: 2026년 2월
    df = fetch_kind_ir_calendar(2026, 2)
    print(df.head(20))
    import os
    os.makedirs("data", exist_ok=True)
    df.to_csv("data/kind_ir_calendar_2026_02.csv", index=False, encoding="utf-8-sig")
    print("saved:", len(df), "rows")
