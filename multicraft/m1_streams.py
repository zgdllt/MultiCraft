"""Bounded, session-local native H.264/PCM transport and acknowledged reconfiguration."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
import contextlib
import json
from pathlib import Path
import re
import struct
import threading
import time
from .pool_config import STREAM_DEFAULT, stream_config

_START = re.compile(b"\x00\x00(?:\x00)?\x01")


def annexb_metadata(data: bytes) -> tuple[bool, str | None]:
    key, codec = False, None
    for match in _START.finditer(data):
        offset = match.end()
        if offset >= len(data): continue
        kind = data[offset] & 31
        key |= kind == 5
        if kind == 7 and offset + 3 < len(data): codec = "avc1." + data[offset + 1:offset + 4].hex()
    return key, codec


class Subscriber:
    def __init__(self, depth=3):
        self.queue = asyncio.Queue(maxsize=depth)
        self.needs_key = True
        self.dropped = self.sent = self.decoded = 0
        self.sent_stamps = OrderedDict()
        self.feedback_lag = None
        self.last_feedback = 0.0


class ConfigConflict(ValueError):
    pass


class ConfigApplyError(RuntimeError):
    def __init__(self, message, rolled_back):
        super().__init__(message)
        self.rolled_back = rolled_back


class VideoChannel:
    def __init__(self, session, display, width, height, fps, bitrate, render_node, *, adaptive=True,
                 resize=None, native_limit=None, policy_file=None):
        self.session, self.display = session, display
        self.width, self.height, self.fps, self.bitrate = width, height, fps, bitrate
        self.effective = stream_config(dict(width=width, height=height, fps=fps, bitrateKbps=bitrate, adaptive=adaptive))
        self.render_node, self.resize, self.native_limit, self.policy_file = render_node, resize, native_limit, policy_file
        self.capture = None
        self.clients, self.sockets = set(), set()
        self.codec = None
        self.frames = self.bytes = self.dropped = 0
        self.error = None
        self.generation = 0
        self.request_id = 'initial'
        self.reconfiguring = False
        self.update_method = 'initial'
        self.adaptive_bitrate = bitrate
        self.feedback_count = 0
        self._last_adaptation = 0.0
        self._last_idr = 0
        self._capture_epoch = 0
        self._loop = asyncio.get_running_loop()
        self._config_lock = asyncio.Lock()
        self._pending = None
        self._pending_lock = threading.Lock()
        self._scheduled = False
        self._loss_pending = False
        self._requests = OrderedDict()
        self.realized = None
        self.measured = {'width': None, 'height': None, 'fps': None, 'bitrateKbps': None, 'sampleSeconds': 0}
        self._observed = None

    async def start(self):
        import pixelflux
        settings = pixelflux.CaptureSettings()
        for name, value in {
            'display_id': self.display, 'capture_width': self.width, 'capture_height': self.height,
            'target_fps': float(self.fps), 'codec': 'h264', 'video_fullframe': True,
            'video_streaming_mode': True, 'use_cpu': False, 'video_cbr_mode': True,
            'video_bitrate_kbps': self.adaptive_bitrate, 'video_reference_frames': 1,
            'keyframe_interval_s': 1.0, 'omit_stripe_headers': True, 'use_wayland': True,
            'render_node_path': self.render_node, 'auto_gpu': 'nvidia', 'capture_cursor': True, 'scale': 1.0,
        }.items(): setattr(settings, name, value)
        self._capture_epoch += 1
        epoch = self._capture_epoch
        self.capture = pixelflux.ScreenCapture()
        await asyncio.to_thread(self.capture.start_capture, lambda frame: self.callback(frame, epoch), settings)
        geometry = await asyncio.to_thread(self.capture.get_realized_geometry, self.display)
        if geometry is None or tuple(geometry[:2]) != (self.width, self.height):
            await self.stop_capture()
            raise RuntimeError('native capture geometry did not match requested full output')
        if not self.capture.is_capturing:
            raise RuntimeError('native encoder did not start')
        self.realized = tuple(geometry)
        self._observed = None
        self.measured = {'width': geometry[0], 'height': geometry[1], 'fps': None, 'bitrateKbps': None, 'sampleSeconds': 0}

    async def stop_capture(self):
        self._capture_epoch += 1
        capture, self.capture = self.capture, None
        if capture is not None: await asyncio.to_thread(capture.stop_capture)
        with self._pending_lock:
            self._pending = None
            self._loss_pending = False
        self.invalidate_clients()

    def invalidate_clients(self):
        for client in self.clients:
            while not client.queue.empty(): client.queue.get_nowait()
            client.needs_key = True
            client.sent_stamps.clear()
            client.feedback_lag = None
            client.last_feedback = 0.0

    def callback(self, frame, epoch=None):
        if epoch is not None and epoch != self._capture_epoch: return
        if frame is None or not len(frame) or len(frame) > 8 * 1024 * 1024: return
        data = bytes(frame)
        stamp = int(getattr(frame, 'capture_ns', 0) or time.monotonic_ns()) // 1000
        with self._pending_lock:
            if epoch is not None and epoch != self._capture_epoch: return
            if self._pending is not None:
                self.dropped += 1; self._loss_pending = True
            self._pending = (data, stamp)
            if self._scheduled: return
            self._scheduled = True
        self._loop.call_soon_threadsafe(self._drain)

    def _drain(self):
        with self._pending_lock:
            pending, self._pending = self._pending, None
            loss, self._loss_pending = self._loss_pending, False
            self._scheduled = False
        if pending is None: return
        if self.reconfiguring:
            self.dropped += 1
            return
        data, stamp = pending
        key, codec = annexb_metadata(data)
        if codec: self.codec = codec
        self.frames += 1; self.bytes += len(data)
        packet = struct.pack('!QB', stamp, key) + data
        if loss:
            for client in self.clients: client.needs_key = True
            self.request_keyframe()
        for client in self.clients: self.offer(client, packet, key)

    def offer(self, client, packet, key):
        if client.queue.full():
            while not client.queue.empty(): client.queue.get_nowait(); client.dropped += 1
            client.needs_key = True; self.request_keyframe()
        if client.needs_key and not key: return
        client.needs_key = False
        client.queue.put_nowait((packet, key, self.codec))

    def request_keyframe(self):
        now = time.monotonic()
        if self.capture is not None and now - self._last_idr > .25:
            self._last_idr = now; self.capture.request_idr_frame()

    async def _native_apply(self, config):
        resolution = (config['width'], config['height']) != (self.width, self.height)
        if resolution:
            if self.resize is None: raise RuntimeError('this channel has no output resize adapter')
            await self.stop_capture()
            await self.resize(config['width'], config['height'])
            self.width, self.height = config['width'], config['height']
            self.fps, self.bitrate, self.adaptive_bitrate = config['fps'], config['bitrateKbps'], config['bitrateKbps']
            await self.start()
        else:
            if self.capture is None: raise RuntimeError('capture is not running')
            if await asyncio.to_thread(self.capture.update_framerate, float(config['fps'])) is False:
                raise RuntimeError('native frame-rate API rejected update')
            if await asyncio.to_thread(self.capture.update_video_bitrate, config['bitrateKbps']) is False:
                raise RuntimeError('native bitrate API rejected update')
            self.fps, self.bitrate, self.adaptive_bitrate = config['fps'], config['bitrateKbps'], config['bitrateKbps']
        if self.native_limit is not None: await self.native_limit(config['fps'])
        return 'native-output-resize' if resolution else 'native-live-tunables'

    async def configure(self, value):
        if not isinstance(value, dict) or set(value) != {'requestId', 'expectedGeneration', 'config'}:
            raise ValueError('requestId,expectedGeneration,config required')
        request_id, expected = value['requestId'], value['expectedGeneration']
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            raise ValueError('invalid requestId')
        if type(expected) is not int or expected < 0: raise ValueError('invalid expectedGeneration')
        config = stream_config(value['config'])
        if self.policy_file is not None:
            from .stream_policy import validate_config, load_limits
            config = validate_config(config, load_limits(self.policy_file))
        fingerprint = json.dumps(value, sort_keys=True)
        async with self._config_lock:
            cached = self._requests.get(request_id)
            if cached:
                if cached[0] != fingerprint: raise ConfigConflict('requestId was reused with a different payload')
                if cached[1]['generation'] != self.generation: raise ConfigConflict('request already applied before a newer configuration')
                return dict(cached[1])
            if expected != self.generation: raise ConfigConflict('stream generation changed')
            previous = dict(self.effective)
            self.reconfiguring = True
            try:
                method = await self._native_apply(config)
            except Exception as error:
                rollback = False
                try:
                    # A failed restart may have stopped capture before assigning new geometry.
                    if self.capture is None:
                        if self.resize is not None: await self.resize(previous['width'], previous['height'])
                        self.width, self.height = previous['width'], previous['height']
                        self.fps = previous['fps']; self.bitrate = self.adaptive_bitrate = previous['bitrateKbps']
                        await self.start()
                    await self._native_apply(previous)
                    rollback = True
                except Exception as rollback_error:
                    self.error = 'rollback failed: ' + type(rollback_error).__name__
                finally: self.reconfiguring = False
                if rollback: self.error = None
                raise ConfigApplyError(type(error).__name__ + ': ' + str(error), rollback) from error
            self.effective = config; self.generation += 1; self.request_id = request_id
            self.update_method = method; self.error = None; self.reconfiguring = False
            self.invalidate_clients(); self.request_keyframe()
            result = {'applied': True, 'requestId': request_id, 'generation': self.generation,
                      'effective': dict(self.effective), 'updateMethod': method, 'rolledBack': False,
                      'acknowledgement': 'native API calls completed; observed encoder output is reported separately'}
            self._requests[request_id] = (fingerprint, result)
            while len(self._requests) > 64: self._requests.popitem(last=False)
            return dict(result)

    async def feedback(self, client, value):
        stamp = value.get('timestampUs')
        generation = value.get('generation')
        if type(stamp) is not int or type(generation) is not int or generation != self.generation: return
        sent = client.sent_stamps.pop(stamp, None)
        if sent is None: return
        now = time.monotonic(); lag = max(0.0, now - sent)
        client.feedback_lag, client.last_feedback = lag, now
        self.feedback_count += 1
        if not self.effective['adaptive'] or now - self._last_adaptation < 1.0: return
        fresh = [c.feedback_lag for c in self.clients if c.feedback_lag is not None and now - c.last_feedback < 3]
        if not fresh: return
        ceiling = self.effective['bitrateKbps']
        low = min(500, ceiling)
        target = self.adaptive_bitrate
        if max(fresh) > .35: target = max(low, int(target * .85))
        elif max(fresh) < .12: target = min(ceiling, max(target + 100, int(target * 1.05)))
        if target == self.adaptive_bitrate: return
        async with self._config_lock:
            if generation != self.generation or self.reconfiguring or not self.effective['adaptive'] or self.capture is None: return
            target = min(self.effective['bitrateKbps'], max(low, target))
            if await asyncio.to_thread(self.capture.update_video_bitrate, target) is False:
                raise RuntimeError('native adaptive bitrate API rejected update')
            self.adaptive_bitrate = target; self._last_adaptation = now

    async def observe(self):
        capture = self.capture
        if capture is None or self.reconfiguring: return
        stats = await asyncio.to_thread(capture.stream_stats)
        geometry = await asyncio.to_thread(capture.get_realized_geometry, self.display)
        if capture is not self.capture or self.reconfiguring: return
        if geometry is not None:
            self.realized = tuple(geometry)
            self.measured.update(width=geometry[0], height=geometry[1])
        if not isinstance(stats, dict) or not all(type(stats.get(key)) is int for key in ('frames', 'bytes')): return
        now = time.monotonic()
        if self._observed is not None:
            before, frames, byte_count = self._observed
            elapsed = now - before
            if elapsed >= .2 and stats['frames'] >= frames and stats['bytes'] >= byte_count:
                self.measured.update(fps=(stats['frames'] - frames) / elapsed,
                    bitrateKbps=(stats['bytes'] - byte_count) * 8 / elapsed / 1000, sampleSeconds=elapsed)
        self._observed = (now, stats['frames'], stats['bytes'])

    async def websocket(self, request):
        from aiohttp import web, WSMsgType
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        if len(self.clients) >= 4:
            await ws.close(code=1013, message=b'viewer limit'); return ws
        client = Subscriber(); self.clients.add(client); self.sockets.add(ws); self.request_keyframe()
        async def send():
            announced = None
            while True:
                packet, _, codec = await client.queue.get()
                if not codec: continue
                generation = self.generation
                if self.reconfiguring: continue
                identity = (codec, self.generation, self.width, self.height, self.fps)
                if announced != identity:
                    await ws.send_json({'type': 'video', 'version': 1, 'session': self.session, 'codec': codec,
                        'width': self.width, 'height': self.height, 'fpsTarget': self.fps, 'generation': self.generation,
                        'effective': dict(self.effective), 'transport': 'H264 Annex-B'})
                    announced = identity
                if self.reconfiguring or generation != self.generation: continue
                await asyncio.wait_for(ws.send_bytes(packet), 3)
                client.sent += 1
                stamp = struct.unpack('!Q', packet[:8])[0]
                client.sent_stamps[stamp] = time.monotonic()
                while len(client.sent_stamps) > 2048: client.sent_stamps.popitem(last=False)
        sending = asyncio.create_task(send()); receiving = None
        try:
            while not ws.closed:
                receiving = asyncio.create_task(ws.receive())
                done, _ = await asyncio.wait({receiving, sending}, return_when=asyncio.FIRST_COMPLETED)
                if sending in done:
                    receiving.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await receiving
                    sending.result(); break
                message = receiving.result()
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR): break
                if message.type == WSMsgType.TEXT:
                    try:
                        value = json.loads(message.data)
                        if value.get('type') == 'keyframe': client.needs_key = True; self.request_keyframe()
                        elif value.get('type') == 'decoded' and type(value.get('frames')) is int:
                            client.decoded = max(0, min(value['frames'], client.sent))
                            await self.feedback(client, value)
                    except (ValueError, AttributeError): continue
        except (ConnectionError, asyncio.TimeoutError): pass
        finally:
            self.clients.discard(client); self.sockets.discard(ws)
            if receiving and not receiving.done():
                receiving.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception): await receiving
            sending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception): await sending
            await ws.close()
        return ws

    def snapshot(self):
        return {'captureView': self.display, 'capturing': bool(self.capture and self.capture.is_capturing),
                'generation': self.generation, 'effective': dict(self.effective), 'requestId': self.request_id,
                'updateMethod': self.update_method, 'reconfiguring': self.reconfiguring,
                'measured': dict(self.measured),
                'frames': self.frames, 'encodedBytes': self.bytes, 'nativeQueueDrops': self.dropped, 'codec': self.codec,
                'clients': len(self.clients), 'clientDecodedFrames': sum(c.decoded for c in self.clients),
                'clientQueueDrops': sum(c.dropped for c in self.clients), 'error': self.error,
                'adaptation': {'enabled': self.effective['adaptive'], 'bitrateKbps': self.adaptive_bitrate,
                               'ceilingKbps': self.effective['bitrateKbps'], 'validReceiverEchoes': self.feedback_count}}

    async def stop(self):
        await asyncio.gather(*(ws.close(code=1001, message=b'media stopping') for ws in tuple(self.sockets)), return_exceptions=True)
        await self.stop_capture()


class AudioChannel:
    def __init__(self, session: str, root: Path, runtime: Path | None = None):
        self.session, self.root = session, root
        self.runtime = runtime or root / ".runtime/m1"
        self.clients = set()
        self.sockets = set()
        self.frames = 0
        self.epoch = None
        self.error = None
        self.source_dropped = 0
        self._task = None

    async def poll(self):
        import aiohttp
        token_file = self.runtime / "control.token"
        if token_file.is_symlink() or not token_file.is_file() or token_file.stat().st_mode & 0o077:
            raise RuntimeError("M1 host token must be an owner-only regular file")
        token = token_file.read_text().strip()
        port = json.loads((self.runtime / "host.json").read_text())["controlPort"]
        if type(port) is not int or not 1024 <= port <= 65535:
            raise RuntimeError("invalid M1 host loopback control port")
        since = 0
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + token},
                                       timeout=aiohttp.ClientTimeout(total=2)) as http:
            while True:
                try:
                    query = {"sinceFrame": str(since), "maxFrames": "4800"}
                    if self.epoch is not None:
                        query["epoch"] = str(self.epoch)
                    async with http.get(f"http://127.0.0.1:{port}/session/{self.session}/audio", params=query) as response:
                        response.raise_for_status()
                        headers = response.headers
                        epoch = int(headers["X-MultiCraft-Audio-Epoch"])
                        start = int(headers["X-MultiCraft-Audio-Start-Frame"])
                        end = int(headers["X-MultiCraft-Audio-End-Frame"])
                        rate = int(headers["X-MultiCraft-Audio-Sample-Rate"])
                        dropped = int(headers.get("X-MultiCraft-Audio-Dropped-Frames", "0"))
                        expected = (end - start) * 4
                        if expected < 0 or expected > 19200:
                            raise ValueError("host PCM body exceeds contract")
                        data = await response.content.readexactly(expected)
                        if await response.content.read(1):
                            raise ValueError("host PCM body exceeds declared frame range")
                    if rate != 48000 or not 0 <= epoch <= 0xFFFFFFFF or start < 0 or end < start or len(data) != (end - start) * 4 or len(data) > 19200:
                        raise ValueError("invalid host PCM frame contract")
                    self.epoch, since, self.error = epoch, end, None
                    self.frames += end - start
                    self.source_dropped += dropped
                    if data:
                        packet = struct.pack("!IQ", epoch, start) + data
                        for client in self.clients:
                            if client.queue.full():
                                client.queue.get_nowait()
                                client.dropped += 1
                            client.queue.put_nowait(packet)
                except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, KeyError) as exc:
                    self.error = type(exc).__name__
                    await asyncio.sleep(.2)
                await asyncio.sleep(.02)

    async def websocket(self, request):
        from aiohttp import web, WSMsgType
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=4096)
        await ws.prepare(request)
        if len(self.clients) >= 4:
            await ws.close(code=1013, message=b"M1 viewer limit")
            return ws
        client = Subscriber(depth=4)
        self.clients.add(client)
        self.sockets.add(ws)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.poll())
        await ws.send_json({"type": "audio", "version": 1, "session": self.session,
                            "sampleRate": 48000, "channels": 2, "format": "s16le", "compression": "none"})

        async def send():
            while True:
                packet = await client.queue.get()
                await asyncio.wait_for(ws.send_bytes(packet), 3)
                client.sent += 1
        sending = asyncio.create_task(send())

        async def receive():
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
        receiving = asyncio.create_task(receive())
        try:
            finished, _ = await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                task.result()
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            self.clients.discard(client)
            self.sockets.discard(ws)
            sending.cancel()
            receiving.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sending
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiving
            await ws.close()
        return ws

    def snapshot(self):
        return {"source": "per-session OpenAL loopback", "format": "48000Hz stereo s16le uncompressed",
                "frames": self.frames, "epoch": self.epoch, "clients": len(self.clients),
                "sourceDroppedFrames": self.source_dropped, "error": self.error}

    async def stop(self):
        await asyncio.gather(*(ws.close(code=1001, message=b"M1 media stopping") for ws in tuple(self.sockets)), return_exceptions=True)
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
