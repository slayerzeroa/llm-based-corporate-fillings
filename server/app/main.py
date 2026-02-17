from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .config import get_settings
from .graph_service import build_graph_response, list_stock_options
from .schemas import GraphQuery, GraphResponse, StockOptionsResponse


settings = get_settings()

app = FastAPI(title="Stock Relationship API", version="1.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/graph/query", response_model=GraphResponse)
def query_graph(payload: GraphQuery) -> GraphResponse:
    try:
        return build_graph_response(payload, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}") from exc


@app.get("/api/stocks", response_model=StockOptionsResponse)
def stocks(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=5000),
    include_periodic_status: bool = Query(default=False),
    include_majorstock_status: bool = Query(default=False),
) -> StockOptionsResponse:
    try:
        items = list_stock_options(
            start_date=start_date,
            end_date=end_date,
            q=q,
            limit=limit,
            include_periodic_status=include_periodic_status,
            include_majorstock_status=include_majorstock_status,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}") from exc
    return StockOptionsResponse(stocks=items)
