import os
import sys

import paramiko

assert os.environ.get("SSH_PASS"), "set SSH_PASS env var"
assert os.environ.get("SSH_HOST"), "set SSH_HOST env var"

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
print("connecting...")
ssh.connect(
    os.environ["SSH_HOST"],
    username=os.environ.get("SSH_USER", "root"),
    password=os.environ["SSH_PASS"],
    timeout=15,
    banner_timeout=15,
    auth_timeout=15,
)
print("connected")
i, o, e = ssh.exec_command(
    "echo OK; hostname; whoami; date; python3 --version; ss -lntp | grep 8787 || echo PORT_FREE; ls -la /opt 2>&1 | head",
    timeout=20,
)
print(o.read().decode())
err = e.read().decode()
if err.strip():
    print("ERR:", err[:500])
ssh.close()
print("done")
