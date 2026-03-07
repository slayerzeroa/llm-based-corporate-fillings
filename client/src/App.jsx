import { useEffect, useMemo, useRef, useState } from "react";
import Plot from "react-plotly.js";
import { fetchStockOptions, postGraphQuery } from "./api";

const SYSTEM_MAX_EDGES = 50;
const DEFAULT_INITIAL_STOCK = "삼성전자";
const DEFAULT_HISTORY_LIMIT = 500;

function todayIso() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = `${d.getMonth() + 1}`.padStart(2, "0");
  const dd = `${d.getDate()}`.padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function clampInt(value, min, max, fallback) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(Math.trunc(n), max));
}

function extractHistoryRows(payload) {
  if (!payload || typeof payload !== "object") return [];
  const keys = ["investing_history", "investingHistory", "history", "table", "rows"];
  for (const key of keys) {
    const rows = payload[key];
    if (!Array.isArray(rows)) continue;
    return rows
      .map((row, idx) => {
        if (row && typeof row === "object" && !Array.isArray(row)) return row;
        return { index: idx + 1, value: row };
      })
      .filter(Boolean);
  }
  return [];
}

function normalizeGraphPayload(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const snapshotDates = Array.isArray(payload.snapshot_dates)
    ? payload.snapshot_dates.map((v) => String(v || "")).filter(Boolean)
    : [];
  const topEdgesRaw = Array.isArray(payload.top_edges) ? payload.top_edges : [];
  const topEdges = topEdgesRaw.map((row) => ({
    src: String(row?.src || ""),
    dst: String(row?.dst || ""),
    weight: Number.isFinite(Number(row?.weight)) ? Number(row.weight) : 0
  }));

  return {
    figure: payload.figure && typeof payload.figure === "object" ? payload.figure : { data: [], layout: {} },
    statusText: String(payload.status_text || ""),
    snapshotDates,
    snapshotDate: payload.snapshot_date ? String(payload.snapshot_date) : null,
    selectedStock: payload.selected_stock ? String(payload.selected_stock) : null,
    rows: Number.isFinite(Number(payload.rows)) ? Number(payload.rows) : 0,
    edgesShown: Number.isFinite(Number(payload.edges_shown)) ? Number(payload.edges_shown) : 0,
    topEdges,
    historyRows: extractHistoryRows(payload)
  };
}

function formatCellValue(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString() : "-";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export default function App() {
  const [filters, setFilters] = useState({
    startDate: "2015-01-01",
    endDate: todayIso(),
    searchStock: DEFAULT_INITIAL_STOCK,
    highlightHops: 1,
    maxEdges: SYSTEM_MAX_EDGES,
    dbLimit: "",
    historyLimit: DEFAULT_HISTORY_LIMIT,
    includePeriodicStatus: false,
    includeMajorstockStatus: false
  });

  const [snapshotDates, setSnapshotDates] = useState([]);
  const [snapshotIndex, setSnapshotIndex] = useState(0);
  const [figure, setFigure] = useState({ data: [], layout: {} });
  const [statusText, setStatusText] = useState("");
  const [selectedStock, setSelectedStock] = useState(null);
  const [rowsCount, setRowsCount] = useState(0);
  const [edgesShown, setEdgesShown] = useState(0);
  const [topEdges, setTopEdges] = useState([]);
  const [historyRows, setHistoryRows] = useState([]);
  const [stocks, setStocks] = useState([]);
  const [loadingGraph, setLoadingGraph] = useState(false);
  const [loadingStocks, setLoadingStocks] = useState(false);
  const [error, setError] = useState("");

  const graphSeqRef = useRef(0);

  const historyColumns = useMemo(() => {
    if (!historyRows.length) return [];
    const keys = new Set();
    historyRows.forEach((row) => {
      if (!row || typeof row !== "object") return;
      Object.keys(row).forEach((k) => keys.add(k));
    });
    return Array.from(keys);
  }, [historyRows]);

  const activeSnapshotDate = useMemo(() => {
    if (!snapshotDates.length) return null;
    const idx = Math.max(0, Math.min(snapshotIndex, snapshotDates.length - 1));
    return snapshotDates[idx];
  }, [snapshotDates, snapshotIndex]);

  async function loadGraph({
    snapshotDateOverride = undefined,
    includeFigure = true,
    includeHistory = true,
    includeTopEdges = true
  } = {}) {
    const reqId = graphSeqRef.current + 1;
    graphSeqRef.current = reqId;
    setLoadingGraph(true);
    setError("");

    const resolvedSnapshotDate =
      snapshotDateOverride === undefined ? activeSnapshotDate : snapshotDateOverride;

    const payload = {
      start_date: filters.startDate || null,
      end_date: filters.endDate || null,
      snapshot_date: resolvedSnapshotDate || null,
      search_stock: (filters.searchStock || "").trim() || null,
      highlight_hops: clampInt(filters.highlightHops, 0, 3, 1),
      max_edges: clampInt(filters.maxEdges, 1, SYSTEM_MAX_EDGES, SYSTEM_MAX_EDGES),
      db_limit: filters.dbLimit ? clampInt(filters.dbLimit, 1, 500000, 10000) : null,
      history_limit: clampInt(filters.historyLimit, 1, 5000, DEFAULT_HISTORY_LIMIT),
      include_periodic_status: Boolean(filters.includePeriodicStatus),
      include_majorstock_status: Boolean(filters.includeMajorstockStatus),
      include_figure: Boolean(includeFigure),
      include_history: Boolean(includeHistory),
      include_top_edges: Boolean(includeTopEdges)
    };

    try {
      const raw = await postGraphQuery(payload);
      if (reqId !== graphSeqRef.current) return;
      const data = normalizeGraphPayload(raw);

      setFigure(data.figure);
      setStatusText(data.statusText);
      setSelectedStock(data.selectedStock);
      setRowsCount(data.rows);
      setEdgesShown(data.edgesShown);
      setTopEdges(data.topEdges);
      setHistoryRows(data.historyRows);

      setSnapshotDates(data.snapshotDates);
      if (!data.snapshotDates.length) {
        setSnapshotIndex(0);
      } else {
        const idx = data.snapshotDate ? data.snapshotDates.indexOf(data.snapshotDate) : -1;
        setSnapshotIndex(idx >= 0 ? idx : data.snapshotDates.length - 1);
      }
    } catch (e) {
      if (reqId !== graphSeqRef.current) return;
      setError(e.message || "Graph query failed.");
    } finally {
      if (reqId === graphSeqRef.current) {
        setLoadingGraph(false);
      }
    }
  }

  useEffect(() => {
    // First paint: server-light query (skip figure/history generation on backend).
    void loadGraph({
      snapshotDateOverride: null,
      includeFigure: false,
      includeHistory: false,
      includeTopEdges: true
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    const timer = setTimeout(async () => {
      setLoadingStocks(true);
      try {
        const data = await fetchStockOptions(
          {
            start_date: filters.startDate || undefined,
            end_date: filters.endDate || undefined,
            q: (filters.searchStock || "").trim() || undefined,
            limit: 500,
            include_periodic_status: Boolean(filters.includePeriodicStatus),
            include_majorstock_status: Boolean(filters.includeMajorstockStatus)
          },
          { signal: ctrl.signal }
        );
        if (!ctrl.signal.aborted) {
          setStocks(Array.isArray(data?.stocks) ? data.stocks : []);
        }
      } catch {
        if (!ctrl.signal.aborted) setStocks([]);
      } finally {
        if (!ctrl.signal.aborted) setLoadingStocks(false);
      }
    }, 250);

    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [
    filters.searchStock,
    filters.startDate,
    filters.endDate,
    filters.includePeriodicStatus,
    filters.includeMajorstockStatus
  ]);

  function setFilter(key, value) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  async function onApplySearch() {
    await loadGraph({ snapshotDateOverride: null });
  }

  async function onReloadRange() {
    await loadGraph({ snapshotDateOverride: null });
  }

  async function onSnapshotCommit() {
    if (!snapshotDates.length) return;
    const idx = Math.max(0, Math.min(snapshotIndex, snapshotDates.length - 1));
    await loadGraph({ snapshotDateOverride: snapshotDates[idx] });
  }

  return (
    <div className="page">
      <h2>Stock Relationship Dashboard</h2>

      <div className="controls">
        <div className="control-row">
          <label>
            Start Date
            <input type="date" value={filters.startDate} onChange={(e) => setFilter("startDate", e.target.value)} />
          </label>
          <label>
            End Date
            <input type="date" value={filters.endDate} onChange={(e) => setFilter("endDate", e.target.value)} />
          </label>
          <label>
            Max Edges
            <input
              type="number"
              min={1}
              max={SYSTEM_MAX_EDGES}
              value={filters.maxEdges}
              onChange={(e) => setFilter("maxEdges", clampInt(e.target.value, 1, SYSTEM_MAX_EDGES, SYSTEM_MAX_EDGES))}
            />
          </label>
          <label>
            DB Limit
            <input type="number" min={1} value={filters.dbLimit} onChange={(e) => setFilter("dbLimit", e.target.value)} />
          </label>
          <label>
            History Limit
            <input
              type="number"
              min={1}
              max={5000}
              value={filters.historyLimit}
              onChange={(e) => setFilter("historyLimit", clampInt(e.target.value, 1, 5000, DEFAULT_HISTORY_LIMIT))}
            />
          </label>
          <button onClick={onReloadRange} disabled={loadingGraph}>Reload Range</button>
        </div>

        <div className="control-row">
          <label className="grow">
            Search Stock
            <input
              list="stock-options"
              value={filters.searchStock}
              onChange={(e) => setFilter("searchStock", e.target.value)}
              placeholder="회사명 또는 8자리 corp_code"
            />
            <datalist id="stock-options">
              {stocks.map((name) => <option key={name} value={name} />)}
            </datalist>
          </label>
          <label>
            Highlight Hops: {filters.highlightHops}
            <input
              type="range"
              min={0}
              max={3}
              step={1}
              value={filters.highlightHops}
              onChange={(e) => setFilter("highlightHops", clampInt(e.target.value, 0, 3, 1))}
            />
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={filters.includePeriodicStatus}
              onChange={(e) => setFilter("includePeriodicStatus", e.target.checked)}
            />
            Include Periodic
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={filters.includeMajorstockStatus}
              onChange={(e) => setFilter("includeMajorstockStatus", e.target.checked)}
            />
            Include Majorstock
          </label>
          <button onClick={onApplySearch} disabled={loadingGraph}>Apply Search</button>
        </div>

        <div className="control-row">
          <label className="grow">
            Snapshot Navigator
            <input
              type="range"
              min={0}
              max={Math.max(snapshotDates.length - 1, 0)}
              step={1}
              value={Math.min(snapshotIndex, Math.max(snapshotDates.length - 1, 0))}
              onChange={(e) => setSnapshotIndex(Number(e.target.value))}
              onMouseUp={onSnapshotCommit}
              onTouchEnd={onSnapshotCommit}
              onKeyUp={onSnapshotCommit}
              disabled={!snapshotDates.length || loadingGraph}
            />
            <div className="snapshot-label">
              {activeSnapshotDate ? `As-Of Snapshot Date: ${activeSnapshotDate}` : "No snapshot"}
            </div>
          </label>
        </div>
      </div>

      <div className="status">
        <span>{loadingGraph ? "Loading graph..." : statusText}</span>
        <span>{loadingStocks ? "Updating stock options..." : ""}</span>
        {error ? <div className="error">{error}</div> : null}
      </div>

      <div className="meta-row">
        <div>Selected: {selectedStock || "-"}</div>
        <div>Rows: {rowsCount.toLocaleString()}</div>
        <div>Edges: {edgesShown.toLocaleString()}</div>
      </div>

      <div className="history-table-wrap">
        {historyRows.length > 0 ? (
          <table className="history-table">
            <thead>
              <tr>{historyColumns.map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {historyRows.map((row, rowIndex) => (
                <tr key={`history-row-${rowIndex}`}>
                  {historyColumns.map((column) => (
                    <td key={`${rowIndex}-${column}`}>{formatCellValue(row?.[column])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="history-empty">No investing history rows returned for this query.</div>
        )}
      </div>

      <div className="top-edges">
        <h3>Top Edges</h3>
        {topEdges.length ? (
          <table className="history-table">
            <thead>
              <tr>
                <th>src</th>
                <th>dst</th>
                <th>weight</th>
              </tr>
            </thead>
            <tbody>
              {topEdges.map((row, idx) => (
                <tr key={`top-edge-${idx}`}>
                  <td>{row.src || "-"}</td>
                  <td>{row.dst || "-"}</td>
                  <td>{formatCellValue(row.weight)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="history-empty">No top edge rows.</div>
        )}
      </div>

      <Plot
        data={figure.data || []}
        layout={{ ...(figure.layout || {}), autosize: true }}
        config={{ responsive: true, displaylogo: false }}
        useResizeHandler
        style={{ width: "100%", height: "880px" }}
      />
    </div>
  );
}
