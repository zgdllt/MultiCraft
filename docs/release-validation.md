# v0.1.0-alpha.1 validation record

This page records the release exercise separately from the feature list and the
[general test procedure](testing.md). It deliberately retains failed runs and
separates confirmed behavior from unresolved limits. Raw receipts, authentication
artifacts, runtime paths and native crash reports remain private; this document
contains only aggregate measurements.

## Scope

The native integration uses the fixed Minecraft Java **26.3** client, JDK **25**,
and the pinned third-party capture stack on an Ubuntu / NVIDIA Vulkan host.
**Four real original clients** were exercised in the same JVM, with independent
native game connections, viewports and loopback audio. Browser checks use the
public single-player UI in four independently authenticated tabs, rather than
the earlier two-panel diagnostic page.

The public configuration accepts 1–16 startup slots and defaults to four. Four
is the live integration count recorded here. No 16-client capacity, cross-view
GPU resource sharing, ray tracing or energy-saving measurement is inferred.

## Confirmed observations

| Exercise | Observed result | Evidence boundary |
| --- | --- | --- |
| Private cache and preparation | The external game cache was fully hash-verified read-only; four independent writable clients and the owned host JAR were prepared. | Preparation does not establish gameplay or browser playback. |
| Four-client native integration | A four-client run performed **53 checks**: its functional identity, per-player movement, independent GUI and four real PCM checks passed, while its 60 fps baseline check failed. | This run's original 60 fps performance baseline did not pass; see below. |
| Actual audio isolation | Targeted real game audio was separated across the four native loopback/PCM streams. | Native/transport isolation is separate from actual browser-speaker confirmation. |
| One-slot resize and stream settings | A separate resize exercise completed **32 checks**. The changed slot's native render and capture both became **960×540**, with a **4500 kbps target** and `adaptive=false`; observed encoded rate was approximately **44.92 fps**. | A configured bitrate target is not a measurement of exact achieved wire bitrate. |
| Peer isolation during resize | Peers retained **1280×720**, approximately **40 native fps** and **60 encoded fps**, with continuing streams; settings were then restored. | Unchanged peers show configuration isolation, not shared rendering or equal native/encoder update rates. |
| Four browser identities | Four actual in-app-browser tabs signed in using local WebCrypto signatures and explicit account selection. Titles remained distinct; later logins did not overwrite the first OP identity. Only the configured `asta` account had OP controls. | This exercises real browser authentication and tab isolation, not only HTTPS requests. |
| Four browser decoders and views | Each of the four real tabs decoded more than **7000 canvas frames** and displayed its own game view. | Encoded/sent counters were not substituted for browser decoding. The parent-page statistics routing issue is tracked separately below. |
| Browser menu isolation | Using the public F9/menu button opened `PauseScreen` only in the selected first slot while the other three remained in-world; the menu was then closed. | This proves one real browser input path and menu isolation, not every keyboard/mouse feature. |
| Cookie compatibility fix | Browser login exposed a collision with the older M0 service's root-path cookie names on the same hostname across ports. The release gateway now uses distinct cookie names as well as per-tab paths; the gateway regression suite reached **13 passing tests**. | Existing M0 login state was preserved. Cookie paths partition login state, not browser origins. |

The measured difference between native FPS and encoded FPS matters: an encoder
may send repeated pictures at its requested rate while the original game creates
new frames more slowly. Both quantities remain visible rather than being
reported as one performance number.

## Failed trials and fixes

**Initial four-client startup failed.** SDL display enumeration order did not
match logical slot order. The adapter now finds the actual native display from
the configured output's bounds/origin and verifies the real display ID, instead
of assuming enumeration order is stable. A later four-client startup succeeded.

The failed startup also exposed a native driver/pipeline-compilation cleanup
race while aborting partially initialized GPU clients. **That early-startup
abort path remains unresolved.** Preserve the crash report and inspect the
pool's actual process state after such a failure. Successful steady-state runs
or the normal `process-exit` shutdown policy do not prove that this initialization
failure path is safe.

**The four-client 60 fps native baseline failed.** At a 60 fps target, the run
measured approximately **39.79 native fps**, while the encoders
continued around 60 fps. The failed performance receipt was retained. That mixed functional/performance receipt alone therefore must not be summarized as “all acceptance
checks passed” or a successful 60 fps native performance result. The corrected
final-build short run is recorded separately below.

**Frame scheduling needed another correction.** A 10 fps slot measured about
**9.31 native fps**, while peers remained around **51 fps**, showing that one
low-rate slot did not force every client to 10 fps. However, the former scheduler
quantized a 30 fps request to approximately **22.6 fps**, which failed that
rate check. The latest implementation accumulates per-slot deadlines to avoid
that quantization. Its compilation, independent review and pure scheduler tests
passed; the final real 30 fps verification is recorded separately below.

## Final-build native and lifecycle results

After rebuilding and preparing the corrected phase-accumulating scheduler, a
**six-second actual frame-counter difference window** measured all four native
clients at **59.91–60.07 fps**, with encoded rates in the same range. Changing
only the first slot to a 30 fps target produced **29.95 native fps / 29.95 encoded
fps**; the three peers continued at **59.91–60.08 native fps** and approximately
**60.08 encoded fps**. The focused exercise passed **10 checks**, then restored
all four slots to **1280×720 / 60 fps / 6000 kbps / adaptive=true**, with no held
keys.

These measurements establish that the final scheduler reached the requested
rates in this short four-client test. They do not erase the earlier failed
receipts, establish sustained performance under every scene/load, or certify
maximum capacity.

An earlier four-slot instance, before the final scheduler rebuild but using the
same cleanup implementation, passed a normal-close exercise with **13 checks**
and independent review of the cleanup results. After each slot closed, surviving slots continued
frames, audio and input acknowledgements without changing network identity.
Final shutdown returned an actual **OS exit code 0**; all **four Vulkan
instances and four SDL windows were really released**, deferred queues were
empty, all four native/audio cleanup acknowledgements completed, all OpenAL
device/context handles were zero and all samplers had joined. The owned host,
media and supervisor exited while the separately owned test server remained
running.

The shutdown still uses the explicit policy that **skips the final global
`SDL_Quit`** after those cleanup gates pass. It does not establish that upstream
SDL shutdown or the unresolved early-startup abort path has been repaired.

The final recorded source checkpoint passed **107 Python tests** and **52 Node
contract tests**, with complete JavaScript syntax checks. The Java 25
host compiled; static adaptation checked 21 selected classes in both audio
modes, and graphics, synthetic audio and scheduler checks completed successfully.
Those contracts/checks are supplementary to the live observations above.

## Final browser and access-control results

After reloading the final frontend, all four real browser tabs retained their
correct identities and native slot mappings. Each tab's parent-page statistics
reported **1280×720**, the **WebCodecs VideoDecoder** path, measured Mbps and
decoded FPS; each canvas decoded thousands of additional frames. There was no
state-message error. The earlier iframe-generation routing problem was fixed by
using non-secret URL-fragment generation metadata rather than relying on
`window.name` surviving navigation.

Logging the third tab out left the first, second and fourth tabs on their
original identities with continuously increasing decoded-frame counts. The
third tab then selected its local key again, signed locally, chose its identity
and reconnected successfully, restoring its own picture. Revisiting all four
accounts confirmed that account IDs and native slots still matched. These were
actual browser interactions with the single-player UI, not only synthetic HTTP
sessions.

An actual authenticated HTTPS policy exercise lowered the new pool's video
bitrate cap to **4000 kbps** and observed **all four effective configurations at
4000 kbps**. A non-OP policy write was rejected with **HTTP 403**. The OP restored
the original **20000 kbps cap**, and all four stream configurations were returned
to **1280×720 / 60 fps / 6000 kbps / adaptive=true**. The effective configuration
acknowledgements are separate from measured content-dependent wire throughput.

The four identities, four decoded views, one-tab logout/re-authentication,
parent-page live statistics and isolated menu action are therefore confirmed.
Native scheduler and normal-close results above also received independent
review. Raw browser/authentication receipts and screenshots remain private.

## Remaining validation boundaries

- **Pointer capture on the new release build was not reverified in an external
  browser.** The in-app browser explicitly reports Pointer Lock unavailable.
  The earlier M1 Edge exercise included human confirmation that capture worked
  and F8 released it; that remains historical evidence for the earlier build.
  Source contracts cover independent capture/fullscreen behavior, but are not a
  replacement for a fresh supported-browser interaction check.
- **Four simultaneous release streams have not received a human speaker
  playback confirmation.** Four native/transport audio paths were isolated; those observations do not establish
  what a user actually heard from their speakers. The earlier two-stream M1
  sound confirmation is historical evidence only.
- **Abort during partial GPU startup remains unsafe on the tested stack.** The
  initial pipeline-compilation cleanup race remains unresolved, even though
  steady-state operation and normal gated shutdown passed their later checks.
- **The measured performance window was short and used four clients.** It is
  not a sustained-load or maximum-capacity benchmark. No claim is made for
  16-client throughput, arbitrary scenes, broader host/browser platforms,
  shared GPU scenes, ray tracing or energy savings.

## Reproduce the evidence categories

In an isolated owned test pool, the public checker can first collect a read-only
aggregate summary:

```sh
python3 scripts/pool_acceptance.py --config multicraft.json --min-sessions 4 \
  --summary-output .runtime/release-validation/native-summary.json
```

Only add `--exercise` when the test pool is intended to receive bounded movement
and inventory input. It does not start services or accept a server EULA. Keep raw
receipts private and review any aggregate output before attaching it to a public
issue. Use the [browser checklist](testing.md) for independent identity, actual
playback, pointer/fullscreen, GUI mapping and stale-controller tests.

The source archive uses a positive allowlist and includes the owned scheduler
Java sources and acceptance checker; it excludes downloaded games, native
binaries, runtime state, keys and original internal evidence. Source contracts
and the public-source audit remain distinct from the native/browser results on
this page.
