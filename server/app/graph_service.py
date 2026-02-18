from __future__ import annotations

import json
import re
from collections import deque
from collections import OrderedDict
from threading import Lock
import time

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import ServerSettings
from .db import get_connection
from .schemas import GraphQuery, GraphResponse, TopEdge

SYSTEM_MAX_EDGES = 50
GRAPH_CACHE_TTL_SEC = 20
GRAPH_CACHE_MAX_ITEMS = 256
STOCK_CACHE_TTL_SEC = 60
STOCK_CACHE_MAX_ITEMS = 256


class _TTLCache:
    def __init__(self, max_items: int, ttl_sec: int) -> None:
        self.max_items = max(int(max_items), 1)
        self.ttl_sec = max(int(ttl_sec), 1)
        self._store: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple) -> object | None:
        now = time.time()
        with self._lock:
            item = self._store.get(key)
            if not item:
                return None
            expires_at, value = item
            if now >= expires_at:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: tuple, value: object) -> None:
        now = time.time()
        with self._lock:
            self._store[key] = (now + self.ttl_sec, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_items:
                self._store.popitem(last=False)


_GRAPH_CACHE = _TTLCache(max_items=GRAPH_CACHE_MAX_ITEMS, ttl_sec=GRAPH_CACHE_TTL_SEC)
_STOCK_CACHE = _TTLCache(max_items=STOCK_CACHE_MAX_ITEMS, ttl_sec=STOCK_CACHE_TTL_SEC)


def _norm_yyyymmdd(value: str) -> str:
    raw = str(value).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {value} (YYYYMMDD or YYYY-MM-DD)")
    return raw


def _to_date_str(value: str) -> str:
    ymd = _norm_yyyymmdd(value)
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _norm_name(text: str) -> str:
    s = str(text).strip().lower()
    s = s.replace("(주)", "").replace("㈜", "").replace("주식회사", "")
    s = s.replace("co.,ltd.", "").replace("co., ltd.", "").replace("corporation", "")
    s = re.sub(r"[^0-9a-zA-Z가-힣]", "", s)
    return s


def _build_base_where(
    *,
    start_date: str | None,
    end_date: str | None,
    snapshot_date: str | None,
    corp_name_filter: str | None,
    include_periodic_status: bool,
    include_majorstock_status: bool,
) -> tuple[str, list[object]]:
    conditions = [
        "rcept_dt IS NOT NULL",
        "corp_name IS NOT NULL",
        "iscmp_cmpnm IS NOT NULL",
        "TRIM(corp_name) <> ''",
        "TRIM(iscmp_cmpnm) <> ''",
        "corp_name <> iscmp_cmpnm",
        "REPLACE(TRIM(iscmp_cmpnm), ' ', '') NOT IN ('합계','총계','소계')",
    ]
    params: list[object] = []

    if start_date:
        conditions.append("rcept_dt >= %s")
        params.append(_to_date_str(start_date))
    if end_date:
        conditions.append("rcept_dt <= %s")
        params.append(_to_date_str(end_date))
    if snapshot_date:
        conditions.append("rcept_dt <= %s")
        params.append(_to_date_str(snapshot_date))
    if corp_name_filter:
        conditions.append("corp_name = %s")
        params.append(str(corp_name_filter).strip())

    if not include_periodic_status:
        conditions.append("(source IS NULL OR source NOT LIKE %s)")
        params.append("%OTRCPR_INVSTMNT_STTUS%")
    if not include_majorstock_status:
        conditions.append("(source IS NULL OR source NOT LIKE %s)")
        params.append("%MAJORSTOCK_STKQY_PROXY%")

    return " AND ".join(conditions), params


def _query_snapshot_dates(query: GraphQuery, settings: ServerSettings) -> list[str]:
    corp_name_filter = str(query.search_stock).strip() if query.search_stock else None
    where_sql, params = _build_base_where(
        start_date=query.start_date,
        end_date=query.end_date,
        snapshot_date=None,
        corp_name_filter=corp_name_filter,
        include_periodic_status=query.include_periodic_status,
        include_majorstock_status=query.include_majorstock_status,
    )
    sql = f"""
    SELECT DISTINCT rcept_dt
    FROM {settings.db_table}
    WHERE {where_sql}
    ORDER BY rcept_dt ASC
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        dt = row.get("rcept_dt")
        if dt is None:
            continue
        out.append(str(pd.to_datetime(dt).date()))
    return out


def _build_base_event_subquery(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    snapshot_date: str,
) -> tuple[str, list[object]]:
    corp_name_filter = str(query.search_stock).strip() if query.search_stock else None
    where_sql, params = _build_base_where(
        start_date=query.start_date,
        end_date=query.end_date,
        snapshot_date=snapshot_date,
        corp_name_filter=corp_name_filter,
        include_periodic_status=query.include_periodic_status,
        include_majorstock_status=query.include_majorstock_status,
    )
    if query.db_limit is not None and query.db_limit > 0:
        sub_sql = f"""
        SELECT
            corp_name,
            iscmp_cmpnm,
            trfdtl_trfprc,
            rcept_dt,
            rcept_no
        FROM {settings.db_table}
        WHERE {where_sql}
        ORDER BY rcept_dt ASC, rcept_no ASC
        LIMIT %s
        """
        return sub_sql, params + [int(query.db_limit)]

    sub_sql = f"""
    SELECT
        corp_name,
        iscmp_cmpnm,
        trfdtl_trfprc,
        rcept_dt,
        rcept_no
    FROM {settings.db_table}
    WHERE {where_sql}
    """
    return sub_sql, params


def _query_aggregated_edges(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    snapshot_date: str,
    node_filter: set[str] | None = None,
) -> pd.DataFrame:
    sub_sql, sub_params = _build_base_event_subquery(query, settings, snapshot_date=snapshot_date)
    where_extra = ""
    params = list(sub_params)
    if node_filter:
        nodes = sorted(node_filter)
        placeholders = ",".join(["%s"] * len(nodes))
        where_extra = f"WHERE src IN ({placeholders}) AND dst IN ({placeholders})"
        params.extend(nodes)
        params.extend(nodes)

    sql = f"""
    SELECT
        src,
        dst,
        SUM(weight_abs) AS weight
    FROM (
        SELECT
            corp_name AS src,
            iscmp_cmpnm AS dst,
            ABS(COALESCE(CAST(trfdtl_trfprc AS DECIMAL(24, 6)), 0)) AS weight_abs
        FROM ({sub_sql}) base
    ) e
    {where_extra}
    GROUP BY src, dst
    """

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    edges = pd.DataFrame(rows)
    if edges.empty:
        return pd.DataFrame(columns=["src", "dst", "weight"])
    edges["src"] = edges["src"].astype(str)
    edges["dst"] = edges["dst"].astype(str)
    edges["weight"] = pd.to_numeric(edges["weight"], errors="coerce").fillna(0.0)
    return edges


def _query_row_count(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    snapshot_date: str,
    node_filter: set[str] | None = None,
) -> int:
    sub_sql, sub_params = _build_base_event_subquery(query, settings, snapshot_date=snapshot_date)
    params = list(sub_params)
    where_extra = ""
    if node_filter:
        nodes = sorted(node_filter)
        placeholders = ",".join(["%s"] * len(nodes))
        where_extra = f"WHERE corp_name IN ({placeholders}) AND iscmp_cmpnm IN ({placeholders})"
        params.extend(nodes)
        params.extend(nodes)

    sql = f"""
    SELECT COUNT(*) AS cnt
    FROM ({sub_sql}) base
    {where_extra}
    """

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone() or {"cnt": 0}
    finally:
        conn.close()
    return int(row.get("cnt", 0) or 0)


def _resolve_highlight_node(all_nodes: list[str], query: str | None) -> str | None:
    if not query:
        return None
    q = query.strip().lower()
    if not q:
        return None

    exact = [n for n in all_nodes if str(n).strip().lower() == q]
    if exact:
        return exact[0]

    contains = [n for n in all_nodes if q in str(n).strip().lower()]
    if contains:
        return contains[0]

    reverse_contains = [n for n in all_nodes if str(n).strip().lower() in q]
    if reverse_contains:
        return reverse_contains[0]

    nq = _norm_name(q)
    if nq:
        normalized = [(n, _norm_name(str(n))) for n in all_nodes]
        norm_exact = [n for n, nn in normalized if nn == nq]
        if norm_exact:
            return norm_exact[0]
        norm_contains = [n for n, nn in normalized if nq and nn and (nq in nn or nn in nq)]
        if norm_contains:
            return norm_contains[0]

    return None


def _aggregate_edges(edges: pd.DataFrame, max_edges: int | None, pinned_node: str | None = None) -> pd.DataFrame:
    if edges is None or edges.empty:
        return pd.DataFrame(columns=["src", "dst", "weight"])

    if max_edges is None or max_edges <= 0 or len(edges) <= max_edges:
        return edges.copy()

    if pinned_node:
        pin_mask = (edges["src"] == pinned_node) | (edges["dst"] == pinned_node)
        pin_edges = edges[pin_mask].copy()
        if len(pin_edges) >= max_edges:
            return pin_edges.nlargest(max_edges, "weight").copy()

        remain = int(max_edges - len(pin_edges))
        top_others = edges[~pin_mask].nlargest(remain, "weight").copy()
        merged = pd.concat([pin_edges, top_others], ignore_index=True)
        merged = merged.drop_duplicates(subset=["src", "dst"], keep="first")
        return merged.reset_index(drop=True)

    return edges.nlargest(max_edges, "weight").copy()


def _related_nodes_undirected(graph: nx.Graph, center: str, hops: int) -> set[str]:
    if center not in graph:
        return set()
    seen = {center}
    queue = deque([(center, 0)])
    while queue:
        node, depth = queue.popleft()
        if depth >= hops:
            continue
        for nb in graph.neighbors(node):
            if nb not in seen:
                seen.add(nb)
                queue.append((nb, depth + 1))
    return seen


def _k_hop_nodes_from_edges(edges: pd.DataFrame, center: str | None, hops: int) -> set[str]:
    if edges.empty or not center:
        return set()
    g = nx.Graph()
    for _, row in edges.iterrows():
        g.add_edge(str(row["src"]), str(row["dst"]))
    # When searching, keep at least direct neighbors (hop=1) to avoid empty graph.
    use_hops = max(int(hops), 1)
    return _related_nodes_undirected(g, center, use_hops)


def _build_figure_json(
    edges: pd.DataFrame,
    reporting_set: set[str],
    selected: str | None,
    highlight_hops: int,
    title: str,
) -> dict:
    if edges.empty:
        fig = go.Figure()
        fig.update_layout(title=title, height=850, margin=dict(l=0, r=0, t=80, b=0))
        return json.loads(fig.to_json())

    graph = nx.DiGraph()
    for _, row in edges.iterrows():
        graph.add_edge(row["src"], row["dst"], weight=float(row["weight"]))

    pos = nx.spring_layout(graph, dim=3, seed=42, weight="weight")
    undirected = graph.to_undirected()
    related = _related_nodes_undirected(undirected, selected, max(highlight_hops, 1)) if selected else set()

    def _rgba(hex_color: str, alpha: float) -> str:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return f"rgba(120,120,120,{alpha})"
        return f"rgba({int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)},{alpha})"

    nodes = list(graph.nodes())
    node_x, node_y, node_z = [], [], []
    node_size_raw, node_text, node_symbol, node_color = [], [], [], []

    for n in nodes:
        x, y, z = pos[n]
        node_x.append(float(x))
        node_y.append(float(y))
        node_z.append(float(z))

        in_w = float(sum(d.get("weight", 0.0) for _, _, d in graph.in_edges(n, data=True)))
        out_w = float(sum(d.get("weight", 0.0) for _, _, d in graph.out_edges(n, data=True)))
        total_w = in_w + out_w
        node_size_raw.append(total_w)
        ntype = "Reporting Entity" if n in reporting_set else "Investee"
        node_text.append(
            f"Name: {n}<br>Type: {ntype}<br>"
            f"In Total: {in_w:,.0f}<br>Out Total: {out_w:,.0f}<br>"
            f"Total Relationship Strength: {total_w:,.0f}"
        )

        node_symbol.append("diamond" if n in reporting_set else "circle")
        if selected:
            if n == selected:
                node_color.append(_rgba("#E74C3C", 1.0))
            elif n in related:
                node_color.append(_rgba("#2E86DE", 0.95))
            else:
                node_color.append(_rgba("#B0B8C2", 0.20))
        else:
            base = "#2E86DE" if n in reporting_set else "#65A30D"
            node_color.append(_rgba(base, 0.9))

    arr = np.array(node_size_raw, dtype=float)
    if len(arr) == 0:
        size_scaled = np.array([])
    elif arr.max() - arr.min() < 1e-12:
        size_scaled = np.full_like(arr, 12.0)
    else:
        size_scaled = 8 + 24 * (arr - arr.min()) / (arr.max() - arr.min())

    w_min = float(edges["weight"].min())
    w_max = float(edges["weight"].max())

    edge_traces = []
    for _, row in edges.iterrows():
        u, v, w = row["src"], row["dst"], float(row["weight"])
        x0, y0, z0 = pos[u]
        x1, y1, z1 = pos[v]

        if abs(w_max - w_min) < 1e-12:
            width = 2.0
        else:
            width = 1.0 + 5.0 * (w - w_min) / (w_max - w_min)

        if selected and (u in related and v in related):
            color = "#FF4D4F"
            opacity = 0.95
            width = width + 1.2
        elif selected:
            color = "#C7CED8"
            opacity = 0.18
        else:
            color = "#7A8797"
            opacity = 0.7

        edge_traces.append(
            go.Scatter3d(
                x=[float(x0), float(x1), None],
                y=[float(y0), float(y1), None],
                z=[float(z0), float(z1), None],
                mode="lines",
                line=dict(width=float(width), color=color),
                opacity=float(opacity),
                hovertemplate=f"{u} -> {v}<br>Relationship Strength: {w:,.0f}<extra></extra>",
                showlegend=False,
            )
        )

    node_trace = go.Scatter3d(
        x=node_x,
        y=node_y,
        z=node_z,
        mode="markers+text",
        text=nodes,
        textposition="top center",
        hovertext=node_text,
        hoverinfo="text",
        marker=dict(
            size=size_scaled.tolist() if len(size_scaled) > 0 else 10,
            symbol=node_symbol,
            color=node_color,
            opacity=1.0,
            line=dict(width=0.6, color="#4B5563"),
        ),
        name="Stock Nodes",
    )

    fig = go.Figure(data=edge_traces + [node_trace])
    fig.update_layout(
        title=title,
        scene=dict(xaxis=dict(title="X"), yaxis=dict(title="Y"), zaxis=dict(title="Z")),
        margin=dict(l=0, r=0, t=90, b=0),
        height=900,
    )
    return json.loads(fig.to_json())


def build_graph_response(query: GraphQuery, settings: ServerSettings) -> GraphResponse:
    selected_corp = str(query.search_stock).strip() if query.search_stock else None

    effective_max_edges = max(1, min(int(query.max_edges), SYSTEM_MAX_EDGES))
    cache_key = (
        settings.db_table,
        query.start_date,
        query.end_date,
        query.snapshot_date,
        query.search_stock,
        int(query.highlight_hops),
        effective_max_edges,
        query.db_limit,
        bool(query.include_periodic_status),
        bool(query.include_majorstock_status),
    )
    cached = _GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    snapshot_dates = _query_snapshot_dates(query, settings)
    if not snapshot_dates:
        empty_fig = go.Figure()
        empty_fig.update_layout(title="No data in selected range", height=850)
        status_text = "No data in selected range."
        if selected_corp:
            status_text = f"'{selected_corp}' 종목 데이터가 선택된 기간에 없습니다."
        response = GraphResponse(
            snapshot_dates=[],
            snapshot_date=None,
            selected_stock=selected_corp,
            rows=0,
            edges_shown=0,
            status_text=status_text,
            figure=json.loads(empty_fig.to_json()),
            top_edges=[],
        )
        _GRAPH_CACHE.set(cache_key, response)
        return response

    snapshot_date = snapshot_dates[-1]
    if query.snapshot_date:
        req = pd.to_datetime(query.snapshot_date, errors="coerce")
        if pd.isna(req):
            raise ValueError(f"Invalid snapshot_date: {query.snapshot_date}")
        req_date = req.date()
        candidates = [pd.to_datetime(d).date() for d in snapshot_dates if pd.to_datetime(d).date() <= req_date]
        snapshot_date = candidates[-1] if candidates else snapshot_dates[0]
    snapshot_date = str(snapshot_date)

    all_edges = _query_aggregated_edges(
        query=query,
        settings=settings,
        snapshot_date=snapshot_date,
        node_filter=None,
    )
    if all_edges.empty:
        empty_fig = go.Figure()
        empty_fig.update_layout(title="No data in selected range", height=850)
        response = GraphResponse(
            snapshot_dates=snapshot_dates,
            snapshot_date=snapshot_date,
            selected_stock=None,
            rows=0,
            edges_shown=0,
            status_text="No data in selected range.",
            figure=json.loads(empty_fig.to_json()),
            top_edges=[],
        )
        _GRAPH_CACHE.set(cache_key, response)
        return response

    all_nodes = sorted(set(all_edges["src"]).union(set(all_edges["dst"])))
    selected = selected_corp if selected_corp else _resolve_highlight_node(all_nodes, query.search_stock)
    keep_nodes: set[str] | None = None

    if selected and not selected_corp:
        keep_nodes = _k_hop_nodes_from_edges(all_edges, selected, query.highlight_hops)
        if keep_nodes:
            all_edges = all_edges[
                all_edges["src"].isin(keep_nodes) & all_edges["dst"].isin(keep_nodes)
            ].copy()

    edges = _aggregate_edges(all_edges, max_edges=effective_max_edges, pinned_node=selected)
    rows_count = _query_row_count(
        query=query,
        settings=settings,
        snapshot_date=snapshot_date,
        node_filter=keep_nodes if selected else None,
    )
    reporting_set = set(edges["src"].astype(str).unique()) if not edges.empty else set()
    title = f"Stock Relationship 3D Network | As-Of Snapshot Date: {snapshot_date}"
    if selected:
        title += f" | Highlight: {selected} (hop <= {query.highlight_hops})"

    figure = _build_figure_json(
        edges=edges,
        reporting_set=reporting_set,
        selected=selected,
        highlight_hops=query.highlight_hops,
        title=title,
    )

    if query.search_stock and not selected:
        status = (
            f"As-Of Snapshot Date: {snapshot_date} | Rows: {rows_count:,} | "
            f"Edges shown: {len(edges):,} | stock search '{query.search_stock}' not found"
        )
    else:
        status = (
            f"As-Of Snapshot Date: {snapshot_date} | Rows: {rows_count:,} | "
            f"Edges shown: {len(edges):,}" + (f" | Highlight: {selected}" if selected else "")
        )

    top = edges.sort_values("weight", ascending=False).head(20).copy()
    top_edges = [
        TopEdge(src=str(r["src"]), dst=str(r["dst"]), weight=float(r["weight"]))
        for _, r in top.iterrows()
    ]

    response = GraphResponse(
        snapshot_dates=[str(x) for x in snapshot_dates],
        snapshot_date=snapshot_date,
        selected_stock=selected,
        rows=int(rows_count),
        edges_shown=int(len(edges)),
        status_text=status,
        figure=figure,
        top_edges=top_edges,
    )
    _GRAPH_CACHE.set(cache_key, response)
    return response


def list_stock_options(
    *,
    start_date: str | None,
    end_date: str | None,
    q: str | None,
    limit: int,
    include_periodic_status: bool,
    include_majorstock_status: bool,
    settings: ServerSettings,
) -> list[str]:
    cache_key = (
        settings.db_table,
        start_date,
        end_date,
        q or "",
        int(limit),
        bool(include_periodic_status),
        bool(include_majorstock_status),
    )
    cached = _STOCK_CACHE.get(cache_key)
    if cached is not None:
        return cached

    where_sql, params = _build_base_where(
        start_date=start_date,
        end_date=end_date,
        snapshot_date=None,
        corp_name_filter=None,
        include_periodic_status=include_periodic_status,
        include_majorstock_status=include_majorstock_status,
    )

    extra = ""
    if q and q.strip():
        extra = " AND corp_name LIKE %s"
        params.append(f"%{q.strip()}%")

    sql = f"""
    SELECT DISTINCT TRIM(corp_name) AS name
    FROM {settings.db_table}
    WHERE {where_sql} {extra}
      AND corp_name IS NOT NULL
      AND TRIM(corp_name) <> ''
      AND TRIM(corp_name) <> '-'
      AND TRIM(corp_name) <> 'nan'
      AND TRIM(corp_name) <> 'None'
    """

    sql = f"""
    SELECT name FROM (
    {sql}
    ) u
    WHERE name IS NOT NULL AND name <> ''
    ORDER BY name ASC
    LIMIT %s
    """
    query_params = list(params) + [max(int(limit) * 4, int(limit), 1)]

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(query_params))
            rows = cur.fetchall()
    finally:
        conn.close()

    nodes = [str(r.get("name", "")).strip() for r in rows if str(r.get("name", "")).strip()]
    if q and q.strip():
        nq = _norm_name(q)
        nodes = [
            n for n in nodes
            if nq and _norm_name(n) and (nq in _norm_name(n) or _norm_name(n) in nq)
        ]
    result = nodes[: max(limit, 1)]
    _STOCK_CACHE.set(cache_key, result)
    return result
