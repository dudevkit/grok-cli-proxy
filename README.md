# Grok CLI Proxy

Thin proxy + account pool manager for xAI Grok CLI. Manages multiple CPA accounts, auto-refreshes tokens, and provides an OpenAI-compatible API surface.

## Quick start

```bash
docker build -t grok-cli-proxy .
docker run -d -p 8787:8787 -v ./data:/app/data -v ./config.json:/app/config.json grok-cli-proxy
```

Or without Docker (Windows):

```powershell
git clone https://github.com/raviakbar97/grok-cli-proxy.git
cd grok-cli-proxy
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json
# edit config.json → ganti admin_password dan api_key
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Or Linux/Mac:

```bash
git clone https://github.com/raviakbar97/grok-cli-proxy.git
cd grok-cli-proxy
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
# edit config.json → set admin_password and api_key
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Open `http://127.0.0.1:8787` — sign in with `admin_password`. Default password is `admin` — change it.

## Features

- **Import CPA accounts** — flat JSON, nested `{email, tokens:{...}}` harvest format, or folder scan. Concurrent via semaphore (default 30) with progress bar.
- **Auto-refresh** — background loop (default every 3600s) concurrent via semaphore (default 100). `invalid_grant` logs but does NOT mark dead — existing access_token may still work.
- **Warmup** — verifies account can reply via real streaming chat (`max_tokens=4`). Exhausted accounts only recover via successful warmup, not refresh.
- **Round-robin proxy** — `POST /v1/chat/completions`, `POST /v1/responses`, `GET /v1/models`. Retries all active accounts before failing. 403 → exhausted, proxy 401 → dead.
- **Token tracking** — 2M limit per account, accumulated from `usage.total_tokens`. Dashboard shows pool capacity and remaining.
- **Account health** — active / exhausted / dead / error. Sort: active first, then exhausted, dead, error.
- **Live WebSocket log** — real-time console of all proxy requests, refresh events, warmup. Method badges (colored), status codes (200=green, 403/401=red, 429=orange), duration, tokens, model.
- **Dashboard** — password-protected login (session cookie 7d), manage pool, API keys (RBAC), bulk actions (warmup, refresh, enable/disable, delete), action progress bar, drag-drop import, stats sidebar.
- **Server stats** — `GET /api/stats` returns account counts by status, tokens used, pool remaining, uptime, refresh interval.
- **429 backoff** — exponential + jitter on proxy, refresh, and warmup retries.

## Import format

Flat:
```json
{"email": "u@h.com", "access_token": "...", "refresh_token": "...", "id_token": "..."}
```

Or nested (harvest format):
```json
{"email": "u@h.com", "tokens": {"access_token": "...", "refresh_token": "..."}}
```

Arrays accepted for bulk. Drag-drop `.json` files or paste raw JSON.

## Proxy usage

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"hi"}]}'
```

Generate API keys in Dashboard → API Keys panel. Proxy auth uses API key; dashboard auth uses password.

## Docker

```bash
docker build -t grok-cli-proxy .
docker run -d \
  --name grok-cli-proxy \
  -p 8787:8787 \
  -v /path/to/data:/app/data \
  -v /path/to/config.json:/app/config.json \
  grok-cli-proxy
```

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `port` | `8787` | HTTP listen port |
| `db_path` | `data/accounts.db` | SQLite database path (relative to project root) |

| `admin_password` | `admin` | Dashboard login password |
| `refresh_interval_sec` | `300` | Token refresh interval in seconds |
| `refresh_concurrency` | `100` | Max concurrent refresh operations |
| `import_concurrency` | `30` | Max concurrent account imports |
| `token_url` | xAI OAuth | Token endpoint URL (proxy configurable) |
| `client_id` | xAI default | OAuth client ID |
| `upstream_base` | xAI API | Upstream proxy base URL |

## Design

- **Single-process** FastAPI on Python 3.11
- **SQLite** with WAL mode, single-writer lock
- **Shared httpx client** — connection pooling (max 50 keepalive, 100 total connections) injected into proxy, refresh, warmup
- **Refresh loop** first tick after 3s boot delay, then every `refresh_interval_sec`
- **Import** rejects duplicate emails (upsert returns None)
- **team_id** auto-extracted from `id_token` JWT (`sub` claim)
- **Round-robin** over `ORDER BY enabled DESC, CASE status WHEN 'active' THEN 0 WHEN 'exhausted' THEN 1 WHEN 'dead' THEN 2 WHEN 'error' THEN 3 ELSE 4 END ASC, priority ASC, email ASC`

## Endpoints

| Path | Auth | Description |
|------|------|-------------|
| `/` | session | Dashboard UI |
| `/api/auth/login` | none | Password login |
| `/api/auth/logout` | none | Session logout |
| `/api/auth/me` | none | Session check |
| `/api/config/public` | none | Public config (base URL, endpoints, curl examples) |
| `/api/stats` | admin | Server stats (accounts, tokens, uptime) |
| `/api/keys` | admin | API key management |
| `/api/accounts` | admin | Account CRUD, filter, bulk |
| `/api/accounts/{id}/warmup` | admin | Warmup single account |
| `/api/accounts/refresh` | admin | Refresh tokens (single/bulk) |
| `/api/accounts/import` | admin | Import accounts |
| `/api/models` | admin | Available models |
| `/ws/log` | admin | Live WebSocket activity log |
| `/v1/models` | api_key | List models (OpenAI-compatible) |
| `/v1/chat/completions` | api_key | Chat completions proxy |
| `/v1/responses` | api_key | Responses proxy |

## Thanks to

- **enowxlab** — member and admin

## Disclaimer

This project is for educational/experimental use. It interacts with xAI's Grok CLI API. Use at your own risk. Not affiliated with xAI.
