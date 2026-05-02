# Multi-stage Dockerfile for Atlas DCT Infrastructure Intelligence

# --- Stage 1: Builder ---
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Stage 2: Runtime ---
FROM python:3.11-slim

RUN groupadd -r -g 1000 atlas && useradd -r -u 1000 -g 1000 -d /app atlas

WORKDIR /app

COPY --from=builder /install /usr/local

# Copy application code
COPY *.py ./
COPY atlas_schema.sql ./
COPY netbox_dashboard.html ./

# Create upload directory
RUN mkdir -p /app/uploads && chown -R atlas:atlas /app

USER atlas

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5050/api/health')" || exit 1

# Production server: 1 worker + 4 threads.
# Single worker keeps USER_CONTEXT/USER_SITE in one process (no cross-worker
# state isolation). Threads share memory safely under _state_lock. Atlas is
# I/O bound (Postgres + LLM API), so threads give equivalent throughput.
# Timeout for large cutsheet uploads (19MB+ workbooks) plus combined
# preprocessor + optional in-service sort + Postgres ingest in one request.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5050", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "atlas_web_app:app"]
