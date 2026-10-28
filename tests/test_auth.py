import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from multicraft.auth import AuthError, AuthManager


def public(key):
    raw = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return {"format": "spki", "key": base64.b64encode(raw).decode()}


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "accounts.json"
        self.key = Ed25519PrivateKey.generate()
        self.accounts = [self.account("ordinary", "alice"), self.account("operator", "asta")]
        self.save()
        self.now = 1000
        self.auth = AuthManager(self.path, "http://localhost:18080", lambda: self.now)
        self.browser, self.csrf = self.auth.browser(None)

    def tearDown(self):
        self.directory.cleanup()

    def account(self, account_id, linux_user):
        return {"id": account_id, "linuxUser": linux_user, "enabled": True,
                "minecraftUsername": "M0Player", "publicKeys": [public(self.key)]}

    def save(self):
        self.path.write_text(json.dumps({"operatorAccountId": "operator", "accounts": self.accounts}))

    def challenge(self):
        return self.auth.issue(self.browser, public(self.key))

    def signed(self, challenge):
        return base64.b64encode(self.key.sign(base64.b64decode(challenge["challengeBase64"]))).decode()

    def verified(self):
        challenge = self.challenge()
        return self.auth.verify(self.browser, challenge["attemptId"], self.signed(challenge))

    def test_anonymous_state_expires_quickly_but_login_keeps_its_csrf_state(self):
        anonymous, _ = self.auth.browser(None)
        verified = self.verified()
        self.auth.select(self.browser, verified["preAuthId"], "ordinary")
        self.now += 601
        self.auth._prune()
        self.assertNotIn(anonymous, self.auth.browsers)
        self.assertIn(self.browser, self.auth.browsers)

    def test_multiple_accounts_require_explicit_selection_and_do_not_merge_op(self):
        result = self.verified()
        self.assertNotIn("sessionToken", result)
        self.assertEqual(len(result["accounts"]), 2)
        selected = self.auth.select(self.browser, result["preAuthId"], "ordinary")
        self.assertFalse(selected["account"]["isOp"])
        self.assertEqual(self.auth.authorize(self.browser, selected["sessionToken"])["id"], "ordinary")

    def test_operator_selection_is_op(self):
        result = self.verified()
        self.assertTrue(self.auth.select(self.browser, result["preAuthId"], "operator")["account"]["isOp"])

    def test_signed_challenge_cannot_be_replayed_or_moved_to_other_browser(self):
        challenge = self.challenge()
        signature = self.signed(challenge)
        other, _ = self.auth.browser(None)
        with self.assertRaises(AuthError):
            self.auth.verify(other, challenge["attemptId"], signature)
        self.auth.verify(self.browser, challenge["attemptId"], signature)
        with self.assertRaises(AuthError):
            self.auth.verify(self.browser, challenge["attemptId"], signature)

    def test_challenge_expiry_and_wrong_signature(self):
        challenge = self.challenge()
        with self.assertRaises(AuthError):
            self.auth.verify(self.browser, challenge["attemptId"], base64.b64encode(b"bad").decode())
        self.now += 121
        with self.assertRaises(AuthError):
            self.auth.verify(self.browser, challenge["attemptId"], self.signed(challenge))

    def test_preauth_bound_to_browser_and_rechecks_revoked_keys(self):
        result = self.verified()
        other, _ = self.auth.browser(None)
        with self.assertRaises(AuthError):
            self.auth.select(other, result["preAuthId"], "operator")
        self.accounts[1]["publicKeys"] = []
        self.save()
        with self.assertRaises(AuthError):
            self.auth.select(self.browser, result["preAuthId"], "operator")

    def test_session_revocation_and_csrf(self):
        result = self.verified()
        selected = self.auth.select(self.browser, result["preAuthId"], "ordinary")
        self.assertEqual(self.auth.check_browser(self.browser, self.csrf), self.browser)
        with self.assertRaises(AuthError):
            self.auth.check_browser(self.browser, "wrong")
        self.accounts[0]["enabled"] = False
        self.save()
        with self.assertRaises(AuthError):
            self.auth.authorize(self.browser, selected["sessionToken"])

    def test_login_and_logout_invalidate_all_old_pending_and_inflight_proofs(self):
        earlier = self.verified()
        old_challenge = self.challenge()
        inflight = self.auth.consume_challenge(self.browser, old_challenge["attemptId"])
        latest = self.verified()
        session = self.auth.select(self.browser, latest["preAuthId"], "ordinary")
        with self.assertRaises(AuthError):
            self.auth.select(self.browser, earlier["preAuthId"], "operator")
        with self.assertRaises(AuthError):
            self.auth.finish_verified(self.browser, inflight)
        pending = self.verified()
        self.auth.logout(session["sessionToken"])
        with self.assertRaises(AuthError):
            self.auth.select(self.browser, pending["preAuthId"], "operator")

    def test_invalid_enabled_value_fails_closed(self):
        self.accounts[0]["enabled"] = "false"
        self.save()
        with self.assertRaises(AuthError):
            self.auth._matches("anything")

    def test_unknown_key_gets_no_account_candidates(self):
        unknown = Ed25519PrivateKey.generate()
        challenge = self.auth.issue(self.browser, public(unknown))
        signature = base64.b64encode(unknown.sign(base64.b64decode(challenge["challengeBase64"]))).decode()
        with self.assertRaises(AuthError):
            self.auth.verify(self.browser, challenge["attemptId"], signature)

    def test_sshsig_success_and_wrong_embedded_key_rejected(self):
        key_path = Path(self.directory.name) / "ssh-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True)
        ssh_public = key_path.with_suffix(".pub").read_text().strip()
        self.accounts[0]["publicKeys"] = [ssh_public]
        self.accounts[1]["publicKeys"] = []
        self.save()
        challenge = self.auth.issue(self.browser, ssh_public)
        payload = base64.b64decode(challenge["challengeBase64"])
        signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key_path), "-n", "mc-cloud-login"],
                                input=payload, capture_output=True, check=True).stdout.decode()
        result = self.auth.verify(self.browser, challenge["attemptId"], signed)
        self.assertEqual(result["account"]["id"], "ordinary")
        wrong = self.auth.issue(self.browser, public(self.key))
        # A valid SSHSIG from another public key cannot satisfy that challenge.
        with self.assertRaises(AuthError):
            self.auth.verify(self.browser, wrong["attemptId"], signed)


if __name__ == "__main__":
    unittest.main()
