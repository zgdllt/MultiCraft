"""Validated operator-owned native pool configuration; no service side effects."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

SLOT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
NAME = re.compile(r"[A-Za-z0-9_]{1,16}\Z")
STREAM_DEFAULT = {"width": 1280, "height": 720, "fps": 60, "bitrateKbps": 6000, "adaptive": True}
STREAM_BOUNDS = {"width": (320, 3840), "height": (180, 2160), "fps": (10, 1000), "bitrateKbps": (500, 50000)}


def offline_uuid(name: str) -> str:
    data = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode()).digest())
    data[6], data[8] = (data[6] & 15) | 48, (data[8] & 63) | 128
    return str(uuid.UUID(bytes=bytes(data)))


def stream_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(STREAM_DEFAULT):
        raise ValueError("stream requires exactly width,height,fps,bitrateKbps,adaptive")
    for key, (low, high) in STREAM_BOUNDS.items():
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f"{key} must be an integer {low}..{high}")
    if value["width"] % 2 or value["height"] % 2 or type(value["adaptive"]) is not bool:
        raise ValueError("even video dimensions and boolean adaptive required")
    return dict(value)


def server_address(address: str) -> str:
    if not isinstance(address, str) or len(address) > 300 or any(c.isspace() for c in address):
        raise ValueError("explicit serverAddress hostname:port required")
    parsed = urlsplit("minecraft://" + address)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (not parsed.hostname or port is None or not 1 <= port <= 65535 or parsed.username is not None
            or parsed.path or parsed.query or parsed.fragment or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+:[0-9]+", address)):
        raise ValueError("serverAddress must be hostname:port or [IPv6]:port")
    return address


def default_config(count: int = 4) -> dict:
    if type(count) is not int or not 1 <= count <= 16:
        raise ValueError("pool count must be 1..16")
    return {"schema": 1, "runtimeDirectory": ".runtime/pool", "serverAddress": "127.0.0.1:25565",
            "controlPort": 18091, "render": {"width": 1280, "height": 720, "node": "/dev/dri/renderD128"},
            "sessions": [{"id": f"slot{i + 1}", "username": f"MCPlayer{i + 1}", "displayIndex": i,
                          "stream": dict(STREAM_DEFAULT)} for i in range(count)]}


def load_config(path: Path) -> dict:
    path = path.resolve()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("MultiCraft config schema 1 required")
    result = dict(value)
    project = path.parent
    raw_runtime = Path(value.get("runtimeDirectory", ".runtime/pool"))
    runtime = (project / raw_runtime).resolve()
    owned = project / ".runtime"
    if runtime == owned or not runtime.is_relative_to(owned) or any(p.is_symlink() for p in [(project / raw_runtime), *(project / raw_runtime).parents] if p == project or p.is_relative_to(project)):
        raise ValueError("runtimeDirectory must be an owned nonsymlink directory under this config's .runtime")
    result.update(runtimeDirectory=str(runtime), projectDirectory=str(project), configPath=str(path))
    cache = Path(value.get("minecraftCache", ".runtime/minecraft/26.3"))
    result["minecraftCache"] = str((project / cache).resolve())
    result["serverAddress"] = server_address(value.get("serverAddress"))
    port = value.get("controlPort", 18091)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("controlPort must be a nonprivileged TCP port")
    result["controlPort"] = port
    render = {"width": 1280, "height": 720, "node": "/dev/dri/renderD128", **value.get("render", {})}
    stream_config({**STREAM_DEFAULT, "width": render["width"], "height": render["height"]})
    if not isinstance(render["node"], str) or not render["node"].startswith("/dev/dri/renderD"):
        raise ValueError("render.node must explicitly select a render device")
    result["render"] = render
    result["policyFile"] = str((project / value.get("policyFile", ".runtime/stream-policy.json")).resolve())
    from .stream_policy import clamp_config, load_limits
    limits = load_limits(Path(result["policyFile"]))
    sessions = value.get("sessions")
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 16:
        raise ValueError("sessions must contain 1..16 configured slots")
    seen = {key: set() for key in ("id", "username", "uuid", "displayIndex")}
    normalized = []
    for index, session in enumerate(sessions):
        if not isinstance(session, dict) or not SLOT_ID.fullmatch(session.get("id", "")) or not NAME.fullmatch(session.get("username", "")):
            raise ValueError("invalid slot id or Minecraft username")
        entry = {**session, "uuid": str(uuid.UUID(session.get("uuid", offline_uuid(session["username"])))),
                 "displayIndex": session.get("displayIndex", index),
                 "stream": stream_config({**STREAM_DEFAULT, "width": render["width"], "height": render["height"], **session.get("stream", {})})}
        entry["stream"] = clamp_config(entry["stream"], limits)
        if type(entry["displayIndex"]) is not int or not 0 <= entry["displayIndex"] < len(sessions):
            raise ValueError("displayIndex must select a distinct display within the configured pool")
        for key in seen:
            token = entry[key].lower() if key in {"id", "username"} else entry[key]
            if token in seen[key]:
                raise ValueError(f"duplicate session {key}")
            seen[key].add(token)
        normalized.append(entry)
    result["sessions"] = normalized
    return result
