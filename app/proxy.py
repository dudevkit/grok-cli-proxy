from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .broadcaster import LogBroadcaster
from .client import HttpClient, backoff_sleep
from .db import Database

log = logging.getLogger("grok-cli-proxy.proxy")


class UpstreamProxy:
    def __init__(self, db: Database, config: dict[str, Any], http_client: HttpClient, broadcaster: LogBroadcaster | None = None):
        self.db = db
        self.config = config
        self.http = http_client.client
        self.bc = broadcaster

    @property
    def base(self) -> str:
        return self.config.get(
            "upstream_base", "https://cli-chat-proxy.grok.com/v1"
        ).rstrip("/")

    def _headers(self, account: dict[str, Any], model: str | None = None) -> dict[str, str]:
        ver = self.config.get("client_version", "0.2.99")
        h = {
            "Authorization": f"Bearer {account['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.config.get(
                "user_agent",
                f"grok-pager/{ver} grok-shell/{ver} (linux; x86_64)",
            ),
            # CLI-required auth middleware header
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-client-identifier": self.config.get(
                "client_identifier", "grok-pager"
            ),
            "x-grok-client-version": ver,
            "x-authenticateresponse": "authenticate-response",
        }
        # Critical for routing to correct inference cluster (CLI docs)
        if model:
            h["x-grok-model-override"] = model
        if account.get("email"):
            h["x-email"] = account["email"]
        # prefer user_id/sub/principal_id if present
        uid = account.get("sub") or account.get("user_id") or account.get("principal_id")
        if uid:
            h["x-userid"] = str(uid)
        team = account.get("team_id")
        if team:
            h["x-teamid"] = str(team)
        return h

    def _classify_error(self, status: int, body: str) -> str | None:
        low = (body or "").lower()
        if status == 403 or "spending limit" in low or "credits are exhausted" in low or "quota" in low:
            return "exhausted"
        if status == 401 and (
            "invalid_grant" in low
            or "revoked" in low
            or "invalid token" in low
            or "unauthorized" in low
        ):
            # 401 can be expired access token; caller may retry after refresh.
            if "revoked" in low or "invalid_grant" in low:
                return "dead"
            return "auth"
        if "invalid_grant" in low or "revoked" in low:
            return "dead"
        return None

    async def forward(
        self,
        request: Request,
        path: str,
        *,
        refresh_service=None,
        max_retries: int = 3,
        round_robin: bool = True,
    ) -> Response:
        body = await request.body()
        # try parse for stream flag + model
        stream = False
        model = None
        try:
            if body:
                payload = json.loads(body)
                stream = bool(payload.get("stream"))
                model = payload.get("model")
        except Exception:
            pass

        last_err = None
        fixed_acc = None  # for non-round-robin mode, lock to one account
        tried: set[str] = set()  # account IDs already attempted
        
        for attempt in range(100 if round_robin else 1):
            if round_robin:
                acc = self.db.pick_active_round_robin()
                if not acc or acc["id"] in tried:
                    break  # all active accounts tried
                tried.add(acc["id"])
            else:
                if fixed_acc is None:
                    fixed_acc = self.db.pick_active_round_robin()
                acc = fixed_acc
            if not acc:
                raise HTTPException(
                    status_code=404,
                    detail="No active credentials for provider: grok-cli",
                )

            t0 = time.time()
            email_acc = acc.get("email", "?")
            url = f"{self.base}/{path.lstrip('/')}"
            headers = self._headers(acc, model=model)
            try:
                client_override = request.headers.get("x-grok-model-override")
                if client_override:
                    headers["x-grok-model-override"] = client_override
            except Exception:
                pass
            timeout = float(self.config.get("request_timeout_sec", 120))

            def log_req(status, dur, err=None):
                if self.bc:
                    self.bc.log(
                        "proxy_ok" if status < 400 else "proxy_err",
                        err or f"{request.method} {path}",
                        method=request.method,
                        path=f"/{path}",
                        status=status,
                        account=email_acc,
                        duration_ms=dur,
                        model=model,
                    )

            try:
                if stream:
                    req = self.http.build_request(
                        request.method,
                        url,
                        headers=headers,
                        content=body,
                    )
                    upstream = await self.http.send(req, stream=True)
                    if upstream.status_code >= 400:
                        raw = (await upstream.aread()).decode("utf-8", "replace")
                        await upstream.aclose()
                        ms = (time.time() - t0) * 1000
                        err_cls = self._classify_error(upstream.status_code, raw)
                        if upstream.status_code == 429:
                            log_req(429, ms, "rate limited")
                            await backoff_sleep(attempt, max_sec=timeout)
                            continue
                        if err_cls == "exhausted":
                            self.db.mark_status(acc["id"], "exhausted", error=raw[:300])
                            log_req(403, ms, f"exhausted: {email_acc}")
                            continue
                        if err_cls == "dead":
                            self.db.mark_status(acc["id"], "dead", error=raw[:300])
                            log_req(401, ms, f"dead: {email_acc}")
                            continue
                        if err_cls == "auth" and refresh_service is not None:
                            await refresh_service.refresh_one(acc)
                            log_req(401, ms, f"auth retry: {email_acc}")
                            continue
                        self.db.mark_status(acc["id"], "error", error=raw[:300])
                        log_req(upstream.status_code, ms, raw[:100])
                        return Response(
                            content=raw,
                            status_code=upstream.status_code,
                            media_type="application/json",
                        )

                    async def gen():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                try:
                                    text = chunk.decode("utf-8")
                                    if "usage" in text and "total_tokens" in text:
                                        for line in text.splitlines():
                                            if line.startswith("data: "):
                                                try:
                                                    data = json.loads(line[6:])
                                                    if "usage" in data and "total_tokens" in data["usage"]:
                                                        self.db.add_tokens_used(acc["id"], int(data["usage"]["total_tokens"]))
                                                except:
                                                    pass
                                except:
                                    pass
                                yield chunk
                            self.db.mark_success(acc["id"])
                        finally:
                            await upstream.aclose()

                    log_req(200, (time.time() - t0) * 1000)
                    return StreamingResponse(
                        gen(),
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get(
                            "content-type", "text/event-stream"
                        ),
                        headers={
                            "x-grok-proxy-account": email_acc,
                            "x-grok-proxy-account-id": acc["id"],
                        },
                    )

                upstream = await self.http.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body,
                )
                raw = upstream.content
                text = raw.decode("utf-8", "replace")
                ms = (time.time() - t0) * 1000
                if upstream.status_code >= 400:
                    err_cls = self._classify_error(upstream.status_code, text)
                    if upstream.status_code == 429:
                        log_req(429, ms, "rate limited")
                        await backoff_sleep(attempt, max_sec=timeout)
                        continue
                    if err_cls == "exhausted":
                        self.db.mark_status(acc["id"], "exhausted", error=text[:300])
                        log_req(403, ms, f"exhausted: {email_acc}")
                        continue
                    if err_cls == "dead":
                        self.db.mark_status(acc["id"], "dead", error=text[:300])
                        log_req(401, ms, f"dead: {email_acc}")
                        continue
                    if err_cls == "auth" and refresh_service is not None:
                        await refresh_service.refresh_one(acc)
                        log_req(401, ms, f"auth retry: {email_acc}")
                        continue
                    self.db.mark_status(acc["id"], "error", error=text[:300])
                    log_req(upstream.status_code, ms, text[:100])
                    return Response(
                        content=raw,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get(
                            "content-type", "application/json"
                        ),
                    )
                    
                try:
                    js = json.loads(text)
                    if "usage" in js and "total_tokens" in js["usage"]:
                        self.db.add_tokens_used(acc["id"], int(js["usage"]["total_tokens"]))
                except:
                    pass
                    
                self.db.mark_success(acc["id"])
                log_req(upstream.status_code, ms)
                return Response(
                    content=raw,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get(
                        "content-type", "application/json"
                    ),
                    headers={
                        "x-grok-proxy-account": email_acc,
                        "x-grok-proxy-account-id": acc["id"],
                    },
                )
            except HTTPException:
                raise
            except Exception as e:
                last_err = str(e)
                self.db.mark_status(acc["id"], "error", error=str(e))
                log.exception("upstream error account=%s", acc.get("email"))
                continue

        raise HTTPException(
            status_code=503,
            detail=last_err or "All active accounts failed",
        )
