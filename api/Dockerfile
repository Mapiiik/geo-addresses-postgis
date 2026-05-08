FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY api/ ./api/

# Shell form via `sh -c` so $API_PORT is expanded at container start;
# `exec` replaces the shell so SIGTERM propagates straight to uvicorn.
# No EXPOSE — the actual listening port is runtime-configurable via $API_PORT.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${API_PORT:-8000}"]
