#!/usr/bin/env python3
"""Portable, owned-runtime launcher for the MultiCraft native pool alpha."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from multicraft.pool_config import default_config, load_config
from native_session import (client_environment, expand_arguments, offline_uuid, proc_start_ticks,
                            process_owned, read_record, launch, update_options, server_command, stop)
from prepare_minecraft import VERSION, METADATA_SHA1, prepare as download_cache, selected_libraries, sha1_file
from build_m1_host import build, CLIENT_SHA256


def atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with temporary.open('x') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2); stream.write('\n')
    os.replace(temporary, path)


def java_home(config: dict, override: str | None = None) -> Path:
    value = override or config.get('javaHome') or os.environ.get('JAVA_HOME')
    if not value:
        raise ValueError('select an existing JDK 25 with --java-home, javaHome or JAVA_HOME')
    home = Path(value).expanduser().resolve()
    if not (home / 'bin/java').is_file() or not (home / 'bin/javac').is_file():
        raise ValueError('an existing complete JDK 25 is required; no software is installed')
    result = subprocess.run([str(home / 'bin/java'), '-version'], capture_output=True, text=True, check=True, timeout=10)
    if not re.search(r'version "25[.\"]', result.stdout + result.stderr):
        raise ValueError('this pinned Minecraft client requires JDK 25')
    return home


def validate_cache(cache: Path) -> dict:
    if not (cache / 'lock.json').is_file():
        raise ValueError('verified Minecraft cache missing; set minecraftCache or explicitly prepare --download')
    metadata_path = cache / 'version.json'
    if sha1_file(metadata_path) != METADATA_SHA1:
        raise ValueError('pinned official Minecraft metadata hash mismatch')
    metadata = json.loads(metadata_path.read_text())
    lock = json.loads((cache / 'lock.json').read_text())
    expected = [(cache / f'{kind}.jar', metadata['downloads'][kind]['sha1']) for kind in ('client', 'server')]
    artifacts = selected_libraries(metadata)
    if lock['libraryPaths'] != [item['path'] for item in artifacts]:
        raise ValueError('cache library lock differs from pinned official metadata')
    expected.extend((cache / 'libraries' / item['path'], item['sha1']) for item in artifacts)
    index = metadata['assetIndex']
    index_path = cache / 'assets/indexes' / (index['id'] + '.json')
    expected.append((index_path, index['sha1']))
    if sha1_file(index_path) != index['sha1']:
        raise ValueError('official asset index hash mismatch')
    for digest in sorted({item['hash'] for item in json.loads(index_path.read_text())['objects'].values()}):
        expected.append((cache / 'assets/objects' / digest[:2] / digest, digest))
    for path, digest in expected:
        if not path.resolve().is_relative_to(cache.resolve()) or not path.is_file() or sha1_file(path) != digest:
            raise ValueError('official cache integrity check failed: ' + str(path))
    if hashlib.sha256((cache / 'client.jar').read_bytes()).hexdigest() != CLIENT_SHA256:
        raise ValueError('fixed-version Java adapter client hash mismatch')
    return {'verified': True, 'cache': str(cache), 'files': len(expected) + 1,
            'clientSha256': CLIENT_SHA256, 'externalCacheWrites': False, 'verifiedAt': time.time()}


def launch_settings_fingerprint(config: dict) -> str:
    # Includes effective startup streams after persisted OP caps are applied.
    value = {key: config[key] for key in ('sessions', 'serverAddress', 'render', 'controlPort', 'minecraftCache')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare_pool(config: dict, home: Path, download: bool = False) -> dict:
    runtime, project, cache = (Path(config[key]) for key in ('runtimeDirectory', 'projectDirectory', 'minecraftCache'))
    for role in ('host', 'media', 'supervisor'):
        record = read_record(runtime, role)
        if record and owned(record):
            raise ValueError('stop the owned pool before preparing a new generation')
    runtime.mkdir(parents=True, exist_ok=True); os.chmod(runtime, 0o700)
    if download:
        if cache != project / '.runtime/minecraft' / VERSION:
            raise ValueError('--download only writes this project\'s default cache; external caches are read-only')
        download_cache(cache.parent)
    verified = validate_cache(cache)
    atomic(runtime / 'cache-verified.json', verified)
    token_file = runtime / 'control.token'
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(secrets.token_urlsafe(48) + '\n')
    if token_file.is_symlink() or not token_file.is_file() or token_file.stat().st_mode & 0o077:
        raise ValueError('control token must be an owner-only regular file')
    metadata, lock = [json.loads((cache / name).read_text()) for name in ('version.json', 'lock.json')]
    from multicraft.m1_media import output_layout
    layout = output_layout(config['sessions'])
    sessions = []
    for session in config['sessions']:
        game = runtime / 'sessions' / session['id']
        game.mkdir(parents=True, exist_ok=True)
        if not game.resolve().is_relative_to(runtime / 'sessions'):
            raise ValueError('session game directory cannot escape the owned runtime')
        update_options(game, session['stream']['width'], session['stream']['height'])
        path = game / 'options.txt'
        lines = [line for line in path.read_text().splitlines() if not line.startswith(('preferredGraphicsBackend:', 'inactivityFpsLimit:', 'maxFps:'))]
        path.write_text('\n'.join([*lines, 'preferredGraphicsBackend:"vulkan"', 'inactivityFpsLimit:"minimized"', 'maxFps:260']) + '\n')
        values = {'auth_player_name': session['username'], 'version_name': VERSION, 'game_directory': str(game),
                  'assets_root': str(cache / 'assets'), 'assets_index_name': lock['assetIndex'],
                  'auth_uuid': session['uuid'], 'auth_access_token': '0', 'clientid': '', 'auth_xuid': '',
                  'version_type': 'release', 'resolution_width': str(session['stream']['width']),
                  'resolution_height': str(session['stream']['height']), 'quickPlayPath': str(game / 'quickplay.json'),
                  'quickPlayMultiplayer': config['serverAddress']}
        arguments = [*expand_arguments(metadata['arguments']['game'], values), '--graphicsBackend', 'vulkan', '--fullscreen']
        sessions.append({'id': session['id'], 'displayIndex': session['displayIndex'], 'args': arguments,
                         'displayOrigin': [layout[session['id']]['x'], layout[session['id']]['y']],
                         'serverAddress': config['serverAddress'], 'initialFrameLimit': session['stream']['fps']})
    host = {'clientJar': str(cache / 'client.jar'), 'libraries': [str(cache / 'libraries' / name) for name in lock['libraryPaths']],
            'audioMode': 'loopback-pcm', 'tokenFile': str(token_file), 'stateFile': str(runtime / 'host.state.json'),
            'controlPort': config['controlPort'], 'maxCycles': 0, 'requiredGraphicsBackend': 'Vulkan',
            'requiredGraphicsVendor': 'NVIDIA', 'teardownMode': 'defer-native', 'sdlQuitPolicy': 'process-exit', 'sessions': sessions}
    atomic(runtime / 'host.json', host)
    manifest = build(project, cache, runtime / 'build', home)
    prepared = {'prepared': True, 'sessionCount': len(sessions), 'runtimeDirectory': str(runtime),
                'hostJarSha256': manifest['jarSha256'], 'javaHome': str(home), 'configSha256': hashlib.sha256(Path(config['configPath']).read_bytes()).hexdigest(),
                'launchSettingsSha256': launch_settings_fingerprint(config),
                'serverAddress': config['serverAddress'], 'authentication': 'offline identities; authorized offline-mode server required'}
    atomic(runtime / 'prepared.json', prepared)
    return prepared


def owned(record: dict) -> bool:
    if record.get('role') in ('host', 'server'):
        return process_owned(record)
    try:
        pid = int(record['pid'])
        if proc_start_ticks(pid) != record['startTicks'] or Path(f'/proc/{pid}/cwd').resolve() != Path(record['cwd']).resolve():
            return False
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        return b'--owner' in command and command[command.index(b'--owner') + 1] == record['owner'].encode()
    except (KeyError, ValueError, OSError, IndexError):
        return False


def python_child(runtime: Path, role: str, arguments: list[str], project: Path) -> tuple[subprocess.Popen, dict]:
    owner = uuid.uuid4().hex
    with (runtime / (role + '.console.log')).open('ab', buffering=0) as log:
        child = subprocess.Popen([sys.executable, *arguments, '--owner', owner], cwd=project, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    record = {'role': role, 'pid': child.pid, 'owner': owner, 'cwd': str(project),
              'startTicks': proc_start_ticks(child.pid), 'startedAt': time.time()}
    atomic(runtime / (role + '.process.json'), record)
    return child, record


def host_request(config: dict, path: str, body=None):
    runtime = Path(config['runtimeDirectory'])
    token = (runtime / 'control.token').read_text().strip()
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{config['controlPort']}" + path, data=data,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def environment(config: dict) -> dict:
    from m1_session import host_environment
    runtime = Path(config['runtimeDirectory'])
    return host_environment(runtime, json.loads((runtime / 'media/environment.json').read_text()))


def media_ready(runtime: Path, record: dict) -> bool:
    """A stale socket or JSON file can never authorize another media generation."""
    try:
        state = json.loads((runtime / 'media/state.json').read_text())
        env = json.loads((runtime / 'media/environment.json').read_text())
        return (state.get('ready') is True and state.get('owner') == record['owner']
                and env.get('owner') == record['owner'] and state.get('pid') == env.get('pid')
                and type(state.get('pid')) is int and state['pid'] > 0
                and state.get('updatedAt', 0) >= record['startedAt']
                and (runtime / 'media/control.sock').is_socket())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def supervisor(config: dict, home: Path):
    runtime, project = [Path(config[key]) for key in ('runtimeDirectory', 'projectDirectory')]
    stopping = False
    def request_stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, request_stop); signal.signal(signal.SIGINT, request_stop)
    media = None; media_record = None; host_record = None; host_exit = None
    try:
        media, media_record = python_child(runtime, 'media', ['-m', 'multicraft.m1_media', '--root', str(project),
            '--runtime-dir', str(runtime), '--config', config['configPath']], project)
        deadline = time.monotonic() + 35
        while not media_ready(runtime, media_record):
            if stopping or media.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('private media did not become ready; inspect media.console.log')
            time.sleep(.1)
        host = json.loads((runtime / 'host.json').read_text())
        jar = runtime / 'build/multicraft-host.jar'
        classpath = os.pathsep.join([str(jar), host['clientJar'], *host['libraries']])
        (runtime / 'tmp').mkdir(exist_ok=True)
        arguments = ['-Djava.io.tmpdir=' + str(runtime / 'tmp'), '-Xms512M', '-Xmx' + str(config.get('heapMaximum', '8G')), '-XX:StackShadowPages=32',
            '--enable-native-access=ALL-UNNAMED', '--add-exports', 'java.base/jdk.internal.misc=ALL-UNNAMED',
            '-Dorg.lwjgl.system.SharedLibraryExtractPath=' + str(runtime / 'natives/lwjgl'),
            '-Dio.netty.native.workdir=' + str(runtime / 'natives/netty'), '-Djna.tmpdir=' + str(runtime / 'natives/jna'),
            '-cp', classpath, 'multicraft.host.Host', str(runtime / 'host.json')]
        host_record = launch(runtime, 'host', str(home / 'bin/java'), arguments, runtime, env=environment(config))
        atomic(runtime / 'startup.json', {'hostStarted': True, 'host': host_record, 'media': media_record, 'at': time.time()})
        while True:
            pid, status = os.waitpid(host_record['pid'], os.WNOHANG)
            if pid:
                host_exit = os.waitstatus_to_exitcode(status); break
            if stopping or media.poll() is not None:
                break
            time.sleep(.2)
    finally:
        if host_record and host_exit is None:
            with contextlib.suppress(Exception): host_request(config, '/shutdown', {})
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                pid, status = os.waitpid(host_record['pid'], os.WNOHANG)
                if pid:
                    host_exit = os.waitstatus_to_exitcode(status); break
                time.sleep(.1)
            if host_exit is None and process_owned(host_record):
                os.kill(host_record['pid'], signal.SIGKILL)
                _, status = os.waitpid(host_record['pid'], 0); host_exit = os.waitstatus_to_exitcode(status)
        if host_record:
            atomic(runtime / ('exit-' + host_record['owner'] + '.json'), {'pid': host_record['pid'], 'owner': host_record['owner'],
                'exitCode': host_exit, 'finishedAt': time.time(), 'nativeQuitPolicy': 'process-exit',
                'hsErr': [str(path) for path in runtime.glob('hs_err_pid' + str(host_record['pid']) + '.log')]})
        if media and media.poll() is None:
            if owned(media_record): os.killpg(media.pid, signal.SIGTERM)
            try: media.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if owned(media_record): os.killpg(media.pid, signal.SIGKILL)
                media.wait(timeout=5)
    return host_exit or 0


def status(config: dict) -> dict:
    runtime = Path(config['runtimeDirectory'])
    result = {'runtimeDirectory': str(runtime), 'sessionCount': len(config['sessions'])}
    for role in ('supervisor', 'host', 'media', 'server'):
        record = read_record(runtime, role)
        result[role] = {'running': bool(record and owned(record)), 'record': record}
    if (runtime / 'host.state.json').is_file(): result['native'] = json.loads((runtime / 'host.state.json').read_text())
    if (runtime / 'media/state.json').is_file(): result['mediaState'] = json.loads((runtime / 'media/state.json').read_text())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'prepare', 'start', 'status', 'stop', 'command', 'server-command', 'demo-server', '_supervise'))
    parser.add_argument('--config', type=Path, default=PROJECT / 'multicraft.json')
    parser.add_argument('--count', type=int, default=4)
    parser.add_argument('--java-home')
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--session')
    parser.add_argument('--text')
    parser.add_argument('--role', choices=('all', 'pool', 'server'), default='all')
    parser.add_argument('--type', choices=('connect', 'disconnect', 'respawn', 'reloadResources', 'close'))
    parser.add_argument('--accept-eula', action='store_true')
    parser.add_argument('--demo-port', type=int, default=25586)
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--owner', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action == 'init':
        if args.config.exists(): raise ValueError('configuration already exists; edit it explicitly')
        atomic(args.config, default_config(args.count)); print(json.dumps({'created': str(args.config.resolve()), 'sessions': args.count})); return
    config = load_config(args.config)
    runtime = Path(config['runtimeDirectory']); runtime.mkdir(parents=True, exist_ok=True); os.chmod(runtime, 0o700)
    if args.action == '_supervise':
        raise SystemExit(supervisor(config, java_home(config, args.java_home)))
    if args.action == 'status': print(json.dumps(status(config), indent=2)); return
    with (runtime / '.lifecycle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'prepare':
            result = prepare_pool(config, java_home(config, args.java_home), args.download)
        elif args.action == 'start':
            current = read_record(runtime, 'supervisor')
            if current and owned(current):
                result = {'alreadyRunning': True, 'supervisor': current}
            else:
                home = java_home(config, args.java_home)
                prepared = json.loads((runtime / 'prepared.json').read_text())
                if prepared['configSha256'] != hashlib.sha256(args.config.read_bytes()).hexdigest():
                    raise ValueError('configuration changed; prepare the stopped pool again before starting')
                if prepared.get('launchSettingsSha256') != launch_settings_fingerprint(config):
                    raise ValueError('effective startup settings or OP limits changed; prepare the stopped pool again')
                for role in ('host', 'media'):
                    record = read_record(runtime, role)
                    if record and owned(record): raise ValueError('an owned pool child is already running; inspect status')
                (runtime / 'startup.json').unlink(missing_ok=True)
                child, record = python_child(runtime, 'supervisor', [str(Path(__file__).resolve()), '_supervise',
                    '--config', str(args.config.resolve()), '--java-home', str(home)], PROJECT)
                deadline = time.monotonic() + 40
                while not (runtime / 'startup.json').is_file():
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('pool startup failed; inspect supervisor.console.log and media.console.log')
                    time.sleep(.1)
                result = {'started': True, 'supervisor': record, **json.loads((runtime / 'startup.json').read_text())}
        elif args.action == 'stop':
            record = read_record(runtime, 'supervisor')
            if args.role != 'server' and record and owned(record): os.kill(record['pid'], signal.SIGTERM)
            result = {'stopRequested': bool(args.role != 'server' and record and owned(record)), 'supervisor': record if args.role != 'server' else None}
            if args.role in ('server', 'all'): result['server'] = stop(runtime, 'server')
        elif args.action == 'command':
            if args.session not in {entry['id'] for entry in config['sessions']} or not args.type:
                raise ValueError('command requires a configured --session and --type')
            state = host_request(config, '/state')
            session = next(entry for entry in state['sessions'] if entry['id'] == args.session)
            result = host_request(config, '/session/' + args.session + '/command', {'type': args.type,
                'expectedRuntimeOwner': state['runtimeOwner'], 'expectedEpoch': session['connectionEpoch']})
        elif args.action == 'server-command':
            server_command(runtime, args.text or '')
            result = {'submitted': True}
        elif args.action == 'demo-server':
            if not args.accept_eula: raise ValueError('demo server requires explicit --accept-eula after reading the Minecraft EULA')
            if not 1024 <= args.demo_port <= 65535: raise ValueError('choose a nonprivileged demo port')
            home = java_home(config, args.java_home)
            game = runtime / 'demo-server'; game.mkdir(exist_ok=True)
            if not (game / 'server.properties').exists():
                (game / 'server.properties').write_text('server-ip=127.0.0.1\nserver-port=' + str(args.demo_port)
                    + '\nonline-mode=false\nenforce-secure-profile=false\nwhite-list=true\nenforce-whitelist=true\n'
                    + 'enable-rcon=false\nspawn-protection=0\nview-distance=5\nsimulation-distance=4\nmax-players=16\n')
            (game / 'eula.txt').write_text('eula=true\n')
            atomic(game / 'whitelist.json', [{'name': s['username'], 'uuid': s['uuid']} for s in config['sessions']])
            fifo = runtime / 'server.stdin'
            if not fifo.exists(): os.mkfifo(fifo, 0o600)
            if fifo.is_symlink() or not fifo.is_fifo(): raise ValueError('server input must be an owned FIFO')
            fd = os.open(fifo, os.O_RDWR)
            try: result = launch(runtime, 'server', str(home / 'bin/java'), ['-Xms512M', '-Xmx2G', '-jar',
                str(Path(config['minecraftCache']) / 'server.jar'), 'nogui'], game, stdin=fd)
            finally: os.close(fd)
        else: raise ValueError('unknown action')
    print(json.dumps(result, indent=2), flush=True)
    if args.action == 'stop' and result.get('supervisor'):
        deadline = time.monotonic() + 55
        while owned(result['supervisor']) and time.monotonic() < deadline: time.sleep(.1)
        if owned(result['supervisor']): raise RuntimeError('supervisor has not stopped; inspect status without killing unrelated processes')
    if args.action == 'start' and args.wait and result.get('started'):
        child.wait(); raise SystemExit(child.returncode)


if __name__ == '__main__':
    main()
