from __future__ import annotations

import argparse
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import pymysql
from dotenv import load_dotenv


PLACEHOLDERS = {
    "합계",
    "총계",
    "소계",
    "회사명",
    "회사명(국적)",
    "발행회사",
    "대표자",
    "대표이사",
    "국적",
    "성명",
}


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def _first_env(*names: str) -> str | None:
    for name in names:
        v = os.getenv(name)
        if v and v.strip():
            return v.strip()
    return None


def _load_db_config() -> DbConfig:
    load_dotenv(dotenv_path=".env")
    host = _first_env("DB_HOST")
    user = _first_env("DB_USER", "DB_USERNAME")
    password = _first_env("DB_PASSWORD", "DB_PASS") or ""
    database = _first_env("DB_NAME", "DB_DATABASE")
    port_raw = _first_env("DB_PORT") or "3306"
    if not host or not user or not database:
        raise RuntimeError("Missing DB env vars: DB_HOST/DB_USER/DB_NAME")
    return DbConfig(
        host=host,
        port=int(port_raw),
        user=user,
        password=password,
        database=database,
    )


def _norm_date(s: str | None) -> str | None:
    if not s:
        return None
    raw = str(s).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {s}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _to_num(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _is_placeholder(name: str) -> bool:
    compact = re.sub(r"\s+", "", str(name or ""))
    return compact in {re.sub(r"\s+", "", x) for x in PLACEHOLDERS}


def _connect(cfg: DbConfig) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _table_exists(conn: pymysql.connections.Connection, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (table,),
        )
        return cur.fetchone() is not None


def _pick_table(conn: pymysql.connections.Connection, explicit: str | None) -> str:
    if explicit:
        return explicit
    env_table = _first_env("DB_TABLE", "TABLE_NAME")
    candidates = [
        "dart_investment_events_copy",
        "dart_investment_events",
        env_table,
        "dart_edge_events",
        "dart_edge_state_daily",
    ]
    for c in candidates:
        if c and _table_exists(conn, c):
            return c
    raise RuntimeError("No usable source table found.")


def _table_cols(conn: pymysql.connections.Connection, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (table,),
        )
        return {r["column_name"] for r in cur.fetchall()}


def _load_raw_events(
    conn: pymysql.connections.Connection,
    table: str,
    corp_code: str,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    where = [
        "corp_code = %s",
        "rcept_dt IS NOT NULL",
        "corp_name IS NOT NULL",
        "iscmp_cmpnm IS NOT NULL",
        "TRIM(corp_name) <> ''",
        "TRIM(iscmp_cmpnm) <> ''",
        "corp_name <> iscmp_cmpnm",
    ]
    params: list[Any] = [corp_code]
    if start_date:
        where.append("rcept_dt >= %s")
        params.append(start_date)
    if end_date:
        where.append("rcept_dt <= %s")
        params.append(end_date)

    sql = f"""
    SELECT rcept_dt, corp_name, iscmp_cmpnm, trfdtl_trfprc, trfdtl_stkcnt, report_nm, trf_pp, source
    FROM {table}
    WHERE {" AND ".join(where)}
    ORDER BY rcept_dt ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        df = pd.DataFrame(cur.fetchall())
    if df.empty:
        return df

    def _signed_delta(r: pd.Series) -> float:
        amt = _to_num(r.get("trfdtl_trfprc"))
        qty = _to_num(r.get("trfdtl_stkcnt"))
        base = amt if amt is not None else qty
        if base is None:
            return 0.0

        if base < 0:
            sign = -1
        elif base > 0:
            sign = 1
        else:
            report = str(r.get("report_nm") or "")
            sign = -1 if re.search(r"처분|양도|매도", report) else 1

        text = f"{r.get('report_nm') or ''} {r.get('trf_pp') or ''}"
        if re.search(r"취소|철회|중단|해제", text):
            sign *= -1
        return abs(float(base)) * sign

    df["rcept_dt"] = pd.to_datetime(df["rcept_dt"], errors="coerce").dt.date.astype(str)
    df["src"] = df["corp_name"].astype(str).str.strip()
    df["dst"] = df["iscmp_cmpnm"].astype(str).str.strip()
    df = df[(~df["dst"].map(_is_placeholder)) & (df["dst"] != "")]
    df["delta"] = df.apply(_signed_delta, axis=1)
    return df[["rcept_dt", "src", "dst", "delta"]].copy()


def _load_edge_events(
    conn: pymysql.connections.Connection,
    table: str,
    corp_code: str,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    where = ["corp_code = %s", "corp_name IS NOT NULL", "target_name_norm IS NOT NULL"]
    params: list[Any] = [corp_code]
    if start_date:
        where.append("COALESCE(effective_dt, rcept_dt) >= %s")
        params.append(start_date)
    if end_date:
        where.append("COALESCE(effective_dt, rcept_dt) <= %s")
        params.append(end_date)

    sql = f"""
    SELECT
        COALESCE(effective_dt, rcept_dt) AS rcept_dt,
        corp_name,
        target_name_norm,
        delta_amount,
        delta_shares
    FROM {table}
    WHERE {" AND ".join(where)}
    ORDER BY COALESCE(effective_dt, rcept_dt) ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        df = pd.DataFrame(cur.fetchall())
    if df.empty:
        return df

    df["rcept_dt"] = pd.to_datetime(df["rcept_dt"], errors="coerce").dt.date.astype(str)
    df["src"] = df["corp_name"].astype(str).str.strip()
    df["dst"] = df["target_name_norm"].astype(str).str.strip()
    df = df[(~df["dst"].map(_is_placeholder)) & (df["dst"] != "")]

    amt = pd.to_numeric(df["delta_amount"], errors="coerce")
    qty = pd.to_numeric(df["delta_shares"], errors="coerce")
    df["delta"] = amt.where(~amt.isna(), qty).fillna(0.0)
    return df[["rcept_dt", "src", "dst", "delta"]].copy()


def _build_daily_series(events_df: pd.DataFrame, top_n: int, max_edges: int) -> tuple[list[str], dict[str, list[tuple[str, str, float]]]]:
    if events_df.empty:
        return [], {}

    daily_group = (
        events_df.groupby(["rcept_dt", "src", "dst"], as_index=False)
        .agg(delta=("delta", "sum"))
        .sort_values("rcept_dt")
    )

    final_sum = (
        daily_group.groupby(["src", "dst"], as_index=False)
        .agg(net=("delta", "sum"))
        .assign(abs_net=lambda d: d["net"].abs())
        .sort_values("abs_net", ascending=False)
    )
    keep_pairs = set(
        tuple(x) for x in final_sum[["src", "dst"]].head(max(top_n, max_edges))[["src", "dst"]].itertuples(index=False, name=None)
    )

    dates = sorted(daily_group["rcept_dt"].unique().tolist())
    by_date: dict[str, list[tuple[str, str, float]]] = {}
    running: defaultdict[tuple[str, str], float] = defaultdict(float)
    grouped_by_date = {d: g for d, g in daily_group.groupby("rcept_dt")}

    for d in dates:
        g = grouped_by_date.get(d)
        if g is not None:
            for _, r in g.iterrows():
                pair = (str(r["src"]), str(r["dst"]))
                if pair in keep_pairs:
                    running[pair] += float(r["delta"])

        edges = [(s, t, w) for (s, t), w in running.items() if abs(w) > 1e-12]
        edges = sorted(edges, key=lambda x: abs(x[2]), reverse=True)[:max_edges]
        by_date[d] = edges

    return dates, by_date


def _make_figure(dates: list[str], by_date: dict[str, list[tuple[str, str, float]]], corp_code: str) -> go.Figure:
    all_nodes: set[str] = set()
    for edges in by_date.values():
        for s, t, _ in edges:
            all_nodes.add(s)
            all_nodes.add(t)

    G = nx.Graph()
    G.add_nodes_from(sorted(all_nodes))
    for edges in by_date.values():
        for s, t, _ in edges:
            G.add_edge(s, t)

    pos = nx.spring_layout(G, seed=42) if len(G.nodes) > 0 else {}

    def _frame_traces(edges: list[tuple[str, str, float]]) -> list[go.Scatter]:
        edge_x: list[float] = []
        edge_y: list[float] = []
        edge_text: list[str] = []
        for s, t, w in edges:
            x0, y0 = pos.get(s, (0.0, 0.0))
            x1, y1 = pos.get(t, (0.0, 0.0))
            edge_x += [x0, x1, None]
            edge_y += [y0, y1, None]
            edge_text.append(f"{s} -> {t}: {w:,.0f}")

        node_x = []
        node_y = []
        node_text = []
        node_size = []
        degree = defaultdict(int)
        for s, t, _ in edges:
            degree[s] += 1
            degree[t] += 1
        for n in sorted({x for e in edges for x in (e[0], e[1])}):
            x, y = pos.get(n, (0.0, 0.0))
            node_x.append(x)
            node_y.append(y)
            d = degree.get(n, 1)
            node_size.append(8 + 3 * math.log2(d + 1))
            node_text.append(f"{n}<br>degree={d}")

        return [
            go.Scatter(x=edge_x, y=edge_y, mode="lines", line=dict(width=1), hoverinfo="none", name="edges"),
            go.Scatter(
                x=node_x,
                y=node_y,
                mode="markers+text",
                marker=dict(size=node_size, color="#1f77b4", opacity=0.9),
                text=[n for n in sorted({x for e in edges for x in (e[0], e[1])})],
                textposition="top center",
                hovertext=node_text,
                hoverinfo="text",
                name="nodes",
            ),
        ]

    frames = [go.Frame(name=d, data=_frame_traces(by_date.get(d, []))) for d in dates]
    init_data = _frame_traces(by_date.get(dates[0], [])) if dates else []
    fig = go.Figure(data=init_data, frames=frames)

    sliders = [
        {
            "steps": [
                {
                    "method": "animate",
                    "label": d,
                    "args": [[d], {"mode": "immediate", "frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}}],
                }
                for d in dates
            ],
            "x": 0.1,
            "len": 0.85,
        }
    ]

    fig.update_layout(
        title=f"Daily Cumulative Investment Network (corp_code={corp_code})",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        sliders=sliders,
        updatemenus=[
            {
                "type": "buttons",
                "buttons": [
                    {
                        "label": "Play",
                        "method": "animate",
                        "args": [None, {"frame": {"duration": 450, "redraw": True}, "fromcurrent": True}],
                    },
                    {"label": "Pause", "method": "animate", "args": [[None], {"mode": "immediate", "frame": {"duration": 0}}]},
                ],
            }
        ],
        template="plotly_white",
        showlegend=False,
    )
    return fig


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--corp-code", required=True, help="8-digit DART corp_code")
    p.add_argument("--time-mode", choices=["daily"], default="daily")
    p.add_argument("--top-n", type=int, default=200)
    p.add_argument("--max-edges", type=int, default=50)
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD or YYYYMMDD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD or YYYYMMDD")
    p.add_argument("--source-table", default=None)
    p.add_argument("--out-html", default="sample_graph_timeline.html")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = _load_db_config()
    corp_code = str(args.corp_code).strip().zfill(8)
    start_date = _norm_date(args.start_date)
    end_date = _norm_date(args.end_date)

    conn = _connect(cfg)
    try:
        table = _pick_table(conn, args.source_table)
        cols = _table_cols(conn, table)
        if {"rcept_dt", "corp_name", "iscmp_cmpnm", "trfdtl_trfprc", "trfdtl_stkcnt"}.issubset(cols):
            events_df = _load_raw_events(conn, table, corp_code, start_date, end_date)
        elif {"corp_name", "target_name_norm", "delta_amount", "delta_shares"}.issubset(cols):
            events_df = _load_edge_events(conn, table, corp_code, start_date, end_date)
        else:
            raise RuntimeError(f"Unsupported source table schema for graph builder: {table}")
    finally:
        conn.close()

    dates, by_date = _build_daily_series(events_df, top_n=args.top_n, max_edges=args.max_edges)
    if not dates:
        raise RuntimeError("No graph data found for the given corp_code/date range.")

    fig = _make_figure(dates, by_date, corp_code=corp_code)
    fig.write_html(args.out_html, include_plotlyjs="cdn")
    print(
        f"[DONE] table={table}, rows={len(events_df):,}, dates={len(dates):,}, "
        f"max_edges={args.max_edges}, out={args.out_html}"
    )


if __name__ == "__main__":
    main()
