# Server (FastAPI)

## 1) Install
```bash
pip install -r server/requirements.txt
```

## 2) Env
Root `.env` should include:
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- optional: `DB_TABLE` (default: `dart_investment_events`)
- optional: `CORS_ORIGINS` (comma-separated)

## 3) Run
```bash
python server/run.py
```

API base: `http://127.0.0.1:5623`

운영용 권장 env:
- `APP_RELOAD=false`
- `APP_WORKERS=1` (저메모리 환경 권장)
- `GRAPH_CACHE_TTL_SEC=120`
- `GRAPH_CACHE_MAX_ITEMS=128`
- `STOCK_CACHE_TTL_SEC=300`
- `STOCK_CACHE_MAX_ITEMS=256`

경량화 쿼리 옵션(`POST /api/graph/query`):
- `include_figure` (default: `true`)
- `include_history` (default: `true`)
- `include_top_edges` (default: `true`)

참고:
- `include_figure=false` 로 호출하면 서버에서 Plotly 3D figure 생성을 생략합니다.
- 이 경우 `nodes`, `edges` 필드는 반환되며, 클라이언트가 직접 그래프를 렌더링할 수 있습니다.
