from __future__ import annotations

import argparse
from dataclasses import asdict

import requests

from config import load_settings
from function.dart_preprocess import preprocess_bytes, records_to_dataframe


LIST_JSON_URL = "https://opendart.fss.or.kr/api/list.json"
LIST_XML_URL = "https://opendart.fss.or.kr/api/list.xml"
DOCUMENT_XML_URL = "https://opendart.fss.or.kr/api/document.xml"


def _fetch(url: str, params: dict) -> bytes:
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.content


def _summarize(name: str, result) -> None:
    print(f"{name}: kind={result.kind} status={result.status} message={result.message}")
    print(f"  records={len(result.records)} text_len={len(result.text or '')} tables={len(result.tables or [])}")
    if result.files:
        print(f"  zip_files={len(result.files)}")


def run(rcept_no: str) -> int:
    settings = load_settings()
    if not settings.dart_api_key:
        raise RuntimeError("DART API key not found. Set DART_API_KEY or OPENDART_API_KEY.")

    date = rcept_no[:8] if rcept_no and len(rcept_no) >= 8 else "20240102"
    list_params = {"crtfc_key": settings.dart_api_key, "bgn_de": date, "end_de": date}

    json_bytes = _fetch(LIST_JSON_URL, list_params)
    xml_bytes = _fetch(LIST_XML_URL, list_params)
    zip_bytes = _fetch(DOCUMENT_XML_URL, {"crtfc_key": settings.dart_api_key, "rcept_no": rcept_no})

    json_result = preprocess_bytes(json_bytes, source="list.json")
    xml_result = preprocess_bytes(xml_bytes, source="list.xml")
    zip_result = preprocess_bytes(zip_bytes, source="document.xml")

    _summarize("JSON(list.json)", json_result)
    _summarize("XML(list.xml)", xml_result)
    _summarize("ZIP(document.xml)", zip_result)

    if json_result.records:
        df_json = records_to_dataframe(json_result.records)
        print("  json_sample_rows:", min(3, len(df_json)))
        print(df_json.head(3))

    if xml_result.records:
        df_xml = records_to_dataframe(xml_result.records)
        print("  xml_sample_rows:", min(3, len(df_xml)))
        print(df_xml.head(3))

    if zip_result.files:
        first = zip_result.files[0]
        print("  zip_first_file:", asdict(first))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch 3 DART documents and preprocess them.")
    parser.add_argument("--rcept-no", required=True, help="Receipt number (e.g., 20240102000345)")
    args = parser.parse_args()
    return run(args.rcept_no)


if __name__ == "__main__":
    raise SystemExit(main())
