#!/usr/bin/env python3
"""Generate an Ed25519 key pair for signing the ZoneVPN server list.

Run once on your own machine (NOT on a shared server):

    python generate_keys.py

Then:
  1. Put the PRIVATE key into the server's config.json:
         "ed25519_private_key": "<private-b64>"
     (or save it to a file and use "ed25519_private_key_file": "/path/key").
     Keep it secret — anyone with it can sign a fake list.
  2. Put the PUBLIC key into the Flutter app:
         lib/core/config/secret_endpoint.dart  ->  signingPublicKeyB64
     and set AppConfig.requireSignature = true.
  3. Restart the service:  sudo systemctl restart zonevpn
"""

from zonevpn.sign import generate_keypair


def main() -> None:
    priv, pub = generate_keypair()
    print("=" * 70)
    print("PRIVATE KEY (server config.json -> ed25519_private_key) — KEEP SECRET")
    print(priv)
    print("-" * 70)
    print("PUBLIC KEY (app -> SecretEndpoint.signingPublicKeyB64)")
    print(pub)
    print("=" * 70)


if __name__ == "__main__":
    main()
