from __future__ import annotations

import pymysql

from .config import get_settings


def get_connection() -> pymysql.connections.Connection:
    s = get_settings()
    return pymysql.connect(
        host=s.db_host,
        port=s.db_port,
        user=s.db_user,
        password=s.db_password,
        database=s.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=3,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
    )
