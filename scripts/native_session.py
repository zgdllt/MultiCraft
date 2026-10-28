#!/usr/bin/env python3
"""Own the Vanilla 26.3 client and loopback-only isolated test server for M0."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import uuid

from prepare_minecraft import VERSION, FEATURES, rules_allow


def offline_uuid(username: str) -> str:
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + username).encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def expand_arguments(arguments: list, substitutions: dict[str, str]) -> list[str]:
    result = []
    for item in arguments:
        if isinstance(item, dict):
            if not rules_allow(item.get("rules"), features=FEATURES):
                continue
            item = item["value"]
        values = item if isinstance(item, list) else [item]
        for value in values:
            for key, replacement in substitutions.items():
                value = value.replace("${" + key + "}", replacement)
            if "${" in value:
                raise ValueError(f"unresolved launcher argument: {value}")
            result.append(value)
    return result


def proc_start_ticks(pid: int) -> str:
    text = Path(f"/proc/{pid}/stat").read_text()
    return text[text.rindex(")") + 2:].split()[19]


def process_owned(record: dict) -> bool:
    try:
        pid = int(record["pid"])
        if proc_start_ticks(pid) != record["startTicks"]:
            return False
        if Path(f"/proc/{pid}/cwd").resolve() != Path(record["cwd"]).resolve():
            return False
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        marker = ("-Dmulticraft.owner=" + record["owner"]).encode()
        return marker in command
    except (KeyError, OSError, ValueError, ProcessLookupError):
        return False


def read_record(root: Path, role: str) -> dict | None:
    path = root / f"{role}.process.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def launch(root: Path, role: str, java: str, arguments: list[str], cwd: Path,
           *, env: dict | None = None, stdin=None) -> dict:
    configuration = {"java": java, "arguments": arguments, "cwd": str(cwd.resolve()),
                     "environment": {key: (env or {}).get(key) for key in
                                     ["DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "PULSE_SERVER"]}}
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    existing = read_record(root, role)
    if existing and process_owned(existing):
        if existing.get("launchFingerprint") != fingerprint:
            if existing.get("launchFingerprint") or role != "server":
                raise ValueError(f"{role} is running with another launch configuration; stop it explicitly first")
            # The initially deployed server predates configuration fingerprints.
            # Permit only the identical command, with its separately checked port.
            actual = Path(f"/proc/{existing['pid']}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
            requested = [java, "-Dmulticraft.owner=" + existing["owner"], *arguments]
            if actual != requested:
                raise ValueError(f"{role} is running with another launch configuration; stop it explicitly first")
        return existing
    owner = uuid.uuid4().hex
    log_path = root / f"{role}.console.log"
    command = [java, "-Dmulticraft.owner=" + owner, *arguments]
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(command, cwd=cwd, env=env,
                                   stdin=subprocess.DEVNULL if stdin is None else stdin,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    record = {"role": role, "pid": process.pid, "owner": owner, "cwd": str(cwd.resolve()),
              "startTicks": proc_start_ticks(process.pid), "startedAt": time.time(),
              "log": str(log_path.resolve()), "launchFingerprint": fingerprint}
    record_path = root / f"{role}.process.json"
    temporary = root / f".{role}.{owner}.json"
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    os.replace(temporary, record_path)
    return record


def server_start(root: Path, java: str, port: int, *, accept_eula: bool = False) -> dict:
    if not accept_eula:
        raise ValueError("starting a demo server requires explicit --accept-eula")
    game = root / "server"
    game.mkdir(parents=True, exist_ok=True)
    existing = read_record(root, "server")
    if existing and process_owned(existing):
        property_lines = (game / "server.properties").read_text().splitlines()
        current_port = next((line.partition("=")[2] for line in property_lines
                             if line.startswith("server-port=")), None)
        if current_port != str(port):
            raise ValueError("server is running with another port; stop it explicitly first")
        return launch(root, "server", java, ["-Xms512M", "-Xmx2G", "-jar", str(root / "server.jar"), "nogui"], game)
    properties = {
        "server-ip": "127.0.0.1", "server-port": str(port), "online-mode": "false",
        "enforce-secure-profile": "false", "level-name": "m0-test-world",
        "white-list": "true", "enforce-whitelist": "true",
        "level-seed": "74261028", "gamemode": "survival", "difficulty": "easy",
        "max-players": "4", "view-distance": "6", "simulation-distance": "4",
        "spawn-protection": "0", "enable-command-block": "false", "enable-rcon": "false",
        "motd": "MultiCraft M0 isolated test server", "enable-status": "true",
        "allow-flight": "true", "sync-chunk-writes": "true", "max-tick-time": "60000",
    }
    (game / "server.properties").write_text("\n".join(f"{k}={v}" for k, v in properties.items()) + "\n")
    whitelist = game / "whitelist.json"
    entries = json.loads(whitelist.read_text()) if whitelist.exists() else []
    for name in ("M0Player", "M0Native"):
        if not any(entry.get("uuid") == offline_uuid(name) for entry in entries):
            entries.append({"uuid": offline_uuid(name), "name": name})
    whitelist.write_text(json.dumps(entries, indent=2) + "\n")
    (game / "eula.txt").write_text("# Isolated test server under the existing Minecraft run authorization.\neula=true\n")
    fifo = root / "server.stdin"
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    if not fifo.is_fifo():
        raise ValueError("server input path is not the owned FIFO")
    # RDWR opens without waiting for a peer, while a blocking descriptor keeps
    # Java's console reader alive until a later command arrives.
    fd = os.open(fifo, os.O_RDWR)
    try:
        return launch(root, "server", java, ["-Xms512M", "-Xmx2G", "-jar", str(root / "server.jar"), "nogui"], game, stdin=fd)
    finally:
        os.close(fd)


def update_options(game: Path, width: int, height: int) -> None:
    path = game / "options.txt"
    options = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator:
                options[key] = value
    # The private compositor forces its toplevel fullscreen. A windowed
    # startup request makes Vanilla reconfigure every frame, reinitializing
    # screens such as DeathScreen before their delayed buttons can activate.
    options.update({"fullscreen": "true", "overrideWidth": str(width), "overrideHeight": str(height),
                    "enableVsync": "false", "maxFps": "260", "rawMouseInput": "false",
                    "pauseOnLostFocus": "false",
                    "onboardAccessibility": "false", "narrator": "0", "tutorialStep": "none",
                    "skipMultiplayerWarning": "true", "hideServerAddress": "true",
                    "telemetryOptInExtra": "false",
                    "renderDistance": "6", "simulationDistance": "5", "guiScale": "2",
                    "soundCategory_master": "0.7", "soundCategory_music": "0.0",
                    "preferredGraphicsBackend": "vulkan"})
    path.write_text("\n".join(f"{key}:{value}" for key, value in options.items()) + "\n")


def client_command(root: Path, username: str, port: int, width: int, height: int) -> tuple[list[str], Path]:
    metadata = json.loads((root / "version.json").read_text())
    lock = json.loads((root / "lock.json").read_text())
    game = root / "client-game"
    game.mkdir(parents=True, exist_ok=True)
    classpath = [str(root / "libraries" / item) for item in lock["libraryPaths"]]
    classpath.append(str(root / "client.jar"))
    substitutions = {
        "auth_player_name": username, "version_name": VERSION, "game_directory": str(game),
        "assets_root": str(root / "assets"), "assets_index_name": lock["assetIndex"],
        "auth_uuid": offline_uuid(username), "auth_access_token": "0", "clientid": "",
        "auth_xuid": "", "version_type": "release", "resolution_width": str(width),
        "resolution_height": str(height), "quickPlayPath": str(game / "quickplay.json"),
        "quickPlayMultiplayer": f"127.0.0.1:{port}", "natives_directory": str(root / "natives"),
        "launcher_name": "MultiCraft-M0", "launcher_version": "0.1", "classpath": os.pathsep.join(classpath),
    }
    jvm = expand_arguments(metadata["arguments"]["jvm"], substitutions)
    game_args = expand_arguments(metadata["arguments"]["game"], substitutions)
    return ["-Xms512M", "-Xmx3G", *jvm, metadata["mainClass"], *game_args], game


def server_command(root: Path, command: str) -> None:
    record = read_record(root, "server")
    if not record or not process_owned(record):
        raise ValueError("owned isolated server is not running")
    payload = (command + "\n").encode()
    if not command or "\n" in command or "\r" in command or len(payload) > 4096:
        raise ValueError("supply one bounded Minecraft console command")
    fd = os.open(root / "server.stdin", os.O_WRONLY | os.O_NONBLOCK)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError("incomplete isolated-server console write")
    finally:
        os.close(fd)


def stop(root: Path, role: str, expected_owner: str | None = None) -> dict:
    record = read_record(root, role)
    if expected_owner is not None and (not record or record.get("owner") != expected_owner):
        return {"role": role, "stopped": False, "reason": "owner generation changed"}
    if not record or not process_owned(record):
        return {"role": role, "stopped": False, "reason": "no owned live process"}
    if role == "server":
        server_command(root, "stop")
    else:
        os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and process_owned(record):
        time.sleep(0.1)
    if process_owned(record):
        os.kill(record["pid"], signal.SIGTERM)
        time.sleep(1)
    if process_owned(record):
        os.kill(record["pid"], signal.SIGKILL)
    return {"role": role, "pid": record["pid"], "stopped": True}


def client_environment(display: str | None, inherited: dict | None = None) -> dict:
    env = dict(os.environ if inherited is None else inherited)
    if display:
        # Accept only a local, explicitly selected display. Do not let :1.0,
        # unix:1 or an inherited desktop bypass the protected :1 check.
        match = re.fullmatch(r":(\d+)(?:\.\d+)?", display)
        if not match or int(match.group(1)) in {0, 1}:
            raise ValueError("choose an explicit isolated local X11 display, excluding :0/:1")
        env["DISPLAY"] = display
        return env
    env.pop("DISPLAY", None)
    wayland = env.get("WAYLAND_DISPLAY")
    runtime = env.get("XDG_RUNTIME_DIR")
    if not wayland or not runtime:
        raise ValueError("an isolated X11 display or an explicit Wayland environment is required")
    socket_path = Path(wayland) if Path(wayland).is_absolute() else Path(runtime) / wayland
    if not socket_path.is_socket():
        raise ValueError("the supplied private Wayland socket does not exist")
    return env


def execute(args, root: Path):
    if args.action in {"server", "client"}:
        java_version = subprocess.run([args.java, "-version"], capture_output=True, text=True,
                                      timeout=10, check=True)
        if not re.search(r'version "25[.\"]', java_version.stderr + java_version.stdout):
            raise ValueError("this locked Minecraft release requires the selected Java 25 runtime")
    if args.action == "server":
        return server_start(root, args.java, args.port, accept_eula=args.accept_eula)
    if args.action == "client":
        arguments, game = client_command(root, args.username, args.port, args.width, args.height)
        env = client_environment(args.display)
        existing = read_record(root, "client")
        if not existing or not process_owned(existing):
            update_options(game, args.width, args.height)
        return launch(root, "client", args.java, arguments, game, env=env)
    if args.action == "command":
        server_command(root, args.command or "")
        return {"submitted": True}
    if args.action == "stop":
        roles = ["client", "server"] if args.role == "all" else [args.role]
        return [stop(root, role, args.expected_owner) for role in roles]
    result = {}
    for role in ["client", "server"]:
        record = read_record(root, role)
        result[role] = {"running": bool(record and process_owned(record)),
                        "pid": record["pid"] if record else None,
                        "log": str(root / f"{role}.console.log")}
    with socket.socket() as sock:
        sock.settimeout(0.2)
        result["server"]["acceptingTcp"] = sock.connect_ex(("127.0.0.1", args.port)) == 0
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["server", "client", "status", "stop", "command"])
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime/minecraft"))
    parser.add_argument("--java", default=str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.environ.get("JAVA_HOME") else "java")
    parser.add_argument("--display", help="isolated X11 display; omit for inherited headless Wayland")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--port", type=int, default=25585)
    parser.add_argument("--username", default="M0Player")
    parser.add_argument("--role", choices=["client", "server", "all"], default="all")
    parser.add_argument("--expected-owner", help="stop only this launch generation, checked under the lifecycle lock")
    parser.add_argument("--command")
    parser.add_argument("--accept-eula", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port in {25565, 25575}:
        parser.error("use an isolated nonprivileged port, never the existing server ports")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", args.username):
        parser.error("invalid Minecraft username")
    if not (320 <= args.width <= 7680 and 180 <= args.height <= 4320) or args.width % 2 or args.height % 2:
        parser.error("invalid framebuffer dimensions")
    root = args.runtime_dir.resolve() / VERSION
    if not (root / "lock.json").is_file():
        parser.error("prepare_minecraft.py must complete first")
    with (root / ".lifecycle.lock").open("a") as lifecycle:
        fcntl.flock(lifecycle.fileno(), fcntl.LOCK_EX)
        result = execute(args, root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
