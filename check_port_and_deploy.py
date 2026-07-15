import json
import os
import secrets
import sys
import time
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SSH_HOST = os.environ.get("SSH_HOST", "192.168.1.230")
SSH_USER = os.environ.get("SSH_USER", "root")
SSH_PASS = os.environ.get("SSH_PASS", "")

if not SSH_PASS:
    print("FATAL: set SSH_PASS env var")
    sys.exit(1)

HOST = SSH_HOST
USER = SSH_USER
PASS = SSH_PASS
LOCAL = Path(r"F:\claudecode\grok-cli-proxy")
REMOTE = "/opt/grok-cli-proxy"
SERVICE = "grok-cli-proxy"
PORT = 8787

INCLUDE = [
    "app/__init__.py",
    "app/db.py",
    "app/main.py",
    "app/proxy.py",
    "app/refresh.py",
    "static/index.html",
    "static/app.js",
    "static/style.css",
    "config.example.json",
    "requirements.txt",
    "run.ps1",
]
SERVICE_CONTENT = f"""[Unit]
Description=Grok CLI Proxy
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/grok-cli-proxy
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/grok-cli-proxy/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port {PORT} --log-level warning
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def run(ssh, cmd, timeout=300):
    i, o, e = ssh.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    if err.strip():
        print("STDERR:", err[:500])
    return out


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print("connecting...")
    ssh.connect(HOST, username=USER, password=PASS, timeout=15, banner_timeout=15, auth_timeout=15)
    print("connected")

    # kill existing
    run(ssh, "ss -lntp | grep 8787 | grep -oP 'pid=\\K\\d+' | xargs -r kill -9 || true")

    # setup system
    run(ssh, "apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip curl", timeout=300)

    # prepare remote dir
    run(ssh, f"mkdir -p {REMOTE}/app {REMOTE}/static")

    sftp = ssh.open_sftp()
    local = Path(LOCAL)
    for rel in INCLUDE:
        lp = local / rel
        rp = f"{REMOTE}/{rel}"
        print(f"upload {rel} -> {rp}")
        sftp.put(str(lp), rp)
    sftp.close()

    # config
    cfg_cmd = f"""cd {REMOTE} && python3 -c "
import json, secrets
cfg = json.load(open('config.example.json'))
if not cfg.get('api_key') or cfg.get('api_key') in ('change-me',''):
    cfg['api_key'] = secrets.token_hex(16)
with open('config.json','w') as f: json.dump(cfg,f,indent=2)
print('api_key=', cfg['api_key'])
" """
    out = run(ssh, cfg_cmd)
    print(out)

    # venv
    run(ssh, f"cd {REMOTE} && python3 -m venv .venv", timeout=120)
    run(ssh, f"cd {REMOTE} && .venv/bin/pip install -q -r requirements.txt", timeout=420)

    run(ssh, f"cat > /etc/systemd/system/{SERVICE}.service << 'EOFSVC'\n{SERVICE_CONTENT}\nEOFSVC")
    run(ssh, "systemctl daemon-reload")
    run(ssh, f"systemctl enable --now {SERVICE}")

    for _ in range(12):
        time.sleep(5)
        out = run(ssh, f"curl -s -m 8 http://127.0.0.1:{PORT}/api/health; echo")
        if out.strip():
            print("HEALTH OK:", out[:200])
            break
        print("waiting...")

    cfg_out = run(ssh, f"python3 -c \"import json;c=json.load(open('{REMOTE}/config.json'));print('API_KEY='+str(c.get('api_key')))\"")
    print(cfg_out)
    run(ssh, f"systemctl --no-pager -l status {SERVICE} 2>&1 | head -20")
    ssh.close()
    print("deploy done")


if __name__ == "__main__":
    main()
