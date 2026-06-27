"""Publish the result JSON to a GitHub Gist."""

from __future__ import annotations

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


def publish(token: str, gist_id: str, filename: str, payload: dict) -> bool:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    return update_gist(token, gist_id, filename, content)
