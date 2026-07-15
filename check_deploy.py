import os
import sys

import paramiko

SSH_HOST = os.environ.get("SSH_HOST", "192.168.1.230")
SSH_USER = os.environ.get("SSH_USER", "root")
SSH_PASS = os.environ.get("SSH_PASS", "")

if not SSH_PASS:
    print("FATAL: set SSH_PASS env var")
    sys.exit(1)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS)

cmds = [
    "ls -la /opt/grok-cli-proxy 2>&1 | head -30",
    "ls -la /opt/grok-cli-proxy/app 2>&1 | head -20",
    "ls -la /opt/grok-cli-proxy/static 2>&1 | head -20",
    "test -x /opt/grok-cli-proxy/.venv/bin/python && echo VENV_OK || echo NO_VENV",
    "systemctl is-active grok-cli-proxy 2>&1; systemctl is-enabled grok-cli-proxy 2>&1",
    "systemctl --no-pager -l status grok-cli-proxy 2>&1 | head -40",
    "ss -lntp | grep 8787 || netstat -lntp 2>/dev/null | grep 8787 || echo NO_PORT_8787",
    "curl -s -m 5 http://127.0.0.1:8787/api/health 2>&1 || echo HEALTH_FAIL",
    "test -f /opt/grok-cli-proxy/config.json && python3 -c 'import json;c=json.load(open(\"/opt/grok-cli-proxy/config.json\"));print(\"port\",c.get(\"port\"));print(\"db_path\",c.get(\"db_path\"))' || echo NO_CONFIG",
    "tail -n 30 /tmp/gcp-apt-install.log 2>/dev/null || true",
]
for c in cmds:
    print(f"\n> {c[:80]}")
    i, o, e = ssh.exec_command(c, timeout=30)
    print(o.read().decode("utf-8", "replace")[:3000])
    err = e.read().decode("utf-8", "replace")
    if err.strip():
        print("ERR:", err[:500])
ssh.close()
