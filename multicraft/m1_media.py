"""One private compositor and independently acknowledged streams for 1..16 slots."""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import fcntl
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
from .pool_config import default_config, load_config

# Compatibility helpers for the earlier two-session contract tests.
TITLES = {'MultiCraft M1 A': 0, 'MultiCraft M1 B': 2}
SINKS = {'A': 'multicraft_m1_a', 'B': 'multicraft_m1_b'}
GRID_WIDTH, GRID_HEIGHT = 4096, 2304


def atomic_json(path: Path, value) -> None:
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(value, indent=2) + '\n'); os.chmod(pending, 0o600); os.replace(pending, path)


def private_environment(runtime: Path, inherited: dict | None = None) -> dict:
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        if key.startswith(('SELKIES_', 'PIXELFLUX_', 'PCMFLUX_')) or key in {
            'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'WAYLAND_SOCKET', 'PULSE_SERVER', 'PULSE_RUNTIME_PATH',
            'PULSE_SOURCE', 'PULSE_SINK', 'PIPEWIRE_REMOTE', 'PIPEWIRE_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
            'XDG_CURRENT_DESKTOP', 'ALSOFT_CONF', 'ALSOFT_DRIVERS'}: env.pop(key, None)
    xdg = runtime / 'xdg'; xdg.mkdir(parents=True, exist_ok=True); os.chmod(xdg, 0o700)
    env.update(XDG_RUNTIME_DIR=str(xdg), PIPEWIRE_RUNTIME_DIR=str(xdg), PULSE_RUNTIME_PATH=str(xdg / 'pulse'),
               PULSE_SERVER='unix:' + str(xdg / 'pulse/native'), PULSE_SINK='multicraft_pool_0', ALSOFT_DRIVERS='pulse',
               XDG_SESSION_TYPE='wayland', XCURSOR_THEME='Adwaita', XCURSOR_SIZE='24')
    return env


def assignments(windows: list, titles: dict | None = None) -> list[tuple[int, int]]:
    result = []
    for title, output in (TITLES if titles is None else titles).items():
        matching = [window for window in windows if window[1] == title]
        if len(matching) == 1 and (matching[0][3] != output or matching[0][4]): result.append((matching[0][0], output))
    return result


def output_layout(sessions: list) -> dict:
    if not 1 <= len(sessions) <= 16: raise ValueError('1..16 output slots required')
    columns = math.ceil(math.sqrt(len(sessions)))
    return {session['id']: {'output': 2 * session['displayIndex'], 'captureView': 2 * session['displayIndex'] + 1,
            'displayIndex': session['displayIndex'], 'x': session['displayIndex'] % columns * GRID_WIDTH,
            'y': session['displayIndex'] // columns * GRID_HEIGHT} for session in sessions}


async def stop_children(children):
    for child in reversed(children):
        if child.returncode is None: child.terminate()
    for child in reversed(children):
        if child.returncode is None:
            try: await asyncio.wait_for(child.wait(), 5)
            except asyncio.TimeoutError: child.kill(); await child.wait()


async def audio_start(runtime: Path, sinks: dict) -> list:
    children = []
    try:
        for name in ('pipewire', 'wireplumber', 'pipewire-pulse'):
            with (runtime / (name + '.log')).open('ab') as log:
                children.append(await asyncio.create_subprocess_exec(name, stdout=log, stderr=log))
        socket = Path(os.environ['PULSE_RUNTIME_PATH']) / 'native'
        for _ in range(100):
            if socket.is_socket(): break
            if any(child.returncode is not None for child in children): raise RuntimeError('private audio child exited')
            await asyncio.sleep(.1)
        else: raise RuntimeError('private audio socket did not become ready')
        from pulsectl_asyncio import PulseAsync
        async with PulseAsync('multicraft-pool-media', server=os.environ['PULSE_SERVER']) as pulse:
            for index, sink in enumerate(sinks.values()):
                await pulse.module_load('module-null-sink', f'sink_name={sink} sink_properties=device.description=MultiCraft_pool_{index}')
            await pulse.sink_default_set(next(iter(sinks.values())))
        return children
    except BaseException:
        await stop_children(children); raise


class OutputManager:
    """Resize only one physical output, preserving its SDL identity and every peer."""
    def __init__(self, capture, layout):
        self.capture, self.layout = capture, layout
        self.lock = asyncio.Lock()

    async def resize(self, session, width, height):
        item = self.layout[session]; physical, view = item['output'], item['captureView']
        async with self.lock:
            before = await asyncio.to_thread(self.capture.list_outputs)
            peers = {entry[0]: tuple(entry[:6]) for entry in before if entry[0] not in (physical, view)}
            if any(entry[0] == view for entry in before):
                if not await asyncio.to_thread(self.capture.destroy_output, view): raise RuntimeError('cannot remove owned capture view')
            if not await asyncio.to_thread(self.capture.resize_output, physical, width, height, 1.0):
                raise RuntimeError('cannot resize owned physical output')
            if not await asyncio.to_thread(self.capture.create_view, view, physical, 0, 0, width, height):
                raise RuntimeError('cannot rebuild owned full-output capture view')
            after = await asyncio.to_thread(self.capture.list_outputs)
            actual_peers = {entry[0]: tuple(entry[:6]) for entry in after if entry[0] not in (physical, view)}
            if peers != actual_peers: raise RuntimeError('peer output geometry changed during isolated resize')
            geometry = next((entry for entry in after if entry[0] == physical), None)
            if geometry is None or tuple(geometry[3:5]) != (width, height): raise RuntimeError('physical resize was not realized')


async def native_frame_limit(runtime: Path, session: str, fps: int):
    import aiohttp
    host = json.loads((runtime / 'host.json').read_text())
    token = (runtime / 'control.token').read_text().strip()
    base = 'http://127.0.0.1:' + str(host['controlPort'])
    async with aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token}, timeout=aiohttp.ClientTimeout(total=20)) as http:
        async with http.get(base + '/state') as response:
            response.raise_for_status(); state = await response.json()
        sequence = time.monotonic_ns() // 1000
        async with http.post(base + '/session/' + session + '/command', json={'type': 'setFrameLimit', 'fps': fps,
            'expectedRuntimeOwner': state['runtimeOwner'], 'configSequence': sequence}) as response:
            response.raise_for_status(); result = await response.json()
        if not result.get('ok') or result.get('nativeFrameLimit') != fps or result.get('frameConfigSequence') != sequence:
            raise RuntimeError('native frame target was not acknowledged')


async def serve(args):
    if version('pixelflux') != '2.2.1': raise RuntimeError('requires pinned pixelflux 2.2.1')
    import pixelflux
    from aiohttp import web
    from .m1_streams import AudioChannel, VideoChannel, ConfigConflict, ConfigApplyError
    config, runtime = args.configuration, args.runtime
    sessions = config['sessions']; layout = output_layout(sessions)
    titles = {'MultiCraft ' + session['id']: layout[session['id']]['output'] for session in sessions}
    sinks = {session['id']: 'multicraft_pool_' + str(session['displayIndex']) for session in sessions}
    children, runner, videos, audios = [], None, {}, {}
    control = runtime / 'media/control.sock'; owns_control = False
    media = runtime / 'media'
    atomic_json(media / 'state.json', {'ready': False, 'pid': os.getpid(), 'owner': args.owner, 'updatedAt': time.time()})
    try:
        children = await audio_start(media, sinks)
        primary = next(session for session in sessions if session['displayIndex'] == 0)
        name = await asyncio.to_thread(pixelflux.ensure_wayland_display, primary['stream']['width'], primary['stream']['height'],
                                      config['render']['node'], 'nvidia', 24)
        if not name or '/' in name or not (Path(os.environ['XDG_RUNTIME_DIR']) / name).is_socket():
            raise RuntimeError('private compositor socket did not become ready')
        os.environ['WAYLAND_DISPLAY'] = name
        capture = pixelflux.ScreenCapture()
        for session in sorted(sessions, key=lambda value: value['displayIndex']):
            item, stream = layout[session['id']], session['stream']
            if item['output'] == 0: continue  # primary output 0 and view 1 are reserved by pixelflux
            if not await asyncio.to_thread(capture.create_output, item['output'], stream['width'], stream['height'], item['x'], item['y'], 1.0):
                raise RuntimeError('cannot create output for ' + session['id'])
            if not await asyncio.to_thread(capture.create_view, item['captureView'], item['output'], 0, 0, stream['width'], stream['height']):
                raise RuntimeError('cannot create capture view for ' + session['id'])
        manager = OutputManager(capture, layout)
        env = {key: os.environ[key] for key in ('XDG_RUNTIME_DIR', 'WAYLAND_DISPLAY', 'PULSE_SERVER', 'ALSOFT_DRIVERS',
                                                'XDG_SESSION_TYPE', 'XCURSOR_THEME', 'XCURSOR_SIZE')}
        atomic_json(media / 'environment.json', {'environment': env, 'owner': args.owner, 'pid': os.getpid(), 'audioSinks': sinks, 'sessionOutputs': layout,
            'audioSource': 'native per-session OpenAL loopback; physical Pulse sinks are not the stream source'})
        for session in sessions:
            slot, stream = session['id'], session['stream']
            async def resize(width, height, slot=slot): await manager.resize(slot, width, height)
            async def frame_limit(fps, slot=slot): await native_frame_limit(runtime, slot, fps)
            channel = VideoChannel(slot, layout[slot]['captureView'], stream['width'], stream['height'], stream['fps'],
                stream['bitrateKbps'], config['render']['node'], adaptive=stream['adaptive'], resize=resize,
                native_limit=frame_limit, policy_file=Path(config['policyFile']))
            videos[slot] = channel; audios[slot] = AudioChannel(slot, args.root, runtime=runtime)
            from .stream_policy import validate_config, load_limits
            validate_config(stream, load_limits(Path(config['policyFile'])))
            await channel.start()
        observed = {'outputs': [], 'windows': []}
        def current_status():
            return {'ready': True, 'pid': os.getpid(), 'owner': args.owner, 'waylandDisplay': name, 'sessionOutputs': layout,
                **observed, 'sessions': {slot: {'generation': channel.generation, 'effective': dict(channel.effective),
                    'video': channel.snapshot(), 'audio': audios[slot].snapshot()} for slot, channel in videos.items()},
                'video': {slot: channel.snapshot() for slot, channel in videos.items()},
                'audio': {slot: channel.snapshot() for slot, channel in audios.items()}, 'updatedAt': time.time()}
        async def state(_): return web.json_response(current_status())
        async def assign(request):
            value = await request.json()
            if (not isinstance(value, dict) or set(value) != {'windowId', 'outputId'} or type(value['windowId']) is not int
                    or type(value['outputId']) is not int or value['outputId'] not in {v['output'] for v in layout.values()}):
                raise web.HTTPBadRequest(text='configured outputId and integer windowId required')
            success = await asyncio.to_thread(capture.move_window_to_output, value['windowId'], value['outputId'])
            return web.json_response({'moved': success})
        async def screenshot(request):
            try: output = int(request.match_info['output'])
            except ValueError: raise web.HTTPBadRequest(text='configured physical output required')
            if output not in {v['output'] for v in layout.values()}: raise web.HTTPBadRequest(text='configured physical output required')
            pixels = await asyncio.to_thread(pixelflux.screenshot_png, output)
            return web.Response(body=pixels, content_type='image/png', headers={'Cache-Control': 'no-store'})
        async def video(request):
            slot = request.match_info['session']
            if slot not in videos: raise web.HTTPNotFound()
            return await videos[slot].websocket(request)
        async def audio(request):
            slot = request.match_info['session']
            if slot not in audios: raise web.HTTPNotFound()
            return await audios[slot].websocket(request)
        async def configure(request):
            slot = request.match_info['session']
            if slot not in videos: raise web.HTTPNotFound()
            try: result = await videos[slot].configure(await request.json())
            except ConfigConflict as exc:
                return web.json_response({'applied': False, 'generation': videos[slot].generation,
                    'effective': dict(videos[slot].effective), 'error': str(exc)}, status=409)
            except ValueError as exc:
                return web.json_response({'applied': False, 'error': str(exc), 'effective': dict(videos[slot].effective)}, status=400)
            except ConfigApplyError as exc:
                return web.json_response({'applied': False, 'generation': videos[slot].generation, 'error': str(exc),
                    'rolledBack': exc.rolled_back, 'effective': dict(videos[slot].effective)}, status=503)
            return web.json_response(result)
        app = web.Application(client_max_size=4096)
        app.router.add_get('/status', state); app.router.add_post('/assign', assign)
        app.router.add_get('/screenshot/{output}', screenshot); app.router.add_get('/video/{session}', video)
        app.router.add_get('/audio/{session}', audio); app.router.add_post('/session/{session}/config', configure)
        runner = web.AppRunner(app, access_log=None); await runner.setup()
        if control.exists():
            # Only the service lock owner may remove a leftover socket from its own prior crash.
            if control.is_symlink() or not control.is_socket(): raise RuntimeError('invalid owned control socket path')
            control.unlink()
        await web.UnixSite(runner, str(control)).start(); owns_control = True; os.chmod(control, 0o600)
        while True:
            if any(child.returncode is not None for child in children): raise RuntimeError('private audio dependency exited')
            observed['windows'] = await asyncio.to_thread(capture.list_windows)
            for window, output in assignments(observed['windows'], titles):
                await asyncio.to_thread(capture.move_window_to_output, window, output)
            observed['outputs'] = await asyncio.to_thread(capture.list_outputs)
            for channel in videos.values(): await channel.observe()
            atomic_json(media / 'state.json', current_status()); await asyncio.sleep(1)
    finally:
        for channel in audios.values():
            with contextlib.suppress(Exception): await channel.stop()
        for channel in videos.values():
            with contextlib.suppress(Exception): await channel.stop()
        if runner: await runner.cleanup()
        if owns_control: control.unlink(missing_ok=True)
        await stop_children(children)
        atomic_json(media / 'state.json', {'ready': False, 'pid': os.getpid(), 'owner': args.owner, 'updatedAt': time.time()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--private-bus-ready', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(); args.root = args.root.resolve()
    args.configuration = load_config(args.config or args.root / 'multicraft.json')
    args.runtime = Path(args.configuration['runtimeDirectory'])
    if args.runtime_dir and args.runtime_dir.resolve() != args.runtime: parser.error('runtime-dir must match operator configuration')
    media = args.runtime / 'media'; media.mkdir(parents=True, exist_ok=True); os.chmod(media, 0o700)
    os.chdir(args.root); os.umask(0o077)
    if not args.private_bus_ready:
        env = private_environment(media)
        os.execvpe('dbus-run-session', ['dbus-run-session', '--', sys.executable, '-m', 'multicraft.m1_media',
                     *sys.argv[1:], '--private-bus-ready'], env)
    async def run():
        task = asyncio.current_task(); loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(signum, task.cancel)
        with contextlib.suppress(asyncio.CancelledError): await serve(args)
    with (media / 'service.lock').open('a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('another pool media service owns this runtime') from None
        asyncio.run(run())


if __name__ == '__main__': main()
