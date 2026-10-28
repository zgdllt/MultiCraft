"""Pinned Selkies integration on a private GPU Wayland and audio session.

The adapter uses Selkies 2.1.0 internals deliberately. It never modifies the
installed third-party package. Config acknowledgements and observed captures
are separate; an idle encoder is not reported as a received video stream.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

LOG = logging.getLogger("multicraft.media")
DEFAULT = {"width": 1280, "height": 720, "fps": 30,
           "bitrateKbps": 6000, "adaptive": True}


def validate_config(value: dict, limits: dict | None = None) -> dict:
    """Defense in depth: the media adapter does not trust a writable JSON file."""
    if set(value) != set(DEFAULT):
        raise ValueError("stream config must contain exactly width,height,fps,bitrateKbps,adaptive")
    result = dict(value)
    limits = limits or {"width": [320, 1920], "height": [180, 1080],
                        "fps": [10, 1000], "bitrateKbps": [500, 20000]}
    for key, (low, high) in limits.items():
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"invalid {key}: integer {low}..{high} required")
    if result["width"] % 2 or result["height"] % 2:
        raise ValueError("video dimensions must be even")
    if type(result["adaptive"]) is not bool:
        raise ValueError("adaptive must be a boolean")
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(pending, 0o600)
    os.replace(pending, path)


def private_environment(root: Path) -> Path:
    """Never inherit the user's display, sound socket or session bus."""
    xdg = root / ".runtime/media/xdg"
    xdg.mkdir(parents=True, exist_ok=True)
    os.chmod(xdg, 0o700)
    for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "PULSE_SERVER",
                "PULSE_RUNTIME_PATH", "PULSE_SOURCE", "PIPEWIRE_REMOTE", "PIPEWIRE_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP"):
        os.environ.pop(key, None)
    os.environ.update(XDG_RUNTIME_DIR=str(xdg), PIPEWIRE_RUNTIME_DIR=str(xdg),
                      PULSE_RUNTIME_PATH=str(xdg / "pulse"),
                      PULSE_SERVER="unix:" + str(xdg / "pulse/native"),
                      PULSE_SINK="multicraft", ALSOFT_DRIVERS="pulse", XDG_SESSION_TYPE="wayland",
                      XCURSOR_THEME="Adwaita", XCURSOR_SIZE="24")
    return xdg


class TaskReentrantLock:
    """An asyncio lock reentrant only for the task that acquired it.

    ``locked()`` retains asyncio's global meaning: Selkies uses it to coalesce
    reconfiguration requests, including nested requests from the owning task.
    """
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    def locked(self):
        return self._lock.locked()

    async def acquire(self):
        task = asyncio.current_task()
        if task is not self._owner:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return True

    def release(self):
        if self._owner is not asyncio.current_task() or not self._depth:
            raise RuntimeError("media lock released by a task that does not own it")
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()


def install_transaction_locks(service):
    """Install before the pinned service starts any of its background tasks."""
    for name in ("_reconfigure_lock", "_video_capture_lock"):
        current = getattr(service, name, None)
        if isinstance(current, TaskReentrantLock):
            continue
        if current is not None and current.locked():
            raise RuntimeError("cannot replace an active media lock")
        setattr(service, name, TaskReentrantLock())


class MediaAdapter:
    def __init__(self, server: Any, settings: Any, root: Path, policy_file: Path | None = None):
        self.server, self.settings, self.root = server, settings, root
        self.policy_file = policy_file or root / ".runtime/stream-policy.json"
        self.config = dict(DEFAULT)
        self.generation = 0
        self.request_id = "initial"
        self.last_error = None
        self.cursor_events = 0
        install_transaction_locks(self.service)

    async def drain_reconfigure(self) -> None:
        """Drain Selkies' coalesced work while the transaction owns both locks.

        Calling reconfigure_displays() under its lock only marks pending. Run
        the pinned pass here, retaining its bookkeeping and drain semantics,
        so capture is rebuilt before either lock is released or an ACK is made.
        """
        service = self.service
        while getattr(service, "_reconfigure_pending", False):
            service._reconfigure_pending = False
            service._is_reconfiguring = True
            try:
                await service._reconfigure_displays_locked()
            finally:
                service._last_display_count = len(service.display_clients)
                service._is_reconfiguring = False

    @contextlib.asynccontextmanager
    async def transaction(self):
        service = self.service
        # Match Selkies' R -> V ordering, including START_VIDEO paths that
        # acquire V directly. Nested upstream calls may acquire the same locks.
        async with service._reconfigure_lock:
            async with service._video_capture_lock:
                try:
                    yield
                finally:
                    await self.drain_reconfigure()

    def observe_cursor_events(self) -> None:
        """Count formatted native callbacks without storing/logging PNG payloads."""
        app = self.service.app
        original = app.send_ws_cursor_data

        def forward(data):
            self.cursor_events += 1
            return original(data)

        app.send_ws_cursor_data = forward

    @property
    def service(self):
        return self.server.services["websockets"]

    def observed(self) -> dict:
        service = self.service
        state = service.display_clients.get("primary") or {}
        inst = service.capture_instances.get("primary") or {}
        capture = inst.get("settings")
        module = inst.get("module")
        active = bool(module and module.is_capturing)
        cursor = getattr(service.app, "last_cursor_sent", None)
        pointer = None
        pointer_error = None
        native_input = getattr(getattr(service, "input_handler", None), "wayland_input", None)
        if native_input is not None:
            try:
                pointer = native_input.pointer_location()
            except Exception as exc:
                pointer_error = str(exc)
        return {
            "ready": active,
            "width": int(getattr(capture, "capture_width", 0)) if active else None,
            "height": int(getattr(capture, "capture_height", 0)) if active else None,
            "captureFpsTarget": float(getattr(capture, "target_fps", 0)) if active else None,
            "encoderBitrateKbps": int(getattr(capture, "video_bitrate_kbps", 0)) if active else None,
            "clientAckFps": state.get("_measured_client_fps"),
            "roundTripMs": state.get("smoothed_rtt"),
            "adaptiveLimitKbps": state.get("link_kbps"),
            "audioCapturing": bool(service.is_pcmflux_capturing),
            "clients": len(service.clients),
            "cursorEvents": self.cursor_events,
            "pointerLocation": pointer,
            "pointerLocationError": pointer_error,
            "nativeCursor": ({"handle": cursor.get("handle"),
                              "hotx": cursor.get("hotx"), "hoty": cursor.get("hoty"),
                              "imageLength": len(cursor.get("curdata") or "")}
                             if isinstance(cursor, dict) else None),
            "source": "Selkies live capture state; browser decoding measured separately",
        }

    async def apply(self, config: dict) -> None:
        async with self.transaction():
            await self._apply_locked(config)

    async def _apply_locked(self, config: dict) -> None:
        from .stream_policy import load_limits
        config = validate_config(config, load_limits(self.policy_file))
        service = self.service
        if service.app is None:
            raise RuntimeError("media service has not initialized")
        state = service.display_clients.get("primary")
        expects_video = bool(state and (state.get("video_active") or self.observed()["ready"]))
        if state and (state.get("width"), state.get("height")) != (config["width"], config["height"]):
            # A live resize has left this Vulkan client waiting in swapchain
            # creation/destruction while capture continued sending old frames.
            # Stop capture before resizing as a compatibility constraint; the
            # separate contributions of retained images and DPI are unproven.
            await service._stop_capture_for_display("primary")
        self.settings.manual_width = config["width"]
        self.settings.manual_height = config["height"]
        self.settings.congestion_control = (config["adaptive"], True)
        # Locks the upstream sidebar/protocol to the choices accepted by our
        # gateway. Only this adapter changes the lock, on the owning event loop.
        self.settings.framerate = (config["fps"], config["fps"])
        # The lower bound is also the native congestion controller's floor;
        # locking both ends to the target would silently disable adaptation.
        self.settings.video_bitrate = (500, config["bitrateKbps"])
        service.app.set_framerate(config["fps"])
        service.app.display_width = config["width"]
        service.app.display_height = config["height"]
        service._initial_video_bitrate = config["bitrateKbps"]
        if state:
            # An old congestion estimate must not cap the new manual target.
            # The native congestion controller will learn a new limit if enabled.
            for key in ("link_kbps", "link_steer", "link_capacity_kbps", "cc_start_kbps"):
                state.pop(key, None)
            owner = state.get("ws")
            await service._apply_client_settings(owner, {
                "displayId": "primary", "framerate": config["fps"],
                "video_bitrate": config["bitrateKbps"], "rate_control_mode": "cbr",
                "manual_resolution": True, "manual_width": config["width"],
                "manual_height": config["height"],
            }, False, "controller")
            await service._handle_opcode_fps(config["fps"])
            await service._handle_opcode_video_bitrate(config["bitrateKbps"])
            await service._tell_display_settings("primary", service.clients)
            await service._broadcast_live_server_settings("primary")
            # The upstream reconfiguration logs some failures instead of
            # raising. Retry explicitly when a stopped/failed capture must be
            # rebuilt, including rollback to an otherwise unchanged config.
            if expects_video and not self.observed()["ready"]:
                await service.reconfigure_displays()
            await self.drain_reconfigure()
            seen = self.observed()
            if expects_video and not seen["ready"]:
                raise RuntimeError("active video capture did not recover after configuration")
            if seen["ready"] and (seen["width"], seen["height"]) != (config["width"], config["height"]):
                raise RuntimeError("capture did not realize the requested dimensions")
            if seen["ready"]:
                pace = state.get("decode_pace")
                expected_fps = min(config["fps"], pace) if pace else config["fps"]
                if state.get("framerate") != config["fps"] or seen["captureFpsTarget"] != expected_fps:
                    raise RuntimeError("capture did not apply the requested frame rate")
                rate = seen["encoderBitrateKbps"]
                if rate is None or not 500 <= rate <= config["bitrateKbps"]:
                    raise RuntimeError("encoder bitrate is outside the requested bounds")
                if not config["adaptive"] and rate != config["bitrateKbps"]:
                    raise RuntimeError("encoder did not apply the fixed bitrate target")
                target = state.get("video_bitrate", service._initial_video_bitrate)
                limit = state.get("link_kbps") if config["adaptive"] else None
                expected_rate = min(config["bitrateKbps"], limit) if limit else config["bitrateKbps"]
                if target != config["bitrateKbps"] or rate != int(round(expected_rate)):
                    raise RuntimeError("encoder did not apply the current adaptive bitrate target")
        else:
            # The compositor exists before a client, so the native window can
            # start, but capture/encoding begins only when an authenticated page joins.
            await service._size_wayland_screen(config["width"], config["height"])
        self.config = config

    async def watch(self, request: Path, effective: Path) -> None:
        requested = dict(self.config)
        while True:
            try:
                if request.exists():
                    item = json.loads(request.read_text())
                    request_id = item["requestId"]
                    generation = item["generation"]
                    if not isinstance(request_id, str) or type(generation) is not int:
                        raise ValueError("invalid request identity")
                    if generation > self.generation and request_id != self.request_id:
                        requested = item["config"]
                        async with self.transaction():
                            old = dict(self.config)
                            try:
                                await self.apply(item["config"])
                                self.last_error = None
                            except Exception as exc:
                                self.last_error = str(exc)
                                LOG.exception("Stream configuration failed; restoring last valid config")
                                try:
                                    await self.apply(old)
                                except Exception as rollback_error:
                                    atomic_json(effective, {
                                        "requestId": request_id, "generation": generation,
                                        "requested": requested, "effective": None, "measured": self.observed(),
                                        "error": "media rollback failed: " + str(rollback_error),
                                        "fatal": True, "updatedAt": time.time(),
                                    })
                                    raise RuntimeError("media configuration and rollback both failed") from rollback_error
                        self.generation, self.request_id = generation, request_id
                atomic_json(effective, {
                    "requestId": self.request_id, "generation": self.generation,
                    "requested": requested,
                    "effective": self.config, "measured": self.observed(),
                    "error": self.last_error, "updatedAt": time.time(),
                })
            except (OSError, ValueError, KeyError):
                LOG.exception("Invalid stream request; keeping last valid media config")
            await asyncio.sleep(0.2)


async def stop_children(children: list[asyncio.subprocess.Process]) -> None:
    for child in reversed(children):
        if child.returncode is not None:
            continue
        with contextlib.suppress(ProcessLookupError):
            child.terminate()
        try:
            await asyncio.wait_for(child.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            await child.wait()


async def monitor_native(record: dict) -> None:
    """The media process being alive does not imply its game client is alive."""
    pid = int(record["pid"])
    while True:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            ticks = stat[stat.rindex(")") + 2:].split()[19]
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            matches = (ticks == record["startTicks"]
                       and Path(f"/proc/{pid}/cwd").resolve() == Path(record["cwd"]).resolve()
                       and ("-Dmulticraft.owner=" + record["owner"]).encode() in command)
        except (OSError, KeyError, ValueError):
            matches = False
        if not matches:
            raise RuntimeError("owned native Minecraft client exited")
        await asyncio.sleep(0.5)


async def start_audio(root: Path) -> list[asyncio.subprocess.Process]:
    """Use a private PipeWire graph; never capture the host user's audio."""
    children = []
    try:
        for name in ("pipewire", "wireplumber", "pipewire-pulse"):
            log = (root / "logs" / (name + ".log")).open("ab")
            try:
                child = await asyncio.create_subprocess_exec(name, stdout=log, stderr=log)
            finally:
                log.close()
            children.append(child)
        pulse_socket = Path(os.environ["PULSE_RUNTIME_PATH"]) / "native"
        for _ in range(100):
            if pulse_socket.exists():
                break
            if any(child.returncode is not None for child in children):
                raise RuntimeError("private sound server exited; inspect PipeWire logs")
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("private audio socket did not appear")
        from pulsectl_asyncio import PulseAsync
        async with PulseAsync("multicraft-media", server=os.environ["PULSE_SERVER"]) as pulse:
            await pulse.module_load("module-null-sink", "sink_name=multicraft sink_properties=device.description=MultiCraft_M0")
            await pulse.sink_default_set("multicraft")
        return children
    except BaseException:
        await stop_children(children)
        raise


def configure_selkies(args, initial: dict) -> None:
    # Host-capture, command hooks and native extension fallbacks are not part
    # of this runtime's configuration, even if the invoking shell set them.
    for key in list(os.environ):
        if key.startswith(("SELKIES_", "PIXELFLUX_", "PCMFLUX_")):
            os.environ.pop(key)
    values = {
        "addr": "127.0.0.1", "port": args.port, "subfolder": "/stream",
        "mode": "websockets", "enable_dual_mode": "false",
        "wayland": "true", "render_dri": args.render_node, "auto_gpu": "nvidia",
        "wayland_host_display": "", "app_wayland_display": "",
        "enable_cursors": "true", "use_browser_cursors": "true",
        "encoder": "h264enc", "use_cpu": "false", "rate_control_mode": "cbr",
        "framerate": str(initial["fps"]), "video_bitrate": str(initial["bitrateKbps"]),
        "congestion_control": str(initial["adaptive"]).lower(),
        "manual_resolution": "true", "manual_width": initial["width"],
        "manual_height": initial["height"], "enable_resize": "true", "scaling_dpi": "96",
        "video_streaming_mode": "true", "audio_enabled": "true", "audio_on_start": "true",
        "audio_device_name": "multicraft.monitor", "audio_bitrate": 128000,
        "microphone_enabled": "false", "webcam_enabled": "false", "gamepad_enabled": "false",
        "second_screen": "false", "enable_sharing": "false", "enable_collab": "false",
        "file_transfers": "none", "printing_enabled": "false", "command_enabled": "false",
        "enable_clipboard": "in", "enable_binary_clipboard": "false", "clipboard_seamless": "false",
        "enable_basic_auth": "false", "enable_https": "false",
        "ui_sidebar_show_video_settings": "false", "ui_sidebar_show_screen_settings": "false",
        "ui_sidebar_show_files": "false", "ui_sidebar_show_apps": "false",
        "ui_sidebar_show_sharing": "false", "ui_sidebar_show_webcam": "false",
    }
    for key, value in values.items():
        os.environ["SELKIES_" + key.upper()] = str(value)
    token = args.master_token_file.read_text().strip()
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError("media master token must contain at least 32 non-whitespace characters")
    if args.master_token_file.stat().st_mode & 0o077:
        raise ValueError("media master token file must have mode 0600")
    os.environ["SELKIES_MASTER_TOKEN"] = token


async def serve(args) -> None:
    from importlib.metadata import version
    if version("selkies") != "2.1.0":
        raise RuntimeError("This adapter requires the pinned selkies==2.1.0")
    from .stream_policy import load_limits, clamp_config
    previous = json.loads(args.stream_effective.read_text()) if args.stream_effective.exists() else {}
    initial = clamp_config(previous.get("effective") or DEFAULT, load_limits(args.policy_file))
    configure_selkies(args, initial)
    # Its settings module parses argv on import; do not give it our CLI.
    saved_argv = sys.argv
    sys.argv = ["selkies"]
    try:
        from selkies.settings import settings
        from selkies.stream_server import CentralizedStreamServer
        from selkies.websockets_mode import DataStreamingServer
    finally:
        sys.argv = saved_argv
    children = []
    server = None
    watcher = None
    serving = None
    media_task = None
    dependency_tasks = []
    native_started = False
    native_record = None
    try:
        children = await start_audio(args.root)
        await asyncio.to_thread(settings.resolve_encoder_backends)
        server = CentralizedStreamServer(settings)
        service = DataStreamingServer(server)
        install_transaction_locks(service)
        server.register_service("websockets", service)
        await server.switch_to_mode("websockets")
        media_task = server.active_task
        if media_task is None:
            raise RuntimeError("Selkies did not start its required media task")
        for _ in range(100):
            if media_task.done():
                media_task.result()
                raise RuntimeError("Selkies media task stopped during initialization")
            if service.app:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Selkies did not initialize")
        if not service.is_secure_mode:
            raise RuntimeError("refusing a media service without token authentication")
        adapter = MediaAdapter(server, settings, args.root, args.policy_file)
        adapter.observe_cursor_events()
        adapter.config = dict(initial)
        adapter.generation = previous.get("generation", 0)
        adapter.request_id = previous.get("requestId", "initial")
        await adapter.apply(initial)
        wayland = os.environ.get("WAYLAND_DISPLAY", "")
        xdg = Path(os.environ["XDG_RUNTIME_DIR"]).resolve()
        if not wayland or "/" in wayland or not (xdg / wayland).is_socket():
            raise RuntimeError("private Wayland compositor did not establish its own socket")
        env_record = {k: os.environ[k] for k in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_SINK", "ALSOFT_DRIVERS")}
        atomic_json(args.root / ".runtime/media/environment.json", env_record)
        atomic_json(args.runtime_state, {
            "backendReady": True, "nativeReady": False, "pid": os.getpid(),
            "waylandDisplay": os.environ.get("WAYLAND_DISPLAY"),
            "transport": "WebSocket / WebCodecs", "video": "H264", "audio": "Opus",
            "minecraft": "26.3", "mediaBackend": "Selkies 2.1.0", "updatedAt": time.time(),
        })
        watcher = asyncio.create_task(adapter.watch(args.stream_request, args.stream_effective))
        if args.start_client:
            command = [sys.executable, str(args.root / "scripts/native_session.py"), "client",
                       "--runtime-dir", str(args.root / ".runtime/minecraft"), "--java", str(args.java),
                       "--width", str(initial["width"]), "--height", str(initial["height"]),
                       "--username", args.minecraft_username, "--port", str(args.minecraft_port)]
            started = await asyncio.create_subprocess_exec(*command, cwd=args.root,
                                                            stdout=asyncio.subprocess.PIPE)
            output, _ = await started.communicate()
            if started.returncode:
                raise RuntimeError("Native launcher failed: " + output.decode(errors="replace"))
            LOG.info("Native launcher: %s", output.decode().strip())
            native_record = json.loads(output)
            native_started = True
            state = json.loads(args.runtime_state.read_text())
            state["nativeReady"] = True
            state["nativeReadyMeaning"] = "owned native process started; gameplay checked independently"
            atomic_json(args.runtime_state, state)
        serving = asyncio.create_task(server.run())
        dependency_tasks = [asyncio.create_task(child.wait()) for child in children]
        if native_record:
            dependency_tasks.append(asyncio.create_task(monitor_native(native_record)))
        finished, _ = await asyncio.wait([serving, watcher, media_task, *dependency_tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            task.result()
        raise RuntimeError("a required media task stopped unexpectedly")
    finally:
        if watcher:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        if serving:
            serving.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await serving
        if native_started:
            with contextlib.suppress(Exception):
                current = json.loads((args.root / ".runtime/minecraft/26.3/client.process.json").read_text())
                if current.get("owner") == native_record.get("owner") and current.get("pid") == native_record.get("pid"):
                    child = await asyncio.create_subprocess_exec(sys.executable, str(args.root / "scripts/native_session.py"),
                        "stop", "--runtime-dir", str(args.root / ".runtime/minecraft"), "--role", "client",
                        "--expected-owner", native_record["owner"],
                        cwd=args.root, stdout=asyncio.subprocess.DEVNULL)
                    await asyncio.wait_for(child.wait(), 15)
        if server:
            with contextlib.suppress(Exception):
                await server.stop_server()
            # The upstream callback may already have cleared current_mode on
            # an unexpected service failure; stop that service explicitly too.
            with contextlib.suppress(Exception):
                await server.services["websockets"].stop()
        if media_task and not media_task.done():
            media_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await media_task
        await stop_children(children)
        for task in dependency_tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        atomic_json(args.runtime_state, {"backendReady": False, "nativeReady": False, "updatedAt": time.time()})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--render-node", default="/dev/dri/renderD128")
    parser.add_argument("--master-token-file", type=Path, required=True)
    parser.add_argument("--runtime-state", type=Path, default=Path(".runtime/media/state.json"))
    parser.add_argument("--stream-request", type=Path, default=Path(".runtime/media/request.json"))
    parser.add_argument("--stream-effective", type=Path, default=Path(".runtime/media/effective.json"))
    parser.add_argument("--policy-file", type=Path, default=Path(".runtime/stream-policy.json"))
    parser.add_argument("--java", type=Path, default=Path(os.environ["JAVA_HOME"]) / "bin/java" if os.environ.get("JAVA_HOME") else Path("java"))
    parser.add_argument("--minecraft-port", type=int, default=25585)
    parser.add_argument("--minecraft-username", default="M0Player")
    parser.add_argument("--start-client", action="store_true")
    parser.add_argument("--private-bus-ready", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.root = args.root.resolve()
    os.chdir(args.root)
    os.umask(0o077)
    (args.root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not args.private_bus_ready:
        private_environment(args.root)
        os.execvp("dbus-run-session", ["dbus-run-session", "--", sys.executable, "-m",
                  "multicraft.media_runtime", *sys.argv[1:], "--private-bus-ready"])
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
        try:
            await serve(args)
        except asyncio.CancelledError:
            pass
    asyncio.run(run())


if __name__ == "__main__":
    main()
