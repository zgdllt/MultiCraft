# Deploying the alpha client pool

This guide runs the public single-player browser UI over a configurable client
pool. Existing M0 / M1 probes remain developer tools; they are not the public
multi-panel landing page. All paths below are relative to the cloned repository
unless an existing external cache is explicitly configured.

## 1. Provision the host

Use a Linux x86_64 GPU host with a working NVIDIA Vulkan device, a suitable render
node, JDK 25, Python 3.12 and the exact Python dependencies in `requirements.txt`.
The native media stack also needs the host tools `dbus-run-session`, `pipewire`,
`pipewire-pulse` and `wireplumber`. See [third-party](../THIRD_PARTY.md). A modern
Chromium / Edge browser with WebCodecs H.264 decoding is the initially tested
player path. Other browsers/devices require their own checks.

MultiCraft does not install system packages or modify an existing desktop
session. An operator must provision the dependencies first, in a dedicated
virtual environment or another controlled runtime. Node 20+ is only needed for
development tests.

Set `JAVA_HOME` to an existing JDK 25, or set `javaHome` in the private config. Do
not use a normal player's `.minecraft` directory as a writable game directory.

## 2. Create a private configuration

```sh
python3 scripts/multicraft.py init
```

This creates `multicraft.json` with four `slot1`–`slot4` sessions. The file is
ignored by Git. The source template [multicraft.example.json](../config/multicraft.example.json)
shows the settings shared by the launcher and gateway.

| Field | Meaning |
| --- | --- |
| `runtimeDirectory` | Private pool state, game directories, control credentials, media sockets and logs. Default `.runtime/pool`. |
| `minecraftCache` | Optional existing, read-only, verified 26.3 cache directory. When absent, the default private cache is used. |
| `javaHome` | Optional existing JDK 25 directory; otherwise use `JAVA_HOME`. |
| `serverAddress` | The Minecraft host and port the configured clients may join. Use a server you administer or have permission to access. |
| `controlPort` | Loopback-only native control port. Never route it to the public network. |
| `render` | Initial per-session physical output width/height and GPU render node. |
| `sessions` | 1–16 fixed startup slots, each with a unique `id`, game `username`, optional UUID/display index and initial five-field `stream` configuration. |
| `accountsFile` | Private OP-managed public-key identity registry; default `.runtime/accounts.json`. |
| `policyFile` | Persisted OP stream limits; default `.runtime/stream-policy.json`. |
| `tlsCertificate`, `tlsKey` | HTTPS gateway certificate and private key. |

The slot count is an operator choice. Begin with a small pool and measure actual
GPU memory, game update rate and encoder load. A configuration limit of 16 does
not certify that every GPU can run 16 clients.

The current game adapter uses offline-derived identities and does not implement
Microsoft OAuth. Only point this alpha at an explicitly authorized offline-mode
test server. Keep that server's gameplay port private or otherwise access
controlled; the HTTPS gateway is the intended authenticated entry for players.
This does not change the authentication setting of any existing server.

## 3. Prepare owned code and private game files

```sh
python3 scripts/multicraft.py prepare --config multicraft.json
```

Without a complete cache this fails with a preparation error. To request the
fixed official download explicitly:

```sh
python3 scripts/multicraft.py prepare --config multicraft.json --download
```

The launcher verifies the pinned metadata and game files, builds MultiCraft's
own Java host using the configured JDK, and makes independent writable client
directories. When `minecraftCache` points outside this project, it is borrowed
read-only. Source releases never contain those game files or generated clients.
Preparing clients does not start a server or accept its EULA.

## 4. Register identities

Create a private registry from the example and replace **every placeholder** with
an authorized public key before starting the gateway:

```sh
mkdir -p .runtime
cp config/accounts.example.json .runtime/accounts.json
chmod 700 .runtime
chmod 600 .runtime/accounts.json
```

The template uses `asta` as the operator Linux user. `operatorAccountId` selects
exactly one OP account whose `linuxUser` must match `operatorLinuxUser`. For the
existing project deployment that operator remains `asta`; a separate deployment
may explicitly configure its own controlled operator. Other accounts do not gain
OP by sharing a key with that account.

Each enabled record maps `nativeSession` to exactly one configured slot. Do not
map two enabled accounts to one slot. `minecraftUsername` should match the
slot's configured game username. `linuxUser` is an OP-managed identity binding;
the application does not create OS accounts, grant shell access or read every
user's home directory.

`publicKeys` accepts an OpenSSH public-key string, or an Ed25519 SPKI object:
`{"format":"spki","key":"BASE64_DER_PUBLIC_KEY"}`. The example intentionally
contains placeholders and will not authenticate until edited. Store only public
keys here. Keep the corresponding private keys on each player's local machine.

Existing SSH key flow:

1. Paste the registered OpenSSH **public** key into the login page and download
   its short-lived challenge JSON.
2. On the player's own computer, sign that challenge:

   ```sh
   python3 scripts/sign_login.py challenge.json --key ~/.ssh/id_ed25519
   ```

3. Check the target origin and fingerprint shown by the helper, then paste the
   resulting SSH signature into the same tab.
4. If the key matches several enabled identities, select one explicitly.

The optional game-specific Ed25519 JSON flow signs locally through WebCrypto.
Its `publicKey` is SPKI DER Base64 and `privateKey` is PKCS8 DER Base64. Such a
file is a private credential: do not add it to this repository or the server.
No SSH tunnel is needed for either login method.

## 5. Configure HTTPS

Use an operator-managed certificate valid for the gateway origin. For a local
experiment only, an existing OpenSSL installation can create a short-lived
localhost certificate:

```sh
mkdir -p .runtime/tls
openssl req -x509 -newkey rsa:3072 -sha256 -days 30 -nodes \
  -keyout .runtime/tls/server.key -out .runtime/tls/server.crt \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'
chmod 600 .runtime/tls/server.key
```

For a remote hostname/IP use a certificate with the correct subject alternative
names. The actual browser user must handle any self-signed certificate prompt;
automation should not bypass it. The configured `--origin` must match the URL
players use, including scheme and non-default port. HTTPS is required for the
browser APIs used by the player outside trusted localhost contexts.

## 6. Start the pool and product gateway

```sh
python3 scripts/multicraft.py start --config multicraft.json
python3 scripts/multicraft.py status --config multicraft.json
```

The pool starts its own private media graph/compositor and one JVM. Wait for the
actual native clients and capture streams to be ready. Then run the gateway in
a separate terminal or service supervisor:

```sh
python3 -m multicraft.release_gateway --root . --config multicraft.json \
  --origin https://localhost:18443 --port 18443
```

Expose only the HTTPS gateway. Its native upstream is loopback-only and media
upstream is a private Unix socket. Keep the runtime directory and token files
owner-only. Do not mount the native control API or media socket into an
untrusted web container.

## 7. Test several identities

Open the root URL for the first identity. Use “新身份标签页” for each additional
identity; each gets the normal one-player interface. A reload of the current
page keeps its current identity. Copying the full `/t/.../` URL initially addresses the same tab namespace.
When BroadcastChannel is available, an already-live tab detects the duplicate
and the new tab allocates a new namespace. Without that browser support, a
duplicate controller receives a lease-conflict error. Use the new-identity entry
to obtain an independent login directly.
These namespaces separate login state, not browser origins; tabs in one browser
profile must still be treated as trusted same-origin pages.

Use at least four distinct accounts/slots for a multi-instance check. Move one
player while the others remain still; open a menu in one, inventory in another;
change one stream configuration; log one tab out and reconnect it. Check that
other tabs keep their identities, controls, video and audio. See the complete
[evidence checklist](testing.md).

“进入游戏 / 捕获鼠标” requests pointer lock. It does not request fullscreen.
The independent fullscreen button controls presentation only. **F8** releases
held input and pointer capture without leaving fullscreen; **F9** sends the
Minecraft menu action. Browser Esc remains subject to browser security rules.

## 8. Stream limits and shutdown

Players may set width/height, target fps, video kbps and `adaptive` within the
current OP limits. Width/height must be even. The public release hard limits
are 3840×2160, 1000 fps and 50000 video kbps; the default OP policy allows
1920×1080, 1000 fps and 20000 kbps. These are request ceilings, not achieved rates. The OP form updates global limits;
lowering a limit must first apply and acknowledge affected streams before the
new policy revision is committed. Failed application must remain visible.
Startup configurations are also clamped to the persisted policy. If an OP limit
change alters those effective initial settings, stop the pool and run `prepare`
again before `start`; the launcher rejects a stale preparation fingerprint.
Preparation records from an older launcher must likewise be regenerated.
Actual measured game FPS and browser decoded/received rates are different from
the allowed target.

Resolution changes resize only the owning native virtual output/capture view,
using a reserved grid cell. Bitrate and FPS use native encoder updates. Adaptive
mode uses validated receiver feedback to adjust the bitrate below the user's
requested ceiling. Do not call a silent stream or configuration receipt a
successful browser-playback test.

```sh
python3 scripts/multicraft.py stop --config multicraft.json --role pool
```

`--role pool` preserves an owned optional demo server. Omitting it uses the
default `all` role, which also stops only the launcher-owned demo server; an
external configured server is never stopped. Stop only owned processes; do not
terminate unrelated Java/media processes. A
normal disconnect can reconnect, but a terminally closed slot requires a pool
restart to recreate. Preserve a failed shutdown receipt and native crash logs
for diagnosis. The current controlled `process-exit` strategy is explained in
[architecture](architecture.md); a clean exit does not claim upstream
`SDL_Quit` itself is fixed.
