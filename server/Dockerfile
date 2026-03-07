FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY app /app/app
COPY run.py /app/run.py

# ---- Build-time defaults (override with --build-arg if needed) ----
ARG DB_HOST=slayerzeroa.iptime.org
ARG DB_NAME=dart
ARG DB_USER=dart
ARG DB_PORT=3306

ARG TABLE_NAME=dart_edge_events
ARG DB_TABLE=dart_investment_events_copy
ARG DB_DISCLOSURES_TABLE=dart_disclosures_raw
ARG DB_LINES_TABLE=dart_investment_lines_raw
ARG DB_FAMILIES_TABLE=dart_disclosure_families
ARG DB_FAMILY_MEMBERS_TABLE=dart_disclosure_family_members
ARG DB_INVESTEE_DIM_TABLE=dart_investee_dim
ARG DB_EDGE_STATE_CURRENT_TABLE=dart_edge_state_current
ARG DB_EDGE_STATE_DAILY_TABLE=dart_edge_state_daily

# ---- Convert build args into runtime env vars ----
ENV DB_HOST=${DB_HOST} \
    DB_NAME=${DB_NAME} \
    DB_USER=${DB_USER} \
    DB_PORT=${DB_PORT} \
    TABLE_NAME=${TABLE_NAME} \
    DB_TABLE=${DB_TABLE} \
    DB_DISCLOSURES_TABLE=${DB_DISCLOSURES_TABLE} \
    DB_LINES_TABLE=${DB_LINES_TABLE} \
    DB_FAMILIES_TABLE=${DB_FAMILIES_TABLE} \
    DB_FAMILY_MEMBERS_TABLE=${DB_FAMILY_MEMBERS_TABLE} \
    DB_INVESTEE_DIM_TABLE=${DB_INVESTEE_DIM_TABLE} \
    DB_EDGE_STATE_CURRENT_TABLE=${DB_EDGE_STATE_CURRENT_TABLE} \
    DB_EDGE_STATE_DAILY_TABLE=${DB_EDGE_STATE_DAILY_TABLE} \
    APP_HOST=0.0.0.0 \
    APP_PORT=5623 \
    APP_RELOAD=false \
    APP_WORKERS=1

EXPOSE 5623

CMD ["python", "run.py"]
