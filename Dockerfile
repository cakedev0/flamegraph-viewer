# ── Stage 1: build inferno-flamegraph from source ────────────────────────────
FROM rust:slim AS builder

RUN cargo install inferno

# ── Stage 2: Python runtime ───────────────────────────────────────────────────
FROM python:3.12-slim

# Copy the inferno-flamegraph binary produced in stage 1
COPY --from=builder /usr/local/cargo/bin/inferno-flamegraph /usr/local/bin/inferno-flamegraph

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Cloud Run injects PORT; default to 8080
ENV PORT=8080
EXPOSE 8080

# 2 workers; generous timeout for slow downloads + rendering
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "180", "app:app"]
