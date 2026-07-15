import json, sqlite3, asyncio, httpx

conn = sqlite3.connect("F:/claudecode/grok-cli-proxy/data/accounts.db")
conn.row_factory = sqlite3.Row
acc = conn.execute("SELECT * FROM accounts WHERE enabled=1 AND status='active' LIMIT 1").fetchone()
if not acc:
    print("no active account")
    exit()

headers = {
    "Authorization": f"Bearer {acc['access_token']}",
    "Content-Type": "application/json",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
    "x-xai-token-auth": "xai-grok-cli",
    "x-grok-client-identifier": "grok-shell",
    "x-grok-client-version": "0.2.93",
}

async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://cli-chat-proxy.grok.com/v1/chat/completions",
            headers=headers,
            json={"model": "grok-4.5", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
        )
        js = resp.json()
        print("keys:", list(js.keys()))
        if "usage" in js:
            print("usage:", json.dumps(js["usage"], indent=2))
        else:
            print("NO usage field in response!")
            # show first 500 chars
            print("sample:", json.dumps(js)[:500])

asyncio.run(test())
