# storage-rest-router

Local HTTP reverse proxy for [`ksync --storage-rest`](https://github.com/KYVENetwork/ksync) with per-request upstream failover.

ksync keeps running on transient bundle/storage errors. This router retries each HTTP request against an ordered list of storage-rest bases so the next fetch can succeed without restarting ksync.

Requires **Python 3.10+**. No third-party packages (see `requirements.txt`).

## Install

```bash
git clone https://github.com/Liver-23/storage-rest-router.git
cd storage-rest-router
```

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv venv
uv pip install -r requirements.txt
```

If `uv` is not installed:

```bash
snap install astral-uv
```

Without uv, Python 3.10+ is enough because there are no pip packages to install:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python3 -m pip install -r requirements.txt
```

## Use

Start the router **before** ksync. Keep `--listen` on loopback.

```bash
uv run python storage_rest_router.py \
  --listen 127.0.0.1:18080 \
  --backend https://ario.ionode.top \
  --backend https://backup.example
```

Or, with the venv activated:

```bash
python3 storage_rest_router.py \
  --listen 127.0.0.1:18080 \
  --backend https://ario.ionode.top \
  --backend https://backup.example
```

Backends can also be set with `STORAGE_REST_ROUTER_BACKENDS` (space-separated URLs) instead of repeating `--backend`.

Probe that the router is up:

```bash
curl -sSf http://127.0.0.1:18080/storage-rest-router-health
```

Point ksync at the router:

```bash
ksync serve-snapshots ... --storage-rest http://127.0.0.1:18080
```

By default HTTP 404 is retried on the next backend (mirrors may not host every bundle). Use `--no-retry-404` if a true 404 should not trigger failover.

Useful flags:

- `--timeout SEC` — per-upstream request timeout (default `120`)
- `--max-body-bytes N` — reject larger request bodies (default `8388608`)
- `--retry-status CODES` — statuses that fail over to the next backend
- `--verbose` — debug logs, including query strings

## Tests

```bash
uv run python -m unittest test_python.py -v
```

or:

```bash
python3 -m unittest test_python.py -v
```

## Security notes

Keep `--listen` on loopback (`127.0.0.1` or `::1`) unless you put another access control layer in front. This process has no auth of its own.

The proxy:

- stays on the configured backend hosts (rejects absolute / scheme-relative request targets)
- follows HTTP(S) 3xx `Location` redirects (needed for gateways such as turbo-gateway.com)
- talks HTTP/HTTPS only (no `file:` / `ftp:`)
- caps request bodies (`--max-body-bytes`, default 8 MiB)
- returns a generic 502 body (details stay in process logs)
- logs the path without query string at INFO (`--verbose` logs the query)
