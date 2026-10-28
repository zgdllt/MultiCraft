#!/usr/bin/env python3
"""Native pool evidence check. Mutating input probes require explicit --exercise.

Writes only a redacted aggregate summary by default. It does not start services,
change a server, accept an EULA, or publish player identities/positions.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from multicraft.pool_config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PROJECT / 'multicraft.json')
    parser.add_argument('--min-sessions', type=int, default=4)
    parser.add_argument('--exercise', action='store_true', help='send bounded movement and inventory input to each configured slot')
    parser.add_argument('--summary-output', type=Path)
    args = parser.parse_args()
    config = load_config(args.config); runtime = Path(config['runtimeDirectory'])
    token = (runtime / 'control.token').read_text().strip()
    base = 'http://127.0.0.1:' + str(config['controlPort'])
    def request(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response: return json.load(response)
    checks = []
    def check(name, passed): checks.append({'check': name, 'passed': bool(passed)})
    pinned = None
    def state():
        value = request('/state')
        if pinned is not None and (value.get('runtimeOwner'), value.get('pid')) != pinned:
            raise RuntimeError('native runtime generation changed; acceptance will not retarget another process')
        return value
    def active(snapshot): return [s for s in snapshot['sessions'] if not s['closed']]
    first = state(); sessions = active(first)
    pinned = (first.get('runtimeOwner'), first.get('pid'))
    epochs = {session['id']: session.get('connectionEpoch') for session in sessions}
    configured = {session['id']: session['uuid'] for session in config['sessions']}
    check('configured-pool-count-and-identities', len(sessions) == len(configured) >= args.min_sessions
          and {session['id'] for session in sessions} == set(configured)
          and all(session.get('uuid') == configured.get(session['id']) for session in sessions))
    check('one-native-process-and-owner-thread', all(s.get('gameThreadId') == first['ownerThreadId'] for s in sessions))
    for key in ('uuid', 'minecraftIdentity', 'playerIdentity', 'levelIdentity', 'guiIdentity', 'inventoryIdentity', 'connectionIdentity', 'windowId'):
        values = [s.get(key) for s in sessions]
        check('independent-' + key, None not in values and len(set(values)) == len(sessions))
    for key in ('nativeInstance', 'nativeDevice', 'nativeSurface', 'nativeSwapchain'):
        values = [s.get('graphics', {}).get(key) for s in sessions]
        check('independent-' + key, all(isinstance(value, str) and value.isascii() and value.isdigit() and int(value)>0 for value in values)
              and len(set(values)) == len(sessions))
    check('all-real-network-connections', all(s.get('networkConnected') for s in sessions))
    check('all-NVIDIA-Vulkan', all(s.get('graphics', {}).get('vendorName') == 'NVIDIA' and s.get('graphics', {}).get('backendName') == 'Vulkan' for s in sessions))
    check('all-real-display-targets', all(s['display']['targetDisplayId'] == s['display']['actualDisplayId'] for s in sessions))
    check('all-loopback-contexts-independent', len({s.get('audio', {}).get('context') for s in sessions}) == len(sessions)
          and all(s.get('audio', {}).get('active') for s in sessions))
    time.sleep(1)
    second = state()
    by_id = {s['id']: s for s in active(second)}
    check('all-native-frames-advance', all(by_id.get(s['id'], {}).get('frames', 0) > s['frames'] for s in sessions))
    if args.exercise and all(item['passed'] for item in checks):
        def send(slot, events):
            snapshot = state(); item = next(s for s in snapshot['sessions'] if s['id'] == slot)
            if item.get('connectionEpoch') != epochs[slot]:
                raise RuntimeError('connection generation changed; input probe will not retarget a new connection')
            return request('/session/' + slot + '/input', {'expectedRuntimeOwner': snapshot['runtimeOwner'],
                'expectedEpoch': item['connectionEpoch'], 'inputSeq': item['lastInputSeq'] + 1, 'events': events})
        for index, original in enumerate(sessions):
            slot = original['id']
            before = state(); own = next(s for s in before['sessions'] if s['id'] == slot)
            if own.get('screen') is not None:
                check('slot-' + str(index + 1) + '-exercise-started-without-menu', False); continue
            try:
                send(slot, [{'type': 'grab'}, {'type': 'key', 'scancode': 26, 'keycode': 119, 'action': 1}])
                time.sleep(.25)
            finally:
                send(slot, [{'type': 'key', 'scancode': 26, 'keycode': 119, 'action': 0}, {'type': 'release'}])
            moved = next(s for s in state()['sessions'] if s['id'] == slot)
            check('slot-' + str(index + 1) + '-real-movement', abs(moved['x'] - own['x']) + abs(moved['z'] - own['z']) > .02)
            peers = {s['id']: s.get('screen') for s in before['sessions'] if s['id'] != slot}
            try:
                send(slot, [{'type': 'key', 'scancode': 8, 'keycode': 101, 'action': 1}, {'type': 'key', 'scancode': 8, 'keycode': 101, 'action': 0}])
                time.sleep(.15)
                menu = state(); own_menu = next(s for s in menu['sessions'] if s['id'] == slot)
                check('slot-' + str(index + 1) + '-inventory-isolated',
                      (own_menu.get('screen') or '').endswith('InventoryScreen') and all(s.get('screen') == peers[s['id']] for s in menu['sessions'] if s['id'] in peers))
            finally:
                current = next(s for s in state()['sessions'] if s['id'] == slot)
                if (current.get('screen') or '').endswith('InventoryScreen'):
                    send(slot, [{'type': 'key', 'scancode': 41, 'keycode': 27, 'action': 1}, {'type': 'key', 'scancode': 41, 'keycode': 27, 'action': 0}])
                send(slot, [{'type': 'key', 'scancode': 8, 'keycode': 101, 'action': 0}, {'type': 'release'}])
    summary = {'schema': 1, 'kind': 'native-pool-aggregate-check', 'sessionCount': len(sessions), 'mutatingInputProbe': args.exercise,
        'passed': all(item['passed'] for item in checks), 'checks': checks,
        'boundaries': {'browserDecodingTested': False, 'userAudibilityTested': False, 'graphicsResourceSharingClaimed': False,
                       'processShutdownTested': False, 'nativeSDLQuitSafetyClaimed': False}}
    output = args.summary_output or runtime / 'acceptance-summary.json'
    output.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    if not summary['passed']: raise SystemExit(1)


if __name__ == '__main__': main()
