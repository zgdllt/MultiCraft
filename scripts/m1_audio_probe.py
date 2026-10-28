#!/usr/bin/env python3
"""Measure real Minecraft A-only/B-only sounds through isolated M1 PCM WebSockets."""
from __future__ import annotations

import argparse
from array import array
import asyncio
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import uuid

from m1_acceptance import Probe


MAX_PHASE_BYTES = 768000  # four seconds of 48k stereo s16 PCM per session/phase
PHASES = ("baseline", "a-only", "b-only")
PLAYERS = {"A": "M1PlayerA", "B": "M1PlayerB"}
THRESHOLDS = {"silentRmsMaximum": 2.0, "targetRmsMinimum": 20.0,
              "rmsRatioMinimum": 20.0, "minimumSamplesPerWindow": 48000}


def summarize(data: bytes) -> dict:
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return {"samples": len(samples), "frames": len(data) // 4, "bytes": len(data),
            "rms": math.sqrt(sum(value * value for value in samples) / max(1, len(samples))),
            "peak": max(map(abs, samples), default=0), "sha256": hashlib.sha256(data).hexdigest()}


async def run_probe(root: Path, output: Path) -> dict:
    import aiohttp
    probe = Probe(root)
    before = await asyncio.to_thread(probe.state)
    if before.get("audioMode") != "loopback-pcm":
        raise ValueError("M1 loopback-pcm host required")
    if any(probe.session(before, sid).get("closed") or not probe.session(before, sid).get("playerIdentity") for sid in PLAYERS):
        raise ValueError("both M1 native players must be connected")
    report = {"schema": 1, "startedAt": time.time(), "scope": "real Minecraft sound packets through per-session OpenAL loopback and private PCM WebSockets",
              "thresholds": THRESHOLDS, "hostBefore": before, "phases": {}, "commands": [],
              "audioIsolationVerified": False, "browserPlaybackVerified": False}
    samples = {phase: {sid: bytearray() for sid in PLAYERS} for phase in PHASES}
    chunks = {phase: {sid: [] for sid in PLAYERS} for phase in PHASES}
    current_phase = None
    readers, sockets = [], []
    last = {}
    failures = []
    output.parent.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
    raw_dir = root / "evidence/audio" / run_id
    raw_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(raw_dir, 0o700)
    report["runId"] = run_id

    async def console(command):
        # Only the two explicit test identities are command targets.
        report["commands"].append({"command": command, "wallTime": time.time(), "monotonic": time.monotonic()})
        await asyncio.to_thread(probe.console, command)

    async def stop_sounds():
        for name in PLAYERS.values():
            await console("stopsound " + name)

    async def read(ws, sid):
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                hello = json.loads(message.data)
                if hello.get("session") != sid or hello.get("sampleRate") != 48000 or hello.get("channels") != 2 or hello.get("format") != "s16le":
                    raise ValueError("PCM WebSocket hello does not match session contract")
            elif message.type == aiohttp.WSMsgType.BINARY:
                data = message.data
                if len(data) < 12 or (len(data) - 12) % 4:
                    raise ValueError("invalid PCM WebSocket packet")
                epoch, start = struct.unpack("!IQ", data[:12])
                pcm, end = data[12:], start + (len(data) - 12) // 4
                prior = last.get(sid)
                gap = prior is not None and prior != (epoch, start)
                last[sid] = (epoch, end)
                phase = current_phase
                if phase is not None:
                    if len(samples[phase][sid]) + len(pcm) > MAX_PHASE_BYTES:
                        raise ValueError("bounded audio evidence window exceeded")
                    samples[phase][sid].extend(pcm)
                    chunks[phase][sid].append({"wallTime": time.time(), "monotonic": time.monotonic(),
                                               "epoch": epoch, "startFrame": start, "endFrame": end,
                                               "gapBefore": gap, "bytes": len(pcm)})
            else:
                raise RuntimeError("PCM WebSocket closed during isolation measurement")
        raise RuntimeError("PCM WebSocket ended during isolation measurement")

    async def window(phase, target=None):
        nonlocal current_phase
        await stop_sounds()
        await asyncio.sleep(.4)
        report["phases"][phase] = {"startedAt": time.time(), "monotonicStart": time.monotonic(), "target": target}
        current_phase = phase
        if target is not None:
            name = PLAYERS[target]
            command = f"execute at {name} run playsound minecraft:block.note_block.pling master {name} ~ ~ ~ 1 1 1"
            await console(command)
            await asyncio.sleep(.35)
            await console(command)
            await asyncio.sleep(1.25)
        else:
            await asyncio.sleep(1.0)
        current_phase = None
        report["phases"][phase].update(finishedAt=time.time(), monotonicEnd=time.monotonic())
        for task in readers:
            if task.done():
                task.result()

    try:
        for sid in PLAYERS:
            await asyncio.to_thread(probe.input, sid, [{"type": "release"}])
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(root / "media/control.sock"))) as client:
            for sid in PLAYERS:
                ws = await client.ws_connect("http://localhost/audio/" + sid, heartbeat=5)
                sockets.append(ws)
                readers.append(asyncio.create_task(read(ws, sid)))
            await asyncio.sleep(.3)
            await window("baseline")
            await window("a-only", "A")
            await window("b-only", "B")
            await stop_sounds()
            for task in readers:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for ws in sockets:
                await ws.close()
        after = await asyncio.to_thread(probe.state)
        report["hostAfter"] = after
        if (before["pid"], before["runtimeOwner"]) != (after["pid"], after["runtimeOwner"]):
            failures.append("host runtime generation changed during measurement")
        for sid in PLAYERS:
            for key in ("epoch", "device", "context"):
                if probe.session(before, sid)["audio"][key] != probe.session(after, sid)["audio"][key]:
                    failures.append(sid + ": audio " + key + " changed during measurement")
    except Exception as exc:
        failures.append(type(exc).__name__ + ": " + str(exc))
    finally:
        current_phase = None
        with contextlib.suppress(Exception):
            await stop_sounds()
        for ws in sockets:
            await ws.close()
        for task in readers:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for phase in PHASES:
            entry = report["phases"].setdefault(phase, {})
            entry["sessions"] = {}
            for sid in PLAYERS:
                data = bytes(samples[phase][sid])
                path = raw_dir / f"{phase}-{sid}.s16le"
                path.write_bytes(data)
                os.chmod(path, 0o600)
                entry["sessions"][sid] = {**summarize(data), "rawPcm": str(path), "chunks": chunks[phase][sid]}
                if len(data) // 2 < THRESHOLDS["minimumSamplesPerWindow"]:
                    failures.append(f"{phase}/{sid}: insufficient PCM samples")
                if any(chunk["gapBefore"] for chunk in chunks[phase][sid]):
                    failures.append(f"{phase}/{sid}: PCM sample-index discontinuity")
                expected_epoch = probe.session(before, sid)["audio"]["epoch"]
                if any(chunk["epoch"] != expected_epoch for chunk in chunks[phase][sid]):
                    failures.append(f"{phase}/{sid}: audio epoch does not match initial host snapshot")
                if entry["sessions"][sid]["rms"] > THRESHOLDS["silentRmsMaximum"] and (
                    phase == "baseline" or phase == "a-only" and sid == "B" or phase == "b-only" and sid == "A"):
                    failures.append(f"{phase}/{sid}: non-target sound exceeds silent threshold")
        for phase, target, other in (("a-only", "A", "B"), ("b-only", "B", "A")):
            measured = report["phases"][phase]["sessions"]
            ratio = measured[target]["rms"] / max(1.0, measured[other]["rms"])
            report["phases"][phase]["targetToOtherRmsRatio"] = ratio
            if measured[target]["rms"] < THRESHOLDS["targetRmsMinimum"] or ratio < THRESHOLDS["rmsRatioMinimum"]:
                failures.append(phase + ": target signal did not exceed required RMS/ratio")
        report.update(finishedAt=time.time(), audioIsolationVerified=not failures, failures=failures)
        archive = raw_dir / "report.json"
        archive.write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(archive, 0o600)
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/m1/audio-game-check.json"))
    args = parser.parse_args()
    root = Path.cwd().resolve() / ".runtime/m1"
    report = asyncio.run(run_probe(root, args.output.resolve()))
    print(json.dumps({"audioIsolationVerified": report["audioIsolationVerified"], "failures": report["failures"],
                      "windows": {phase: {sid: {k: v for k, v in data.items() if k in ("samples", "rms", "peak")} for sid, data in value["sessions"].items()} for phase, value in report["phases"].items()},
                      "output": str(args.output.resolve())}, indent=2))
    if not report["audioIsolationVerified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
