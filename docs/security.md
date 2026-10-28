# Identity, browser tabs and private controls

The browser gateway authenticates access to an OP-managed client pool. Its
public-key login is independent of Minecraft's own Microsoft authentication.
The current native adapter does not implement Microsoft account login.

## Public-key login

The operator registers public keys against enabled identities. A local private
key signs a short-lived, random, origin-bound challenge; the server checks the
signature and grants only the chosen matching identity. A key matching several
accounts leads to an explicit choice, not merged privileges. Sharing a key with
an operator identity does not make every other matching account an operator.

Existing OpenSSH keys use `ssh-keygen -Y sign` locally and the project helper;
the server checks SSHSIG against the registered public key and namespace.
Browser-specific Ed25519 keys use WebCrypto locally. The server receives the
public key, challenge and signature, never the private key. Do not paste a
private SSH key into the public-key field, upload it to the host or put it into
an issue/report.

The registry declares one `operatorAccountId` and its controlled
`operatorLinuxUser` (default `asta`). An enabled account maps to one unique
`nativeSession`; the browser cannot choose an arbitrary native session ID.
Accounts and authorizations are rechecked rather than granting permanent access
from an old successful login. The registry is operator-owned, not a public API
for OS user creation or shell access.

## Tab-scoped login state

Each independent entry allocates an unpredictable `/t/<id>/` namespace. HTTP-only
cookies are restricted to that path, and REST, input WebSockets and media iframe
requests remain under the same namespace. A login or logout in a newly allocated
identity tab therefore does not overwrite another tab's normal login cookies.
Use “新身份标签页” or the root URL to allocate an independent identity; copying a
full namespaced URL initially addresses the same namespace. With
BroadcastChannel support, a live-tab collision redirects the duplicate to a new
namespace; without it, duplicate control receives a lease-conflict error.

Path-scoped cookies are a login-state partition, **not a separate browser
origin/security sandbox**. Same-origin script security, trusted page delivery,
TLS and a clean browser profile still matter. Do not embed untrusted scripts
or serve arbitrary user HTML from the gateway origin. No auth secret is stored
in `localStorage` or a URL query string. Authentication and native control
credentials are distinct.

## Input and native boundary

The public gateway checks account ownership before forwarding any input or
media request. A controller lease plus runtime owner, connection epoch and
sequence fences prevents stale messages from taking over a later controller.
A disconnect releases only the owning active controller, not a newer socket.
Authentication does not expose a reflective native API or arbitrary shell RPC.

The native control API binds loopback and requires an owner-only token. The media
service exposes a permission-restricted Unix socket. Browser code receives
neither those token-file contents nor direct native/media upstream addresses.
Keep control ports and sockets private even when the HTTPS gateway is publicly
reachable.

## Operator checklist

- Use HTTPS with a certificate valid for the public origin. The human browser
  user handles any self-signed certificate decision.
- Keep runtime state, game caches, private keys, account registries and TLS keys
  outside source control, with owner-only filesystem access.
- Give only the gateway its required private sockets/configuration. Do not expose
  Minecraft's offline-mode test port or native control listener as an unauthenticated
  public service.
- Revoke or disable public-key authorizations when access ends. Keep one explicit
  OP and avoid reusing a sensitive shell private key when a dedicated game key
  is preferable.
- Treat local key JSON files as private credentials. Challenge/signature artifacts
  are short-lived authentication material and should not be pasted into public
  bug reports.
- Preserve ownership checks in lifecycle scripts. Never stop processes merely
  because their name is `java`, `pipewire` or a previous PID was recorded.

## Reporting

For a security issue, send a minimal description through a private channel to the
repository owner before publishing sensitive details. Do not attach private
keys, cookies, token files, account registries, world saves or raw runtime dumps
to public GitHub issues. Public source checks are useful guardrails, not a
substitute for reviewing the Git index and release archive.
