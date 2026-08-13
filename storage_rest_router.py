#!/usr/bin/env python3
"""Local HTTP reverse proxy for ksync --storage-rest with upstream failover.

ksync keeps running on transient bundle/storage errors; this router retries each
HTTP request against an ordered list of storage-rest bases so the next fetch can
succeed without restarting ksync.

Example:
  python3 storage_rest_router.py \\
    --listen 127.0.0.1:18080 \\
    --backend https://ario.ionode.top \\
    --backend https://backup.example

  ksync serve-snapshots ... --storage-rest http://127.0.0.1:18080

Start this process before ksync. If ksync logs "connection refused" to the
router address, nothing is listening yet (or wrong host/port); the router will
not log HTTP requests because no TCP connection was accepted.

By default HTTP 404 is retried on the next backend (mirrors may not host every
bundle). Use --no-retry-404 if a true 404 should not trigger failover.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable, Mapping
from urllib.parse import urlsplit

# Local-only path: returns 200 without contacting backends (for readiness checks).
_HEALTH_PATH = "/storage-rest-router-health"

# RFC 7230 § 6.1 — hop-by-hop headers we must not forward blindly.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
    }
)

# Client closed the connection while we were writing the response (common when
# the HTTP client cancels or times out). Must not be treated as upstream failure.
_CLIENT_DISCONNECT_EXC = (BrokenPipeError, ConnectionResetError)

_ALLOWED_BACKEND_SCHEMES = frozenset({"http", "https"})
_DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
_GENERIC_BAD_GATEWAY = b"All storage-rest backends failed\n"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return 3xx to the client instead of following Location (SSRF via redirect)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _build_http_opener() -> urllib.request.OpenerDirector:
    """HTTP/HTTPS only: no file/ftp/data handlers, no automatic redirects."""
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.UnknownHandler())
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(_NoRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


_OPENER = _build_http_opener()


def _normalize_backend_base(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        raise ValueError("empty backend URL")
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_BACKEND_SCHEMES or not parts.netloc:
        raise ValueError(f"backend must be http(s)://host[:port]: {url!r}")
    return url


def join_backend_url(backend_base: str, path: str, query: str) -> str:
    """Build upstream URL on the configured backend host only.

    Rejects absolute URLs, scheme-relative //host paths, and backslashes so a
    client cannot steer the proxy at an arbitrary origin (open-proxy / SSRF).
    """
    base = _normalize_backend_base(backend_base)
    if "\\" in path or "\x00" in path:
        raise ValueError("request path contains illegal characters")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("request path must be origin-form (start with a single /)")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        raise ValueError("request path must not include a scheme or host")
    safe_path = parsed.path
    if not safe_path.startswith("/") or safe_path.startswith("//"):
        raise ValueError("request path must be origin-form (start with a single /)")
    if query:
        return f"{base}{safe_path}?{query}"
    return f"{base}{safe_path}"


def _http_base_for_listen(addr: str, port: int) -> str:
    """Build http://host:port base for curl hints (IPv6 brackets)."""
    if ":" in addr and not addr.startswith("["):
        return f"http://[{addr}]:{port}"
    return f"http://{addr}:{port}"


def _is_loopback_host(host: str) -> bool:
    if host.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def filter_forward_headers(
    headers: Mapping[str, str],
    *,
    strip_host: bool = True,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_k, v in headers.items():
        k = raw_k.lower()
        if k in _HOP_BY_HOP:
            continue
        if strip_host and k == "host":
            continue
        out[raw_k] = v
    return out


class StorageRestRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    backends: list[str] = []
    upstream_timeout: float = 120.0
    retry_statuses: frozenset[int] = frozenset()
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes | None:
        if self.command in ("GET", "HEAD", "DELETE", "OPTIONS"):
            return b""
        length_hdr = self.headers.get("Content-Length")
        if not length_hdr:
            return b""
        try:
            n = int(length_hdr)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if n < 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if n > self.max_body_bytes:
            self.send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "Request body too large",
            )
            return None
        return self.rfile.read(n)

    def _should_retry_status(self, status: int) -> bool:
        return status in self.retry_statuses

    def _proxy_request(self) -> None:
        body = self._read_body()
        if body is None:
            return

        req_display = self.path
        if "?" in req_display:
            path_only, query = req_display.split("?", 1)
        else:
            path_only = req_display
            query = ""

        logging.info(
            "request %s %s from %s",
            self.command,
            path_only,
            self.client_address[0],
        )
        if query:
            logging.debug("query %s %s", self.command, query)

        if path_only == _HEALTH_PATH:
            payload = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        try:
            join_backend_url(self.backends[0], path_only, query)
        except (ValueError, IndexError):
            logging.warning("rejected request path %r", path_only)
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid request path")
            return

        path = path_only
        fwd_headers = filter_forward_headers(self.headers)

        last_exc: BaseException | None = None
        n_backends = len(self.backends)

        for idx, backend in enumerate(self.backends):
            try:
                target = join_backend_url(backend, path, query)
            except ValueError as e:
                last_exc = e
                logging.warning("skipping invalid backend %s: %s", backend, e)
                continue
            logging.debug(
                "upstream try [%s/%s] %s %s",
                idx + 1,
                n_backends,
                self.command,
                target,
            )
            req = urllib.request.Request(
                target,
                data=body if body != b"" else None,
                headers=fwd_headers,
                method=self.command,
            )

            try:
                with _OPENER.open(req, timeout=self.upstream_timeout) as resp:
                    status = resp.status
                    logging.info(
                        "served %s %s via [%s/%s] %s (HTTP %s)",
                        self.command,
                        path_only,
                        idx + 1,
                        n_backends,
                        backend,
                        status,
                    )
                    try:
                        self.send_response(status)
                        for hk, hv in resp.headers.items():
                            hl = hk.lower()
                            if hl in _HOP_BY_HOP:
                                continue
                            self.send_header(hk, hv)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        if self.command != "HEAD":
                            while True:
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                    except _CLIENT_DISCONNECT_EXC:
                        logging.debug(
                            "client disconnected during response for %s %s "
                            "(already committed upstream from [%s/%s])",
                            self.command,
                            path_only,
                            idx + 1,
                            n_backends,
                        )
                    except (OSError, urllib.error.URLError) as stream_exc:
                        logging.warning(
                            "upstream stream failed after headers sent for %s %s: %s",
                            self.command,
                            path_only,
                            stream_exc,
                        )
                    return

            except urllib.error.HTTPError as e:
                status = e.code
                if self._should_retry_status(status) and idx != n_backends - 1:
                    try:
                        e.read()
                    except Exception:
                        pass
                    nxt = self.backends[idx + 1]
                    logging.info(
                        "failover: HTTP %s from [%s/%s] %s -> next [%s/%s] %s "
                        "for %s %s",
                        status,
                        idx + 1,
                        n_backends,
                        backend,
                        idx + 2,
                        n_backends,
                        nxt,
                        self.command,
                        path_only,
                    )
                    continue
                # Last upstream, or status not in --retry-status: stream error.
                if not self._should_retry_status(status) and idx != n_backends - 1:
                    logging.warning(
                        "HTTP %s from [%s/%s] %s is not a retry status; "
                        "returning to client without trying %s other backend(s) "
                        "(%s %s)",
                        status,
                        idx + 1,
                        n_backends,
                        backend,
                        n_backends - idx - 1,
                        self.command,
                        path_only,
                    )
                else:
                    logging.warning(
                        "response %s %s from last upstream [%s/%s] %s "
                        "(HTTP %s, to client)",
                        self.command,
                        path_only,
                        idx + 1,
                        n_backends,
                        backend,
                        status,
                    )
                self.send_response(status)
                for hk, hv in e.headers.items():
                    hl = hk.lower()
                    if hl in _HOP_BY_HOP:
                        continue
                    self.send_header(hk, hv)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    err_body = e.read()
                    if err_body:
                        try:
                            self.wfile.write(err_body)
                        except _CLIENT_DISCONNECT_EXC:
                            logging.debug(
                                "client disconnected during error body for %s %s",
                                self.command,
                                path_only,
                            )
                return

            except (
                urllib.error.URLError,
                TimeoutError,
                socket.timeout,
                ConnectionResetError,
                BrokenPipeError,
                OSError,
            ) as e:
                last_exc = e
                if idx != n_backends - 1:
                    nxt = self.backends[idx + 1]
                    logging.info(
                        "failover: error from [%s/%s] %s -> next [%s/%s] %s "
                        "for %s %s: %s",
                        idx + 1,
                        n_backends,
                        backend,
                        idx + 2,
                        n_backends,
                        nxt,
                        self.command,
                        path_only,
                        e,
                    )
                    continue

        logging.error(
            "all %s upstream(s) failed for %s %s; last error: %s",
            n_backends,
            self.command,
            path_only,
            last_exc,
        )
        try:
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(_GENERIC_BAD_GATEWAY)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(_GENERIC_BAD_GATEWAY)
        except _CLIENT_DISCONNECT_EXC:
            logging.debug(
                "client disconnected before gateway error body for %s %s",
                self.command,
                path_only,
            )

    def do_GET(self) -> None:
        self._proxy_request()

    def do_HEAD(self) -> None:
        self._proxy_request()

    def do_PUT(self) -> None:
        self._proxy_request()

    def do_POST(self) -> None:
        self._proxy_request()

    def do_DELETE(self) -> None:
        self._proxy_request()

    def do_PATCH(self) -> None:
        self._proxy_request()

    def do_OPTIONS(self) -> None:
        self._proxy_request()


def _parse_retry_statuses(spec: str) -> frozenset[int]:
    out: set[int] = set()
    for part in spec.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        out.add(int(part, 10))
    return frozenset(out)


def _backends_from_env() -> list[str] | None:
    raw = os.environ.get("STORAGE_REST_ROUTER_BACKENDS", "").strip()
    if not raw:
        return None
    return [_normalize_backend_base(u) for u in raw.split()]


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "HTTP reverse proxy for ksync --storage-rest with per-request "
            "upstream failover."
        ),
        epilog=(
            "Start the router before ksync. Verify with: curl -sSf "
            f"http://127.0.0.1:PORT{_HEALTH_PATH} "
            "(adjust host/port to match --listen)."
        ),
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:18080",
        metavar="HOST:PORT",
        help="address to bind (default: %(default)s)",
    )
    p.add_argument(
        "--backend",
        action="append",
        dest="backends",
        metavar="URL",
        help=(
            "storage-rest base URL (scheme://host[:port]); repeat in failover "
            "order. If omitted, use STORAGE_REST_ROUTER_BACKENDS "
            "(space-separated)."
        ),
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        metavar="SEC",
        help="per-upstream request timeout (default: %(default)s)",
    )
    p.add_argument(
        "--max-body-bytes",
        type=int,
        default=_DEFAULT_MAX_BODY_BYTES,
        metavar="N",
        help=(
            "reject request bodies larger than N bytes "
            "(default: %(default)s)"
        ),
    )
    p.add_argument(
        "--retry-status",
        default="404,408,429,500,502,503,504,522,523,524",
        metavar="CODES",
        help=(
            "comma/space-separated HTTP statuses that trigger try-next-backend "
            "(default includes 404 for mirror gaps; %(default)s)"
        ),
    )
    p.add_argument(
        "--no-retry-404",
        action="store_true",
        help=(
            "do not failover on HTTP 404 (return first upstream's 404 even if "
            "other backends are listed)"
        ),
    )
    p.add_argument(
        "--retry-404",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="debug logging (includes query strings)",
    )
    return p.parse_args(list(argv))


def _parse_listen(spec: str) -> tuple[str, int]:
    s = spec.strip()
    if s.startswith("[") and "]:" in s:
        inner, _, port_s = s.rpartition("]:")
        host = inner[1:]
        port = int(port_s, 10)
        return host, port
    if ":" not in s:
        raise ValueError("listen address must be HOST:PORT or [::1]:PORT")
    host, port_s = s.rsplit(":", 1)
    if not port_s.isdigit():
        raise ValueError(f"invalid listen port in {spec!r}")
    return host, int(port_s, 10)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.max_body_bytes < 0:
        logging.error("--max-body-bytes must be >= 0")
        return 1

    backends = args.backends
    if not backends:
        backends = _backends_from_env()
    if not backends:
        logging.error(
            "no backends: pass --backend URL one or more times or set "
            "STORAGE_REST_ROUTER_BACKENDS",
        )
        return 1

    try:
        backends = [_normalize_backend_base(u) for u in backends]
    except ValueError as e:
        logging.error("bad --backend: %s", e)
        return 1

    retry_statuses = set(_parse_retry_statuses(args.retry_status))
    if args.no_retry_404:
        retry_statuses.discard(404)
    if args.retry_404:
        logging.warning(
            "--retry-404 is obsolete (404 is in default retry set); "
            "use --no-retry-404 to disable failover on 404",
        )
    retry_frozen = frozenset(retry_statuses)

    try:
        host, port = _parse_listen(args.listen)
    except ValueError as e:
        logging.error("bad --listen %r: %s", args.listen, e)
        return 1

    StorageRestRouterHandler.backends = backends
    StorageRestRouterHandler.upstream_timeout = args.timeout
    StorageRestRouterHandler.retry_statuses = retry_frozen
    StorageRestRouterHandler.max_body_bytes = args.max_body_bytes

    httpd = ThreadingHTTPServer((host, port), StorageRestRouterHandler)
    bound = httpd.socket.getsockname()
    bound_host = bound[0]
    bound_port = int(bound[1])
    base = _http_base_for_listen(bound_host, bound_port)
    logging.info(
        "storage-rest router listening %s -> backends %s",
        base,
        backends,
    )
    if not _is_loopback_host(host):
        logging.warning(
            "listen address %s is not loopback; anyone who can reach this "
            "port can use the proxy. Prefer 127.0.0.1 or ::1",
            host,
        )
    logging.info(
        "ready (start ksync after this); probe: curl -sSf %s%s",
        base,
        _HEALTH_PATH,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
