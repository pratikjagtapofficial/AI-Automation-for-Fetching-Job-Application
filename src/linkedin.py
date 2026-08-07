"""LinkedIn posting via the official "Share on LinkedIn" product.

Free, self-serve, no partner approval — the w_member_social scope covers
posting to the author's own feed, which is the only thing this module
does. See SPEC.md for the one-time OAuth setup PJ runs by hand; this
module only ever holds the access token that flow produces.

Tokens from the standard (non-MDP) authorization-code flow expire ~60
days after issue and carry no refresh_token, so LinkedIn gives no way to
renew automatically — re-auth is a manual, periodic chore. token_status()
exists so that shows up as a Telegram warning before posting starts
failing outright, the same "surface it, don't fail silently" principle
the job pipeline uses for expiring Naukri/Indeed session cookies.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

# LinkedIn versions its REST API by calendar month (YYYYMM) and expects
# the header on every call. Override via env if this drifts stale rather
# than editing code.
API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202506")
BASE = "https://api.linkedin.com"


def _token() -> str:
    return os.environ.get("LINKEDIN_ACCESS_TOKEN", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "LinkedIn-Version": API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def whoami() -> tuple[str, str]:
    """-> (person_urn, error).

    Resolves via the OpenID Connect userinfo endpoint so PJ never has to
    compute his own URN by hand during setup. Cache the result in
    LINKEDIN_PERSON_URN once known — this hits the network every call
    otherwise.
    """
    token = _token()
    if not token:
        return "", "LINKEDIN_ACCESS_TOKEN not set"
    try:
        r = httpx.get(f"{BASE}/v2/userinfo",
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=20.0)
        if r.status_code != 200:
            return "", f"HTTP {r.status_code}: {r.text[:200]}"
        sub = r.json().get("sub", "")
        if not sub:
            return "", "userinfo returned no 'sub'"
        return f"urn:li:person:{sub}", ""
    except Exception as e:
        return "", type(e).__name__


@dataclass
class PostResult:
    ok: bool
    post_id: str = ""
    error: str = ""


def post_text(text: str, author_urn: str | None = None) -> PostResult:
    """Publish to the author's own feed. Never raises.

    Nothing calls this without a prior Telegram approval tap — see
    social_bot.py's callback handler. This function has no concept of
    approval itself; it posts whatever text it is given, immediately.
    """
    token = _token()
    if not token:
        return PostResult(ok=False, error="LINKEDIN_ACCESS_TOKEN not set")

    author = author_urn or os.environ.get("LINKEDIN_PERSON_URN", "")
    if not author:
        author, err = whoami()
        if err:
            return PostResult(ok=False,
                              error=f"could not resolve author: {err}")

    body = {
        "author": author,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    try:
        r = httpx.post(f"{BASE}/rest/posts", headers=_headers(),
                       json=body, timeout=30.0)
    except Exception as e:
        return PostResult(ok=False, error=type(e).__name__)

    if r.status_code not in (200, 201):
        return PostResult(ok=False,
                          error=f"HTTP {r.status_code}: {r.text[:300]}")

    post_id = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id", "")
    return PostResult(ok=True, post_id=post_id)


def token_status() -> str:
    """Human-readable expiry check for /status.

    Best-effort: LinkedIn's standard flow gives no refresh_token, so this
    env-stored expiry timestamp (set by hand at OAuth time) is the only
    early warning available before posting fails outright.
    """
    expires_at = os.environ.get("LINKEDIN_ACCESS_TOKEN_EXPIRES_AT", "")
    if not expires_at:
        return "unknown — LINKEDIN_ACCESS_TOKEN_EXPIRES_AT not set"
    try:
        remaining_days = (float(expires_at) - time.time()) / 86400
    except ValueError:
        return "unknown — LINKEDIN_ACCESS_TOKEN_EXPIRES_AT is not a number"
    if remaining_days < 0:
        return f"EXPIRED {abs(remaining_days):.0f}d ago — re-run the OAuth flow"
    if remaining_days < 7:
        return f"expires in {remaining_days:.0f}d — re-auth soon"
    return f"ok — {remaining_days:.0f}d remaining"
