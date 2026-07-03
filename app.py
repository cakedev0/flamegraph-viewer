import gzip
import logging
import os
import subprocess
from urllib.parse import urlparse

import requests
from flask import Flask, Response, request

app = Flask(__name__)
logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_ALLOWED_SCHEMES = {"https"}


def _validate_url(url: str) -> bool:
    """Accept only https:// URLs with a non-empty host."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in _ALLOWED_SCHEMES and bool(parsed.netloc)


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

    if not _validate_url(url):
        return Response(
            "Invalid url: only https:// URLs are accepted",
            status=400,
            mimetype="text/plain",
        )

    try:
        resp = requests.get(url, timeout=60, stream=True)  # noqa: S113
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
        logger.warning("Download failed for %s: %s", url, exc)
        return Response(
            "Failed to download the profile file",
            status=502,
            mimetype="text/plain",
        )

    try:
        raw_data = gzip.decompress(compressed)
    except Exception as exc:
        logger.warning("Decompression failed: %s", exc)
        return Response(
            "Failed to decompress file: content is not valid gzip",
            status=422,
            mimetype="text/plain",
        )

    try:
        result = subprocess.run(
            ["perl", "/usr/local/bin/flamegraph.pl"],
            input=raw_data,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        return Response(
            "flamegraph.pl or perl not found on PATH",
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
            f"flamegraph.pl failed (exit {result.returncode}):\n"
            + result.stderr.decode(errors="replace"),
            status=500,
            mimetype="text/plain",
        )

    return Response(result.stdout, mimetype="image/svg+xml")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
