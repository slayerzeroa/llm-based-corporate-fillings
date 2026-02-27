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
- `APP_WORKERS=2`
