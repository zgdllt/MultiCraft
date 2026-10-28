"""Browser-bound public-key challenges. No private key reaches this module."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class AuthError(ValueError):
    pass


def b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthError("无效的 Base64 数据") from exc


def normalize_public_key(value: dict | str) -> tuple[str, bytes, object, str]:
    """Return canonical SSH fingerprint/blob, loaded key, and signature scheme."""
    try:
        if isinstance(value, str):
            key = serialization.load_ssh_public_key(value.encode("ascii"))
            scheme = "sshsig"
        elif isinstance(value, dict) and set(value) == {"format", "key"} and value["format"] == "spki":
            key = serialization.load_der_public_key(b64decode(value["key"]))
            if not isinstance(key, Ed25519PublicKey):
                raise AuthError("网页专用密钥必须为 Ed25519")
            scheme = "ed25519"
        else:
            raise AuthError("不支持的公钥格式")
        ssh = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        blob = b64decode(ssh.decode("ascii").split()[1])
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        return fingerprint, blob, key, scheme
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm) as exc:
        raise AuthError("公钥格式无效") from exc


def verify_sshsig(signature: str, payload: bytes, expected_blob: bytes, key: object) -> None:
    """Check the embedded public key before delegating SSHSIG verification to OpenSSH."""
    if not isinstance(signature, str) or len(signature) > 16384:
        raise AuthError("SSHSIG 签名无效")
    try:
        lines = signature.strip().splitlines()
        if lines[0] != "-----BEGIN SSH SIGNATURE-----" or lines[-1] != "-----END SSH SIGNATURE-----":
            raise AuthError("需要 OpenSSH SSHSIG 签名")
        raw = b64decode("".join(lines[1:-1]))
        if raw[:6] != b"SSHSIG" or struct.unpack(">I", raw[6:10])[0] != 1:
            raise AuthError("SSHSIG 版本无效")
        size = struct.unpack(">I", raw[10:14])[0]
        if size > 8192 or raw[14:14 + size] != expected_blob:
            raise AuthError("签名内公钥与登录挑战不匹配")
        ssh = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        with tempfile.TemporaryDirectory(prefix="multicraft-verify-") as directory:
            allowed = Path(directory) / "allowed"
            sig = Path(directory) / "signature"
            allowed.write_text(f'multicraft namespaces="mc-cloud-login" {ssh}\n')
            sig.write_text(signature)
            result = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "multicraft",
                 "-n", "mc-cloud-login", "-s", str(sig)],
                input=payload, capture_output=True, timeout=5, check=False,
            )
            if result.returncode:
                raise AuthError("签名验证失败")
    except (IndexError, struct.error, OSError, subprocess.TimeoutExpired) as exc:
        raise AuthError("SSHSIG 验证失败") from exc


@dataclass
class Challenge:
    browser: str
    fingerprint: str
    public_blob: bytes
    key: object
    scheme: str
    payload: bytes
    expires: float
    auth_epoch: int


class AuthManager:
    def __init__(self, accounts_file: Path, service_origin: str, clock: Callable[[], float] = time.time):
        self.accounts_file = Path(accounts_file)
        self.origin = service_origin
        self.clock = clock
        self.browsers: dict[str, dict] = {}
        self.challenges: dict[str, Challenge] = {}
        self.preauth: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self._registry()

    def _registry(self) -> tuple[list[dict], str | None]:
        data = json.loads(self.accounts_file.read_text())
        accounts = data.get("accounts", [])
        operator = data.get("operatorAccountId")
        ids = [a["id"] for a in accounts]
        if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids):
            raise AuthError("账号注册表 ID 无效")
        if any(type(a.get("enabled", True)) is not bool for a in accounts):
            raise AuthError("账号 enabled 必须为布尔值")
        operator_user = data.get("operatorLinuxUser", "asta")
        if not isinstance(operator_user, str) or not operator_user:
            raise AuthError("operatorLinuxUser 必须为受控 Linux 用户名")
        if operator and not any(a["id"] == operator and a.get("linuxUser") == operator_user for a in accounts):
            raise AuthError("OP 必须绑定配置的受控 Linux 账号")
        return accounts, operator

    def _prune(self) -> None:
        now = self.clock()
        self.challenges = {k: v for k, v in self.challenges.items() if v.expires > now}
        for table in (self.browsers, self.preauth, self.sessions):
            for key in [k for k, v in table.items() if v["expires"] <= now]:
                del table[key]

    def browser(self, browser_id: str | None) -> tuple[str, str]:
        self._prune()
        if browser_id not in self.browsers:
            if len(self.browsers) >= 2048:
                raise AuthError("登录尝试过多，请稍后重试")
            browser_id = secrets.token_urlsafe(32)
            self.browsers[browser_id] = {"csrf": secrets.token_urlsafe(32), "authEpoch": 0, "expires": self.clock() + 600}
        return browser_id, self.browsers[browser_id]["csrf"]

    def check_browser(self, browser_id: str | None, csrf: str | None) -> str:
        self._prune()
        state = self.browsers.get(browser_id or "")
        if not state or not csrf or not secrets.compare_digest(state["csrf"], csrf):
            raise AuthError("浏览器状态或 CSRF 校验失败，请刷新页面")
        return browser_id

    def issue(self, browser_id: str, public_key: dict | str) -> dict:
        self._prune()
        if browser_id not in self.browsers:
            raise AuthError("浏览器状态无效")
        if len(self.challenges) >= 1024 or sum(c.browser == browser_id for c in self.challenges.values()) >= 8:
            raise AuthError("待验证挑战过多")
        fingerprint, blob, key, scheme = normalize_public_key(public_key)
        attempt = secrets.token_urlsafe(24)
        expiry = int(self.clock()) + 120
        payload = json.dumps({"version": 1, "purpose": "mc-cloud-login", "service": self.origin,
                              "nonce": secrets.token_urlsafe(32), "attemptId": attempt, "expiresAt": expiry},
                             separators=(",", ":"), sort_keys=True).encode()
        self.challenges[attempt] = Challenge(browser_id, fingerprint, blob, key, scheme, payload, expiry,
                                             self.browsers[browser_id]["authEpoch"])
        return {"attemptId": attempt, "challengeBase64": base64.b64encode(payload).decode(),
                "fingerprint": fingerprint, "scheme": scheme, "expiresAt": expiry,
                "service": self.origin, "publicKey": public_key}

    def _matches(self, fingerprint: str) -> list[dict]:
        accounts, _ = self._registry()
        return [a for a in accounts if a.get("enabled", True) is True and any(
            normalize_public_key(k)[0] == fingerprint for k in a.get("publicKeys", []))]

    def _public(self, account: dict) -> dict:
        _, operator = self._registry()
        return {"id": account["id"], "linuxUser": account.get("linuxUser"),
                "displayName": account.get("displayName", account.get("linuxUser", account["id"])),
                "minecraftUsername": account.get("minecraftUsername"),
                "minecraftUuid": account.get("minecraftUuid"), "isOp": account["id"] == operator}

    def _session(self, browser_id: str, account: dict, fingerprint: str) -> dict:
        self._invalidate_pending(browser_id)
        for token in [t for t, s in self.sessions.items() if s["browser"] == browser_id]:
            del self.sessions[token]
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"browser": browser_id, "accountId": account["id"],
                                "fingerprint": fingerprint, "expires": self.clock() + 43200}
        self.browsers[browser_id]["expires"] = self.clock() + 43200
        return {"sessionToken": token, "account": self._public(account)}

    def _invalidate_pending(self, browser_id: str) -> None:
        if browser_id in self.browsers:
            self.browsers[browser_id]["authEpoch"] += 1
        self.challenges = {k: c for k, c in self.challenges.items() if c.browser != browser_id}
        self.preauth = {k: s for k, s in self.preauth.items() if s["browser"] != browser_id}

    def consume_challenge(self, browser_id: str, attempt_id: str) -> Challenge:
        self._prune()
        challenge = self.challenges.get(attempt_id)
        if not challenge or challenge.browser != browser_id:
            raise AuthError("挑战已失效或不属于当前浏览器")
        del self.challenges[attempt_id]
        return challenge

    @staticmethod
    def check_signature(challenge: Challenge, signature: str) -> None:
        try:
            if challenge.scheme == "ed25519":
                challenge.key.verify(b64decode(signature), challenge.payload)
            else:
                verify_sshsig(signature, challenge.payload, challenge.public_blob, challenge.key)
        except Exception as exc:
            raise AuthError("签名验证失败") from exc

    def finish_verified(self, browser_id: str, challenge: Challenge) -> dict:
        self._prune()
        if (challenge.browser != browser_id or browser_id not in self.browsers
                or challenge.expires <= self.clock()
                or challenge.auth_epoch != self.browsers[browser_id]["authEpoch"]):
            raise AuthError("挑战或浏览器状态已过期")
        matches = self._matches(challenge.fingerprint)
        if not matches:
            raise AuthError("该公钥没有已启用的登录授权")
        if len(matches) == 1:
            return self._session(browser_id, matches[0], challenge.fingerprint)
        preauth_id = secrets.token_urlsafe(32)
        self.preauth[preauth_id] = {"browser": browser_id, "fingerprint": challenge.fingerprint,
                                   "accountIds": [a["id"] for a in matches], "expires": self.clock() + 120}
        return {"preAuthId": preauth_id, "accounts": [self._public(a) for a in matches]}

    def verify(self, browser_id: str, attempt_id: str, signature: str) -> dict:
        challenge = self.consume_challenge(browser_id, attempt_id)
        self.check_signature(challenge, signature)
        return self.finish_verified(browser_id, challenge)

    def select(self, browser_id: str, preauth_id: str, account_id: str) -> dict:
        self._prune()
        state = self.preauth.get(preauth_id)
        if not state or state["browser"] != browser_id or account_id not in state["accountIds"]:
            raise AuthError("身份选择状态无效")
        account = next((a for a in self._matches(state["fingerprint"]) if a["id"] == account_id), None)
        if not account:
            raise AuthError("账号授权已撤销")
        del self.preauth[preauth_id]
        return self._session(browser_id, account, state["fingerprint"])

    def authorize(self, browser_id: str | None, session_id: str | None) -> dict:
        self._prune()
        state = self.sessions.get(session_id or "")
        if not state or state["browser"] != browser_id:
            raise AuthError("请先登录")
        account = next((a for a in self._matches(state["fingerprint"]) if a["id"] == state["accountId"]), None)
        if not account:
            self.sessions.pop(session_id, None)
            raise AuthError("账号授权已撤销")
        return self._public(account)

    def logout(self, session_id: str | None) -> None:
        state = self.sessions.pop(session_id or "", None)
        if state:
            self._invalidate_pending(state["browser"])
            if state["browser"] in self.browsers:
                self.browsers[state["browser"]]["expires"] = self.clock() + 600
