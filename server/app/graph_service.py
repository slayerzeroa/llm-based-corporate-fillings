from __future__ import annotations

import json
import re
from collections import deque

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import ServerSettings
from .db import get_connection
from .schemas import GraphQuery, GraphResponse, TopEdge

SYSTEM_MAX_EDGES = 50


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
        norm_contains = [n for n, nn in normalized if nq in nn or nn in nq]
        if norm_contains:
            return norm_contains[0]

    return None


def _aggregate_edges(df: pd.DataFrame, max_edges: int | None, pinned_node: str | None = None) -> pd.DataFrame:
    edges = (
        df.groupby(["corp_name", "iscmp_cmpnm"], as_index=False)["amount_abs"]
        .sum()
        .rename(columns={"corp_name": "src", "iscmp_cmpnm": "dst", "amount_abs": "weight"})
    )
    if max_edges is None or max_edges <= 0 or len(edges) <= max_edges:
        return edges

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


def _load_events_from_db(query: GraphQuery, settings: ServerSettings) -> pd.DataFrame:
    conditions = ["1=1"]
    params: list[object] = []

    if query.start_date:
        conditions.append("rcept_dt >= %s")
        params.append(_to_date_str(query.start_date))
    if query.end_date:
        conditions.append("rcept_dt <= %s")
        params.append(_to_date_str(query.end_date))

    sql = f"""
    SELECT
        rcept_no, rcept_dt, corp_cls, corp_code, report_nm, flr_nm, pblntf_ty,
        source, viewer_url, corp_name, iscmp_cmpnm, trfdtl_trfprc, trfdtl_stkcnt, trf_pp
    FROM {settings.db_table}
    WHERE {' AND '.join(conditions)}
    ORDER BY rcept_dt ASC, rcept_no ASC
    """
    if query.db_limit is not None and query.db_limit > 0:
        sql += " LIMIT %s"
        params.append(int(query.db_limit))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    finally:
        conn.close()

    return pd.DataFrame(rows)


def _preprocess(df: pd.DataFrame, include_periodic: bool, include_majorstock: bool) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out["rcept_dt"] = pd.to_datetime(out["rcept_dt"], errors="coerce")
    out = out.dropna(subset=["rcept_dt"]).copy()
    out["amount_abs"] = pd.to_numeric(out["trfdtl_trfprc"], errors="coerce").abs().fillna(0)
    out["corp_name"] = out["corp_name"].astype(str).str.strip()
    out["iscmp_cmpnm"] = out["iscmp_cmpnm"].astype(str).str.strip()

    if "source" in out.columns:
        if not include_periodic:
            out = out[~out["source"].astype(str).str.contains("OTRCPR_INVSTMNT_STTUS", na=False)].copy()
        if not include_majorstock:
            out = out[~out["source"].astype(str).str.contains("MAJORSTOCK_STKQY_PROXY", na=False)].copy()

    out = out[~out["iscmp_cmpnm"].str.replace(r"\s+", "", regex=True).isin(["합계", "총계", "소계"])].copy()
    out = out[(out["corp_name"] != "") & (out["iscmp_cmpnm"] != "")].copy()
    out = out[out["corp_name"] != out["iscmp_cmpnm"]].copy()
    return out.reset_index(drop=True)


def build_graph_response(query: GraphQuery, settings: ServerSettings) -> GraphResponse:
    raw = _load_events_from_db(query, settings)
    df = _preprocess(
        raw,
        include_periodic=query.include_periodic_status,
        include_majorstock=query.include_majorstock_status,
    )
    if df.empty:
        empty_fig = go.Figure()
        empty_fig.update_layout(title="No data in selected range", height=850)
        return GraphResponse(
            snapshot_dates=[],
            snapshot_date=None,
            selected_stock=None,
            rows=0,
            edges_shown=0,
            status_text="No data in selected range.",
            figure=json.loads(empty_fig.to_json()),
            top_edges=[],
        )

    snapshot_dates = sorted(df["rcept_dt"].dt.date.dropna().unique())
    snapshot_date = snapshot_dates[-1]
    if query.snapshot_date:
        req = pd.to_datetime(query.snapshot_date, errors="coerce")
        if pd.isna(req):
            raise ValueError(f"Invalid snapshot_date: {query.snapshot_date}")
        req_date = req.date()
        candidates = [d for d in snapshot_dates if d <= req_date]
        snapshot_date = candidates[-1] if candidates else snapshot_dates[0]

    dsub = df[df["rcept_dt"].dt.date <= snapshot_date].copy()
    all_edges = _aggregate_edges(dsub, max_edges=None)

    all_nodes = sorted(set(all_edges["src"]).union(set(all_edges["dst"])))
    selected = _resolve_highlight_node(all_nodes, query.search_stock)

    if selected:
        keep_nodes = _k_hop_nodes_from_edges(all_edges, selected, query.highlight_hops)
        if keep_nodes:
            dsub = dsub[
                dsub["corp_name"].isin(keep_nodes) & dsub["iscmp_cmpnm"].isin(keep_nodes)
            ].copy()
            all_edges = all_edges[
                all_edges["src"].isin(keep_nodes) & all_edges["dst"].isin(keep_nodes)
            ].copy()

    effective_max_edges = max(1, min(int(query.max_edges), SYSTEM_MAX_EDGES))
    edges = _aggregate_edges(dsub, max_edges=effective_max_edges, pinned_node=selected)
    reporting_set = set(dsub["corp_name"].unique())
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
            f"As-Of Snapshot Date: {snapshot_date} | Rows: {len(dsub):,} | "
            f"Edges shown: {len(edges):,} | stock search '{query.search_stock}' not found"
        )
    else:
        status = (
            f"As-Of Snapshot Date: {snapshot_date} | Rows: {len(dsub):,} | "
            f"Edges shown: {len(edges):,}" + (f" | Highlight: {selected}" if selected else "")
        )

    top = edges.sort_values("weight", ascending=False).head(20).copy()
    top_edges = [
        TopEdge(src=str(r["src"]), dst=str(r["dst"]), weight=float(r["weight"]))
        for _, r in top.iterrows()
    ]

    return GraphResponse(
        snapshot_dates=[str(x) for x in snapshot_dates],
        snapshot_date=str(snapshot_date),
        selected_stock=selected,
        rows=int(len(dsub)),
        edges_shown=int(len(edges)),
        status_text=status,
        figure=figure,
        top_edges=top_edges,
    )


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
    query = GraphQuery(
        start_date=start_date,
        end_date=end_date,
        db_limit=None,
        include_periodic_status=include_periodic_status,
        include_majorstock_status=include_majorstock_status,
    )
    raw = _load_events_from_db(query, settings)
    df = _preprocess(raw, include_periodic_status, include_majorstock_status)
    if df.empty:
        return []

    nodes = sorted(set(df["corp_name"].astype(str).tolist()) | set(df["iscmp_cmpnm"].astype(str).tolist()))
    if q and q.strip():
        nq = _norm_name(q)
        nodes = [n for n in nodes if nq in _norm_name(n) or _norm_name(n) in nq]
    return nodes[: max(limit, 1)]
