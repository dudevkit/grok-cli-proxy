from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .broadcaster import LogBroadcaster
from .client import HttpClient, backoff_sleep
from .db import Database

log = logging.getLogger("grok-cli-proxy.warmup")


class WarmupService:
    def __init__(
        self,
        db: Database,
        config: dict[str, Any],
        http_client: HttpClient,
        broadcaster: LogBroadcaster | None = None,
        refresh_service=None,
    ):
        self.db = db
        self.config = config
        self.http = http_client.client
        self.bc = broadcaster
        self.refresh_svc = refresh_service
        self._cancel = asyncio.Event()
        self._running = False
        self.progress: dict[str, Any] = {
            "running": False,
            "total": 0,
            "done": 0,
            "errors": 0,
            "label": "",
            "kind": "",
            "cancelled": False,
        }

    @property
    def concurrency(self) -> int:
        return max(1, int(self.config.get("warmup_concurrency", 10)))

    def request_stop(self) -> dict[str, Any]:
        if not self._running:
            return {"ok": False, "error": "no warmup running"}
        self._cancel.set()
        self.progress.update({"label": "Stopping warmup...", "cancelled": True})
        if self.bc:
            self.bc.log("warmup_cancel", "stop requested")
        return {"ok": True, "message": "stop requested"}

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    async def warmup_one(self, account_id: str, *, do_refresh: bool = False) -> dict[str, Any]:
        if self.is_cancelled():
            return {"ok": False, "cancelled": True, "error": "cancelled"}

        acc = self.db.get_account(account_id)
        if not acc:
            return {"ok": False, "error": "not found"}

        email = acc.get("email", "?")
        if do_refresh and self.refresh_svc is not None:
            if self.bc:
                self.bc.log("warmup_try", f"refresh before warmup", account=email)
            r = await self.refresh_svc.refresh_one(acc)
            if not r.get("ok"):
                if self.bc:
                    self.bc.log(
                        "warmup_err",
                        f"refresh failed: {r.get('error') or r.get('status')}",
                        account=email,
                    )
                return {"ok": False, "refresh": r, "email": email}
            acc = self.db.get_account(account_id) or acc

        if self.is_cancelled():
            return {"ok": False, "cancelled": True, "error": "cancelled", "email": email}

        ver = self.config.get("client_version", "0.2.99")
        model = self.config.get("warmup_model", "grok-4.5")
        headers = {
            "Authorization": f"Bearer {acc['access_token']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.config.get(
                "user_agent", f"grok-pager/{ver} grok-shell/{ver} (linux; x86_64)"
            ),
            "X-XAI-Token-Auth": "xai-grok-cli",
            "x-xai-token-auth": "xai-grok-cli",
            "x-grok-client-identifier": self.config.get("client_identifier", "grok-pager"),
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
            self.config.get("upstream_base", "https://cli-chat-proxy.grok.com/v1").rstrip("/")
            + "/chat/completions"
        )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "say only: halo"}],
            "max_tokens": 4,
            "stream": True,
        }

        if self.bc:
            self.bc.log("warmup_try", f"warmup {email}", account=email, model=model)

        for attempt in range(4):
            if self.is_cancelled():
                return {"ok": False, "cancelled": True, "error": "cancelled", "email": email}
            try:
                async with self.http.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code == 429:
                        if self.bc:
                            self.bc.log(
                                "warmup_err",
                                "rate limited",
                                account=email,
                                status=429,
                            )
                        await backoff_sleep(attempt, max_sec=30)
                        continue
                    if resp.status_code == 403:
                        text = (await resp.aread()).decode("utf-8", "replace")[:296]
                        self.db.mark_status(account_id, "exhausted", error=f"403: {text}")
                        if self.bc:
                            self.bc.log(
                                "warmup_err",
                                f"exhausted: {text[:80]}",
                                account=email,
                                status=403,
                            )
                        return {
                            "ok": False,
                            "status": "exhausted",
                            "body": text,
                            "email": email,
                        }
                    if resp.status_code == 401:
                        text = (await resp.aread()).decode("utf-8", "replace")[:296]
                        self.db.mark_status(account_id, "dead", error=f"401: {text}")
                        if self.bc:
                            self.bc.log(
                                "warmup_err",
                                f"dead: {text[:80]}",
                                account=email,
                                status=401,
                            )
                        return {
                            "ok": False,
                            "status": "dead",
                            "body": text,
                            "email": email,
                        }
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")[:296]
                        self.db.mark_status(
                            account_id, "error", error=f"{resp.status_code}: {text}"
                        )
                        if self.bc:
                            self.bc.log(
                                "warmup_err",
                                f"{resp.status_code}: {text[:80]}",
                                account=email,
                                status=resp.status_code,
                            )
                        return {
                            "ok": False,
                            "status": "error",
                            "code": resp.status_code,
                            "body": text,
                            "email": email,
                        }

                    content = ""
                    total_tokens = 0
                    async for line in resp.aiter_lines():
                        if self.is_cancelled():
                            return {
                                "ok": False,
                                "cancelled": True,
                                "error": "cancelled",
                                "email": email,
                            }
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
                        self.db.mark_status(account_id, "error", error="empty chat reply")
                        if self.bc:
                            self.bc.log(
                                "warmup_err",
                                "empty chat reply",
                                account=email,
                            )
                        return {
                            "ok": False,
                            "status": "error",
                            "error": "empty chat reply",
                            "email": email,
                        }

                    if total_tokens > 0:
                        self.db.add_tokens_used(account_id, total_tokens)

                    self.db.mark_status(account_id, "active")
                    self.db.mark_success(account_id)
                    if self.bc:
                        self.bc.log(
                            "warmup_ok",
                            f"reply={content[:40]} tok={total_tokens}",
                            account=email,
                            tokens=total_tokens,
                        )
                    return {
                        "ok": True,
                        "status": "active",
                        "reply": content[:80],
                        "tokens": total_tokens,
                        "email": email,
                    }
            except Exception as e:
                if attempt < 3 and not self.is_cancelled():
                    await backoff_sleep(attempt, max_sec=10)
                    continue
                self.db.mark_status(account_id, "error", error=str(e))
                if self.bc:
                    self.bc.log("warmup_err", str(e)[:120], account=email)
                return {"ok": False, "error": str(e), "email": email}

        return {"ok": False, "error": "warmup exhausted retries", "email": email}

    async def warmup_many(
        self,
        ids: list[str],
        *,
        do_refresh: bool = True,
        label: str = "Warming up...",
    ) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "error": "warmup already running"}

        self._running = True
        self._cancel.clear()
        ok = fail = cancelled = 0
        results: list[dict[str, Any]] = []
        n = len(ids)
        conc = self.concurrency
        sem = asyncio.Semaphore(conc)
        lock = asyncio.Lock()
        done_count = 0

        self.progress.update(
            {
                "running": True,
                "total": n,
                "done": 0,
                "errors": 0,
                "label": label,
                "kind": "warmup",
                "cancelled": False,
            }
        )
        self.db.add_event("warmup", f"warmup count={n} concurrency={conc}")
        if self.bc:
            self.bc.log("warmup_try", f"start count={n} concurrency={conc}")

        async def worker(acc_id: str) -> dict[str, Any]:
            nonlocal ok, fail, cancelled, done_count
            if self.is_cancelled():
                row = {
                    "id": acc_id,
                    "ok": False,
                    "cancelled": True,
                    "error": "cancelled",
                }
            else:
                async with sem:
                    if self.is_cancelled():
                        row = {
                            "id": acc_id,
                            "ok": False,
                            "cancelled": True,
                            "error": "cancelled",
                        }
                    else:
                        w = await self.warmup_one(acc_id, do_refresh=do_refresh)
                        row = {"id": acc_id, **w}

            async with lock:
                done_count += 1
                if row.get("cancelled"):
                    cancelled += 1
                    fail += 1
                elif row.get("ok"):
                    ok += 1
                else:
                    fail += 1
                results.append(row)
                self.progress.update(
                    {
                        "done": done_count,
                        "errors": fail,
                        "label": (
                            f"Stopping {done_count}/{n}..."
                            if self.is_cancelled()
                            else f"Warming up {done_count}/{n}"
                        ),
                    }
                )
            return row

        try:
            await asyncio.gather(*(worker(i) for i in ids))
            was_cancelled = self.is_cancelled()
            final_label = (
                f"Warmup cancelled: {ok} ok, {fail} fail, {cancelled} skipped"
                if was_cancelled
                else f"Warmup done: {ok} ok, {fail} fail"
            )
            self.progress.update(
                {
                    "running": False,
                    "label": final_label,
                    "cancelled": was_cancelled,
                }
            )
            self.db.add_event(
                "warmup_done",
                f"success={ok} failed={fail} cancelled={cancelled}",
            )
            if self.bc:
                self.bc.log(
                    "warmup_ok" if not was_cancelled else "warmup_cancel",
                    final_label,
                )
            return {
                "ok": True,
                "success": ok,
                "failed": fail,
                "cancelled": cancelled,
                "stopped": was_cancelled,
                "results": results,
                "stats": self.db.stats(),
            }
        finally:
            self._running = False
            self._cancel.clear()
            self.progress["running"] = False
