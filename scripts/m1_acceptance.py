#!/usr/bin/env python3
"""Exercise only the owned M1 world and native host; emit bounded evidence."""
from __future__ import annotations

import argparse
from array import array
import http.client
import json
import math
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.request

from native_session import process_owned, read_record, server_command


class Probe:
    def __init__(self, root: Path):
        self.root = root
        self.configuration = json.loads((root / "host.json").read_text())
        self.token = (root / "control.token").read_text().strip()
        self.url = "http://127.0.0.1:" + str(self.configuration["controlPort"])
        self.report = {"schema": 1, "startedAt": time.time(), "m1Complete": False,
                       "graphicsSharing": False, "checks": [], "snapshots": {}}
        if root != Path.cwd().resolve() / ".runtime" / "m1":
            raise ValueError("only the exact isolated M1 runtime is supported")
        for role in ("host", "server"):
            record = read_record(root, role)
            if not record or not process_owned(record):
                raise ValueError("the owned M1 host and server must both be running")
        properties = (root / "server" / "server.properties").read_text()
        if "level-name=m1-test-world\n" not in properties or "server-ip=127.0.0.1\n" not in properties:
            raise ValueError("M1 acceptance refuses any other world/server")
        if any(session["serverAddress"] != "127.0.0.1:25586" for session in self.configuration["sessions"]):
            raise ValueError("M1 acceptance only mutates its fixed test port")

    def rpc(self, path: str, value=None, *, raw=False):
        data = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(self.url + path, data=data, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return (dict(response.headers), body) if raw else json.loads(body)

    def state(self):
        return self.rpc("/state")

    @staticmethod
    def session(state, name):
        return next(session for session in state["sessions"] if session["id"] == name)

    def wait(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            state = self.state()
            if condition(state):
                return state
            if time.monotonic() >= deadline:
                raise AssertionError("native state did not reach the required condition")
            time.sleep(.1)

    def input(self, name, events):
        state = self.state()
        session = self.session(state, name)
        value = {"events": events}
        if "connectionEpoch" in session:
            value.update(expectedEpoch=session["connectionEpoch"], inputSeq=session["lastInputSeq"] + 1)
        if "runtimeOwner" in state:
            value["expectedRuntimeOwner"] = state["runtimeOwner"]
        return self.rpc(f"/session/{name}/input", value)

    def key(self, name, scan, keycode):
        return self.input(name, [{"type": "key", "scancode": scan, "keycode": keycode, "action": action}
                                 for action in (1, 0)])

    def command(self, name, command):
        state = self.state()
        value = {"type": command}
        if "runtimeOwner" in state:
            value["expectedRuntimeOwner"] = state["runtimeOwner"]
        session = self.session(state, name)
        if "connectionEpoch" in session:
            value["expectedEpoch"] = session["connectionEpoch"]
        return self.rpc(f"/session/{name}/command", value)

    def console(self, command):
        server_command(self.root, command)

    def record(self, name, condition, **evidence):
        self.report["checks"].append({"name": name, "passed": bool(condition), **evidence})
        if not condition:
            raise AssertionError(name)

    def save(self, name):
        state = self.state()
        self.report["snapshots"][name] = state
        return state

    def settle(self, name, timeout=5):
        previous = self.state()
        stable = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(.1)
            current = self.state()
            first, last = self.session(previous, name), self.session(current, name)
            stable = stable + 1 if max(abs(last[axis] - first[axis]) for axis in ("x", "y", "z")) < .002 else 0
            if stable >= 4:
                return current
            previous = current
        raise AssertionError("native player momentum did not settle")

    def capture(self, label):
        for name, output in (("A", 0), ("B", 2)):
            connection = http.client.HTTPConnection("localhost", timeout=10)
            connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.sock.settimeout(10)
            try:
                connection.sock.connect(str(self.root / "media/control.sock"))
                connection.request("GET", "/screenshot/" + str(output))
                response = connection.getresponse()
                data = response.read(8 * 1024 * 1024)
                if response.status != 200 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise AssertionError("M1 native output capture failed")
                destination = self.root / "evidence" / (label + "-" + name + ".png")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            finally:
                connection.close()

    def fixture(self):
        for command in ("gamerule minecraft:keep_inventory true", "gamerule minecraft:advance_time false",
                        "gamerule minecraft:spawn_mobs false", "time set day", "weather clear",
                        "fill -8 100 -8 20 100 20 minecraft:stone", "fill -8 101 -8 20 105 20 minecraft:air",
                        "gamemode survival @a", "clear M1PlayerA", "clear M1PlayerB",
                        "give M1PlayerA minecraft:diamond 3", "give M1PlayerB minecraft:emerald 7",
                        "tp M1PlayerA 0.5 101 0.5 0 0", "tp M1PlayerB 8.5 101 0.5 0 0"):
            self.console(command)
        self.wait(lambda state: abs(self.session(state, "A").get("y", 0) - 101) < .1 and
                  abs(self.session(state, "B").get("x", 0) - 8.5) < .1)

    def core(self):
        initial = self.wait(lambda state: all(session.get("playerIdentity") for session in state["sessions"]))
        a, b = (self.session(initial, name) for name in ("A", "B"))
        self.record("one JVM and one native owner thread", len(initial["sessions"]) == 2 and
                    a["gameThreadId"] == b["gameThreadId"] == initial["ownerThreadId"], pid=initial["pid"])
        for name, session in (("A", a), ("B", b)):
            graphics = session.get("graphics", {})
            self.record(name + " uses native NVIDIA Vulkan", graphics.get("backendName") == "Vulkan" and
                        graphics.get("vendorName") == "NVIDIA", graphics=graphics)
            self.record(name + " has a real connected native channel", session.get("networkConnected") is True)
            display = session.get("display", {})
            self.record(name + " owns its configured native display", display.get("targetDisplayId") ==
                        display.get("actualDisplayId") and bool(display.get("targetDisplayId")), display=display)
            self.record(name + " actually renders near the configured 60 fps", 50 <= session.get("nativeFps", 0) <= 70 and
                        session.get("framerate", {}).get("window", {}).get("iconified") is False,
                        observedFps=session.get("nativeFps"))
        for field in ("nativeInstance", "nativeDevice", "nativeSurface", "nativeSwapchain"):
            values = [str(session["graphics"].get(field, "")) for session in (a, b)]
            self.record("independent live " + field, all(value.isdigit() and int(value) > 0 for value in values)
                        and values[0] != values[1])
        for field in ("minecraftIdentity", "windowId", "playerIdentity", "levelIdentity", "guiIdentity",
                      "inventoryIdentity", "containerIdentity", "connectionIdentity", "uuid"):
            self.record("independent " + field, a[field] != b[field])
        for name in ("A", "B"):
            if self.session(self.state(), name).get("health", 0) <= 0:
                self.command(name, "respawn")
                self.wait(lambda state: self.session(state, name).get("health", 0) == 20)
        self.fixture()
        for name in ("A", "B"):
            self.input(name, [{"type": "grab"}])
        before = self.save("before-input")
        for moving, still in (("A", "B"), ("B", "A")):
            start = self.state()
            self.input(moving, [{"type": "key", "scancode": 26, "keycode": 119, "action": 1}])
            try:
                changed = self.wait(lambda state: abs(self.session(state, moving)["z"] -
                                                     self.session(start, moving)["z"]) > .8, timeout=5)
            finally:
                self.input(moving, [{"type": "key", "scancode": 26, "keycode": 119, "action": 0}])
            self.record(moving + " movement does not move " + still,
                        abs(self.session(changed, still)["z"] - self.session(start, still)["z"]) < .01)
            self.settle(moving)
        start = self.state()
        # Vanilla deliberately discards the first cursor motion after capture.
        self.input("A", [{"type": "move", "x": 640, "y": 360, "dx": 0, "dy": 0},
                         {"type": "move", "x": 760, "y": 360, "dx": 120, "dy": 0}])
        changed = self.wait(lambda state: abs(self.session(state, "A")["yaw"] - self.session(start, "A")["yaw"]) > 1)
        self.record("A camera does not rotate B", abs(self.session(changed, "B")["yaw"] - self.session(start, "B")["yaw"]) < .01)
        self.key("A", 8, 101)
        inventory = self.wait(lambda state: "InventoryScreen" in str(self.session(state, "A").get("screen")))
        self.record("A inventory leaves B in world", self.session(inventory, "B").get("screen") is None)
        self.report["snapshots"]["A-inventory-B-world"] = inventory
        self.capture("A-inventory-B-world")
        self.key("A", 41, 27)
        self.wait(lambda state: self.session(state, "A").get("screen") is None)
        self.key("B", 8, 101)
        self.wait(lambda state: "InventoryScreen" in str(self.session(state, "B").get("screen")))
        self.capture("B-inventory-A-world")
        self.key("B", 41, 27)
        self.wait(lambda state: self.session(state, "B").get("screen") is None)
        self.key("B", 41, 27)
        paused = self.wait(lambda state: "PauseScreen" in str(self.session(state, "B").get("screen")))
        self.record("B pause leaves A in world", self.session(paused, "A").get("screen") is None)
        self.key("B", 41, 27)
        self.wait(lambda state: self.session(state, "B").get("screen") is None)
        self.console("execute in minecraft:the_nether run forceload add 0 0")
        time.sleep(.5)
        self.console("execute in minecraft:the_nether run fill -4 100 -4 4 100 4 minecraft:stone")
        self.console("execute in minecraft:the_nether run fill -4 101 -4 4 104 4 minecraft:air")
        self.console("execute in minecraft:the_nether run tp M1PlayerB 0.5 101 0.5")
        split = self.wait(lambda state: "the_nether" in self.session(state, "B").get("dimension", ""))
        self.record("different native dimensions", "overworld" in self.session(split, "A")["dimension"])
        self.report["snapshots"]["different-dimensions"] = split
        self.capture("different-dimensions")
        self.console("execute in minecraft:overworld run tp M1PlayerB 8.5 101 0.5 0 0")
        self.wait(lambda state: "overworld" in self.session(state, "B").get("dimension", ""))
        before_disconnect = self.save("before-disconnect")
        server_log = self.root / "server.console.log"
        disconnect_offset = server_log.stat().st_size
        self.command("A", "disconnect")
        disconnected = self.wait(lambda state: self.session(state, "A").get("playerIdentity") is None)
        continued = self.wait(lambda state: self.session(state, "B")["frames"] > self.session(disconnected, "B")["frames"] + 10)
        self.record("A disconnect preserves B connection and frames", self.session(continued, "B")["connectionIdentity"] ==
                    self.session(before_disconnect, "B")["connectionIdentity"])
        deadline = time.monotonic() + 10
        disconnect_log = ""
        while time.monotonic() < deadline:
            with server_log.open("rb") as stream:
                stream.seek(disconnect_offset)
                disconnect_log = stream.read(65536).decode(errors="replace")
            if "M1PlayerA lost connection:" in disconnect_log:
                break
            time.sleep(.1)
        self.record("A disconnect closes its real TCP connection before reconnect",
                    "M1PlayerA lost connection:" in disconnect_log and "another location" not in disconnect_log,
                    serverLog=disconnect_log.strip())
        self.command("A", "connect")
        connected = self.wait(lambda state: self.session(state, "A").get("playerIdentity") is not None)
        self.record("A reconnect creates a new native connection", self.session(connected, "A")["connectionIdentity"] !=
                    self.session(before_disconnect, "A")["connectionIdentity"])
        if "runtimeOwner" in connected:
            for key, value in (("expectedRuntimeOwner", "previous-runtime"),
                               ("expectedEpoch", self.session(before_disconnect, "A")["connectionEpoch"])):
                payload = {"events": [{"type": "key", "scancode": 26, "keycode": 119, "action": 1}],
                           "expectedRuntimeOwner": connected["runtimeOwner"],
                           "expectedEpoch": self.session(connected, "A")["connectionEpoch"],
                           "inputSeq": self.session(connected, "A")["lastInputSeq"] + 100}
                payload[key] = value
                rejected = False
                try:
                    self.rpc("/session/A/input", payload)
                except urllib.error.HTTPError as error:
                    rejected = error.code == 400
                self.record("reject stale " + key, rejected)
            for command in ("disconnect", "respawn"):
                before_command = self.state()
                rejected = False
                try:
                    self.rpc("/session/A/command", {"type": command,
                             "expectedRuntimeOwner": connected["runtimeOwner"],
                             "expectedEpoch": self.session(before_disconnect, "A")["connectionEpoch"]})
                except urllib.error.HTTPError as error:
                    rejected = error.code == 400
                after_command = self.state()
                self.record("reject stale command " + command, rejected and
                            self.session(before_command, "A")["connectionIdentity"] ==
                            self.session(after_command, "A")["connectionIdentity"])
        # The real server grants a freshly logged-in player a short immunity
        # window. Do not mistake an ignored /kill during that window for a UI bug.
        time.sleep(3.5)
        self.console("kill M1PlayerA")
        self.wait(lambda state: "DeathScreen" in str(self.session(state, "A").get("screen")))
        self.command("A", "respawn")
        respawn = self.wait(lambda state: self.session(state, "A").get("health", 0) == 20 and
                            "DeathScreen" not in str(self.session(state, "A").get("screen")))
        self.record("A native respawn preserves B", self.session(respawn, "B")["connectionIdentity"] ==
                    self.session(before_disconnect, "B")["connectionIdentity"])
        self.save("after-core")

    def audio_samples(self, seconds=1.0):
        samples = {"A": array("h"), "B": array("h")}
        cursors = {}
        for name in samples:
            state = self.rpc(f"/session/{name}/audio-state")
            cursors[name] = (state["epoch"], state["producedFrames"])
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for name, (epoch, since) in list(cursors.items()):
                headers, data = self.rpc(f"/session/{name}/audio?sinceFrame={since}&epoch={epoch}&maxFrames=4800", raw=True)
                block = array("h")
                block.frombytes(data)
                if sys.byteorder != "little":
                    block.byteswap()
                samples[name].extend(block)
                cursors[name] = (int(headers["X-multicraft-audio-epoch"]), int(headers["X-multicraft-audio-end-frame"]))
            time.sleep(.01)
        return {name: {"sampleCount": len(data), "rms": math.sqrt(sum(value * value for value in data) / max(1, len(data))),
                       "peak": max(map(abs, data), default=0)} for name, data in samples.items()}

    def close_one(self, closing="A"):
        survivor = "B" if closing == "A" else "A"
        before = self.save("before-close-" + closing)
        self.command(closing, "close")
        closed = self.wait(lambda state: self.session(state, closing)["closed"])
        after = self.wait(lambda state: self.session(state, survivor)["frames"] > self.session(closed, survivor)["frames"] + 30)
        self.record("close " + closing + " preserves " + survivor + " and host PID", before["pid"] == after["pid"] and
                    self.session(after, survivor)["connectionIdentity"] == self.session(before, survivor)["connectionIdentity"])
        self.input(survivor, [{"type": "grab"}, {"type": "key", "scancode": 26, "keycode": 119, "action": 1}])
        try:
            moved = self.wait(lambda state: abs(self.session(state, survivor)["z"] - self.session(after, survivor)["z"]) > .5)
        finally:
            self.input(survivor, [{"type": "key", "scancode": 26, "keycode": 119, "action": 0}])
        self.record(survivor + " input still works after " + closing + " closes", self.session(moved, survivor)["connectionIdentity"] ==
                    self.session(before, survivor)["connectionIdentity"])
        if before["audioMode"] == "loopback-pcm":
            first = self.session(before, survivor)["audio"]
            last = self.session(moved, survivor)["audio"]
            self.record(survivor + " native audio continues after " + closing + " closes", last["active"] and not last.get("error") and
                        last["epoch"] == first["epoch"] and last["producedFrames"] > first["producedFrames"])
        self.save("after-close-" + closing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("core", "close"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--closing-session", choices=("A", "B"), default="A")
    args = parser.parse_args()
    probe = Probe(Path.cwd().resolve() / ".runtime/m1")
    try:
        if args.suite == "close":
            probe.close_one(args.closing_session)
        else:
            probe.core()
        probe.report["suitePassed"] = True
    except Exception as error:
        probe.report.update(suitePassed=False, error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        for name in ("A", "B"):
            try:
                if not probe.session(probe.state(), name)["closed"]:
                    probe.input(name, [{"type": "release"}])
            except Exception:
                pass
        probe.report["finishedAt"] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(probe.report, indent=2) + "\n")
        print(json.dumps({"suite": args.suite, "passed": probe.report.get("suitePassed"),
                          "checks": len(probe.report["checks"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
