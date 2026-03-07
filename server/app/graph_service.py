from __future__ import annotations

import json
import re
from collections import deque
from collections import OrderedDict
from threading import Lock
import time
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import ServerSettings, get_settings
from .db import get_connection
from .schemas import GraphQuery, GraphResponse, TopEdge

SYSTEM_MAX_EDGES = 50
_BOOT_SETTINGS = get_settings()
GRAPH_CACHE_TTL_SEC = _BOOT_SETTINGS.graph_cache_ttl_sec
GRAPH_CACHE_MAX_ITEMS = _BOOT_SETTINGS.graph_cache_max_items
STOCK_CACHE_TTL_SEC = _BOOT_SETTINGS.stock_cache_ttl_sec
STOCK_CACHE_MAX_ITEMS = _BOOT_SETTINGS.stock_cache_max_items


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


def _empty_figure(title: str, *, include_figure: bool, height: int = 850) -> dict[str, Any]:
    if not include_figure:
        return {"data": [], "layout": {"title": title, "height": height}}
    fig = go.Figure()
    fig.update_layout(title=title, height=height)
    return json.loads(fig.to_json())


def _fetchall(
    sql: str,
    params: list[object] | tuple[object, ...] | None = None,
    *,
    conn=None,
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    use_conn = conn or get_connection()
    try:
        with use_conn.cursor() as cur:
            cur.execute(sql, tuple(params or []))
            rows = cur.fetchall()
            return rows or []
    finally:
        if owns_conn:
            use_conn.close()


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


def _build_date_filters(
    *,
    column_sql: str,
    start_date: str | None,
    end_date: str | None,
    snapshot_date: str | None = None,
) -> tuple[list[str], list[object]]:
    conditions: list[str] = []
    params: list[object] = []
    if start_date:
        conditions.append(f"{column_sql} >= %s")
        params.append(_to_date_str(start_date))
    if end_date:
        conditions.append(f"{column_sql} <= %s")
        params.append(_to_date_str(end_date))
    if snapshot_date:
        conditions.append(f"{column_sql} <= %s")
        params.append(_to_date_str(snapshot_date))
    return conditions, params


def _build_source_filter(
    *,
    alias: str,
    include_periodic_status: bool,
    include_majorstock_status: bool,
) -> tuple[str, list[object]]:
    conditions: list[str] = []
    params: list[object] = []

    if not include_periodic_status:
        conditions.append(
            f"({alias}.source IS NULL OR ({alias}.source NOT LIKE %s AND {alias}.source NOT LIKE %s))"
        )
        params.extend(["%OTRCPR_INVSTMNT_STTUS%", "%OTCPR_STK_INH_DECSN%"])

    if not include_majorstock_status:
        conditions.append(f"({alias}.source IS NULL OR {alias}.source NOT LIKE %s)")
        params.append("%MAJORSTOCK_STKQY_PROXY%")

    if not conditions:
        return "1=1", []
    return " AND ".join(conditions), params


def _safe_trimmed(value: Any) -> str:
    return str(value or "").strip()


def _resolve_search_corp_codes(
    *,
    search_stock: str,
    query: GraphQuery,
    settings: ServerSettings,
    conn=None,
) -> tuple[set[str], str | None]:
    needle = _safe_trimmed(search_stock)
    if not needle:
        return set(), None
    if re.fullmatch(r"\d{8}", needle):
        return {needle}, needle

    conditions = [
        "d.corp_code IS NOT NULL",
        "TRIM(d.corp_code) <> ''",
        "d.corp_name IS NOT NULL",
        "TRIM(d.corp_name) <> ''",
        "d.rcept_dt IS NOT NULL",
        "d.corp_name LIKE %s",
    ]
    params: list[object] = [f"%{needle}%"]

    date_conds, date_params = _build_date_filters(
        column_sql="d.rcept_dt",
        start_date=query.start_date,
        end_date=query.end_date,
    )
    conditions.extend(date_conds)
    params.extend(date_params)

    sql = f"""
    SELECT DISTINCT
        TRIM(d.corp_code) AS corp_code,
        TRIM(d.corp_name) AS corp_name
    FROM {settings.db_disclosures_raw_table} d
    WHERE {' AND '.join(conditions)}
    ORDER BY d.rcept_dt DESC, d.rcept_no DESC
    LIMIT 500
    """

    rows = _fetchall(sql, params, conn=conn)

    if not rows:
        return set(), None

    q_lower = needle.lower()
    q_norm = _norm_name(needle)
    scored: list[tuple[float, str, str]] = []
    for row in rows:
        code = _safe_trimmed(row.get("corp_code"))
        name = _safe_trimmed(row.get("corp_name"))
        if not code or not name:
            continue
        n_lower = name.lower()
        n_norm = _norm_name(name)
        score = 0.0
        if n_lower == q_lower:
            score += 100.0
        if q_norm and n_norm == q_norm:
            score += 95.0
        if q_lower and q_lower in n_lower:
            score += 50.0
        if q_norm and n_norm and q_norm in n_norm:
            score += 45.0
        score -= min(len(name), 200) * 0.01
        scored.append((score, code, name))

    if not scored:
        return set(), None

    best = max(x[0] for x in scored)
    picked = [(c, n) for s, c, n in scored if s >= best - 2.0]
    codes = {c for c, _ in picked if c}
    if not codes:
        return set(), None
    label = sorted([n for _, n in picked if n], key=len)[0]
    return codes, label


def _query_snapshot_dates(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    corp_codes: set[str] | None,
    conn=None,
) -> list[str]:
    conditions = ["s.as_of_date IS NOT NULL"]
    params: list[object] = []

    date_conds, date_params = _build_date_filters(
        column_sql="s.as_of_date",
        start_date=query.start_date,
        end_date=query.end_date,
    )
    conditions.extend(date_conds)
    params.extend(date_params)

    if corp_codes:
        codes = sorted(corp_codes)
        placeholders = ",".join(["%s"] * len(codes))
        conditions.append(f"s.corp_code IN ({placeholders})")
        params.extend(codes)

    src_cond, src_params = _build_source_filter(
        alias="l",
        include_periodic_status=query.include_periodic_status,
        include_majorstock_status=query.include_majorstock_status,
    )
    if src_cond == "1=1":
        sql = f"""
        SELECT DISTINCT s.as_of_date
        FROM {settings.db_edge_state_daily_table} s
        WHERE {' AND '.join(conditions)}
        ORDER BY s.as_of_date ASC
        """
        query_params = params
    else:
        sql = f"""
        WITH eligible_lines AS (
            SELECT DISTINCT l.rcept_no, l.corp_code
            FROM {settings.db_investment_lines_raw_table} l
            WHERE {src_cond}
        )
        SELECT DISTINCT s.as_of_date
        FROM {settings.db_edge_state_daily_table} s
        INNER JOIN eligible_lines el
          ON el.rcept_no = s.last_rcept_no
         AND el.corp_code = s.corp_code
        WHERE {' AND '.join(conditions)}
        ORDER BY s.as_of_date ASC
        """
        query_params = list(src_params) + params

    rows = _fetchall(sql, query_params, conn=conn)

    out = []
    for row in rows:
        dt = row.get("as_of_date")
        if dt is None:
            continue
        out.append(str(pd.to_datetime(dt).date()))
    return out


def _query_aggregated_edges(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    snapshot_date: str,
    corp_codes: set[str] | None = None,
    pre_limit: int | None = None,
    use_current_state: bool = False,
    conn=None,
) -> pd.DataFrame:
    src_cond, src_params = _build_source_filter(
        alias="l",
        include_periodic_status=query.include_periodic_status,
        include_majorstock_status=query.include_majorstock_status,
    )

    conditions = [
        "COALESCE(s.active_flag, 0) = 1",
        "COALESCE(CAST(s.holding_shares AS DECIMAL(30,6)), 0) > 0",
    ]
    filter_params: list[object] = []
    snapshot_date_sql = _to_date_str(snapshot_date)

    if corp_codes:
        codes = sorted(corp_codes)
        placeholders = ",".join(["%s"] * len(codes))
        conditions.append(f"s.corp_code IN ({placeholders})")
        filter_params.extend(codes)

    corp_name_sql = f"""
    SELECT t.corp_code, t.corp_name
    FROM (
        SELECT
            d.corp_code,
            TRIM(d.corp_name) AS corp_name,
            ROW_NUMBER() OVER (
                PARTITION BY d.corp_code
                ORDER BY d.rcept_dt DESC, d.rcept_no DESC
            ) AS rn
        FROM {settings.db_disclosures_raw_table} d
        WHERE d.corp_code IS NOT NULL
          AND d.rcept_dt IS NOT NULL
          AND d.rcept_dt <= %s
    ) t
    WHERE t.rn = 1
    """

    ctes: list[str] = []
    if src_cond != "1=1":
        ctes.append(
            f"""
            eligible_lines AS (
                SELECT DISTINCT l.rcept_no, l.corp_code
                FROM {settings.db_investment_lines_raw_table} l
                WHERE {src_cond}
            )
            """
        )
    if use_current_state:
        ctes.append(
            f"""
            state_src AS (
                SELECT *
                FROM {settings.db_edge_state_current_table}
            )
            """
        )
    else:
        ctes.append(
            f"""
            state_src AS (
                SELECT z.*
                FROM (
                    SELECT
                        sd.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                sd.corp_code,
                                COALESCE(sd.target_name_norm, ''),
                                COALESCE(sd.investee_id, 0)
                            ORDER BY
                                sd.as_of_date DESC,
                                COALESCE(sd.last_edge_event_id, 0) DESC
                        ) AS rn
                    FROM {settings.db_edge_state_daily_table} sd
                    WHERE sd.as_of_date <= %s
                ) z
                WHERE z.rn = 1
            )
            """
        )
    ctes.append(
        f"""
        corp_latest AS (
            {corp_name_sql}
        )
        """
    )

    base_from = "state_src s"
    if src_cond != "1=1":
        base_from += """
    INNER JOIN eligible_lines el
      ON el.rcept_no = s.last_rcept_no
     AND el.corp_code = s.corp_code
        """

    sql = f"""
    WITH {",".join(ctes)}
    SELECT
        COALESCE(NULLIF(TRIM(cl.corp_name), ''), s.corp_code) AS src,
        COALESCE(
            NULLIF(TRIM(i.name_canonical), ''),
            NULLIF(TRIM(s.target_name_norm), ''),
            CONCAT('investee:', COALESCE(CAST(s.investee_id AS CHAR), 'unknown'))
        ) AS dst,
        CAST(COALESCE(s.holding_shares, 0) AS DECIMAL(30,6)) AS net_weight,
        CAST(
            CASE
                WHEN COALESCE(CAST(s.holding_shares AS DECIMAL(30,6)), 0) > 0
                    THEN CASE
                        WHEN COALESCE(CAST(s.holding_amount AS DECIMAL(30,6)), 0) > 0
                            THEN CAST(s.holding_amount AS DECIMAL(30,6))
                        ELSE CAST(s.holding_shares AS DECIMAL(30,6))
                    END
                ELSE 0
            END
            AS DECIMAL(30,6)
        ) AS weight
    FROM {base_from}
    LEFT JOIN {settings.db_investee_dim_table} i
      ON i.investee_id = s.investee_id
    LEFT JOIN corp_latest cl
      ON cl.corp_code = s.corp_code
    WHERE {' AND '.join(conditions)}
    """
    query_params: list[object] = []
    if src_cond != "1=1":
        query_params.extend(src_params)
    if not use_current_state:
        query_params.append(snapshot_date_sql)
    query_params.append(snapshot_date_sql)
    query_params.extend(filter_params)
    if pre_limit is not None and int(pre_limit) > 0:
        sql += "\nORDER BY weight DESC\nLIMIT %s"
        query_params.append(int(pre_limit))

    rows = _fetchall(sql, query_params, conn=conn)

    edges = pd.DataFrame(rows)
    if edges.empty:
        return pd.DataFrame(columns=["src", "dst", "weight", "net_weight"])
    edges["src"] = edges["src"].astype(str)
    edges["dst"] = edges["dst"].astype(str)
    edges["weight"] = pd.to_numeric(edges["weight"], errors="coerce").fillna(0.0)
    edges["net_weight"] = pd.to_numeric(edges["net_weight"], errors="coerce").fillna(0.0)
    edges = edges[edges["weight"] > 0].copy()
    return edges


def _query_investing_history(
    query: GraphQuery,
    settings: ServerSettings,
    *,
    snapshot_date: str,
    corp_codes: set[str] | None = None,
    node_filter: set[str] | None = None,
    limit: int = 500,
    conn=None,
) -> list[dict[str, object]]:
    conditions = ["COALESCE(e.effective_dt, d.rcept_dt) IS NOT NULL"]
    params: list[object] = []

    date_conds, date_params = _build_date_filters(
        column_sql="COALESCE(e.effective_dt, d.rcept_dt)",
        start_date=query.start_date,
        end_date=query.end_date,
        snapshot_date=snapshot_date,
    )
    conditions.extend(date_conds)
    params.extend(date_params)

    if corp_codes:
        codes = sorted(corp_codes)
        placeholders = ",".join(["%s"] * len(codes))
        conditions.append(f"e.corp_code IN ({placeholders})")
        params.extend(codes)

    src_cond, src_params = _build_source_filter(
        alias="la",
        include_periodic_status=query.include_periodic_status,
        include_majorstock_status=query.include_majorstock_status,
    )

    sql = f"""
    WITH filtered_events AS (
        SELECT
            e.edge_event_id,
            e.rcept_no,
            e.corp_code,
            e.corp_name,
            e.investee_id,
            e.target_name_norm,
            e.reason_text,
            e.delta_amount,
            e.delta_shares,
            e.event_effect_sign,
            e.event_action,
            d.rcept_dt,
            d.report_nm,
            d.viewer_url,
            COALESCE(e.effective_dt, d.rcept_dt) AS sort_dt
        FROM {settings.db_edge_events_table} e
        LEFT JOIN {settings.db_disclosures_raw_table} d
          ON d.rcept_no = e.rcept_no
        WHERE {' AND '.join(conditions)}
        ORDER BY
            sort_dt DESC,
            e.rcept_no DESC,
            e.edge_event_id DESC
        LIMIT %s
    ),
    targets AS (
        SELECT DISTINCT
            fe.rcept_no,
            fe.corp_code,
            COALESCE(NULLIF(TRIM(fe.target_name_norm), ''), '') AS target_name_norm
        FROM filtered_events fe
    ),
    line_agg AS (
        SELECT
            l.rcept_no,
            l.corp_code,
            COALESCE(NULLIF(TRIM(l.iscmp_cmpnm_norm), ''), '') AS target_name_norm,
            MAX(NULLIF(TRIM(l.iscmp_cmpnm_raw), '')) AS iscmp_cmpnm_raw,
            MAX(l.source) AS source,
            MAX(l.viewer_url) AS viewer_url,
            MAX(l.trf_pp) AS trf_pp,
            MAX(l.trfdtl_trfprc) AS trfdtl_trfprc,
            MAX(l.trfdtl_stkcnt) AS trfdtl_stkcnt
        FROM {settings.db_investment_lines_raw_table} l
        INNER JOIN targets t
          ON t.rcept_no = l.rcept_no
         AND t.corp_code = l.corp_code
         AND t.target_name_norm = COALESCE(NULLIF(TRIM(l.iscmp_cmpnm_norm), ''), '')
        GROUP BY
            l.rcept_no,
            l.corp_code,
            COALESCE(NULLIF(TRIM(l.iscmp_cmpnm_norm), ''), '')
    )
    SELECT
        fe.rcept_dt,
        fe.rcept_no,
        COALESCE(NULLIF(TRIM(fe.corp_name), ''), fe.corp_code) AS corp_name,
        COALESCE(
            NULLIF(TRIM(la.iscmp_cmpnm_raw), ''),
            NULLIF(TRIM(i.name_canonical), ''),
            NULLIF(TRIM(fe.target_name_norm), ''),
            ''
        ) AS iscmp_cmpnm,
        COALESCE(fe.report_nm, '') AS report_nm,
        COALESCE(la.trf_pp, fe.reason_text, '') AS trf_pp,
        COALESCE(la.source, '') AS source,
        COALESCE(la.viewer_url, fe.viewer_url, '') AS viewer_url,
        COALESCE(la.trfdtl_trfprc, fe.delta_amount) AS trfdtl_trfprc,
        COALESCE(la.trfdtl_stkcnt, fe.delta_shares) AS trfdtl_stkcnt,
        fe.event_effect_sign,
        fe.event_action
    FROM filtered_events fe
    LEFT JOIN line_agg la
      ON la.rcept_no = fe.rcept_no
     AND la.corp_code = fe.corp_code
     AND la.target_name_norm = COALESCE(NULLIF(TRIM(fe.target_name_norm), ''), '')
    LEFT JOIN {settings.db_investee_dim_table} i
      ON i.investee_id = fe.investee_id
    WHERE {src_cond}
    ORDER BY
        fe.sort_dt DESC,
        fe.rcept_no DESC,
        fe.edge_event_id DESC
    """
    query_params = list(params)
    query_params.append(max(int(limit), 1))
    if src_cond != "1=1":
        query_params.extend(src_params)

    rows = _fetchall(sql, query_params, conn=conn)

    out: list[dict[str, object]] = []
    for row in rows or []:
        corp_name = _safe_trimmed(row.get("corp_name"))
        cmp_name = _safe_trimmed(row.get("iscmp_cmpnm"))
        if cmp_name in {",", "및", "-", "nan", "None", "null", "NULL"}:
            cmp_name = ""

        if node_filter:
            if _norm_name(corp_name) not in node_filter:
                continue
            if _norm_name(cmp_name) not in node_filter:
                continue

        amt = pd.to_numeric(row.get("trfdtl_trfprc"), errors="coerce")
        stk = pd.to_numeric(row.get("trfdtl_stkcnt"), errors="coerce")
        sign_num = pd.to_numeric(row.get("event_effect_sign"), errors="coerce")
        action = _safe_trimmed(row.get("event_action")).upper()
        if pd.notna(sign_num) and float(sign_num) < 0:
            direction = "OUT"
        elif pd.notna(sign_num) and float(sign_num) > 0:
            direction = "IN"
        elif action in {"DISPOSE", "PLAN_DISPOSE", "CLOSE", "CANCEL_EXECUTED", "CANCEL_PLAN"}:
            direction = "OUT"
        else:
            direction = "IN"

        rcept_no = _safe_trimmed(row.get("rcept_no"))
        viewer_url = _safe_trimmed(row.get("viewer_url"))
        if not viewer_url and rcept_no:
            viewer_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

        dt = row.get("rcept_dt")
        dt_str = None if dt is None else str(pd.to_datetime(dt).date())

        out.append(
            {
                "rcept_dt": dt_str,
                "rcept_no": rcept_no,
                "corp_name": corp_name,
                "iscmp_cmpnm": cmp_name,
                "report_nm": _safe_trimmed(row.get("report_nm")),
                "trf_pp": _safe_trimmed(row.get("trf_pp")),
                "source": _safe_trimmed(row.get("source")),
                "viewer_url": viewer_url,
                "trfdtl_trfprc": None if pd.isna(amt) else float(amt),
                "trfdtl_stkcnt": None if pd.isna(stk) else float(stk),
                "event_direction": direction,
            }
        )
    return out


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
    selected_query = _safe_trimmed(query.search_stock) or None
    effective_max_edges = max(1, min(int(query.max_edges), SYSTEM_MAX_EDGES))

    table_sig = (
        settings.db_disclosures_raw_table,
        settings.db_edge_state_daily_table,
        settings.db_edge_events_table,
        settings.db_investment_lines_raw_table,
        settings.db_investee_dim_table,
    )
    cache_key = (
        table_sig,
        query.start_date,
        query.end_date,
        query.snapshot_date,
        selected_query,
        int(query.highlight_hops),
        effective_max_edges,
        query.db_limit,
        int(query.history_limit),
        bool(query.include_periodic_status),
        bool(query.include_majorstock_status),
        bool(query.include_figure),
        bool(query.include_history),
        bool(query.include_top_edges),
    )
    cached = _GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    pre_limit = int(query.db_limit) if query.db_limit else max(effective_max_edges * 80, 5000)

    conn = get_connection()
    try:
        corp_codes: set[str] | None = None
        resolved_name: str | None = None
        if selected_query:
            corp_codes, resolved_name = _resolve_search_corp_codes(
                search_stock=selected_query,
                query=query,
                settings=settings,
                conn=conn,
            )
            if not corp_codes:
                response = GraphResponse(
                    snapshot_dates=[],
                    snapshot_date=None,
                    selected_stock=selected_query,
                    rows=0,
                    edges_shown=0,
                    status_text=f"search_stock '{selected_query}' not found in corp_name",
                    figure=_empty_figure("No data in selected range", include_figure=query.include_figure),
                    nodes=[],
                    edges=[],
                    top_edges=[],
                    investing_history=[],
                )
                _GRAPH_CACHE.set(cache_key, response)
                return response

        snapshot_dates = _query_snapshot_dates(
            query=query,
            settings=settings,
            corp_codes=corp_codes,
            conn=conn,
        )
        if not snapshot_dates:
            response = GraphResponse(
                snapshot_dates=[],
                snapshot_date=None,
                selected_stock=resolved_name or selected_query,
                rows=0,
                edges_shown=0,
                status_text="No data in selected range.",
                figure=_empty_figure("No data in selected range", include_figure=query.include_figure),
                nodes=[],
                edges=[],
                top_edges=[],
                investing_history=[],
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
            snapshot_date = candidates[-1] if candidates else pd.to_datetime(snapshot_dates[0]).date()
        snapshot_date = str(snapshot_date)
        use_current_state = bool(snapshot_dates) and snapshot_date == str(snapshot_dates[-1])

        all_edges = _query_aggregated_edges(
            query=query,
            settings=settings,
            snapshot_date=snapshot_date,
            corp_codes=corp_codes,
            pre_limit=pre_limit,
            use_current_state=use_current_state,
            conn=conn,
        )
        if all_edges.empty:
            response = GraphResponse(
                snapshot_dates=[str(x) for x in snapshot_dates],
                snapshot_date=snapshot_date,
                selected_stock=resolved_name or selected_query,
                rows=0,
                edges_shown=0,
                status_text="No active holding edges in selected snapshot.",
                figure=_empty_figure("No active holding edges", include_figure=query.include_figure),
                nodes=[],
                edges=[],
                top_edges=[],
                investing_history=[],
            )
            _GRAPH_CACHE.set(cache_key, response)
            return response

        all_nodes = sorted(set(all_edges["src"]).union(set(all_edges["dst"])))
        selected = _resolve_highlight_node(all_nodes, resolved_name or selected_query)
        keep_nodes: set[str] | None = None
        keep_nodes_norm: set[str] | None = None
        if selected and not selected_query:
            keep_nodes = _k_hop_nodes_from_edges(all_edges, selected, query.highlight_hops)
            keep_nodes_norm = {_norm_name(x) for x in keep_nodes}
            if keep_nodes:
                all_edges = all_edges[
                    all_edges["src"].isin(keep_nodes) & all_edges["dst"].isin(keep_nodes)
                ].copy()

        rows_count = int(len(all_edges))
        edges = _aggregate_edges(all_edges, max_edges=effective_max_edges, pinned_node=selected)
        history_rows: list[dict[str, object]] = []
        if query.include_history:
            history_rows = _query_investing_history(
                query=query,
                settings=settings,
                snapshot_date=snapshot_date,
                corp_codes=corp_codes,
                node_filter=keep_nodes_norm if selected else None,
                limit=query.history_limit,
                conn=conn,
            )
    finally:
        conn.close()

    reporting_set = set(edges["src"].astype(str).unique()) if not edges.empty else set()
    title = f"Stock Relationship 3D Network (State Snapshot) | As-Of Snapshot Date: {snapshot_date}"
    if selected:
        title += f" | Highlight: {selected} (hop <= {query.highlight_hops})"

    if query.include_figure:
        figure = _build_figure_json(
            edges=edges,
            reporting_set=reporting_set,
            selected=selected,
            highlight_hops=query.highlight_hops,
            title=title,
        )
    else:
        figure = _empty_figure(title, include_figure=False, height=900)

    status = (
        f"As-Of Snapshot Date: {snapshot_date} | Rows: {rows_count:,} | "
        f"Edges shown: {len(edges):,} | active holding only"
        + (f" | Highlight: {selected}" if selected else "")
    )

    edge_rows = [
        TopEdge(src=str(r["src"]), dst=str(r["dst"]), weight=float(r["weight"]))
        for _, r in edges.iterrows()
    ]
    top_edges: list[TopEdge] = []
    if query.include_top_edges:
        top = edges.sort_values("weight", ascending=False).head(20).copy()
        top_edges = [
            TopEdge(src=str(r["src"]), dst=str(r["dst"]), weight=float(r["weight"]))
            for _, r in top.iterrows()
        ]
    node_rows = sorted(set(edges["src"].astype(str)).union(set(edges["dst"].astype(str))))

    response = GraphResponse(
        snapshot_dates=[str(x) for x in snapshot_dates],
        snapshot_date=snapshot_date,
        selected_stock=selected or resolved_name or selected_query,
        rows=rows_count,
        edges_shown=int(len(edges)),
        status_text=status,
        figure=figure,
        nodes=node_rows,
        edges=edge_rows,
        top_edges=top_edges,
        investing_history=history_rows,
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
    conn=None,
) -> list[str]:
    table_sig = (
        settings.db_disclosures_raw_table,
        settings.db_investment_lines_raw_table,
        settings.db_edge_events_table,
    )
    cache_key = (
        table_sig,
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

    conditions = [
        "d.rcept_dt IS NOT NULL",
        "d.corp_name IS NOT NULL",
        "TRIM(d.corp_name) <> ''",
        "TRIM(d.corp_name) <> '-'",
        "TRIM(d.corp_name) <> 'nan'",
        "TRIM(d.corp_name) <> 'None'",
        "TRIM(d.corp_code) <> ''",
        (
            "EXISTS ("
            f"SELECT 1 FROM {settings.db_edge_events_table} e "
            "WHERE e.rcept_no = d.rcept_no AND e.corp_code = d.corp_code"
            ")"
        ),
    ]
    params: list[object] = []

    date_conds, date_params = _build_date_filters(
        column_sql="d.rcept_dt",
        start_date=start_date,
        end_date=end_date,
        snapshot_date=None,
    )
    conditions.extend(date_conds)
    params.extend(date_params)

    if q and q.strip():
        conditions.append("d.corp_name LIKE %s")
        params.append(f"%{q.strip()}%")

    src_cond, src_params = _build_source_filter(
        alias="l",
        include_periodic_status=include_periodic_status,
        include_majorstock_status=include_majorstock_status,
    )
    if src_cond == "1=1":
        sql = f"""
        SELECT DISTINCT TRIM(d.corp_name) AS name
        FROM {settings.db_disclosures_raw_table} d
        WHERE {' AND '.join(conditions)}
        ORDER BY name ASC
        LIMIT %s
        """
        query_params = list(params) + [max(int(limit) * 4, int(limit), 1)]
    else:
        sql = f"""
        WITH eligible_lines AS (
            SELECT DISTINCT l.rcept_no, l.corp_code
            FROM {settings.db_investment_lines_raw_table} l
            WHERE {src_cond}
        )
        SELECT DISTINCT TRIM(d.corp_name) AS name
        FROM {settings.db_disclosures_raw_table} d
        INNER JOIN eligible_lines el
          ON el.rcept_no = d.rcept_no
         AND el.corp_code = d.corp_code
        WHERE {' AND '.join(conditions)}
        ORDER BY name ASC
        LIMIT %s
        """
        query_params = list(src_params) + list(params) + [max(int(limit) * 4, int(limit), 1)]

    rows = _fetchall(sql, query_params, conn=conn)

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
