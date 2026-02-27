from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "5623"))
    reload_enabled = os.getenv("APP_RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"}
    workers = int(os.getenv("APP_WORKERS", "1"))
    if reload_enabled:
        workers = 1

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        workers=max(workers, 1),
        timeout_keep_alive=30,
    )
