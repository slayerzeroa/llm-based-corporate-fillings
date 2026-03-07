function compactParams(input) {
  const out = {};
  Object.entries(input || {}).forEach(([key, value]) => {
    if (value === undefined || value === null) return;
    if (typeof value === "string" && value.trim() === "") return;
    out[key] = value;
  });
  return out;
}

function toQueryString(params) {
  const usp = new URLSearchParams();
  Object.entries(compactParams(params)).forEach(([key, value]) => {
    usp.append(key, `${value}`);
  });
  return usp.toString();
}

async function requestJson(url, options = {}) {
  const res = await fetch(url, options);
  const raw = await res.text();
  let data = null;

  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const detail =
      (data && typeof data === "object" && (data.detail || data.message)) ||
      raw ||
      `HTTP ${res.status}`;
    throw new Error(String(detail));
  }

  return data ?? {};
}

export async function postGraphQuery(payload, options = {}) {
  return requestJson("/api/graph/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(compactParams(payload)),
    signal: options.signal
  });
}

export async function fetchStockOptions(params, options = {}) {
  const query = toQueryString(params);
  const path = query ? `/api/stocks?${query}` : "/api/stocks";
  return requestJson(path, { signal: options.signal });
}
