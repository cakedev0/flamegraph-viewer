import gzip
import io
import json
import logging
import os
import pstats
import subprocess
import tempfile
from collections import Counter
from urllib.parse import urlparse

import flameprof
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


def _is_cprofile_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".prof.gz")


def _run_inferno_flamegraph(folded_stacks: bytes) -> tuple[bytes | None, Response | None]:
    try:
        result = subprocess.run(
            ["inferno-flamegraph"],
            input=folded_stacks,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        return None, Response(
            "inferno-flamegraph binary not found on PATH",
            status=500,
            mimetype="text/plain",
        )
    except subprocess.TimeoutExpired:
        return None, Response(
            "Flamegraph generation timed out",
            status=504,
            mimetype="text/plain",
        )

    if result.returncode != 0:
        return None, Response(
            f"inferno-flamegraph failed (exit {result.returncode}):\n"
            + result.stderr.decode(errors="replace"),
            status=500,
            mimetype="text/plain",
        )

    return result.stdout, None


def _cprofile_stats_to_folded_stacks(stats: dict) -> bytes:
    funcs, calls = flameprof.calc_callers(stats)
    blocks = []
    block_counts = Counter()

    def _counts(parent, visited):
        for child in funcs[parent]["calls"]:
            key = (parent, child)
            block_counts[key] += 1
            if block_counts[key] < 2 and key not in visited:
                _counts(child, visited | {key})

    maxw = funcs["root"]["stat"][3] * 1.0
    if maxw <= 0:
        return b""

    def _calc(parent, timings, origin, visited, trace=(), pccnt=1, pblock=None):
        childs = funcs[parent]["calls"]
        _, _, ptt, ptc = timings
        fchilds = sorted(
            (
                (func, funcs[func], calls[(parent, func)], max(block_counts[(parent, func)], pccnt))
                for func in childs
            ),
            key=lambda row: row[0],
        )

        gchilds = [row for row in fchilds if row[3] == 1]
        bchilds = [row for row in fchilds if row[3] > 1]
        if bchilds:
            gctc = sum(row[2][3] for row in gchilds)
            bctc = sum(row[2][3] for row in bchilds)
            rest = ptc - ptt - gctc
            factor = rest / bctc if bctc > 0 else 1
            bchilds = [
                (
                    func,
                    ffunc,
                    (round(cc * factor), round(nc * factor), tt * factor, tc * factor),
                    ccnt,
                )
                for func, ffunc, (cc, nc, tt, tc), ccnt in bchilds
            ]

        for child, _, (cc, nc, tt, tc), ccnt in gchilds + bchilds:
            if tc / maxw > flameprof.DEFAULT_THRESHOLD / 100:
                ckey = (parent, child)
                ctrace = trace + (child,)
                block = {"trace": ctrace, "ww": tt}
                blocks.append(block)
                if ckey not in visited:
                    _calc(child, (cc, nc, tt, tc), origin, visited | {ckey}, ctrace, ccnt, block)
            elif pblock:
                pblock["ww"] += tc

            origin += tc

    _counts("root", set())
    _calc("root", (1, 1, maxw, maxw), 0, set())

    folded = io.StringIO()
    flameprof.render_fg(blocks, flameprof.DEFAULT_LOG_MULT, folded)
    return folded.getvalue().encode()


def _render_cprofile_flamegraph(profile_data: bytes) -> tuple[bytes | None, Response | None]:
    profile_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".prof", delete=False) as profile_file:
            profile_path = profile_file.name
            profile_file.write(profile_data)

        try:
            stats = pstats.Stats(profile_path).stats
            folded = _cprofile_stats_to_folded_stacks(stats)
        except TimeoutError:
            return None, Response(
                "cProfile conversion timed out",
                status=504,
                mimetype="text/plain",
            )
        except Exception as exc:
            logger.warning("cProfile conversion failed for %s: %s", profile_path, exc)
            return None, Response(
                "Failed to convert cProfile stats",
                status=422,
                mimetype="text/plain",
            )

        return _run_inferno_flamegraph(folded)
    finally:
        if profile_path is not None:
            try:
                os.unlink(profile_path)
            except FileNotFoundError:
                pass


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

    if _is_cprofile_url(url):
        svg, error_response = _render_cprofile_flamegraph(raw_data)
    else:
        svg, error_response = _run_inferno_flamegraph(raw_data)

    if error_response is not None:
        return error_response

    return Response(svg, mimetype="image/svg+xml")


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
