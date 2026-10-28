#!/usr/bin/env python3
"""Own the isolated M1 server and one two-realm Java host; never touch M0."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from native_session import (client_environment, expand_arguments, launch, offline_uuid,
                            process_owned, read_record, server_command, stop, update_options)

VERSION = "26.3"
NAMES = ("M1PlayerA", "M1PlayerB")


def atomic_json(path: Path, value: dict | list, *, private: bool = False) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def validate_port(port: int) -> int:
    if not 1024 <= port <= 65535 or port in {25565, 25575, 25585, 18080, 18081, 8795}:
        raise ValueError("choose a dedicated M1 port, excluding production, M0 and observation ports")
    return port


def pin_graphics_options(game: Path) -> None:
    # Vanilla 26.3 option strings are JSON encoded; an unquoted value silently
    # falls back to "default", which may select a software OpenGL device.
    path = game / "options.txt"
    lines = [line for line in path.read_text().splitlines()
             if not line.startswith(("preferredGraphicsBackend:", "inactivityFpsLimit:", "maxFps:"))]
    lines.append('preferredGraphicsBackend:"vulkan"')
    # Browser input owns activity; an unfocused physical seat is not an idle
    # cloud player. Retain Vanilla's minimized-window limit, disable AFK limits.
    lines.append('inactivityFpsLimit:"minimized"')
    lines.append("maxFps:60")  # Matches the fixed M1 test stream; M0 stays configurable.
    path.write_text("\n".join(lines) + "\n")


def game_arguments(cache: Path, game: Path, name: str, port: int,
                   width: int, height: int) -> list[str]:
    metadata = json.loads((cache / "version.json").read_text())
    lock = json.loads((cache / "lock.json").read_text())
    substitutions = {
        "auth_player_name": name, "version_name": VERSION, "game_directory": str(game),
        "assets_root": str(cache / "assets"), "assets_index_name": lock["assetIndex"],
        "auth_uuid": offline_uuid(name), "auth_access_token": "0", "clientid": "",
        "auth_xuid": "", "version_type": "release", "resolution_width": str(width),
        "resolution_height": str(height), "quickPlayPath": str(game / "quickplay.json"),
        "quickPlayMultiplayer": f"127.0.0.1:{port}",
    }
    return [*expand_arguments(metadata["arguments"]["game"], substitutions),
            "--graphicsBackend", "vulkan", "--fullscreen"]


def prepare(root: Path, cache: Path, port: int, control_port: int,
            width: int, height: int) -> dict:
    # Running instances retain their exact launch generation and files.
    for role in ("host", "server"):
        record = read_record(root, role)
        if record and process_owned(record):
            raise ValueError("stop the owned M1 host and server before preparing a new configuration")
    if not (cache / "client.jar").is_file() or not (cache / "server.jar").is_file():
        raise ValueError("the verified existing Minecraft cache is required")
    token_path = root / "control.token"
    if not token_path.exists():
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as token_file:
            token_file.write(secrets.token_urlsafe(48) + "\n")
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_mode & 0o077:
        raise ValueError("M1 control token must be an owner-only regular file")
    lock = json.loads((cache / "lock.json").read_text())
    sessions = []
    for session_id, name in zip(("A", "B"), NAMES):
        game = root / "sessions" / session_id
        game.mkdir(parents=True, exist_ok=True)
        update_options(game, width, height)
        pin_graphics_options(game)
        sessions.append({"id": session_id, "displayIndex": 0 if session_id == "A" else 1,
                         "args": game_arguments(cache, game, name, port, width, height),
                         "serverAddress": f"127.0.0.1:{port}"})
    configuration = {"clientJar": str(cache / "client.jar"),
                     "libraries": [str(cache / "libraries" / path) for path in lock["libraryPaths"]],
                     "audioMode": "loopback-pcm", "tokenFile": str(token_path),
                     "stateFile": str(root / "host.state.json"), "controlPort": control_port,
                     "maxCycles": 0, "requiredGraphicsBackend": "Vulkan",
                     "requiredGraphicsVendor": "NVIDIA", "teardownMode": "defer-native",
                     "sdlQuitPolicy": "process-exit", "sessions": sessions}
    atomic_json(root / "host.json", configuration)
    game = root / "server"
    game.mkdir(exist_ok=True)
    properties = {
        "server-ip": "127.0.0.1", "server-port": str(port), "online-mode": "false",
        "enforce-secure-profile": "false", "level-name": "m1-test-world",
        "white-list": "true", "enforce-whitelist": "true", "level-seed": "74261029",
        "gamemode": "survival", "difficulty": "easy", "max-players": "4",
        "view-distance": "5", "simulation-distance": "4", "spawn-protection": "0",
        "enable-command-block": "false", "enable-rcon": "false", "allow-flight": "true",
        "motd": "MultiCraft M1 isolated two-session test", "max-tick-time": "60000",
    }
    (game / "server.properties").write_text("".join(f"{key}={value}\n" for key, value in properties.items()))
    atomic_json(game / "whitelist.json", [{"uuid": offline_uuid(name), "name": name} for name in NAMES])
    (game / "eula.txt").write_text("# Read the Minecraft EULA and use --accept-eula before running this optional demo.\neula=false\n")
    return {"prepared": True, "root": str(root), "serverPort": port,
            "controlPort": control_port, "players": list(NAMES), "audioMode": configuration["audioMode"]}


def server_start(root: Path, cache: Path, java: str, *, accept_eula: bool = False) -> dict:
    if not accept_eula:
        raise ValueError("starting a demo server requires explicit --accept-eula")
    (root / "server" / "eula.txt").write_text("eula=true\n")
    if not (root / "host.json").is_file():
        raise ValueError("prepare M1 first")
    fifo = root / "server.stdin"
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    if not fifo.is_fifo() or fifo.is_symlink():
        raise ValueError("M1 input must be the owned FIFO")
    fd = os.open(fifo, os.O_RDWR)
    try:
        return launch(root, "server", java, ["-Xms512M", "-Xmx2G", "-jar", str(cache / "server.jar"), "nogui"],
                      root / "server", stdin=fd)
    finally:
        os.close(fd)


def host_environment(root: Path, values: dict, inherited: dict | None = None) -> dict:
    """Validate both real socket endpoints before allowing a native connection."""
    values = values.get("environment", values)
    allowed = {"XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "PULSE_SERVER", "PULSE_SINK", "PULSE_SOURCE",
               "DBUS_SESSION_BUS_ADDRESS", "XCURSOR_THEME", "XCURSOR_SIZE", "SDL_VIDEODRIVER",
               "ALSOFT_DRIVERS", "PIPEWIRE_REMOTE", "XDG_SESSION_TYPE"}
    if not isinstance(values, dict) or any(key not in allowed or not isinstance(value, str)
                                           for key, value in values.items()):
        raise ValueError("media environment must contain only the explicit private runtime variables")
    inherited = dict(os.environ if inherited is None else inherited)
    for key in list(inherited):
        if key.startswith(("SELKIES_", "PIXELFLUX_", "PCMFLUX_")) or key in {
            "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XDG_RUNTIME_DIR",
            "PULSE_SERVER", "PULSE_RUNTIME_PATH", "PULSE_SOURCE", "PULSE_SINK",
            "PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
            "XDG_CURRENT_DESKTOP", "ALSOFT_CONF", "ALSOFT_DRIVERS",
        }:
            inherited.pop(key, None)
    private_xdg = root.resolve() / "media" / "xdg"
    if Path(values.get("XDG_RUNTIME_DIR", "")).resolve() != private_xdg:
        raise ValueError("M1 host must use its own media xdg runtime")
    wayland = values.get("WAYLAND_DISPLAY", "")
    if not re.fullmatch(r"wayland-[A-Za-z0-9_-]+", wayland):
        raise ValueError("M1 Wayland display must be a private socket basename")
    wayland_socket = private_xdg / wayland
    if wayland_socket.resolve().parent != private_xdg or not wayland_socket.is_socket():
        raise ValueError("M1 Wayland socket must belong to the private runtime")
    pulse_socket = private_xdg / "pulse" / "native"
    if (values.get("PULSE_SERVER") != "unix:" + str(pulse_socket) or
            pulse_socket.resolve().parent != private_xdg / "pulse" or not pulse_socket.is_socket()):
        raise ValueError("M1 Pulse socket must belong to the private runtime")
    env = client_environment(None, {**inherited, **values})
    env["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"
    return env


def host_start(root: Path, java: str, host_jar: Path, environment: Path) -> dict:
    configuration = json.loads((root / "host.json").read_text())
    if not host_jar.is_file():
        raise ValueError("build the M1 host before starting it")
    env = host_environment(root, json.loads(environment.read_text()))
    existing = read_record(root, "host")
    if not existing or not process_owned(existing):
        for session in configuration["sessions"]:
            arguments = session["args"]
            game = Path(arguments[arguments.index("--gameDir") + 1]).resolve()
            if not game.is_relative_to(root / "sessions"):
                raise ValueError("M1 writable game directories must remain inside M1 sessions")
            pin_graphics_options(game)
            if "--graphicsBackend" not in arguments:
                arguments.extend(["--graphicsBackend", "vulkan"])
            elif arguments[arguments.index("--graphicsBackend") + 1].lower() != "vulkan":
                raise ValueError("M1's locked graphics profile requires the official Vulkan backend")
            if "--fullscreen" not in arguments:
                arguments.append("--fullscreen")
        atomic_json(root / "host.json", configuration)
    fingerprint = hashlib.sha256(json.dumps({"configuration": configuration,
        "hostJarSha256": hashlib.sha256(host_jar.read_bytes()).hexdigest(),
        "environment": {key: env.get(key) for key in (
            "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "PULSE_SERVER", "SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS")}},
        sort_keys=True).encode()).hexdigest()
    classpath = os.pathsep.join([str(host_jar), configuration["clientJar"], *configuration["libraries"]])
    arguments = ["-Xms1G", "-Xmx6G", "-XX:StackShadowPages=32", "--enable-native-access=ALL-UNNAMED",
                 "-Dmulticraft.configurationSha256=" + fingerprint,
                 "--add-exports", "java.base/jdk.internal.misc=ALL-UNNAMED",
                 "-Dorg.lwjgl.system.SharedLibraryExtractPath=" + str(root / "natives" / "lwjgl"),
                 "-Dio.netty.native.workdir=" + str(root / "natives" / "netty"),
                 "-Djna.tmpdir=" + str(root / "natives" / "jna"),
                 "-cp", classpath, "multicraft.host.Host", str(root / "host.json")]
    return launch(root, "host", java, arguments, root, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "server", "host", "status", "stop", "command"))
    parser.add_argument("--root", type=Path, default=Path(".runtime/m1"))
    parser.add_argument("--cache", type=Path, default=Path(".runtime/minecraft/26.3"))
    parser.add_argument("--java", default=str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.environ.get("JAVA_HOME") else "java")
    parser.add_argument("--port", type=int, default=25586)
    parser.add_argument("--control-port", type=int, default=18091)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--host-jar", type=Path)
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--role", choices=("host", "server", "all"), default="all")
    parser.add_argument("--expected-owner")
    parser.add_argument("--command")
    parser.add_argument("--accept-eula", action="store_true")
    parser.add_argument("--wait", action="store_true", help="new host only: record its real OS exit status")
    args = parser.parse_args()
    if args.wait and args.action != "host":
        parser.error("--wait is only available when launching a new host")
    root, cache = args.root.resolve(), args.cache.resolve()
    # A new test root may never be an alias of a live M0/production directory.
    if root != Path.cwd().resolve() / ".runtime" / "m1" or root.is_symlink():
        parser.error("this M1 launcher only owns this project's .runtime/m1")
    root.mkdir(parents=True, exist_ok=True)
    validate_port(args.port)
    validate_port(args.control_port)
    if args.port == args.control_port:
        parser.error("server and host control ports must differ")
    if not (320 <= args.width <= 3840 and 180 <= args.height <= 2160) or args.width % 2 or args.height % 2:
        parser.error("invalid M1 framebuffer dimensions")
    with (root / ".lifecycle.lock").open("a") as lifecycle:
        fcntl.flock(lifecycle.fileno(), fcntl.LOCK_EX)
        if args.action in {"server", "host"}:
            result = subprocess.run([args.java, "-version"], capture_output=True, text=True, check=True, timeout=10)
            if not re.search(r'version "25[.\"]', result.stdout + result.stderr):
                parser.error("the pinned native client requires Java 25")
        if args.action == "prepare":
            result = prepare(root, cache, args.port, args.control_port, args.width, args.height)
        elif args.action == "server":
            result = server_start(root, cache, args.java, accept_eula=args.accept_eula)
        elif args.action == "host":
            existing = read_record(root, "host")
            if args.wait and existing and process_owned(existing):
                parser.error("--wait requires a new child, not an already-running host")
            result = host_start(root, args.java, (args.host_jar or root / "build" / "multicraft-host.jar").resolve(),
                                (args.environment or root / "media" / "environment.json").resolve())
        elif args.action == "command":
            server_command(root, args.command or "")
            result = {"submitted": True}
        elif args.action == "stop":
            roles = ("host", "server") if args.role == "all" else (args.role,)
            result = [stop(root, role, args.expected_owner) for role in roles]
        else:
            result = {}
            for role in ("host", "server"):
                record = read_record(root, role)
                result[role] = {"running": bool(record and process_owned(record)),
                                "pid": record["pid"] if record else None,
                                "log": str(root / f"{role}.console.log")}
        print(json.dumps(result, indent=2), flush=True)
    if args.wait:
        # The launching process remains the child's supervisor. Do not hold the
        # lifecycle lock while waiting: owned stop/control must remain usable.
        pid, status = os.waitpid(result["pid"], 0)
        receipt = {"pid": pid, "owner": result["owner"], "finishedAt": time.time(),
                   "exitCode": os.waitstatus_to_exitcode(status)}
        atomic_json(root / ("exit-" + result["owner"] + ".json"), receipt)
        print(json.dumps(receipt, indent=2), flush=True)


if __name__ == "__main__":
    main()
