"""Publish the result JSON to a GitHub Gist."""

from __future__ import annotations

import base64
import json
import logging
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
    resp = requests.patch(
        f"{_API}/gists/{gist_id}", headers=_headers(token), json=payload, timeout=30
    )
    if resp.status_code == 200:
        return True
    log.error("update gist failed: %s %s", resp.status_code, resp.text[:200])
    return False


def raw_url(gist_id: str, filename: str) -> str:
    return f"https://gist.githubusercontent.com/raw/{gist_id}/{filename}"


def publish(token: str, gist_id: str, filename: str, payload: dict,
            base64_encode: bool = False) -> bool:
    """Publish the payload. When base64_encode is set, the gist stores a single
    base64 string of the (compact) JSON instead of readable JSON, so the content
    is not obvious at a glance. The mobile app must base64-decode -> utf-8 -> JSON."""
    if base64_encode:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        content = base64.b64encode(raw).decode("ascii")
    else:
        content = json.dumps(payload, ensure_ascii=False, indent=2)
    return update_gist(token, gist_id, filename, content)
