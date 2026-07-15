from __future__ import annotations

import asyncio
import logging
import time as time_module
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .broadcaster import LogBroadcaster
from .client import HttpClient, backoff_sleep
from .db import Database

log = logging.getLogger("grok-cli-proxy.refresh")


class RefreshService:
    def __init__(self, db: Database, config: dict[str, Any], http_client: HttpClient | None = None, broadcaster: LogBroadcaster | None = None):
        self.db = db
        self.config = config
        self.http = http_client.client if http_client else None
        self.bc = broadcaster
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._running = False
        self.progress: dict[str, Any] = {"running": False, "total": 0, "done": 0, "errors": 0, "label": ""}

    @property
    def token_url(self) -> str:
        return self.config.get("token_url", "https://auth.x.ai/oauth2/token")

    @property
    def client_id(self) -> str:
        return self.config.get(
            "client_id", "b1a00492-073a-47ea-816f-4c329264a828"
        )

    @property
    def interval(self) -> int:
        return int(self.config.get("refresh_interval_sec", 3600))

    @property
    def concurrency(self) -> int:
        return int(self.config.get("refresh_concurrency", 100))

    async def start(self) -> None:
        if self._task:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("refresh loop started interval=%ss", self.interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # first tick shortly after boot
        await asyncio.sleep(3)
        while not self._stop.is_set():
            try:
                await self.refresh_all()
            except Exception as e:
                log.exception("refresh tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def refresh_one(self, account: dict[str, Any]) -> dict[str, Any]:
        acc_id = account["id"]
        email = account["email"]
        rt = account.get("refresh_token") or ""
        if not rt:
            self.db.mark_status(acc_id, "dead", refresh_error="missing refresh_token")
            self.db.add_event("refresh_fail", "missing refresh_token", account_id=acc_id, email=email)
            if self.bc:
                self.bc.log("refresh_err", f"missing refresh_token", account=email, account_id=acc_id)
            return {"ok": False, "email": email, "error": "missing refresh_token"}

        client_id = account.get("client_id") or self.client_id
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": rt,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "grok-cli/proxy",
        }
        try:
            url = self.token_url
            for attempt in range(4):
                if self.http:
                    resp = await self.http.post(url, data=data, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=30) as _c:
                        resp = await _c.post(url, data=data, headers=headers)
                if resp.status_code == 429:
                    await backoff_sleep(attempt, max_sec=30)
                    continue
                break
            body = resp.text
            if resp.status_code >= 400:
                err = body[:300]
                low = err.lower()
                if "invalid_grant" in low or "revoked" in low or "unknown refresh" in low:
                    self.db.add_event("refresh_auth_fail", err, account_id=acc_id, email=email)
                    self.db.update_refresh_error(acc_id, err)
                    if self.bc:
                        self.bc.log("refresh_err", f"invalid_grant: {email}", account=email, account_id=acc_id)
                    return {"ok": False, "email": email, "status": "auth_failed", "error": err}
                self.db.mark_status(acc_id, "error", refresh_error=err, error=err)
                self.db.add_event("refresh_fail", err, account_id=acc_id, email=email)
                if self.bc:
                    self.bc.log("refresh_err", f"fail: {email}: {err[:80]}", account=email, account_id=acc_id)
                return {"ok": False, "email": email, "status": "error", "error": err}

            js = resp.json()
            access = js.get("access_token")
            if not access:
                self.db.mark_status(
                    acc_id, "error", refresh_error="no access_token in response"
                )
                self.db.add_event("refresh_fail", "no access_token", account_id=acc_id, email=email)
                if self.bc:
                    self.bc.log("refresh_err", f"no access_token: {email}", account=email, account_id=acc_id)
                return {"ok": False, "email": email, "error": "no access_token"}
            self.db.update_tokens(
                acc_id,
                access_token=access,
                refresh_token=js.get("refresh_token") or rt,
                id_token=js.get("id_token"),
                expires_in=int(js.get("expires_in") or 21600),
                scope=js.get("scope"),
            )
            self.db.add_event("refresh_ok", f"expires_in={js.get('expires_in')}", account_id=acc_id, email=email)
            if self.bc:
                self.bc.log("refresh_ok", f"tokens refreshed: {email}", account=email, account_id=acc_id)
            return {
                "ok": True,
                "email": email,
                "expires_in": js.get("expires_in"),
            }
        except Exception as e:
            self.db.mark_status(acc_id, "error", refresh_error=str(e), error=str(e))
            self.db.add_event("refresh_fail", str(e), account_id=acc_id, email=email)
            if self.bc:
                self.bc.log("refresh_err", f"exception: {email}: {str(e)[:80]}", account=email, account_id=acc_id)
            return {"ok": False, "email": email, "error": str(e)}

    @staticmethod
    def _needs_refresh(account: dict[str, Any], freshness_sec: int = 3600) -> bool:
        expires_at = account.get("expires_at")
        if not expires_at:
            return True
        try:
            expiry = datetime.fromisoformat(expires_at)
            remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
            return remaining < freshness_sec
        except Exception:
            return True

    async def refresh_all(
        self,
        *,
        only_ids: list[str] | None = None,
        include_exhausted: bool = True,
        include_dead: bool = False,
    ) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "error": "refresh already running"}
        self._running = True
        try:
            if only_ids is not None:
                accounts = []
                for i in only_ids:
                    acc = self.db.get_account(i)
                    if acc and acc.get("enabled", 1):
                        accounts.append(acc)
            else:
                accounts = self.db.list_accounts()
                filtered = []
                for a in accounts:
                    if not a.get("enabled", 1):
                        continue
                    if a["status"] == "active":
                        if self._needs_refresh(a):
                            filtered.append(a)
                    elif a["status"] == "exhausted" and include_exhausted:
                        filtered.append(a)
                    elif a["status"] == "dead" and include_dead:
                        filtered.append(a)
                    elif a["status"] == "error":
                        filtered.append(a)
                accounts = filtered

            n = len(accounts)
            ok = fail = 0
            results = []
            self.progress.update({"running": True, "total": n, "done": 0, "errors": 0, "label": "Refreshing..."})

            sem = asyncio.Semaphore(self.concurrency)
            done_count = 0

            async def worker(acc: dict) -> dict:
                nonlocal ok, fail, done_count
                async with sem:
                    r = await self.refresh_one(acc)
                if r.get("ok"):
                    ok += 1
                else:
                    fail += 1
                done_count += 1
                if done_count % 50 == 0 or done_count == n:
                    self.progress.update({"done": done_count, "errors": fail, "label": f"Refreshing {done_count}/{n}"})
                return r

            tasks = [worker(acc) for acc in accounts]
            results = await asyncio.gather(*tasks)
            self.progress.update({"running": False, "label": f"Done: {ok} ok, {fail} fail"})
            return {
                "ok": True,
                "total": n,
                "success": ok,
                "failed": fail,
                "results": results,
            }
        finally:
            self._running = False

    async def refresh_filter(self, mode: str) -> dict[str, Any]:
        """
        mode:
          - selected: caller passes ids separately
          - all / active / exhausted / dead / enabled
        """
        if mode == "all":
            ids = self.db.ids_by_filter("all")
            return await self.refresh_all(only_ids=ids, include_dead=True)
        if mode == "active":
            ids = self.db.ids_by_filter("active")
            return await self.refresh_all(only_ids=ids)
        if mode in ("exhausted", "all_exhausted"):
            ids = self.db.ids_by_filter("exhausted")
            return await self.refresh_all(only_ids=ids, include_exhausted=True)
        if mode == "dead":
            ids = self.db.ids_by_filter("dead")
            return await self.refresh_all(only_ids=ids, include_dead=True)
        if mode == "enabled":
            ids = self.db.ids_by_filter("enabled")
            return await self.refresh_all(only_ids=ids, include_exhausted=True)
        return await self.refresh_all()
