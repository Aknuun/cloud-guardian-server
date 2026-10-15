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

STATS_CMD = ("echo UPTIME_S:$(cat /proc/uptime | cut -d' ' -f1); "
             "echo LOAD1:$(cat /proc/loadavg | cut -d' ' -f1); "
             "echo CORES:$(nproc); "
             "echo MEM_USED_MB:$(free -m | awk '/Mem:/{print $3}'); "
             "echo MEM_TOTAL_MB:$(free -m | awk '/Mem:/{print $2}'); "
             "echo DISK_USED:$(df / | awk 'NR==2{print $3}'); "
             "echo DISK_TOTAL:$(df / | awk 'NR==2{print $2}'); "
             "echo DISK_PCT:$(df / | awk 'NR==2{print $5}')")


def stats(host: str, user: str, secret: str, port: int) -> dict:
    """Structured stats or {'error': msg}."""
    r = run_cmd(host, user or "root", secret or "", port or 22, STATS_CMD)
    if not r["ok"]:
        return {"error": r["err"] or "ssh failed"}
    vals = {}
    for ln in r["out"].splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            vals[k.strip()] = v.strip()
    try:
        cores = max(1, int(float(vals.get("CORES", 1))))
        load = float(vals.get("LOAD1", 0))
        mem_used = int(float(vals.get("MEM_USED_MB", 0)))
        mem_total = int(float(vals.get("MEM_TOTAL_MB", 0)))
        disk_used_kb = int(float(vals.get("DISK_USED", 0)))
        disk_total_kb = int(float(vals.get("DISK_TOTAL", 0)))
    except (ValueError, TypeError):
        return {"error": "parse"}
    gb = lambda kb: round(kb / 1024 / 1024, 1)
    return {
        "uptimeS": int(float(vals.get("UPTIME_S", 0))),
        "cpu": {"pct": round(load / cores * 100),
                "cores": cores},
        "mem": {"usedMb": mem_used, "totalMb": mem_total,
                "pct": round(mem_used / mem_total * 100) if mem_total else 0},
        "disk": {"used": f'{gb(disk_used_kb)} GB',
                 "total": f'{gb(disk_total_kb)} GB',
                 "pct": vals.get("DISK_PCT", "—")},
    }
