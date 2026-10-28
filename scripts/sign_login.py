#!/usr/bin/env python3
"""Sign a downloaded MultiCraft challenge locally with an OpenSSH key/agent.

Only the armored signature is printed. Private key content is never loaded by
Python or sent over the network. ssh-keygen may ask to unlock an encrypted key.
"""
import argparse
import base64
import json
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("challenge", help="downloaded challenge JSON")
    parser.add_argument("--key", required=True, help="local OpenSSH private key or public key backed by ssh-agent")
    args = parser.parse_args()
    with open(args.challenge) as handle:
        challenge = json.load(handle)
    if challenge.get("scheme") != "sshsig":
        parser.error("this challenge expects a browser Ed25519 signature rather than SSHSIG")
    payload = base64.b64decode(challenge["challengeBase64"], validate=True)
    details = json.loads(payload)
    if details.get("purpose") != "mc-cloud-login" or details.get("service") != challenge.get("service"):
        parser.error("challenge purpose or target service is invalid")
    print("Signing login for " + details["service"] + "\nPublic fingerprint: " + challenge["fingerprint"], file=sys.stderr)
    result = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", args.key, "-n", "mc-cloud-login"],
                            input=payload, stdout=subprocess.PIPE, check=True)
    sys.stdout.buffer.write(result.stdout)


if __name__ == "__main__":
    main()
