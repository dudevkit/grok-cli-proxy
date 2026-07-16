from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .broadcaster import LogBroadcaster
from .client import HttpClient, backoff_sleep
from .db import Database
from .proxy import UpstreamProxy
from .refresh import RefreshService

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"
STATIC_DIR = ROOT / "static"
SESSION_COOKIE = "gcp_session"
SESSION_TTL_SEC = 7 * 24 * 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("grok-cli-proxy")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dirty = False
    if cfg.get("api_key") in (None, "", "change-me"):
        cfg["api_key"] = secrets.token_hex(16)
        dirty = True
        log.warning("generated api_key saved to config.json")
    if cfg.get("admin_password") in (None, "", "change-me"):
        cfg["admin_password"] = "admin"
        dirty = True
        log.warning("admin_password defaulted to 'admin' — change it in config.json")
    if dirty:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    # resolve relative db path
    db_path = Path(cfg.get("db_path") or "data/accounts.db")
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    cfg["db_path"] = str(db_path)
    return cfg


config = load_config()
db = Database(config["db_path"])
http_client = HttpClient(config)
broadcaster = LogBroadcaster()
refresh_svc = RefreshService(db, config, http_client, broadcaster)
proxy = UpstreamProxy(db, config, http_client, broadcaster)
server_start = time.time()

app = FastAPI(title="Grok CLI Proxy", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _session_secret() -> bytes:
    raw = f"{config.get('admin_password','')}|{config.get('api_key','')}|gcp-session"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def make_session_token() -> str:
    exp = int(time.time()) + SESSION_TTL_SEC
    payload = str(exp)
    sig = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        payload, sig = token.split(".", 1)
        exp = int(payload)
        if exp < int(time.time()):
            return False
        expected = hmac.new(
            _session_secret(), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _extract_bearer(
    authorization: str | None = None,
    x_api_key: str | None = None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return (x_api_key or "").strip() or None


def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    """Proxy / client auth — API keys only."""
    key = _extract_bearer(authorization, x_api_key)
    if not key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    expected = config.get("api_key") or ""
    if expected and key == expected:
        return {"type": "admin", "key": key, "round_robin": True}

    found = db.find_api_key(key)
    if found:
        try:
            db.touch_api_key(found["id"])
        except Exception:
            pass
        return {
            "type": "db",
            "id": found["id"],
            "name": found["name"],
            "key": key,
            "round_robin": bool(found.get("round_robin", 1)),
        }

    raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    """Dashboard / admin API — session cookie OR admin/API key bearer."""
    # 1) Session cookie from login splash
    if verify_session_token(request.cookies.get(SESSION_COOKIE)):
        return {"type": "session", "round_robin": True}

    # 2) Bearer admin/api key (scripts, curl)
    key = _extract_bearer(authorization, x_api_key)
    if key:
        expected = config.get("api_key") or ""
        if expected and key == expected:
            return {"type": "admin", "key": key, "round_robin": True}
        found = db.find_api_key(key)
        if found and found.get("enabled", 1):
            try:
                db.touch_api_key(found["id"])
            except Exception:
                pass
            return {
                "type": "db",
                "id": found["id"],
                "name": found["name"],
                "key": key,
                "round_robin": bool(found.get("round_robin", 1)),
            }

    raise HTTPException(status_code=401, detail="Unauthorized")


class ImportBody(BaseModel):
    accounts: list[dict[str, Any]] | None = None


class IdsBody(BaseModel):
    ids: list[str] = Field(default_factory=list)
    enabled: bool | None = None
    mode: str | None = None


@app.on_event("startup")
async def _startup() -> None:
    ok, msg = db.integrity_ok()
    log.info("db=%s integrity=%s", config["db_path"], msg)
    if not ok:
        log.error("database integrity failed: %s", msg)
    await refresh_svc.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await refresh_svc.stop()
    await http_client.close()
    db.close()


@app.get("/", response_class=HTMLResponse)
async def ui_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    ok, msg = db.integrity_ok()
    return {
        "ok": ok,
        "integrity": msg,
        "refresh_interval_sec": config.get("refresh_interval_sec", 300),
    }


@app.get("/api/stats")
async def stats(_: dict = Depends(require_admin)) -> dict[str, Any]:
    s = db.stats()
    uptime_sec = int(time.time() - server_start)
    return {
        **s,
        "uptime_sec": uptime_sec,
        "refresh_interval_sec": config.get("refresh_interval_sec", 300),
    }


class LoginBody(BaseModel):
    password: str = ""


@app.post("/api/auth/login")
async def auth_login(body: LoginBody, response: Response) -> dict[str, Any]:
    expected = config.get("admin_password") or ""
    if not expected or not hmac.compare_digest(body.password or "", expected):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = make_session_token()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SEC,
        path="/",
    )
    return {"ok": True, "expires_in": SESSION_TTL_SEC}


@app.post("/api/auth/logout")
async def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    ok = verify_session_token(request.cookies.get(SESSION_COOKIE))
    return {"authenticated": ok}


@app.get("/api/events")
async def list_events(limit: int = 120, _: dict = Depends(require_admin)) -> dict[str, Any]:
    rows = db.list_events(limit=max(1, min(limit, 300)))
    return {"events": rows}


@app.websocket("/ws/log")
async def ws_log(ws: WebSocket):
    await ws.accept()
    q = broadcaster.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
                await ws.send_json(event)
            except asyncio.TimeoutError:
                await ws.send_json({"kind": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.unsubscribe(q)


@app.get("/api/config/public")
async def public_config(request: Request) -> dict[str, Any]:
    port = int(config.get("port", 8787))
    host_header = request.headers.get("host") or f"127.0.0.1:{port}"
    base = str(request.base_url).rstrip("/")
    if not base:
        base = f"http://{host_header}"
    return {
        "port": port,
        "base_url": base,
        "auth_required": True,
        "endpoints": {
            "ui": f"{base}/",
            "health": f"{base}/api/health",
            "import": f"{base}/api/accounts/import",
            "accounts": f"{base}/api/accounts",
            "models": f"{base}/v1/models",
            "responses": f"{base}/v1/responses",
            "chat_completions": f"{base}/v1/chat/completions",
            "openai_base": f"{base}/v1",
        },
        "curl_examples": {
            "responses": f"curl {base}/v1/responses -H \"Authorization: Bearer <API_KEY>\" -H \"Content-Type: application/json\" -d \"{{\\\"model\\\":\\\"grok-4.5\\\",\\\"input\\\":\\\"hi\\\"}}\"",
            "chat": f"curl {base}/v1/chat/completions -H \"Authorization: Bearer <API_KEY>\" -H \"Content-Type: application/json\" -d \"{{\\\"model\\\":\\\"grok-4.5\\\",\\\"messages\\\":[{{\\\"role\\\":\\\"user\\\",\\\"content\\\":\\\"hi\\\"}}]}}\"",
        },
        "refresh_interval_sec": config.get("refresh_interval_sec", 300),
        "upstream_base": config.get("upstream_base"),
    }


@app.get("/api/keys")
async def list_keys(_: dict = Depends(require_admin)) -> dict[str, Any]:
    rows = db.list_api_keys()
    return {"keys": rows}


@app.post("/api/keys")
async def create_key(body: dict[str, Any], _: dict = Depends(require_admin)) -> dict[str, Any]:
    name = str(body.get("name") or "default").strip() or "default"
    note = str(body.get("note") or "").strip()
    row = db.create_api_key(name=name, note=note)
    return {"ok": True, "key": row}


@app.post("/api/keys/{key_id}/enable")
async def enable_key(key_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
    db.set_api_key_enabled(key_id, True)
    return {"ok": True}


@app.post("/api/keys/{key_id}/disable")
async def disable_key(key_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
    db.set_api_key_enabled(key_id, False)
    return {"ok": True}


@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: str, _: dict = Depends(require_admin)) -> dict[str, Any]:
    db.delete_api_key(key_id)
    return {"ok": True}


@app.post("/api/keys/{key_id}/round-robin")
async def toggle_round_robin(key_id: str, body: dict[str, Any], _: dict = Depends(require_admin)) -> dict[str, Any]:
    enabled = bool(body.get("enabled", True))
    db.set_api_key_round_robin(key_id, enabled)
    return {"ok": True, "round_robin": enabled}


@app.get("/api/accounts")
async def list_accounts(
    status: str | None = None,
    enabled: str | None = None,
    q: str | None = None,
    _: dict = Depends(require_admin),
) -> dict[str, Any]:
    en = None
    if enabled in ("1", "true", "True"):
        en = True
    elif enabled in ("0", "false", "False"):
        en = False
    rows = db.list_accounts(status=status or None, enabled=en, q=q or None)
    # redact tokens in list view
    safe = []
    for r in rows:
        item = dict(r)
        item["access_token"] = (item.get("access_token") or "")[:16] + "..."
        item["refresh_token"] = (item.get("refresh_token") or "")[:12] + "..."
        item["id_token"] = (item.get("id_token") or "")[:12] + ("..." if item.get("id_token") else "")
        safe.append(item)
    return {"accounts": safe, "stats": db.stats()}


async def _import_one(raw: dict[str, Any], *, index: int | None = None, file: str | None = None) -> dict[str, Any]:
    """Import one CPA account: upsert → refresh → warmup. Returns result row + counters."""
    meta: dict[str, Any] = {}
    if index is not None:
        meta["index"] = index
    if file is not None:
        meta["file"] = file
    try:
        acc = db.upsert_from_cpa(raw)
        if not acc:
            email = raw.get("email", "?")
            db.add_event("import_reject", f"duplicate {email}", email=email)
            return {
                **meta,
                "ok": False,
                "rejected": True,
                "duplicate": True,
                "email": email,
                "error": "duplicate email",
                "_counter": "rejected",
            }
        r = await refresh_svc.refresh_one(acc)
        # invalid_grant: access_token may still work for chat
        is_auth_fail = r.get("status") == "auth_failed"
        if not r.get("ok") and not is_auth_fail:
            dead = r.get("status") == "dead"
            db.delete_account(acc["id"])
            db.add_event(
                "import_reject",
                f"rejected {acc['email']}: dead={dead}",
                account_id=acc["id"],
                email=acc["email"],
            )
            return {
                **meta,
                "ok": False,
                "rejected": True,
                "email": acc["email"],
                "error": r.get("error", "refresh failed"),
                "_counter": "rejected",
            }
        acc2 = db.get_account(acc["id"])
        if not acc2:
            return {**meta, "ok": False, "error": "account missing after refresh", "_counter": "error"}
        w = await _warmup(acc2["id"])
        status = (w or {}).get("status", "error")
        db.add_event(
            "import",
            f"imported {acc['email']} status={status}",
            account_id=acc["id"],
            email=acc["email"],
        )
        if w.get("ok"):
            counter = "active"
        elif status == "exhausted":
            counter = "exhausted"
        else:
            counter = "error"
        return {
            **meta,
            "ok": True,
            "id": acc["id"],
            "email": acc["email"],
            "status": status if not w.get("ok") else "active",
            "warmup": w,
            "_counter": counter,
            "_imported": True,
        }
    except Exception as e:
        db.add_event("import_error", str(e) if file is None else f"{file}: {e}")
        return {**meta, "ok": False, "error": str(e), "_counter": "error"}


async def _import_many(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    label: str = "Importing...",
) -> dict[str, Any]:
    """Concurrent import with semaphore. items = [(raw, meta), ...]."""
    n = len(items)
    conc = max(1, int(config.get("import_concurrency", 30)))
    sem = asyncio.Semaphore(conc)
    lock = asyncio.Lock()
    done_count = 0
    counters = {"imported": 0, "active": 0, "exhausted": 0, "rejected": 0, "error": 0}
    results: list[dict[str, Any]] = []

    refresh_svc.progress.update(
        {"running": True, "total": n, "done": 0, "errors": 0, "label": label}
    )
    db.add_event("import", f"{label} count={n} concurrency={conc}")

    async def worker(raw: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        nonlocal done_count
        async with sem:
            row = await _import_one(raw, **meta)
            async with lock:
                done_count += 1
                c = row.pop("_counter", "error")
                if row.pop("_imported", False):
                    counters["imported"] += 1
                if c in counters:
                    counters[c] += 1
                else:
                    counters["error"] += 1
                results.append(row)
                err_n = counters["rejected"] + counters["error"]
                refresh_svc.progress.update(
                    {
                        "done": done_count,
                        "errors": err_n,
                        "label": f"Importing {done_count}/{n}",
                    }
                )
            return row

    await asyncio.gather(*(worker(raw, meta) for raw, meta in items))

    refresh_svc.progress.update(
        {
            "running": False,
            "label": (
                f"Import done: {counters['active']} active, "
                f"{counters['exhausted']} exhausted, "
                f"{counters['rejected']} rejected, "
                f"{counters['error']} error"
            ),
        }
    )
    db.add_event(
        "import_summary",
        (
            f"imported={counters['imported']} active={counters['active']} "
            f"exhausted={counters['exhausted']} rejected={counters['rejected']} "
            f"error={counters['error']}"
        ),
    )
    return {
        "imported": counters["imported"],
        "active": counters["active"],
        "exhausted": counters["exhausted"],
        "rejected": counters["rejected"],
        "error": counters["error"],
        "success": counters["active"],
        "failed": counters["exhausted"] + counters["error"] + counters["rejected"],
        "results": results,
        "stats": db.stats(),
    }


@app.post("/api/accounts/import")
async def import_accounts(body: dict[str, Any] | list[Any], _: dict = Depends(require_admin)):
    if isinstance(body, list):
        accounts = body
    elif isinstance(body, dict) and isinstance(body.get("accounts"), list):
        accounts = body["accounts"]
    elif isinstance(body, dict):
        accounts = [body]
    else:
        raise HTTPException(status_code=400, detail="No accounts provided")

    items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for i, raw in enumerate(accounts):
        if not isinstance(raw, dict):
            continue
        items.append((raw, {"index": i}))
    return await _import_many(items, label="Importing...")


@app.post("/api/accounts/refresh")
async def refresh_accounts(body: IdsBody, _: dict = Depends(require_admin)):
    if body.ids:
        db.add_event("refresh", f"refresh selected count={len(body.ids)}")
        result = await refresh_svc.refresh_all(
            only_ids=body.ids, include_exhausted=True, include_dead=True
        )
    else:
        mode = body.mode or "active"
        db.add_event("refresh", f"refresh mode={mode}")
        result = await refresh_svc.refresh_filter(mode)
    db.add_event(
        "refresh_done",
        f"success={result.get('success')} failed={result.get('failed')} total={result.get('total')}",
    )
    return result


@app.post("/api/accounts/enable")
async def enable_accounts(body: IdsBody, _: dict = Depends(require_admin)):
    ids = body.ids or db.ids_by_filter(body.mode or "exhausted")
    n = db.bulk_set_enabled(ids, True)
    db.add_event("enable", f"enabled={n} mode={body.mode or 'ids'}")
    return {"updated": n, "enabled": True, "stats": db.stats()}


@app.post("/api/accounts/disable")
async def disable_accounts(body: IdsBody, _: dict = Depends(require_admin)):
    ids = body.ids or db.ids_by_filter(body.mode or "exhausted")
    n = db.bulk_set_enabled(ids, False)
    db.add_event("disable", f"disabled={n} mode={body.mode or 'ids'}")
    return {"updated": n, "enabled": False, "stats": db.stats()}


@app.post("/api/accounts/mark")
async def mark_accounts(body: dict[str, Any], _: dict = Depends(require_admin)):
    ids = body.get("ids") or []
    status = body.get("status")
    if status not in ("active", "exhausted", "dead", "error"):
        raise HTTPException(status_code=400, detail="invalid status")
    for i in ids:
        db.mark_status(i, status, error=body.get("error"))
    return {"updated": len(ids), "status": status, "stats": db.stats()}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, _: dict = Depends(require_admin)):
    db.delete_account(account_id)
    return {"ok": True, "stats": db.stats()}


@app.post("/api/accounts/delete")
async def delete_accounts(body: IdsBody, _: dict = Depends(require_admin)):
    ids = body.ids or db.ids_by_filter(body.mode or "dead")
    n = 0
    for i in ids:
        db.delete_account(i)
        n += 1
    db.add_event("delete", f"deleted={n} mode={body.mode or 'ids'}")
    return {"deleted": n, "stats": db.stats()}


async def _warmup(account_id: str) -> dict:
    """Warmup via real chat call. Must get a reply before marking active."""
    acc = db.get_account(account_id)
    if not acc:
        return {"ok": False, "error": "not found"}

    ver = config.get("client_version", "0.2.99")
    model = config.get("warmup_model", "grok-4.5")
    headers = {
        "Authorization": f"Bearer {acc['access_token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": config.get(
            "user_agent", f"grok-pager/{ver} grok-shell/{ver} (linux; x86_64)"
        ),
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-identifier": config.get("client_identifier", "grok-pager"),
        "x-grok-client-version": ver,
        "x-grok-model-override": model,
        "x-authenticateresponse": "authenticate-response",
    }
    if acc.get("email"):
        headers["x-email"] = acc["email"]
    uid = acc.get("sub") or acc.get("user_id") or acc.get("principal_id")
    if uid:
        headers["x-userid"] = str(uid)
    if acc.get("team_id"):
        headers["x-teamid"] = str(acc["team_id"])

    url = (
        config.get("upstream_base", "https://cli-chat-proxy.grok.com/v1").rstrip("/")
        + "/chat/completions"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "say only: halo"}],
        "max_tokens": 4,
        "stream": True,
    }
    for attempt in range(4):
        try:
            async with http_client.client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 429:
                    await backoff_sleep(attempt, max_sec=30)
                    continue
                if resp.status_code == 403:
                    text = (await resp.aread()).decode("utf-8", "replace")[:296]
                    db.mark_status(account_id, "exhausted", error=f"403: {text}")
                    return {"ok": False, "status": "exhausted", "body": text}
                if resp.status_code == 401:
                    text = (await resp.aread()).decode("utf-8", "replace")[:296]
                    db.mark_status(account_id, "dead", error=f"401: {text}")
                    return {"ok": False, "status": "dead", "body": text}
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")[:296]
                    db.mark_status(account_id, "error", error=f"{resp.status_code}: {text}")
                    return {"ok": False, "status": "error", "code": resp.status_code, "body": text}

                content = ""
                total_tokens = 0
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            js = json.loads(data)
                        except Exception:
                            continue
                        choices = js.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            msg = choices[0].get("message") or {}
                            piece = delta.get("content") or msg.get("content") or ""
                            if piece:
                                content += piece
                        usage = js.get("usage") or {}
                        if usage.get("total_tokens"):
                            total_tokens = int(usage["total_tokens"])

                content = content.strip()
                if not content:
                    db.mark_status(account_id, "error", error="empty chat reply")
                    return {"ok": False, "status": "error", "error": "empty chat reply"}

                if total_tokens > 0:
                    db.add_tokens_used(account_id, total_tokens)

                db.mark_status(account_id, "active")
                db.mark_success(account_id)
                return {
                    "ok": True,
                    "status": "active",
                    "reply": content[:80],
                    "tokens": total_tokens,
                }
        except Exception as e:
            if attempt < 3:
                await backoff_sleep(attempt, max_sec=10)
                continue
            db.mark_status(account_id, "error", error=str(e))
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "warmup exhausted retries"}


@app.post("/api/accounts/{account_id}/warmup")
async def warmup_one_route(account_id: str, _: dict = Depends(require_admin)):
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="not found")
    r = await refresh_svc.refresh_one(acc)
    if not r.get("ok"):
        return {"ok": False, "refresh": r}
    return await _warmup(account_id)


@app.get("/api/accounts/action-progress")
async def action_progress(_: dict = Depends(require_admin)):
    return refresh_svc.progress


@app.post("/api/warmup")
async def warmup_many(body: IdsBody, _: dict = Depends(require_admin)):
    ids = body.ids
    if not ids and body.mode:
        ids = db.ids_by_filter(body.mode)
    if not ids:
        ids = db.ids_by_filter("active")
    # filter out disabled
    ids = [i for i in ids if db.get_account(i).get("enabled", 1)]
    results = []
    ok = fail = 0
    db.add_event("warmup", f"warmup count={len(ids)} mode={body.mode or 'ids'}")
    # set progress
    refresh_svc.progress.update({"running": True, "total": len(ids), "done": 0, "errors": 0, "label": "Warming up..."})
    for i, acc_id in enumerate(ids):
        acc = db.get_account(acc_id)
        if not acc:
            fail += 1
            results.append({"id": acc_id, "ok": False, "error": "not found"})
            continue
        r = await refresh_svc.refresh_one(acc)
        if not r.get("ok"):
            fail += 1
            results.append({"id": acc_id, "ok": False, "refresh": r})
        else:
            w = await _warmup(acc_id)
            if w.get("ok"):
                ok += 1
            else:
                fail += 1
            results.append({"id": acc_id, **w})
        refresh_svc.progress.update({"done": i + 1, "errors": fail, "label": f"Warming up {i+1}/{len(ids)}"})
    refresh_svc.progress.update({"running": False, "label": f"Warmup done: {ok} ok, {fail} fail"})
    db.add_event("warmup_done", f"success={ok} failed={fail}")
    return {"success": ok, "failed": fail, "results": results, "stats": db.stats()}


# ---- OpenAI-compatible proxy surface ----
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def v1_proxy(path: str, request: Request, auth: dict = Depends(require_api_key)):
    return await proxy.forward(request, path, refresh_service=refresh_svc, round_robin=auth.get("round_robin", True))


@app.api_route("/responses", methods=["POST"])
async def responses_alias(request: Request, auth: dict = Depends(require_api_key)):
    return await proxy.forward(request, "responses", refresh_service=refresh_svc, round_robin=auth.get("round_robin", True))


@app.get("/api/models")
async def models_alias(_: bool = Depends(require_api_key)):
    # static list based on 9router sniff
    return {
        "object": "list",
        "data": [
            {"id": "grok-4.5", "object": "model"},
            {"id": "grok-4.5-high", "object": "model"},
            {"id": "grok-4.5-medium", "object": "model"},
            {"id": "grok-4.5-low", "object": "model"},
        ],
    }
