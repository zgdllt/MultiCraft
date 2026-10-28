#!/usr/bin/env python3
"""Start/stop only this checkout's M0 processes; never manage the live server."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def start_ticks(pid: int) -> str:
    data = Path(f"/proc/{pid}/stat").read_text()
    return data[data.rindex(")") + 2:].split()[19]


def owned(record: dict, root: Path, role: str) -> bool:
    try:
        pid = int(record["pid"])
        if record["startTicks"] != start_ticks(pid):
            return False
        if Path(f"/proc/{pid}/cwd").resolve() != root.resolve():
            return False
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        expected = b"multicraft.media_runtime" if role == "media" else b"multicraft.gateway"
        return any(command[i] == b"-m" and command[i + 1] == expected
                   for i in range(len(command) - 1))
    except (OSError, ValueError, KeyError):
        return False


def record_for(root: Path, role: str) -> tuple[Path, dict | None]:
    path = root / ".runtime" / f"{role}-process.json"
    if not path.exists():
        return path, None
    record = json.loads(path.read_text())
    # Early manual bring-up wrote only the wrapper PID. The media state names
    # its actual child; accept it only after verifying the exact module/cwd.
    if role == "media":
        state = root / ".runtime/media/state.json"
        if state.exists() and json.loads(state.read_text()).get("backendReady"):
            child = json.loads(state.read_text()).get("pid")
            if child:
                try:
                    candidate = {**record, "pid": child, "startTicks": start_ticks(child)}
                    if owned(candidate, root, role):
                        return path, candidate
                except OSError:
                    pass
    return path, record


def stop(root: Path, role: str) -> dict:
    path, record = record_for(root, role)
    if not record or not owned(record, root, role):
        return {"role": role, "stopped": False, "reason": "no owned live process"}
    os.kill(record["pid"], signal.SIGTERM)
    grace_seconds = 75 if role == "media" else 15
    for _ in range(grace_seconds * 10):
        if not owned(record, root, role):
            path.unlink(missing_ok=True)
            return {"role": role, "stopped": True}
        time.sleep(0.1)
    # Escalate only the exact PID that still matches this checkout, role and
    # start time. The Minecraft server and unrelated processes are excluded.
    if owned(record, root, role):
        os.kill(record["pid"], signal.SIGKILL)
    for _ in range(50):
        if not owned(record, root, role):
            path.unlink(missing_ok=True)
            return {"role": role, "stopped": True, "forcedOwnedPid": True}
        time.sleep(0.1)
    raise RuntimeError(f"{role} did not exit; no other processes were signaled")


def arguments(root: Path, cfg: dict, role: str) -> list[str]:
    def path(key):
        return str((root / cfg[key]).resolve())
    command = [str(root / ".venv/bin/python"), "-m", "multicraft." + ("media_runtime" if role == "media" else "gateway")]
    command += ["--master-token-file", path("masterToken")]
    command += ["--policy-file", str((root / cfg.get("policyFile", ".runtime/stream-policy.json")).resolve())]
    if role == "media":
        return command + ["--port", str(cfg["backendPort"]), "--render-node", cfg["renderNode"],
                          "--java", cfg["java"], "--minecraft-port", str(cfg["minecraftPort"]),
                          "--minecraft-username", cfg["minecraftUsername"], "--start-client"]
    command += ["--backend", f"http://127.0.0.1:{cfg['backendPort']}", "--accounts", path("accounts"),
                "--origin", cfg["origin"], "--host", cfg["host"], "--port", str(cfg["port"]),
                "--minecraft-username", cfg["minecraftUsername"],
                "--runtime-state", str(root / ".runtime/media/state.json"),
                "--stream-request", str(root / ".runtime/media/request.json"),
                "--stream-effective", str(root / ".runtime/media/effective.json")]
    if cfg["origin"].startswith("https://"):
        command += ["--tls-cert", path("tlsCert"), "--tls-key", path("tlsKey")]
    elif cfg["host"] in ("127.0.0.1", "localhost", "::1"):
        command += ["--insecure-loopback"]
    else:
        raise ValueError("public gateway requires HTTPS")
    if cfg.get("testEgressFile"):
        command += ["--test-egress-file", path("testEgressFile")]
    return command


def start(root: Path, cfg: dict, role: str) -> dict:
    path, record = record_for(root, role)
    if record and owned(record, root, role):
        return {"role": role, "pid": record["pid"], "alreadyRunning": True}
    command = arguments(root, cfg, role)
    with (root / "logs" / f"{role}.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
    record = {"pid": process.pid, "startTicks": start_ticks(process.pid), "cwd": str(root),
              "module": command[2], "startedAt": time.time()}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(temporary, path)
    return {"role": role, "pid": process.pid, "started": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    parser.add_argument("--role", choices=("all", "media", "gateway"), default="all")
    parser.add_argument("--config", type=Path, default=ROOT / "config/m0.example.json")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("run the runtime manager on the Linux test host")
    os.chdir(ROOT)
    os.umask(0o077)
    cfg = json.loads(args.config.read_text())
    roles = ["media", "gateway"] if args.role == "all" else [args.role]
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / ".runtime").mkdir(exist_ok=True)
    results = []
    with (ROOT / ".runtime/service.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action in ("stop", "restart"):
            results += [stop(ROOT, role) for role in reversed(roles)]
        if args.action in ("start", "restart"):
            results += [start(ROOT, cfg, role) for role in roles]
        if args.action == "status":
            for role in roles:
                _, record = record_for(ROOT, role)
                results.append({"role": role, "running": bool(record and owned(record, ROOT, role)),
                                "pid": record.get("pid") if record else None})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
