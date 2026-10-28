import asyncio
from collections import OrderedDict
import time
import unittest
from multicraft.m1_streams import VideoChannel, Subscriber, ConfigConflict, ConfigApplyError


class FakeCapture:
    is_capturing = True
    def __init__(self): self.calls = []; self.fail_rate_once = False
    def update_framerate(self, value): self.calls.append(('fps', value))
    def update_video_bitrate(self, value):
        self.calls.append(('bitrate', value))
        if self.fail_rate_once:
            self.fail_rate_once = False
            raise RuntimeError('native refusal')
    def request_idr_frame(self): self.calls.append(('idr',))


class StreamTransactionTests(unittest.IsolatedAsyncioTestCase):
    def channel(self, name='slot1'):
        channel = VideoChannel(name, 1, 1280, 720, 60, 6000, '/dev/dri/renderD128')
        channel.capture = FakeCapture(); return channel

    async def test_live_config_ack_idempotence_and_conflict(self):
        channel = self.channel()
        value = dict(channel.effective, fps=90, bitrateKbps=8000)
        request = {'requestId': 'one', 'expectedGeneration': 0, 'config': value}
        result = await channel.configure(request)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(result['effective'], value)
        calls = list(channel.capture.calls)
        self.assertEqual(await channel.configure(request), result)
        self.assertEqual(channel.capture.calls, calls)
        with self.assertRaises(ConfigConflict): await channel.configure({**request, 'requestId': 'two'})
        with self.assertRaises(ConfigConflict): await channel.configure({**request, 'config': dict(value, fps=100)})
        await channel.configure({'requestId': 'newer', 'expectedGeneration': 1, 'config': dict(value, fps=100)})
        with self.assertRaises(ConfigConflict): await channel.configure(request)

    async def test_native_failure_rolls_back_without_advancing_generation_or_peer(self):
        a, b = self.channel(), self.channel('slot2')
        a.capture.fail_rate_once = True
        before = dict(a.effective)
        with self.assertRaises(ConfigApplyError) as error:
            await a.configure({'requestId': 'failure', 'expectedGeneration': 0, 'config': dict(before, fps=100, bitrateKbps=9000)})
        self.assertTrue(error.exception.rolled_back)
        self.assertEqual(a.effective, before); self.assertEqual(a.generation, 0)
        self.assertEqual(b.capture.calls, [])
        self.assertEqual(a.capture.calls[-2:], [('fps', 60.0), ('bitrate', 6000)])

    async def test_abr_uses_only_sent_timestamp_current_generation_and_bounds(self):
        channel = self.channel(); client = Subscriber(); channel.clients.add(client)
        before = channel.adaptive_bitrate
        await channel.feedback(client, {'generation': 0, 'timestampUs': 10})
        self.assertEqual(channel.feedback_count, 0)
        client.sent_stamps[10] = time.monotonic() - .5
        await channel.feedback(client, {'generation': 99, 'timestampUs': 10})
        self.assertEqual(channel.feedback_count, 0)
        await channel.feedback(client, {'generation': 0, 'timestampUs': 10})
        self.assertLess(channel.adaptive_bitrate, before)
        self.assertGreaterEqual(channel.adaptive_bitrate, 500)
        self.assertLessEqual(channel.adaptive_bitrate, channel.effective['bitrateKbps'])
        calls = list(channel.capture.calls)
        await channel.feedback(client, {'generation': 0, 'timestampUs': 10})
        self.assertEqual(channel.capture.calls, calls)
        self.assertEqual(channel.feedback_count, 1)

    async def test_stale_native_callback_and_partial_reconfig_frames_are_not_published(self):
        channel = self.channel(); client = Subscriber(); channel.clients.add(client)
        channel._capture_epoch = 2
        frame = b'\0\0\1\x67\x64\0\x28\0\0\1\x65x'
        channel.callback(frame, 1); await asyncio.sleep(0)
        self.assertTrue(client.queue.empty())
        channel.reconfiguring = True; channel.callback(frame, 2); await asyncio.sleep(0)
        self.assertTrue(client.queue.empty())
