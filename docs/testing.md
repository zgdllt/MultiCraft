# Verification and release evidence

Different checks answer different questions. Unit tests establish contracts,
not GPU/native gameplay success. A running JVM does not prove that every client
renders. Encoded frames do not prove browser decoding, and decoded PCM does not
prove the user's speakers played it. Keep those evidence boundaries explicit.

## CPU-only source checks

Run from the repository root using a pre-provisioned Python environment with
`requirements-test.txt` installed and Node 20+:

```sh
python3 -m unittest discover -s tests -v
npm test
npm run check:web
python3 scripts/release_source.py
```

CI performs these source/contract checks on Linux. It does not install a game
cache, create a virtual GPU display, start a Minecraft server or run real clients.
The release-source checker takes a positive file allowlist, rejects private
artifacts/symlinks and common credential/deployment leaks, and can produce a
reproducible source-only archive:

```sh
python3 scripts/release_source.py --archive .runtime/release/MultiCraft-source.tar.gz
```

Review the generated `SOURCE-MANIFEST.json` inside the archive and the Git index
before publication. The checker is deliberately narrow; no static scanner can
prove that every possible secret is absent.

## Native adapter checks

Build the owned host using an existing JDK 25 and a verified official private
cache. The transform check verifies the fixed-version in-memory adaptation;
`GraphicsCheck` checks native loader/device selection without creating complete
game clients; `AudioCheck` generates isolated synthetic loopback signals. See
[runtime-host/README.md](../runtime-host/README.md). Passing those checks is a
prerequisite, not a replacement for the live tests below.

## Real N-session pool

Use an isolated runtime directory, server/world and ports. Never point an
acceptance probe at someone else's active server or an existing gameplay world.
Record software versions, source/JAR hashes and the pool owner generation. Use
at least **four actual native clients** for the public multi-instance path,
then report the exact tested count; larger supported configuration counts are
not inferred capacity results.

| Check | Required observation |
| --- | --- |
| One process | All tested original clients are registered in the same live JVM/owner generation. |
| Real native rendering | Every realm reports its actual NVIDIA Vulkan device, distinct native objects, correct physical display and increasing original game frame counters. |
| Gameplay isolation | One client moves/turns while peers remain still. One can open inventory or pause menu while others keep their own screens. |
| Protocol isolation | Distinct configured usernames/UUIDs connect as separate actual server players; one disconnect leaves other connections live. |
| Configurable N | A fresh four-slot startup works; parsing/validation separately checks allowed counts and duplicate IDs/display/account ownership. |
| Lifecycle | Close an early slot while later slots continue native frames/input/audio, then test a late slot close. Full shutdown produces actual cleanup and process exit receipts. |
| Persistence | A normal disconnect/reconnect changes that slot's connection epoch without reusing stale input ownership. A terminally closed slot is reported closed until pool restart. |

When testing stream changes, verify the owning output's realized dimensions and
new browser decoder configuration. Check that peers' geometry and generation do
not change. Changing only a requested JSON value is not a successful resize.

## Real product browser tests

Use the public single-player layout, not the dual-panel engineering console.
Open four independent identity namespaces in different browser tabs and sign in
as four different registered identities. Record only a redacted result summary
in public documentation; keep authentication artifacts private.

1. Verify each tab shows its own identity and one game picture. Reload and log
   out/re-authenticate one tab; peers must retain their identities and streams.
2. Move or turn one actual player and query/observe the others remain still.
   Open a menu in one and inventory in another, using browser input.
3. Request pointer lock in a window without requesting fullscreen. Verify
   continuous first-person turning, F8 release, then separately enter/leave
   fullscreen. On a GUI screen, point at several buttons and verify hover and
   click align at multiple CSS sizes/aspect ratios.
4. Change one user's resolution, fps, video bitrate and adaptive setting. Observe
   request ID/generation acknowledgement, realized dimensions, actual decoded
   frames and peer isolation. Exercise failure and rollback reporting.
5. As OP lower stream limits so several active users need changes. Verify every
   affected backend acknowledges before the persisted policy revision advances;
   a failed update must not silently commit a partially applied policy.
6. Deliver genuine targeted game sounds to each identity while peers are quiet.
   Measure native/transport isolation, confirm browser PCM decoding and separately
   ask a human whether the expected sound is audible. Do not invent a speaker
   confirmation from a WebAudio counter.
7. Disconnect one tab while holding a key, then reconnect. Verify no stuck input,
   no stale socket control, and continued peer video/audio.

For autoplay-restricted audio or a browser security prompt, human input may be
required. Record that limitation and the actual result instead of treating it
as a passed automated check.

## What to publish

A release summary may state the exact real host platform, tested session count,
version pins, passed check categories and known limitations. Keep original crash
reports and private runtime receipts recoverable locally. Do not publish raw
account registries, cookie headers, native-control tokens, TLS keys or full
operator filesystem paths. Test failure history must not be rewritten into a
claim of universal platform support.

The alpha release must not claim Microsoft account login, arbitrary mod/shader
support, scene/GPU resource sharing, ray tracing, end-to-end photon latency,
16-player capacity or energy savings without separate direct evidence.
