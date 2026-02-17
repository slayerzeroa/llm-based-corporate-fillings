# stock_relationship_3d.py
import csv
import os
from collections import deque
from typing import Optional

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go


def _norm_yyyymmdd(s: str) -> str:
    raw = str(s).replace("-", "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"Invalid date format: {s} (YYYYMMDD or YYYY-MM-DD)")
    return raw


def _prepare_graph_input_csv(
    csv_path: str,
    input_source: str,
    *,
    api_key: str | None,
    investor: str | None,
    stock_code: str | None,
    corp_code: str | None,
    fetch_start: str | None,
    fetch_end: str | None,
    reprt_codes: tuple[str, ...],
    include_periodic_status: bool,
    include_majorstock_status: bool,
    include_transfer_note_plan: bool,
    max_note_reports: int,
) -> str:
    if input_source == "csv":
        return csv_path

    from config import load_settings
    from function import CorporateHoldingsModule

    settings = load_settings()
    use_api_key = (api_key or settings.dart_api_key or "").strip()
    if not use_api_key:
        raise RuntimeError(
            "DART API key not found. Set OPENDART_API_KEY (or DART_API_KEY) "
            "or pass --api-key."
        )

    if not fetch_start or not fetch_end:
        raise ValueError(
            "Module input requires --fetch-start and --fetch-end "
            "(or --start-date/--end-date as fallback)."
        )

    start_ymd = _norm_yyyymmdd(fetch_start)
    end_ymd = _norm_yyyymmdd(fetch_end)

    module = CorporateHoldingsModule(api_key=use_api_key)

    if investor and stock_code:
        raise ValueError("Use either --investor or --stock-code, not both.")

    lookup_investor = investor
    if stock_code:
        lookup_investor = str(stock_code).strip().zfill(6)

    if lookup_investor:
        resolved_code, resolved_name, resolved_stock, data = module.fetch_all_holding_dfs_by_investor(
            investor=lookup_investor,
            bgn_de=start_ymd,
            end_de=end_ymd,
            start_year=int(start_ymd[:4]),
            end_year=int(end_ymd[:4]),
            reprt_codes=reprt_codes,
            include_periodic_status=include_periodic_status,
            include_majorstock_status=include_majorstock_status,
            include_transfer_note_plan=include_transfer_note_plan,
            max_note_reports=max_note_reports,
        )
        print(
            f"[INFO] module input by investor/stock-code -> corp_code={resolved_code}, "
            f"corp_name={resolved_name}, stock_code={resolved_stock}"
        )
    elif corp_code:
        data = module.fetch_all_holding_dfs(
            corp_code=str(corp_code).zfill(8),
            bgn_de=start_ymd,
            end_de=end_ymd,
            start_year=int(start_ymd[:4]),
            end_year=int(end_ymd[:4]),
            reprt_codes=reprt_codes,
            include_periodic_status=include_periodic_status,
            include_majorstock_status=include_majorstock_status,
            include_transfer_note_plan=include_transfer_note_plan,
            max_note_reports=max_note_reports,
        )
        print(f"[INFO] module input by corp_code={str(corp_code).zfill(8)}")
    else:
        raise ValueError("Provide --investor, --stock-code, or --corp-code when --input-source module is used.")

    combined = data.get("combined", pd.DataFrame())
    if combined.empty:
        raise ValueError("Module returned an empty combined dataframe.")

    out_csv = csv_path or "./data/holdings_graph_input.csv"
    out_dir = os.path.dirname(out_csv)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    combined.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"[INFO] module combined rows={len(combined):,} saved -> {out_csv}")

    return out_csv


def _read_transfer_csv_robust(csv_path: str) -> pd.DataFrame:
    """
    Load transfer CSV robustly.

    Primary path:
    - normal pandas read_csv

    Fallback path:
    - line-by-line repair when malformed rows have extra commas
      (common in free-text fields like `trf_pp`)
    """
    try:
        return pd.read_csv(csv_path)
    except pd.errors.ParserError:
        pass

    encodings = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]
    last_err = None
    text = None
    for enc in encodings:
        try:
            with open(csv_path, "r", encoding=enc, newline="") as f:
                text = f.read()
            break
        except UnicodeDecodeError as e:
            last_err = e

    if text is None:
        raise RuntimeError(f"Could not decode CSV: {csv_path}") from last_err

    lines = text.splitlines()
    if not lines:
        return pd.DataFrame()

    header = next(csv.reader([lines[0]]))
    expected = len(header)

    repaired_rows = []
    bad_row_count = 0
    for raw in lines[1:]:
        if not raw.strip():
            continue
        row = next(csv.reader([raw]))

        if len(row) == expected:
            repaired_rows.append(row)
            continue

        bad_row_count += 1

        if len(row) > expected:
            # For this schema, extra commas usually originate from free-text fields.
            # Keep fixed columns, merge overflow into `trf_pp` slot, preserve tail columns.
            if expected >= 21:
                fixed = row[:16] + [",".join(row[16:-4])] + row[-4:]
            else:
                fixed = row[: expected - 1] + [",".join(row[expected - 1 :])]
            repaired_rows.append(fixed[:expected])
            continue

        # len(row) < expected
        repaired_rows.append(row + [""] * (expected - len(row)))

    df = pd.DataFrame(repaired_rows, columns=header)
    if bad_row_count > 0:
        print(f"[WARN] Repaired malformed CSV rows: {bad_row_count}")
    return df


def _aggregate_edges(df: pd.DataFrame, max_edges: int) -> pd.DataFrame:
    edges = (
        df.groupby(["corp_name", "iscmp_cmpnm"], as_index=False)["amount_abs"]
        .sum()
        .rename(columns={"corp_name": "src", "iscmp_cmpnm": "dst", "amount_abs": "weight"})
    )
    if len(edges) > max_edges:
        edges = edges.nlargest(max_edges, "weight").copy()
    return edges


def _prepare_network_dataframe(csv_path: str, only_last_1y: bool = False) -> pd.DataFrame:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    df = _read_transfer_csv_robust(csv_path)

    required_cols = ["corp_name", "iscmp_cmpnm", "trfdtl_trfprc", "rcept_dt"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df["rcept_dt"] = pd.to_datetime(df["rcept_dt"], errors="coerce")
    df = df.dropna(subset=["rcept_dt"]).copy()

    if only_last_1y and len(df) > 0:
        end_dt = df["rcept_dt"].max()
        start_dt = end_dt - pd.Timedelta(days=365)
        df = df[df["rcept_dt"] >= start_dt].copy()

    df["amount_abs"] = pd.to_numeric(df["trfdtl_trfprc"], errors="coerce").abs().fillna(0)
    df["corp_name"] = df["corp_name"].astype(str).str.strip()
    df["iscmp_cmpnm"] = df["iscmp_cmpnm"].astype(str).str.strip()

    # Legacy CSV safety filters:
    # - exclude periodic status snapshots
    # - exclude majorstock proxy snapshots
    # - exclude summary rows
    if "source" in df.columns:
        df = df[~df["source"].astype(str).str.contains("OTRCPR_INVSTMNT_STTUS", na=False)].copy()
        df = df[~df["source"].astype(str).str.contains("MAJORSTOCK_STKQY_PROXY", na=False)].copy()
    df = df[~df["iscmp_cmpnm"].astype(str).str.replace(r"\s+", "", regex=True).isin(["합계", "총계", "소계"])].copy()

    df = df[(df["corp_name"] != "") & (df["iscmp_cmpnm"] != "")]
    df = df[df["corp_name"] != df["iscmp_cmpnm"]].copy()

    if df.empty:
        raise ValueError("No valid data remains after preprocessing.")

    return df


def _filter_by_date_range(
    df: pd.DataFrame,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    out = df.copy()
    if start_date:
        start_ts = pd.to_datetime(start_date, errors="coerce")
        if pd.isna(start_ts):
            raise ValueError(f"Invalid start_date: {start_date}")
        out = out[out["rcept_dt"] >= start_ts]
    if end_date:
        end_ts = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(end_ts):
            raise ValueError(f"Invalid end_date: {end_date}")
        out = out[out["rcept_dt"] <= end_ts]
    if out.empty:
        raise ValueError("No data in the selected date range.")
    return out.copy()


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

    return None


def _related_nodes_undirected(G: nx.Graph, center: str, hops: int = 1) -> set[str]:
    if center not in G:
        return set()
    hops = max(int(hops), 0)
    seen = {center}
    dq = deque([(center, 0)])
    while dq:
        cur, d = dq.popleft()
        if d >= hops:
            continue
        for nb in G.neighbors(cur):
            if nb not in seen:
                seen.add(nb)
                dq.append((nb, d + 1))
    return seen


def _build_snapshot_traces(
    edges: pd.DataFrame,
    pos: dict,
    reporting_set: set[str],
    highlight_node: str | None,
    highlight_hops: int,
):
    def _hex_to_rgba(hex_color: str, alpha: float) -> str:
        h = hex_color.lstrip("#")
        if len(h) != 6:
            return f"rgba(120,120,120,{alpha})"
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"

    G = nx.DiGraph()
    for _, r in edges.iterrows():
        G.add_edge(r["src"], r["dst"], weight=float(r["weight"]))

    if G.number_of_nodes() == 0:
        return [], "No nodes in this snapshot"

    nodes = list(G.nodes())
    G_u = G.to_undirected()
    missing_nodes = [n for n in nodes if n not in pos]
    fallback_pos = nx.spring_layout(G, dim=3, seed=42, weight="weight") if missing_nodes else {}

    def _coord(node):
        if node in pos:
            return pos[node]
        if node in fallback_pos:
            return fallback_pos[node]
        return np.array([0.0, 0.0, 0.0])

    selected = highlight_node if (highlight_node in G) else None
    related = _related_nodes_undirected(G_u, selected, hops=highlight_hops) if selected else set()

    node_x, node_y, node_z = [], [], []
    node_size_raw, node_text = [], []
    node_symbol, node_color = [], []

    for n in nodes:
        x, y, z = _coord(n)
        node_x.append(x)
        node_y.append(y)
        node_z.append(z)

        in_w = sum(d.get("weight", 0.0) for _, _, d in G.in_edges(n, data=True))
        out_w = sum(d.get("weight", 0.0) for _, _, d in G.out_edges(n, data=True))
        total_w = in_w + out_w
        node_size_raw.append(total_w)

        ntype = "Reporting Entity" if n in reporting_set else "Investee"
        node_text.append(
            f"Name: {n}<br>"
            f"Type: {ntype}<br>"
            f"In Total: {in_w:,.0f}<br>"
            f"Out Total: {out_w:,.0f}<br>"
            f"Total Relationship Strength: {total_w:,.0f}"
        )

        node_symbol.append("diamond" if n in reporting_set else "circle")
        if selected:
            if n == selected:
                node_color.append(_hex_to_rgba("#E74C3C", 1.0))
            elif n in related:
                node_color.append(_hex_to_rgba("#2E86DE", 0.95))
            else:
                node_color.append(_hex_to_rgba("#B0B8C2", 0.20))
        else:
            base = "#2E86DE" if n in reporting_set else "#65A30D"
            node_color.append(_hex_to_rgba(base, 0.9))

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

    for _, r in edges.iterrows():
        u, v, w = r["src"], r["dst"], float(r["weight"])
        x0, y0, z0 = _coord(u)
        x1, y1, z1 = _coord(v)

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
                x=[x0, x1, None],
                y=[y0, y1, None],
                z=[z0, z1, None],
                mode="lines",
                line=dict(width=width, color=color),
                opacity=opacity,
                hovertemplate=f"{u} -> {v}<br>Relationship Strength (Amount): {w:,.0f}<extra></extra>",
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

    subtitle = ""
    if selected:
        subtitle = f" | Highlight: {selected} (hop <= {highlight_hops})"

    return edge_traces + [node_trace], subtitle


def _build_figure_for_filtered_df(
    dsub: pd.DataFrame,
    max_edges: int,
    highlight_stock: Optional[str],
    highlight_hops: int,
    title_prefix: str,
) -> tuple[go.Figure, pd.DataFrame, Optional[str]]:
    if dsub.empty:
        fig = go.Figure()
        fig.update_layout(
            title=f"{title_prefix} | No data in selected window",
            height=850,
            margin=dict(l=0, r=0, t=80, b=0),
        )
        return fig, pd.DataFrame(columns=["src", "dst", "weight"]), None

    edges = _aggregate_edges(dsub, max_edges=max_edges)
    if edges.empty:
        fig = go.Figure()
        fig.update_layout(
            title=f"{title_prefix} | No edge data in selected window",
            height=850,
            margin=dict(l=0, r=0, t=80, b=0),
        )
        return fig, pd.DataFrame(columns=["src", "dst", "weight"]), None

    G = nx.DiGraph()
    for _, r in edges.iterrows():
        G.add_edge(r["src"], r["dst"], weight=float(r["weight"]))

    pos = nx.spring_layout(G, dim=3, seed=42, weight="weight")
    reporting_set = set(dsub["corp_name"].unique())
    all_nodes = list(G.nodes())
    selected = _resolve_highlight_node(all_nodes, highlight_stock)

    traces, subtitle = _build_snapshot_traces(
        edges=edges,
        pos=pos,
        reporting_set=reporting_set,
        highlight_node=selected,
        highlight_hops=highlight_hops,
    )

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=f"{title_prefix}{subtitle}",
        scene=dict(xaxis=dict(title="X"), yaxis=dict(title="Y"), zaxis=dict(title="Z")),
        margin=dict(l=0, r=0, t=90, b=0),
        height=900,
    )

    top_edges = edges.sort_values("weight", ascending=False).head(20).copy()
    top_edges["weight"] = top_edges["weight"].round(0).astype("int64")
    return fig, top_edges, selected


def build_stock_relationship_3d(
    csv_path: str = "./sample_transfer_1y.csv",
    out_html: str = "./stock_relationship_3d.html",
    max_edges: int = 80,
    only_last_1y: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    highlight_stock: str | None = None,
    highlight_hops: int = 1,
    date_navigation: bool = False,
    date_freq: str = "D",
    cumulative_by_date: bool = True,
):
    """
    Build a 3D stock relationship network.
    - Node: stock/company name (corp_name + iscmp_cmpnm)
    - Edge: corp_name -> iscmp_cmpnm
    - Edge weight: sum(abs(trfdtl_trfprc))
    - Search: highlight a specific stock and related neighborhood
    - Date range: filter by start_date/end_date
    - Optional date navigation: one-date as-of snapshot slider
    - Output: interactive HTML
    """

    df = _prepare_network_dataframe(csv_path, only_last_1y=only_last_1y)
    df = _filter_by_date_range(df, start_date=start_date, end_date=end_date)

    # Build full graph once for stable coordinates and stock search target resolution.
    full_edges = _aggregate_edges(df, max_edges=max_edges)
    if full_edges.empty:
        raise ValueError("No edge data available.")

    G_full = nx.DiGraph()
    for _, r in full_edges.iterrows():
        G_full.add_edge(r["src"], r["dst"], weight=float(r["weight"]))
    if G_full.number_of_nodes() == 0:
        raise ValueError("Graph has no nodes.")

    pos = nx.spring_layout(G_full, dim=3, seed=42, weight="weight")
    reporting_set = set(df["corp_name"].unique())
    all_nodes = list(G_full.nodes())

    selected_node = _resolve_highlight_node(all_nodes, highlight_stock)
    if highlight_stock and not selected_node:
        print(f"[WARN] highlight_stock '{highlight_stock}' not found. Highlight disabled.")

    # Build snapshots (optional)
    snapshots = []
    if date_navigation:
        snapshot_days = sorted(df["rcept_dt"].dt.date.dropna().unique())
        if not snapshot_days:
            raise ValueError("No snapshot dates available for navigation.")

        for day in snapshot_days:
            if cumulative_by_date:
                dsub = df[df["rcept_dt"].dt.date <= day].copy()
            else:
                dsub = df[df["rcept_dt"].dt.date == day].copy()
            edges = _aggregate_edges(dsub, max_edges=max_edges)
            if edges.empty:
                continue
            label = str(day)
            snapshots.append((label, edges))
    else:
        snapshots.append(("All Dates", full_edges))

    if not snapshots:
        raise ValueError("No snapshot data available.")

    # Build all traces snapshot-by-snapshot and wire slider visibility.
    fig = go.Figure()
    trace_ranges = []
    titles = []

    for label, edges in snapshots:
        traces, subtitle = _build_snapshot_traces(
            edges=edges,
            pos=pos,
            reporting_set=reporting_set,
            highlight_node=selected_node,
            highlight_hops=highlight_hops,
        )
        start = len(fig.data)
        for tr in traces:
            tr.visible = False
            fig.add_trace(tr)
        end = len(fig.data)
        trace_ranges.append((start, end))
        titles.append(
            f"Stock Relationship 3D Network | Date: {label}"
            f"{subtitle} (edge = summed transaction/planned amount)"
        )

    # Show latest snapshot by default.
    default_idx = len(trace_ranges) - 1
    s0, e0 = trace_ranges[default_idx]
    for i in range(s0, e0):
        fig.data[i].visible = True

    # Snapshot slider controls (only when requested)
    if len(trace_ranges) > 1:
        steps = []
        for idx, (s, e) in enumerate(trace_ranges):
            visible = [False] * len(fig.data)
            for i in range(s, e):
                visible[i] = True
            steps.append(
                {
                    "method": "update",
                    "label": snapshots[idx][0],
                    "args": [
                        {"visible": visible},
                        {"title": titles[idx]},
                    ],
                }
            )

        fig.update_layout(
            sliders=[
                {
                    "active": default_idx,
                    "currentvalue": {"prefix": "Date: "},
                    "pad": {"t": 35},
                    "steps": steps,
                }
            ],
        )

    fig.update_layout(
        title=titles[default_idx],
        scene=dict(
            xaxis=dict(title="X"),
            yaxis=dict(title="Y"),
            zaxis=dict(title="Z"),
        ),
        margin=dict(l=0, r=0, t=90, b=0),
        height=900,
    )

    fig.write_html(out_html, include_plotlyjs="cdn")

    # Return top edges of the default snapshot.
    top_edges = snapshots[default_idx][1].sort_values("weight", ascending=False).head(20).copy()
    top_edges["weight"] = top_edges["weight"].round(0).astype("int64")
    return fig, top_edges, out_html


def run_stock_relationship_dashboard(
    csv_path: str = "./sample_transfer_1y.csv",
    max_edges: int = 80,
    only_last_1y: bool = False,
    host: str = "127.0.0.1",
    port: int = 8050,
    debug: bool = False,
):
    """
    Run real-time interactive dashboard:
    - stock search highlight
    - live one-date as-of snapshot navigation
    """
    try:
        from dash import Dash, dcc, html, Input, Output
    except Exception as e:
        raise RuntimeError("Dash is required for real-time mode. Install with: pip install dash") from e

    df = _prepare_network_dataframe(csv_path, only_last_1y=only_last_1y)
    snapshot_days = sorted(df["rcept_dt"].dt.date.dropna().unique())
    if not snapshot_days:
        raise ValueError("No valid snapshot dates found.")

    def _build_slider_marks(days: list) -> dict:
        n = len(days)
        if n <= 10:
            idxs = set(range(n))
        else:
            idxs = {0, n - 1}
            step = max(1, n // 8)
            idxs.update(range(0, n, step))
        return {int(i): str(days[i]) for i in sorted(idxs)}

    stock_options = sorted(set(df["corp_name"].astype(str).tolist()) | set(df["iscmp_cmpnm"].astype(str).tolist()))
    dropdown_options = [{"label": s, "value": s} for s in stock_options]

    app = Dash(__name__)
    app.title = "Stock Network Dashboard"

    app.layout = html.Div(
        [
            html.H3("Stock Relationship 3D Dashboard"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Search Stock"),
                            dcc.Dropdown(
                                id="stock-search",
                                options=dropdown_options,
                                placeholder="Type and select a stock/company...",
                                searchable=True,
                                clearable=True,
                            ),
                        ],
                        style={"width": "48%", "display": "inline-block", "verticalAlign": "top"},
                    ),
                    html.Div(
                        [
                            html.Label("Highlight Hop Radius"),
                            dcc.Slider(
                                id="highlight-hops",
                                min=0,
                                max=3,
                                step=1,
                                value=1,
                                marks={i: str(i) for i in range(4)},
                            ),
                        ],
                        style={"width": "48%", "display": "inline-block", "marginLeft": "2%"},
                    ),
                ]
            ),
            html.Div(
                [
                    html.Label("Snapshot Navigator (one date only)"),
                    dcc.Slider(
                        id="snapshot-index",
                        min=0,
                        max=len(snapshot_days) - 1,
                        step=1,
                        value=len(snapshot_days) - 1,
                        marks=_build_slider_marks(snapshot_days),
                        updatemode="drag",
                    ),
                ],
                style={"marginTop": "12px"},
            ),
            html.Div(id="status-text", style={"marginTop": "10px", "fontSize": "14px"}),
            dcc.Graph(id="network-graph", style={"height": "900px"}),
        ],
        style={"padding": "16px"},
    )

    @app.callback(
        Output("network-graph", "figure"),
        Output("status-text", "children"),
        Input("stock-search", "value"),
        Input("snapshot-index", "value"),
        Input("highlight-hops", "value"),
    )
    def _update_graph(stock_value, snapshot_index, hops):
        idx = int(snapshot_index) if snapshot_index is not None else len(snapshot_days) - 1
        idx = max(0, min(idx, len(snapshot_days) - 1))
        snapshot_day = snapshot_days[idx]
        dsub = df[df["rcept_dt"].dt.date <= snapshot_day].copy()
        title_prefix = (
            f"Stock Relationship 3D Network | As-Of Snapshot Date: {snapshot_day} "
            "(edge = summed transaction/planned amount)"
        )
        scope_text = f"As-Of Snapshot Date: {snapshot_day}"
        not_found_scope = "the selected as-of snapshot date"

        fig, top_edges, selected = _build_figure_for_filtered_df(
            dsub=dsub,
            max_edges=max_edges,
            highlight_stock=stock_value,
            highlight_hops=int(hops or 1),
            title_prefix=title_prefix,
        )

        if stock_value and not selected:
            note = f" | stock search '{stock_value}' not found in {not_found_scope}"
        else:
            note = ""
        status = (
            f"{scope_text} | Rows: {len(dsub):,} | Edges shown: {len(top_edges):,}"
            + (f" | Highlight: {selected}" if selected else "")
            + note
        )
        return fig, status

    print(f"[INFO] Dashboard running: http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dashboard", "static"], default="dashboard")
    parser.add_argument("--input-source", choices=["csv", "module"], default="module")
    parser.add_argument("--csv-path", default="./sample_transfer_1y.csv")
    parser.add_argument("--out-html", default="./stock_relationship_3d.html")
    parser.add_argument("--max-edges", type=int, default=80)
    parser.add_argument("--only-last-1y", action="store_true")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--fetch-start", default=None, help="YYYY-MM-DD or YYYYMMDD (module fetch range)")
    parser.add_argument("--fetch-end", default=None, help="YYYY-MM-DD or YYYYMMDD (module fetch range)")
    parser.add_argument("--api-key", default=None, help="DART API key override for module input")
    parser.add_argument("--investor", default=None, help="Investor name or code for module input")
    parser.add_argument("--stock-code", default=None, help="6-digit stock code for module input")
    parser.add_argument("--corp-code", default=None, help="8-digit corp_code for module input")
    parser.add_argument("--reprt-codes", default="11011", help="Comma-separated reprt_code values")
    parser.add_argument("--include-periodic-status", action="store_true", help="Include periodic status data (otrCprInvstmntSttus)")
    parser.add_argument("--include-majorstock-status", action="store_true", help="Include majorstock status proxy data")
    parser.add_argument("--exclude-note-plan", action="store_true", help="Disable note-plan extraction")
    parser.add_argument("--max-note-reports", type=int, default=200)
    parser.add_argument("--snapshot-nav", action="store_true", help="Enable one-date snapshot slider navigation")
    parser.add_argument("--highlight-stock", default="SK")
    parser.add_argument("--highlight-hops", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    reprt_codes = tuple(c.strip() for c in str(args.reprt_codes).split(",") if c.strip())
    fetch_start = args.fetch_start or args.start_date
    fetch_end = args.fetch_end or args.end_date
    input_csv = _prepare_graph_input_csv(
        csv_path=args.csv_path,
        input_source=args.input_source,
        api_key=args.api_key,
        investor=args.investor,
        stock_code=args.stock_code,
        corp_code=args.corp_code,
        fetch_start=fetch_start,
        fetch_end=fetch_end,
        reprt_codes=reprt_codes or ("11011",),
        include_periodic_status=args.include_periodic_status,
        include_majorstock_status=args.include_majorstock_status,
        include_transfer_note_plan=not args.exclude_note_plan,
        max_note_reports=args.max_note_reports,
    )

    if args.mode == "dashboard":
        run_stock_relationship_dashboard(
            csv_path=input_csv,
            max_edges=args.max_edges,
            only_last_1y=args.only_last_1y,
            host=args.host,
            port=args.port,
            debug=args.debug,
        )
    else:
        fig, top_edges, saved_path = build_stock_relationship_3d(
            csv_path=input_csv,
            out_html=args.out_html,
            max_edges=args.max_edges,
            only_last_1y=args.only_last_1y,
            start_date=args.start_date,
            end_date=args.end_date,
            highlight_stock=args.highlight_stock,
            highlight_hops=args.highlight_hops,
            date_navigation=args.snapshot_nav,
            date_freq="D",
            cumulative_by_date=True,
        )
        print(f"[DONE] interactive html saved: {saved_path}")
        print("\n[Top 20 edges]")
        print(top_edges.to_string(index=False))
