"""SSH operations via paramiko. Short timeouts; never raises (returns err)."""
from __future__ import annotations


def run_cmd(host: str, user: str, secret: str, port: int, command: str,
            timeout: int = 20) -> dict:
    import paramiko
    out = {"ok": False, "out": "", "err": ""}
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        conn = {"hostname": host, "username": user or "root",
                "port": int(port or 22), "timeout": timeout,
                "banner_timeout": timeout, "auth_timeout": timeout}
        if secret and secret.startswith("-----BEGIN"):
            import io
            conn["pkey"] = paramiko.RSAKey.from_private_key(io.StringIO(secret))
        else:
            conn["password"] = secret
        cli.connect(**conn)
        _, stdout, stderr = cli.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out["out"] = stdout.read().decode("utf-8", "replace").strip()[:2000]
        out["err"] = stderr.read().decode("utf-8", "replace").strip()[:300]
        out["ok"] = rc == 0
    except Exception as e:
        out["err"] = str(e)[:300]
    finally:
        try:
            cli.close()
        except Exception:
            pass
    return out


INFO_CMD = ("echo UNAME:$(uname -a); echo UPTIME:$(uptime -p); "
            "echo LOAD:$(cat /proc/loadavg); "
            "echo MEM:$(free -m | awk '/Mem:/{print $3\"/\"$2\"MB\"}'); "
            "echo DISK:$(df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}')")
DISK_CMD = "df -h | head -12"
