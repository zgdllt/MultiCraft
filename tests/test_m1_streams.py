import asyncio
import struct
import unittest

from multicraft.m1_streams import Subscriber, VideoChannel, annexb_metadata


IDR = b"\x00\x00\x00\x01\x67\x64\x00\x28\x01\x00\x00\x01\x65\x88\x01"
DELTA = b"\x00\x00\x01\x41\x10\x01"


class M1FrameTests(unittest.IsolatedAsyncioTestCase):
    def test_annexb_sps_codec_and_actual_idr_are_distinguished(self):
        self.assertEqual(annexb_metadata(IDR), (True, "avc1.640028"))
        self.assertEqual(annexb_metadata(DELTA), (False, None))
        self.assertEqual(annexb_metadata(b"\x00\x00\x01\x67\x64"), (False, None))

    async def test_new_viewer_and_overflow_recover_only_at_keyframe(self):
        channel = VideoChannel("A", 1, 1280, 720, 60, 6000, "/dev/dri/renderD128")
        client = Subscriber(depth=2)
        channel.offer(client, b"delta", False)
        self.assertTrue(client.queue.empty())
        channel.offer(client, b"key", True)
        channel.offer(client, b"delta1", False)
        channel.offer(client, b"delta2", False)
        self.assertTrue(client.queue.empty())
        self.assertTrue(client.needs_key)
        channel.offer(client, b"key2", True)
        self.assertEqual(client.queue.get_nowait()[0], b"key2")
        self.assertEqual(client.dropped, 2)

    async def test_session_fanout_and_native_callback_gap_are_isolated(self):
        a = VideoChannel("A", 1, 1280, 720, 60, 6000, "/dev/dri/renderD128")
        b = VideoChannel("B", 3, 1280, 720, 60, 6000, "/dev/dri/renderD128")
        ca, cb = Subscriber(), Subscriber()
        a.clients.add(ca); b.clients.add(cb)
        a.callback(IDR)
        await asyncio.sleep(0)
        packet, key, codec = ca.queue.get_nowait()
        self.assertTrue(key)
        self.assertEqual(codec, "avc1.640028")
        self.assertEqual(packet[9:], IDR)
        self.assertGreater(struct.unpack("!Q", packet[:8])[0], 0)
        self.assertTrue(cb.queue.empty())
        # If a busy loop skips a native AU, no dependent delta may be sent.
        a.callback(DELTA); a.callback(DELTA)
        await asyncio.sleep(0)
        self.assertTrue(ca.needs_key)
        self.assertTrue(ca.queue.empty())
        self.assertEqual(a.dropped, 1)


if __name__ == "__main__":
    unittest.main()
