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

    missing = []
    if not db_host:
        missing.append("DB_HOST")
    if not db_user:
        missing.append("DB_USER")
    if not db_name:
        missing.append("DB_NAME")
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    if not re.fullmatch(r"[A-Za-z0-9_]+", db_table):
        raise RuntimeError(f"Invalid DB_TABLE: {db_table}")

    cors_origins = _parse_cors(_first_env("CORS_ORIGINS"))
    return ServerSettings(
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        db_name=db_name,
        db_table=db_table,
        cors_origins=cors_origins,
    )
