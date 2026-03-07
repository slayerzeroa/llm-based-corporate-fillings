from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "https://slayerzeroa.github.io",
]


@dataclass(frozen=True)
class ServerSettings:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    db_table: str
    db_disclosures_raw_table: str
    db_disclosure_families_table: str
    db_disclosure_family_members_table: str
    db_investment_lines_raw_table: str
    db_investee_dim_table: str
    db_edge_events_table: str
    db_edge_state_current_table: str
    db_edge_state_daily_table: str
    db_etl_runs_table: str
    graph_cache_ttl_sec: int
    graph_cache_max_items: int
    stock_cache_ttl_sec: int
    stock_cache_max_items: int
    cors_origins: list[str]


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _parse_port(raw: str | None) -> int:
    value = raw or "3306"
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid DB_PORT: {value}") from exc


def _parse_int(raw: str | None, *, default: int, minimum: int = 1) -> int:
    if raw is None or not str(raw).strip():
        return max(int(default), minimum)
    try:
        return max(int(raw), minimum)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer value: {raw}") from exc


def _parse_cors(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_CORS_ORIGINS.copy()
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        return ["*"]
    merged = list(dict.fromkeys(items + DEFAULT_CORS_ORIGINS))
    return merged


@lru_cache(maxsize=1)
def get_settings() -> ServerSettings:
    load_dotenv()

    db_host = _first_env("DB_HOST")
    db_user = _first_env("DB_USER", "DB_USERNAME")
    db_name = _first_env("DB_NAME", "DB_DATABASE")
    db_password = _first_env("DB_PASSWORD", "DB_PASS") or ""
    db_port = _parse_port(_first_env("DB_PORT"))
    db_table = _first_env("DB_TABLE") or "dart_investment_events"
    db_disclosures_raw_table = (
        _first_env("DB_DISCLOSURES_RAW_TABLE", "DART_DISCLOSURES_RAW_TABLE")
        or "dart_disclosures_raw"
    )
    db_disclosure_families_table = (
        _first_env("DB_DISCLOSURE_FAMILIES_TABLE", "DART_DISCLOSURE_FAMILIES_TABLE")
        or "dart_disclosure_families"
    )
    db_disclosure_family_members_table = (
        _first_env("DB_DISCLOSURE_FAMILY_MEMBERS_TABLE", "DART_DISCLOSURE_FAMILY_MEMBERS_TABLE")
        or "dart_disclosure_family_members"
    )
    db_investment_lines_raw_table = (
        _first_env("DB_INVESTMENT_LINES_RAW_TABLE", "DART_INVESTMENT_LINES_RAW_TABLE")
        or "dart_investment_lines_raw"
    )
    db_investee_dim_table = (
        _first_env("DB_INVESTEE_DIM_TABLE", "DART_INVESTEE_DIM_TABLE")
        or "dart_investee_dim"
    )
    db_edge_events_table = (
        _first_env("DB_EDGE_EVENTS_TABLE", "DART_EDGE_EVENTS_TABLE")
        or "dart_edge_events"
    )
    db_edge_state_current_table = (
        _first_env("DB_EDGE_STATE_CURRENT_TABLE", "DART_EDGE_STATE_CURRENT_TABLE")
        or "dart_edge_state_current"
    )
    db_edge_state_daily_table = (
        _first_env("DB_EDGE_STATE_DAILY_TABLE", "DART_EDGE_STATE_DAILY_TABLE")
        or "dart_edge_state_daily"
    )
    db_etl_runs_table = (
        _first_env("DB_ETL_RUNS_TABLE", "DART_ETL_RUNS_TABLE")
        or "etl_runs"
    )
    graph_cache_ttl_sec = _parse_int(_first_env("GRAPH_CACHE_TTL_SEC"), default=120, minimum=1)
    graph_cache_max_items = _parse_int(_first_env("GRAPH_CACHE_MAX_ITEMS"), default=128, minimum=1)
    stock_cache_ttl_sec = _parse_int(_first_env("STOCK_CACHE_TTL_SEC"), default=300, minimum=1)
    stock_cache_max_items = _parse_int(_first_env("STOCK_CACHE_MAX_ITEMS"), default=256, minimum=1)

    missing = []
    if not db_host:
        missing.append("DB_HOST")
    if not db_user:
        missing.append("DB_USER")
    if not db_name:
        missing.append("DB_NAME")
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    table_values = {
        "DB_TABLE": db_table,
        "DB_DISCLOSURES_RAW_TABLE": db_disclosures_raw_table,
        "DB_DISCLOSURE_FAMILIES_TABLE": db_disclosure_families_table,
        "DB_DISCLOSURE_FAMILY_MEMBERS_TABLE": db_disclosure_family_members_table,
        "DB_INVESTMENT_LINES_RAW_TABLE": db_investment_lines_raw_table,
        "DB_INVESTEE_DIM_TABLE": db_investee_dim_table,
        "DB_EDGE_EVENTS_TABLE": db_edge_events_table,
        "DB_EDGE_STATE_CURRENT_TABLE": db_edge_state_current_table,
        "DB_EDGE_STATE_DAILY_TABLE": db_edge_state_daily_table,
        "DB_ETL_RUNS_TABLE": db_etl_runs_table,
    }
    for key, value in table_values.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise RuntimeError(f"Invalid {key}: {value}")

    cors_origins = _parse_cors(_first_env("CORS_ORIGINS"))
    return ServerSettings(
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=db_name,
        db_table=db_table,
        db_disclosures_raw_table=db_disclosures_raw_table,
        db_disclosure_families_table=db_disclosure_families_table,
        db_disclosure_family_members_table=db_disclosure_family_members_table,
        db_investment_lines_raw_table=db_investment_lines_raw_table,
        db_investee_dim_table=db_investee_dim_table,
        db_edge_events_table=db_edge_events_table,
        db_edge_state_current_table=db_edge_state_current_table,
        db_edge_state_daily_table=db_edge_state_daily_table,
        db_etl_runs_table=db_etl_runs_table,
        graph_cache_ttl_sec=graph_cache_ttl_sec,
        graph_cache_max_items=graph_cache_max_items,
        stock_cache_ttl_sec=stock_cache_ttl_sec,
        stock_cache_max_items=stock_cache_max_items,
        cors_origins=cors_origins,
    )
