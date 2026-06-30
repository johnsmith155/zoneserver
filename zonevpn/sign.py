"""Ed25519 signing for the published server list (anti-spoofing).

The mobile app embeds the *public* key and refuses any list whose signature
doesn't verify. That way, even if someone discovers the gist URL or MITMs the
connection, they cannot feed the app a poisoned/fake server list — they don't
have the private key.

Signed payload format (what the gist stores when signing is enabled):

    base64( utf8( {
        "v":   1,
        "data": "<base64 of the minified payload JSON>",
        "sig":  "<base64 Ed25519 signature over the raw payload JSON bytes>"
    } ) )

The app base64-decodes the gist, sees `data`+`sig`, verifies `sig` against the
decoded `data` bytes, then parses `data` as the usual JSON payload.
"""

from __future__ import annotations

import base64
import json
from typing import Optional, Tuple

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - cryptography not installed
    _HAVE_CRYPTO = False


def generate_keypair() -> Tuple[str, str]:
    """Return (private_key_b64, public_key_b64). Keep the private one SECRET;
    paste the public one into the app's SecretEndpoint.signingPublicKeyB64."""
    _require_crypto()
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    pub_raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()


def _sign(private_key_b64: str, message: bytes) -> bytes:
    _require_crypto()
    priv = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(private_key_b64)
    )
    return priv.sign(message)


def build_signed_content(payload: dict, private_key_b64: str) -> str:
    """Produce the exact (base64) string to store in the gist for a signed list.
    Mirrors the app's RemoteConfigSource verification logic."""
    inner = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    sig = _sign(private_key_b64, inner)
    envelope = {
        "v": 1,
        "data": base64.b64encode(inner).decode("ascii"),
        "sig": base64.b64encode(sig).decode("ascii"),
    }
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def signing_available() -> bool:
    return _HAVE_CRYPTO


def _require_crypto() -> None:
    if not _HAVE_CRYPTO:
        raise SystemExit(
            "The 'cryptography' package is required for signing.\n"
            "Install it:  ./venv/bin/pip install 'cryptography>=42'"
        )


def load_private_key(cfg: dict) -> Optional[str]:
    """Read the signing private key from config (inline or file path)."""
    inline = cfg.get("ed25519_private_key")
    if inline:
        return inline.strip()
    path = cfg.get("ed25519_private_key_file")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return None
    return None
