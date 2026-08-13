# storage-rest-router

Local HTTP reverse proxy for [`ksync --storage-rest`](https://github.com/KYVENetwork/ksync) with per-request upstream failover.

ksync keeps running on transient bundle/storage errors. This router retries each HTTP request against an ordered list of storage-rest bases so the next fetch can succeed without restarting ksync.

Python 3.10+ standard library only. No extra packages.

## Run

```bash
python3 storage_rest_router.py \
  --listen 127.0.0.1:18080 \
  --backend https://ario.ionode.top \
  --backend https://backup.example
```

Then:

```bash
ksync serve-snapshots ... --storage-rest http://127.0.0.1:18080
```

Start the router **before** ksync. Probe:

```bash
curl -sSf http://127.0.0.1:18080/storage-rest-router-health
```

Backends can also be set with `STORAGE_REST_ROUTER_BACKENDS` (space-separated URLs).

By default HTTP 404 is retried on the next backend (mirrors may not host every bundle). Use `--no-retry-404` if a true 404 should not trigger failover.

## Security notes

Keep `--listen` on loopback (`127.0.0.1` or `::1`) unless you put another access control layer in front. This process has no auth of its own.

The proxy:

- stays on the configured backend hosts (rejects absolute / scheme-relative request targets)
- does not follow upstream `Location` redirects
- talks HTTP/HTTPS only (no `file:` / `ftp:`)
- caps request bodies (`--max-body-bytes`, default 8 MiB)
- returns a generic 502 body (details stay in process logs)
- logs the path without query string at INFO (`--verbose` logs the query)

## Tests

```bash
python3 -m unittest test_python.py -v
```
