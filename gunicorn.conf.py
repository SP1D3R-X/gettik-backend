import os
import multiprocessing

# Gunicorn Production Configuration for Gettik FastAPI Backend
bind = f"{os.getenv('APP_HOST', '0.0.0.0')}:{os.getenv('APP_PORT', '8000')}"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"
keepalive = 65
timeout = 120
graceful_timeout = 30
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"
errorlog = "-"
preload_app = False
