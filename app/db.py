from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Database:
    """Single-writer SQLite store for Grok CLI accounts."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _init_schema(self) -> None:
        with self.tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  display_name TEXT,
                  status TEXT NOT NULL DEFAULT 'active',
                  enabled INTEGER NOT NULL DEFAULT 1,
                  priority INTEGER NOT NULL DEFAULT 0,
                  access_token TEXT NOT NULL,
                  refresh_token TEXT NOT NULL,
                  id_token TEXT,
                  token_type TEXT DEFAULT 'Bearer',
                  expires_in INTEGER DEFAULT 21600,
                  expires_at TEXT,
                  scope TEXT,
                  sub TEXT,
                  client_id TEXT,
                  base_url TEXT,
                  raw_json TEXT,
                  last_refresh_at TEXT,
                  last_refresh_error TEXT,
                  last_used_at TEXT,
                  last_error TEXT,
                  last_error_at TEXT,
                  consecutive_use_count INTEGER NOT NULL DEFAULT 0,
                  success_count INTEGER NOT NULL DEFAULT 0,
                  fail_count INTEGER NOT NULL DEFAULT 0,
                  tokens_used INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
                CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled);
                CREATE INDEX IF NOT EXISTS idx_accounts_priority ON accounts(priority);

                CREATE TABLE IF NOT EXISTS meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  account_id TEXT,
                  email TEXT,
                  kind TEXT NOT NULL,
                  message TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  key TEXT NOT NULL UNIQUE,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  round_robin INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  last_used_at TEXT,
                  note TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled);

                CREATE TABLE IF NOT EXISTS request_logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  created_at TEXT NOT NULL,
                  account_id TEXT,
                  email TEXT,
                  model TEXT,
                  path TEXT,
                  status_code INTEGER,
                  duration_ms REAL,
                  input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0,
                  cached_tokens INTEGER NOT NULL DEFAULT 0,
                  total_tokens INTEGER NOT NULL DEFAULT 0,
                  ok INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_request_logs_created ON request_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_request_logs_ok ON request_logs(ok);
                """
            )
            # rr pointer
            row = conn.execute("SELECT value FROM meta WHERE key='rr_index'").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('rr_index', '0')"
                )

    def integrity_ok(self) -> tuple[bool, str]:
        with self._lock:
            val = self._conn.execute("PRAGMA integrity_check").fetchone()[0]
            return val == "ok", val

    def stats(self) -> dict[str, int | float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c FROM accounts GROUP BY status"
            ).fetchall()
            out: dict[str, int | float] = {r["status"]: r["c"] for r in rows}
            out["total"] = sum(out.values())
            
            # Count enabled stats
            out["enabled"] = self._conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE enabled=1"
            ).fetchone()[0]
            
            active_enabled = self._conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE enabled=1 AND status='active'"
            ).fetchone()[0]
            out["active_enabled"] = active_enabled
            
            # Calculate credit (2M tokens limit per active account)
            # Only count active + enabled accounts towards the available pool capacity
            token_stats = self._conn.execute(
                "SELECT SUM(tokens_used) FROM accounts WHERE enabled=1 AND status='active'"
            ).fetchone()[0]
            
            active_used = token_stats if token_stats else 0
            pool_capacity = active_enabled * 2_000_000
            
            out["active_tokens_used"] = active_used
            out["pool_capacity"] = pool_capacity
            out["pool_remaining"] = max(0, pool_capacity - active_used)
            
            return out

    def list_accounts(
        self,
        status: str | None = None,
        enabled: bool | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status=?"
            args.append(status)
        if enabled is not None:
            sql += " AND enabled=?"
            args.append(1 if enabled else 0)
        if q:
            sql += " AND (email LIKE ? OR display_name LIKE ? OR id LIKE ?)"
            like = f"%{q}%"
            args.extend([like, like, like])
        # enabled first, then active -> exhausted -> dead -> error
        sql += """
            ORDER BY
              CASE WHEN enabled=1 THEN 0 ELSE 1 END ASC,
              CASE lower(coalesce(status,''))
                WHEN 'active' THEN 0
                WHEN 'exhausted' THEN 1
                WHEN 'dead' THEN 2
                WHEN 'error' THEN 3
                ELSE 4
              END ASC,
              priority ASC,
              lower(email) ASC
        """
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            return [dict(r) for r in rows]

    def list_events(self, limit: int = 120) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, account_id, email, kind, message, created_at
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_events(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM events")

    def add_event(
        self,
        kind: str,
        message: str,
        *,
        account_id: str | None = None,
        email: str | None = None,
    ) -> None:
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO events(account_id, email, kind, message, created_at)
                VALUES(?,?,?,?,?)
                """,
                (account_id, email, kind, message[:500], now),
            )

    def api_key_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0])

    def _hydrate_account(self, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        # expose team_id / principal fields from raw_json for proxy headers
        if not item.get("team_id") or not item.get("user_id"):
            try:
                raw = json.loads(item.get("raw_json") or "{}")
                if isinstance(raw, dict):
                    if not item.get("team_id") and raw.get("team_id"):
                        item["team_id"] = raw["team_id"]
                    if not item.get("user_id"):
                        item["user_id"] = raw.get("user_id") or raw.get("principal_id") or ""
                    if not item.get("principal_id") and raw.get("principal_id"):
                        item["principal_id"] = raw["principal_id"]
            except Exception:
                pass
        return item

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            return self._hydrate_account(row)

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE lower(email)=lower(?)", (email,)
            ).fetchone()
            return self._hydrate_account(row)

    def _normalize_cpa(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept flat CPA or nested {email, tokens:{...}} harvest format."""
        if not isinstance(payload, dict):
            raise ValueError("account must be an object")
        p = dict(payload)
        tokens = p.get("tokens")
        if isinstance(tokens, dict):
            # nested harvest format — tokens fields win for auth, outer for meta
            merged = {**tokens, **{k: v for k, v in p.items() if k != "tokens"}}
            # prefer token fields from nested tokens when outer lacks them
            for k in (
                "access_token",
                "refresh_token",
                "id_token",
                "expires_at",
                "expires_in",
                "client_id",
                "scope",
                "auth_mode",
                "token_type",
                "sub",
            ):
                if not merged.get(k) and tokens.get(k):
                    merged[k] = tokens[k]
            if not merged.get("email") and tokens.get("email"):
                merged["email"] = tokens["email"]
            if not merged.get("access_token"):
                merged["access_token"] = tokens.get("access_token") or tokens.get("accessToken")
            if not merged.get("refresh_token"):
                merged["refresh_token"] = tokens.get("refresh_token") or tokens.get("refreshToken")
            p = merged
        return p

    def upsert_from_cpa(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._normalize_cpa(payload)
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise ValueError("email required")
        access = payload.get("access_token") or payload.get("accessToken")
        refresh = payload.get("refresh_token") or payload.get("refreshToken")
        if not access or not refresh:
            raise ValueError("access_token and refresh_token required")

        id_token = payload.get("id_token") or payload.get("idToken") or ""
        expires_in = int(payload.get("expires_in") or payload.get("expiresIn") or 21600)
        expires_at = (
            payload.get("expired")
            or payload.get("expiresAt")
            or payload.get("expires_at")
        )
        if not expires_at:
            expires_at = datetime.fromtimestamp(
                time.time() + expires_in, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        display = (
            payload.get("displayName")
            or payload.get("display_name")
            or email.split("@")[0]
        )
        # pull sub/principal from id_token JWT payload if missing
        sub = payload.get("sub") or payload.get("principal_id") or ""
        team_id = payload.get("team_id") or ""
        if (not sub or not team_id) and id_token and id_token.count(".") >= 2:
            try:
                import base64

                part = id_token.split(".")[1]
                pad = "=" * (-len(part) % 4)
                claims = json.loads(base64.urlsafe_b64decode(part + pad))
                if not sub:
                    sub = claims.get("sub") or claims.get("principal_id") or ""
                if not team_id:
                    team_id = claims.get("team_id") or ""
            except Exception:
                pass
        now = utc_now()
        existing = self.get_by_email(email)
        if existing:
            return None
        with self.tx() as conn:
            maxp = conn.execute(
                "SELECT COALESCE(MAX(priority),0) FROM accounts"
            ).fetchone()[0]
            acc_id = str(uuid.uuid4())
            # stash team_id into raw_json only (column may not exist)
            raw_store = dict(payload)
            if team_id and "team_id" not in raw_store:
                raw_store["team_id"] = team_id
            conn.execute(
                """
                INSERT INTO accounts(
                  id,email,display_name,status,enabled,priority,
                  access_token,refresh_token,id_token,token_type,
                  expires_in,expires_at,scope,sub,client_id,base_url,
                  raw_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    acc_id,
                    email,
                    display,
                    "active",
                    1,
                    int(maxp) + 1,
                    access,
                    refresh,
                    id_token,
                    payload.get("token_type") or "Bearer",
                    expires_in,
                    expires_at,
                    payload.get("scope")
                    or "openid profile email offline_access grok-cli:access api:access",
                    sub,
                    payload.get("client_id")
                    or "b1a00492-073a-47ea-816f-4c329264a828",
                    payload.get("base_url")
                    or "https://cli-chat-proxy.grok.com/v1",
                    json.dumps(raw_store, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        acc = self.get_account(acc_id)
        assert acc
        return acc

    def update_tokens(
        self,
        account_id: str,
        *,
        access_token: str,
        refresh_token: str,
        id_token: str | None = None,
        expires_in: int = 21600,
        scope: str | None = None,
    ) -> None:
        now = utc_now()
        expires_at = datetime.fromtimestamp(
            time.time() + expires_in, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id_token, scope FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not row:
                return
            conn.execute(
                """
                UPDATE accounts SET
                  access_token=?, refresh_token=?, id_token=?,
                  expires_in=?, expires_at=?, scope=COALESCE(?, scope),
                  last_refresh_at=?, last_refresh_error=NULL,
                  updated_at=?
                WHERE id=?
                """,
                (
                    access_token,
                    refresh_token,
                    id_token or row["id_token"] or "",
                    expires_in,
                    expires_at,
                    scope,
                    now,
                    now,
                    account_id,
                ),
            )

    def mark_status(
        self,
        account_id: str,
        status: str,
        *,
        error: str | None = None,
        refresh_error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.tx() as conn:
            acc = conn.execute(
                "SELECT email FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            if not acc:
                return
            conn.execute(
                """
                UPDATE accounts SET
                  status=?,
                  last_error=COALESCE(?, last_error),
                  last_error_at=CASE WHEN ? IS NOT NULL THEN ? ELSE last_error_at END,
                  last_refresh_error=COALESCE(?, last_refresh_error),
                  last_refresh_at=CASE WHEN ? IS NOT NULL THEN ? ELSE last_refresh_at END,
                  fail_count=fail_count + CASE WHEN ? IN ('dead','exhausted','error') THEN 1 ELSE 0 END,
                  updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    error,
                    error,
                    now,
                    refresh_error,
                    refresh_error,
                    now,
                    status,
                    now,
                    account_id,
                ),
            )
            conn.execute(
                "INSERT INTO events(account_id,email,kind,message,created_at) VALUES(?,?,?,?,?)",
                (
                    account_id,
                    acc["email"],
                    "status",
                    f"{status}: {error or refresh_error or ''}".strip(),
                    now,
                ),
            )

    def update_refresh_error(self, account_id: str, error: str) -> None:
        """Update last_refresh_error without changing status (e.g. invalid_grant but access still alive)."""
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                "UPDATE accounts SET last_refresh_error=?, updated_at=? WHERE id=?",
                (error, now, account_id),
            )

    def set_enabled(self, account_id: str, enabled: bool) -> None:
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                "UPDATE accounts SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, now, account_id),
            )

    def bulk_set_enabled(self, ids: list[str], enabled: bool) -> int:
        if not ids:
            return 0
        now = utc_now()
        with self.tx() as conn:
            q = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE accounts SET enabled=?, updated_at=? WHERE id IN ({q})",
                [1 if enabled else 0, now, *ids],
            )
            return cur.rowcount

    def delete_account(self, account_id: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def pick_active_round_robin(self) -> dict[str, Any] | None:
        with self.tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM accounts
                WHERE enabled=1 AND status='active'
                ORDER BY priority ASC, email ASC
                """
            ).fetchall()
            if not rows:
                return None
            idx_row = conn.execute(
                "SELECT value FROM meta WHERE key='rr_index'"
            ).fetchone()
            idx = int(idx_row["value"] if idx_row else 0)
            pick = rows[idx % len(rows)]
            conn.execute(
                "UPDATE meta SET value=? WHERE key='rr_index'",
                (str(idx + 1),),
            )
            now = utc_now()
            conn.execute(
                """
                UPDATE accounts SET
                  last_used_at=?,
                  consecutive_use_count=consecutive_use_count+1,
                  updated_at=?
                WHERE id=?
                """,
                (now, now, pick["id"]),
            )
            return dict(pick)

    def mark_success(self, account_id: str) -> None:
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                """
                UPDATE accounts SET
                  success_count=success_count+1,
                  last_error=NULL,
                  updated_at=?
                WHERE id=?
                """,
                (now, account_id),
            )

    def add_tokens_used(self, account_id: str, tokens: int) -> None:
        if tokens <= 0:
            return
        with self.tx() as conn:
            conn.execute(
                "UPDATE accounts SET tokens_used=tokens_used+? WHERE id=?",
                (tokens, account_id),
            )

    def log_request(
        self,
        *,
        account_id: str | None = None,
        email: str | None = None,
        model: str | None = None,
        path: str | None = None,
        status_code: int | None = None,
        duration_ms: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        total_tokens: int = 0,
        ok: bool = True,
    ) -> None:
        now = utc_now()
        inp = max(0, int(input_tokens or 0))
        out = max(0, int(output_tokens or 0))
        cached = max(0, int(cached_tokens or 0))
        total = max(0, int(total_tokens or 0))
        if total <= 0:
            total = inp + out
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO request_logs(
                  created_at, account_id, email, model, path, status_code,
                  duration_ms, input_tokens, output_tokens, cached_tokens,
                  total_tokens, ok
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    account_id,
                    email,
                    model,
                    path,
                    status_code,
                    duration_ms,
                    inp,
                    out,
                    cached,
                    total,
                    1 if ok else 0,
                ),
            )

    def usage_overview(self, day: str | None = None) -> dict[str, Any]:
        """day = YYYY-MM-DD (UTC). Default: today UTC."""
        if not day:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = f"{day}T00:00:00.000Z"
        end = f"{day}T23:59:59.999Z"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                  COUNT(*) AS total_requests,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                  COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM request_logs
                WHERE ok=1 AND created_at >= ? AND created_at <= ?
                """,
                (start, end),
            ).fetchone()
            recent = self._conn.execute(
                """
                SELECT id, created_at, account_id, email, model, path,
                       status_code, duration_ms, input_tokens, output_tokens,
                       cached_tokens, total_tokens, ok
                FROM request_logs
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (start, end),
            ).fetchall()
            hourly_rows = self._conn.execute(
                """
                SELECT CAST(substr(created_at, 12, 2) AS INTEGER) AS hour,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_logs
                WHERE ok=1 AND created_at >= ? AND created_at <= ?
                GROUP BY hour
                ORDER BY hour
                """,
                (start, end),
            ).fetchall()
        hour_map = {int(r["hour"]): int(r["tokens"] or 0) for r in hourly_rows if r["hour"] is not None}
        hourly = [
            {"hour": h, "label": f"{h:02d}:00", "tokens": hour_map.get(h, 0)}
            for h in range(24)
        ]
        return {
            "date": day,
            "total_requests": int(row["total_requests"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cached_tokens": int(row["cached_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "hourly": hourly,
            "recent": [dict(r) for r in recent],
        }

    def list_request_logs(
        self,
        *,
        day: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not day:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = f"{day}T00:00:00.000Z"
        end = f"{day}T23:59:59.999Z"
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        with self._lock:
            total = self._conn.execute(
                """
                SELECT COUNT(*) FROM request_logs
                WHERE created_at >= ? AND created_at <= ?
                """,
                (start, end),
            ).fetchone()[0]
            rows = self._conn.execute(
                """
                SELECT id, created_at, account_id, email, model, path,
                       status_code, duration_ms, input_tokens, output_tokens,
                       cached_tokens, total_tokens, ok
                FROM request_logs
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (start, end, limit, offset),
            ).fetchall()
        return {
            "date": day,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "requests": [dict(r) for r in rows],
        }

    def ids_by_filter(self, mode: str) -> list[str]:
        mapping = {
            "all": "SELECT id FROM accounts",
            "active": "SELECT id FROM accounts WHERE status='active'",
            "exhausted": "SELECT id FROM accounts WHERE status='exhausted'",
            "dead": "SELECT id FROM accounts WHERE status='dead'",
            "error": "SELECT id FROM accounts WHERE status='error'",
            "enabled": "SELECT id FROM accounts WHERE enabled=1",
            "disabled": "SELECT id FROM accounts WHERE enabled=0",
            "all_active": "SELECT id FROM accounts WHERE status='active'",
            "all_exhausted": "SELECT id FROM accounts WHERE status='exhausted'",
        }
        sql = mapping.get(mode)
        if not sql:
            return []
        with self._lock:
            return [r["id"] for r in self._conn.execute(sql).fetchall()]

    # ---- API keys ----
    def list_api_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, key, enabled, round_robin, created_at, last_used_at, note FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def create_api_key(self, name: str, note: str = "") -> dict[str, Any]:
        import secrets

        now = utc_now()
        kid = str(uuid.uuid4())
        key = "gcp_" + secrets.token_hex(24)
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO api_keys(id, name, key, enabled, round_robin, created_at, note)
                VALUES(?,?,?,?,?,?,?)
                """,
                (kid, name.strip() or "default", key, 1, 1, now, note or ""),
            )
        acc = self.get_api_key_by_id(kid)
        assert acc
        return acc

    def get_api_key_by_id(self, key_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, key, enabled, round_robin, created_at, last_used_at, note FROM api_keys WHERE id=?",
                (key_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_api_key(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, key, enabled, round_robin, created_at, last_used_at, note FROM api_keys WHERE key=? AND enabled=1",
                (key,),
            ).fetchone()
            return dict(row) if row else None

    def touch_api_key(self, key_id: str) -> None:
        now = utc_now()
        with self.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at=? WHERE id=?",
                (now, key_id),
            )

    def set_api_key_enabled(self, key_id: str, enabled: bool) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET enabled=? WHERE id=?",
                (1 if enabled else 0, key_id),
            )

    def set_api_key_round_robin(self, key_id: str, rr: bool) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET round_robin=? WHERE id=?",
                (1 if rr else 0, key_id),
            )

    def delete_api_key(self, key_id: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))

    def all_valid_api_keys(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM api_keys WHERE enabled=1"
            ).fetchall()
            return {r["key"] for r in rows}
