from __future__ import annotations

import argparse
import math
import os
import re
from typing import Any, Optional

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import pymysql
from dotenv import load_dotenv


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _safe_table_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"Invalid table name: {name}")
    return name


def _norm_corp_code(value: str) -> str:
    raw = re.sub(r"[^\d]", "", str(value or ""))
    if not re.fullmatch(r"\d{1,8}", raw):
        raise ValueError(f"Invalid corp_code: {value}")
    return raw.zfill(8)


def _load_rows(
    *,
    state_table: str,
    investee_table: str,
    corp_code: Optional[str],
    include_inactive: bool,
    top_n: int,
) -> pd.DataFrame:
    load_dotenv()

    host = _first_env("DB_HOST")
    port_raw = _first_env("DB_PORT") or "3306"
    user = _first_env("DB_USER", "DB_USERNAME")
    password = _first_env("DB_PASSWORD", "DB_PASS") or ""
    database = _first_env("DB_NAME", "DB_DATABASE")

    missing = []
    if not host:
        missing.append("DB_HOST")
    if not user:
        missing.append("DB_USER")
    if not database:
        missing.append("DB_NAME")
    if missing:
        raise RuntimeError(f"Missing DB env vars: {', '.join(missing)}")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid DB_PORT: {port_raw}") from exc

    st = _safe_table_name(state_table)
    it = _safe_table_name(investee_table)

    where = ["1=1"]
    params: list[Any] = []
    if corp_code:
        where.append("s.corp_code = %s")
        params.append(_norm_corp_code(corp_code))
    if not include_inactive:
        where.append("s.active_flag = 1")

    sql = f"""
    SELECT
        s.corp_code,
        s.corp_name,
        s.target_name_norm,
        COALESCE(i.name_canonical, s.target_name_norm) AS target_name,
        s.active_flag,
        s.holding_amount,
        s.planned_amount,
        s.holding_shares,
        s.planned_shares,
        s.first_connected_dt,
        s.last_changed_dt,
        s.closed_dt
    FROM {st} s
    LEFT JOIN {it} i
      ON s.investee_id = i.investee_id
    WHERE {" AND ".join(where)}
    ORDER BY ABS(COALESCE(s.holding_amount, 0) + COALESCE(s.planned_amount, 0)) DESC,
             ABS(COALESCE(s.holding_shares, 0) + COALESCE(s.planned_shares, 0)) DESC
    LIMIT %s
    """
    params.append(max(int(top_n), 1))

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    return pd.DataFrame(rows)


def _to_num(x: Any) -> float:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return 0.0
        return float(x)
    except Exception:
        return 0.0


def _prepare_edges(df: pd.DataFrame, min_weight: float, max_edges: int) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["src"] = out["corp_name"].astype(str).str.strip()
    out["dst"] = out["target_name"].astype(str).str.strip()

    out = out[
        (out["src"] != "")
        & (out["dst"] != "")
        & (out["src"] != "nan")
        & (out["dst"] != "nan")
        & (out["src"] != out["dst"])
    ].copy()

    out["holding_amount_n"] = out["holding_amount"].map(_to_num)
    out["planned_amount_n"] = out["planned_amount"].map(_to_num)
    out["holding_shares_n"] = out["holding_shares"].map(_to_num)
    out["planned_shares_n"] = out["planned_shares"].map(_to_num)
    out["edge_weight"] = (
        out["holding_amount_n"].abs()
        + out["planned_amount_n"].abs()
        + out["holding_shares_n"].abs()
        + out["planned_shares_n"].abs()
    )

    if min_weight > 0:
        out = out[out["edge_weight"] >= float(min_weight)].copy()

    out = out.sort_values("edge_weight", ascending=False).head(max(int(max_edges), 1)).reset_index(drop=True)
    return out


def _build_3d_figure(edges: pd.DataFrame, title: str) -> go.Figure:
    g = nx.DiGraph()
    for _, r in edges.iterrows():
        g.add_edge(
            r["src"],
            r["dst"],
            weight=float(r["edge_weight"]),
            holding_amount=float(r["holding_amount_n"]),
            planned_amount=float(r["planned_amount_n"]),
            active=int(r.get("active_flag", 0) or 0),
            first_dt=str(r.get("first_connected_dt") or ""),
            last_dt=str(r.get("last_changed_dt") or ""),
        )

    if g.number_of_nodes() == 0:
        raise RuntimeError("No nodes after filtering.")

    pos = nx.spring_layout(g, dim=3, seed=42, weight="weight")

    edge_x: list[float] = []
    edge_y: list[float] = []
    edge_z: list[float] = []
    for u, v in g.edges():
        x0, y0, z0 = pos[u]
        x1, y1, z1 = pos[v]
        edge_x.extend([x0, x1, None])
        edge_y.extend([y0, y1, None])
        edge_z.extend([z0, z1, None])

    edge_trace = go.Scatter3d(
        x=edge_x,
        y=edge_y,
        z=edge_z,
        mode="lines",
        line=dict(width=1, color="rgba(120,120,120,0.45)"),
        hoverinfo="none",
        name="Edges",
    )

    src_nodes = set(edges["src"].tolist())
    dst_nodes = set(edges["dst"].tolist())

    node_x: list[float] = []
    node_y: list[float] = []
    node_z: list[float] = []
    node_text: list[str] = []
    node_color: list[str] = []
    node_size: list[float] = []

    for n in g.nodes():
        x, y, z = pos[n]
        node_x.append(x)
        node_y.append(y)
        node_z.append(z)

        in_deg = g.in_degree(n)
        out_deg = g.out_degree(n)
        deg = in_deg + out_deg
        wsum = sum(float(d.get("weight", 0.0)) for _, _, d in g.edges(n, data=True))

        if n in src_nodes and n in dst_nodes:
            color = "#f59e0b"
            role = "Investor+Target"
        elif n in src_nodes:
            color = "#2563eb"
            role = "Investor"
        else:
            color = "#16a34a"
            role = "Target"

        node_color.append(color)
        node_size.append(8 + 3.5 * math.log1p(max(deg, 1)))
        node_text.append(
            f"{n}<br>"
            f"Role: {role}<br>"
            f"In/Out: {in_deg}/{out_deg}<br>"
            f"Connected Weight Sum: {wsum:,.2f}"
        )

    node_trace = go.Scatter3d(
        x=node_x,
        y=node_y,
        z=node_z,
        mode="markers",
        text=node_text,
        hoverinfo="text",
        marker=dict(size=node_size, color=node_color, opacity=0.9),
        name="Nodes",
    )

    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        title=title,
        showlegend=False,
        scene=dict(xaxis=dict(visible=False), yaxis=dict(visible=False), zaxis=dict(visible=False)),
        margin=dict(l=0, r=0, t=60, b=0),
    )
    return fig


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Build a sample 3D graph from edge-state tables in DB.")
    parser.add_argument("--corp-code", default=None, help="Optional 8-digit corp_code filter.")
    parser.add_argument("--top-n", type=int, default=300, help="Rows to read from DB before graph filtering.")
    parser.add_argument("--max-edges", type=int, default=120, help="Max edges to render.")
    parser.add_argument("--min-weight", type=float, default=0.0, help="Min edge weight to keep.")
    parser.add_argument("--include-inactive", action="store_true", help="Include inactive edges too.")
    parser.add_argument("--state-table", default=_first_env("DB_EDGE_STATE_CURRENT_TABLE") or "dart_edge_state_current")
    parser.add_argument("--investee-table", default=_first_env("DB_INVESTEE_DIM_TABLE") or "dart_investee_dim")
    parser.add_argument("--out-html", default="sample_edge_graph.html")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    corp_code = _norm_corp_code(args.corp_code) if args.corp_code else None

    raw_df = _load_rows(
        state_table=args.state_table,
        investee_table=args.investee_table,
        corp_code=corp_code,
        include_inactive=bool(args.include_inactive),
        top_n=int(args.top_n),
    )
    if raw_df.empty:
        raise RuntimeError("No rows found from DB for the given condition.")

    edges = _prepare_edges(raw_df, min_weight=float(args.min_weight), max_edges=int(args.max_edges))
    if edges.empty:
        raise RuntimeError("No edges left after filtering.")

    title = (
        f"Sample 3D Graph from DB ({args.state_table})"
        + (f" | corp_code={corp_code}" if corp_code else " | ALL")
        + f" | rows={len(raw_df):,}, edges={len(edges):,}"
    )
    fig = _build_3d_figure(edges, title=title)
    fig.write_html(args.out_html, include_plotlyjs="cdn")

    print(
        f"[DONE] rows={len(raw_df):,}, edges={len(edges):,}, "
        f"nodes={len(set(edges['src']).union(set(edges['dst']))):,}, "
        f"out={args.out_html}"
    )


if __name__ == "__main__":
    main()
