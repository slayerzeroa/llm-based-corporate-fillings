from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GraphQuery(BaseModel):
    start_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    snapshot_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    search_stock: str | None = None
    highlight_hops: int = Field(default=1, ge=0, le=3)
    max_edges: int = Field(default=80, ge=1, le=5000)
    db_limit: int | None = Field(default=None, ge=1, le=500000)
    include_periodic_status: bool = False
    include_majorstock_status: bool = False


class TopEdge(BaseModel):
    src: str
    dst: str
    weight: float


class GraphResponse(BaseModel):
    snapshot_dates: list[str]
    snapshot_date: str | None
    selected_stock: str | None
    rows: int
    edges_shown: int
    status_text: str
    figure: dict[str, Any]
    top_edges: list[TopEdge]


class StockOptionsResponse(BaseModel):
    stocks: list[str]

