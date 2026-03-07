from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class GraphQuery(BaseModel):
    start_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    snapshot_date: str | None = Field(default=None, description="YYYY-MM-DD or YYYYMMDD")
    search_stock: str | None = None
    highlight_hops: int = Field(default=1, ge=0, le=3)
    max_edges: int = Field(default=50, ge=1, le=50)
    db_limit: int | None = Field(default=None, ge=1, le=500000)
    history_limit: int = Field(default=500, ge=1, le=5000)
    include_periodic_status: bool = False
    include_majorstock_status: bool = False
    include_figure: bool = True
    include_history: bool = True
    include_top_edges: bool = True


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
    nodes: list[str] = Field(default_factory=list)
    edges: list[TopEdge] = Field(default_factory=list)
    top_edges: list[TopEdge]
    investing_history: list[dict[str, Any]] = Field(default_factory=list)


class StockOptionsResponse(BaseModel):
    stocks: list[str]
