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

    @staticmethod
    def _parse_usage(usage: dict[str, Any] | None) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
            }
        inp = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        out = int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        total = int(usage.get("total_tokens") or 0)
        cached = 0
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or details.get("cache_read_input_tokens") or 0)
        if not cached:
            cached = int(
                usage.get("cached_tokens")
                or usage.get("cache_read_input_tokens")
                or 0
            )
        if total <= 0:
            total = inp + out
        return {
            "input_tokens": max(0, inp),
            "output_tokens": max(0, out),
            "cached_tokens": max(0, cached),
            "total_tokens": max(0, total),
        }

    def _record_usage(
        self,
        acc: dict[str, Any],
        *,
        model: str | None,
        path: str,
        status: int,
        duration_ms: float,
        usage: dict[str, int] | None = None,
        ok: bool = True,
    ) -> None:
        u = usage or {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }
        try:
            if ok and u.get("total_tokens", 0) > 0:
                self.db.add_tokens_used(acc["id"], int(u["total_tokens"]))
            self.db.log_request(
                account_id=acc.get("id"),
                email=acc.get("email"),
                model=model,
                path=f"/{path.lstrip('/')}",
                status_code=status,
                duration_ms=duration_ms,
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                cached_tokens=u.get("cached_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
                ok=ok,
            )
        except Exception:
            log.exception("failed to record usage account=%s", acc.get("email"))

    @staticmethod
    def _strip_data_url(value: Any) -> str:
        raw = str(value or "").strip()
        if raw.startswith("data:") and "," in raw:
            return raw.split(",", 1)[1]
        return raw

    @classmethod
    def extract_generated_images(cls, response: dict[str, Any] | None) -> list[str]:
        images: list[str] = []
        if not isinstance(response, dict):
            return images
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_generation_call":
                continue
            raw = item.get("result") or item.get("image") or ""
            if isinstance(raw, dict):
                raw = (
                    raw.get("b64_json")
                    or raw.get("base64")
                    or raw.get("data")
                    or ""
                )
            raw = cls._strip_data_url(raw)
            if raw:
                images.append(raw)
        return images

    @staticmethod
    def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
        usage = usage if isinstance(usage, dict) else {}
        tin = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        tout = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0) or (tin + tout)
        return {
            "input_tokens": tin,
            "output_tokens": tout,
            "prompt_tokens": tin,
            "completion_tokens": tout,
            "total_tokens": total,
        }

    def _build_image_upstream_body(self, body: dict[str, Any]) -> dict[str, Any]:
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        if len(prompt) > 4000:
            raise HTTPException(status_code=400, detail="prompt too long (max 4000)")

        try:
            n = int(body.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, 4))

        model = str(body.get("model") or "grok-4.5").strip() or "grok-4.5"
        # Free CLI path uses chat model + image_generation tool
        if model in ("grok-imagine", "grok-2-image", "dall-e-3", "dall-e-2"):
            model = "grok-4.5"

        tool: dict[str, Any] = {"type": "image_generation"}
        size = body.get("size")
        quality = body.get("quality")
        if size:
            tool["size"] = size
        if quality:
            tool["quality"] = quality

        text = f"Generate an image: {prompt}. Use the image_generation tool."
        negative = str(body.get("negative_prompt") or "").strip()
        if negative:
            text += f" Avoid: {negative}."

        upstream = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            ],
            "tools": [tool],
            "stream": False,
            "reasoning": {"effort": "low"},
            "max_output_tokens": 1024,
        }
        return {"upstream": upstream, "n": n, "model": model, "prompt": prompt}

    async def generate_images(
        self,
        body: dict[str, Any],
        *,
        refresh_service=None,
        round_robin: bool = True,
    ) -> dict[str, Any]:
        """OpenAI-like images API via Grok CLI free responses + image_generation tool."""
        parsed = self._build_image_upstream_body(body)
        upstream_body = parsed["upstream"]
        n = int(parsed["n"])
        model = parsed["model"]
        timeout = float(
            self.config.get("image_timeout_sec")
            or max(float(self.config.get("request_timeout_sec", 120)), 180)
        )
        url = f"{self.base}/responses"
        generated: list[str] = []
        total_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        last_err: str | None = None
        max_attempts = max(n * 4, 4)
        attempt = 0
        tried: set[str] = set()

        while len(generated) < n and attempt < max_attempts:
            attempt += 1
            acc = self.db.pick_active_round_robin()
            if not acc:
                break
            if acc["id"] in tried and len(tried) >= max(1, self.db.stats().get("active_enabled", 0) or 1):
                # allow reuse after cycling full pool once
                tried.clear()
            tried.add(acc["id"])

            t0 = time.time()
            email_acc = acc.get("email", "?")
            headers = self._headers(acc, model=model)
            # Prefer grok-shell identifier for image tool path (per free-image guide)
            headers["x-grok-client-identifier"] = self.config.get(
                "image_client_identifier",
                self.config.get("client_identifier", "grok-shell"),
            )
            try:
                resp = await self.http.post(
                    url,
                    headers=headers,
                    json=upstream_body,
                    timeout=timeout,
                )
                ms = (time.time() - t0) * 1000
                text = resp.text
                if resp.status_code >= 400:
                    err_cls = self._classify_error(resp.status_code, text)
                    if resp.status_code == 429:
                        if self.bc:
                            self.bc.log(
                                "proxy_err",
                                "image rate limited",
                                method="POST",
                                path="/images/generations",
                                status=429,
                                account=email_acc,
                                duration_ms=ms,
                                model=model,
                            )
                        await backoff_sleep(attempt, max_sec=30)
                        continue
                    if err_cls == "exhausted":
                        self.db.mark_status(
                            acc["id"], "exhausted", error=f"{resp.status_code}: {text[:296]}"
                        )
                        if self.bc:
                            self.bc.log(
                                "proxy_err",
                                f"image exhausted: {email_acc}",
                                method="POST",
                                path="/images/generations",
                                status=403,
                                account=email_acc,
                                duration_ms=ms,
                                model=model,
                            )
                        continue
                    if err_cls == "dead":
                        self.db.mark_status(
                            acc["id"], "dead", error=f"{resp.status_code}: {text[:296]}"
                        )
                        continue
                    if err_cls == "auth" and refresh_service is not None:
                        await refresh_service.refresh_one(acc)
                        continue
                    last_err = text[:300] or f"upstream {resp.status_code}"
                    self.db.mark_status(
                        acc["id"], "error", error=f"{resp.status_code}: {text[:296]}"
                    )
                    continue

                try:
                    js = resp.json()
                except Exception:
                    last_err = "invalid upstream json"
                    continue

                images = self.extract_generated_images(js)
                usage = self.normalize_usage(js.get("usage") if isinstance(js, dict) else None)
                for k in total_usage:
                    total_usage[k] += int(usage.get(k, 0) or 0)

                if not images:
                    last_err = "no image_generation_call in response"
                    if self.bc:
                        self.bc.log(
                            "proxy_err",
                            last_err,
                            method="POST",
                            path="/images/generations",
                            status=502,
                            account=email_acc,
                            duration_ms=ms,
                            model=model,
                        )
                    # don't kill account for empty tool result; try next
                    continue

                generated.extend(images)
                self.db.mark_success(acc["id"])
                self._record_usage(
                    acc,
                    model=model,
                    path="images/generations",
                    status=200,
                    duration_ms=ms,
                    usage=self._parse_usage(js.get("usage") if isinstance(js, dict) else None),
                    ok=True,
                )
                if self.bc:
                    self.bc.log(
                        "proxy_ok",
                        f"image gen +{len(images)}",
                        method="POST",
                        path="/images/generations",
                        status=200,
                        account=email_acc,
                        duration_ms=ms,
                        model=model,
                        tokens=usage.get("total_tokens"),
                    )
            except httpx.TimeoutException:
                last_err = "upstream timeout"
                self.db.mark_status(acc["id"], "error", error="image timeout")
                if self.bc:
                    self.bc.log(
                        "proxy_err",
                        "image timeout",
                        method="POST",
                        path="/images/generations",
                        status=504,
                        account=email_acc,
                        model=model,
                    )
                continue
            except Exception as e:
                last_err = str(e)
                self.db.mark_status(acc["id"], "error", error=str(e))
                log.exception("image generation error account=%s", acc.get("email"))
                continue

        if not generated:
            if last_err and "timeout" in (last_err or "").lower():
                raise HTTPException(status_code=504, detail=last_err)
            raise HTTPException(
                status_code=503,
                detail=last_err or "All active accounts failed to generate image",
            )

        return {
            "created": int(time.time()),
            "data": [{"b64_json": img} for img in generated[:n]],
            "usage": total_usage,
            "model": model,
        }

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
        # try parse for stream flag + model, strip unsupported tools
        stream = False
        model = None
        try:
            if body:
                payload = json.loads(body)
                stream = bool(payload.get("stream"))
                model = payload.get("model")
                # strip tools with unsupported types (e.g. "custom") that xAI rejects
                tools = payload.get("tools")
                if tools:
                    cleaned = [t for t in tools if isinstance(t, dict) and t.get("type") not in ("custom",)]
                    if len(cleaned) != len(tools):
                        payload["tools"] = cleaned
                        body = json.dumps(payload).encode("utf-8")
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
                        url=url,
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
                            self.db.mark_status(acc["id"], "exhausted", error=f"{upstream.status_code}: {raw[:296]}")
                            log_req(403, ms, f"exhausted: {email_acc}")
                            continue
                        if err_cls == "dead":
                            self.db.mark_status(acc["id"], "dead", error=f"{upstream.status_code}: {raw[:296]}")
                            log_req(401, ms, f"dead: {email_acc}")
                            continue
                        if err_cls == "auth" and refresh_service is not None:
                            await refresh_service.refresh_one(acc)
                            log_req(401, ms, f"auth retry: {email_acc}")
                            continue
                        self.db.mark_status(acc["id"], "error", error=f"{upstream.status_code}: {raw[:296]}")
                        log_req(upstream.status_code, ms, raw[:100])
                        return Response(
                            content=raw,
                            status_code=upstream.status_code,
                            media_type="application/json",
                        )

                    last_usage: dict[str, int] = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": 0,
                    }

                    async def gen():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                try:
                                    text = chunk.decode("utf-8")
                                    if "usage" in text:
                                        for line in text.splitlines():
                                            if line.startswith("data: "):
                                                try:
                                                    data = json.loads(line[6:])
                                                    if isinstance(data, dict) and data.get("usage"):
                                                        parsed = self._parse_usage(data.get("usage"))
                                                        if parsed.get("total_tokens") or parsed.get("input_tokens") or parsed.get("output_tokens"):
                                                            last_usage.update(parsed)
                                                except Exception:
                                                    pass
                                except Exception:
                                    pass
                                yield chunk
                            self.db.mark_success(acc["id"])
                            ms_done = (time.time() - t0) * 1000
                            self._record_usage(
                                acc,
                                model=model,
                                path=path,
                                status=upstream.status_code,
                                duration_ms=ms_done,
                                usage=last_usage,
                                ok=True,
                            )
                            log_req(200, ms_done)
                        finally:
                            await upstream.aclose()

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
                        self.db.mark_status(acc["id"], "exhausted", error=f"{upstream.status_code}: {text[:296]}")
                        log_req(403, ms, f"exhausted: {email_acc}")
                        continue
                    if err_cls == "dead":
                        self.db.mark_status(acc["id"], "dead", error=f"{upstream.status_code}: {text[:296]}")
                        log_req(401, ms, f"dead: {email_acc}")
                        continue
                    if err_cls == "auth" and refresh_service is not None:
                        await refresh_service.refresh_one(acc)
                        log_req(401, ms, f"auth retry: {email_acc}")
                        continue
                    self.db.mark_status(acc["id"], "error", error=f"{upstream.status_code}: {text[:296]}")
                    log_req(upstream.status_code, ms, text[:100])
                    return Response(
                        content=raw,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get(
                            "content-type", "application/json"
                        ),
                    )
                    
                usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                }
                try:
                    js = json.loads(text)
                    if isinstance(js, dict) and js.get("usage"):
                        usage = self._parse_usage(js.get("usage"))
                except Exception:
                    pass

                self.db.mark_success(acc["id"])
                self._record_usage(
                    acc,
                    model=model,
                    path=path,
                    status=upstream.status_code,
                    duration_ms=ms,
                    usage=usage,
                    ok=True,
                )
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
