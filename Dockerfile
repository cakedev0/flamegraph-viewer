FROM python:3.12-slim

# Install Perl and flamegraph.pl (converts py-spy folded-stack traces to SVG)
RUN apt-get update \
    && apt-get install -y --no-install-recommends perl curl \
    && curl -fsSL \
       "https://raw.githubusercontent.com/brendangregg/FlameGraph/master/flamegraph.pl" \
       -o /usr/local/bin/flamegraph.pl \
    && chmod +x /usr/local/bin/flamegraph.pl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Cloud Run injects PORT; default to 8080
ENV PORT=8080
EXPOSE 8080

# 2 workers; generous timeout for slow downloads + rendering
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "180", "app:app"]
