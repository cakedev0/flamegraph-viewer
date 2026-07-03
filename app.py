import gzip
import json
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
_GZIP_MAGIC = b"\x1f\x8b"


def _validate_url(url: str) -> bool:
    """Accept only https:// URLs with a non-empty host."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in _ALLOWED_SCHEMES and bool(parsed.netloc)


def _download_bytes(url: str, *, description: str) -> tuple[bytes | None, Response | None]:
    try:
        resp = requests.get(url, timeout=60, stream=True)  # noqa: S113
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                return None, Response(
                    "Downloaded file exceeds the 100 MB limit",
                    status=413,
                    mimetype="text/plain",
                )
            chunks.append(chunk)
        return b"".join(chunks), None
    except requests.RequestException as exc:
        logger.warning("Download failed for %s: %s", url, exc)
        return None, Response(
            f"Failed to download the {description}",
            status=502,
            mimetype="text/plain",
        )


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

    compressed, error_response = _download_bytes(url, description="profile file")
    if error_response is not None:
        return error_response

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


@app.route("/json")
def json_viewer():
    url = request.args.get("url")
    if not url:
        return Response(
            "Missing required query parameter: url\n"
            "Usage: /json?url=https://example.com/result.json",
            status=400,
            mimetype="text/plain",
        )

    if not _validate_url(url):
        return Response(
            "Invalid url: only https:// URLs are accepted",
            status=400,
            mimetype="text/plain",
        )

    data, error_response = _download_bytes(url, description="JSON file")
    if error_response is not None:
        return error_response

    if data.startswith(_GZIP_MAGIC):
        try:
            data = gzip.decompress(data)
        except Exception as exc:
            logger.warning("JSON gzip decompression failed for %s: %s", url, exc)
            return Response(
                "Failed to decompress file: content is not valid gzip",
                status=422,
                mimetype="text/plain",
            )

    try:
        json.loads(data)
    except ValueError as exc:
        logger.warning("JSON parsing failed for %s: %s", url, exc)
        return Response(
            "Failed to parse file: content is not valid JSON",
            status=422,
            mimetype="text/plain",
        )

    return Response(
        data,
        mimetype="application/json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
