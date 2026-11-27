FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Single worker: the app keeps in-process caches (ticker/response caches) and
# sync de-dup locks (_syncing_tickers) in memory. Multiple workers would each
# hold their own copy, causing cache incoherence and duplicate background syncs.
# Slow SEC calls are already offloaded to threads/background tasks, so one
# async worker stays responsive. Scale out only after moving shared state to Redis.
# Bind to $PORT when the platform injects one (Render/Cloud Run/Railway all do);
# fall back to 8000 for local `docker run`. Shell form so ${PORT} is expanded.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
