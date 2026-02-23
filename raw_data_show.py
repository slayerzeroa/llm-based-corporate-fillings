from __future__ import annotations

import argparse
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from config import load_settings


BASE_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
TRF_DECSN_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrTrfDecsn.json"
INH_DECSN_URL = "https://opendart.fss.or.kr/api/otcprStkInvscrInhDecsn.json"
OTR_CPR_INVSTMNT_URL = "https://opendart.fss.or.kr/api/otrCprInvstmntSttus.json"
MAJORSTOCK_URL = "https://opendart.fss.or.kr/api/majorstock.json"
STK_EXTR_DECSN_URL = "https://opendart.fss.or.kr/api/stkExtrDecsn.json"
DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do"
VIEWER_URL = "https://dart.fss.or.kr/report/viewer.do"


VIEWDOC_PATTERN_7 = re.compile(
    r"""viewDoc\(
        \s*['"](?P<rcpNo>\d{14})['"]\s*,\s*
        ['"](?P<dcmNo>\d+)['"]\s*,\s*
        ['"](?P<eleId>\d+)['"]\s*,\s*
        ['"](?P<offset>\d+)['"]\s*,\s*
        ['"](?P<length>\d+)['"]\s*,\s*
        ['"](?P<dtd>[^'"]+)['"]\s*,\s*
        ['"](?P<tocNo>[^'"]*)['"]\s*
    \)""",
    flags=re.IGNORECASE | re.VERBOSE,
)

VIEWDOC_PATTERN_6 = re.compile(
    r"""viewDoc\(
        \s*['"](?P<rcpNo>\d{14})['"]\s*,\s*
        ['"](?P<dcmNo>\d+)['"]\s*,\s*
        ['"](?P<eleId>\d+)['"]\s*,\s*
        ['"](?P<offset>\d+)['"]\s*,\s*
        ['"](?P<length>\d+)['"]\s*,\s*
        ['"](?P<dtd>[^'"]+)['"]\s*
    \)""",
    flags=re.IGNORECASE | re.VERBOSE,
)

OTR_REPORT_NAME_REGEX = re.compile(
    r"타법인.*(?:출자|출자증권|주식)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchResult:
    name: str
    status: str
    message: str
    rows: int
    file_path: str


def _norm_yyyymmdd(value: str) -> str:
    raw = str(value).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {value} (YYYYMMDD or YYYY-MM-DD)")
    return raw


def _safe_readable_json(js: Any) -> str:
    return json.dumps(js, ensure_ascii=False, indent=2)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, _safe_readable_json(payload))


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _call_json(
    session: requests.Session,
    api_key: str,
    url: str,
    *,
    timeout: int,
    max_retries: int,
    base_sleep: float,
    **params: Any,
) -> dict[str, Any]:
    payload = {"crtfc_key": api_key, **params}
    for attempt in range(max(max_retries, 1)):
        resp = session.get(url, params=payload, timeout=timeout)
        resp.raise_for_status()
        js = resp.json()
        status = str(js.get("status", ""))
        if status in ("000", "013"):
            return js
        if status == "020":
            time.sleep(base_sleep * (2 ** attempt))
            continue
        raise RuntimeError(
            f"DART API error: status={status}, message={js.get('message')}, url={url}, params={params}"
        )
    raise RuntimeError(f"Request retries exhausted: {url}")


def _is_otr_investment_report_name(report_nm: Any) -> bool:
    name = re.sub(r"\s+", "", str(report_nm or "")).strip()
    if not name:
        return False
    return bool(OTR_REPORT_NAME_REGEX.search(name))


def _filter_list_payload_for_otr(js: dict[str, Any]) -> tuple[dict[str, Any], int]:
    rows = js.get("list", [])
    if not isinstance(rows, list):
        return js, 0

    filtered = [row for row in rows if _is_otr_investment_report_name(row.get("report_nm"))]
    dropped = len(rows) - len(filtered)

    out = dict(js)
    out["list"] = filtered
    out["list_count_before_filter"] = len(rows)
    out["list_count_after_filter"] = len(filtered)
    out["list_filtered_by"] = "report_nm contains 타법인 + (출자|출자증권|주식)"
    return out, dropped


def _find_viewdoc_candidates(main_html: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for m in VIEWDOC_PATTERN_7.finditer(main_html):
        c = m.groupdict()
        st, ed = m.span()
        c["_ctx"] = main_html[max(0, st - 240) : min(len(main_html), ed + 240)]
        candidates.append(c)
    for m in VIEWDOC_PATTERN_6.finditer(main_html):
        c = m.groupdict()
        c["tocNo"] = ""
        st, ed = m.span()
        c["_ctx"] = main_html[max(0, st - 240) : min(len(main_html), ed + 240)]
        candidates.append(c)
    return candidates


def _choose_best_viewdoc(candidates: list[dict[str, str]]) -> Optional[dict[str, str]]:
    if not candidates:
        return None
    keywords = [
        "타법인주식및출자증권처분결정",
        "타법인주식및출자증권취득결정",
        "타법인주식및출자증권양도결정",
        "타법인주식및출자증권양수결정",
        "타법인 주식 및 출자증권",
    ]
    scored: list[tuple[int, dict[str, str]]] = []
    for c in candidates:
        score = 0
        ctx = c.get("_ctx", "")
        if any(k in ctx for k in keywords):
            score += 10
        dtd = str(c.get("dtd", "")).lower()
        if "html" in dtd:
            score += 2
        if str(c.get("tocNo", "")).strip():
            score += 1
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _collect_rcept_nos(
    payloads: list[dict[str, Any]],
    explicit_rcept_no: Optional[str],
    *,
    crawl_all: bool,
    crawl_max_reports: Optional[int],
    only_otr_reports: bool,
) -> list[str]:
    cands: list[str] = []

    if explicit_rcept_no:
        for token in str(explicit_rcept_no).split(","):
            rcp = token.strip()
            if re.fullmatch(r"\d{14}", rcp):
                cands.append(rcp)
    else:
        for js in payloads:
            rows = js.get("list", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                report_nm = row.get("report_nm")
                if only_otr_reports and report_nm:
                    if not _is_otr_investment_report_name(report_nm):
                        continue
                rcp = str(row.get("rcept_no", "")).strip()
                if re.fullmatch(r"\d{14}", rcp):
                    cands.append(rcp)

    if not cands:
        return []

    picked = sorted(set(cands), reverse=True)
    if not crawl_all:
        picked = picked[:1]
    if crawl_max_reports is not None:
        picked = picked[: max(int(crawl_max_reports), 0)]
    return picked


def _fetch_openapi_raw(
    session: requests.Session,
    api_key: str,
    *,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    bsns_year: Optional[str],
    reprt_code: str,
    pblntf_tys: tuple[str, ...],
    timeout: int,
    max_retries: int,
    base_sleep: float,
    out_dir: Path,
    include_optional_endpoints: bool,
    only_otr_reports: bool,
) -> tuple[list[FetchResult], list[dict[str, Any]]]:
    results: list[FetchResult] = []
    payloads: list[dict[str, Any]] = []

    def _run(name: str, url: str, params: dict[str, Any], file_name: str) -> None:
        js = _call_json(
            session,
            api_key,
            url,
            timeout=timeout,
            max_retries=max_retries,
            base_sleep=base_sleep,
            **params,
        )
        dropped = 0
        if only_otr_reports and url == BASE_LIST_URL:
            js, dropped = _filter_list_payload_for_otr(js)
        payloads.append(js)
        file_path = out_dir / "openapi" / file_name
        _write_json(file_path, js)
        results.append(
            FetchResult(
                name=name,
                status=str(js.get("status", "")),
                message=(
                    f"{js.get('message', '')} (filtered_out={dropped})"
                    if dropped > 0 else str(js.get("message", ""))
                ),
                rows=len(js.get("list", []) or []),
                file_path=str(file_path),
            )
        )

    for ty in pblntf_tys:
        _run(
            name=f"list.json (pblntf_ty={ty})",
            url=BASE_LIST_URL,
            params={
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": ty,
                "sort": "date",
                "sort_mth": "desc",
                "page_no": "1",
                "page_count": "100",
                "last_reprt_at": "N",
            },
            file_name=f"list_{ty}.json",
        )

    _run(
        name="otcprStkInvscrTrfDecsn.json",
        url=TRF_DECSN_URL,
        params={"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de},
        file_name="otcprStkInvscrTrfDecsn.json",
    )
    _run(
        name="otcprStkInvscrInhDecsn.json",
        url=INH_DECSN_URL,
        params={"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de},
        file_name="otcprStkInvscrInhDecsn.json",
    )

    if include_optional_endpoints:
        _run(
            name="majorstock.json",
            url=MAJORSTOCK_URL,
            params={"corp_code": corp_code},
            file_name="majorstock.json",
        )
        _run(
            name="stkExtrDecsn.json",
            url=STK_EXTR_DECSN_URL,
            params={"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de},
            file_name="stkExtrDecsn.json",
        )
        if bsns_year:
            _run(
                name=f"otrCprInvstmntSttus.json ({bsns_year}, {reprt_code})",
                url=OTR_CPR_INVSTMNT_URL,
                params={"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
                file_name=f"otrCprInvstmntSttus_{bsns_year}_{reprt_code}.json",
            )

    return results, payloads


def _fetch_crawling_raw(
    session: requests.Session,
    api_key: str,
    *,
    rcept_no: str,
    timeout: int,
    out_dir: Path,
    preview_chars: int,
) -> dict[str, Any]:
    crawl_dir = out_dir / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)

    main_resp = session.get(MAIN_URL, params={"rcpNo": rcept_no}, timeout=timeout)
    main_resp.raise_for_status()
    main_html = main_resp.text
    _write_text(crawl_dir / f"main_{rcept_no}.html", main_html)

    candidates = _find_viewdoc_candidates(main_html)
    _write_json(crawl_dir / f"viewdoc_candidates_{rcept_no}.json", candidates)
    best = _choose_best_viewdoc(candidates)

    viewer_path: Optional[Path] = None
    viewer_preview = ""
    if best:
        params = {
            "rcpNo": best["rcpNo"],
            "dcmNo": best["dcmNo"],
            "eleId": best["eleId"],
            "offset": best["offset"],
            "length": best["length"],
            "dtd": best["dtd"],
        }
        toc_no = str(best.get("tocNo", "")).strip()
        if toc_no:
            params["tocNo"] = toc_no
        viewer_resp = session.get(VIEWER_URL, params=params, timeout=timeout)
        viewer_resp.raise_for_status()
        viewer_html = viewer_resp.text
        viewer_path = crawl_dir / f"viewer_{rcept_no}.html"
        _write_text(viewer_path, viewer_html)
        viewer_preview = viewer_html[:preview_chars]

    doc_resp = session.get(
        DOCUMENT_URL,
        params={"crtfc_key": api_key, "rcept_no": rcept_no},
        timeout=timeout,
    )
    doc_resp.raise_for_status()
    doc_bytes = doc_resp.content
    doc_zip_path = crawl_dir / f"document_{rcept_no}.zip"
    _write_bytes(doc_zip_path, doc_bytes)

    zip_entries: list[str] = []
    text_preview = ""
    try:
        with zipfile.ZipFile(io.BytesIO(doc_bytes)) as zf:
            zip_entries = zf.namelist()
            for name in zip_entries:
                low = name.lower()
                if low.endswith((".xml", ".html", ".htm", ".xhtml", ".txt")):
                    data = zf.read(name)
                    for enc in ("utf-8", "cp949", "euc-kr"):
                        try:
                            text_preview = data.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    if not text_preview:
                        text_preview = data.decode("utf-8", errors="ignore")
                    text_preview = text_preview[:preview_chars]
                    break
    except zipfile.BadZipFile:
        zip_entries = []

    _write_text(crawl_dir / f"document_preview_{rcept_no}.txt", text_preview)
    if viewer_preview:
        _write_text(crawl_dir / f"viewer_preview_{rcept_no}.txt", viewer_preview)

    return {
        "rcept_no": rcept_no,
        "main_html_path": str(crawl_dir / f"main_{rcept_no}.html"),
        "viewer_html_path": str(viewer_path) if viewer_path else "",
        "document_zip_path": str(doc_zip_path),
        "viewdoc_candidates": len(candidates),
        "best_viewdoc": best or {},
        "document_zip_entries": zip_entries,
        "document_preview_path": str(crawl_dir / f"document_preview_{rcept_no}.txt"),
        "viewer_preview_path": str(crawl_dir / f"viewer_preview_{rcept_no}.txt") if viewer_preview else "",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Show raw incoming data for OpenDART OpenAPI and DART crawling paths. "
            "This script stores raw JSON/HTML/ZIP files for inspection."
        )
    )
    parser.add_argument("--corp-code", required=True, help="8-digit DART corp_code")
    parser.add_argument("--bgn-de", required=True, help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument("--end-de", required=True, help="YYYYMMDD or YYYY-MM-DD")
    parser.add_argument(
        "--rcept-no",
        default=None,
        help="Optional 14-digit rcept_no. Multiple values allowed with commas.",
    )
    parser.add_argument("--bsns-year", default=None, help="Optional business year for otrCprInvstmntSttus")
    parser.add_argument("--reprt-code", default="11011", help="Report code for otrCprInvstmntSttus")
    parser.add_argument("--pblntf-tys", default="B,I", help="Comma-separated pblntf_ty values for list.json")
    parser.add_argument("--include-optional-endpoints", action="store_true")
    parser.add_argument("--out-dir", default="logs/raw_data_show")
    parser.add_argument("--preview-chars", type=int, default=2500)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--base-sleep", type=float, default=0.8)
    parser.add_argument(
        "--include-non-otr-reports",
        action="store_true",
        help="Disable OTR-only filtering and include unrelated reports too.",
    )
    parser.add_argument(
        "--crawl-all",
        action="store_true",
        help="Fetch crawling raw for all discovered rcept_no values (default behavior).",
    )
    parser.add_argument(
        "--crawl-single",
        action="store_true",
        help="Fetch crawling raw for only one rcept_no (latest). Overrides --crawl-all.",
    )
    parser.add_argument(
        "--crawl-max-reports",
        type=int,
        default=None,
        help="Optional cap for number of crawl documents.",
    )
    parser.add_argument(
        "--crawl-sleep-sec",
        type=float,
        default=0.2,
        help="Sleep seconds between crawling documents.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    api_key = (settings.dart_api_key or "").strip()
    if not api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY/OPENDART_API_KEY in .env.")

    corp_code = str(args.corp_code).strip().zfill(8)
    if not re.fullmatch(r"\d{8}", corp_code):
        raise ValueError(f"Invalid corp_code: {args.corp_code}")
    bgn_de = _norm_yyyymmdd(args.bgn_de)
    end_de = _norm_yyyymmdd(args.end_de)

    pblntf_tys = tuple([x.strip() for x in str(args.pblntf_tys).split(",") if x.strip()]) or ("B", "I")
    only_otr_reports = not bool(args.include_non_otr_reports)
    run_dir = Path(args.out_dir) / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{corp_code}_{bgn_de}_{end_de}"
    run_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )

    openapi_results, payloads = _fetch_openapi_raw(
        session,
        api_key,
        corp_code=corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
        bsns_year=args.bsns_year,
        reprt_code=str(args.reprt_code),
        pblntf_tys=pblntf_tys,
        timeout=args.timeout,
        max_retries=args.max_retries,
        base_sleep=args.base_sleep,
        out_dir=run_dir,
        include_optional_endpoints=bool(args.include_optional_endpoints),
        only_otr_reports=only_otr_reports,
    )

    if args.crawl_all and args.crawl_single:
        raise ValueError("Use only one of --crawl-all or --crawl-single.")
    crawl_all = True if args.crawl_all else not bool(args.crawl_single)
    picked_rcept_nos = _collect_rcept_nos(
        payloads,
        args.rcept_no,
        crawl_all=crawl_all,
        crawl_max_reports=args.crawl_max_reports,
        only_otr_reports=only_otr_reports,
    )
    crawl_results: list[dict[str, Any]] = []
    crawl_errors: list[dict[str, str]] = []
    for idx, rcp in enumerate(picked_rcept_nos, start=1):
        try:
            one = _fetch_crawling_raw(
                session,
                api_key,
                rcept_no=rcp,
                timeout=args.timeout,
                out_dir=run_dir,
                preview_chars=max(int(args.preview_chars), 200),
            )
            crawl_results.append(one)
            print(f"[Crawl {idx}/{len(picked_rcept_nos)}] OK rcept_no={rcp}")
        except Exception as exc:
            crawl_errors.append({"rcept_no": rcp, "error": str(exc)})
            print(f"[Crawl {idx}/{len(picked_rcept_nos)}] ERROR rcept_no={rcp} error={exc}")

        if float(args.crawl_sleep_sec) > 0 and idx < len(picked_rcept_nos):
            time.sleep(float(args.crawl_sleep_sec))

    manifest = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "picked_rcept_nos": picked_rcept_nos,
        "openapi": [r.__dict__ for r in openapi_results],
        "only_otr_reports": only_otr_reports,
        "crawl_count": len(crawl_results),
        "crawl_error_count": len(crawl_errors),
        "crawl": crawl_results,
        "crawl_errors": crawl_errors,
    }
    _write_json(run_dir / "manifest.json", manifest)

    print(f"[DONE] raw files saved to: {run_dir}")
    print("[OpenAPI]")
    for item in openapi_results:
        print(
            f"  - {item.name}: status={item.status}, rows={item.rows}, "
            f"message='{item.message}', file={item.file_path}"
        )
    if picked_rcept_nos:
        print(
            f"[Crawl] requested={len(picked_rcept_nos)}, success={len(crawl_results)}, errors={len(crawl_errors)}"
        )
        if crawl_results:
            first = crawl_results[0]
            print(f"  - sample rcept_no: {first.get('rcept_no', '')}")
            print(f"  - sample main_html: {first.get('main_html_path', '')}")
            print(f"  - sample viewer_html: {first.get('viewer_html_path', '')}")
            print(f"  - sample document_zip: {first.get('document_zip_path', '')}")
            print(f"  - sample document_entries: {len(first.get('document_zip_entries', []))}")
        if crawl_errors:
            print(f"  - first error: {crawl_errors[0]}")
    else:
        print("[Crawl] rcept_no not found from OpenAPI payloads and not provided by --rcept-no")


if __name__ == "__main__":
    main()
