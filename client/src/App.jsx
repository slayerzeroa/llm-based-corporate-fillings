import { useEffect, useMemo, useState } from "react";
import Plot from "react-plotly.js";
import { fetchStockOptions, postGraphQuery } from "./api";

function todayIso() {
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = `${d.getMonth() + 1}`.padStart(2, "0");
  const dd = `${d.getDate()}`.padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export default function App() {
  const SYSTEM_MAX_EDGES = 50;
  const [startDate, setStartDate] = useState("2015-01-01");
  const [endDate, setEndDate] = useState(todayIso());
  const [searchStock, setSearchStock] = useState("");
  const [highlightHops, setHighlightHops] = useState(1);
  const [maxEdges, setMaxEdges] = useState(SYSTEM_MAX_EDGES);
  const [dbLimit, setDbLimit] = useState("");
  const [snapshotDates, setSnapshotDates] = useState([]);
  const [snapshotIndex, setSnapshotIndex] = useState(0);
  const [figure, setFigure] = useState({ data: [], layout: {} });
  const [statusText, setStatusText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [stocks, setStocks] = useState([]);

  const snapshotDate = useMemo(() => {
    if (!snapshotDates.length) return null;
    const idx = Math.max(0, Math.min(snapshotIndex, snapshotDates.length - 1));
    return snapshotDates[idx];
  }, [snapshotDates, snapshotIndex]);

  const loadGraph = async (opts = {}) => {
    setLoading(true);
    setError("");
    try {
      const payload = {
        start_date: startDate,
        end_date: endDate,
        snapshot_date: opts.snapshotDate ?? snapshotDate,
        search_stock: searchStock || null,
        highlight_hops: highlightHops,
        max_edges: Math.max(1, Math.min(Number(maxEdges || SYSTEM_MAX_EDGES), SYSTEM_MAX_EDGES)),
        db_limit: dbLimit ? Number(dbLimit) : null
      };
      const data = await postGraphQuery(payload);
      setFigure(data.figure || { data: [], layout: {} });
      setStatusText(data.status_text || "");

      const dates = Array.isArray(data.snapshot_dates) ? data.snapshot_dates : [];
      setSnapshotDates(dates);
      if (dates.length) {
        const idx = dates.indexOf(data.snapshot_date);
        setSnapshotIndex(idx >= 0 ? idx : dates.length - 1);
      } else {
        setSnapshotIndex(0);
      }
    } catch (e) {
      setError(e.message || "Graph query failed.");
    } finally {
      setLoading(false);
    }
  };

  const loadStocks = async (query) => {
    try {
      const data = await fetchStockOptions({
        start_date: startDate,
        end_date: endDate,
        q: query || undefined,
        limit: 500
      });
      setStocks(data.stocks || []);
    } catch {
      setStocks([]);
    }
  };

  useEffect(() => {
    loadGraph({ snapshotDate: null });
    loadStocks("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      loadStocks(searchStock);
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchStock, startDate, endDate]);

  const onRefresh = async () => {
    await loadGraph();
  };

  const onRangeReload = async () => {
    await loadGraph({ snapshotDate: null });
    await loadStocks(searchStock);
  };

  const onSnapshotChange = (nextIndex) => {
    const idx = Number(nextIndex);
    setSnapshotIndex(idx);
  };

  const onSnapshotCommit = async () => {
    if (!snapshotDates.length) return;
    const idx = Math.max(0, Math.min(snapshotIndex, snapshotDates.length - 1));
    await loadGraph({ snapshotDate: snapshotDates[idx] });
  };

  return (
    <div className="page">
      <h2>Stock Relationship 3D Dashboard (React)</h2>

      <div className="controls">
        <div className="control-row">
          <label>
            Start Date
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label>
            End Date
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </label>
          <label>
            Max Edges
            <input
              type="number"
              min={1}
              max={SYSTEM_MAX_EDGES}
              value={maxEdges}
              onChange={(e) =>
                setMaxEdges(
                  Math.max(1, Math.min(Number(e.target.value || SYSTEM_MAX_EDGES), SYSTEM_MAX_EDGES))
                )
              }
            />
          </label>
          <label>
            DB Limit
            <input
              type="number"
              min={1}
              placeholder="optional"
              value={dbLimit}
              onChange={(e) => setDbLimit(e.target.value)}
            />
          </label>
          <button onClick={onRangeReload} disabled={loading}>
            Reload Range
          </button>
        </div>

        <div className="control-row">
          <label className="grow">
            Search Stock
            <input
              list="stock-options"
              value={searchStock}
              onChange={(e) => setSearchStock(e.target.value)}
              placeholder="회사명을 입력하세요"
            />
            <datalist id="stock-options">
              {stocks.map((s) => (
                <option key={s} value={s} />
              ))}
            </datalist>
          </label>
          <label>
            Highlight Hops: {highlightHops}
            <input
              type="range"
              min={0}
              max={3}
              step={1}
              value={highlightHops}
              onChange={(e) => setHighlightHops(Number(e.target.value))}
            />
          </label>
          <button onClick={onRefresh} disabled={loading}>
            Apply Search
          </button>
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
              onChange={(e) => onSnapshotChange(e.target.value)}
              onMouseUp={onSnapshotCommit}
              onTouchEnd={onSnapshotCommit}
              onKeyUp={onSnapshotCommit}
              disabled={!snapshotDates.length || loading}
            />
            <div className="snapshot-label">
              {snapshotDate ? `As-Of Snapshot Date: ${snapshotDate}` : "No snapshot"}
            </div>
          </label>
        </div>
      </div>

      <div className="status">
        {loading ? "Loading..." : statusText}
        {error ? <div className="error">{error}</div> : null}
      </div>

      <Plot
        data={figure.data || []}
        layout={{
          ...(figure.layout || {}),
          autosize: true
        }}
        config={{ responsive: true, displaylogo: false }}
        useResizeHandler
        style={{ width: "100%", height: "880px" }}
      />
    </div>
  );
}
