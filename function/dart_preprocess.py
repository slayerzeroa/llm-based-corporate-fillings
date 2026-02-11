# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import xml.etree.ElementTree as ET


@dataclass
class DartPreprocessResult:
    kind: str
    status: Optional[str]
    message: Optional[str]
    records: list[dict]
    text: Optional[str]
    tables: Optional[list[list[list[str]]]]
    source: Optional[str] = None
    files: Optional[list["DartPreprocessResult"]] = None


def _decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_json(text: str, source: Optional[str]) -> DartPreprocessResult:
    payload = json.loads(text)
    status = None
    message = None

    if isinstance(payload, dict):
        status = payload.get("status")
        message = payload.get("message")
        records = []
        if isinstance(payload.get("list"), list):
            records = payload["list"]
        elif isinstance(payload.get("data"), list):
            records = payload["data"]
        return DartPreprocessResult(
            kind="json",
            status=status,
            message=message,
            records=records,
            text=None,
            tables=None,
            source=source,
        )

    return DartPreprocessResult(
        kind="json",
        status=None,
        message=None,
        records=[],
        text=None,
        tables=None,
        source=source,
    )


def _extract_tables(root: ET.Element) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []

    for table in root.iter():
        if _strip_ns(table.tag).upper() != "TABLE":
            continue

        rows: list[list[str]] = []
        for row in table.iter():
            if _strip_ns(row.tag).upper() != "TR":
                continue
            cells = []
            for cell in row.iter():
                if _strip_ns(cell.tag).upper() not in {"TD", "TH"}:
                    continue
                cell_text = _normalize_text(" ".join(cell.itertext()))
                cells.append(cell_text)
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)

    return tables


def _parse_xml(text: str, source: Optional[str]) -> DartPreprocessResult:
    root = ET.fromstring(text)

    status = None
    message = None
    records: list[dict] = []

    status_node = root.find(".//status")
    if status_node is not None and status_node.text:
        status = status_node.text.strip()

    message_node = root.find(".//message")
    if message_node is not None and message_node.text:
        message = message_node.text.strip()

    list_nodes = root.findall(".//list")
    if list_nodes:
        for node in list_nodes:
            item: dict[str, Optional[str]] = {}
            for child in list(node):
                key = _strip_ns(child.tag)
                value = (child.text or "").strip()
                item[key] = value
            if item:
                records.append(item)
        return DartPreprocessResult(
            kind="xml",
            status=status,
            message=message,
            records=records,
            text=None,
            tables=None,
            source=source,
        )

    # If not a list-type XML, treat as document XML.
    doc_text = _normalize_text(" ".join(root.itertext()))
    tables = _extract_tables(root)
    return DartPreprocessResult(
        kind="xml",
        status=status,
        message=message,
        records=[],
        text=doc_text,
        tables=tables,
        source=source,
    )


def preprocess_bytes(data: bytes, *, source: Optional[str] = None) -> DartPreprocessResult:
    if data[:2] == b"PK":
        return _preprocess_zip(data, source=source)

    text = _decode_bytes(data).lstrip()
    if text.startswith("{") or text.startswith("["):
        return _parse_json(text, source=source)

    return _parse_xml(text, source=source)


def _preprocess_zip(data: bytes, *, source: Optional[str] = None) -> DartPreprocessResult:
    files: list[DartPreprocessResult] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            content = zf.read(name)
            files.append(preprocess_bytes(content, source=name))

    return DartPreprocessResult(
        kind="zip",
        status=None,
        message=None,
        records=[],
        text=None,
        tables=None,
        source=source,
        files=files,
    )


def preprocess_file(path: str) -> DartPreprocessResult:
    with open(path, "rb") as handle:
        data = handle.read()
    return preprocess_bytes(data, source=path)


def records_to_dataframe(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)
