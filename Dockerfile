# Design Ops — A1 Daily Ops Digest. Single always-on container (FastAPI + in-process
# scheduler). Pair with a managed Postgres; set secrets via env (never baked in).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CORPUS_STORE_DIR=/data/corpus

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run DB migrations, then serve. Railway/Render/Fly inject $PORT.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn designops.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
