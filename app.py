import gzip
import os
import subprocess

import requests
from flask import Flask, Response, request

app = Flask(__name__)

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


@app.route("/healthz")
def healthz():
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/")
def flamegraph():
    url = request.args.get("url")
    if not url:
        return Response(
            "Missing required query parameter: url\n"
            "Usage: /?url=https://example.com/profile.raw.gz",
            status=400,
            mimetype="text/plain",
        )

    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                return Response(
                    "Downloaded file exceeds the 100 MB limit",
                    status=413,
                    mimetype="text/plain",
                )
            chunks.append(chunk)
        compressed = b"".join(chunks)
    except requests.RequestException as exc:
        return Response(
            f"Failed to download file: {exc}",
            status=502,
            mimetype="text/plain",
        )

    try:
        raw_data = gzip.decompress(compressed)
    except Exception as exc:
        return Response(
            f"Failed to decompress file: {exc}",
            status=422,
            mimetype="text/plain",
        )

    try:
        result = subprocess.run(
            ["inferno-flamegraph"],
            input=raw_data,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        return Response(
            "inferno-flamegraph binary not found on PATH",
            status=500,
            mimetype="text/plain",
        )
    except subprocess.TimeoutExpired:
        return Response(
            "Flamegraph generation timed out",
            status=504,
            mimetype="text/plain",
        )

    if result.returncode != 0:
        return Response(
            f"inferno-flamegraph failed (exit {result.returncode}):\n"
            + result.stderr.decode(errors="replace"),
            status=500,
            mimetype="text/plain",
        )

    return Response(result.stdout, mimetype="image/svg+xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
