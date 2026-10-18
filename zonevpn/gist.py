"""Publish the result JSON to a GitHub Gist."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

import requests

log = logging.getLogger("zonevpn.gist")

_API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def create_gist(token: str, filename: str, content: str, description: str) -> Optional[str]:
    """Create a new public gist and return its id."""
    payload = {
        "description": description,
        "public": True,
        "files": {filename: {"content": content}},
    }
    resp = requests.post(_API + "/gists", headers=_headers(token), json=payload, timeout=30)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    log.error("create gist failed: %s %s", resp.status_code, resp.text[:200])
    return None


def update_gist(token: str, gist_id: str, filename: str, content: str) -> bool:
    payload = {"files": {filename: {"content": content}}}
    # Replacing one file with identical content is safe to retry after a lost
    # response. Keep the retry bounded so publication fits the five-minute
    # cycle, and return False on failure so the mirror still gets its chance.
    for attempt in range(2):
        try:
            resp = requests.patch(
                f"{_API}/gists/{gist_id}", headers=_headers(token),
                json=payload, timeout=(5, 15),
            )
            if resp.status_code == 200:
                return True
            log.warning("update gist attempt %d failed: HTTP %s",
                        attempt + 1, resp.status_code)
            if resp.status_code not in (500, 502, 503, 504):
                return False  # auth/rate limits wait for the next cycle
        except requests.RequestException as exc:
            log.warning("update gist attempt %d failed: %s",
                        attempt + 1, type(exc).__name__)
        if attempt == 0:
            time.sleep(1)
    log.error("update gist failed after two attempts")
    return False


def raw_url(gist_id: str, filename: str) -> str:
    return f"https://gist.githubusercontent.com/raw/{gist_id}/{filename}"


def publish(token: str, gist_id: str, filename: str, payload: dict,
            base64_encode: bool = False,
            sign_key_b64: Optional[str] = None) -> bool:
    """Publish the payload.

    - When [sign_key_b64] is provided, the gist stores a *signed envelope*
      (base64) the app can cryptographically verify (Ed25519). This is the
      strongest anti-spoofing option and takes priority over base64_encode.
    - Else when [base64_encode] is set, the gist stores a single base64 string
      of the (compact) JSON so the content isn't obvious at a glance. The mobile
      app must base64-decode -> utf-8 -> JSON.
    - Else the gist stores readable, indented JSON.
    """
    return update_gist(token, gist_id, filename,
                       render(payload, base64_encode, sign_key_b64))


def render(payload: dict, base64_encode: bool = False,
           sign_key_b64: Optional[str] = None) -> str:
    """The exact text the gist stores, so a mirror can serve the same bytes."""
    if sign_key_b64:
        from . import sign as _sign
        return _sign.build_signed_content(payload, sign_key_b64)
    if base64_encode:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")
    return json.dumps(payload, ensure_ascii=False, indent=2)
