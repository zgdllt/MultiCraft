# Third-party software and content

MultiCraft's own source is MIT licensed. That license does **not** relicense the
software below. No Minecraft JAR, game asset, native library, dependency wheel,
GPU driver or certificate is included in this source repository or its source
archive. Install/provide dependencies separately and retain their upstream
notices when redistributing any runtime or container of your own.

## Direct Python runtime

The versions in `requirements.txt` match the alpha integration environment.
Their transitive dependencies remain third-party too; a local package manager
should record the complete resolved environment for a particular deployment.
`requirements-test.txt` is the smaller CPU-only contract-test environment.

| Component | Pinned version | Use | Upstream license and source |
| --- | --- | --- | --- |
| Selkies | 2.1.0 | Existing M0 media integration and its native capture dependency stack | MPL-2.0 · [source](https://github.com/selkies-project/selkies) · [pinned package metadata](https://pypi.org/project/selkies/2.1.0/) |
| pixelflux | 2.2.1 | Private Wayland compositor, output management, GPU capture and H.264 encoding | MPL-2.0 · [source](https://github.com/selkies-project/pixelflux) · [pinned package](https://pypi.org/project/pixelflux/2.2.1/) |
| PCMflux | 2.2.0 | Installed M0 audio integration dependency; the public pool currently transports native loopback PCM directly | MPL-2.0 · [source](https://github.com/selkies-project/pcmflux) · [pinned package](https://pypi.org/project/pcmflux/2.2.0/) |
| aiohttp | 3.14.5 | HTTP / HTTPS and WebSocket transport | Apache-2.0 AND MIT in this release's package metadata · [source](https://github.com/aio-libs/aiohttp) · [pinned package](https://pypi.org/project/aiohttp/3.14.5/) |
| cryptography | 50.0.2 | Public-key parsing and Ed25519 signature verification | Apache-2.0 OR BSD-3-Clause · [source](https://github.com/pyca/cryptography) · [pinned package](https://pypi.org/project/cryptography/50.0.2/) |
| pulsectl-asyncio | 1.3.2 | Async management of the private PulseAudio graph | MIT · [source](https://github.com/mhthies/pulsectl-asyncio) · [pinned package](https://pypi.org/project/pulsectl-asyncio/1.3.2/) |

Package licenses above were checked against the exact PyPI version metadata;
they do not describe every codec or system library linked into a native wheel.
For example, a pixelflux build can link software encoders with additional license
terms. Follow the [upstream build and dependency notices](https://github.com/selkies-project/pixelflux)
for the exact wheel/build you deploy. MultiCraft does not modify the installed
third-party packages.

## Game and Java runtime

| Component | Required or pinned version | Use and license/source |
| --- | --- | --- |
| Minecraft Java Edition client | **26.3**, SHA-256 `4508d006323f24fa02876310c192d739af56516eb259000ac50f0909a68c9a2d` | Proprietary Mojang / Microsoft software and game content. Obtain it separately through the official version metadata. Subject to the [Minecraft EULA](https://www.minecraft.net/en-us/eula) and applicable [usage guidelines](https://www.minecraft.net/en-us/usage-guidelines). |
| Minecraft dedicated server | Optional 26.3 private demo server | Proprietary; separately obtained, independently configured and not included. Starting a new demo server requires the operator's explicit EULA acceptance. Existing servers do not need this project to replace their software. |
| Java JDK | **25**; integration used Temurin 25.0.4.1+1 | JDK compiler, runtime and Class-File API. OpenJDK code is generally GPL-2.0 with the Classpath Exception; distributions include their own notices. [OpenJDK](https://openjdk.org/) · [Temurin](https://adoptium.net/temurin/releases/) |
| LWJGL, SDL, OpenAL Soft, Gson, JTracy and other game-manifest libraries | Exact artifacts/hashes selected from the official 26.3 version manifest | JNI, graphics/window/audio, JSON and tracing dependencies. Their versions are locked in the private prepared cache; they are not vendored here. Main upstream licenses include LWJGL BSD-3-Clause, SDL Zlib, OpenAL Soft LGPL-2.0-or-later, Gson Apache-2.0. Inspect the actual manifest JAR notices for the full dependency inventory. [LWJGL](https://github.com/LWJGL/lwjgl3) · [SDL](https://github.com/libsdl-org/SDL) · [OpenAL Soft](https://github.com/kcat/openal-soft) · [Gson](https://github.com/google/gson) · JTracy follows the notices of the exact `com.mojang:jtracy` manifest artifact |

`prepare` validates the selected metadata, JARs and assets before building the
owned host. The default uses an existing private cache. `prepare --download`
explicitly obtains fixed-version files from Mojang's official endpoints into a
private runtime directory. Downloading a file does not grant redistribution or
account rights. The runtime adapter transforms selected classes in memory; it
does not ship a patched copy of the game.

## Host tools and system libraries

| Component | Role | Source / license context |
| --- | --- | --- |
| NVIDIA Linux driver, Vulkan loader and NVENC | GPU rendering and video encoding | NVIDIA driver terms plus the respective Vulkan loader notices. The supported integration path requires a working NVIDIA Vulkan device; this project does not install or redistribute the driver. [NVIDIA](https://www.nvidia.com/en-us/drivers/) · [Vulkan Loader](https://github.com/KhronosGroup/Vulkan-Loader) |
| PipeWire, pipewire-pulse, WirePlumber | Private per-runtime audio graph | Primarily MIT; distribution packages carry complete notices. [PipeWire](https://gitlab.freedesktop.org/pipewire/pipewire) · [WirePlumber](https://gitlab.freedesktop.org/pipewire/wireplumber) |
| D-Bus (`dbus-run-session`) | Private session bus | AFL-2.1 OR GPL-2.0-or-later upstream dual licensing. [D-Bus](https://gitlab.freedesktop.org/dbus/dbus) |
| OpenSSH (`ssh-keygen`) | Local SSHSIG signing and server-side signature verification | BSD-style notices; [upstream](https://www.openssh.com/) |
| OpenSSL | Operator TLS certificate tooling | Apache-2.0 for current OpenSSL 3; actual distribution notices apply. [OpenSSL](https://openssl-library.org/) |
| Python | Backend and local tools | Python Software Foundation license and bundled notices. [Python](https://www.python.org/) |
| Node.js | Optional source tests / syntax checks only | MIT and bundled notices. No Node server or frontend dependency bundle is required. [Node.js](https://nodejs.org/) |
| Browser WebCodecs, WebAudio, Pointer Lock, Fullscreen | Player-side media and input | Provided by the user's browser; codec/device/platform support varies. No browser runtime is redistributed. |

## Branding

`assets/brand/logo.png` is a newly generated MultiCraft project asset. Its design
prompt and generation provenance are recorded in `assets/brand/prompt.md`. It
does not use Mojang's Minecraft logo or another project's artwork. The name is
used here for this independent project; no affiliation with similarly named
software or services is asserted.
